"""An exploratory Fable-input-rate envelope from masterrig's own stretches, since 6 September.

This is NOT a measurement this repository adopts: Fable's published row stays "rate not yet
identified" and no figure here changes anything on the page (`tracker/credits.py`'s
`fable_interval` is untouched). It exists to see what masterrig's own passive record gives, at
stated assumptions, without aiming at any prior figure.

Method:

1. An Opus anchor, B5 = Opus input-equivalent tokens per 1% of the five-hour meter, from every
   masterrig stretch where Opus is at least 90% of input + cache_write + 5x output (summed
   across every family in the stretch). Cache reads carry a weight of 0.015 relative to input
   (the range `data/prices.json` keeps beside its published 0), writes count 1x, output 5x --
   all three are assumptions, stated once here and not re-derived.
2. For every stretch where Fable is at least 50% of input + cache_write + output (unweighted,
   raw counts, no output multiplier), solve
   `f = (delta_pct * B5 - other_models_charge - read_charge) / (Fable_input + Fable_write + 5 *
   Fable_output)`, in units of Opus input tokens, where `other_models_charge` is every
   non-Fable family's own input-equivalent tokens taken at Opus's own per-token weight (an
   assumption: it treats one Opus-equivalent token from any family as one Opus input token) and
   `read_charge` is 0.015 times the stretch's total cache reads across every model. `f` is
   solved once at B5's median and once at each of its two rounding-implied edges.
3. Report n, the median and the 10th-90th percentile of the median-B5 solve, and the full row
   table.

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
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import credits as C

ACCOUNT = "masterrig"
CUT = datetime(2026, 9, 6, tzinfo=timezone.utc)
CUT_REASON = ("stretches starting before this are excluded: a pipeline ran against this "
             "account from another host between 2 and 5 September, and no masterrig entry in "
             "history/harness-runs.jsonl records the window, so this is a dated exclusion "
             "stated here rather than a file-backed one.")
OPUS_DOMINANCE = 0.90
FABLE_SHARE = 0.50
READ_WEIGHT = 0.015     # relative to Opus input, an assumption
OUTPUT_MULT = 5         # an assumption
WRITE_MULT = 1          # an assumption
FAMILIES = ("opus", "sonnet", "haiku", "fable")


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


def family_sums(tokens: dict, credits: dict) -> dict[str, dict[str, float]]:
    """{family: {input, output, cache_read, cache_write}}, summed over that family's models."""
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


def ie5(sums: dict[str, float]) -> float:
    """input + cache_write*WRITE_MULT + output*OUTPUT_MULT, one family's or the total's."""
    return sums["input"] + WRITE_MULT * sums["cache_write"] + OUTPUT_MULT * sums["output"]


def total_reads(sums_by_family: dict[str, dict[str, float]]) -> float:
    return sum(v["cache_read"] for v in sums_by_family.values())


def opus_anchor_rows(stretches: list[dict], credits: dict) -> list[dict]:
    """Rows behind the Opus anchor: every stretch where Opus is >= OPUS_DOMINANCE of total ie5."""
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


def show(anchor: dict | None, env: dict) -> None:
    print(f"masterrig, stretches since {CUT.isoformat()} ({CUT_REASON})\n")
    if not anchor:
        print("No stretch is Opus-dominant enough to anchor B5; nothing to report.")
        return
    lo, hi = anchor["range"]
    print(f"Opus anchor B5: n={anchor['n']}, median={anchor['median']:,.0f} Opus-input-equivalent "
          f"tokens per 1%, range over +-windows rounding [{lo:,.0f}, {hi:,.0f}]")
    print(f"  (assumptions: output {OUTPUT_MULT}x, cache reads {READ_WEIGHT} relative to Opus "
          f"input, cache writes {WRITE_MULT}x)\n")
    if env["n"] == 0:
        print("No stretch is Fable-heavy enough (>= 50% of raw input+cache_write+output) to solve.")
        return
    print(f"Fable envelope: n={env['n']}, median f={env['median']:.4f} Opus input tokens, "
          f"10th-90th percentile [{env['p10_p90'][0]:.4f}, {env['p10_p90'][1]:.4f}]\n")
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
    stretches = since_cut(load_stretches(a.masterrig))
    anchor_rows = opus_anchor_rows(stretches, credits)
    anchor = opus_anchor(anchor_rows)
    rows = fable_rows(stretches, credits)
    env = envelope(anchor, rows) if anchor else {"n": 0, "median": None, "p10_p90": None, "rows": []}
    show(anchor, env)
    if a.json:
        a.json.write_text(json.dumps({
            "_meta": {
                "what": "exploratory Fable-input-rate envelope from masterrig's own stretches; "
                        "not adopted anywhere, Fable's published row stays 'rate not yet "
                        "identified'.",
                "no_traffic": "read-only arithmetic over committed files; no account was driven",
                "cut_at": CUT.isoformat(), "cut_reason": CUT_REASON,
                "assumptions": {"output_multiplier": OUTPUT_MULT, "read_weight": READ_WEIGHT,
                                "write_multiplier": WRITE_MULT, "opus_dominance": OPUS_DOMINANCE,
                                "fable_share": FABLE_SHARE},
            },
            "opus_anchor": anchor, "opus_anchor_rows": anchor_rows, "fable_envelope": env,
        }, indent=1, default=float) + "\n")
        print(f"\nJSON -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
