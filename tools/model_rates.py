"""Measure the meter's own per-model credit rates from the passive stretches already on disk.

The publisher prices tokens in credits at Shellac's January 2026 table (Haiku 2/15 and 10/15,
Sonnet 6/15 and 30/15, Opus 10/15 and 50/15 credits per input and output token, cache writes
at the input rate, cache reads free). Those rows are unversioned and eight months old, and the
ratios between them have never been checked against our own data. This tool checks them, using
no traffic: every figure comes from history/gs-passive.json, a masterrig stretch file in the
same shape, history/harness-runs.jsonl, history/probes.jsonl and data/effort_matrix.json.

    python3 -m tools.model_rates history/masterrig-passive.json
    python3 -m tools.model_rates history/masterrig-passive.json --json out.json

Sections: (1) tokens per 1% per model from stretches one model dominates, (2) the meter's
model ratios from those, (3) the same rates fitted across every clean stretch, twice -- once
with cache reads held at zero and once with the cache-read weight fitted jointly with the
rates -- (4) whether model mix, reset_verified or stretch length explains the jwork/Dave gap,
(5) the window in tokens per model, and (6) the rates the publisher adopts, pooled across the
fits that agree within their intervals. Ratios from fewer than three stretches on a side are
reported as not measurable rather than as a number.

Token counts are grouped into model families (Opus 5, 4.8 and 4.7 into Opus, and so on;
Opus 5.5 is a family of its own, tracker.credits.family taking the most specific match),
because Shellac's rows are unversioned; a stretch carrying tokens from a family this tool does
not know is dropped from that account's fits. Input-equivalent tokens are
input + cache_write + OUT x output, with OUT = 5 (Shellac's output ratio for every model in
its table) and OUT = 3 (the ratio fitted for Fable in the credits-model note) reported beside
it.

Cache reads are carried two ways. Held at zero, as in tools.reconcile_window.split and as the
reference table reads literally; and as a column of their own in the same fit, so the weight
the meter puts on a cache-read token is estimated jointly with the per-model rates rather than
assumed. The second exists because a Sonnet-heavy stretch on these accounts is also a
cache-read-heavy one -- sub-agent traffic -- so a Sonnet rate fitted with cache reads held at
zero can absorb the cache-read charge, and the hostile review of 2026-09-20 put that weight at
0.005 to 0.015 rather than at nothing. Section 6 and the --json `measured_rates` block report
the joint fit, which is what the publisher adopts.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.harness_runs import effort_matrix_row, probe_rows
from tracker import credits as C

CUT = C.CUT_AT
P = datetime.fromisoformat
HARNESS_RUNS = C.HARNESS_RUNS_PATH
#: Cache reads count nothing here, as in the reconciliation. data/prices.json publishes 0
#: with an uncertainty range, and this analysis reads the reference literally.
CACHE_READ_WEIGHT = 0.0

FAMILIES = ("opus", "sonnet", "haiku", "fable", "opus-5-5")
#: How a family is named in a sentence. `opus-5-5` is a key, not a name.
NAMES = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "fable": "Fable",
         "opus-5-5": "Opus 5.5"}
OUT_MULTS = (5, 3)
DOMINANCE = 0.95        # share of raw tokens one family must carry for a stretch to price it alone
MIN_N = 3               # fewer stretches than this on either side of a ratio is not measurable
MIN_FIT_N = 8           # fewer clean stretches than this does not support a four-rate fit
MIN_NONZERO = 3         # a family needs this many stretches with tokens before it is fitted
#: The dominance rule for pooling. A fit contributes to a family's pooled rate only if at
#: least DOMINANCE_MIN_N of its stretches carry DOMINANCE_SHARE or more of their raw tokens
#: on that family. A fit in which the family is only ever a minority column has no stretch
#: whose meter movement the family explains, and its coefficient is whatever the majority
#: columns leave -- the mechanism docs/findings-2026-09-23-sonnet-rate.md found behind the
#: old Sonnet rate. The anchor is exempt: it is not fitted, it sets the unit.
DOMINANCE_SHARE = 0.60
DOMINANCE_MIN_N = 3
ANCHOR = "opus"
SEED = 20260920
RESAMPLES = 600
INTERVAL = (10, 90)     # bootstrap percentiles reported as the interval

# Priors. Shellac's per-model input rates come from data/prices.json's `_credits` block
# through tracker.credits, so this tool carries no rate of its own. Fable is not in that
# table: the interval below is the one docs/findings-2026-09-20-reconciliation.md publishes
# (1.8 to 4.0 times Opus, from the Fable-heavy stretches), with the credits-model note's
# fitted 25/15 as its point value.
CREDITS = C.load_credits()
SHELLAC = {f: pair[0] for f in FAMILIES for pair in [C.rates(f, CREDITS)] if pair}
FABLE_POINT = 25 / 15
FABLE_INTERVAL = (1.8, 4.0)


def family(model: str) -> str:
    return C.family(model, CREDITS) or "other"


def name(f: str) -> str:
    return NAMES.get(f, f.capitalize())


def api_ratio(prices: dict, num: str, den: str) -> float | None:
    """The API list-price ratio between two families, from data/prices.json, or None.

    A family can have several rows in the dollar table -- Opus 5, 5.5, 4.8 and 4.7, or
    Sonnet 5 and 4.6 -- and they do not all list at the same price, so the family's
    price is the row `_credits.list_price_model` names rather than whichever row the
    dict happens to yield first.
    """
    by_family = CREDITS.get("list_price_model") or {}

    def rate(f):
        row = prices.get(by_family.get(f) or "")
        return row["input"] if row else None
    a, b = rate(num), rate(den)
    return a / b if a and b else None


#: The list prices data/prices.json carries beside the credit table, for `inferred_rate`.
LIST_PRICES = {k: v for k, v in json.loads(C.PRICES_PATH.read_text(encoding="utf-8")).items()
               if not k.startswith("_")}


def inferred_rate(f: str) -> dict | None:
    """The rate a family is shown at while the pooled fit cannot measure it, or None.

    The reference table's own row where it has one (Haiku, 2/15 per input token, which is
    also its list-price ratio to Opus 5); otherwise the family's list-price ratio to Opus,
    from data/prices.json (Opus 5.5 lists at $4/$20 against Opus 5's $5/$25, 0.8x). Both
    are inferences, not measurements: list price is not always the meter -- Fable measures
    about 2.1x Opus against a list 2.0x, Sonnet about 0.55x against a list 0.4x and a table
    0.6x (docs/findings-2026-09-23-pooled-rates.md). The anchor needs none.
    """
    if f == ANCHOR:
        return None
    if SHELLAC.get(f):
        return {"input": SHELLAC[f], "times_opus": SHELLAC[f] / SHELLAC[ANCHOR],
                "output_multiplier": POOLED_OUT_MULT, "inferred_from": "reference_table",
                "basis": (f"data/prices.json `_credits` reference table: {name(f)} at "
                          f"{SHELLAC[f]:.4f} credits per input token")}
    ratio = api_ratio(LIST_PRICES, f, ANCHOR)
    if ratio is None:
        return None
    by_family = CREDITS.get("list_price_model") or {}
    num, den = LIST_PRICES[by_family[f]], LIST_PRICES[by_family[ANCHOR]]
    return {"input": ratio * SHELLAC[ANCHOR], "times_opus": ratio,
            "output_multiplier": POOLED_OUT_MULT, "inferred_from": "list_price",
            "basis": (f"data/prices.json list prices: {by_family[f]} ${num['input']:g}/${num['output']:g} "
                      f"against {by_family[ANCHOR]} ${den['input']:g}/${den['output']:g} per million, "
                      f"{ratio:g}x the Opus anchor")}


def prepare(account: str, kept: list[dict]) -> list[dict]:
    """One record per clean stretch: its era, its per-family token counts and its meter movement."""
    out = []
    for s in kept:
        d, t = s["delta_pct"], s["tokens"]
        ie = {m: {f: 0.0 for f in FAMILIES + ("other",)} for m in OUT_MULTS}
        raw = {f: 0.0 for f in FAMILIES + ("other",)}
        reads = {f: 0.0 for f in FAMILIES + ("other",)}
        for model, tok in t.items():
            if not isinstance(tok, dict):
                continue
            f = family(model)
            i, o = C.input_side(tok, CACHE_READ_WEIGHT), tok.get("output", 0)
            raw[f] += i + o
            reads[f] += tok.get("cache_read", 0)
            for m in OUT_MULTS:
                ie[m][f] += i + m * o
        total = sum(raw.values())
        out.append({"account": account, "start": s["start"], "end": s["end"], "delta": d,
                    "era": "pre" if P(s["start"]) < CUT else "post",
                    "reset_verified": bool(s.get("reset_verified")),
                    "ie": ie, "raw": raw, "total_raw": total, "ok": raw["other"] == 0,
                    # Cache reads are kept out of `ie` and out of `raw` (both read the
                    # reference literally, at a weight of zero) and carried here on their
                    # own, so the joint fit can put a column of them in the design matrix
                    # without moving a single figure the zero-weight fit produces.
                    "reads": reads, "total_reads": sum(reads.values()),
                    "share": {f: (raw[f] / total if total else 0.0) for f in FAMILIES},
                    "dominant": max(FAMILIES, key=lambda f: raw[f]) if total else None,
                    # No default: a stretch file that omits `windows` (tracker/join.py's
                    # Stretch field, always written by this repository's own producer) is
                    # missing data this tool's quantisation floor depends on, and silently
                    # treating it as one window would understate the floor for any stretch
                    # actually pooled from more than one. Let it raise.
                    "windows": s["windows"]})
    return out


def quantiles(v: list[float]) -> dict:
    s = sorted(v)
    n = len(s)
    return {"n": n, "median": st.median(s), "p25": s[n // 4], "p75": s[(3 * n) // 4],
            "min": s[0], "max": s[-1]}


def interval(v: list[float]) -> tuple[float, float]:
    s = sorted(v)
    lo = s[max(0, int(len(s) * INTERVAL[0] / 100) - 1)]
    hi = s[min(len(s) - 1, int(len(s) * INTERVAL[1] / 100))]
    return lo, hi


def single_model(recs: list[dict], out_mult: int) -> dict[str, list[float]]:
    """Input-equivalent tokens per 1%, for stretches one family carries at DOMINANCE or above."""
    g: dict[str, list[float]] = {f: [] for f in FAMILIES}
    for r in recs:
        if not r["total_raw"] or not r["ok"]:
            continue
        f = r["dominant"]
        if r["share"][f] >= DOMINANCE:
            g[f].append(sum(r["ie"][out_mult].values()) / r["delta"])
    return {f: v for f, v in g.items() if v}


def nnls(X: np.ndarray, y: np.ndarray, iters: int = 400) -> np.ndarray:
    """Non-negative least squares. Deterministic, no scipy.

    The plain least-squares solution first: where every coefficient of it is already
    non-negative it *is* the constrained solution, exactly, and no descent can improve on it.
    That is the ordinary case here, and it matters because coordinate descent converges on the
    iterations it is given rather than on a tolerance it is promised to reach -- on the joint
    fit, whose cache-read column is correlated with the Sonnet one by construction, 400
    iterations land 40% away from the optimum where this returns it. Where a coefficient comes
    out negative the constraint binds and the descent below does the work, clipping at zero.
    """
    plain = np.linalg.lstsq(X, y, rcond=None)[0]
    if np.all(plain >= 0):
        return plain
    G, c = X.T @ X, X.T @ y
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        before = b.copy()
        for j in range(len(b)):
            if G[j, j] <= 0:
                continue
            b[j] = max(0.0, (c[j] - G[j] @ b + G[j, j] * b[j]) / G[j, j])
        if np.allclose(b, before, rtol=1e-10, atol=1e-14):
            break
    return b


#: The design matrix's last column when the cache-read weight is fitted rather than assumed.
#: One column for every family's cache reads together, so the fit estimates one weight for the
#: meter rather than one per model: cache reads are 97% of a typical stretch's tokens and the
#: families' shares of them move together, and a column per family would be four collinear
#: columns of the same traffic.
#:
#: Family columns count millions of tokens and this one counts hundreds of millions, because
#: cache reads outnumber everything else by about two orders of magnitude and the coordinate
#: descent below converges on the number of iterations it is given, not on a tolerance it is
#: guaranteed to reach. On the unscaled column the same fit lands at 0.006 where the exact
#: least-squares solution is 0.010; scaled, it agrees to seven figures in 400 iterations.
#: `CACHE_READ_PER_MILLION` converts the fitted coefficient back to a per-token one.
CACHE_READ_UNIT = 1e8
CACHE_READ_PER_MILLION = 1e6 / CACHE_READ_UNIT


def design(recs: list[dict], out_mult: int, families: tuple[str, ...],
           joint: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """The fit's matrix: one column per family, plus the cache-read column when `joint`."""
    usable = [r for r in recs if r["ok"]]
    rows = []
    for r in usable:
        row = [r["ie"][out_mult][f] / 1e6 for f in families]
        if joint:
            row.append(r["total_reads"] / CACHE_READ_UNIT)
        rows.append(row)
    return np.array(rows), np.array([r["delta"] for r in usable])


def fitted_weight(b: np.ndarray, families: tuple[str, ...]) -> float:
    """The cache-read weight a fit implies: its cache-read rate over its Opus input rate.

    A ratio of two coefficients, so the rescaling that puts the fit in credits cancels; the
    only conversion left is the two columns' different units (`CACHE_READ_PER_MILLION`).
    """
    return float(b[len(families)] * CACHE_READ_PER_MILLION / b[0])


def fit(recs: list[dict], out_mult: int, joint: bool = False) -> dict | None:
    """Fit delta_pct = sum over families of (input-equivalent tokens x rate), Opus fixed at 10/15.

    The fit is unconstrained apart from non-negativity; fixing Opus is a rescaling afterwards,
    which is what sets the credits-per-1% the other rates are read against.

    With `joint`, the stretch's cache-read tokens are a column of their own and the meter's
    cache-read charge is fitted with the rates instead of held at zero. The fitted weight is
    reported two ways: `cache_read_rate`, credits per cache-read token, and
    `cache_read_weight`, that rate over the Opus input rate -- which is the number
    data/prices.json's `cache_read_weight` holds, and the ratio of two fitted coefficients, so
    the Opus rescaling cancels out of it. The weight is one number for every family, where
    tracker.credits.input_side scales a cache read by the family's own input rate; a family
    whose cache reads are dearer or cheaper than Opus's loads that difference onto this column.
    """
    usable = [r for r in recs if r["ok"]]
    if len(usable) < MIN_FIT_N:
        return None
    families = tuple(f for f in FAMILIES
                     if sum(1 for r in usable if r["raw"][f] > 0) >= MIN_NONZERO)
    if "opus" not in families:
        return None
    if joint and not any(r["total_reads"] > 0 for r in usable):
        return None
    X, y = design(usable, out_mult, families, joint)
    b = nnls(X, y)
    if b[0] <= 0:
        return None
    scale = SHELLAC["opus"] / b[0]
    pred = X @ b
    return {"n": len(y), "families": families, "joint": joint,
            "rates": {f: v * scale for f, v in zip(families, b)},
            # How many of the fit's stretches each family dominates, for the pooling rule
            # (DOMINANCE_SHARE, DOMINANCE_MIN_N). Every family is counted, fitted or not.
            "dominant_n": {f: sum(1 for r in usable if r["share"][f] >= DOMINANCE_SHARE)
                           for f in FAMILIES},
            "cache_read_rate": fitted_weight(b, families) * SHELLAC["opus"] if joint else 0.0,
            "cache_read_weight": fitted_weight(b, families) if joint else 0.0,
            "window_credits_per_pct": SHELLAC["opus"] / (b[0] / 1e6),
            "residual_median_abs_rel": float(np.median(np.abs(pred - y) / y)),
            "residual_p90_abs_rel": float(np.percentile(np.abs(pred - y) / y, 90))}


def fit_bootstrap(recs: list[dict], out_mult: int, rng: random.Random, resamples: int,
                  joint: bool = False) -> dict | None:
    base = fit(recs, out_mult, joint)
    if not base:
        return None
    usable = [r for r in recs if r["ok"]]
    families = base["families"]
    X, y = design(usable, out_mult, families, joint)
    draws: dict[str, list[float]] = {f: [] for f in families}
    weight_draws: list[float] = []
    pairs = [(num, den, free) for num, den, free in PAIRS if num in families and den in families]
    ratio_draws: dict[str, list[float]] = {f"{num}:{den}": [] for num, den, _ in pairs}
    windows = []
    n = len(y)
    at = {f: i for i, f in enumerate(families)}
    for _ in range(resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        b = nnls(X[idx], y[idx])
        if b[0] <= 0:
            continue
        for f, v in zip(families, b):
            draws[f].append(v / b[0] * SHELLAC["opus"])
        if joint:
            weight_draws.append(fitted_weight(b, families))
        for num, den, _ in pairs:
            if b[at[den]] > 0:
                ratio_draws[f"{num}:{den}"].append(b[at[num]] / b[at[den]])
        windows.append(SHELLAC["opus"] / (b[0] / 1e6))
    base["interval"] = {f: interval(v) for f, v in draws.items() if v}
    base["window_interval"] = interval(windows) if windows else None
    base["cache_read_weight_interval"] = list(interval(weight_draws)) if weight_draws else None
    base["ratios"] = {}
    for num, den, free in pairs:
        key = f"{num}:{den}"
        rates = base["rates"]
        if not rates.get(den) or not rates.get(num) or len(ratio_draws[key]) < resamples // 2:
            base["ratios"][key] = {"measurable": False, "n_draws": len(ratio_draws[key])}
            continue
        base["ratios"][key] = {"measurable": True,
                               **verdict(num, den, free, rates[num] / rates[den], list(interval(ratio_draws[key])))}
    return base


def ratio_bootstrap(num: list[float], den: list[float], rng: random.Random, resamples: int) -> dict:
    """median(num) / median(den), resampling each side independently."""
    draws = []
    for _ in range(resamples):
        rn = [num[rng.randrange(len(num))] for _ in num]
        rd = [den[rng.randrange(len(den))] for _ in den]
        md = st.median(rd)
        if md:
            draws.append(st.median(rn) / md)
    lo, hi = interval(draws)
    return {"ratio": st.median(num) / st.median(den), "interval": [lo, hi],
            "n_num": len(num), "n_den": len(den)}


def price(rec: dict, rates: dict[str, float], out_mult: int) -> float:
    return sum(rec["ie"][out_mult][f] * rates.get(f, 0.0) for f in FAMILIES)


PAIRS = (("opus", "sonnet", "sonnet"), ("sonnet", "haiku", "haiku"), ("fable", "opus", "fable"))
PAIR_PRIOR = {("opus", "sonnet"): lambda: SHELLAC["opus"] / SHELLAC["sonnet"],
              ("sonnet", "haiku"): lambda: SHELLAC["sonnet"] / SHELLAC["haiku"],
              ("fable", "opus"): lambda: FABLE_POINT / SHELLAC["opus"]}


def verdict(num: str, den: str, free: str, value: float, iv: list[float]) -> dict:
    """One ratio against its prior: does the interval hold it, and what does the page's row do.

    The free model's tokens-per-window row is the window over its rate, so it scales with the
    ratio when the free model is the denominator and against it when it is the numerator.
    """
    prior = PAIR_PRIOR[(num, den)]()
    lo, hi = iv
    if (num, den) == ("fable", "opus"):
        pl, ph = FABLE_INTERVAL  # already in multiples of the Opus rate
        agrees = not (hi < pl or lo > ph)
    else:
        agrees = lo <= prior <= hi
    move = (value / prior - 1) if free == den else (prior / value - 1)
    return {"ratio": value, "interval": [lo, hi], "prior": prior, "free": free,
            "verdict": "agrees" if agrees else "disagrees", "row_move": move,
            "interval_width": hi / lo if lo else None}


#: masterrig enters the fits from this instant and not before; the rule and its reason live
#: with the selection in tracker/credits.py (`rate_fit_stretches`), which the publisher's
#: before-and-after comparison reads too.
MASTERRIG_FROM = C.MASTERRIG_FROM


def clean(by_account: dict[str, list[dict]], runs: list) -> dict[str, list[dict]]:
    """The fits' selection: tracker.credits.rate_fit_stretches, one rule in one place.

    Capture-accepted and harness-clean on every account, masterrig only from MASTERRIG_FROM,
    and no stretch that spans the cut. masterrig takes the same capture test as jwork and
    dave, now that the bootstrap fix fills its capture column; without it its post-cut
    Sonnet coefficient read 1.15x Opus against 0.65x from its own Sonnet-heavy stretches
    (docs/findings-2026-09-23-masterrig-admitted.md). A stretch that starts before 14
    September and ends after it is half one regime and half the other, and filing it by its
    start put one masterrig stretch of 12 to 18 September into the "pre" group
    (docs/findings-2026-09-23-pooled-rates.md).
    """
    return C.rate_fit_stretches(by_account, runs)


def probes_only_runs() -> list:
    """The exclusion list as it was before history/harness-runs.jsonl: completed probes only.

    Kept for the comparison in section 0 and for --exclusions probes-only. It is built here
    from the same two files tools/harness_runs.py reads, so nothing else in the tracker has
    to keep reading them.
    """
    rows = probe_rows() + [effort_matrix_row()]
    return sorted((C.HarnessRun(r["account"], r["start"], r["end"], C.run_reason(r)) for r in rows),
                  key=lambda r: (r.account, r.start))


def window_check(S: dict, lists: dict[str, list]) -> dict:
    """Reproduce the reconciliation's pure-Opus jwork window under each exclusion list."""
    out = {}
    for label, runs in lists.items():
        pure = C.pure_family_rows(clean(S, runs), CREDITS, "opus", CACHE_READ_WEIGHT)
        v = [r["credits_per_pct"] for r in pure.get("jwork", []) if P(r["start"]) < CUT]
        out[label] = quantiles(v) if v else None
    return out


def exclusion_report(S: dict, lists: dict[str, list]) -> dict:
    """What the harness-runs list removes beyond the probes-only rule, per account."""
    kept = {label: clean(S, runs) for label, runs in lists.items()}
    out = {}
    for a in S:
        keys = {label: {s["start"] for s in k[a]} for label, k in kept.items()}
        dropped = sorted(keys["probes-only"] - keys["harness-runs"])
        out[a] = {"probes_only": len(kept["probes-only"][a]), "harness_runs": len(kept["harness-runs"][a]),
                  "dropped": [{"start": d, "era": "pre" if P(d) < CUT else "post"} for d in dropped]}
    return out


def section1(data: dict[str, list[dict]]) -> dict:
    """Tokens per 1% per model, from the stretches one model carries."""
    out = {}
    for a, recs in data.items():
        for era in ("pre", "post"):
            sub = [r for r in recs if r["era"] == era]
            if not sub:
                continue
            row = {"max_share": {f: max(r["share"][f] for r in sub) for f in FAMILIES}}
            for m in OUT_MULTS:
                for f, v in single_model(sub, m).items():
                    q = quantiles(v)
                    q["measurable"] = q["n"] >= MIN_N
                    row.setdefault(f, {})[f"out{m}x"] = q
            out[f"{a}/{era}"] = row
    return out


def section2(data: dict[str, list[dict]], seed: int, resamples: int) -> dict:
    """The meter's ratios between models, from the single-model stretches of section 1."""
    out = {}
    for a, recs in data.items():
        for era in ("pre", "post"):
            sub = [r for r in recs if r["era"] == era]
            if not sub:
                continue
            groups = {m: single_model(sub, m) for m in OUT_MULTS}
            for num, den, free in PAIRS:
                key = f"{a}/{era}/{num}:{den}"
                per_mult = {}
                for m in OUT_MULTS:
                    g = groups[m]
                    va, vb = g.get(num, []), g.get(den, [])
                    if len(va) < MIN_N or len(vb) < MIN_N:
                        per_mult[f"out{m}x"] = {"measurable": False, "n_num": len(va), "n_den": len(vb)}
                        continue
                    # tokens per 1% is the window over the rate, so the rate ratio num:den is
                    # the token ratio the other way up.
                    r = ratio_bootstrap(vb, va, random.Random(f"{seed}/ratio/{key}/{m}"), resamples)
                    per_mult[f"out{m}x"] = {"measurable": True, "n_num": len(va), "n_den": len(vb),
                                            **verdict(num, den, free, r["ratio"], r["interval"])}
                out[key] = per_mult
    return out


def _fit_row(f: dict) -> dict:
    """One fit's published figures."""
    return {"n": f["n"], "rates": f["rates"], "interval": f["interval"],
            "dominant_n": f["dominant_n"],
            "cache_read_weight": f["cache_read_weight"],
            "cache_read_weight_interval": f["cache_read_weight_interval"],
            "window_credits_per_pct": f["window_credits_per_pct"],
            "window_interval": f["window_interval"],
            "residual_median_abs_rel": f["residual_median_abs_rel"],
            "residual_p90_abs_rel": f["residual_p90_abs_rel"],
            "ratios": f["ratios"]}


def section3(data: dict[str, list[dict]], seed: int, resamples: int) -> dict:
    """The same rates fitted across every clean stretch, not only the single-model ones.

    Each group is fitted twice. The flat fields are the fit with cache reads held at zero,
    which is what this tool published before and what the reference table reads literally;
    `joint` is the same stretches with the cache-read weight fitted alongside the rates. The
    two use their own seeded resamplers, so adding the joint fit moved no figure of the other.
    """
    out = {}
    for a, recs in data.items():
        for era in ("pre", "post"):
            sub = [r for r in recs if r["era"] == era]
            for m in OUT_MULTS:
                key = f"{a}/{era}/out{m}x"
                f = fit_bootstrap(sub, m, random.Random(f"{seed}/fit/{key}"), resamples)
                if not f:
                    continue
                out[key] = _fit_row(f)
                j = fit_bootstrap(sub, m, random.Random(f"{seed}/fit-joint/{key}"), resamples,
                                  joint=True)
                out[key]["joint"] = _fit_row(j) if j else None
    return out


def tables(section3_out: dict) -> dict[str, dict[str, float]]:
    """The rate tables section 4 prices with: Shellac's, and each gs fit's."""
    shellac = dict(SHELLAC)
    shellac["fable"] = FABLE_POINT
    out = {"shellac": shellac}
    for key, f in section3_out.items():
        account, era, mult = key.split("/")
        if account in ("jwork", "dave") and mult == "out5x":
            out[f"fitted {account}/{era}"] = {k: v for k, v in f["rates"].items() if v > 0}
    return out


def section4(data: dict[str, list[dict]], s3: dict, seed: int, resamples: int) -> dict:
    """Does model mix, reset_verified or stretch length explain the jwork/Dave gap?"""
    post = {a: [r for r in data[a] if r["era"] == "post" and r["ok"]] for a in ("jwork", "dave")}
    out = {"n": {a: len(v) for a, v in post.items()}, "mix": {}, "shares": {}, "reset_verified": {},
           "length": {}, "restricted": {}}
    for a, recs in post.items():
        out["shares"][a] = {f: st.median([r["share"][f] for r in recs]) for f in FAMILIES}
    for label, rates in tables(s3).items():
        per = {a: [price(r, rates, 5) / r["delta"] for r in recs] for a, recs in post.items()}
        if not all(per.values()):
            continue
        r = ratio_bootstrap(per["jwork"], per["dave"], random.Random(f"{seed}/mix/{label}"), resamples)
        out["mix"][label] = {"jwork": st.median(per["jwork"]), "dave": st.median(per["dave"]),
                             "ratio": r["ratio"], "interval": r["interval"],
                             "verdict": "explains" if r["interval"][0] <= 1.0 <= r["interval"][1]
                                        else "does not explain"}
    shellac = tables(s3)["shellac"]
    out["quantisation"] = {}
    for a, recs in data.items():
        for era in ("pre", "post"):
            sub = [r for r in recs if r["era"] == era and r["ok"]]
            if len(sub) < MIN_N:
                continue
            key = f"{a}/{era}"
            # A stretch pooled from `windows` separate window pieces carries two whole-percent
            # endpoints per piece, so the bound is +-windows on delta_pct, not +-0.5
            # (tracker/join.py:Stretch.bounds does the same division). This is the floor on any
            # per-stretch figure, and roughly that over the root of n on a median of them.
            out["quantisation"][key] = {"n": len(sub),
                                        "per_stretch": st.median([r["windows"] / r["delta"] for r in sub]),
                                        "deltas": sorted({r["delta"] for r in sub})}
            groups = {True: [], False: []}
            for r in sub:
                groups[r["reset_verified"]].append(price(r, shellac, 5) / r["delta"])
            entry = {str(k): quantiles(v) if v else None for k, v in groups.items()}
            if all(len(v) >= MIN_N for v in groups.values()):
                entry["ratio"] = ratio_bootstrap(groups[True], groups[False],
                                                 random.Random(f"{seed}/verified/{key}"), resamples)
            out["reset_verified"][key] = entry
            cut = st.median([r["delta"] for r in sub])
            halves = {"short": [price(r, shellac, 5) / r["delta"] for r in sub if r["delta"] <= cut],
                      "long": [price(r, shellac, 5) / r["delta"] for r in sub if r["delta"] > cut]}
            entry = {"cut_delta_pct": cut, **{k: quantiles(v) if v else None for k, v in halves.items()}}
            if all(len(v) >= MIN_N for v in halves.values()):
                entry["ratio"] = ratio_bootstrap(halves["long"], halves["short"],
                                                 random.Random(f"{seed}/length/{key}"), resamples)
            out["length"][key] = entry
    for label, keep in (("reset_verified only", lambda r: r["reset_verified"]),
                        ("delta_pct above 10", lambda r: r["delta"] > 10)):
        per = {a: [price(r, shellac, 5) / r["delta"] for r in recs if keep(r)] for a, recs in post.items()}
        if all(len(v) >= MIN_N for v in per.values()):
            r = ratio_bootstrap(per["jwork"], per["dave"],
                                random.Random(f"{seed}/restricted/{label}"), resamples)
            out["restricted"][label] = {"n": {a: len(v) for a, v in per.items()},
                                        "jwork": st.median(per["jwork"]), "dave": st.median(per["dave"]),
                                        "ratio": r["ratio"], "interval": r["interval"],
                                        "verdict": "explains" if r["interval"][0] <= 1.0 <= r["interval"][1]
                                                   else "does not explain"}
        else:
            out["restricted"][label] = {"measurable": False, "n": {a: len(v) for a, v in per.items()}}
    return out


#: The accounts whose fits are poolable. masterrig was kept out on the belief that its meter
#: also counts claude.ai web and phone use; it does not -- the account is used only for Claude
#: Code on masterrig -- and the phantom meter movement that made its residuals large was the
#: takeoff pipeline of 2 to 5 September, which MASTERRIG_FROM leaves out. It pools under the
#: same dominance rule as the other two.
FIT_ACCOUNTS = ("jwork", "dave", "masterrig")
#: A family's rate is one rate only if every fit's interval for it holds a value every other
#: fit's interval holds too. Fable's two jwork regimes fail this, and that failure is the
#: finding, not a reason to average them.
def agree(intervals: list[list[float]]) -> bool:
    # bool() rather than the numpy comparison's own type: this flag is published, and the
    # publish check treats a flag that became a number as a defect (and rightly).
    return bool(intervals) and bool(max(i[0] for i in intervals) <= min(i[1] for i in intervals))


def _variant(row: dict | None, variant: str) -> dict | None:
    """One group's fit under the named variant: the flat fields, or the nested joint ones."""
    if row is None:
        return None
    return row if variant == "zero" else row.get("joint")


def group_fits(s3: dict, variant: str = "joint", mult: str = "out5x") -> dict[str, dict]:
    """The poolable fits, keyed by account and side of the cut: `jwork/pre`, `dave/post`."""
    out = {}
    for key, row in s3.items():
        account, era, m = key.split("/")
        if account not in FIT_ACCOUNTS or m != mult:
            continue
        fit_row = _variant(row, variant)
        if fit_row:
            out[f"{account}/{era}"] = fit_row
    return out


def adopt(s3: dict, variant: str = "joint", mult: str = "out5x") -> dict:
    """Pool the FIT_ACCOUNTS fits into one rate per family, whether or not the fits agree.

    A family's point estimate is the median of the fits' point estimates and its interval is
    the union of theirs. `agree` is still recorded (every fit's interval for the family
    overlaps every other's, or not) and still published, because it says how much the median
    is trusted -- but it no longer withholds the value: Fable fits 1.754 [1.619, 1.927] times
    Opus on jwork before 14 September and 3.798 [3.324, 4.324] after, and where those disagree
    the published rate is the median of the two with the union of their intervals as its
    interval, not a status sentence in place of a number.

    Only the fits that pass the dominance rule are pooled: at least DOMINANCE_MIN_N of the
    fit's stretches must carry DOMINANCE_SHARE or more of their raw tokens on the family (the
    anchor is exempt). Every fit that returned a coefficient stays in `per_fit` either way,
    with `qualified` and a `qualification` sentence saying why it was or was not pooled, and
    `n_fits` counts the pooled ones. One qualifying fit is enough: its rate and interval are
    then the family's.
    """
    fits = group_fits(s3, variant, mult)
    out = {}
    for f in FAMILIES:
        pts, ivs, per_fit = [], [], {}
        for label, v in sorted(fits.items()):
            rate = v["rates"].get(f, 0)
            iv = v["interval"].get(f)
            if rate > 0 and iv:
                k = (v.get("dominant_n") or {}).get(f, 0)
                qualified = f == ANCHOR or k >= DOMINANCE_MIN_N
                if f == ANCHOR:
                    why = "the anchor is exempt from the dominance rule"
                else:
                    why = (f"{k} of its {v['n']} stretches carry {DOMINANCE_SHARE:.0%} or more of "
                           f"their raw tokens on {name(f)}; pooling needs {DOMINANCE_MIN_N}")
                per_fit[label] = {"rate": rate, "interval": list(iv), "n": v["n"],
                                  "dominant_n": k, "qualified": qualified,
                                  "qualification": why}
                if qualified:
                    pts.append(rate)
                    ivs.append(list(iv))
        row = {"measured": None, "interval": None, "n_fits": len(pts), "points": sorted(pts),
               "n_fits_fitted": len(per_fit), "per_fit": per_fit, "agree": None,
               "shellac": SHELLAC.get(f), "variant": variant}
        if pts and ivs:
            row["agree"] = agree(ivs)
            row["interval"] = [min(i[0] for i in ivs), max(i[1] for i in ivs)]
            row["measured"] = st.median(pts)
        out[f] = row
    return out


def weight_pool(s3: dict, variant: str = "joint", mult: str = "out5x") -> dict:
    """The fitted cache-read weight, pooled the same way the rates are."""
    fits = group_fits(s3, variant, mult)
    pts, ivs, per_fit = [], [], {}
    for label, v in sorted(fits.items()):
        iv = v.get("cache_read_weight_interval")
        if iv is None:
            continue
        pts.append(v["cache_read_weight"])
        ivs.append(list(iv))
        per_fit[label] = {"weight": v["cache_read_weight"], "interval": list(iv), "n": v["n"]}
    row = {"value": None, "interval": None, "n_fits": len(pts), "points": sorted(pts),
           "per_fit": per_fit, "agree": None, "reference": C.cache_read_weight(CREDITS),
           "reference_range": CREDITS.get("cache_read_weight_range")}
    if pts and ivs:
        row["agree"] = agree(ivs)
        row["interval"] = [min(i[0] for i in ivs), max(i[1] for i in ivs)]
        row["value"] = st.median(pts) if row["agree"] else None
    return row


#: The adopted method. One set of per-token rates shared by every account and side of the
#: cut, and one credits-per-1% scale per group (account x era), fitted together. The scale
#: is never a parameter: minimising the spread of log(credits / delta_pct) about each group's
#: own mean is the same as fitting a free scale per group, because the best log-scale for a
#: group is that mean. This is the "stable account-specific scale cancels" point in
#: tracker/credits.py used as the model instead of an obstacle: what differs between the
#: accounts' meters is absorbed by their scales, and what the rates must explain is only how
#: each group's credits per 1% moves from stretch to stretch with its model mix. Every group
#: carries every family it has tokens of, so a family no single group is dominated by is
#: still measured from all of them at once -- which is what the per-group fits and the
#: dominance rule could not do (docs/findings-2026-09-23-pooled-rates.md).
#:
#: A family is measurable when its bootstrap interval is finite, starts above zero and its
#: high end over its low end is under this ratio. Wider than that, the fit is not pinning
#: the family down and the page publishes a status sentence instead of a number.
MAX_INTERVAL_RATIO = 1.5
#: The output multiplier the pooled fit counts input-equivalent tokens at. Its cache-read
#: column counts millions of tokens, like the family columns, and the fitted weight
#: multiplies the Opus input rate, so it reads directly as data/prices.json's
#: `cache_read_weight`.
POOLED_OUT_MULT = 5
#: Nelder-Mead's starting point in times-Opus: each free family at its reference ratio where
#: the January table has one and at the anchor where it does not, and the cache-read weight
#: at 1%. The fit restarts from its own optimum until the loss stops moving, so the start
#: decides only how long it takes.
POOLED_START_WEIGHT = 0.01


def nelder_mead(f, x0: np.ndarray, step: float = 0.1, xatol: float = 1e-7, fatol: float = 1e-10,
                maxiter: int = 20000) -> tuple[np.ndarray, float]:
    """Minimise `f` from `x0` by Nelder-Mead (standard coefficients). numpy only, deterministic.

    scipy is not a dependency of this repository and is not installed on the host that
    publishes, so the simplex is written out here. It stops when every vertex is within
    `xatol` of the best one and the spread of the losses is within `fatol`.
    """
    n = len(x0)
    pts = [np.asarray(x0, dtype=float)]
    for i in range(n):
        x = np.array(x0, dtype=float)
        x[i] += step
        pts.append(x)
    vals = [f(x) for x in pts]
    for _ in range(maxiter):
        order = np.argsort(vals, kind="stable")
        pts = [pts[i] for i in order]
        vals = [vals[i] for i in order]
        if (max(float(np.max(np.abs(x - pts[0]))) for x in pts[1:]) <= xatol
                and vals[-1] - vals[0] <= fatol):
            break
        centre = np.mean(pts[:-1], axis=0)
        xr = centre + (centre - pts[-1])
        fr = f(xr)
        if fr < vals[0]:
            xe = centre + 2.0 * (centre - pts[-1])
            fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            outside = fr < vals[-1]
            xc = centre + 0.5 * ((xr if outside else pts[-1]) - centre)
            fc = f(xc)
            if (fc <= fr) if outside else (fc < vals[-1]):
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0]] + [pts[0] + 0.5 * (x - pts[0]) for x in pts[1:]]
                vals = [vals[0]] + [f(x) for x in pts[1:]]
    i = int(np.argmin(vals))
    return pts[i], float(vals[i])


