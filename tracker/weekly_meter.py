"""The weekly limit measured directly on the seven-day meter.

Jonathan's ruling, 2026-10-05: "We can work out the weekly tokens directly without having to
measure the ratio between the five hour and the weekly limits. We can see exactly how much
weekly usage we are using by the percentage increases."

So the weekly limit is credits per 1% of the seven-day meter, read between exact one-point
seven-day crossings, and windows per week is derived from it (the weekly figure over the
five-hour window), never measured as a meter ratio and multiplied back in. On the gs
accounts this figure agrees across accounts within a few percent in every regime, where
credits per 1% of the five-hour meter does not (docs/findings/2026-10-04-account-agreement.md,
section 2c, on branch wf/139-account-agreement, whose `crossing_spans` and `meter_levels`
this lifts).

Two halves:

- the collector's (`seven_day_steps`, called by tracker/gs_passive.py `report`): every
  one-point step of an account's seven-day meter, tiled end to end, with the account's own
  tokens in it, recorded as `weekly_steps` on the account's record;
- the publisher's (`clean_steps`, `valued`, `level`, `weekly_change`): the selection the other
  figures use, each step valued as they value a stretch, pooled meter-weighted with a
  whole-UTC-day bootstrap, and the known-date test on it.

No fitted coefficient enters any figure here: a level is a sum of credits over a sum of
seven-day points.
"""
from __future__ import annotations

import bisect
import math
import random
from collections.abc import Collection, Iterable
from datetime import datetime, timezone
from statistics import stdev

from . import credits as credit_model
from .cloud_sessions import CloudSpan, overlaps
from .crossings import crossings
from .join import Stretch
from .samples import Sample
from .turns import Turn

#: Bootstrap draws behind every interval here, and the seed every draw's stream starts from
#: (a publish is reproducible: the same history gives the same intervals).
WEEKLY_BOOTSTRAP = 400
WEEKLY_SEED = 20261005
#: An account's change across a candidate needs whole UTC days of steps on each side: its
#: standard error is read off resampled days, and one day resamples to itself.
WEEKLY_MIN_DAYS = 2


def seven_day_steps(samples: Iterable[Sample], turns: Iterable[Turn], prices: dict,
                    cloud: Iterable[CloudSpan] = (), fast: Collection[str] = frozenset()) -> list[dict]:
    """Every one-point step of the seven-day meter, tiled, with the account's tokens in it.

    Within one seven-day segment (`Crossing.segment_id`: unbroken readings inside one weekly
    window), consecutive seven-day crossings a and b, one point apart, own the half-open span
    [a.upper, b.upper): each crossing is placed at the upper reading of its bracket, so the
    steps tile the segment with no gap and no overlap, and summed over a chain they hold
    exactly (last - first) seven-day points against every turn in between. A pair that
    jumped two points in one reading gives one step holding the work and one empty step at
    the same instant; the chain's total is still exact. Placing a crossing at its upper
    reading moves it by at most one sample gap, and over a chain those errors cancel except
    at its two ends. (Counting only the span strictly between the brackets instead drops the
    work of any burst whose crossings share one sample pair.)

    A five-hour crossing is counted in the step holding its own upper reading, by the same
    rule (`d5`). A turn belongs to the step whose [start, end) holds its timestamp, and is
    added as a stretch adds it (`join.Stretch.add`), so `tokens` is a stretch's shape: priced
    models and unpriced raw ids side by side. No step runs while the seven-day meter sits at
    100%: it cannot cross a point there. `cloud_session` marks a step holding a turn
    teleported from a cloud session or overlapping a recorded cloud session span, as a
    stretch is marked (tracker/gs_passive.py `_stretch_record`).
    """
    samples = sorted(samples, key=lambda s: s.ts)
    turns = sorted(turns, key=lambda t: t.ts)
    keys = [t.ts for t in turns]
    cloud = list(cloud)
    five = sorted(c.upper for c in crossings(samples, "five_hour"))
    by_seg: dict[int, list] = {}
    for c in crossings(samples, "seven_day"):
        by_seg.setdefault(c.segment_id, []).append(c)
    out = []
    for seg in by_seg.values():
        seg = sorted(seg, key=lambda c: c.value)
        for a, b in zip(seg, seg[1:]):
            if b.value - a.value != 1:
                continue
            st = Stretch(a.upper, b.upper, delta_pct=1.0)
            for t in turns[bisect.bisect_left(keys, a.upper):bisect.bisect_left(keys, b.upper)]:
                st.add(t, prices, t.id in fast)
            out.append({
                "start": a.upper.isoformat(), "end": b.upper.isoformat(), "seven_day": b.value,
                "d7": 1, "d5": bisect.bisect_left(five, b.upper) - bisect.bisect_left(five, a.upper),
                "tokens": {**st.tokens, **st.unpriced}, "unpriced_tokens": st.unpriced_tokens,
                "turns": st.turns, "remote_sourced_turns": st.remote_sourced_turns,
                "headless_tokens": st.headless_tokens, "headless_turns": st.headless_turns,
                "cloud_session": bool(st.remote_sourced_turns or overlaps(cloud, a.upper, b.upper)),
            })
    return sorted(out, key=lambda s: datetime.fromisoformat(s["start"]))


