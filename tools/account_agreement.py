"""Why the accounts disagree on credits per 1%, and what 22 September reads at list prices.

Read-only arithmetic over the committed history (history/gs-passive.json,
history/masterrig-passive.json, history/model-rates.json). It spends no allowance, runs no
probe and writes nothing but --json. Findings: docs/findings/2026-10-04-account-agreement.md.

    python3 -m tools.account_agreement change   # known-date test under three valuations
    python3 -m tools.account_agreement eras     # credits per 1% per account and era
    python3 -m tools.account_agreement checks   # duplicates across accounts, delegate totals
    python3 -m tools.account_agreement mix      # per-stretch work mix and the source fit
    python3 -m tools.account_agreement meters   # both meters between exact seven-day crossings
    python3 -m tools.account_agreement wpw      # windows per week per regime, tiled crossings
    python3 -m tools.account_agreement fivehour # the five-hour counting path (part 2)

`checks`, `mix` and `meters` read the gs transcripts (tools/account_transcripts.py) and
cache them under ~/.cache/account-agreement/; masterrig's transcripts are not on gs, so
those three cover a2 (jwork), a3 (dave) and a4 (avis) only.

Valuations (`valuation`), each the publisher's `comparison_value` over a copy of the pooled
fit with some families' `times_opus` replaced:

- `fitted`: the pooled fit as committed (what the page uses);
- `new_at_list`: Opus 5.5 and Sonnet 5.5 at their list-price ratio to Opus 5 (0.8, 0.4),
  every older family at its fit;
- `all_at_list`: every family at its list-price ratio (data/prices.json input class).

Cache reads stay at the fitted cache-read weight in all three.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from tracker import credits as C
from tracker.publish import ACCOUNT_LABELS, CREDITS

ROOT = Path(__file__).resolve().parent.parent
LABELS = dict(ACCOUNT_LABELS)
NEW_FAMILIES = ("opus-5-5", "sonnet-5-5")
ERAS = (
    ("before 14 Sep", None, C.CUT_AT),
    ("14-22 Sep", C.CUT_AT, datetime(2026, 9, 22, 19, 41, 49, tzinfo=timezone.utc)),
    ("after 22 Sep", datetime(2026, 9, 22, 19, 41, 49, tzinfo=timezone.utc), None),
)


def load_history(gs: Path | None = None, masterrig: Path | None = None) -> dict[str, list[dict]]:
    gs_raw = json.loads((gs or ROOT / "history" / "gs-passive.json").read_text())
    mr_raw = json.loads((masterrig or ROOT / "history" / "masterrig-passive.json").read_text())
    return C.stretches_by_account(gs_raw, mr_raw)


def valuation_rates(kind: str, model_rates: dict | None = None) -> dict:
    """A copy of the measured rates with the chosen families at their list-price ratio."""
    rates = copy.deepcopy(model_rates if model_rates is not None else C.load_model_rates())
    times = rates["pooled_fit"]["times_opus"]
    if kind == "fitted":
        return rates
    fams = NEW_FAMILIES if kind == "new_at_list" else tuple(times)
    for fam in fams:
        ratio = C.list_price_ratio(fam, CREDITS)
        if ratio is not None:
            times[fam] = ratio
    return rates


def value_for(kind: str, model_rates: dict | None = None):
    return C.comparison_value(CREDITS, valuation_rates(kind, model_rates))


def change(by_account: dict[str, list[dict]], kind: str) -> list[dict]:
    """`announced_change` under one valuation: every candidate, per account and combined."""
    out = C.announced_change(by_account, C.harness_runs(), CREDITS, value_for(kind), LABELS,
                             C.ANNOUNCEMENTS)
    return out["candidates"]


def era_of(st: dict) -> str | None:
    start, end = datetime.fromisoformat(st["start"]), datetime.fromisoformat(st["end"])
    for name, lo, hi in ERAS:
        if (lo is None or start >= lo) and (hi is None or end <= hi):
            return name
    return None


def eras(by_account: dict[str, list[dict]], kind: str, since: datetime | None = None) -> dict:
    """Meter-weighted credits per 1% (sum of credits over sum of %) per account and era.

    The selection is the known-date test's (`announced_change_stretches`: accepted, harness
    runs and cloud sessions out, masterrig from MASTERRIG_FROM). A stretch spanning an era
    boundary is in no era. Unclaimed pooled work is not added.
    """
    value = value_for(kind)
    selected = C.announced_change_stretches(by_account, C.harness_runs())
    out = {}
    for name, rows in selected.items():
        label = LABELS.get(name, name)
        acc = {}
        for st in rows:
            if since and datetime.fromisoformat(st["start"]) < since:
                continue
            era = era_of(st)
            v = value(st["tokens"])
            if era is None or v is None:
                continue
            e = acc.setdefault(era, {"credits": 0.0, "pct": 0.0, "n": 0, "logs": []})
            e["credits"] += v
            e["pct"] += st["delta_pct"]
            e["n"] += 1
            e["logs"].append(math.log(v / st["delta_pct"]))
        out[f"{label} ({name})"] = {
            era: {"credits_per_pct": round(e["credits"] / e["pct"]), "pct": e["pct"], "n": e["n"],
                  "geomean": round(math.exp(sum(e["logs"]) / len(e["logs"])))}
            for era, e in acc.items()}
    return out


def _fmt_change(c: dict) -> str:
    lines = [f"{c['family']} at {c['at'][:16]}: combined {c['change_pct']}% {c['interval_pct']} "
             f"({c['state']})"]
    for label, row in c["per_account"].items():
        lines.append(f"  {label}: {row['change_pct']}% {row['interval_pct']} "
                     f"n={row['n_before']}/{row['n_after']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", choices=("change", "eras", "checks", "mix", "meters", "wpw", "fivehour"))
    ap.add_argument("--kinds", default="fitted,new_at_list,all_at_list")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    if a.what in ("checks", "mix", "meters", "wpw", "fivehour"):
        result = {"checks": checks, "mix": mix, "meters": meters, "wpw": wpw,
                  "fivehour": fivehour}[a.what]()
        if a.json:
            a.json.write_text(json.dumps(result, indent=1, default=str))
        return 0
    by_account = load_history()
    result = {}
    for kind in a.kinds.split(","):
        if a.what == "change":
            cands = [c for c in change(by_account, kind) if c["family"] in NEW_FAMILIES]
            result[kind] = cands
            print(f"== {kind}")
            for c in cands:
                print(_fmt_change(c))
        else:
            result[kind] = eras(by_account, kind)
            print(f"== {kind}")
            for acct, rows in result[kind].items():
                print(acct, json.dumps(rows))
    if a.json:
        a.json.write_text(json.dumps(result, indent=1, default=str))
    return 0


# ---------------------------------------------------------------------------------------
# Transcript checks (gs accounts only: masterrig's transcripts are not on this host).

def turn_tokens(t) -> dict:
    return {t.model: {"input": t.input, "output": t.output, "cache_read": t.cache_read,
                      "cache_write": t.cache_write}}


def stretch_features(data: dict, kind: str = "all_at_list") -> list[dict]:
    """One row per selected gs stretch: its meter %, credits, and where those credits came from.

    Turns are the collector's own (`own`, one `seen` set per account) whose first-line time
    falls in [start, end) of a selected stretch, cloud (`remote`) turns left out as the
    selection leaves their stretches out. Shares are of the stretch's credits under `kind`.
    """
    import bisect

    from tools.account_transcripts import entrypoint
    rates_k = valuation_rates(kind)
    value = C.comparison_value(CREDITS, rates_k)
    fit = C.pooled_fit_prices(rates_k)
    fam_rate: dict[str, tuple[float, float] | None] = {}
    by_account = load_history()
    selected = C.announced_change_stretches(by_account, C.harness_runs())
    ep_cache: dict[str, str] = {}

    def ep(path: str) -> str:
        if "/subagents/" in path:
            return "subagent"
        if path not in ep_cache:
            ep_cache[path] = entrypoint(path) or "unknown"
        return ep_cache[path]

    rows = []
    for name in ("jwork", "dave", "avis"):
        turns = sorted(data[name]["own"], key=lambda x: x.turn.ts)
        stamps = [x.turn.ts for x in turns]
        for st in selected.get(name, []):
            lo, hi = datetime.fromisoformat(st["start"]), datetime.fromisoformat(st["end"])
            if lo < datetime(2026, 9, 14, tzinfo=timezone.utc):
                continue
            part = turns[bisect.bisect_left(stamps, lo):bisect.bisect_left(stamps, hi)]
            f = {"account": name, "label": LABELS[name], "start": st["start"], "end": st["end"],
                 "era": era_of(st), "pct": st["delta_pct"], "stretch_credits": value(st["tokens"]),
                 "credits": 0.0, "by": {}}
            for x in part:
                t = x.turn
                if t.remote:
                    continue
                v = value(turn_tokens(t)) or 0.0
                f["credits"] += v
                fam = C.family(t.model, CREDITS) or "other"
                src = ep(x.path)
                for key in (f"fam:{fam}", f"src:{src}", f"famsrc:{fam}/{'sub' if src == 'subagent' else 'main'}"):
                    f["by"][key] = f["by"].get(key, 0.0) + v
                # class split, in credits of this turn's family
                if fam not in fam_rate:
                    fam_rate[fam] = C.comparison_rate(fam, CREDITS, rates_k) if fam != "other" else None
                rate = fam_rate[fam]
                if rate:
                    cw5 = t.cache_write - t.cache_write_1h
                    for cls, amt in (("input", t.input * rate[0]), ("output", t.output * rate[1]),
                                     ("cw1h", t.cache_write_1h * rate[0]), ("cw5m", cw5 * rate[0]),
                                     ("cread", t.cache_read * fit["cache_read_rate"])):
                        f["by"][f"cls:{cls}"] = f["by"].get(f"cls:{cls}", 0.0) + amt
                f["by"]["tok:cread"] = f["by"].get("tok:cread", 0) + t.cache_read
                if rate:
                    # the same classes at the API's list multiples of the input rate:
                    # 5-minute write 1.25, 1-hour write 2, cache read 0.1
                    f["by"]["api"] = f["by"].get("api", 0.0) + rate[0] * (
                        t.input + 1.25 * cw5 + 2.0 * t.cache_write_1h + 0.1 * t.cache_read) \
                        + rate[1] * t.output
                    for key in (f"api:src:{src}",):
                        f["by"][key] = f["by"].get(key, 0.0) + rate[0] * (
                            t.input + 1.25 * cw5 + 2.0 * t.cache_write_1h + 0.1 * t.cache_read) \
                            + rate[1] * t.output
                f["by"]["tok:all"] = f["by"].get("tok:all", 0) + t.total
            rows.append(f)
    return rows


# ---------------------------------------------------------------------------------------
# Windows per week from exact crossings (question 3).

REGIME_EDGES = (C.CUT_AT, datetime(2026, 9, 22, 19, 41, 49, tzinfo=timezone.utc),
                datetime(2026, 9, 29, 18, 25, 18, tzinfo=timezone.utc), None)


def crossing_spans(samples) -> list[dict]:
    """Every one-point step of the seven-day meter, tiled, with the five-hour points in it.

    Within one seven-day segment (`Crossing.segment_id`: unbroken readings, one window),
    consecutive seven-day crossings a and b (one point apart) own the half-open span
    [a.upper, b.upper): each crossing is placed at the upper reading of its bracket, so
    the spans tile the segment with no gap and no overlap, and summed over a chain they
    give exactly (last - first) seven-day points against everything in between. A
    five-hour crossing is counted in the span holding its own upper reading, by the same
    rule. Placing a crossing at its upper reading moves it by at most one sample gap; over
    a chain those errors cancel except at its two ends. (Counting only the "definite"
    interior instead drops the work of any burst whose crossings share one sample pair.)
    """
    import bisect

    from tracker.crossings import crossings
    five = sorted(c.upper for c in crossings(samples, "five_hour"))
    seven = crossings(samples, "seven_day")
    by_seg: dict[int, list] = {}
    for c in seven:
        by_seg.setdefault(c.segment_id, []).append(c)
    out = []
    for cs in by_seg.values():
        cs = sorted(cs, key=lambda c: c.value)
        for a, b in zip(cs, cs[1:]):
            if b.value - a.value != 1:
                continue
            d5 = bisect.bisect_left(five, b.upper) - bisect.bisect_left(five, a.upper)
            out.append({"start": a.upper, "end": b.upper, "d7": 1, "d5": d5, "d5_amb": 0})
    return out


def regime_of(t: datetime) -> int | None:
    for k in range(len(REGIME_EDGES) - 1):
        lo, hi = REGIME_EDGES[k], REGIME_EDGES[k + 1]
        if t >= lo and (hi is None or t < hi):
            return k
    return None


def windows_per_week(names=("jwork", "dave", "avis"), boot: int = 2000, seed: int = 1) -> dict:
    """d5/d7 per regime from exact one-point seven-day crossing pairs, per account and pooled.

    A pair (seven-day crossings one point apart) belongs to the regime holding both ends.
    The point estimate counts ambiguous five-hour crossings at half; `bounds` takes none or
    all of them. The 95% interval resamples whole UTC days (per account, per regime) with
    the pooled ratio recomputed from the resampled sums.
    """
    import random
    from tracker import gs_passive as G
    pairs: dict[str, list[dict]] = {}
    for name in names:
        rows = []
        for p in crossing_spans(G.load_samples(G.gs_accounts()[name])):
            k0, k1 = regime_of(p["start"]), regime_of(p["end"])
            if k0 is None or k0 != k1:
                continue
            rows.append({**p, "regime": k0, "day": p["end"].date().isoformat()})
        pairs[name] = rows
    rng = random.Random(seed)
    out = {}
    for k in range(len(REGIME_EDGES) - 1):
        def ratio(sel):
            d7 = sum(p["d7"] for p in sel)
            return (sum(p["d5"] + p["d5_amb"] / 2 for p in sel) / d7) if d7 else None
        days = {n: {} for n in names}
        for n in names:
            for p in pairs[n]:
                if p["regime"] == k:
                    days[n].setdefault(p["day"], []).append(p)
        per = {}
        for n in names:
            sel = [p for ps in days[n].values() for p in ps]
            if sel:
                d7 = sum(p["d7"] for p in sel)
                per[n] = {"ratio": ratio(sel), "d7": d7, "d5": sum(p["d5"] for p in sel),
                          "d5_amb": sum(p["d5_amb"] for p in sel), "days": len(days[n]),
                          "bounds": [sum(p["d5"] for p in sel) / d7,
                                     sum(p["d5"] + p["d5_amb"] for p in sel) / d7]}
        draws = {n: [] for n in names}
        pooled_draws = []
        for _ in range(boot):
            pick = {}
            for n in names:
                ds = list(days[n].values())
                pick[n] = [p for _ in ds for p in rng.choice(ds)] if ds else []
                r = ratio(pick[n])
                if r is not None:
                    draws[n].append(r)
            r = ratio([p for n in names for p in pick[n]])
            if r is not None:
                pooled_draws.append(r)
        q = lambda xs: [sorted(xs)[int(0.025 * len(xs))], sorted(xs)[int(0.975 * len(xs)) - 1]] if xs else None
        for n in per:
            per[n]["interval"] = q(draws[n])
        allp = [p for n in names for ps in days[n].values() for p in ps]
        out[k] = {"from": REGIME_EDGES[k], "until": REGIME_EDGES[k + 1], "per_account": per,
                  "pooled": ratio(allp), "pooled_interval": q(pooled_draws), "_draws": pooled_draws,
                  "_acct_draws": draws}
    return out


def meter_levels(data: dict, names=("jwork", "dave", "avis"), kind: str = "all_at_list",
                 boot: int = 2000, seed: int = 2, days: tuple[str, str] | None = None) -> dict:
    """Credits per 1% of each meter, per account and regime, between exact seven-day crossings.

    Every one-point seven-day pair (`crossing_spans`) inside one regime gives the account's
    own credits (collector turns, teleported cloud turns out) over exactly 1% of the seven-day
    meter, and the five-hour points crossed in the same span (ambiguous at half). A pair
    overlapping a harness run or a recorded cloud-session span is left out. `days` limits
    the pairs to [first, last] UTC days. Intervals resample whole UTC days.
    """
    import bisect
    import random

    from tracker import gs_passive as G
    value = value_for(kind)
    runs = C.harness_runs()
    gs_raw = json.loads((ROOT / "history" / "gs-passive.json").read_text())
    rng = random.Random(seed)
    out: dict = {}
    for name in names:
        cloud = [(datetime.fromisoformat(c["start"]), datetime.fromisoformat(c["end"]))
                 for c in gs_raw["accounts"][name]["cloud_sessions"]["spans"]]
        tl = sorted((t.turn.ts, value(turn_tokens(t.turn)) or 0.0)
                    for t in data[name]["own"] if not t.turn.remote)
        ts = [x[0] for x in tl]
        cum = [0.0]
        for _, c in tl:
            cum.append(cum[-1] + c)
        by: dict[int, dict[str, list]] = {}
        for p in crossing_spans(G.load_samples(G.gs_accounts()[name])):
            k = regime_of(p["start"])
            if k is None or k != regime_of(p["end"]):
                continue
            if C.overlapping_run(runs, name, p["start"], p["end"]) is not None:
                continue
            if any(a < p["end"] and b > p["start"] for a, b in cloud):
                continue
            day = p["end"].date().isoformat()
            if days and not days[0] <= day <= days[1]:
                continue
            credits = cum[bisect.bisect_left(ts, p["end"])] - cum[bisect.bisect_left(ts, p["start"])]
            by.setdefault(k, {}).setdefault(day, []).append((credits, p["d7"], p["d5"] + p["d5_amb"] / 2))
        for k, per_day in by.items():
            def stats(groups):
                c = sum(x[0] for g in groups for x in g)
                d7 = sum(x[1] for g in groups for x in g)
                d5 = sum(x[2] for g in groups for x in g)
                return c / d7, c / d5 if d5 else None, d5 / d7
            groups = list(per_day.values())
            point = stats(groups)
            draws = [stats([rng.choice(groups) for _ in groups]) for _ in range(boot)]
            iv = lambda i: [sorted(x[i] for x in draws if x[i])[int(0.025 * boot)],
                            sorted(x[i] for x in draws if x[i])[int(0.975 * boot) - 1]]
            out.setdefault(k, {})[name] = {
                "seven_day_points": sum(x[1] for g in groups for x in g), "days": len(groups),
                "credits_per_7d_pct": point[0], "credits_per_7d_iv": iv(0),
                "credits_per_5h_pct": point[1], "credits_per_5h_iv": iv(1),
                "windows_per_week": point[2], "windows_per_week_iv": iv(2)}
    return out


TURNS_CACHE = Path.home() / ".cache" / "account-agreement" / "turns.pkl"


def _data() -> dict:
    from tools.account_transcripts import cached
    return cached(TURNS_CACHE)


def checks() -> dict:
    """Message ids or identical usage records shared across accounts; delegate run totals."""
    import collections
    import itertools

    from tools.account_transcripts import delegate_runs, session_transcripts
    from tracker.turns import iter_turns
    data = _data()
    out: dict = {"shared_ids": {}, "shared_usage": {}, "in_account_repeats": {}}
    ids = {n: {t.turn.id for t in v["own"]} for n, v in data.items()}
    uc = {t.turn.id for v in data.values() for t in v["unclaimed"]}

    def key(t):
        T = t.turn
        return (T.ts.isoformat()[:19], T.model, T.input, T.output, T.cache_read, T.cache_write)
    usage = {n: {key(t) for t in v["own"]} for n, v in data.items()}
    for a, b in itertools.combinations(ids, 2):
        out["shared_ids"][f"{a}/{b}"] = len(ids[a] & ids[b])
        out["shared_usage"][f"{a}/{b}"] = len(usage[a] & usage[b])
    for n in ids:
        out["shared_ids"][f"{n}/unclaimed"] = len(ids[n] & uc)
        c = collections.Counter(t.turn.id for t in data[n]["raw"])
        out["in_account_repeats"][n] = sum(1 for k in c.values() if k > 1)
    runs = delegate_runs()
    tot: dict = collections.defaultdict(collections.Counter)
    homes = collections.Counter()
    for r in runs:
        paths = session_transcripts(r["session_id"])
        if paths:
            homes[str(paths[0]).split("/")[3]] += 1
        for t in iter_turns(paths):
            tot[t.model]["transcript_output"] += t.output
            tot[t.model]["transcript_cache_read"] += t.cache_read
            tot[t.model]["transcript_cache_write"] += t.cache_write
        for m, u in r["model_usage"].items():
            tot[m]["claude_code_output"] += u.get("outputTokens", 0)
            tot[m]["claude_code_cache_read"] += u.get("cacheReadInputTokens", 0)
            tot[m]["claude_code_cache_write"] += u.get("cacheCreationInputTokens", 0)
    out["delegates"] = {"runs": len(runs), "transcript_home": dict(homes),
                        "by_model": {m: dict(c) for m, c in tot.items()}}
    print(json.dumps(out, indent=1))
    return out


def source_fit(rows: list[dict], prefix: str = "") -> dict:
    """Meter % against credits split by source, one shared scale: a grid search over the
    sub-agent and headless weights (interactive main sessions at 1), with an era term."""
    import numpy as np
    rows = [r for r in rows if r["era"] and r["credits"] > 0]

    def g(r, k):
        return r["by"].get(prefix + k, 0.0)
    cli = np.array([g(r, "src:cli") + g(r, "src:unknown") for r in rows])
    sub = np.array([g(r, "src:subagent") for r in rows])
    hl = np.array([g(r, "src:sdk-cli") for r in rows])
    pct = np.array([r["pct"] for r in rows])
    after = np.array([r["era"] == "after 22 Sep" for r in rows], float)
    lab = np.array([r["label"] for r in rows])

    def fit(ws, wh):
        y = np.log(cli + ws * sub + wh * hl) - np.log(pct)
        X = np.column_stack([np.ones(len(y)), after])
        W = np.sqrt(pct)
        b, *_ = np.linalg.lstsq(X * W[:, None], y * W, rcond=None)
        r = y - X @ b
        return float((pct * r * r).sum()), r
    grid = np.exp(np.linspace(math.log(0.2), math.log(3), 121))
    ss, ws, wh = min((fit(a, c)[0], a, c) for a in grid for c in grid)
    s2 = ss / (len(pct) - 4)
    band_s = [x for x in grid if min(fit(x, c)[0] for c in grid) <= ss + 3.84 * s2]
    band_h = [x for x in grid if min(fit(a, x)[0] for a in grid) <= ss + 3.84 * s2]
    _, r = fit(ws, wh)
    resid = {a: 100 * (math.exp(float((r[lab == a] * pct[lab == a]).sum() / pct[lab == a].sum())) - 1)
             for a in ("a2", "a3", "a4")}
    return {"subagent_weight": ws, "subagent_band": [band_s[0], band_s[-1]],
            "headless_weight": wh, "headless_band": [band_h[0], band_h[-1]],
            "account_mean_residual_pct": resid}


def account_offsets(rows: list[dict], value_key: str = "credits") -> dict:
    """Weighted least squares of log credits per 1% on account and era: each account's
    offset from a2, in percent."""
    import numpy as np
    rows = [r for r in rows if r["era"] and r["credits"] > 0]
    c = np.array([r["credits"] if value_key == "credits" else r["by"][value_key] for r in rows])
    pct = np.array([r["pct"] for r in rows])
    lab = np.array([r["label"] for r in rows])
    after = np.array([r["era"] == "after 22 Sep" for r in rows], float)
    y = np.log(c / pct)
    X = np.column_stack([np.ones(len(y)), after, lab == "a3", lab == "a4"]).astype(float)
    W = np.sqrt(pct)
    b, *_ = np.linalg.lstsq(X * W[:, None], y * W, rcond=None)
    return {"a3": 100 * (math.exp(b[2]) - 1), "a4": 100 * (math.exp(b[3]) - 1)}


def mix() -> dict:
    """Per account and era: credits per 1% and shares of credits by source, family and class;
    the account offsets under the list valuation and under API list class multiples; and the
    source fit."""
    import collections
    rows = stretch_features(_data())
    g = collections.defaultdict(list)
    for r in rows:
        g[(r["label"], r["era"] or "spans 22 Sep")].append(r)
    keys = ["src:subagent", "src:sdk-cli", "src:cli", "fam:opus", "fam:opus-5-5", "fam:sonnet",
            "fam:fable", "cls:output", "cls:cw1h", "cls:cw5m", "cls:cread"]
    table = {}
    for (label, era), rs in sorted(g.items()):
        c = sum(r["credits"] for r in rs)
        p = sum(r["pct"] for r in rs)
        table[f"{label} {era}"] = {"n": len(rs), "pct": p,
                                   "record_per_pct": sum(r["stretch_credits"] for r in rs) / p,
                                   "disk_per_pct": c / p,
                                   **{k: sum(r["by"].get(k, 0) for r in rs) / c for k in keys}}
    out = {"table": table, "offsets": account_offsets(rows),
           "offsets_api_classes": account_offsets(rows, "api"),
           "source_fit": source_fit(rows), "source_fit_api_classes": source_fit(rows, "api:")}
    print(json.dumps(out, indent=1, default=str))
    return out


def meters() -> dict:
    out = meter_levels(_data())
    print(json.dumps(out, indent=1, default=str))
    return out


def wpw() -> dict:
    out = windows_per_week()
    for v in out.values():
        v.pop("_draws")
        v.pop("_acct_draws")
    print(json.dumps(out, indent=1, default=str))
    return out


# ---------------------------------------------------------------------------------------
# The five-hour counting path (part 2): window starts, resets, weekly-at-100% spans.

def five_hour_windows(samples) -> list[dict]:
    """The five-hour windows a meter log shows, one row per window.

    A window is a run of readings sharing `resets_at` (`same_reset`) with no drop. Per window:
    its reset, first and last reading, the first reading's value, the peak, how many
    readings, how many sample gaps over MAX_PAIR_GAP, and the seven-day value range.
    """
    from tracker.join import MAX_PAIR_GAP
    from tracker.usage_api import same_reset
    out: list[dict] = []
    cur: dict | None = None
    prev = None
    for s in sorted(samples, key=lambda x: x.ts):
        if s.five_hour is None:
            continue
        new = (cur is None or not same_reset(cur["resets_at"], s.resets_at)
               or (prev is not None and s.five_hour < prev.five_hour))
        if new:
            cur = {"resets_at": s.resets_at, "first_ts": s.ts, "first_value": s.five_hour,
                   "peak": s.five_hour, "last_ts": s.ts, "readings": 0, "gaps": 0,
                   "seven_min": s.seven_day, "seven_max": s.seven_day,
                   "drop_start": prev is not None and s.five_hour < prev.five_hour}
            out.append(cur)
        else:
            if s.ts - prev.ts > MAX_PAIR_GAP:
                cur["gaps"] += 1
        cur["readings"] += 1
        cur["peak"] = max(cur["peak"], s.five_hour)
        cur["last_ts"] = s.ts
        if s.seven_day is not None:
            cur["seven_min"] = s.seven_day if cur["seven_min"] is None else min(cur["seven_min"], s.seven_day)
            cur["seven_max"] = s.seven_day if cur["seven_max"] is None else max(cur["seven_max"], s.seven_day)
        prev = s
    return out


def tiled_steps(samples, *, skip_first_points: int = 0, min_peak: float = 0,
                weekly_below: float | None = None) -> list[dict]:
    """`crossing_spans` with variants of which five-hour crossings count.

    `skip_first_points`: drop five-hour crossings of value <= that (each window's first
    points). `min_peak`: only five-hour crossings in windows whose peak is at least that.
    `weekly_below`: drop seven-day steps (and their five-hour points) whose seven-day value
    is at or above it. Every variant keeps the tiling: a seven-day step owns
    [a.upper, b.upper).
    """
    import bisect

    from tracker.crossings import crossings
    wins = five_hour_windows(samples)
    peak_by_reset = {}
    for w in wins:
        peak_by_reset[w["resets_at"]] = max(peak_by_reset.get(w["resets_at"], 0), w["peak"])
    five = []
    for c in crossings(samples, "five_hour"):
        if c.value <= skip_first_points:
            continue
        if min_peak and peak_by_reset.get(c.window_id, 0) < min_peak:
            continue
        five.append(c.upper)
    five.sort()
    seven = crossings(samples, "seven_day")
    by_seg: dict[int, list] = {}
    for c in seven:
        by_seg.setdefault(c.segment_id, []).append(c)
    out = []
    for cs in by_seg.values():
        cs = sorted(cs, key=lambda c: c.value)
        for a, b in zip(cs, cs[1:]):
            if b.value - a.value != 1:
                continue
            if weekly_below is not None and b.value >= weekly_below:
                continue
            d5 = bisect.bisect_left(five, b.upper) - bisect.bisect_left(five, a.upper)
            out.append({"start": a.upper, "end": b.upper, "d7": 1, "d5": d5})
    return out


def ratio_by_regime(steps: list[dict]) -> dict[int, tuple[float, int]]:
    acc: dict[int, list[int]] = {}
    for p in steps:
        k = regime_of(p["start"])
        if k is None or k != regime_of(p["end"]):
            continue
        a = acc.setdefault(k, [0, 0])
        a[0] += p["d5"]
        a[1] += p["d7"]
    return {k: (a[0] / a[1], a[1]) for k, a in sorted(acc.items())}


def _poisson(y, X, iters: int = 60):
    """Quasi-Poisson regression by IRLS: (coefficients, standard errors, deviance)."""
    import numpy as np
    b = np.zeros(X.shape[1])
    b[0] = math.log(max(y.mean(), 1e-9))
    for _ in range(iters):
        mu = np.exp(X @ b)
        z = X @ b + (y - mu) / mu
        b = np.linalg.solve((X * mu[:, None]).T @ X, (X * mu[:, None]).T @ z)
    mu = np.exp(X @ b)
    disp = float(((y - mu) ** 2 / mu).sum() / (len(y) - X.shape[1]))
    se = np.sqrt(np.diag(np.linalg.inv((X * mu[:, None]).T @ X)) * disp)
    dev = float(2 * np.sum(np.where(y > 0, y * np.log(np.where(y > 0, y, 1) / mu), 0) - (y - mu)))
    return b, se, dev


def fivehour() -> dict:
    """Every check of the five-hour counting path, per gs account, from 14 September 12:00Z.

    - `windows`: windows per regime, their first reading, peak, and weekly-at-100% windows;
    - `drops`: five-hour drops that are not to 0, and drops to 0 away from the recorded reset;
    - `reset_alignment`: recorded reset minus the account's first own turn after the
      previous reset (a window opened by this account's own work resets 5 hours later);
    - `variants`: five-hour points per seven-day point with each window's first one or two
      points left out, and with seven-day steps at 95% or more left out;
    - `by_headless`: the same ratio, and credits per 1% of each meter, by the headless
      (`sdk-cli`) share of the step's credits;
    - `poisson`: five-hour points per seven-day step on regime and account, then with the
      headless share added.
    """
    import bisect
    import collections
    import statistics

    import numpy as np

    from tools.account_transcripts import entrypoint
    from tracker import gs_passive as G
    from tracker.usage_api import same_reset
    since = C.CUT_AT
    data = _data()
    value = value_for("all_at_list")
    ep: dict[str, bool] = {}

    def headless(path: str) -> bool:
        if "/subagents/" in path:
            return False
        if path not in ep:
            ep[path] = entrypoint(path) == "sdk-cli"
        return ep[path]

    out: dict = {"windows": {}, "drops": {}, "reset_alignment": {}, "variants": {}}
    steps = []
    for name in ("jwork", "dave", "avis"):
        smp = [x for x in G.load_samples(G.gs_accounts()[name]) if x.ts >= since]
        by_k = collections.defaultdict(list)
        for w in five_hour_windows(smp):
            by_k[regime_of(w["first_ts"])].append(w)
        out["windows"][name] = {
            str(k): {"windows": len(ws), "first_value_0": sum(1 for w in ws if w["first_value"] == 0),
                     "peak_median": statistics.median(w["peak"] for w in ws),
                     "weekly_at_100": sum(1 for w in ws if (w["seven_max"] or 0) >= 100)}
            for k, ws in by_k.items()}
        live = sorted((x for x in smp if x.five_hour is not None), key=lambda x: x.ts)
        nonzero = [(a.ts.isoformat(), a.five_hour, b.five_hour) for a, b in zip(live, live[1:])
                   if 0 < b.five_hour < a.five_hour]
        resets = []
        for r in sorted(datetime.fromisoformat(x.resets_at) for x in smp if x.resets_at):
            if not resets or (r - resets[-1]).total_seconds() > 120:
                resets.append(r)
        own = sorted(t.turn.ts for t in data[name]["own"])
        offs = []
        for prev, r in zip(resets, resets[1:]):
            i = bisect.bisect_left(own, prev)
            if i < len(own) and own[i] < r:
                offs.append((r - own[i]).total_seconds() / 3600)
        out["drops"][name] = {"to_nonzero": nonzero,
                              "same_reset_dips": sum(1 for a, b in zip(live, live[1:])
                                                     if 0 < b.five_hour < a.five_hour
                                                     and same_reset(a.resets_at, b.resets_at))}
        out["reset_alignment"][name] = {"resets": len(resets),
                                        "median_h": statistics.median(offs),
                                        "p10_h": sorted(offs)[len(offs) // 10],
                                        "p90_h": sorted(offs)[9 * len(offs) // 10]}
        out["variants"][name] = {
            label: {str(k): v for k, v in ratio_by_regime(tiled_steps(smp, **kw)).items()}
            for label, kw in (("all", {}), ("skip first point", {"skip_first_points": 1}),
                              ("skip first two", {"skip_first_points": 2}),
                              ("weekly below 95", {"weekly_below": 95}))}
        turns = sorted(data[name]["own"], key=lambda x: x.turn.ts)
        stamps = [x.turn.ts for x in turns]
        for p in crossing_spans(smp):
            k = regime_of(p["start"])
            if k is None or k != regime_of(p["end"]):
                continue
            part = turns[bisect.bisect_left(stamps, p["start"]):bisect.bisect_left(stamps, p["end"])]
            c = h = 0.0
            for x in part:
                v = value(turn_tokens(x.turn)) or 0.0
                c += v
                h += v if headless(x.path) else 0.0
            if c > 0:
                steps.append({"account": name, "regime": k, "d5": p["d5"], "credits": c, "headless": h / c})
    bins = ((0, 0.2), (0.2, 0.8), (0.8, 1.01))
    out["by_headless"] = {}
    for scope in ("jwork", "dave", "avis", "all"):
        row = {}
        for lo, hi in bins:
            rs = [r for r in steps if (scope == "all" or r["account"] == scope) and lo <= r["headless"] < hi]
            if rs:
                c, d5 = sum(r["credits"] for r in rs), sum(r["d5"] for r in rs)
                row[f"{lo}-{hi}"] = {"seven_day_points": len(rs), "five_per_seven": d5 / len(rs),
                                     "credits_per_7d_pct": c / len(rs), "credits_per_5h_pct": c / d5 if d5 else None}
        out["by_headless"][scope] = row
    y = np.array([r["d5"] for r in steps], float)
    base = [np.ones(len(steps)), np.array([r["regime"] == 1 for r in steps], float),
            np.array([r["regime"] == 2 for r in steps], float),
            np.array([r["account"] == "dave" for r in steps], float),
            np.array([r["account"] == "avis" for r in steps], float)]
    out["poisson"] = {}
    for label, cols in (("account", base), ("account + headless", base + [np.array([r["headless"] for r in steps])])):
        b, se, dev = _poisson(y, np.column_stack(cols))
        names = ["dave", "avis"] + (["headless"] if len(cols) > 5 else [])
        out["poisson"][label] = {"deviance": dev, **{
            n: [math.exp(b[3 + i]), math.exp(b[3 + i] - 1.96 * se[3 + i]), math.exp(b[3 + i] + 1.96 * se[3 + i])]
            for i, n in enumerate(names)}}
    print(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
