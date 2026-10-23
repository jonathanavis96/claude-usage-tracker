"""Limit changes as a step every Max account shares: the four-account detector.

    python3 -m tracker.four_accounts --state history/four-accounts.json            # the daily run
    python3 -m tracker.four_accounts --state history/four-accounts.json --replay   # rebuild the hold
    python3 -m tracker.four_accounts --now 2026-10-23T00:00Z --snapshot FILE       # frozen readings

Each Max account is its own ruler. From the passive records (history/gs-passive.json for the
gs accounts, history/masterrig-passive.json for Max account 1) the detector keeps three
lines per account:

- `seven_day`: credits per 1% of the seven-day meter, read on the one-point seven-day steps.
  Searched.
- `ratio`: five-hour points per seven-day point on the same steps, each step's five-hour
  points over its headless inflation (the two meters against each other; no valuation enters
  it except the headless share). Searched.
- `five_hour`: credits per 1% of the five-hour meter on the stretches, headless work at the
  fitted factor. Measured as a size at every change, never searched.

A limit change is a step that every account takes at the same instant. At every six-hour
instant of the history, each account's two weeks after are compared with its two weeks
before (pooled sums, so a reading counts by the meter movement it carries; 24-hour units,
because readings within a day share their shocks). The accounts' steps are averaged by
inverse variance. The instant flags when:

- shuffling each account's days between the two sides makes a step that large less than
  once in a thousand tries (permutation p < ALPHA). The shuffles at an instant are seeded by
  the line, the instant and the account alone, never by the day the search runs, so the same
  readings give the same p on every run; and where p lies within MC_Z Monte Carlo standard
  errors of ALPHA the shuffles go on, up to PERMUTATIONS_MAX, so a borderline step is decided
  by its readings rather than by the draw;
- every voting account agrees (none more than AGREE_Z standard errors from the others, none
  significantly the other way);
- it still holds with any one account left out (plan-wide, PLAN_WIDE_Z).

The strongest flagged instant becomes a change, the history is split there, and the search
repeats until nothing flags. A step one account takes alone is a counting problem on that
account (`not_limit_changes`), never a limit change. No date is read anywhere: no cut, no
announcement, no family's first use, no account list.

The search runs once a day on the data known at midnight UTC (`hold`). A change is a
`candidate` the day it is first found, `provisional` while it keeps being found, and
`measured` once it has been found HOLD_DAYS runs in a row on the same line, in the same
direction, each find within MATCH_HOURS of the median of the hold's finds so far. MATCH_HOURS
is one weekly cycle: a weekly limit change reaches each account at its own seven-day reset,
so the strongest instant of one shared step can sit anywhere in a week (on the records of
2026-10-23 the 22 September step's moved between 23 and 30 September from one run to the
next; with the 36 hours of the spec it was never held seven runs). Matching against the
hold's median rather than yesterday's find keeps the instant from walking a week a day. The
change's instant is the median of its finds and its published range spans every find's
range, so a step whose instant the data cannot place to the day says so rather than taking
whichever instant the seventh run happened to pick.

A change found on the seven-day line also needs every voting account's capture to pass on
both sides (`capture_audit`: at least CAPTURE_MIN of the five-hour meter's movement in
stretches with captured work); one that fails stays provisional and names the account.

Only a measured change reaches the page's headline, `events`, the window regimes and the
email. The search remembers nothing but its measured changes: each is carried into every
later run as a fixed change (`carried`), never moved and never withdrawn, and re-tested every
day within its published range; a run that no longer flags it says so (`found_today` false)
and the invariants alert. A flagged instant inside a carried change's range (widened by
MIN_GAP_HOURS), on its line and in its direction, is that change found again, never a second
one. What a measured change says moved (`decided`: which lines moved, and the interval
factors that said so) is decided the day it is measured and never re-decided: its sizes and
intervals are re-measured every run, but the charts, the scope and the email do not flip
with them.

Valuation (`valuation`): one valuation across the whole history, the rates in force in
history/model-rates.json as every change figure prices them (`credits.comparison_value`),
except a family held at its list price (`credits.PINNED_AT_LIST`: Opus 5.5 at 0.8x Opus,
ADR 0001 rule 23), which has no rate interval to move.
The same rate per family on both sides of every instant, so no dated regime enters a line.
The rates the publisher absorbs at a family's first use (`absorb_new_family_rates`, fitted
jointly with a limit change at that first-seen instant) are never read here: they would carry
that date into the seven-day line. The ratio reads no rate except through a step's headless
share, so a refit moves the seven-day and five-hour sizes and never what the ratio finds.
What a rate's own uncertainty does to a size is measured at every change (`rate_dependence`):
each family's rate moved to either end of its published interval, the size re-read, and the
interval it would then have published beside the size, flagged `rate_limited` where the rates
move it more than the meters' own interval does. A change whose after side runs on a model the
before side never used cannot be told from that model's rate on the credit lines (22
September, Opus 5.5); its record says so. The size, its interval and what the charts draw stay
at the rates in force, the valuation every figure on the page is in.

The headless factor is fitted at every run from the seven-day steps, with one level per
account per ISO week in place of dated regimes (`headless_factor`).

Every interval is widened by the measured spread of z on the history with the measured changes
divided out (`coverage`): on the five-hour line and the ratio the nominal 95% interval
covered 59-74% of injected steps (the 2026-10-23 re-check), so each line's interval is
multiplied by its own measured spread, never narrowed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import zlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import credits as credit_model
from .atomic import write_text_atomic
from .capture import COLLECTION_GAP
# What the publisher and the invariants read lives in the stdlib module; re-exported here.
from .four_accounts_page import (  # noqa: F401
    CREDIT_LINES, EPOCH, HEADLINE_NAMES, LINE_METRIC, LINE_NAMES, LINES, METHOD, SCHEMA, SCOPES,
    SEARCHED, SENSITIVITY, SENTENCE, account_labels, certified_on, change_record, drawn,
    drawn_step, events, from_hours, hours, last_change, levels, line_moved, load_state,
    measured_changes, moved, p_value, page_round, parse, pct, public_block, public_label,
    reading, regime_changes, stamp, write_state,
)

UTC = timezone.utc
LINE_CODE = {line: i + 1 for i, line in enumerate(LINES)}

#: The rule. Every constant here was fixed before the history was searched with it
#: (docs/adr/0001-acceptance-rules-for-published-figures.md, rule 22).
ALPHA = 0.001
PERMUTATIONS = 4000
#: Where p after PERMUTATIONS shuffles lies within MC_Z Monte Carlo standard errors of ALPHA
#: (and at least one shuffle reached the observed |z|), the shuffles go on, five times as many
#: each round, up to PERMUTATIONS_MAX.
PERMUTATIONS_MAX = 100_000
MC_Z = 3.0
#: The permutation test runs only where the pooled |z| reaches this: on the 2026-10-23 test
#: beds no instant below it came near p < ALPHA, and the search is about ten times faster.
Z_SCREEN = 2.0
AGREE_Z = 2.5
PLAN_WIDE_Z = 1.96
GRID_HOURS = 6.0
SIDE_DAYS = 14
MIN_GAP_HOURS = 48.0
REFINE_HOURS = 48
INSTANT_RANGE_Z = 1.0
MAX_CHANGES = 12
UNIT_HOURS = 24.0
PRIOR_UNITS = 4.0
MIN_UNITS = 2
MIN_POINTS = {"five_hour": 20.0, "seven_day": 5.0, "ratio": 5.0}
HOLD_DAYS = 7
#: One weekly cycle: how far a day's find may sit from the median of the hold's finds so far
#: and still be the same change (module docstring).
MATCH_HOURS = 7 * 24.0
#: A seven-day change's voting accounts each need at least this share of their five-hour
#: meter movement, on each side, in stretches with captured work (`capture_audit`).
CAPTURE_MIN = 0.9
#: The account lines on the chart: each day's level, and the level pooled over this many days.
LINE_DAYS = 7
NOISE_HALFWIDTH_DAYS = 7
#: The coverage correction needs at least this many null instants on a line.
COVERAGE_MIN_INSTANTS = 20
#: The headless fit needs at least this many steps in informative account-weeks.
HEADLESS_MIN_STEPS = 50
#: The permutations' seed: one number for every run, combined with the line, the instant and
#: the account (`_rng`). Never the run's day.
SEED = 20261023

RULE = {
    "alpha": ALPHA, "permutations": PERMUTATIONS, "permutations_max": PERMUTATIONS_MAX,
    "mc_z": MC_Z, "seed": SEED, "z_screen": Z_SCREEN, "agreement_z": AGREE_Z,
    "plan_wide_z": PLAN_WIDE_Z, "grid_hours": GRID_HOURS, "side_days": SIDE_DAYS,
    "min_gap_hours": MIN_GAP_HOURS, "refine_hours": REFINE_HOURS, "unit_hours": UNIT_HOURS,
    "prior_units": PRIOR_UNITS, "min_units": MIN_UNITS, "min_points": dict(MIN_POINTS),
    "hold_days": HOLD_DAYS, "match_hours": MATCH_HOURS, "match_against": "median of the hold's finds",
    "capture_min": CAPTURE_MIN, "lines_searched": list(SEARCHED), "lines_measured": list(LINES),
    "collection_gap": COLLECTION_GAP,
}


# ------------------------------------------------------------------------------- accounts
def labels_for(names: list[str], fixed: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """Each account name's published label: the fixed one where it has one, else the next free
    `aN` in the order the names are given. Names are never published."""
    out = {name: label for name, label in fixed if name in names}
    used = {int(label[1:]) for label in out.values() if label[1:].isdigit()} | \
           {int(label[1:]) for _, label in fixed if label[1:].isdigit()}
    n = max(used, default=0)
    for name in names:
        if name not in out:
            n += 1
            out[name] = f"a{n}"
    return out


def records(gs_passive: dict | None, masterrig_passive: dict | None) -> dict[str, dict]:
    """Every account's record by name: {"stretches": [...], "weekly_steps": [...]}. An account
    in both files keeps the readings of the file that holds more of them."""
    out: dict[str, dict] = {}
    for report in (masterrig_passive, gs_passive):
        for name, body in ((report or {}).get("accounts") or {}).items():
            body = {"stretches": list(body.get("stretches") or []),
                    "weekly_steps": list(body.get("weekly_steps") or [])}
            have = out.get(name)
            if have is None or (len(body["stretches"]) + len(body["weekly_steps"])
                                > len(have["stretches"]) + len(have["weekly_steps"])):
                out[name] = body
    return out


# ------------------------------------------------------------------------------- valuation
def valuation(credits: dict, model_rates: dict):
    """`value(tokens)`: a reading's credits at the rates in force, or None when a model has
    no rate.

    The rates in force are history/model-rates.json as committed (`model_rates`, its
    `measured_rates` block), priced the way every change figure prices them
    (`credits.comparison_value`: each family at the pooled fit's point estimate, every cache
    read at the fit's one cache-read rate). They are applied to the whole history: one rate
    per family on both sides of every instant, so no dated regime enters a line. Not the rates
    `publish` absorbs at a family's first use (`absorb_new_family_rates`), which are fitted
    jointly with a limit change at that family's first-seen instant and would carry that date
    into the seven-day line. A held refit (tools/model_rates.py exit 3) changes nothing here
    until it is accepted; once it is, the next run values the whole history at the new rates.
    The ratio reads no rate except through a step's headless share.
    """
    return credit_model.comparison_value(credits, model_rates)


def family_parts(credits: dict, model_rates: dict):
    """`parts(tokens)`: {family: the credits of `valuation` that scale with that family's rate}
    (its input, cache-write and output tokens; with a pooled fit every cache read is at the
    fit's one cache-read rate, whichever family it came from, and moves with none). Moving a
    family's rate by a factor k moves a reading's credits by (k - 1) x its part."""
    fit = credit_model.pooled_fit_prices(model_rates)
    weight = credit_model.cache_read_weight(credits)
    cache: dict = {}

    def parts(tokens: dict) -> dict[str, float]:
        out: dict[str, float] = {}
        for model, tok in (tokens or {}).items():
            if not isinstance(tok, dict) or not credit_model.raw_tokens(tok):
                continue
            fam = credit_model.family(model, credits)
            if fam is None:
                continue
            if fam not in cache:
                cache[fam] = credit_model.comparison_rate(fam, credits, model_rates)
            pair = cache[fam]
            if pair is None:
                continue
            side = credit_model.input_side(tok, 0.0 if fit is not None else weight)
            out[fam] = out.get(fam, 0.0) + side * pair[0] + tok.get("output", 0) * pair[1]
        return out
    return parts


def rate_intervals(model_rates_raw: dict | None) -> dict:
    """Each family's rate in force and its published interval (times Opus), for the families
    the rates file gives an interval: what `rate_dependence` moves each rate across. A family
    held at its list price (`credits.PINNED_AT_LIST`, Opus 5.5) is not valued at its fitted
    rate, so its fitted interval moves nothing and it is left out."""
    raw = model_rates_raw or {}
    per = ((raw.get("measured_rates") or {}).get("per_family")) or {}
    out = {}
    for fam, row in sorted(per.items()):
        if fam in credit_model.PINNED_AT_LIST:
            continue
        x, iv = row.get("times_opus"), row.get("times_opus_interval")
        if not x or not iv or len(iv) != 2 or not all(iv):
            continue
        out[fam] = {"times_opus": round(float(x), 4), "interval": [round(float(v), 4) for v in iv],
                    "provisional": bool(row.get("provisional"))}
    return {"percentiles": raw.get("interval_percentiles"), "families": out}


def valuation_block(model_rates_raw: dict | None, credits: dict | None = None) -> dict:
    """What the run's credits are valued at, for the state and the page."""
    meta = (model_rates_raw or {}).get("_meta") or {}
    fit = credit_model.pooled_fit_prices(credit_model.load_model_rates(raw=model_rates_raw or {}))
    credits = credit_model.load_credits() if credits is None else credits
    pinned = {fam: credit_model.list_price_ratio(fam, credits)
              for fam in credit_model.PINNED_AT_LIST}
    pinned = {fam: round(x, 4) for fam, x in pinned.items() if x is not None}
    return {"kind": "rates_in_force", "source": "history/model-rates.json",
            "rates_generated_at": meta.get("generated_at") or (model_rates_raw or {}).get("generated_at"),
            "times_opus": ({fam: pinned.get(fam, round(x, 4)) for fam, x in sorted(fit["times_opus"].items())}
                           if fit else None),
            "pinned_at_list": pinned,
            "cache_read_weight": round(fit["cache_read_weight"], 6) if fit else None,
            "rate_intervals": rate_intervals(model_rates_raw),
            "rule": VALUATION_RULE}


VALUATION_RULE = ("each family at the rates in force (history/model-rates.json, the pooled fit's "
                  "point estimates, cache reads at its one fitted cache-read rate), except Opus 5.5, "
                  "held at Anthropic's list ratio 0.8x Opus (ADR 0001 rule 23), the same rates "
                  "across the whole history, so no dated regime enters a line; the ratio reads no "
                  "rate but a step's headless share; each family's rate interval widens the "
                  "sizes it moves (rate_dependence)")
#: The valuation a snapshot or a test names when it has no rates file to describe.
VALUATION = {"kind": "rates_in_force", "source": "history/model-rates.json", "rule": VALUATION_RULE}


# ------------------------------------------------------------------------------- readings
@dataclass
class Reading:
    """One reading of one account, in hours since EPOCH. `fam` and `hfam` are the credits of
    the work and of its headless part that scale with each family's rate (`family_parts`)."""
    t0: float
    t1: float
    credits: float | None
    headless: float
    d5: float
    d7: float
    fam: dict | None = None
    hfam: dict | None = None


def _flag_out(row: dict, name: str, runs: list) -> bool:
    """A reading the detector leaves out: cloud-session work, a harness run, unpriced tokens."""
    if row.get("cloud_session") or row.get("unpriced_tokens", 0):
        return True
    start, end = row.get("start"), row.get("end")
    if not start or not end:
        return True
    return credit_model.overlapping_run(runs, name, parse(start), parse(end)) is not None


def account_readings(body: dict, name: str, value, runs: list, until: datetime | None, parts=None):
    """(stretches, steps) of one account as Readings, cut at `until` (a reading ending after
    it is not yet known)."""
    out = {"stretches": [], "steps": []}
    for kind, rows in (("stretches", body.get("stretches") or []),
                       ("steps", body.get("weekly_steps") or [])):
        for row in rows:
            if _flag_out(row, name, runs):
                continue
            t0, t1 = parse(row["start"]), parse(row["end"])
            if until is not None and t1 > until:
                continue
            cr = value(row.get("tokens") or {})
            head = value(row.get("headless_tokens") or {}) if row.get("headless_tokens") else 0.0
            if cr is not None and head is None:
                cr = None
            d5 = float((row.get("delta_pct") if kind == "stretches" else row.get("d5")) or 0)
            d7 = float(row.get("d7") or 1) if kind == "steps" else 0.0
            fam = hfam = None
            if parts is not None and cr is not None:
                fam = {k: round(v, 1) for k, v in parts(row.get("tokens") or {}).items() if v}
                hfam = {k: round(v, 1) for k, v in parts(row.get("headless_tokens") or {}).items() if v}
            out[kind].append(Reading(hours(t0), hours(t1), cr, head or 0.0, d5 or 0.0, d7,
                                     fam or None, hfam or None))
    return out


def readings_for(recs: dict[str, dict], labels: dict[str, str], value, runs: list,
                 parts=None) -> dict[str, dict]:
    """Every account's readings by label, each valued once by `value` (`valuation`, with each
    family's share by `parts`): all of them, a run cuts them at the instant it runs as
    (`known_at`). Accounts with no reading are left out."""
    out = {labels[name]: account_readings(body, name, value, runs, None, parts)
           for name, body in recs.items()}
    return {k: v for k, v in out.items() if v["stretches"] or v["steps"]}


def known_at(readings: dict[str, dict], until: datetime) -> dict[str, dict]:
    """The readings known at `until`: each one that ended by then."""
    h = hours(until)
    out = {k: {kind: [r for r in rs if r.t1 <= h] for kind, rs in v.items()} for k, v in readings.items()}
    return {k: v for k, v in out.items() if v["stretches"] or v["steps"]}


def revalued(readings: dict[str, dict], fam: str, k: float) -> dict[str, dict]:
    """The readings with family `fam`'s rate multiplied by `k`, every other rate as it was."""
    def one(r: Reading) -> Reading:
        a = (r.fam or {}).get(fam, 0.0)
        h = (r.hfam or {}).get(fam, 0.0)
        if not a and not h:
            return r
        return replace(r, credits=None if r.credits is None else r.credits + (k - 1.0) * a,
                       headless=r.headless + (k - 1.0) * h)
    return {acc: {kind: [one(r) for r in rs] for kind, rs in v.items()} for acc, v in readings.items()}


#: A readings snapshot's columns (`readings_to_json`), the frozen inputs of the recovery tests.
SNAPSHOT_COLUMNS = ["t0_hours", "t1_hours", "credits", "headless_credits", "five_hour_points",
                    "seven_day_points", "credits_by_family", "headless_credits_by_family"]


def readings_to_json(readings: dict[str, dict], valued: dict | None = None) -> dict:
    """The readings as plain numbers by account label, hours since EPOCH: no account name, no
    token count, nothing but what the detector reads. `valued` names their valuation."""
    def row(r: Reading) -> list:
        return [round(r.t0, 4), round(r.t1, 4), None if r.credits is None else round(r.credits, 1),
                round(r.headless, 1), r.d5, r.d7, r.fam, r.hfam]
    return {"epoch": EPOCH.isoformat(), "columns": SNAPSHOT_COLUMNS, "valuation": valued or VALUATION,
            "accounts": {a: {kind: [row(r) for r in rs] for kind, rs in sorted(v.items())}
                         for a, v in sorted(readings.items())}}


def readings_from_json(obj: dict) -> dict[str, dict]:
    return {a: {kind: [Reading(*row) for row in rows] for kind, rows in v.items()}
            for a, v in obj["accounts"].items()}


# ------------------------------------------------------------------------------- headless
def _irls_poisson(y: np.ndarray, X: np.ndarray, iters: int = 60) -> tuple[np.ndarray, np.ndarray, float]:
    """Quasi-Poisson regression by IRLS: (coefficients, dispersion-scaled se, dispersion)."""
    n, p = X.shape
    b = np.zeros(p)
    mu = np.full(n, max(y.mean(), 1e-9))
    eta = np.log(mu)
    for _ in range(iters):
        z = eta + (y - mu) / mu
        W = mu
        XtW = X.T * W
        new = np.linalg.solve(XtW @ X, XtW @ z)
        done = np.max(np.abs(new - b)) < 1e-10
        b = new
        eta = X @ b
        mu = np.exp(eta)
        if done:
            break
    disp = float(np.sum((y - mu) ** 2 / mu) / max(n - p, 1))
    cov = np.linalg.inv((X.T * mu) @ X)
    se = np.sqrt(np.maximum(np.diag(cov) * disp, 0.0))
    return b, se, disp


def headless_factor(steps: dict[str, list[Reading]]) -> dict:
    """The five-hour meter's headless factor from the seven-day steps, with no date in it.

    A quasi-Poisson regression of each step's five-hour points on one level per account per
    ISO week and the step's headless share of credits (as tracker/five_hour_weight.py fits it,
    with ISO weeks in place of the dated regimes). Only account-weeks whose share varies carry
    information on the factor: at least three steps and a share range over 0.2. None, with a
    status, below HEADLESS_MIN_STEPS such steps; the lines are then unweighted.
    """
    rows = []
    for label, rs in steps.items():
        for r in rs:
            if not r.credits:
                continue
            wk = from_hours(r.t1).isocalendar()
            rows.append((f"{label}-{wk[0]}-{wk[1]:02d}", r.d5, min(max(r.headless / r.credits, 0.0), 1.0)))
    by: dict[str, list[float]] = {}
    for g, _, share in rows:
        by.setdefault(g, []).append(share)
    moved_: dict[str, float] = {}
    for g, d5, _ in rows:
        moved_[g] = moved_.get(g, 0.0) + d5
    informative = sorted(g for g, xs in by.items()
                         if len(xs) >= 3 and max(xs) - min(xs) > 0.2 and moved_[g] > 0)
    rows = [r for r in rows if r[0] in set(informative)]
    block = {"value": None, "interval": None, "n_steps": len(rows), "account_weeks": len(informative),
             "fit": "quasi-Poisson of each step's five-hour points on account x ISO-week levels "
                    "and its headless share of credits", "status": None}
    if len(rows) < HEADLESS_MIN_STEPS:
        block["status"] = (f"not measured: {len(rows)} steps in account-weeks whose headless share "
                           f"varies, fewer than {HEADLESS_MIN_STEPS}; headless work is counted once")
        return block
    idx = {g: i for i, g in enumerate(informative)}
    X = np.zeros((len(rows), len(informative) + 1))
    for i, (g, _, share) in enumerate(rows):
        X[i, idx[g]] = 1.0
        X[i, -1] = share
    y = np.array([r[1] for r in rows], dtype=float)
    try:
        b, se, disp = _irls_poisson(y, X)
    except np.linalg.LinAlgError:
        block["status"] = "not measured: the fit is singular; headless work is counted once"
        return block
    block.update(value=round(float(math.exp(b[-1])), 3),
                 interval=[round(float(math.exp(b[-1] - 1.96 * se[-1])), 3),
                           round(float(math.exp(b[-1] + 1.96 * se[-1])), 3)],
                 dispersion=round(disp, 3))
    return block


# ------------------------------------------------------------------------------- series
@dataclass
class Prepared:
    """One line's readings per account, sorted by midpoint, with each account's long-run noise
    per meter point (the prior the window's own scatter is shrunk toward)."""
    accounts: list
    tm: dict
    W: dict
    D: dict
    s2: dict
    first: dict = field(default_factory=dict)
    last: dict = field(default_factory=dict)


def _weighted_median(x: np.ndarray, w: np.ndarray) -> float:
    o = np.argsort(x)
    cw = np.cumsum(w[o])
    return float(x[o][np.searchsorted(cw, 0.5 * cw[-1])])


def series(readings: dict[str, dict], line: str, factor: float | None) -> dict[str, tuple]:
    """Each account's (t0, t1, W, D) arrays on one line.

    seven_day: W = credits, D = seven-day points, on the steps. ratio: W = five-hour points
    over 1 + (factor - 1) x the step's headless share of credits, D = seven-day points.
    five_hour: W = credits + (factor - 1) x headless credits, D = five-hour points, on the
    stretches. A reading under COLLECTION_GAP of the account's own meter-weighted median work
    per point on a credit line is a collection gap (the meter moved with nothing recorded) and
    is left out; the ratio reads no transcript work and has none.
    """
    h = factor or 1.0
    out = {}
    for label, rs in readings.items():
        rows = rs["stretches"] if line == "five_hour" else rs["steps"]
        t0, t1, W, D = [], [], [], []
        for r in rows:
            if r.credits is None:
                continue
            if line == "seven_day":
                w, d = r.credits, r.d7
            elif line == "ratio":
                share = min(max(r.headless / r.credits, 0.0), 1.0) if r.credits else 0.0
                w, d = r.d5 / (1.0 + (h - 1.0) * share), r.d7
            else:
                w, d = r.credits + (h - 1.0) * r.headless, r.d5
            if d <= 0:
                continue
            t0.append(r.t0)
            t1.append(r.t1)
            W.append(w)
            D.append(d)
        t0, t1, W, D = (np.array(x, dtype=float) for x in (t0, t1, W, D))
        if len(W) and line != "ratio":
            ref = _weighted_median(W / D, D)
            keep = W / D >= COLLECTION_GAP * ref
            t0, t1, W, D = t0[keep], t1[keep], W[keep], D[keep]
        out[label] = (t0, t1, W, D)
    return out


def _day_values(tm: np.ndarray, W: np.ndarray, D: np.ndarray):
    day = np.floor(tm / 24).astype(int)
    uk, inv = np.unique(day, return_inverse=True)
    Wd, Dd = np.bincount(inv, W), np.bincount(inv, D)
    with np.errstate(divide="ignore"):
        y = np.log(np.maximum(Wd, 1e-12) / Dd)
    return uk, y, Dd, Wd


def noise_per_point(tm: np.ndarray, W: np.ndarray, D: np.ndarray) -> float:
    """An account's long-run day-to-day variance of log work per point, per meter point: the
    median over its days of D_day (y_day - local level)^2 / 0.455, the local level its pooled
    rate over the days within NOISE_HALFWIDTH_DAYS, that day left out."""
    uk, y, Dd, Wd = _day_values(tm, W, D)
    if len(uk) < 5:
        return float("nan")
    vals = []
    for i in range(len(uk)):
        m = (np.abs(uk - uk[i]) <= NOISE_HALFWIDTH_DAYS) & (np.arange(len(uk)) != i)
        if Dd[m].sum() < 3 * Dd[i] and Dd[m].sum() < 20:
            continue
        if Wd[m].sum() <= 0:
            continue
        vals.append(Dd[i] * (y[i] - math.log(Wd[m].sum() / Dd[m].sum())) ** 2)
    return float(np.median(vals) / 0.455) if len(vals) >= 5 else float("nan")


def prepare(by_label: dict[str, tuple]) -> Prepared:
    accounts, tm, W, D, s2, first, last = [], {}, {}, {}, {}, {}, {}
    for label in sorted(by_label):
        t0, t1, w, d = by_label[label]
        if len(w) < 3:
            continue
        mid = 0.5 * (t0 + t1)
        o = np.argsort(mid, kind="stable")
        tm[label], W[label], D[label] = mid[o], w[o], d[o]
        s2[label] = noise_per_point(tm[label], W[label], D[label])
        first[label], last[label] = float(t0.min()), float(t1.max())
        accounts.append(label)
    return Prepared(accounts, tm, W, D, s2, first, last)


def destepped(p: Prepared, breaks: list[float]) -> Prepared:
    """A copy of `p` with each account's work divided by its own level in every segment between
    `breaks`: the found changes removed account by account, the day-to-day noise left."""
    edges = [-1e18] + sorted(breaks) + [1e18]
    W = {}
    for a in p.accounts:
        w = p.W[a].copy()
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = (p.tm[a] >= lo) & (p.tm[a] < hi)
            if p.D[a][m].sum() > 0 and p.W[a][m].sum() > 0:
                w[m] = p.W[a][m] / (p.W[a][m].sum() / p.D[a][m].sum())
        W[a] = w
    return Prepared(p.accounts, p.tm, W, p.D, p.s2, p.first, p.last)


# ------------------------------------------------------------------------------- one step
def _units(p: Prepared, a: str, tau: float, lo: float, hi: float):
    i, j = np.searchsorted(p.tm[a], [lo, hi], side="left")
    if j <= i:
        return None
    t, W, D = p.tm[a][i:j], p.W[a][i:j], p.D[a][i:j]
    u = np.floor((t - tau) / UNIT_HOURS).astype(int)
    u0 = u.min()
    Wu, Du = np.bincount(u - u0, W), np.bincount(u - u0, D)
    keep = Du > 0
    after = (np.arange(len(Du)) + u0) >= 0
    return Wu[keep], Du[keep], after[keep]


def _studentized(Wu, Du, Dy, Sy, Sy2, M, s2_prior):
    """(d, var) of one account for every row of the after-mask M: d the log step of its pooled
    levels, var its scatter around each side's own level shrunk toward the long-run prior with
    PRIOR_UNITS units of weight, times 1/D_after + 1/D_before."""
    WA, DA = M @ Wu, M @ Du
    WB, DB = Wu.sum() - WA, Du.sum() - DA
    with np.errstate(divide="ignore", invalid="ignore"):
        rA, rB = np.log(WA / DA), np.log(WB / DB)
        yA, y2A, dyA = M @ Sy, M @ Sy2, M @ Dy
        yB, y2B, dyB = Sy.sum() - yA, Sy2.sum() - y2A, Dy.sum() - dyA
        rss = (y2A - 2 * rA * yA + rA ** 2 * dyA) + (y2B - 2 * rB * yB + rB ** 2 * dyB)
        n = Du.size
        s2 = (PRIOR_UNITS * s2_prior + np.maximum(rss, 0.0)) / (PRIOR_UNITS + max(n - 2, 1))
        var = s2 * (1.0 / DA + 1.0 / DB)
    return rA - rB, var


def _rng(seed: int, line: str, tau: float, account: str) -> np.random.Generator:
    """The shuffles of one account at one instant on one line: seeded by those alone (and the
    one SEED), never by the run's day or by what else was scanned, so the same readings give
    the same p on every run, and one account's day count never shifts another's draws."""
    return np.random.default_rng(np.random.SeedSequence(
        [seed, LINE_CODE[line], int(round(tau * 4)), zlib.crc32(account.encode())]))


def _ambiguous(k: int, n: int) -> bool:
    """Whether p = (1 + k) / (n + 1) after `n` shuffles (`k` of them at least as large) is
    within MC_Z Monte Carlo standard errors of ALPHA. k = 0 is never ambiguous: no shuffle
    reached the step, which is under ALPHA with probability 1 - (1 - ALPHA)^n."""
    if k == 0:
        return False
    p = (1 + k) / (n + 1)
    return abs(p - ALPHA) < MC_Z * math.sqrt(ALPHA * (1 - ALPHA) / n)


def _exceed(parts: dict, rngs: dict, n: int, z: float) -> int:
    """How many of `n` more shuffles give a pooled |z| at least |z|."""
    num = 0.0
    den = 0.0
    for a in sorted(parts):
        Wu, Du, Dy, Sy, Sy2, nA, s2p = parts[a]
        m = len(Du)
        ranks = np.argsort(rngs[a].random((n, m)), axis=1)
        M = np.zeros((n, m))
        np.put_along_axis(M, ranks[:, :nA], 1.0, axis=1)
        pd, pv = _studentized(Wu, Du, Dy, Sy, Sy2, M, s2p)
        num = num + np.nan_to_num(pd / pv, nan=0.0, posinf=0.0, neginf=0.0)
        den = den + np.nan_to_num(1 / pv, nan=0.0, posinf=0.0, neginf=0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        zp = np.where(den > 0, num / np.sqrt(np.where(den > 0, den, 1.0)), 0.0)
    return int(np.sum(np.abs(zp) >= abs(z)))


def step_at(p: Prepared, line: str, tau: float, lo: float, hi: float, seed: int = SEED,
            n_perm: int = PERMUTATIONS, screen: float | None = Z_SCREEN,
            n_max: int | None = None) -> dict | None:
    """The pooled step at instant `tau`: before side [lo, tau), after side [tau, hi).

    Per account, d = log(sum work / sum points after) - log(the same before), from 24-hour
    units aligned at tau; an account votes with MIN_UNITS units and MIN_POINTS points and
    positive work on each side. The pooled step is the inverse-variance mean of the voting
    accounts', z its ratio to its standard error. p is the permutation p-value: each account's
    units shuffled between the sides (keeping how many each has), the pooled |z| recomputed,
    p = (1 + #{|z*| >= |z|}) / (n + 1) over n shuffles: `n_perm` first, then, while p is
    within MC_Z Monte Carlo errors of ALPHA (`_ambiguous`), five times as many again up to
    `n_max` (PERMUTATIONS_MAX by default; `n_perm` to stop there). With `screen`, p is computed
    only where |z| reaches it and is None below. None when no account votes.
    """
    obs, parts = {}, {}
    for a in p.accounts:
        got = _units(p, a, tau, lo, hi)
        if got is None:
            continue
        Wu, Du, after = got
        nA, nB = int(after.sum()), int((~after).sum())
        DA, DB = float(Du[after].sum()), float(Du[~after].sum())
        WA, WB = float(Wu[after].sum()), float(Wu[~after].sum())
        if nA < MIN_UNITS or nB < MIN_UNITS or DA < MIN_POINTS[line] or DB < MIN_POINTS[line] \
                or WA <= 0 or WB <= 0:
            continue
        s2p = p.s2.get(a, float("nan"))
        if not math.isfinite(s2p):
            continue
        pos = Wu > 0
        y = np.where(pos, np.log(np.where(pos, Wu, 1.0) / Du), 0.0)
        Dy = np.where(pos, Du, 0.0)
        Sy, Sy2 = Dy * y, Dy * y * y
        d, var = _studentized(Wu, Du, Dy, Sy, Sy2, after[None, :].astype(float), s2p)
        d, var = float(d[0]), float(var[0])
        if not (math.isfinite(d) and math.isfinite(var) and var > 0):
            continue
        obs[a] = {"d": d, "var": var, "points_before": DB, "points_after": DA,
                  "days_before": nB, "days_after": nA}
        parts[a] = (Wu, Du, Dy, Sy, Sy2, nA, s2p)
    if not obs:
        return None
    w = {a: 1 / r["var"] for a, r in obs.items()}
    wsum = sum(w.values())
    d = sum(r["d"] * w[a] for a, r in obs.items()) / wsum
    se = math.sqrt(1 / wsum)
    z = d / se
    pval, n_done = None, 0
    if n_perm and (screen is None or abs(z) >= screen):
        rngs = {a: _rng(seed, line, tau, a) for a in parts}
        cap = PERMUTATIONS_MAX if n_max is None else n_max
        k, n_done, batch = 0, 0, n_perm
        while True:
            k += _exceed(parts, rngs, batch, z)
            n_done += batch
            if n_done >= cap or not _ambiguous(k, n_done):
                break
            batch = min(4 * n_done, cap - n_done)
        pval = (1 + k) / (n_done + 1)
    for a, r in obs.items():
        others = [b for b in obs if b != a]
        if others:
            ws = sum(w[b] for b in others)
            d_o = sum(obs[b]["d"] * w[b] for b in others) / ws
            r["d_others"] = d_o
            r["z_others"] = d_o * math.sqrt(ws)
            r["z_vs_others"] = (r["d"] - d_o) / math.sqrt(r["var"] + 1 / ws)
        r["weight"] = w[a] / wsum
    return {"tau": tau, "lo": lo, "hi": hi, "d": d, "se": se, "z": z, "p": pval,
            "permutations": n_done, "k": len(obs), "acc": obs}


def agreement(r: dict) -> dict:
    """Do the voting accounts agree? Each account's step against the pooled step of the others:
    none more than AGREE_Z standard errors from them, none significant on its own (|d| over
    two of its standard errors) the other way, and at least two voting. Otherwise the accounts
    that break it are named: one account stepping alone is a counting problem on that account."""
    bad, opposite = [], []
    for a, x in r["acc"].items():
        if "z_vs_others" in x and abs(x["z_vs_others"]) > AGREE_Z:
            bad.append(a)
        if math.copysign(1, x["d"]) != math.copysign(1, r["d"]) and abs(x["d"]) > 2 * math.sqrt(x["var"]):
            opposite.append(a)
    apart = sorted(set(bad) | set(opposite))
    return {"agree": not apart and r["k"] >= 2, "accounts_apart": apart}


def plan_wide(r: dict) -> bool:
    """With each voting account left out in turn, the rest still step the same way at
    |z| >= PLAN_WIDE_Z (ADR 0001 rule 9's test, on every line)."""
    acc = r["acc"]
    if len(acc) < 2:
        return False
    sign = 1 if r["d"] > 0 else -1
    return all(sign * x["z_others"] >= PLAN_WIDE_Z for x in acc.values())


def flagged(r: dict | None) -> bool:
    return bool(r) and r["k"] >= 2 and r["p"] is not None and r["p"] < ALPHA \
        and agreement(r)["agree"] and plan_wide(r)


# ------------------------------------------------------------------------------- the search
def grid(preps: dict[str, Prepared], until_h: float | None) -> np.ndarray:
    firsts = [t for p in preps.values() for t in p.first.values()]
    lasts = [t for p in preps.values() for t in p.last.values()]
    if not firsts:
        return np.array([])
    t0, t1 = min(firsts), max(lasts)
    if until_h is not None:
        t1 = min(t1, until_h)
    taus = np.arange(math.ceil(t0 / GRID_HOURS) * GRID_HOURS, t1, GRID_HOURS)
    return taus[taus <= t1 - UNIT_HOURS]


def sides(tau: float, breaks: list[float]) -> tuple[float, float]:
    """Up to SIDE_DAYS a side, cut at every other change."""
    lo, hi = tau - SIDE_DAYS * 24.0, tau + SIDE_DAYS * 24.0
    for b in breaks:
        if abs(b - tau) < 1e-9:
            continue
        if b < tau:
            lo = max(lo, b)
        else:
            hi = min(hi, b)
    return lo, hi


class Scanner:
    """step_at over the grid, cached by (line, instant, sides): a later pass of the search only
    recomputes the instants whose sides a new change cut."""

    def __init__(self, preps: dict[str, Prepared], seed: int = SEED):
        self.preps, self.seed, self.cache = preps, seed, {}

    def at(self, line: str, tau: float, breaks: list[float]) -> dict | None:
        lo, hi = sides(tau, breaks)
        key = (line, round(tau, 6), round(lo, 6), round(hi, 6))
        if key not in self.cache:
            self.cache[key] = step_at(self.preps[line], line, tau, lo, hi, self.seed)
        return self.cache[key]


def _near_carried(c: dict, tau: float) -> bool:
    """Whether instant `tau` lies in a carried change's published range, widened by
    MIN_GAP_HOURS a side."""
    lo, hi = c["instant_range"]
    return lo - MIN_GAP_HOURS <= tau <= hi + MIN_GAP_HOURS


def _carried_again(fixed_changes: list[dict], line: str, tau: float, r: dict) -> bool:
    """Whether a flagged step is a carried change found again: on its line, in its direction,
    inside its published range (`_near_carried`). One further off, even on the same line and
    the same way, is a change of its own."""
    sign = 1 if r["d"] > 0 else -1
    return any(c["found_on"] == line and c["sign"] == sign and _near_carried(c, tau)
               for c in fixed_changes)


def _rank(r: dict) -> tuple[float, float]:
    """The order flagged instants are taken in: the smallest p, ties by the largest |z|. p is
    read at the resolution every instant gets (PERMUTATIONS shuffles): an instant near ALPHA
    gets more shuffles (`step_at`), and a finer p there must not rank a borderline step ahead
    of a stronger one whose p sits at the floor of the first round."""
    return max(r["p"], 1 / (PERMUTATIONS + 1)), -abs(r["z"])


def search(scanner: Scanner, taus: np.ndarray, fixed: list[float] = (),
           fixed_changes: list[dict] = ()) -> list[dict]:
    """The greedy segmentation: the flagged (line, instant) with the smallest p (ties: the
    largest |z|) at least MIN_GAP_HOURS from every change so far becomes a change; repeat until
    nothing flags (`_rank`). `fixed` are the measured changes carried from earlier runs: they cut the
    sides from the start and are never searched again, and a flagged instant that is one of
    them found again (`fixed_changes`, `_carried_again`) is not a new change. Returns the new
    changes at their grid instants, in the order found."""
    breaks: list[float] = list(fixed)
    found: list[dict] = []
    for _ in range(MAX_CHANGES):
        best = None
        for line in SEARCHED:
            for tau in taus:
                tau = float(tau)
                if any(abs(tau - b) < MIN_GAP_HOURS for b in breaks):
                    continue
                r = scanner.at(line, tau, breaks)
                if flagged(r) and not _carried_again(fixed_changes, line, tau, r):
                    key = _rank(r)
                    if best is None or key < best[0]:
                        best = (key, line, tau, r)
        if best is None:
            break
        _, line, tau, r = best
        breaks.append(tau)
        found.append({"line": line, "grid": tau, "p": r["p"], "z": r["z"]})
    return found


def refine(prep: Prepared, line: str, grid_tau: float, neighbours: list[float]) -> tuple[float, list[float]]:
    """The change's instant: the hour with the largest |z| within REFINE_HOURS of its grid
    instant, on the line that found it, sides cut at the neighbouring changes; and the range of
    hours whose |z| is within INSTANT_RANGE_Z of it."""
    zs, ts = [], []
    for t in np.arange(grid_tau - REFINE_HOURS, grid_tau + REFINE_HOURS + 1e-9, 1.0):
        r = step_at(prep, line, float(t), *sides(float(t), neighbours), n_perm=0)
        zs.append(abs(r["z"]) if r and r["k"] >= 2 else 0.0)
        ts.append(float(t))
    zs = np.array(zs)
    if zs.max() <= 0:
        return grid_tau, [grid_tau, grid_tau]
    best = ts[int(np.argmax(zs))]
    close = [t for t, z in zip(ts, zs) if z >= zs.max() - INSTANT_RANGE_Z]
    return best, [min(close), max(close)]


def refine_all(preps: dict[str, Prepared], found: list[dict], fixed: list[float] = (),
               passes: int = 3) -> None:
    """Every new change's instant (`_h`) and range, each refined with the carried changes and
    the other new ones fixed at their current instants, oldest first, until no instant moves
    (at most `passes`)."""
    for f in found:
        f["_h"], f["instant_range"] = f["grid"], [f["grid"], f["grid"]]
        f["sign"] = 1 if f["z"] > 0 else -1
    for _ in range(passes):
        moved_ = False
        for f in sorted(found, key=lambda f: f["grid"]):
            others = list(fixed) + [g["_h"] for g in found if g is not f]
            t, span = refine(preps[f["line"]], f["line"], f["grid"], others)
            moved_ |= t != f["_h"]
            f["_h"], f["instant_range"] = t, span
        if not moved_:
            break


def not_limit_changes(scanner: Scanner, taus: np.ndarray, breaks: list[float]) -> list[dict]:
    """Steps significant by permutation that fail agreement or plan-wide, on the searched lines
    at the final segmentation, as episodes (one per line per run of instants within two days)."""
    hits = []
    for line in SEARCHED:
        for tau in taus:
            tau = float(tau)
            if any(abs(tau - b) < MIN_GAP_HOURS for b in breaks):
                continue
            r = scanner.at(line, tau, breaks)
            if not r or r["p"] is None or r["p"] >= ALPHA or flagged(r):
                continue
            hits.append((line, tau, r))
    episodes: list[dict] = []
    for line, tau, r in sorted(hits, key=lambda h: (h[0], h[1])):
        ag = agreement(r)
        why = ("one account only" if r["k"] < 2 else
               "accounts disagree" if not ag["agree"] else "does not hold with one account left out")
        row = {"line": line, "at": stamp(tau), "first": stamp(tau), "last": stamp(tau),
               "change_pct": pct(r["d"]), "p": p_value(r["p"]), "why": why,
               "accounts_apart": ag["accounts_apart"],
               "per_account": {a: pct(x["d"]) for a, x in sorted(r["acc"].items())}}
        if episodes and episodes[-1]["line"] == line and \
                tau - hours(parse(episodes[-1]["last"])) <= MIN_GAP_HOURS:
            ep = episodes[-1]
            ep["last"] = row["at"]
            if r["p"] < ep["p"]:
                ep.update({k: row[k] for k in ("at", "change_pct", "p", "why", "accounts_apart",
                                               "per_account")})
            continue
        episodes.append(row)
    return episodes


# ------------------------------------------------------------------------------- sizes
def coverage(preps: dict[str, Prepared], breaks: list[float], taus: np.ndarray) -> dict:
    """Each line's interval factor: the spread (sd) of z at every instant of the history at
    least MIN_GAP_HOURS from a measured change, with the measured changes (`breaks`) divided
    out account by account, floored at 1. A nominal 95% interval is right when z spreads as a
    standard normal; on the five-hour line and the ratio it spread 1.6-1.9 on the 2026-10-23
    re-check's bed (the 14 and 22 Sep changes divided out, everything else left in), so their
    intervals are widened by it. Changes not yet measured stay in the bed as noise, as they
    did on that bed: an interval is never narrowed by a change the hold has not confirmed.
    Re-measured every run, as the sizes are; what a measured change says moved is not
    (`decided`)."""
    out = {}
    for line in LINES:
        bed = destepped(preps[line], breaks)
        zs = []
        for tau in taus:
            tau = float(tau)
            if any(abs(tau - b) < MIN_GAP_HOURS for b in breaks):
                continue
            r = step_at(bed, line, tau, *sides(tau, breaks), n_perm=0)
            if r and r["k"] >= 2:
                zs.append(r["z"])
        if len(zs) >= COVERAGE_MIN_INSTANTS:
            spread = float(np.std(zs))
            out[line] = {"factor": round(max(1.0, spread), 3), "z_spread": round(spread, 3),
                         "instants": len(zs)}
        else:
            out[line] = {"factor": 1.0, "z_spread": None, "instants": len(zs),
                         "status": f"{len(zs)} null instants, fewer than {COVERAGE_MIN_INSTANTS}: "
                                   "the interval is the nominal one"}
    return out


def size_on(prep: Prepared, line: str, tau: float, breaks: list[float], factor: float) -> dict | None:
    """One line's pooled step at a change, sides up to SIDE_DAYS cut at the neighbouring
    changes: the percent, its interval widened by the line's coverage factor, z, p, each
    account's own step and interval, and each account left out in turn."""
    r = step_at(prep, line, tau, *sides(tau, breaks), screen=None, n_max=PERMUTATIONS)
    if r is None:
        return None
    ag = agreement(r)
    half = 1.96 * factor
    acc = {}
    for a, x in sorted(r["acc"].items()):
        s = math.sqrt(x["var"])
        acc[a] = {"change_pct": pct(x["d"]), "interval_pct": [pct(x["d"] - half * s), pct(x["d"] + half * s)],
                  "weight": round(x["weight"], 3), "points_before": round(x["points_before"], 1),
                  "points_after": round(x["points_after"], 1), "days_before": x["days_before"],
                  "days_after": x["days_after"]}
        if "d_others" in x:
            acc[a]["without_this_account"] = {"change_pct": pct(x["d_others"]),
                                              "z": round(x["z_others"], 2)}
    lo, hi = pct(r["d"] - half * r["se"]), pct(r["d"] + half * r["se"])
    return {"change_pct": pct(r["d"]), "interval_pct": [lo, hi],
            "nominal_interval_pct": [pct(r["d"] - 1.96 * r["se"]), pct(r["d"] + 1.96 * r["se"])],
            "interval_factor": factor, "z": round(r["z"], 2), "p": p_value(r["p"]),
            "permutations": r["permutations"], "excludes_no_change": bool(lo > 0 or hi < 0),
            "accounts": sorted(r["acc"]), "agree": ag["agree"], "accounts_apart": ag["accounts_apart"],
            "plan_wide": plan_wide(r), "per_account": acc, "rate_dependence": None,
            "before": [stamp(r["lo"] if r["lo"] > -1e17 else min(prep.first.values())), stamp(tau)],
            "after": [stamp(tau), stamp(min(r["hi"], max(prep.last.values())))],
            "_d": r["d"], "_se": r["se"]}


def rate_variants(readings: dict[str, dict], factor: float | None, valued: dict | None) -> dict:
    """Each family with a rate interval, its rate moved to either end of it: {family: {"low" |
    "high": {line: Prepared}}}, every other rate as in force. Only families whose work is in
    the readings."""
    fams = ((valued or {}).get("rate_intervals") or {}).get("families") or {}
    present = {f for rs in readings.values() for kind in rs.values() for r in kind
               for f in list((r.fam or {})) + list((r.hfam or {}))}
    out = {}
    for fam, row in sorted(fams.items()):
        if fam not in present or not row.get("times_opus"):
            continue
        ends = {}
        for end, x in zip(("low", "high"), row["interval"]):
            again = revalued(readings, fam, x / row["times_opus"])
            ends[end] = {line: prepare(series(again, line, factor)) for line in LINES}
        out[fam] = ends
    return out


def rate_dependence(variants: dict, valued: dict | None, line: str, tau: float,
                    breaks: list[float], size: dict) -> None:
    """What each family's rate interval does to `size`, as its `rate_dependence`, in place.

    Each family's rate moved to either end of its published interval (the others as in
    force), the step re-read at the same instant and sides: the family's two ends move the
    log step by delta_low and delta_high. The rates together move it from d + the sum of the
    falls to d + the sum of the rises (each family taken at its worse end, added: wider, never
    narrower, than any joint draw): `change_pct_range`. `interval_pct` runs from the lower
    end's interval low to the upper end's interval high, and `excludes_no_change` reads it.
    `rate_limited` says the rates' range is wider than the size's own interval: the size is
    limited by what a rate is, not by the meters.

    The size's own interval, and every decision drawn from it (`moved`, what the charts
    draw, the scope), stay at the rates in force: every chart, window and week figure on the
    page is valued at those rates, so a step drawn there is drawn in the valuation it is
    drawn in. The rate's uncertainty is stated beside it, never folded into it."""
    fams = ((valued or {}).get("rate_intervals") or {})
    d, se = size["_d"], size["_se"]
    half = 1.96 * size["interval_factor"]
    rows, fall, rise = [], 0.0, 0.0
    for fam, ends in variants.items():
        ds = []
        for end in ("low", "high"):
            r = step_at(ends[end][line], line, tau, *sides(tau, breaks), n_perm=0)
            ds.append(r["d"] - d if r is not None else 0.0)
        if max(abs(x) for x in ds) < 5e-4:
            continue
        fall += min(0.0, *ds)
        rise += max(0.0, *ds)
        info = fams["families"][fam]
        rows.append({"family": fam, "times_opus": info["times_opus"], "interval": info["interval"],
                     "change_pct_at_interval": [pct(d + ds[0]), pct(d + ds[1])],
                     "_moves": max(abs(x) for x in ds)})
    rows.sort(key=lambda x: -x["_moves"])
    lo, hi = pct(d + fall - half * se), pct(d + rise + half * se)
    size["rate_dependence"] = {
        "families": [{k: v for k, v in x.items() if not k.startswith("_")} for x in rows],
        "change_pct_range": [pct(d + fall), pct(d + rise)],
        "interval_pct": [lo, hi], "excludes_no_change": bool(lo > 0 or hi < 0),
        "rate_limited": bool(rise - fall > 2 * half * se),
        "interval_percentiles": fams.get("percentiles"),
    }


def capture_audit(readings: dict[str, dict], tau: float, lo: float, hi: float,
                  accounts: list[str]) -> dict:
    """Spec 9.7's capture audit at a change: for each voting account, on each side, the share
    of its five-hour meter movement in stretches with captured work (not a collection gap: at
    least COLLECTION_GAP of the account's own meter-weighted median work per point). `failing`
    names the accounts under CAPTURE_MIN on either side: a capture gap every account shares
    looks exactly like a limit change on the credit lines (the 17 October artifact)."""
    out, failing = {}, []
    for a in accounts:
        rows = [r for r in (readings.get(a) or {}).get("stretches") or []
                if r.credits is not None and r.d5 > 0]
        if not rows:
            continue
        W = np.array([r.credits for r in rows])
        D = np.array([r.d5 for r in rows])
        tm = np.array([(r.t0 + r.t1) / 2 for r in rows])
        ref = _weighted_median(W / D, D)
        kept = W / D >= COLLECTION_GAP * ref
        shares = {}
        for side, (x, y) in (("before", (lo, tau)), ("after", (tau, hi))):
            m = (tm >= x) & (tm < y)
            tot = float(D[m].sum())
            shares[side] = round(float(D[m & kept].sum()) / tot, 3) if tot > 0 else None
        out[a] = shares
        if any(v is not None and v < CAPTURE_MIN for v in shares.values()):
            failing.append(a)
    return {"min_share": CAPTURE_MIN, "per_account": out, "failing": sorted(failing)}


# ------------------------------------------------------------------------------- lines
def account_lines(prep: Prepared, breaks: list[float], steps: list[float]) -> dict:
    """Each account's days on one line, indexed to what the account gets now.

    Per account and UTC day (each reading filed under its midpoint): [day, the day's level,
    meter points, the level pooled over the LINE_DAYS days to that day inside its own segment
    between changes], each level over the account's own level in the newest segment it has
    readings in, carried to the current segment by the shared steps since (`steps[i]` is the
    log step at `breaks[i]`), so 1.0 is what the account gets now.
    """
    edges = sorted(breaks)
    step_of = dict(zip(breaks, steps))
    out = {}
    for a in prep.accounts:
        tm, W, D = prep.tm[a], prep.W[a], prep.D[a]
        seg = np.searchsorted(edges, tm, side="right")
        newest = int(seg.max())
        m = seg == newest
        own = W[m].sum() / D[m].sum() if D[m].sum() > 0 and W[m].sum() > 0 else None
        if own is None:
            continue
        carried_ = sum(step_of[b] for b in edges[newest:])
        ref = own * math.exp(carried_)
        day = np.floor(tm / 24).astype(int)
        rows = []
        keys = sorted(set(zip(seg.tolist(), day.tolist())), key=lambda k: (k[1], k[0]))
        for s, dd in keys:
            sel = (seg == s) & (day == dd)
            w, d = W[sel].sum(), D[sel].sum()
            if d <= 0 or w <= 0:
                continue
            pool = (seg == s) & (day > dd - LINE_DAYS) & (day <= dd)
            pw, pdd = W[pool].sum(), D[pool].sum()
            rows.append([date.fromordinal(date(2026, 1, 1).toordinal() + int(dd)).isoformat(),
                         round(float(w / d / ref), 4), round(float(d), 1),
                         round(float(pw / pdd / ref), 4)])
        out[a] = rows
    return out


# ------------------------------------------------------------------------------- one run
def run(all_readings: dict[str, dict], until: datetime, previous: dict | None = None,
        valued: dict | None = None, replayed: bool = False) -> dict:
    """One daily run on the readings known at `until` (`readings_for`, cut by `known_at`),
    carrying the hold and the measured changes from `previous` (the previous run's block, or
    None). `valued` names the readings' valuation (`valuation_block`), and its rate intervals
    are what `rate_dependence` moves. `replayed` marks a run rebuilt after the fact (`replay`):
    its hold was not seen live, and the changes it holds say so (`hold_replayed`)."""
    day = until.date()
    readings = known_at(all_readings, until)
    hf = headless_factor({k: v["steps"] for k, v in readings.items()})
    preps = {line: prepare(series(readings, line, hf["value"])) for line in LINES}
    taus = grid({line: preps[line] for line in SEARCHED}, hours(until))
    scanner = Scanner(preps)
    held = carried(previous)
    fixed = [c["_h"] for c in held]
    found = search(scanner, taus, fixed, held)
    refine_all(preps, found, fixed)
    changes, unmatched = advance_changes(found, held, previous, day, replayed)
    breaks = [c["_h"] for c in changes]
    for c in changes:
        if c["carried"]:
            retest(scanner, taus, c, breaks)
    # The capture audit, before the coverage bed reads which changes are measured.
    for c in changes:
        c["capture_audit"] = None
        if c["found_on"] not in CREDIT_LINES:
            continue
        lo, hi = sides(c["_h"], breaks)
        own = step_at(preps[c["found_on"]], c["found_on"], c["_h"], lo, hi, n_perm=0)
        if own is None:
            continue
        c["capture_audit"] = capture_audit(readings, c["_h"], lo, hi, sorted(own["acc"]))
        if c["capture_audit"]["failing"] and c["state"] == "measured" and not c["carried"]:
            c["state"], c["measured_on"] = "provisional", None
            c["held_by_capture"] = c["capture_audit"]["failing"]
    cover = coverage(preps, [c["_h"] for c in changes if c["state"] == "measured"], taus)
    variants = rate_variants(readings, hf["value"], valued) if changes else {}
    for c in changes:
        c["sizes"] = {}
        for line in LINES:
            s = size_on(preps[line], line, c["_h"], breaks, cover[line]["factor"])
            if s is None:
                continue
            rate_dependence(variants, valued, line, c["_h"], breaks, s)
            c["sizes"][line] = s
        if c["state"] == "measured" and not c.get("decided"):
            c["decided"] = {"on": day.isoformat(),
                            "interval_factor": {line: s["interval_factor"] for line, s in c["sizes"].items()},
                            "moved": {line: moved(s) for line, s in c["sizes"].items()}}
        found_on = c["sizes"].get(c["found_on"]) or {}
        c["reading"] = reading(c)
        c["accounts"] = found_on.get("accounts") or []
        c["change_pct"] = found_on.get("change_pct")
        c["interval_pct"] = found_on.get("interval_pct")
        c["at"] = stamp(c["_h"])
    lines = {}
    for line in LINES:
        lines[line] = account_lines(preps[line], breaks, [drawn_step(c, line) for c in changes])
    return {
        "schema": SCHEMA, "method": METHOD, "day": day.isoformat(), "data_until": until.isoformat(),
        "replayed": bool(replayed), "rule": RULE, "valuation": valued or VALUATION,
        "headless_factor": hf,
        "accounts": {a: {"label": public_label(a),
                         "lines": {line: {"readings": int(len(preps[line].W.get(a, ()))),
                                          "first": stamp(preps[line].first[a]) if a in preps[line].first else None,
                                          "last": stamp(preps[line].last[a]) if a in preps[line].last else None}
                                   for line in LINES}}
                     for a in sorted(readings)},
        "interval_coverage": cover,
        "changes": [_public_change(c) for c in changes],
        "withdrawn": _withdrawn(unmatched, changes, previous, day),
        "not_limit_changes": not_limit_changes(scanner, taus, fixed + [f["grid"] for f in found]),
        "lines": lines,
        "levels": {line: levels(changes, line) for line in LINES},
    }


def retest(scanner: Scanner, taus: np.ndarray, c: dict, breaks: list[float]) -> None:
    """Would today's search find the carried change `c` again? A flagged grid instant on its
    line, in its direction, inside its published range (`_near_carried`), with the sides cut at
    every other change. Sets `found_today`, and `p`/`z` of the strongest such instant (of the
    nearest when none flags); a found day adds to `held_days`."""
    others = [b for b in breaks if b != c["_h"]]
    near = [(float(t), scanner.at(c["found_on"], float(t), others)) for t in taus
            if _near_carried(c, float(t))]
    near = [(t, r) for t, r in near if r]
    hits = [(_rank(r), t, r) for t, r in near
            if flagged(r) and (1 if r["d"] > 0 else -1) == c["sign"]]
    if hits:
        best = min(hits, key=lambda h: h[0])[2]
    elif near:
        best = min(near, key=lambda tr: abs(tr[0] - c["_h"]))[1]
    else:
        best = None
    c["found_today"] = bool(hits)
    c["p"], c["z"] = (best["p"], best["z"]) if best else (None, None)
    c["held_days"] += 1 if hits else 0


def _public_change(c: dict) -> dict:
    sizes = {line: {k: v for k, v in s.items() if not k.startswith("_")}
             for line, s in (c.get("sizes") or {}).items()}
    return {"at": stamp(c["_h"]), "instant_range": [stamp(x) for x in c["instant_range"]],
            "finds": c.get("finds") or [], "found_on": c["found_on"], "sign": c["sign"],
            "state": c["state"], "carried": c["carried"], "found_today": c["found_today"],
            "held_days": c["held_days"], "first_found": c["first_found"],
            "measured_on": c.get("measured_on"), "hold_replayed": bool(c.get("hold_replayed")),
            "held_by_capture": c.get("held_by_capture"), "capture_audit": c.get("capture_audit"),
            "decided": c.get("decided"),
            "p": p_value(c.get("p")), "z": round(c["z"], 2) if c.get("z") is not None else None,
            "change_pct": c.get("change_pct"), "interval_pct": c.get("interval_pct"),
            "accounts": c.get("accounts") or [], "reading": c.get("reading"), "sizes": sizes}


# ------------------------------------------------------------------------------- the hold
def _state_of(held: int) -> str:
    if held >= HOLD_DAYS:
        return "measured"
    return "provisional" if held >= 2 else "candidate"


def _lower_median(xs: list[float]) -> float:
    """The median of `xs`, the lower middle one when there are two: always one of them."""
    s = sorted(xs)
    return s[(len(s) - 1) // 2]


def carried(previous: dict | None) -> list[dict]:
    """The previous run's measured changes, carried into today's as fixed changes."""
    out = []
    for c in (previous or {}).get("changes") or []:
        if c.get("state") != "measured":
            continue
        span = c.get("instant_range") or [c["at"], c["at"]]
        out.append({"line": c["found_on"], "found_on": c["found_on"], "grid": None,
                    "_h": hours(parse(c["at"])), "instant_range": [hours(parse(x)) for x in span],
                    "finds": c.get("finds") or [], "sign": c["sign"], "p": None, "z": None,
                    "held_days": int(c.get("held_days") or HOLD_DAYS), "state": "measured",
                    "carried": True, "found_today": None, "first_found": c.get("first_found"),
                    "measured_on": c.get("measured_on"), "decided": c.get("decided"),
                    "hold_replayed": bool(c.get("hold_replayed"))})
    return out


def advance_changes(found: list[dict], held: list[dict], previous: dict | None,
                    day: date, replayed: bool = False) -> tuple[list[dict], list[dict]]:
    """(today's changes, the previous run's unconfirmed changes today does not hold).

    A new find is held N days when the previous run had an unconfirmed change on the same
    line, in the same direction, whose instant (the median of its hold's finds) is within
    MATCH_HOURS of it, held N-1 days; each is matched once, to the nearest. The change's
    `finds` gain today's instant and range; its instant is their median (the lower middle one)
    and its `instant_range` spans every find's range. At HOLD_DAYS it is measured at that
    instant, and from the next run on it is carried (`carried`): the search starts from it and
    never moves or withdraws it.
    """
    prev = [c for c in (previous or {}).get("changes") or [] if c.get("state") != "measured"]
    taken: set[int] = set()
    out = list(held)
    for f in sorted(found, key=lambda f: f["_h"]):
        cands = sorted((abs(hours(parse(c["at"])) - f["_h"]), i) for i, c in enumerate(prev)
                       if i not in taken and c["found_on"] == f["line"] and c["sign"] == f["sign"]
                       and abs(hours(parse(c["at"])) - f["_h"]) <= MATCH_HOURS)
        match = prev[cands[0][1]] if cands else None
        if cands:
            taken.add(cands[0][1])
        today = [stamp(f["_h"]), stamp(f["instant_range"][0]), stamp(f["instant_range"][1])]
        finds = ([list(x) for x in match.get("finds") or [[match["at"], *(match.get("instant_range")
                                                                          or [match["at"]] * 2)]]]
                 if match else []) + [today]
        days = (int(match["held_days"]) + 1) if match else 1
        state = _state_of(days)
        at = _lower_median([hours(parse(x[0])) for x in finds])
        span = [min(hours(parse(x[1])) for x in finds), max(hours(parse(x[2])) for x in finds)]
        out.append({**f, "_h": at, "instant_range": span, "finds": finds, "found_on": f["line"],
                    "held_days": days, "state": state, "carried": False, "found_today": True,
                    "first_found": match["first_found"] if match else day.isoformat(),
                    "measured_on": day.isoformat() if state == "measured" else None,
                    "hold_replayed": bool(replayed or (match or {}).get("hold_replayed"))})
    unmatched = [c for i, c in enumerate(prev) if i not in taken]
    return sorted(out, key=lambda c: c["_h"]), unmatched


def _withdrawn(unmatched: list[dict], changes: list[dict], previous: dict | None, day: date) -> list[dict]:
    """The unconfirmed changes the previous run held and today's does not, appended to the
    earlier ones (the newest 20): never drawn, listed so a find that came and went is seen.
    `replaced_by` is today's change on the same line and in the same direction within
    SIDE_DAYS, when there is one (the find moved rather than vanished)."""
    out = list((previous or {}).get("withdrawn") or [])
    for c in unmatched:
        h = hours(parse(c["at"]))
        near = [o for o in changes if o["found_on"] == c["found_on"] and o["sign"] == c["sign"]
                and abs(o["_h"] - h) <= SIDE_DAYS * 24.0]
        out.append({"at": c["at"], "found_on": c["found_on"], "sign": c["sign"],
                    "change_pct": c.get("change_pct"), "held_days": c["held_days"],
                    "found": [c["first_found"], (day - timedelta(days=1)).isoformat()],
                    "withdrawn_on": day.isoformat(),
                    "replaced_by": stamp(min(near, key=lambda o: abs(o["_h"] - h))["_h"]) if near else None})
    return out[-20:]


# ------------------------------------------------------------------------------- state
#: The fields of each change the run log keeps (and a redone day's run reads back).
LOG_FIELDS = ("at", "instant_range", "finds", "found_on", "sign", "state", "held_days",
              "first_found", "measured_on", "carried", "found_today", "change_pct", "p",
              "decided", "hold_replayed", "held_by_capture")


def run_log_row(block: dict) -> dict:
    """The part of a run the state keeps for every day: what was found and held."""
    return {"day": block["day"], "data_until": block["data_until"],
            "replayed": bool(block.get("replayed")),
            "headless_factor": block["headless_factor"].get("value"),
            "changes": [{k: c.get(k) for k in LOG_FIELDS} for c in block["changes"]],
            "withdrawn_today": [w for w in block["withdrawn"] if w.get("withdrawn_on") == block["day"]]}


def advance(state: dict | None, block: dict) -> dict:
    """The state with `block` as its newest run."""
    runs = [r for r in (state or {}).get("runs") or [] if r["day"] != block["day"]]
    runs.append(run_log_row(block))
    runs.sort(key=lambda r: r["day"])
    replayed = [r["day"] for r in runs if r.get("replayed")]
    return {"schema": SCHEMA, "method": METHOD,
            "what": ("the four-account limit-change detector's daily runs (tracker/four_accounts.py): "
                     "`runs` is one row per UTC day, what that day's run found and held; `latest` "
                     "is the newest run in full, which the publisher copies into the public JSON. "
                     "Accounts are named only by label. Runs up to `replayed_through` were rebuilt "
                     "after the fact from the records of that day (--replay), not seen live."),
            "command": "python3 -m tracker.four_accounts --state history/four-accounts.json",
            "replayed_through": max(replayed) if replayed else None,
            "runs": runs, "latest": block}


def first_day(readings: dict[str, dict]) -> date | None:
    """The first day a change could be found: two days after the second account's first reading."""
    firsts = sorted(min(r.t0 for r in rs["steps"] + rs["stretches"])
                    for rs in readings.values() if rs["steps"] or rs["stretches"])
    if len(firsts) < 2:
        return None
    return (from_hours(firsts[1]) + timedelta(days=2)).date()


def summary(block: dict) -> str:
    """One line per run: each change's line, instant, size, state and days held."""
    parts = []
    for c in block["changes"]:
        size = f"{c['change_pct']:+.1f}%" if c.get("change_pct") is not None else "n/a"
        mark = "" if c.get("found_today") is not False else " NOT FOUND TODAY"
        if c.get("held_by_capture"):
            mark += f" HELD BY CAPTURE {','.join(c['held_by_capture'])}"
        parts.append(f"{c['found_on']} {c['at'][:16]} {size} {c['state']}({c['held_days']}){mark}")
    return ", ".join(parts) or "no change"


def replay(readings: dict[str, dict], start: date, end: date, log=None,
           valued: dict | None = None) -> dict | None:
    """Every daily run from `start` to `end` on the readings as they are now, each on the data
    known at its own midnight: the hold rebuilt from scratch. Every run it builds is marked
    `replayed`, and so is every change it holds (`hold_replayed`)."""
    state = None
    d = start
    while d <= end:
        until = datetime(d.year, d.month, d.day, tzinfo=UTC)
        block = run(readings, until, (state or {}).get("latest"), valued, replayed=True)
        state = advance(state, block)
        if log:
            log(f"{d}: {summary(block)}")
        d += timedelta(days=1)
    return state


# ------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    from .publish import ACCOUNT_LABELS
    ap = argparse.ArgumentParser(description="The four-account limit-change detector's daily run")
    ap.add_argument("--gs-passive", type=Path, default=Path("history/gs-passive.json"))
    ap.add_argument("--masterrig-passive", type=Path, default=Path("history/masterrig-passive.json"))
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--model-rates", type=Path, default=Path("history/model-rates.json"),
                    help="the rates in force (tools/model_rates.py), applied to the whole history")
    ap.add_argument("--harness-runs", type=Path, default=None)
    ap.add_argument("--state", type=Path, default=Path("history/four-accounts.json"))
    ap.add_argument("--now", type=str, default=None, help="ISO instant to run as (default: the clock)")
    ap.add_argument("--replay", action="store_true",
                    help="rebuild every daily run from the first possible day to today")
    ap.add_argument("--force", action="store_true", help="run even when today's run is in the state")
    ap.add_argument("--snapshot", type=Path, default=None,
                    help="write the readings known at --now as a snapshot (the recovery tests' "
                         "frozen inputs) and stop")
    a = ap.parse_args(argv)
    started = time.time()
    now = parse(a.now) if a.now else datetime.now(UTC)
    today = now.date()
    until = datetime(today.year, today.month, today.day, tzinfo=UTC)
    prices_raw = json.loads(a.prices.read_text(encoding="utf-8"))
    credits = credit_model.load_credits(prices_raw)
    rates_raw = (json.loads(a.model_rates.read_text(encoding="utf-8"))
                 if a.model_rates.exists() else {})
    valued = valuation_block(rates_raw, credits)
    gs = json.loads(a.gs_passive.read_text(encoding="utf-8")) if a.gs_passive.exists() else None
    mr = json.loads(a.masterrig_passive.read_text(encoding="utf-8")) if a.masterrig_passive.exists() else None
    recs = records(gs, mr)
    names = sorted(recs, key=lambda n: min((parse(r["start"]) for r in recs[n]["weekly_steps"]
                                            + recs[n]["stretches"] if r.get("start")),
                                           default=datetime.max.replace(tzinfo=UTC)))
    labels = labels_for(names, ACCOUNT_LABELS)
    rates = credit_model.load_model_rates(raw=rates_raw)
    readings = readings_for(recs, labels, valuation(credits, rates),
                            credit_model.harness_runs(a.harness_runs), family_parts(credits, rates))
    if a.snapshot:
        write_text_atomic(a.snapshot, json.dumps(readings_to_json(known_at(readings, until), valued),
                                                 separators=(",", ":")) + "\n")
        print(f"four_accounts: readings known at {until.isoformat()} written to {a.snapshot}")
        return 0
    state = load_state(a.state)
    if a.replay or state is None:
        start = first_day(readings)
        if start is None:
            print("four_accounts: fewer than two accounts with readings, nothing to run", file=sys.stderr)
            return 0
        state = replay(readings, start, today, log=lambda s: print(s, file=sys.stderr, flush=True),
                       valued=valued)
    else:
        if state["latest"]["day"] == today.isoformat() and not a.force:
            print(f"four_accounts: the run for {today} is already in {a.state}", file=sys.stderr)
            return 0
        prev = state["latest"] if state["latest"]["day"] != today.isoformat() else \
            _previous_block(state, today)
        block = run(readings, until, prev, valued)
        state = advance(state, block)
    write_state(a.state, state)
    print(f"four_accounts {state['latest']['day']}: {summary(state['latest'])} "
          f"[{time.time() - started:.1f}s]")
    return 0


def _previous_block(state: dict, today: date) -> dict | None:
    """The run before today's, rebuilt from the run log, when today's run is redone."""
    rows = [r for r in state.get("runs") or [] if r["day"] < today.isoformat()]
    if not rows:
        return None
    return {"changes": rows[-1]["changes"],
            "withdrawn": [w for w in (state.get("latest") or {}).get("withdrawn") or []
                          if w.get("withdrawn_on", "") < today.isoformat()]}


if __name__ == "__main__":
    raise SystemExit(main())