def steps_by_account(*reports: dict | None) -> dict[str, list[dict]]:
    """Every report's `accounts.<name>.weekly_steps`, merged by account name. A report written
    before the collector recorded steps has none, and its accounts are absent."""
    out: dict[str, list[dict]] = {}
    for report in reports:
        for name, body in ((report or {}).get("accounts") or {}).items():
            if body.get("weekly_steps") is not None:
                out.setdefault(name, []).extend(body["weekly_steps"])
    return out


def clean_steps(by_account: dict[str, list[dict]], runs: list[credit_model.HarnessRun],
                stretches: dict[str, list[dict]] | None = None, *,
                whole_history: bool = False) -> dict[str, list[dict]]:
    """The steps that read ordinary use, per account, as every other figure selects them.

    A step overlapping a harness run, or marked `cloud_session`, is left out
    (`credits.clean_stretches`), and masterrig enters from MASTERRIG_FROM as in the rate fits.
    So is a step overlapping one of the account's `stretches` (`credits.stretches_by_account`)
    whose `status` is not accepted, the column the known-date test selects on
    (`credits.announced_change_stretches`): the capture check found the meter moving there on
    work no transcript on this host holds, so the step's credits read low. On 13-14 September
    a2's meter moved 15 points over such stretches at 59k to 242k credits a point, against
    about 1M on its other days.

    With `whole_history` masterrig's history before MASTERRIG_FROM is read too, less
    MASTERRIG_PHANTOM (`credits.masterrig_excluded`): the 14 September before side
    (`credits.cut_direct_tests`) reads it back to the account's last certified boundary.
    """
    withheld: dict[str, list[tuple[datetime, datetime]]] = {}
    for account, rows in (stretches or {}).items():
        withheld[account] = [(datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"]))
                             for s in rows if s.get("start") and s.get("end")
                             and s.get("status") != "accepted"]
    kept: dict[str, list[dict]] = {}
    for account, steps in by_account.items():
        rows = []
        for st in steps:
            start, end = datetime.fromisoformat(st["start"]), datetime.fromisoformat(st["end"])
            if st.get("cloud_session"):
                continue
            if credit_model.overlapping_run(runs, account, start, end) is not None:
                continue
            if any(a < end and b > start for a, b in withheld.get(account, ())):
                continue
            if credit_model.masterrig_excluded(account, start, end, whole_history):
                continue
            rows.append(st)
        kept[account] = rows
    return kept


def valued(steps: list[dict], value, factor: float | None = None) -> list[dict]:
    """Each step as {start, end, day, credits, d7, d5, raw_d5}, its tokens at `value(tokens)`.

    A step `value` cannot price (a model in no family) is left out, with its seven-day point.
    A step with no tokens at all is kept at zero credits: the meter moved a point and the
    account's own transcripts spent nothing in it, which is part of what that point bought.
    `day` is the UTC day the step ends in, the unit every interval here resamples.
    With the fitted headless `factor`, `d5` is the step's five-hour points over its headless
    inflation, 1 + (factor - 1) x its headless share of credits (ADR 0001 rule 16), and
    `raw_d5` the meter's own; the seven-day point is not weighted.
    """
    out = []
    for st in steps:
        credits = value(st.get("tokens") or {})
        if credits is None:
            continue
        end = datetime.fromisoformat(st["end"])
        raw = st.get("d5", 0)
        head = value(st["headless_tokens"]) if st.get("headless_tokens") else 0.0
        share = min(max(head / credits, 0.0), 1.0) if factor and credits and head else 0.0
        out.append({"start": datetime.fromisoformat(st["start"]), "end": end,
                    "day": end.astimezone(timezone.utc).date().isoformat(), "credits": float(credits),
                    "d7": st.get("d7", 1), "d5": raw / (1 + (factor - 1) * share) if share else raw,
                    "raw_d5": raw})
    return out


def _by_day(rows: list[dict]) -> list[tuple[float, ...]]:
    """A side's steps summed per UTC day: (credits, seven-day points, five-hour points, the
    meter's own five-hour points)."""
    days: dict[str, list[float]] = {}
    for r in rows:
        acc = days.setdefault(r["day"], [0.0, 0.0, 0.0, 0.0])
        acc[0] += r["credits"]
        acc[1] += r["d7"]
        acc[2] += r["d5"]
        acc[3] += r.get("raw_d5", r["d5"])
    return [tuple(v) for _, v in sorted(days.items())]


def _resample(days: list[tuple], rng: random.Random) -> list[tuple]:
    return [rng.choice(days) for _ in days]


def _percentile_interval(draws: list[float]) -> list[float] | None:
    if not draws:
        return None
    draws = sorted(draws)
    return [draws[int(0.025 * len(draws))], draws[min(int(0.975 * len(draws)), len(draws) - 1)]]


def level(rows_by_label: dict[str, list[dict]], seed: str) -> dict:
    """Credits per 1% of the seven-day meter, pooled meter-weighted and per account.

    The pooled figure is every account's credits over every account's seven-day points, so
    an account weighs by how far its own meter moved, as the window pools. Its 95% interval
    resamples each account's whole UTC days independently (`WEEKLY_BOOTSTRAP` draws) and
    recomputes the pooled ratio; each account's own interval is read off the same draws.
    `windows_per_week_meters` is five-hour over seven-day points in the same steps, in the
    steps' unit (`valued`), for reference, never an input; `raw_windows_per_week_meters` is
    the meter's own ratio.
    """
    days = {label: _by_day(rows) for label, rows in rows_by_label.items() if rows}

    def summary(groups: list[list[tuple]]) -> dict:
        c = sum(x[0] for g in groups for x in g)
        d7 = sum(x[1] for g in groups for x in g)
        d5 = sum(x[2] for g in groups for x in g)
        raw = sum(x[3] for g in groups for x in g)
        return {"credits_per_pct": c / d7 if d7 else None, "seven_day_points": d7,
                "five_hour_points": d5, "days": sum(len(g) for g in groups),
                "windows_per_week_meters": d5 / d7 if d7 else None,
                "raw_windows_per_week_meters": raw / d7 if d7 else None}

    out = summary(list(days.values()))
    out["accounts"] = sorted(days)
    if not days:
        out.update(interval=None, per_account={})
        return out
    rng = random.Random(f"{WEEKLY_SEED}:{seed}")
    pooled, per = [], {label: [] for label in days}
    for _ in range(WEEKLY_BOOTSTRAP):
        c = d7 = 0.0
        for label, ds in days.items():
            pick = _resample(ds, rng)
            pc, p7 = sum(x[0] for x in pick), sum(x[1] for x in pick)
            c, d7 = c + pc, d7 + p7
            if p7:
                per[label].append(pc / p7)
        if d7:
            pooled.append(c / d7)
    out["interval"] = _percentile_interval(pooled)
    out["per_account"] = {label: dict(summary([ds]), interval=_percentile_interval(per[label]))
                          for label, ds in days.items()}
    return out


#: The three quantities one set of seven-day steps carries, as (numerator, denominator)
#: indices into a `_by_day` tuple: the weekly limit (credits per seven-day point), the window
#: (credits per five-hour point) and windows per week (five-hour per seven-day points). On the
#: same steps the first is the product of the other two exactly (ADR 0001 rule 20).
QUANTITIES = {"weekly": (0, 1), "window": (0, 2), "windows_per_week": (2, 1)}


def account_change(before: list[dict], after: list[dict], seed: str,
                   quantity: str = "weekly") -> dict | None:
    """One account's change in credits per 1% of the seven-day meter, against itself.

    Each side's level is its credits over its seven-day points. The log ratio's standard
    error is the spread of the log ratio over `WEEKLY_BOOTSTRAP` draws resampling each side's
    whole UTC days, and the 95% interval a t interval on it with (days before + days after -
    2) degrees of freedom. None when the before side holds fewer than
    `credit_model.ANNOUNCED_MIN_BEFORE` steps, either side fewer than `WEEKLY_MIN_DAYS` days, or a
    level or the spread is zero.

    `quantity` reads another of the steps' `QUANTITIES` the same way, on the same days (the
    window or windows per week); its draws use their own stream, so the weekly figure is the
    one this function has always returned.
    """
    db, da = _by_day(before), _by_day(after)
    if (len(before) < credit_model.ANNOUNCED_MIN_BEFORE or not after
            or len(db) < WEEKLY_MIN_DAYS or len(da) < WEEKLY_MIN_DAYS):
        return None
    num, den = QUANTITIES[quantity]

    def lvl(ds):
        d = sum(x[den] for x in ds)
        c = sum(x[num] for x in ds)
        return c / d if d and c > 0 else None

    lb, la = lvl(db), lvl(da)
    if lb is None or la is None:
        return None
    rng = random.Random(f"{WEEKLY_SEED}:{seed}" if quantity == "weekly"
                        else f"{WEEKLY_SEED}:{seed}:{quantity}")
    draws = []
    for _ in range(WEEKLY_BOOTSTRAP):
        b, a = lvl(_resample(db, rng)), lvl(_resample(da, rng))
        if b and a:
            draws.append(math.log(a / b))
    se = stdev(draws) if len(draws) > 1 else 0.0
    if se <= 0:
        return None
    d, df = math.log(la / lb), len(db) + len(da) - 2
    h = credit_model._t975(df) * se
    return {"log_ratio": d, "se": se, "df": df, "ratio": math.exp(d),
            "interval": (math.exp(d - h), math.exp(d + h)),
            "level_before": lb, "level_after": la,
            "n_before": len(before), "n_after": len(after),
            "days_before": len(db), "days_after": len(da)}


WEEKLY_CHANGE_ESTIMATOR = ("the combined change in credits per 1% of the seven-day meter, each "
                           "account against itself, weights 1 / se squared")


def weekly_change(sides: dict[str, tuple[list[dict], list[dict]]], seed: str) -> dict:
    """The known-date test on the seven-day meter: per account, combined, and plan-wide.

    `sides[label]` is that account's (before, after) valued steps. Each account is compared
    with itself (`account_change`); those with both sides are combined by inverse variance
    (`credits.combine_inverse_variance`), and the combined change is refitted with each
    account left out (`credits.inverse_variance_plan_wide`, ADR 0001 rule 9). `certified` when
    that test passes and the combined interval excludes no change.
    """
    per_account, paired = {}, {}
    for label in sorted(sides):
        before, after = sides[label]
        pair = account_change(before, after, f"{seed}:{label}")
        row = {"n_before": len(before), "n_after": len(after), "change_pct": None,
               "interval_pct": None, "combined": False}
        if pair is not None:
            paired[label] = pair
            row.update(change_pct=credit_model._pct_of(pair["ratio"]),
                       interval_pct=[credit_model._pct_of(x) for x in pair["interval"]],
                       combined=True, log_ratio=round(pair["log_ratio"], 6),
                       se=round(pair["se"], 6), df=pair["df"],
                       days_before=pair["days_before"], days_after=pair["days_after"],
                       level_before=round(pair["level_before"]),
                       level_after=round(pair["level_after"]))
        per_account[label] = row
    return credit_model.direct_change(per_account, paired, WEEKLY_CHANGE_ESTIMATOR,
                                      "credits per 1% of the seven-day meter")


RECONCILE_METHOD = (
    "The 14 September weekly change and its two factors, each account against itself, on one "
    "set of readings: the direct weekly test's own clean seven-day steps on its own sides "
    "(`credits.cut_weekly_steps`). On those steps the weekly limit (credits per seven-day "
    "point, the measured figure) is the window (credits per five-hour point, each step's "
    "five-hour points over its headless inflation, ADR 0001 rule 16) times windows per week "
    "(five-hour over seven-day points, the derived quotient) exactly, per account. Each has "
    "its own whole-UTC-day bootstrap t interval on the same days. The accounts are combined at "
    "one set of weights, the weekly figure's inverse variances, so the combined log changes "
    "add up exactly too; each combined interval is a t interval on its own weighted standard "
    "error. Each combined change is tested as every direct change is (ADR 0001 rule 9: each "
    "account left out in turn, and the interval excluding no change) (ADR 0001 rule 20).")


def reconcile(sides: dict[str, tuple[list[dict], list[dict]]], seed: str) -> dict:
    """The weekly change, the window change and the windows-per-week change on the same steps.

    `sides[label]` is that account's (before, after) valued steps, as `weekly_change` reads
    them, so the weekly figures here are its own. See RECONCILE_METHOD.
    """
    per_account, paired = {}, {}
    for label in sorted(sides):
        before, after = sides[label]
        weekly = account_change(before, after, f"{seed}:{label}")
        if weekly is None:
            continue
        rows = {"weekly": weekly}
        for q in ("window", "windows_per_week"):
            rows[q] = account_change(before, after, f"{seed}:{label}", q)
        if any(r is None for r in rows.values()):
            continue
        paired[label] = rows
        per_account[label] = {
            q: {"change_pct": credit_model._pct_of(r["ratio"]),
                "interval_pct": [credit_model._pct_of(x) for x in r["interval"]],
                "log_ratio": round(r["log_ratio"], 6), "se": round(r["se"], 6), "df": r["df"],
                "level_before": round(r["level_before"], 4 if q == "windows_per_week" else None),
                "level_after": round(r["level_after"], 4 if q == "windows_per_week" else None)}
            for q, r in rows.items()}
        per_account[label].update(
            n_before=weekly["n_before"], n_after=weekly["n_after"],
            days_before=weekly["days_before"], days_after=weekly["days_after"],
            identity_gap=round(rows["weekly"]["log_ratio"] - rows["window"]["log_ratio"]
                               - rows["windows_per_week"]["log_ratio"], 12) + 0.0)

    def combined(labels: list[str], q: str) -> dict | None:
        if not labels:
            return None
        inv = {k: 1 / paired[k]["weekly"]["se"] ** 2 for k in labels}
        total = sum(inv.values())
        w = {k: v / total for k, v in inv.items()}
        d = sum(w[k] * paired[k][q]["log_ratio"] for k in labels)
        se = math.sqrt(sum((w[k] * paired[k][q]["se"]) ** 2 for k in labels))
        h = credit_model._t975(sum(paired[k][q]["df"] for k in labels)) * se
        return {"weights": w, "log_ratio": d, "change_pct": credit_model._pct_of(math.exp(d)),
                "interval_pct": [credit_model._pct_of(math.exp(d - h)),
                                 credit_model._pct_of(math.exp(d + h))]}

    out = {"accounts": sorted(paired), "per_account": per_account, "combined": {},
           "method": RECONCILE_METHOD}
    for q in QUANTITIES:
        c = combined(sorted(paired), q)
        if c is None:
            out["combined"][q] = None
            continue
        without = {}
        for label in paired if len(paired) > 1 else ():
            rest = combined([k for k in sorted(paired) if k != label], q)
            without[label] = {"change_pct": rest["change_pct"], "interval_pct": rest["interval_pct"]}
        plan_wide = credit_model.plan_wide_verdict(c["change_pct"], sorted(paired), without,
                                                   f"{q}, each account against itself, at the "
                                                   "weekly figure's inverse-variance weights")
        lo, hi = c["interval_pct"]
        excludes = not lo <= 0 <= hi
        out["combined"][q] = {"change_pct": c["change_pct"], "interval_pct": c["interval_pct"],
                              "log_ratio": round(c["log_ratio"], 6),
                              "interval_excludes_no_change": excludes, "plan_wide": plan_wide,
                              "certified": plan_wide["state"] == "passed" and excludes}
        for label, w in c["weights"].items():
            per_account[label]["weight"] = round(w, 4)
    return out
