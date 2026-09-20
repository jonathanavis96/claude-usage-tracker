"""An exploratory Fable-input-rate envelope from masterrig's own stretches, since 6 September.

This is NOT a measurement this repository adopts: Fable's published row stays "rate not yet
identified" and no figure here changes anything on the page (`tracker/credits.py`'s
`fable_interval` is untouched). It exists to see what masterrig's own passive record gives, at
stated assumptions, without aiming at any prior figure.

Two routes for the Opus anchor B5 (Opus input-equivalent tokens per 1% of the five-hour meter)
are tried, both over stretches starting on or after 2026-09-06T00:00 UTC:

1. Joint fit (the route this file leads with). Nonnegative least squares of `delta_pct` on five
   columns -- claude-opus-4-7 (E_opus47), the rest of the Opus family (E_opus, i.e.
   claude-opus-4-8 and claude-opus-5), Sonnet and Fable input-equivalent tokens (input +
   cache_write + 5x output, an assumption on the output multiplier) and total cache reads (an
   unweighted column here, not folded in at a fixed relative weight). claude-opus-4-7 gets its
   own column because `data/prices.json` does not price it, so folding it into the priced Opus
   column would silently assume it costs the same as the models the price file does cover.

   This file's first pass gated the fit on `capture_status == "accepted"`, which excludes every
   row: masterrig's post-cut stretches are either `"unpriced"` (they contain claude-opus-4-7
   tokens, which the price file has no rate for -- circular to use as a *reason* to leave a row
   out of the fit that is trying to price it) or `"surplus"` (a verdict from the old, pre-fix
   price model, not this one). Codex's report calls that gate circular as a completeness
   certificate for this purpose, and it is: it does not test whether a row fits, it tests
   whether a row already fits something else. This pass drops the status gate and fits every
   stretch since the cut with `delta_pct >= DELTA_MIN` (small-delta rows are dropped only
   because their meter movement is dominated by +-1-percentage-point rounding), reported once
   with every such row and once with the 12 `"surplus"`-status rows also dropped, to show
   whether they move the fit.

   B5 is `1 / coefficient on the priced-Opus column`. An 80% row-bootstrap interval (10th-90th
   percentile, `SEED` fixed) is reported on the Fable/priced-Opus coefficient ratio, and a
   rounding-only feasibility check (`tools/rounding_feasibility.py`'s linear programme, reads
   free) on the same rows says whether any nonnegative stationary rate is even consistent with
   whole-percent rounding.
2. The single-family anchor kept from the first pass of this file: every stretch where Opus
   (claude-opus-4-7 included) is at least 90% of input + cache_write + 5x output on its own.
   Kept below as a paragraph, not a route this file solves further -- masterrig never runs Opus
   alone, so this threshold was always going to be sparse, and the joint fit is the route that
   uses every row.

Either way, cache reads carry a weight of 0.015 relative to Opus input in step 2's per-row solve
below (the range `data/prices.json` keeps beside its published 0), writes count 1x, output 5x --
stated assumptions, not re-derived.

Step 2, the Fable solve, is unchanged from the first pass: for every stretch where Fable is at
least 50% of input + cache_write + output (unweighted, raw counts, no output multiplier), solve
`f = (delta_pct * B5 - other_models_charge - read_charge) / (Fable_input + Fable_write + 5 *
Fable_output)`, in units of Opus input tokens, where `other_models_charge` is every non-Fable
family's own input-equivalent tokens (claude-opus-4-7 included) taken at Opus's own per-token
weight (an assumption: it treats one Opus-equivalent token from any family, priced or not, as
one Opus input token) and `read_charge` is 0.015 times the stretch's total cache reads across
every model. `f` is solved once at B5's point estimate (`1 / coef_opus` from the joint fit, full
group) and once at each of two rounding-implied edges, built by refitting the joint fit against
`delta_pct -+ windows` instead of `delta_pct`.

Stretches starting before 2026-09-06T00:00 UTC are excluded: `history/harness-runs.jsonl`
carries no masterrig entry for that window, so this is a dated exclusion stated here rather than
a repository record, on the grounds that a pipeline ran against this account from another host
between 2 and 5 September.

    python3 -m tools.masterrig_fable history/masterrig-passive.json
    python3 -m tools.masterrig_fable history/masterrig-passive.json --json out.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.rounding_feasibility import rounding_lp
from tracker import credits as C

ACCOUNT = "masterrig"
CUT = datetime(2026, 9, 6, tzinfo=timezone.utc)
CUT_REASON = ("stretches starting before this are excluded: a pipeline ran against this "
             "account from another host between 2 and 5 September, and no masterrig entry in "
             "history/harness-runs.jsonl records the window, so this is a dated exclusion "
             "stated here rather than a file-backed one.")
OPUS_DOMINANCE = 0.90
FABLE_SHARE = 0.50
READ_WEIGHT = 0.015     # relative to Opus input, an assumption (used in the per-row Fable solve)
OUTPUT_MULT = 5         # an assumption
WRITE_MULT = 1          # an assumption
FAMILIES = ("opus", "sonnet", "haiku", "fable")
OPUS47_MODEL = "claude-opus-4-7"
#: The joint fit's five columns: claude-opus-4-7 broken out on its own (unpriced by
#: data/prices.json), the rest of the Opus family, Sonnet, Fable, then reads (added separately,
#: not part of this tuple -- see joint_design).
JOINT_FAMILIES = ("opus47", "opus", "sonnet", "fable")
DELTA_MIN = 10.0        # the gate: drop rows whose own meter movement is mostly rounding noise
SURPLUS_STATUS = "surplus"
IE_UNIT = 1e6           # column scale, matching tools/rounding_feasibility.py's own
SEED = 20260920
RESAMPLES = 600
INTERVAL = (10, 90)


def load_stretches(path: Path) -> list[dict]:
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    return report["accounts"][ACCOUNT]["stretches"]


def since_cut(stretches: list[dict]) -> list[dict]:
    out = []
    for s in stretches:
        if not s.get("tokens") or not s.get("start"):
            continue
        if datetime.fromisoformat(s["start"]).astimezone(timezone.utc) < CUT:
            continue
        out.append(s)
    return out


def capture_status_counts(stretches_since_cut: list[dict]) -> dict[str, int]:
    """Every capture_status seen among the post-cut stretches, not just one value.

    Kept because it is the reason the joint fit no longer gates on this field: every post-cut
    masterrig stretch is either "unpriced" (it carries claude-opus-4-7 tokens, which
    data/prices.json has no rate for) or "surplus" (a verdict from the old, pre-fix price
    model). Neither status is a completeness certificate for a fit that is itself trying to
    price claude-opus-4-7, so gating on it would be circular.
    """
    return dict(Counter(s.get("capture_status") for s in stretches_since_cut))


def gated_rows(stretches_since_cut: list[dict]) -> list[dict]:
    """The joint fit's row gate: delta_pct >= DELTA_MIN, no capture_status filter."""
    return [s for s in stretches_since_cut if (s.get("delta_pct") or 0) >= DELTA_MIN]


