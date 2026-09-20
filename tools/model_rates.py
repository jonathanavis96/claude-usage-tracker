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

Token counts are grouped into model families (every claude-opus-* into Opus, and so on),
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

FAMILIES = ("opus", "sonnet", "haiku", "fable")
OUT_MULTS = (5, 3)
DOMINANCE = 0.95        # share of raw tokens one family must carry for a stretch to price it alone
MIN_N = 3               # fewer stretches than this on either side of a ratio is not measurable
MIN_FIT_N = 8           # fewer clean stretches than this does not support a four-rate fit
MIN_NONZERO = 3         # a family needs this many stretches with tokens before it is fitted
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


def api_ratio(prices: dict, num: str, den: str) -> float | None:
    """The API list-price ratio between two families, from data/prices.json, or None."""
    def rate(f):
        v = [b["input"] for m, b in prices.items() if not m.startswith("_") and family(m) == f]
        return v[0] if v else None
    a, b = rate(num), rate(den)
    return a / b if a and b else None


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


def clean(by_account: dict[str, list[dict]], runs: list) -> dict[str, list[dict]]:
    """The reconciliation's own selection: capture-accepted, harness-clean, masterrig exempt."""
    return C.clean_stretches(by_account, runs, require="capture_status", exempt=("masterrig",))


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


#: The accounts whose fits are poolable. masterrig's residual |rel| median is 0.455 against
#: 0.055 to 0.065 on these two -- the phantom meter movement showing up as fit error -- so its
#: fit is reported for completeness and never adopted.
FIT_ACCOUNTS = ("jwork", "dave")
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
    """Pool the gs fits into one rate per family, whether or not the fits agree.

    A family's point estimate is the median of the fits' point estimates and its interval is
    the union of theirs. `agree` is still recorded (every fit's interval for the family
    overlaps every other's, or not) and still published, because it says how much the median
    is trusted -- but it no longer withholds the value: Fable fits 1.754 [1.619, 1.927] times
    Opus on jwork before 14 September and 3.798 [3.324, 4.324] after, and where those disagree
    the published rate is the median of the two with the union of their intervals as its
    interval, not a status sentence in place of a number.
    """
    fits = group_fits(s3, variant, mult)
    out = {}
    for f in FAMILIES:
        pts, ivs, per_fit = [], [], {}
        for label, v in sorted(fits.items()):
            rate = v["rates"].get(f, 0)
            iv = v["interval"].get(f)
            if rate > 0 and iv:
                pts.append(rate)
                ivs.append(list(iv))
                per_fit[label] = {"rate": rate, "interval": list(iv), "n": v["n"]}
        row = {"measured": None, "interval": None, "n_fits": len(pts), "points": sorted(pts),
               "per_fit": per_fit, "agree": None, "shellac": SHELLAC.get(f), "variant": variant}
        if len(pts) >= MIN_N - 1 and ivs:
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


#: Why a family has no measured rate at all, in the words the published row carries. The
#: publisher prints this instead of a number, and the page shows the reference figure beside
#: it: it is used in no arithmetic. A family whose fits disagree is not this case any more --
#: it gets a value (the median) and an interval (the union) and `status: null`, with `agree:
#: false` on the record and a `why` sentence saying so; see `adopt`.
NOT_MEASURABLE = "not measurable, no clean stretch is {family}-heavy"


