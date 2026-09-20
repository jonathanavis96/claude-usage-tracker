"""Is the joint fit's stationary rate model consistent with whole-percent rounding alone?

`tools/model_rates.py` fits one rate per family plus a cache-read weight by least squares,
which always returns *a* answer even when no stationary rate model actually fits the rows. This
tool asks the sharper question directly: do *any* non-negative coefficients on Opus, Sonnet and
Fable input-equivalent tokens (input + cache_write + 5x output) and on cache reads put every
row's predicted meter movement inside its rounding bound, `delta_pct +- windows`
(`tracker/join.py:Stretch.bounds` divides by the same `windows + delta_pct` bound)? Where they
cannot, it reports the smallest uniform extra percentage-point slack `t` that closes the gap, by
linear programming: minimise `t >= 0` subject to `|predicted - delta_pct| <= windows + t` for
every row, coefficients non-negative.

This is a feasibility check, not a fit: a group with `t = 0` is merely *not excluded* by
rounding, and a group with `t > 0` is not explained by a stationary rate at any non-negative
coefficients, for any read weight in the range tried. It says nothing about *why*: omitted
traffic, cross-account misattribution, a real price change and model-mix drift all produce the
same symptom here.

Ported from the read-only Codex review of commit 447b926 (2026-09-20), whose reproduce.py struck
the same three groups. Reads history/gs-passive.json, a masterrig-shaped stretch file, the
harness-runs exclusion list and data/prices.json; drives no account.

    python3 -m tools.rounding_feasibility history/masterrig-passive.json
    python3 -m tools.rounding_feasibility history/masterrig-passive.json --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import linprog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import credits as C
from tools.model_rates import clean, prepare

#: The families the linear programme fits a rate for. Haiku is left out: it carries no clean
#: stretch worth naming (tools/model_rates.py's `max_share_of_a_clean_stretch` is near zero for
#: it on both fitted accounts), and the Codex report's table is Opus, Sonnet, Fable and reads.
LP_FAMILIES = ("opus", "sonnet", "fable")
#: "Reads free" means cache reads carry no charge (weight 0), the reference table's own
#: convention (tools/model_rates.py's module docstring: "cache reads free") and its
#: CACHE_READ_WEIGHT = 0.0. This is a fixed weight of zero, not a free fourth coefficient: with
#: reads genuinely free to fit their own non-negative coefficient, non-negativity alone already
#: pins dave/post and jwork/post to the same answer as weight 0 (their unconstrained read
#: coefficients are at or below zero), but jwork/pre's does not -- a truly free read coefficient
#: gives it 0.1774pp, not the 1.0191pp reported below. The Codex report's table is at weight 0.
READS_FREE_WEIGHT = 0.0
#: The read weight tested relative to the Opus input-equivalent coefficient. 0.0118 is
#: tools/model_rates.py's jwork/pre joint-fit point estimate -- the closest thing this
#: repository has measured to a single number for it.
FIXED_READ_WEIGHT = 0.0118
#: Column scale, matching tools/model_rates.py's CACHE_READ_UNIT: cache reads outnumber
#: input-equivalent tokens by roughly two orders of magnitude, and an unscaled column makes the
#: LP's constraint matrix badly conditioned for no benefit -- the reported ratios are unaffected
#: because they are ratios of coefficients on their own columns.
IE_UNIT = 1e6
GROUPS = ("jwork/pre", "jwork/post", "dave/post")


def family_ie5(rec: dict, fam: str) -> float:
    """A family's input-equivalent tokens (input + cache_write + 5x output) in one record."""
    return rec["ie"][5][fam]


def group_rows(account: str, era: str, kept: dict[str, list[dict]]) -> list[dict]:
    """The same membership tools/model_rates.py's joint fit at out5x uses for this group."""
    recs = prepare(account, kept[account])
    return [r for r in recs if r["era"] == era and r["ok"]]