def drop_surplus(rows: list[dict]) -> list[dict]:
    return [s for s in rows if s.get("capture_status") != SURPLUS_STATUS]


def family_sums(tokens: dict, credits: dict) -> dict[str, dict[str, float]]:
    """{family: {input, output, cache_read, cache_write}}, summed over that family's models.

    Opus is one bucket here (claude-opus-4-7 included), matching `tracker/credits.family`. Used
    for total cache reads and for route B / the Fable solve, which are unaffected by whether
    claude-opus-4-7 is priced. The joint fit's own design (`joint_family_sums`) splits it out.
    """
    out = {f: {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0} for f in FAMILIES}
    for model, tok in tokens.items():
        if not isinstance(tok, dict):
            continue
        fam = C.family(model, credits)
        if fam not in out:
            continue
        for cls in ("input", "output", "cache_read", "cache_write"):
            out[fam][cls] += tok.get(cls, 0)
    return out


def joint_family_sums(tokens: dict, credits: dict) -> dict[str, dict[str, float]]:
    """{opus47, opus, sonnet, fable}: input-equivalent-track sums, claude-opus-4-7 split out of
    the rest of the Opus family so its unpriced tokens get their own nonnegative coefficient
    instead of assuming they cost what the priced Opus models cost."""
    out = {f: {"input": 0.0, "output": 0.0, "cache_write": 0.0} for f in JOINT_FAMILIES}
    for model, tok in tokens.items():
        if not isinstance(tok, dict):
            continue
        fam = C.family(model, credits)
        if fam == "opus" and model.lower() == OPUS47_MODEL:
            bucket = "opus47"
        elif fam in ("opus", "sonnet", "fable"):
            bucket = fam
        else:
            continue
        for cls in ("input", "output", "cache_write"):
            out[bucket][cls] += tok.get(cls, 0)
    return out


