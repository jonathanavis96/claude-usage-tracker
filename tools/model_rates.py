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
model ratios from those, (3) the same rates fitted across every clean stretch, (4) whether
model mix, reset_verified or stretch length explains the jwork/Dave gap, (5) the window in
tokens per model. Ratios from fewer than three stretches on a side are reported as not
measurable rather than as a number.

Token counts are grouped into model families (every claude-opus-* into Opus, and so on),
because Shellac's rows are unversioned; a stretch carrying tokens from a family this tool does
not know is dropped from that account's fits. Input-equivalent tokens are
input + cache_write + OUT x output, with OUT = 5 (Shellac's output ratio for every model in
its table) and OUT = 3 (the ratio fitted for Fable in the credits-model note) reported beside
it. Cache reads count nothing, as in tools.reconcile_window.split.
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

from tools.harness_runs import OUT as HARNESS_RUNS, load_runs
from tools.reconcile_window import CUT, P, RATES, clean, harness_runs, load, split

FAMILIES = ("opus", "sonnet", "haiku", "fable")
OUT_MULTS = (5, 3)
DOMINANCE = 0.95        # share of raw tokens one family must carry for a stretch to price it alone
MIN_N = 3               # fewer stretches than this on either side of a ratio is not measurable
MIN_FIT_N = 8           # fewer clean stretches than this does not support a four-rate fit
MIN_NONZERO = 3         # a family needs this many stretches with tokens before it is fitted
SEED = 20260920
RESAMPLES = 600
INTERVAL = (10, 90)     # bootstrap percentiles reported as the interval

# Priors. Shellac's per-model input rates are RATES in tools.reconcile_window, keyed by model
# id; Fable is not in its table, and the interval below is the one
# docs/findings-2026-09-20-reconciliation.md publishes (1.8 to 4.0 times Opus, from the
# Fable-heavy stretches), with the credits-model note's fitted 25/15 as its point value.
SHELLAC = {f: next(r[0] for m, r in RATES.items() if f in m) for f in FAMILIES if any(f in m for m in RATES)}
FABLE_POINT = 25 / 15
FABLE_INTERVAL = (1.8, 4.0)


def family(model: str) -> str:
    for f in FAMILIES:
        if f in model:
            return f
    return "other"


def api_ratio(prices: dict, num: str, den: str) -> float | None:
    """The API list-price ratio between two families, from data/prices.json, or None."""
    def rate(f):
        v = [b["input"] for m, b in prices.items() if not m.startswith("_") and family(m) == f]
        return v[0] if v else None
    a, b = rate(num), rate(den)
    return a / b if a and b else None


def prepare(account: str, kept: list[tuple[dict, float, dict]]) -> list[dict]:
    """One record per clean stretch: its era, its per-family token counts and its meter movement."""
    out = []
    for s, d, t in kept:
        ie = {m: {f: 0.0 for f in FAMILIES + ("other",)} for m in OUT_MULTS}
        raw = {f: 0.0 for f in FAMILIES + ("other",)}
        for model, tok in t.items():
            if not isinstance(tok, dict):
                continue
            f = family(model)
            i, o = tok.get("input", 0) + tok.get("cache_write", 0), tok.get("output", 0)
            raw[f] += i + o
            for m in OUT_MULTS:
                ie[m][f] += i + m * o
        total = sum(raw.values())
        out.append({"account": account, "start": s["start"], "end": s["end"], "delta": d,
                    "era": "pre" if P(s["start"]) < CUT else "post",
                    "reset_verified": bool(s.get("reset_verified")),
                    "ie": ie, "raw": raw, "total_raw": total, "ok": raw["other"] == 0,
                    "share": {f: (raw[f] / total if total else 0.0) for f in FAMILIES},
                    "dominant": max(FAMILIES, key=lambda f: raw[f]) if total else None})
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
    """Non-negative least squares by coordinate descent. Deterministic, no scipy."""
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