def design_fixed_weight(rows: list[dict], weight: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Opus, Sonnet and Fable, with reads folded into the Opus column at a fixed relative weight.

    A read costs `weight` times the Opus input-equivalent rate, so its charge is
    `weight * b_opus * reads`; adding `weight * reads / IE_UNIT` to the (already-scaled) Opus
    column reproduces that charge through the same coefficient, with no separate reads column
    and so no separate degree of freedom for it. `weight = READS_FREE_WEIGHT` (0) is the "reads
    free" case: the column addition is then zero and reads drop out of the design entirely.
    """
    x = np.array([[family_ie5(r, f) / IE_UNIT for f in LP_FAMILIES] for r in rows])
    x[:, 0] += weight * np.array([r["total_reads"] for r in rows]) / IE_UNIT
    y = np.array([r["delta"] for r in rows])
    p = np.array([r["windows"] for r in rows])
    return x, y, p


def rounding_lp(x: np.ndarray, y: np.ndarray, pieces: np.ndarray) -> dict:
    """Smallest uniform extra percentage-point slack `t` beyond rounding, coefficients >= 0.

    minimise t  s.t.  x @ b - y <= pieces + t,  y - x @ b <= pieces + t,  b >= 0,  t >= 0.
    """
    n_rows, n_cols = x.shape
    a_ub = np.vstack([np.column_stack([x, -np.ones(n_rows)]),
                      np.column_stack([-x, -np.ones(n_rows)])])
    b_ub = np.r_[y + pieces, -y + pieces]
    c = np.r_[np.zeros(n_cols), 1.0]
    res = linprog(c, A_ub=a_ub, b_ub=b_ub, bounds=[(0, None)] * (n_cols + 1), method="highs")
    if not res.success:
        raise RuntimeError(f"rounding_lp: solver did not converge ({res.message})")
    return {"extra_pp": float(res.x[-1]), "coef": res.x[:-1].tolist()}


def run(kept: dict[str, list[dict]]) -> dict:
    out = {}
    for group in GROUPS:
        account, era = group.split("/")
        rows = group_rows(account, era, kept)
        x_free, y, p = design_fixed_weight(rows, READS_FREE_WEIGHT)
        x_fixed, _, _ = design_fixed_weight(rows, FIXED_READ_WEIGHT)
        free = rounding_lp(x_free, y, p)
        fixed = rounding_lp(x_fixed, y, p)
        out[group] = {
            "n": len(rows),
            "reads_free": {"extra_pp": free["extra_pp"],
                          "coef": dict(zip(LP_FAMILIES, free["coef"]))},
            "reads_fixed_0.0118": {"extra_pp": fixed["extra_pp"],
                                   "coef": dict(zip(LP_FAMILIES, fixed["coef"]))},
        }
    return out


def show(result: dict) -> None:
    print("Smallest uniform extra percentage-point slack beyond whole-percent rounding "
          "(delta_pct +- windows), coefficients on Opus/Sonnet/Fable input-equivalent tokens "
          "and cache reads constrained non-negative. t = 0 means the group is not excluded by "
          "rounding alone; t > 0 means no stationary rate at any non-negative coefficients (at "
          "that read weight) fits every row.\n")
    for group, row in result.items():
        free, fixed = row["reads_free"], row["reads_fixed_0.0118"]
        print(f"{group:11} n={row['n']:3}  reads free: t={free['extra_pp']:.4f} pp   "
              f"reads at 0.0118 of Opus: t={fixed['extra_pp']:.4f} pp")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("masterrig", type=Path, help="a masterrig stretch file, as tools/model_rates.py takes")
    ap.add_argument("--json", type=Path, help="write the same figures as JSON")
    a = ap.parse_args(argv)
    reports = [json.loads(Path("history/gs-passive.json").read_text(encoding="utf-8")),
              json.loads(a.masterrig.read_text(encoding="utf-8"))]
    S = C.stretches_by_account(*reports)
    runs = C.harness_runs()
    kept = clean(S, runs)
    result = run(kept)
    show(result)
    if a.json:
        a.json.write_text(json.dumps({
            "_meta": {
                "what": "rounding-only feasibility of tools/model_rates.py's joint fit groups, "
                        "by linear programming; ports the Codex review of commit 447b926.",
                "no_traffic": "read-only arithmetic over committed files; no account was driven",
                "groups": list(GROUPS), "families": list(LP_FAMILIES),
                "fixed_read_weight": FIXED_READ_WEIGHT,
            },
            "result": result,
        }, indent=1) + "\n")
        print(f"\nJSON -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