def ie5(sums: dict[str, float]) -> float:
    """input + cache_write*WRITE_MULT + output*OUTPUT_MULT, one family's or the total's."""
    return sums["input"] + WRITE_MULT * sums["cache_write"] + OUTPUT_MULT * sums["output"]


def total_reads(sums_by_family: dict[str, dict[str, float]]) -> float:
    return sum(v["cache_read"] for v in sums_by_family.values())


# --- Step 1, route A: the joint fit, gated on delta_pct >= DELTA_MIN only ----------------------

def joint_design(rows: list[dict], credits: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Scaled [E_opus47, E_opus, E_sonnet, E_fable, R] columns, delta_pct target, windows pieces.

    R (total cache reads) is summed over every family, including claude-opus-4-7 and Haiku, via
    the un-split `family_sums` -- only the input-equivalent columns split claude-opus-4-7 out.
    """
    jsums = [joint_family_sums(s["tokens"], credits) for s in rows]
    gsums = [family_sums(s["tokens"], credits) for s in rows]
    x = np.array([[ie5(js[f]) / IE_UNIT for f in JOINT_FAMILIES] + [total_reads(gs) / IE_UNIT]
                 for js, gs in zip(jsums, gsums)])
    y = np.array([s["delta_pct"] for s in rows])
    p = np.array([s.get("windows", 1) for s in rows])
    return x, y, p


def _nnls_ratio(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float | None]:
    from scipy.optimize import nnls
    coef, _residual = nnls(x, y)
    opus, fable = coef[1], coef[3]   # coef order is JOINT_FAMILIES: opus47, opus, sonnet, fable
    ratio = fable / opus if opus > 0 else None
    return coef, ratio


def joint_fit(rows: list[dict], credits: dict, seed: int = SEED, resamples: int = RESAMPLES) -> dict | None:
    """Nonnegative least squares of delta_pct on claude-opus-4-7 / priced-Opus / Sonnet / Fable
    input-equivalent tokens and reads, coefficients per raw token (column scale divided back
    out), plus a row bootstrap on the Fable/priced-Opus coefficient ratio."""
    if not rows:
        return None
    x, y, _pieces = joint_design(rows, credits)
    coef, ratio = _nnls_ratio(x, y)
    coef_raw = (coef / IE_UNIT).tolist()   # per raw token, not per IE_UNIT tokens
    rng = random.Random(f"{seed}/masterrig-joint-fit/{len(rows)}")
    n = len(rows)
    draws = []
    for _ in range(resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        _, r = _nnls_ratio(x[idx], y[idx])
        if r is not None:
            draws.append(r)
    interval = [percentile(draws, INTERVAL[0]), percentile(draws, INTERVAL[1])] if draws else None
    return {
        "n": n,
        "coef": dict(zip(JOINT_FAMILIES, coef_raw[:4])),
        "coef_reads": coef_raw[4],
        "fable_opus_ratio": ratio,
        "fable_opus_ratio_interval_10_90": interval,
        "fable_opus_ratio_draws_n": len(draws),
    }


def joint_fit_b5(fit: dict, rows: list[dict], credits: dict) -> dict | None:
    """B5 = 1 / coef_opus (the priced-Opus column) at the point estimate, and at each
    rounding-implied edge (refitting against delta_pct -+ windows instead of delta_pct)."""
    if fit is None or fit["coef"]["opus"] <= 0:
        return None
    x, y, pieces = joint_design(rows, credits)
    coef_lo, _ = _nnls_ratio(x, y - pieces)   # smaller target -> smaller coefficient -> larger B5
    coef_hi, _ = _nnls_ratio(x, y + pieces)
    opus_lo, opus_hi = coef_lo[1], coef_hi[1]
    b5_med = 1 / fit["coef"]["opus"]
    b5_from_hi = IE_UNIT / opus_hi if opus_hi > 0 else float("inf")
    b5_from_lo = IE_UNIT / opus_lo if opus_lo > 0 else float("inf")
    return {"n": fit["n"], "median": b5_med, "range": sorted([b5_from_hi, b5_from_lo])}


def feasibility_reads_free(rows: list[dict], credits: dict) -> dict | None:
    """tools/rounding_feasibility.py's linear programme (reads free) on the same rows: the
    smallest uniform extra percentage-point slack beyond whole-percent rounding at any
    nonnegative claude-opus-4-7/priced-Opus/Sonnet/Fable rate. t=0 means the group is not
    excluded by rounding alone."""
    if not rows:
        return None
    x, y, pieces = joint_design(rows, credits)
    result = rounding_lp(x[:, :4], y, pieces)   # drop the reads column: reads free
    return {"extra_pp": result["extra_pp"], "coef": dict(zip(JOINT_FAMILIES, result["coef"]))}


# --- Step 1, route B: the single-family >=90%-Opus anchor, kept as a paragraph, not extended ---

def opus_anchor_rows(stretches: list[dict], credits: dict) -> list[dict]:
    """Rows behind the single-family anchor: every stretch where Opus (claude-opus-4-7 included,
    via the un-split `family_sums`) is >= OPUS_DOMINANCE of total ie5. Kept for the null result;
    not the route step (ii) below solves against."""
    rows = []
    for s in stretches:
        sums = family_sums(s["tokens"], credits)
        total = sum(ie5(v) for v in sums.values())
        if total <= 0 or ie5(sums["opus"]) / total < OPUS_DOMINANCE:
            continue
        opus_ie = ie5(sums["opus"]) + READ_WEIGHT * sums["opus"]["cache_read"]
        delta, windows = s["delta_pct"], s["windows"]
        rows.append({
            "start": s["start"], "end": s["end"], "delta_pct": delta, "windows": windows,
            "capture_status": s.get("capture_status"),
            "opus_ie": opus_ie,
            "b5_point": opus_ie / delta,
            "b5_lo": opus_ie / (delta + windows),
            "b5_hi": opus_ie / (delta - windows) if delta > windows else float("inf"),
        })
    return rows


def opus_anchor(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    points = [r["b5_point"] for r in rows]
    return {
        "n": len(rows),
        "median": st.median(points),
        "range": [min(r["b5_lo"] for r in rows), max(r["b5_hi"] for r in rows)],
        "points": sorted(points),
    }


# --- Step 2: the Fable solve, against whichever B5 (route A) was found -------------------------

def fable_rows(stretches: list[dict], credits: dict) -> list[dict]:
    """Rows behind the Fable solve: every stretch where Fable is >= FABLE_SHARE of raw tokens."""
    rows = []
    for s in stretches:
        sums = family_sums(s["tokens"], credits)
        fable = sums["fable"]
        raw_total = sum(v["input"] + v["cache_write"] + v["output"] for v in sums.values())
        fable_raw = fable["input"] + fable["cache_write"] + fable["output"]
        if raw_total <= 0 or fable_raw / raw_total < FABLE_SHARE:
            continue
        other_charge = sum(ie5(v) for f, v in sums.items() if f != "fable")
        read_charge = READ_WEIGHT * total_reads(sums)
        fable_ie = ie5(fable)
        rows.append({
            "start": s["start"], "end": s["end"], "delta_pct": s["delta_pct"], "windows": s["windows"],
            "capture_status": s.get("capture_status"),
            "fable_input_plus_write": fable["input"] + WRITE_MULT * fable["cache_write"],
            "fable_output": fable["output"], "fable_reads": fable["cache_read"],
            "other_model_charge": other_charge + read_charge,
            "fable_ie": fable_ie,
        })
    return rows


def solve_f(row: dict, b5: float) -> float | None:
    if row["fable_ie"] <= 0:
        return None
    numerator = row["delta_pct"] * b5 - row["other_model_charge"]
    return numerator / row["fable_ie"]


def percentile(values: list[float], pct: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def envelope(anchor: dict, rows: list[dict]) -> dict:
    b5_med, b5_lo, b5_hi = anchor["median"], anchor["range"][0], anchor["range"][1]
    solved = []
    for row in rows:
        f_med, f_lo, f_hi = solve_f(row, b5_med), solve_f(row, b5_lo), solve_f(row, b5_hi)
        solved.append({**row, "f_at_b5_median": f_med, "f_at_b5_range_lo": f_lo,
                      "f_at_b5_range_hi": f_hi})
    at_median = [r["f_at_b5_median"] for r in solved if r["f_at_b5_median"] is not None]
    return {
        "n": len(solved),
        "median": st.median(at_median) if at_median else None,
        "p10_p90": [percentile(at_median, 10), percentile(at_median, 90)] if at_median else None,
        "rows": solved,
    }


def _show_joint(label: str, joint: dict | None, joint_b5: dict | None, feas: dict | None) -> None:
    print(f"{label}:")
    if joint is None:
        print("  n=0: no row in this group. No joint fit possible.\n")
        return
    c = joint["coef"]
    print(f"  n={joint['n']}  coef: opus47={c['opus47']:.6g}  opus={c['opus']:.6g}  "
          f"sonnet={c['sonnet']:.6g}  fable={c['fable']:.6g}  "
          f"reads={joint['coef_reads']:.6g} (percentage points per raw token)")
    ratio = joint["fable_opus_ratio"]
    print(f"  Fable/priced-Opus coefficient ratio: {ratio!r}"
          + (f", 80% row-bootstrap interval [{joint['fable_opus_ratio_interval_10_90'][0]:.4g}, "
             f"{joint['fable_opus_ratio_interval_10_90'][1]:.4g}] "
             f"(n={joint['fable_opus_ratio_draws_n']}/{RESAMPLES} draws, seed {SEED})"
             if joint["fable_opus_ratio_interval_10_90"] else " (no draws with a positive Opus "
             "coefficient)"))
    if joint_b5:
        print(f"  B5 from this fit: median={joint_b5['median']:,.0f}, "
              f"range over +-windows rounding [{joint_b5['range'][0]:,.0f}, "
              f"{joint_b5['range'][1]:,.0f}]")
    else:
        print("  Priced-Opus coefficient is 0 or negative: no B5 from this fit.")
    if feas is None:
        print("  Feasibility slack: n=0, nothing to test.\n")
    else:
        print(f"  Feasibility slack (reads free, same rows): t={feas['extra_pp']:.4f} pp "
              f"(t=0 -> not excluded by rounding alone; t>0 -> no stationary rate fits every "
              f"row at any nonnegative coefficients)\n")


def show(status_counts: dict, joint_full: dict | None, joint_full_b5: dict | None,
         feas_full: dict | None, joint_nosurplus: dict | None, joint_nosurplus_b5: dict | None,
         feas_nosurplus: dict | None, single_anchor: dict | None, env: dict) -> None:
    print(f"masterrig, stretches since {CUT.isoformat()} ({CUT_REASON})")
    print(f"capture_status counts since the cut: {status_counts}\n")
    print("Route A -- joint fit (nonnegative least squares, claude-opus-4-7 / priced-Opus / "
          f"Sonnet / Fable input-equivalent tokens + cache reads, gated on delta_pct >= "
          f"{DELTA_MIN:g}, no capture_status filter -- see the module docstring for why):\n")
    _show_joint("All gated rows", joint_full, joint_full_b5, feas_full)
    _show_joint("Gated rows, 'surplus'-status dropped", joint_nosurplus, joint_nosurplus_b5,
                feas_nosurplus)

    print("Route B -- single-family anchor (>=90% Opus by input-equivalent tokens, kept as a "
          "paragraph, not extended further):")
    if not single_anchor:
        print("  No stretch reaches 90% Opus share; documented null, as in the first pass.\n")
    else:
        lo, hi = single_anchor["range"]
        print(f"  n={single_anchor['n']}, median={single_anchor['median']:,.0f} Opus-input-"
              f"equivalent tokens per 1%, range [{lo:,.0f}, {hi:,.0f}]\n")

    if joint_full_b5 is None:
        print("No B5 from the full-group joint fit: the Fable solve below cannot run.")
        return
    if env["n"] == 0:
        print("No stretch is Fable-heavy enough (>= 50% of raw input+cache_write+output) to solve.")
        return
    print(f"Fable envelope (B5 from the full-group joint fit): n={env['n']}, "
          f"median f={env['median']:.4f} Opus input tokens, 10th-90th percentile "
          f"[{env['p10_p90'][0]:.4f}, {env['p10_p90'][1]:.4f}]\n")
    print(f"{'start':26} {'end':26} {'delta':>6} {'win':>3} {'capture':11} "
          f"{'F_in+wr':>12} {'F_out':>10} {'F_reads':>12} {'other_chg':>14} {'f@median':>10}")
    for r in env["rows"]:
        f_med = r["f_at_b5_median"]
        f_str = f"{f_med:.4f}" if f_med is not None else "n/a"
        print(f"{r['start']:26} {r['end']:26} {r['delta_pct']:6.1f} {r['windows']:3d} "
              f"{str(r['capture_status']):11} {r['fable_input_plus_write']:12,.0f} "
              f"{r['fable_output']:10,.0f} {r['fable_reads']:12,.0f} {r['other_model_charge']:14,.0f} "
              f"{f_str:>10}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("masterrig", type=Path, help="a masterrig stretch file")
    ap.add_argument("--json", type=Path, help="write the same figures as JSON")
    a = ap.parse_args(argv)
    credits = C.load_credits()
    all_since_cut = since_cut(load_stretches(a.masterrig))
    status_counts = capture_status_counts(all_since_cut)

    full_rows = gated_rows(all_since_cut)
    nosurplus_rows = drop_surplus(full_rows)

    joint_full = joint_fit(full_rows, credits)
    joint_full_b5 = joint_fit_b5(joint_full, full_rows, credits) if joint_full else None
    feas_full = feasibility_reads_free(full_rows, credits)

    joint_nosurplus = joint_fit(nosurplus_rows, credits)
    joint_nosurplus_b5 = joint_fit_b5(joint_nosurplus, nosurplus_rows, credits) if joint_nosurplus else None
    feas_nosurplus = feasibility_reads_free(nosurplus_rows, credits)

    single_anchor_rows = opus_anchor_rows(all_since_cut, credits)
    single_anchor = opus_anchor(single_anchor_rows)

    fable = fable_rows(all_since_cut, credits)
    env = envelope(joint_full_b5, fable) if joint_full_b5 else {
        "n": 0, "median": None, "p10_p90": None, "rows": []}

    show(status_counts, joint_full, joint_full_b5, feas_full, joint_nosurplus, joint_nosurplus_b5,
         feas_nosurplus, single_anchor, env)
    if a.json:
        a.json.write_text(json.dumps({
            "_meta": {
                "what": "exploratory Fable-input-rate envelope from masterrig's own stretches; "
                        "not adopted anywhere, Fable's published row stays 'rate not yet "
                        "identified'.",
                "no_traffic": "read-only arithmetic over committed files; no account was driven",
                "cut_at": CUT.isoformat(), "cut_reason": CUT_REASON,
                "capture_status_counts_since_cut": status_counts,
                "gate": f"delta_pct >= {DELTA_MIN:g}, no capture_status filter (see module "
                        "docstring: the accepted/unpriced/surplus statuses are circular here)",
                "assumptions": {"output_multiplier": OUTPUT_MULT, "read_weight": READ_WEIGHT,
                                "write_multiplier": WRITE_MULT, "opus_dominance": OPUS_DOMINANCE,
                                "fable_share": FABLE_SHARE, "seed": SEED, "resamples": RESAMPLES,
                                "interval": list(INTERVAL)},
            },
            "joint_fit_all_gated": joint_full, "joint_fit_all_gated_b5": joint_full_b5,
            "feasibility_reads_free_all_gated": feas_full,
            "joint_fit_no_surplus": joint_nosurplus, "joint_fit_no_surplus_b5": joint_nosurplus_b5,
            "feasibility_reads_free_no_surplus": feas_nosurplus,
            "single_family_anchor": single_anchor, "single_family_anchor_rows": single_anchor_rows,
            "fable_envelope": env,
        }, indent=1, default=float) + "\n")
        print(f"\nJSON -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