def design(recs: list[dict], out_mult: int, families: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
    X = np.array([[r["ie"][out_mult][f] / 1e6 for f in families] for r in recs if r["ok"]])
    y = np.array([r["delta"] for r in recs if r["ok"]])
    return X, y


def fit(recs: list[dict], out_mult: int) -> dict | None:
    """Fit delta_pct = sum over families of (input-equivalent tokens x rate), Opus fixed at 10/15.

    The fit is unconstrained apart from non-negativity; fixing Opus is a rescaling afterwards,
    which is what sets the credits-per-1% the other rates are read against.
    """
    usable = [r for r in recs if r["ok"]]
    if len(usable) < MIN_FIT_N:
        return None
    families = tuple(f for f in FAMILIES
                     if sum(1 for r in usable if r["raw"][f] > 0) >= MIN_NONZERO)
    if "opus" not in families:
        return None
    X, y = design(usable, out_mult, families)
    b = nnls(X, y)
    if b[0] <= 0:
        return None
    scale = SHELLAC["opus"] / b[0]
    pred = X @ b
    return {"n": len(y), "families": families,
            "rates": {f: v * scale for f, v in zip(families, b)},
            "window_credits_per_pct": SHELLAC["opus"] / (b[0] / 1e6),
            "residual_median_abs_rel": float(np.median(np.abs(pred - y) / y)),
            "residual_p90_abs_rel": float(np.percentile(np.abs(pred - y) / y, 90))}


def fit_bootstrap(recs: list[dict], out_mult: int, rng: random.Random, resamples: int) -> dict | None:
    base = fit(recs, out_mult)
    if not base:
        return None
    usable = [r for r in recs if r["ok"]]
    families = base["families"]
    X, y = design(usable, out_mult, families)
    draws: dict[str, list[float]] = {f: [] for f in families}
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
        for num, den, _ in pairs:
            if b[at[den]] > 0:
                ratio_draws[f"{num}:{den}"].append(b[at[num]] / b[at[den]])
        windows.append(SHELLAC["opus"] / (b[0] / 1e6))
    base["interval"] = {f: interval(v) for f, v in draws.items() if v}
    base["window_interval"] = interval(windows) if windows else None
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


def window_check(S: dict, lists: dict[str, list]) -> dict:
    """Reproduce the reconciliation's pure-Opus jwork window under each exclusion list."""
    out = {}
    for label, runs in lists.items():
        v = []
        for s, d, t in clean(S["jwork"], "jwork", runs):
            ok, known, fi, fo, raw = split(t)
            if P(s["start"]) < CUT and ok and fi + fo == 0 and all(m.startswith("claude-opus") for m in t):
                v.append(known / d)
        out[label] = quantiles(v) if v else None
    return out


def exclusion_report(S: dict, lists: dict[str, list]) -> dict:
    """What the harness-runs list removes beyond the probes-only rule, per account."""
    out = {}
    for a in S:
        kept = {label: clean(S[a], a, runs) for label, runs in lists.items()}
        keys = {label: {s["start"] for s, _, _ in k} for label, k in kept.items()}
        dropped = sorted(keys["probes-only"] - keys["harness-runs"])
        out[a] = {"probes_only": len(kept["probes-only"]), "harness_runs": len(kept["harness-runs"]),
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


def section3(data: dict[str, list[dict]], seed: int, resamples: int) -> dict:
    """The same rates fitted across every clean stretch, not only the single-model ones."""
    out = {}
    for a, recs in data.items():
        for era in ("pre", "post"):
            sub = [r for r in recs if r["era"] == era]
            for m in OUT_MULTS:
                key = f"{a}/{era}/out{m}x"
                f = fit_bootstrap(sub, m, random.Random(f"{seed}/fit/{key}"), resamples)
                if f:
                    out[key] = {
                        "n": f["n"], "rates": f["rates"], "interval": f["interval"],
                        "window_credits_per_pct": f["window_credits_per_pct"],
                        "window_interval": f["window_interval"],
                        "residual_median_abs_rel": f["residual_median_abs_rel"],
                        "residual_p90_abs_rel": f["residual_p90_abs_rel"],
                        "ratios": f["ratios"]}
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
            # A whole-percent meter puts +-0.5 on every delta_pct, so this is the floor on any
            # per-stretch figure, and roughly that over the root of n on a median of them.
            out["quantisation"][key] = {"n": len(sub),
                                        "per_stretch": st.median([0.5 / r["delta"] for r in sub]),
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


def adopt(s3: dict) -> dict:
    """Pool the gs fits into one rate per family: the median point estimate and the union of intervals."""
    fits = {k: v for k, v in s3.items() if k.split("/")[0] in ("jwork", "dave") and k.endswith("out5x")}
    out = {}
    for f in FAMILIES:
        pts = [v["rates"][f] for v in fits.values() if v["rates"].get(f, 0) > 0]
        ivs = [v["interval"][f] for v in fits.values() if v["interval"].get(f) and v["rates"].get(f, 0) > 0]
        if len(pts) >= MIN_N - 1 and ivs:
            out[f] = {"measured": st.median(pts), "interval": [min(i[0] for i in ivs), max(i[1] for i in ivs)],
                      "n_fits": len(pts), "points": sorted(pts), "shellac": SHELLAC.get(f)}
        else:
            out[f] = {"measured": None, "n_fits": len(pts), "points": sorted(pts), "shellac": SHELLAC.get(f)}
    return out


def section5(window: dict, s3: dict) -> dict:
    """The five-hour window in input-equivalent tokens per model, Shellac's rate beside the measured one."""
    per_pct = window["harness-runs"]["median"]
    credits = per_pct * 100
    rows = {}
    for f, a in adopt(s3).items():
        row = {"shellac_rate": a["shellac"], "measured_rate": a["measured"], "rate_interval": a.get("interval"),
               "shellac_tokens": credits / a["shellac"] if a["shellac"] else None,
               "measured_tokens": credits / a["measured"] if a["measured"] else None}
        if a.get("interval"):
            row["measured_tokens_interval"] = [credits / a["interval"][1], credits / a["interval"][0]]
        rows[f] = row
    return {"window_credits_per_pct": per_pct, "window_credits": credits, "rows": rows}


def pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def show(excl: dict, win: dict, s1: dict, s2: dict, s3: dict, s4: dict, s5: dict, lists: dict,
         chosen: str = "harness-runs") -> None:
    print(f"0. Exclusion list ({chosen} is the one sections 1 to 5 use)")
    kinds = {}
    for a, h0, h1 in lists["harness-runs"]:
        kinds[a] = kinds.get(a, 0) + 1
    print(f"   {len(lists['probes-only'])} runs from probes.jsonl and the effort matrix, "
          f"{len(lists['harness-runs'])} from {HARNESS_RUNS} ({', '.join(f'{a} {n}' for a, n in sorted(kinds.items()))})")
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
        print(f"   {key:22} n={f['n']:3} window={f['window_credits_per_pct']:,.0f} credits per 1% "
              f"[{f['window_interval'][0]:,.0f}, {f['window_interval'][1]:,.0f}]  "
              f"residual |rel| median={f['residual_median_abs_rel']:.3f} p90={f['residual_p90_abs_rel']:.3f}")
        for fam, v in f["rates"].items():
            lo, hi = f["interval"][fam]
            if fam == "opus":
                against = "Shellac 0.667, fixed to set the scale"
            elif SHELLAC.get(fam) is None:
                against = "no Shellac rate: Fable is not in the table"
            else:
                against = (f"Shellac {SHELLAC[fam]:.3f} "
                           f"{'inside' if lo <= SHELLAC[fam] <= hi else 'OUTSIDE'} the interval")
            print(f"      {fam:7} rate={v:.3f} [{lo:.3f}, {hi:.3f}]  {against}")
        for pair, r in f["ratios"].items():
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
        if row["measured_rate"]:
            iv = row["measured_tokens_interval"]
            measured = (f"measured rate={row['measured_rate']:.3f} "
                        f"[{row['rate_interval'][0]:.3f}, {row['rate_interval'][1]:.3f}] "
                        f"tokens={row['measured_tokens']:,.0f} [{iv[0]:,.0f}, {iv[1]:,.0f}]")
        else:
            measured = "measured rate=NOT MEASURABLE"
        print(f"   {f:7} {shellac:52} {measured}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("masterrig", type=Path, help="a masterrig stretch file, as tools/reconcile_window.py takes")
    ap.add_argument("--json", type=Path, help="write the same figures as JSON")
    ap.add_argument("--exclusions", choices=("harness-runs", "probes-only"), default="harness-runs",
                    help="which list of the tracker's own runs to exclude stretches by")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--resamples", type=int, default=RESAMPLES)
    a = ap.parse_args(argv)
    S = load(a.masterrig)
    lists = {"probes-only": harness_runs()}
    lists["harness-runs"] = load_runs() if HARNESS_RUNS.exists() else lists["probes-only"]
    excl = exclusion_report(S, lists)
    win = window_check(S, lists)
    data = {acct: prepare(acct, clean(S[acct], acct, lists[a.exclusions])) for acct in S}
    # Every group draws from its own seeded resampler, keyed by its name, so a figure quoted
    # from one row does not move when a different row gains or loses a stretch.
    s1 = section1(data)
    s2 = section2(data, a.seed, a.resamples)
    s3 = section3(data, a.seed, a.resamples)
    s4 = section4(data, s3, a.seed, a.resamples)
    s5 = section5(win, s3)
    show(excl, win, s1, s2, s3, s4, s5, lists, a.exclusions)
    if a.json:
        a.json.write_text(json.dumps({
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "seed": a.seed, "resamples": a.resamples, "exclusions": a.exclusions,
            "dominance": DOMINANCE, "min_n": MIN_N,
            "interval_percentiles": INTERVAL, "shellac_rates": SHELLAC, "fable_point": FABLE_POINT,
            "fable_interval": FABLE_INTERVAL, "exclusion": excl, "window_check": win,
            "section1": s1, "section2": s2, "section3": s3, "section4": s4, "section5": s5,
            "adopt": adopt(s3)}, indent=1, default=float) + "\n")
        print(f"\nJSON -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