def pooled_records(data: dict[str, list[dict]]) -> list[dict]:
    """The stretches the pooled fit runs over: every priceable one that moved the meter."""
    return [r for a in sorted(data) for r in data[a] if r["ok"] and r["delta"] > 0]


def group_key(r: dict) -> str:
    return f"{r['account']}/{r['era']}"


def pooled_families(recs: list[dict]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(free families, held families): which families the fit estimates and which it holds.

    A non-anchor family is free when at least MIN_NONZERO stretches carry its tokens. One
    carried by fewer is held at its inferred rate (`inferred_rate`) rather than dropped with
    the stretches that carry it: on today's data that is Opus 5.5, in two stretches, held at
    its list-price 0.8x Opus -- the rate the page shows it at.
    """
    present = [f for f in FAMILIES if f != ANCHOR and any(r["raw"][f] > 0 for r in recs)]
    free = tuple(f for f in present if sum(1 for r in recs if r["raw"][f] > 0) >= MIN_NONZERO)
    return free, tuple(f for f in present if f not in free)


def held_rate(f: str) -> float:
    """A family's rate in times-Opus when the pooled fit holds it rather than fitting it."""
    if f == ANCHOR:
        return 1.0
    inferred = inferred_rate(f)
    return inferred["times_opus"] if inferred else 1.0


class _Pooled:
    """The pooled fit's arrays for one set of records, and its loss."""

    def __init__(self, recs: list[dict], free: tuple[str, ...], split: str | None = None):
        self.free, self.split = free, split
        groups = sorted({group_key(r) for r in recs})
        self.groups = groups
        at = {g: i for i, g in enumerate(groups)}
        self.g = np.array([at[group_key(r)] for r in recs])
        self.counts = np.bincount(self.g, minlength=len(groups)).astype(float)
        # Columns: the anchor at 1 plus every held family at its inferred rate, then each free
        # family. A family with tokens and no inferred rate either is held at the anchor's.
        self.fixed = np.array([sum(r["ie"][POOLED_OUT_MULT][f] * held_rate(f) for f in FAMILIES
                                   if f == ANCHOR or f not in free) / 1e6 for r in recs])
        self.X = np.array([[r["ie"][POOLED_OUT_MULT][f] / 1e6 for f in free] for r in recs]
                          ).reshape(len(recs), len(free))
        self.reads = np.array([r["total_reads"] / 1e6 for r in recs])
        self.post = np.array([r["era"] == "post" for r in recs])
        self.ly = np.log(np.array([r["delta"] for r in recs], dtype=float))
        self.split_at = free.index(split) if split in free else None

    def n_params(self) -> int:
        return len(self.free) + (1 if self.split_at is not None else 0) + 1

    def credits(self, p: np.ndarray) -> np.ndarray:
        """Credits of every stretch, in Opus-input-token millions, at log-parameters `p`."""
        k = len(self.free)
        rates = np.exp(p[:k])
        cols = self.X * rates
        if self.split_at is not None:
            post_rate = np.exp(p[k])
            j = self.split_at
            cols[:, j] = np.where(self.post, self.X[:, j] * post_rate, cols[:, j])
        return self.fixed + cols.sum(axis=1) + np.exp(p[-1]) * self.reads

    def loss(self, p: np.ndarray) -> float:
        lc = np.log(self.credits(p)) - self.ly
        means = np.bincount(self.g, weights=lc, minlength=len(self.groups)) / self.counts
        return float(((lc - means[self.g]) ** 2).sum())

    def scales(self, p: np.ndarray) -> dict[str, float]:
        """Each group's credits per 1%, in credits: the geometric mean the loss centres on."""
        lc = np.log(self.credits(p)) - self.ly
        means = np.bincount(self.g, weights=lc, minlength=len(self.groups)) / self.counts
        return {g: float(np.exp(m) * 1e6 * SHELLAC[ANCHOR]) for g, m in zip(self.groups, means)}


def _start(free: tuple[str, ...], split: str | None) -> np.ndarray:
    ref = [SHELLAC[f] / SHELLAC[ANCHOR] if SHELLAC.get(f) else 1.0 for f in free]
    extra = [ref[free.index(split)]] if split in free else []
    return np.log(np.array(ref + extra + [POOLED_START_WEIGHT]))


def _minimise(model: _Pooled, x0: np.ndarray) -> tuple[np.ndarray, float]:
    """Nelder-Mead from `x0`, restarted from its own optimum until the loss stops moving."""
    p, v = nelder_mead(model.loss, x0)
    for _ in range(4):
        q, w = nelder_mead(model.loss, p)
        if v - w < 1e-12:
            return (q, w) if w < v else (p, v)
        p, v = q, w
    return p, v


def pooled_fit(recs: list[dict], free: tuple[str, ...] | None = None, split: str | None = None,
               x0: np.ndarray | None = None) -> dict | None:
    """One pooled fit: the shared rates in times-Opus, the cache-read weight, the group scales.

    With `split`, that family gets one rate before 14 September and another after, and
    `times_opus` carries the pre-cut one with `post_rate` beside it.
    """
    if len(recs) < MIN_FIT_N:
        return None
    if free is None:
        free, _ = pooled_families(recs)
    model = _Pooled(recs, free, split)
    p, sse = _minimise(model, _start(free, split) if x0 is None else x0)
    k = len(free)
    out = {"n": len(recs), "groups": model.groups, "free": list(free), "params": p,
           "times_opus": {ANCHOR: 1.0, **{f: float(np.exp(v)) for f, v in zip(free, p[:k])}},
           "cache_read_weight": float(np.exp(p[-1])), "sse": sse,
           "credits_per_pct": model.scales(p)}
    if model.split_at is not None:
        out["post_rate"] = float(np.exp(p[k]))
    return out


def _bootstrap_draw(recs: list[dict], rng: random.Random) -> list[dict]:
    """One resample, stratified by group: each group redrawn to its own size."""
    by: dict[str, list[dict]] = {}
    for r in recs:
        by.setdefault(group_key(r), []).append(r)
    return [by[g][rng.randrange(len(by[g]))] for g in sorted(by) for _ in by[g]]


def _held_out(recs: list[dict], account: str) -> list[dict]:
    return [r for r in recs if r["account"] != account]


def pooled_section(data: dict[str, list[dict]], seed: int, resamples: int) -> dict | None:
    """The adopted fit, its bootstrap, Fable either side of the cut, and each account left out."""
    recs = pooled_records(data)
    free, held = pooled_families(recs)
    base = pooled_fit(recs, free)
    if base is None:
        return None
    rng = random.Random(f"{seed}/pooled")
    draws: dict[str, list[float]] = {f: [] for f in free}
    weights: list[float] = []
    for _ in range(resamples):
        b = pooled_fit(_bootstrap_draw(recs, rng), free, x0=base["params"])
        for f in free:
            draws[f].append(b["times_opus"][f])
        weights.append(b["cache_read_weight"])
    out = {"n": base["n"], "groups": base["groups"], "free": list(free), "held": list(held),
           "held_rate": {f: held_rate(f) for f in held}, "output_multiplier": POOLED_OUT_MULT,
           "times_opus": base["times_opus"], "cache_read_weight": base["cache_read_weight"],
           "sse": base["sse"], "credits_per_pct": base["credits_per_pct"],
           "resamples": resamples,
           "interval": {f: list(interval(v)) for f, v in draws.items()},
           "median": {f: st.median(v) for f, v in draws.items()},
           "cache_read_weight_interval": list(interval(weights)),
           "n_nonzero": {f: sum(1 for r in recs if r["raw"][f] > 0) for f in FAMILIES},
           "groups_carrying": {f: sorted({group_key(r) for r in recs if r["raw"][f] > 0})
                               for f in FAMILIES}}
    # Fable's rate either side of the cut, from the same stretches and the same resamples'
    # seeds, so the question "did Fable's rate move at 14 September" has a measured answer.
    if "fable" in free:
        split = pooled_fit(recs, free, split="fable")
        rng = random.Random(f"{seed}/pooled-fable-by-era")
        pre, post, ratio = [], [], []
        x0 = split["params"]
        for _ in range(resamples):
            b = pooled_fit(_bootstrap_draw(recs, rng), free, split="fable", x0=x0)
            pre.append(b["times_opus"]["fable"])
            post.append(b["post_rate"])
            ratio.append(b["post_rate"] / b["times_opus"]["fable"])
        out["fable_by_era"] = {
            "pre": split["times_opus"]["fable"], "pre_interval": list(interval(pre)),
            "post": split["post_rate"], "post_interval": list(interval(post)),
            "post_over_pre": split["post_rate"] / split["times_opus"]["fable"],
            "post_over_pre_interval": list(interval(ratio)), "sse": split["sse"]}
    out["leave_one_out"] = {}
    for account in sorted({r["account"] for r in recs}):
        sub = _held_out(recs, account)
        f_sub = pooled_fit(sub, free, x0=base["params"]) if sub else None
        out["leave_one_out"][account] = (
            {"n": f_sub["n"], "times_opus": f_sub["times_opus"],
             "cache_read_weight": f_sub["cache_read_weight"]} if f_sub else None)
    return out


def measurable(iv: list[float] | None) -> bool:
    """A bootstrap interval that pins a rate down: finite, above zero, and narrow enough."""
    if not iv:
        return False
    lo, hi = iv
    return bool(np.isfinite(lo) and np.isfinite(hi) and lo > 0 and hi / lo < MAX_INTERVAL_RATIO)


#: Why a family has no measured rate at all, in the words the published row carries. The
#: publisher prints this instead of a number, and the page shows the reference figure beside
#: it: it is used in no arithmetic that the page states.
NOT_MEASURABLE = "not measurable, no clean stretch is {family}-heavy"
#: The same, for a family the pooled fit does carry but cannot pin down: its bootstrap
#: interval reaches zero, or its high end is MAX_INTERVAL_RATIO or more times its low end.
#: Haiku is the case today: it carries at most a quarter of any clean stretch, and the
#: resamples include ones where the other columns absorb it whole. Neither the value nor
#: the interval is published -- an interval is priced at its midpoint where a family has no
#: value (tracker/gs_passive.py) -- and both stay on the record in `pooled`.
NOT_PINNED = ("not measurable, the pooled fit cannot pin {family} down: its 80% interval is "
              "{ratio} wide end to end, and a published rate needs under {limit:g}x")


def measured_rates(s1: dict, s3: dict, pooled: dict | None, variant: str = "joint",
                   mult: str = "out5x") -> dict:
    """The rates the publisher adopts, per family, with Opus as the unit anchor.

    This is the block `tracker/credits.py` reads out of history/model-rates.json. Every
    non-anchor family's rate comes from the pooled fit (`pooled_section`): one set of rates
    shared by every account and side of the cut, one scale per group. A family carries one
    of two things and never both: a value with its bootstrap interval, when that interval
    is `measurable`; or no value and a sentence saying why not. The per-group least-squares
    fits of section 3 stay on the record in `per_fit` as diagnostics and enter no rate.
    `pooled_fit` carries the fit's whole coefficient vector, the withheld families' point
    estimates included, because tracker/credits.py `across_cut` prices with the fit's own
    model on both sides of the cut. `reference_input` is the January table's figure, carried
    so the page can draw it beside the measurement; nothing here divides by it.
    """
    diagnostics = adopt(s3, variant, mult)
    opus_in = SHELLAC[ANCHOR]
    max_share = {f: max((row["max_share"].get(f, 0.0) for key, row in s1.items()
                         if key.split("/")[0] in FIT_ACCOUNTS), default=0.0) for f in FAMILIES}
    pooled = pooled or {}
    free = pooled.get("free") or []
    per_family = {}
    for f in FAMILIES:
        anchor = f == ANCHOR
        iv = (pooled.get("interval") or {}).get(f)
        point = (pooled.get("times_opus") or {}).get(f) if f in free else None
        ok = anchor or (point is not None and measurable(iv))
        value = opus_in if anchor else (point * opus_in if ok else None)
        rate_iv = [iv[0] * opus_in, iv[1] * opus_in] if ok and not anchor else None
        if ok:
            status = None
        elif f in free:
            wide = None if not iv or iv[0] <= 0 else iv[1] / iv[0]
            ratio = "over 100x" if wide is None or wide >= 100 else f"{wide:.2f}x"
            status = NOT_PINNED.format(family=name(f), ratio=ratio, limit=MAX_INTERVAL_RATIO)
        else:
            status = NOT_MEASURABLE.format(family=name(f))
        per_family[f] = {
            "input": value,
            "interval": rate_iv,
            "output_multiplier": POOLED_OUT_MULT,
            "status": status,
            # The anchor is the reference table's own Opus row: this work measures every other
            # family against it and cannot test it, so the row says `reference` rather than
            # claiming a measurement of the one rate nothing here measures.
            "rate_source": "reference" if anchor else "measured",
            "anchor": anchor,
            "reference_input": SHELLAC.get(f),
            "times_opus": (value / opus_in if value else None),
            "times_opus_interval": ([iv[0], iv[1]] if rate_iv else None),
            "pooled": ({"times_opus": point, "interval": iv,
                        "n_stretches_carrying": (pooled.get("n_nonzero") or {}).get(f),
                        "groups_carrying": (pooled.get("groups_carrying") or {}).get(f)}
                       if f in free else None),
            # How many groups' stretches carry the family's tokens into the pooled fit.
            "n_fits": len((pooled.get("groups_carrying") or {}).get(f) or []) if f in free else 0,
            "agree": None,
            "per_fit": diagnostics[f]["per_fit"],
            "max_share_of_a_clean_stretch": max_share[f],
            # What the page shows while the fit cannot measure the family: tracker/credits.py
            # family_rate publishes it with rate_source "inferred". Gone the moment the family
            # passes the measurable rule.
            "inferred": None if ok else inferred_rate(f),
        }
    groups = ", ".join(pooled.get("groups") or [])
    per_family[ANCHOR]["why"] = (
        f"the unit anchor: {opus_in:.4f} credits per input token ({POOLED_OUT_MULT}x that per "
        "output token), which is what a credit means in this repository. The pooled fit holds "
        "it at 1 and measures every other rate relative to it, so none of them tests it. If "
        "this row is wrong every credit figure scales with it and nothing in our stretches "
        "would show it.")
    for f in FAMILIES:
        row = per_family[f]
        if f == ANCHOR:
            continue
        reference = ("The reference figure stands beside this row, untested by our data and "
                     "used in no arithmetic the page states." if SHELLAC.get(f) is not None else
                     f"There is no reference figure for {name(f)} either: the January table "
                     "predates it.")
        pooled_row = row["pooled"]
        if row["input"] is not None:
            iv = pooled_row["interval"]
            row["why"] = (
                f"one fit pooled over {groups}: rates shared by every group, one credits-per-1% "
                f"scale per group. {name(f)} {pooled_row['times_opus']:.3f}x Opus, 80% bootstrap "
                f"interval [{iv[0]:.3f}, {iv[1]:.3f}] from {pooled.get('resamples')} resamples "
                f"stratified by group; {pooled_row['n_stretches_carrying']} of the fit's "
                f"{pooled.get('n')} stretches carry its tokens. The per-group fits are on this "
                "record as diagnostics and enter no rate.")
        elif pooled_row is not None:
            iv = pooled_row["interval"]
            row["why"] = (
                f"the highest {name(f)} share of any clean stretch on a fitted account is "
                f"{max_share[f]:.3f}. The pooled fit carries it and puts it at "
                f"{pooled_row['times_opus']:.3f}x Opus, but its 80% interval runs "
                f"[{iv[0]:.3f}x, {iv[1]:.3f}x], so the data do not pin it down, and neither the "
                "value nor the interval is published: an interval is priced at its midpoint where "
                "a family has no value (tracker/gs_passive.py), and this one's midpoint is not a "
                "rate anything measured. The fit's point estimate still prices this family's "
                "tokens inside the before-and-after comparison (pooled_fit), where the fit's own "
                f"model is used whole. {reference}")
        elif f in (pooled.get("held") or []):
            row["why"] = (
                f"the highest {name(f)} share of any clean stretch on a fitted account is "
                f"{max_share[f]:.3f}, and only {(pooled.get('n_nonzero') or {}).get(f, 0)} of the "
                f"fit's stretches carry it at all, under the {MIN_NONZERO} a fitted family needs. "
                f"The pooled fit holds its tokens at its inferred rate rather than dropping the "
                f"stretches that carry them, and measures nothing about it. {reference}")
        else:
            row["why"] = (
                f"the highest {name(f)} share of any clean stretch on a fitted account is "
                f"{max_share[f]:.3f}, and no stretch the fit runs over carries it at all, so "
                f"there is nothing to measure a rate from. {reference}")
    weight = {"value": None, "interval": None, "measurable": False,
              "reference": C.cache_read_weight(CREDITS),
              "reference_range": CREDITS.get("cache_read_weight_range"),
              "per_fit": weight_pool(s3, variant, mult)["per_fit"]}
    if pooled:
        wiv = pooled["cache_read_weight_interval"]
        weight.update({"fit_point": pooled["cache_read_weight"], "interval": wiv,
                       "measurable": measurable(wiv)})
        weight["value"] = pooled["cache_read_weight"] if weight["measurable"] else None
        weight["why"] = (
            f"the pooled fit puts a cache read at {pooled['cache_read_weight']:.4f} of the Opus "
            f"input rate, 80% interval [{wiv[0]:.4f}, {wiv[1]:.4f}]"
            + ("." if weight["measurable"] else
               ", which reaches zero or is too wide to publish as a value; the point estimate "
               "is used only inside the before-and-after comparison, with the rest of the fit."))
    return {
        "unit": "credits per input token; the output rate is output_multiplier times it",
        "variant": "pooled", "output_multiplier": POOLED_OUT_MULT,
        "fits_pooled": list(pooled.get("groups") or []),
        "anchor": {"family": ANCHOR, "input": opus_in, "output": opus_in * POOLED_OUT_MULT},
        "per_family": per_family,
        "cache_read_weight": weight,
        "max_interval_ratio": MAX_INTERVAL_RATIO,
        "pooled_fit": ({
            "times_opus": pooled["times_opus"],
            "held": pooled.get("held", []), "held_rate": pooled.get("held_rate", {}),
            "cache_read_weight": pooled["cache_read_weight"],
            "output_multiplier": POOLED_OUT_MULT,
            "n": pooled["n"], "groups": pooled["groups"],
            "credits_per_pct": pooled["credits_per_pct"],
            "sse": pooled["sse"], "resamples": pooled["resamples"],
            "interval": pooled["interval"],
            "cache_read_weight_interval": pooled["cache_read_weight_interval"],
            "fable_by_era": pooled.get("fable_by_era"),
            "leave_one_out": pooled["leave_one_out"],
        } if pooled else None),
        "method": (
            f"credits_s = sum over families of rate_f x (input + cache_write + {POOLED_OUT_MULT}x "
            "output)_f,s + w x cache_reads_s, with Opus at 1 and every other rate and w free and "
            "positive; one set of rates for every account and side of 2026-09-14, and one "
            "credits-per-1% scale per account and side. Minimises, over the rates, the sum over "
            "groups and their stretches of (log(credits_s / delta_pct_s) minus that group's mean "
            "of it) squared: the group's scale is the mean, so it is never a parameter. "
            "Nelder-Mead in log space, numpy only. The rates are then read in credits against "
            f"the Opus anchor, {opus_in:.4f} credits per token. Intervals are 80% bootstrap "
            "intervals over stretches resampled within each group. A family is published when "
            f"its interval starts above zero and its high end is under {MAX_INTERVAL_RATIO:g}x its "
            f"low end; a family in fewer than {MIN_NONZERO} of the stretches is held at its "
            "inferred rate and not measured. A family that is not measurable carries an "
            "`inferred` rate -- the reference table's, or its list-price ratio to Opus where the "
            "table has no row -- which the page shows marked as inferred. Stretches that span the "
            "cut are in neither group."),
    }


def section5(window: dict, mr: dict) -> dict:
    """The five-hour window in input-equivalent tokens per model, Shellac's rate beside the measured one.

    The rows are the publisher's own rule: a token figure only where the rate has a value, and
    the rate's status sentence with the inverted interval where it does not (cheapest rate
    against the top of the window, dearest against the bottom).
    """
    per_pct = window["harness-runs"]["median"]
    credits = per_pct * 100
    rows = {}
    for f, a in mr["per_family"].items():
        row = {"shellac_rate": a["reference_input"], "measured_rate": a["input"],
               "rate_interval": a["interval"], "status": a["status"],
               "rate_source": a["rate_source"],
               "shellac_tokens": credits / a["reference_input"] if a["reference_input"] else None,
               "measured_tokens": credits / a["input"] if a["input"] else None,
               # An interval whose ends are both above zero inverts into a token interval; one
               # that reaches zero does not -- a free rate buys an unbounded number of tokens,
               # and `inf` is not a figure to publish beside the others. The rate interval is
               # still carried above, so the reader sees what the fit did say.
               "measured_tokens_interval": ([credits / a["interval"][1], credits / a["interval"][0]]
                                            if a["interval"] and a["interval"][0] > 0
                                            and a["interval"][1] > 0 else None)}
        rows[f] = row
    return {"window_credits_per_pct": per_pct, "window_credits": credits, "rows": rows}


def pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def show(excl: dict, win: dict, s1: dict, s2: dict, s3: dict, s4: dict, s5: dict, mr: dict,
         lists: dict, chosen: str = "harness-runs") -> None:
    print(f"0. Exclusion list ({chosen} is the one sections 1 to 5 use)")
    kinds = {}
    for run in lists["harness-runs"]:
        kinds[run.account] = kinds.get(run.account, 0) + 1
    where = HARNESS_RUNS.relative_to(Path(__file__).resolve().parent.parent)
    print(f"   {len(lists['probes-only'])} runs from probes.jsonl and the effort matrix, "
          f"{len(lists['harness-runs'])} from {where} "
          f"({', '.join(f'{a} {n}' for a, n in sorted(kinds.items()))})")
    for a, e in excl.items():
        extra = ", ".join(f"{d['start'][:19]} ({d['era']})" for d in e["dropped"]) or "none"
        print(f"   {a:9} clean stretches {e['probes_only']:3} -> {e['harness_runs']:3}   removed beyond the current rule: {extra}")
    for label, q in win.items():
        if q:
            print(f"   jwork pure-Opus pre-14-Sep window, {label:12} n={q['n']:2} median={q['median']:,.0f} "
                  f"credits per 1% range={q['min']:,.0f}-{q['max']:,.0f}")

    print(f"\n1. Input-equivalent tokens per 1%, stretches one model carries at >= {DOMINANCE:.0%} of raw tokens")
    for key, row in s1.items():
        print(f"   {key:16} highest share one model reaches: "
              + " ".join(f"{f}={v:.3f}" for f, v in row["max_share"].items()))
        for f, per in row.items():
            if f == "max_share":
                continue
            q5, q3 = per["out5x"], per["out3x"]
            tag = "" if q5["measurable"] else "   NOT MEASURABLE (n < %d)" % MIN_N
            print(f"   {key:16} {f:7} n={q5['n']:3} out5x median={q5['median']:11,.0f} "
                  f"IQR={q5['p25']:11,.0f}-{q5['p75']:11,.0f} | out3x median={q3['median']:11,.0f}{tag}")

    print(f"   masterrig counts only from {MASTERRIG_FROM:%Y-%m-%d}: before it the takeoff pipeline on gs "
          "moved its meter\n   and its transcripts had been cleaned up "
          "(docs/findings-2026-09-23-masterrig-admitted.md).")

    print("\n2. The meter's model ratios, from the single-model stretches above")
    for key, per in s2.items():
        account, era, pair = key.split("/")
        r5 = per["out5x"]
        if not r5["measurable"]:
            print(f"   {account:9} {era:4} {pair:13} NOT MEASURABLE (n={r5['n_num']} and {r5['n_den']}, "
                  f"need {MIN_N} a side)")
            continue
        r3 = per["out3x"]
        print(f"   {account:9} {era:4} {pair:13} n={r5['n_num']}/{r5['n_den']} ratio={r5['ratio']:.3f} "
              f"[{r5['interval'][0]:.3f}, {r5['interval'][1]:.3f}] prior={r5['prior']:.3f} "
              f"{r5['verdict'].upper()} (interval spans x{r5['interval_width']:.1f}); {r5['free']} "
              f"tokens-per-window row moves {pct(r5['row_move'])} (out3x ratio={r3['ratio']:.3f})")

    print("\n3. Rates fitted across every clean stretch, Opus fixed at 10/15")
    for key, f in s3.items():
        for label, v in (("cache reads at 0", f), ("cache-read weight fitted", f.get("joint"))):
            if not v:
                print(f"   {key:22} {label}: NO FIT (no stretch of the group carries cache reads)")
                continue
            weight = ""
            if v.get("cache_read_weight_interval"):
                lo, hi = v["cache_read_weight_interval"]
                weight = (f" cache_read_weight={v['cache_read_weight']:.4f} [{lo:.4f}, {hi:.4f}]"
                          f" of the Opus input rate")
            print(f"   {key:22} {label:24} n={v['n']:3} "
                  f"window={v['window_credits_per_pct']:,.0f} credits per 1% "
                  f"[{v['window_interval'][0]:,.0f}, {v['window_interval'][1]:,.0f}]  "
                  f"residual |rel| median={v['residual_median_abs_rel']:.3f} "
                  f"p90={v['residual_p90_abs_rel']:.3f}{weight}")
            for fam, rate in v["rates"].items():
                lo, hi = v["interval"][fam]
                if fam == "opus":
                    against = "Shellac 0.667, fixed to set the scale"
                elif SHELLAC.get(fam) is None:
                    against = f"no Shellac rate: {name(fam)} is not in the table"
                else:
                    against = (f"Shellac {SHELLAC[fam]:.3f} "
                               f"{'inside' if lo <= SHELLAC[fam] <= hi else 'OUTSIDE'} the interval")
                print(f"      {fam:7} rate={rate:.3f} [{lo:.3f}, {hi:.3f}]  {against}")
            for pair, r in v["ratios"].items():
                if not r["measurable"]:
                    print(f"      {pair:13} NOT MEASURABLE (the fit put one of the two rates at zero)")
                    continue
                # A ratio whose interval starts at zero has no width to print (verdict() sets
                # it to None rather than dividing by zero).
                w = r["interval_width"]
                wide = ("  interval reaches zero" if w is None
                        else f"  interval spans x{w:.1f}" if w > 3 else "")
                print(f"      {pair:13} {r['ratio']:.3f} [{r['interval'][0]:.3f}, {r['interval'][1]:.3f}] "
                      f"prior={r['prior']:.3f} {r['verdict'].upper()}; {r['free']} row moves "
                      f"{pct(r['row_move'])}{wide}")

    print("\n4. The jwork/Dave gap after 14 September")
    print(f"   n: jwork {s4['n']['jwork']}, dave {s4['n']['dave']}   median raw-token shares:")
    for a, sh in s4["shares"].items():
        print(f"      {a:6} " + " ".join(f"{f}={v:.3f}" for f, v in sh.items()))
    for label, m in s4["mix"].items():
        print(f"   priced at {label:20} jwork={m['jwork']:9,.0f} dave={m['dave']:9,.0f} "
              f"ratio={m['ratio']:.3f} [{m['interval'][0]:.3f}, {m['interval'][1]:.3f}] "
              f"mix {m['verdict'].upper()} the gap")
    for key, q in s4["quantisation"].items():
        print(f"   whole-percent floor {key:16} n={q['n']:3} +-{q['per_stretch'] * 100:.1f}% a stretch, "
              f"delta_pct values {', '.join(f'{d:.0f}' for d in q['deltas'])}")
    for key, e in s4["reset_verified"].items():
        parts = " ".join(f"{k}: n={v['n']} median={v['median']:,.0f}" for k, v in e.items()
                         if k in ("True", "False") and v)
        r = e.get("ratio")
        extra = (f" ratio verified/not={r['ratio']:.3f} [{r['interval'][0]:.3f}, {r['interval'][1]:.3f}]"
                 if r else f"  (one side under {MIN_N}, not measurable)")
        print(f"   reset_verified {key:16} {parts}{extra}")
    for key, e in s4["length"].items():
        parts = " ".join(f"{k}: n={e[k]['n']} median={e[k]['median']:,.0f}" for k in ("short", "long") if e[k])
        r = e.get("ratio")
        extra = (f" ratio long/short={r['ratio']:.3f} [{r['interval'][0]:.3f}, {r['interval'][1]:.3f}]"
                 if r else f"  (one side under {MIN_N}, not measurable)")
        print(f"   length         {key:16} cut at delta_pct={e['cut_delta_pct']:.0f} {parts}{extra}")
    for label, r in s4["restricted"].items():
        if r.get("measurable") is False:
            print(f"   restricted to {label:20} NOT MEASURABLE "
                  f"(n={r['n']['jwork']}/{r['n']['dave']}, need {MIN_N} a side)")
        else:
            print(f"   restricted to {label:20} jwork={r['jwork']:9,.0f} dave={r['dave']:9,.0f} "
                  f"ratio={r['ratio']:.3f} [{r['interval'][0]:.3f}, {r['interval'][1]:.3f}] "
                  f"n={r['n']['jwork']}/{r['n']['dave']} {r['verdict'].upper()} the gap")

    print(f"\n5. The five-hour window per model, at {s5['window_credits']:,.0f} credits "
          f"({s5['window_credits_per_pct']:,.0f} per 1%), input-equivalent tokens at output 5x")
    for f, row in s5["rows"].items():
        shellac = (f"Shellac rate={row['shellac_rate']:.3f} tokens={row['shellac_tokens']:,.0f}"
                   if row["shellac_rate"] else f"Shellac rate=absent ({name(f)} is not in the table)")
        if row["measured_rate"] and row["rate_interval"]:
            iv = row["measured_tokens_interval"]
            measured = (f"measured rate={row['measured_rate']:.3f} "
                        f"[{row['rate_interval'][0]:.3f}, {row['rate_interval'][1]:.3f}] "
                        f"tokens={row['measured_tokens']:,.0f} [{iv[0]:,.0f}, {iv[1]:,.0f}]")
        elif row["measured_rate"]:
            measured = (f"{row['rate_source']} rate={row['measured_rate']:.3f} (the anchor) "
                        f"tokens={row['measured_tokens']:,.0f}")
        elif row["rate_interval"]:
            iv = row["measured_tokens_interval"]
            tokens = (f" tokens [{iv[0]:,.0f}, {iv[1]:,.0f}]" if iv
                      else " tokens unbounded (the rate interval reaches zero)")
            measured = (f"{row['status']}: rate interval "
                        f"[{row['rate_interval'][0]:.3f}, {row['rate_interval'][1]:.3f}]"
                        f"{tokens}, no value")
        else:
            measured = row["status"] or "measured rate=NOT MEASURABLE"
        print(f"   {f:7} {shellac:52} {measured}")

    pf = mr.get("pooled_fit")
    print(f"\n6. The rates the publisher adopts: one fit pooled over "
          f"{', '.join(mr['fits_pooled']) or 'nothing'}, output {mr['output_multiplier']}x")
    if pf:
        print(f"   n={pf['n']} stretches, SSE={pf['sse']:.3f}, {pf['resamples']} resamples "
              f"stratified by group; held: " + (", ".join(f"{f} at {v:.3f}x" for f, v in
                                                          pf["held_rate"].items()) or "none"))
        print("   credits per 1% per group: " + ", ".join(
            f"{g} {v:,.0f}" for g, v in pf["credits_per_pct"].items()))
    for f, row in mr["per_family"].items():
        ref = f"reference={row['reference_input']:.3f}" if row["reference_input"] else "reference=absent"
        if row["input"] is not None and row["interval"]:
            lo, hi = row["times_opus_interval"]
            what = f"input={row['input']:.4f} ({row['times_opus']:.3f}x Opus [{lo:.3f}, {hi:.3f}])"
        elif row["input"] is not None:
            what = f"input={row['input']:.4f} (the anchor)"
        else:
            what = row["status"]
            if row.get("inferred"):
                what += (f"; shown INFERRED at {row['inferred']['times_opus']:.3f}x Opus "
                         f"({row['inferred']['inferred_from']})")
            if row.get("pooled"):
                iv = row["pooled"]["interval"]
                what += (f" (fit point {row['pooled']['times_opus']:.3f}x Opus "
                         f"[{iv[0]:.3f}, {iv[1]:.3f}])")
        print(f"   {f:8} {row['rate_source']:9} {ref:18} {what}")
    w = mr["cache_read_weight"]
    if w.get("interval"):
        value = f"{w['value']:.4f}" if w["value"] is not None else "no value"
        print(f"   cache-read weight {value} (fit point {w['fit_point']:.4f}) "
              f"[{w['interval'][0]:.4f}, {w['interval'][1]:.4f}] of the Opus input rate; "
              f"data/prices.json holds {w['reference']:g} with the range {w['reference_range']}")
    if pf and pf.get("fable_by_era"):
        e = pf["fable_by_era"]
        print(f"   Fable by era: pre {e['pre']:.3f}x [{e['pre_interval'][0]:.3f}, "
              f"{e['pre_interval'][1]:.3f}], post {e['post']:.3f}x [{e['post_interval'][0]:.3f}, "
              f"{e['post_interval'][1]:.3f}], post/pre {e['post_over_pre']:.3f} "
              f"[{e['post_over_pre_interval'][0]:.3f}, {e['post_over_pre_interval'][1]:.3f}]")
    if pf:
        for account, v in pf["leave_one_out"].items():
            if v:
                print(f"   without {account:9} n={v['n']:3} " + " ".join(
                    f"{f}={x:.3f}" for f, x in v["times_opus"].items() if f != ANCHOR)
                      + f" w={v['cache_read_weight']:.4f}")
    print("   per-group fits (diagnostics only, section 3):")
    for label, v in group_fits(s3, "joint", "out5x").items():
        r = v["ratios"].get("opus:sonnet")
        if r and r.get("measurable"):
            print(f"   opus:sonnet {label:15} {r['ratio']:.3f} "
                  f"[{r['interval'][0]:.3f}, {r['interval'][1]:.3f}] against the table's "
                  f"{r['prior']:.3f}: {'EXCLUDED' if r['verdict'] == 'disagrees' else 'inside'} "
                  f"the interval")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("masterrig", type=Path, help="a masterrig stretch file, as tools/reconcile_window.py takes")
    ap.add_argument("--json", type=Path, help="write the same figures as JSON")
    ap.add_argument("--exclusions", choices=("harness-runs", "probes-only"), default="harness-runs",
                    help="which list of the tracker's own runs to exclude stretches by")
    ap.add_argument("--variant", choices=("joint", "zero"), default="joint",
                    help="the per-group diagnostic fits kept beside the pooled rates: `joint` fits the cache-read weight "
                         "with the rates, `zero` holds it at 0 as the reference table reads")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--resamples", type=int, default=RESAMPLES)
    a = ap.parse_args(argv)
    S = C.stretches_by_account(json.loads(Path("history/gs-passive.json").read_text(encoding="utf-8")),
                               json.loads(a.masterrig.read_text(encoding="utf-8")))
    lists = {"probes-only": probes_only_runs(), "harness-runs": C.harness_runs()}
    excl = exclusion_report(S, lists)
    win = window_check(S, lists)
    kept = clean(S, lists[a.exclusions])
    data = {acct: prepare(acct, kept[acct]) for acct in S}
    # Every group draws from its own seeded resampler, keyed by its name, so a figure quoted
    # from one row does not move when a different row gains or loses a stretch.
    s1 = section1(data)
    s2 = section2(data, a.seed, a.resamples)
    s3 = section3(data, a.seed, a.resamples)
    s4 = section4(data, s3, a.seed, a.resamples)
    pooled = pooled_section(data, a.seed, a.resamples)
    mr = measured_rates(s1, s3, pooled, a.variant)
    s5 = section5(win, mr)
    show(excl, win, s1, s2, s3, s4, s5, mr, lists, a.exclusions)
    if a.json:
        command = (f"python3 -m tools.model_rates {a.masterrig} --json {a.json}"
                   + ("" if a.variant == "joint" else f" --variant {a.variant}")
                   + ("" if a.exclusions == "harness-runs" else f" --exclusions {a.exclusions}")
                   + ("" if a.seed == SEED else f" --seed {a.seed}")
                   + ("" if a.resamples == RESAMPLES else f" --resamples {a.resamples}"))
        a.json.write_text(json.dumps({
            "_meta": {
                "what": "the meter's own per-model credit rates, fitted from the passive stretches "
                        "already in the repository. `measured_rates` is the block tracker/credits.py "
                        "reads; everything else is the working the fits show.",
                "command": command,
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "inputs": ["history/gs-passive.json", str(a.masterrig), "history/harness-runs.jsonl",
                           "history/probes.jsonl", "data/effort_matrix.json", "data/prices.json"],
                "findings": ["docs/findings-2026-09-20-measured-rates.md",
                             "docs/findings-2026-09-23-sonnet-rate.md",
                             "docs/findings-2026-09-23-opus-5-5-and-dominance.md",
                             "docs/findings-2026-09-23-masterrig-admitted.md",
                             "docs/findings-2026-09-23-pooled-rates.md"],
                "no_traffic": "read-only arithmetic over committed files; no account was driven",
            },
            "measured_rates": mr,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "seed": a.seed, "resamples": a.resamples, "exclusions": a.exclusions,
            "variant": a.variant,
            "dominance": DOMINANCE, "min_n": MIN_N,
            "pool_dominance_share": DOMINANCE_SHARE, "pool_dominance_min_n": DOMINANCE_MIN_N,
            "max_interval_ratio": MAX_INTERVAL_RATIO,
            "fit_accounts": list(FIT_ACCOUNTS), "masterrig_from": MASTERRIG_FROM.isoformat(),
            "interval_percentiles": INTERVAL, "shellac_rates": SHELLAC, "fable_point": FABLE_POINT,
            "fable_interval": FABLE_INTERVAL, "exclusion": excl, "window_check": win,
            "section1": s1, "section2": s2, "section3": s3, "section4": s4, "section5": s5,
            "adopt": adopt(s3, a.variant),
            "adopt_cache_read_zero": adopt(s3, "zero"),
            "adopt_output_3x": adopt(s3, a.variant, "out3x")}, indent=1, default=float) + "\n")
        print(f"\nJSON -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