def measured_rates(s1: dict, s3: dict, variant: str = "joint", mult: str = "out5x") -> dict:
    """The rates the publisher adopts, per family, with Opus as the unit anchor.

    This is the block `tracker/credits.py` reads out of history/model-rates.json. Every family
    carries one of two things and never both: a value with an interval (whether or not its
    fits agreed -- `agree` and `why` say which, and `status` is null either way), or no value
    at all and the sentence saying the rate is not measurable. `reference_input` is the January
    table's figure, carried so the page can draw it beside the measurement; nothing here
    divides by it.
    """
    pooled = adopt(s3, variant, mult)
    out_mult = int(mult.removeprefix("out").removesuffix("x"))
    opus_in = SHELLAC["opus"]
    max_share = {f: max((row["max_share"][f] for key, row in s1.items()
                         if key.split("/")[0] in FIT_ACCOUNTS), default=0.0) for f in FAMILIES}
    per_family = {}
    for f in FAMILIES:
        row = pooled[f]
        anchor = f == "opus"
        value = opus_in if anchor else row["measured"]
        status = None if anchor or value is not None else NOT_MEASURABLE.format(family=f.capitalize())
        per_family[f] = {
            "input": value,
            "interval": None if anchor else row["interval"],
            "output_multiplier": out_mult,
            "status": status,
            # The anchor is the reference table's own Opus row: this work measures every other
            # family against it and cannot test it, so the row says `reference` rather than
            # claiming a measurement of the one rate nothing here measures.
            "rate_source": "reference" if anchor else "measured",
            "anchor": anchor,
            "reference_input": SHELLAC.get(f),
            "times_opus": (value / opus_in if value else None),
            "times_opus_interval": ([row["interval"][0] / opus_in, row["interval"][1] / opus_in]
                                    if row["interval"] else None),
            "n_fits": row["n_fits"], "points": row["points"], "per_fit": row["per_fit"],
            "agree": row["agree"],
            "max_share_of_a_clean_stretch": max_share[f],
        }
    per_family["opus"]["why"] = (
        f"the unit anchor: {opus_in:.4f} credits per input token ({out_mult}x that per output "
        "token), which is what a credit means in this repository. The fit rescales its Opus "
        "coefficient to this figure, so every rate beside it is measured relative to it and "
        "none of them tests it. If this row is wrong every credit figure scales with it and "
        "nothing in our stretches would show it.")
    for f in FAMILIES:
        row = per_family[f]
        if f == "opus" or row["agree"] is not False:
            continue
        sides = ", ".join(f"{label} {v['rate'] / opus_in:.3f}x Opus "
                          f"[{v['interval'][0] / opus_in:.3f}, {v['interval'][1] / opus_in:.3f}]"
                          for label, v in row["per_fit"].items())
        row["why"] = (
            f"the fits disagree within their intervals ({sides}), so the published rate is the "
            f"median of the per-fit rates ({row['input']:.4f} credits per input token, "
            f"{row['times_opus']:.3f}x Opus) and the interval is the union of the per-fit "
            "intervals rather than a single fit's. The data cannot say whether the family's "
            "rate moved or the five-hour window did: the window fitted on the same stretches "
            "moves the same way.")
    if per_family["haiku"]["status"]:
        per_family["haiku"]["why"] = (
            f"the highest Haiku share of any clean stretch on a fitted account is "
            f"{max_share['haiku']:.3f}, and no fit includes Haiku at all, so there is nothing "
            "to measure a Haiku rate from. The reference figure stands beside this row, "
            "untested by our data and used in no arithmetic.")
    return {
        "unit": "credits per input token; the output rate is output_multiplier times it",
        "variant": variant, "output_multiplier": out_mult,
        "fits_pooled": sorted(group_fits(s3, variant, mult)),
        "anchor": {"family": "opus", "input": opus_in, "output": opus_in * out_mult},
        "per_family": per_family,
        "cache_read_weight": weight_pool(s3, variant, mult),
        "method": (
            f"delta_pct = sum over families of (input + cache_write + {out_mult}x output) x rate, "
            "plus a cache-read column, by non-negative least squares over every clean stretch of "
            "the account and side of 2026-09-14, with the Opus coefficient rescaled afterwards to "
            f"{opus_in:.4f} credits per token so the fit reads in credits. Intervals are 80% "
            "bootstrap intervals over resampled stretches. A family's rate is the median of the "
            "per-fit point estimates and the union of their intervals, and only where every fit's "
            "interval for it overlaps every other's; where they do not it keeps the interval and "
            "no value."),
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
               "measured_tokens_interval": ([credits / a["interval"][1], credits / a["interval"][0]]
                                            if a["interval"] else None)}
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

    print("   masterrig prices nothing absolute: its meter counts web and phone usage the transcripts "
          "never see,\n   so its stretches are bimodal and the spread above is that phantom cluster "
          "(docs/findings-2026-09-20-masterrig-stretches.md).")

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
                    against = "no Shellac rate: Fable is not in the table"
                else:
                    against = (f"Shellac {SHELLAC[fam]:.3f} "
                               f"{'inside' if lo <= SHELLAC[fam] <= hi else 'OUTSIDE'} the interval")
                print(f"      {fam:7} rate={rate:.3f} [{lo:.3f}, {hi:.3f}]  {against}")
            for pair, r in v["ratios"].items():
                if not r["measurable"]:
                    print(f"      {pair:13} NOT MEASURABLE (the fit put one of the two rates at zero)")
                    continue
                wide = f"  interval spans x{r['interval_width']:.1f}" if r["interval_width"] > 3 else ""
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
                   if row["shellac_rate"] else "Shellac rate=absent (Fable is not in the table)")
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
            measured = (f"{row['status']}: rate interval "
                        f"[{row['rate_interval'][0]:.3f}, {row['rate_interval'][1]:.3f}] "
                        f"tokens [{iv[0]:,.0f}, {iv[1]:,.0f}], no value")
        else:
            measured = row["status"] or "measured rate=NOT MEASURABLE"
        print(f"   {f:7} {shellac:52} {measured}")

    print(f"\n6. The rates the publisher adopts ({mr['variant']} fit at output "
          f"{mr['output_multiplier']}x, pooled over {', '.join(mr['fits_pooled'])})")
    for f, row in mr["per_family"].items():
        ref = f"reference={row['reference_input']:.3f}" if row["reference_input"] else "reference=absent"
        if row["input"] is not None:
            iv = (f" [{row['interval'][0]:.3f}, {row['interval'][1]:.3f}]" if row["interval"] else "")
            what = f"input={row['input']:.4f}{iv} ({row['times_opus']:.3f}x Opus)"
        elif row["interval"]:
            what = (f"no value, interval [{row['interval'][0]:.3f}, {row['interval'][1]:.3f}]"
                    f" ({row['times_opus_interval'][0]:.2f}x to "
                    f"{row['times_opus_interval'][1]:.2f}x Opus): {row['status']}")
        else:
            what = row["status"] or "no rate"
        print(f"   {f:7} {row['rate_source']:9} n_fits={row['n_fits']} agree={row['agree']!s:5} "
              f"{ref:18} {what}")
    w = mr["cache_read_weight"]
    if w["interval"]:
        value = f"{w['value']:.4f}" if w["value"] is not None else "no value (the fits disagree)"
        print(f"   cache-read weight {value} [{w['interval'][0]:.4f}, {w['interval'][1]:.4f}] of the "
              f"Opus input rate, n_fits={w['n_fits']} agree={w['agree']}; "
              f"data/prices.json holds {w['reference']:g} with the range {w['reference_range']}")
    for label, v in group_fits(s3, mr["variant"], f"out{mr['output_multiplier']}x").items():
        r = v["ratios"].get("opus:sonnet")
        if r and r.get("measurable"):
            print(f"   opus:sonnet {label:11} {r['ratio']:.3f} "
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
                    help="the fit the adopted rates come from: `joint` fits the cache-read weight "
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
    mr = measured_rates(s1, s3, a.variant)
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
                "findings": "docs/findings-2026-09-20-measured-rates.md",
                "no_traffic": "read-only arithmetic over committed files; no account was driven",
            },
            "measured_rates": mr,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "seed": a.seed, "resamples": a.resamples, "exclusions": a.exclusions,
            "variant": a.variant,
            "dominance": DOMINANCE, "min_n": MIN_N,
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
