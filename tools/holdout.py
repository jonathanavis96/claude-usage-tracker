"""Test the per-model meter rates out of sample: fit on one set of stretches, predict another.

The rates the publisher adopts (tools/model_rates.py) are fitted on the same stretches their
residuals are quoted on, so a small residual there says the model fits, not that it predicts.
This tool holds stretches out. It fits with tools/model_rates.py's own code -- the stretch
selection (`clean`, `prepare`), the family columns (`fit`), the design matrix (`design`) and
the solver (`nnls`) -- in the variant the publisher adopts (cache-read weight fitted jointly,
output at 5x input), then predicts each held-out stretch's meter movement from its tokens and
compares it with the movement the meter recorded.

    python3 -m tools.holdout
    python3 -m tools.holdout --json out.json

Two splits:

(a) Across accounts: fit on jwork's stretches and predict dave's, and the reverse, each within
    one side of the 14 September cut (the fits never cross it, and neither does this).
(b) Across time: fit on the stretches that ended by 2026-09-18T00:00Z and predict those that
    started after it, all after the 14 September cut. A stretch that straddles the split
    point is in neither set and is counted as such.

Per direction: n (held-out stretches predicted), the median absolute error in meter points,
the median absolute relative error, and the share of held-out stretches whose recorded
movement falls inside the 80% prediction interval. The interval is a bootstrap: each draw
refits on the training stretches resampled with replacement, and multiplies the draw's
prediction by one plus a relative residual drawn from the full training fit, so it carries
both the uncertainty in the rates and the scatter of a single stretch about them. A direction
whose training set is too small for tools/model_rates.py to fit (`MIN_FIT_N`) is reported as
not fittable, with its counts.

Read-only arithmetic over committed files: history/gs-passive.json, history/masterrig-passive.json
(loaded because tools/model_rates.py loads the accounts together; not fitted here, as the
publisher does not pool it) and history/harness-runs.jsonl. No account is driven.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import model_rates as M
from tracker import credits as C

SPLIT_AT = datetime(2026, 9, 18, tzinfo=timezone.utc)
OUT_MULT = 5          # the publisher's adopted variant: out5x ...
JOINT = True          # ... with the cache-read weight fitted jointly
SEED = 20260923


def _raw_fit(train: list[dict], out_mult: int, joint: bool) -> tuple[tuple[str, ...], np.ndarray] | None:
    """The families and raw coefficients (meter points per million tokens) model_rates.fit settles on.

    `fit` returns rates rescaled to credits, which cannot predict a meter movement on their
    own; the raw coefficients can. They are the same solve `fit` makes: its family selection,
    then `design` and `nnls` on the same rows.
    """
    base = M.fit(train, out_mult, joint)
    if not base:
        return None
    families = base["families"]
    X, y = M.design(train, out_mult, families, joint)
    return families, M.nnls(X, y)


def predict(train: list[dict], test: list[dict], rng: random.Random, resamples: int = M.RESAMPLES,
            out_mult: int = OUT_MULT, joint: bool = JOINT) -> dict:
    """Fit on `train`, predict `test`; the errors, the interval coverage and every stretch's row."""
    usable_train = [r for r in train if r["ok"]]
    usable_test = [r for r in test if r["ok"]]
    out: dict = {"n_train": len(usable_train), "n": len(usable_test),
                 "n_unusable": (len(train) - len(usable_train)) + (len(test) - len(usable_test))}
    solved = _raw_fit(usable_train, out_mult, joint)
    if solved is None or not usable_test:
        out["fitted"] = False
        out["why"] = ("no held-out stretch" if solved is not None else
                      f"the training set does not support a fit (tools/model_rates.py needs "
                      f"{M.MIN_FIT_N} clean stretches with Opus in {M.MIN_NONZERO} of them)")
        return out
    families, b = solved
    X, y = M.design(usable_train, out_mult, families, joint)
    fitted = X @ b
    rel_resid = [float((yy - ff) / ff) for yy, ff in zip(y, fitted) if ff > 0]
    Xt, _ = M.design(usable_test, out_mult, families, joint)
    yt = np.array([r["delta"] for r in usable_test], dtype=float)
    point = Xt @ b
    n = len(y)
    draws = [[] for _ in usable_test]
    for _ in range(resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        bb = M.nnls(X[idx], y[idx])
        pred = Xt @ bb
        for i, p in enumerate(pred):
            draws[i].append(float(p) * (1 + rel_resid[rng.randrange(len(rel_resid))]))
    rows, abs_err, rel_err, inside = [], [], [], 0
    for i, r in enumerate(usable_test):
        lo, hi = M.interval(draws[i])
        hit = lo <= yt[i] <= hi
        inside += hit
        err = float(point[i] - yt[i])
        abs_err.append(abs(err))
        rel_err.append(abs(err) / yt[i])
        rows.append({"account": r["account"], "start": r["start"], "end": r["end"],
                     "actual": float(yt[i]), "predicted": float(point[i]),
                     "interval": [lo, hi], "inside": bool(hit)})
    out.update({
        "fitted": True, "families": list(families),
        "median_abs_error_pts": st.median(abs_err),
        "median_abs_rel_error": st.median(rel_err),
        # Below 1, the training rates under-predict the held-out meter; above 1, over-predict.
        # An account-level offset shows here, where the absolute errors cannot tell its sign.
        "median_predicted_over_actual": st.median(r["predicted"] / r["actual"] for r in rows),
        "below_interval": sum(1 for r in rows if r["actual"] < r["interval"][0]),
        "above_interval": sum(1 for r in rows if r["actual"] > r["interval"][1]),
        "coverage": inside / len(rows), "nominal_coverage": (M.INTERVAL[1] - M.INTERVAL[0]) / 100,
        "train_residual_median_abs_rel": st.median(abs(v) for v in rel_resid),
        "stretches": rows})
    return out


def _parse(ts: str) -> datetime:
    return M.P(ts).astimezone(timezone.utc)


def time_split(recs: list[dict], at: datetime) -> tuple[list[dict], list[dict], int]:
    """Post-cut stretches ended by `at`, those started at or after it, and how many straddle it."""
    post = [r for r in recs if r["era"] == "post"]
    before = [r for r in post if _parse(r["end"]) <= at]
    after = [r for r in post if _parse(r["start"]) >= at]
    return before, after, len(post) - len(before) - len(after)


def directions(data: dict[str, list[dict]], split_at: datetime = SPLIT_AT) -> list[dict]:
    """Every train/test pair this tool reports, in the order it prints them."""
    out = []
    accounts = M.fit_accounts(data)
    for era in ("pre", "post"):
        for src in accounts:
            for dst in accounts:
                if src == dst:
                    continue
                out.append({"split": "account", "label": f"{src} -> {dst} ({era}-cut)", "era": era,
                            "train": [r for r in data.get(src, []) if r["era"] == era],
                            "test": [r for r in data.get(dst, []) if r["era"] == era],
                            "straddling": 0})
    pooled_before, pooled_after, pooled_straddle = [], [], 0
    for a in accounts:
        before, after, straddle = time_split(data.get(a, []), split_at)
        pooled_before += before
        pooled_after += after
        pooled_straddle += straddle
        out.append({"split": "time", "label": f"{a}: ended by {split_at:%Y-%m-%d} -> started after",
                    "era": "post", "train": before, "test": after, "straddling": straddle})
    out.append({"split": "time",
                "label": f"{' + '.join(accounts)}: ended by {split_at:%Y-%m-%d} -> started after",
                "era": "post", "train": pooled_before, "test": pooled_after,
                "straddling": pooled_straddle})
    return out


def run(data: dict[str, list[dict]], seed: int = SEED, resamples: int = M.RESAMPLES,
        split_at: datetime = SPLIT_AT) -> list[dict]:
    results = []
    for d in directions(data, split_at):
        # One seeded resampler per direction, keyed by its label, so one direction's figures
        # do not move when another gains a stretch.
        r = predict(d["train"], d["test"], random.Random(f"{seed}/{d['label']}"), resamples)
        results.append({"split": d["split"], "label": d["label"], "era": d["era"],
                        "straddling": d["straddling"], **r})
    return results


def load(harness_runs: list | None = None) -> dict[str, list[dict]]:
    """Every account's clean stretches, selected exactly as tools/model_rates.py selects them."""
    S = C.stretches_by_account(json.loads(Path("history/gs-passive.json").read_text(encoding="utf-8")),
                               json.loads(Path("history/masterrig-passive.json").read_text(encoding="utf-8")))
    kept = M.clean(S, C.harness_runs() if harness_runs is None else harness_runs)
    return {a: M.prepare(a, kept[a]) for a in S}


def show(results: list[dict]) -> str:
    lines = [f"{'direction':<52} {'train':>5} {'n':>4} {'med |err| pts':>13} {'med |rel|':>9} "
             f"{'in 80% PI':>9} {'pred/act':>8}"]
    for r in results:
        if not r["fitted"]:
            lines.append(f"{r['label']:<52} {r['n_train']:>5} {r['n']:>4}   not fittable: {r['why']}")
            continue
        lines.append(f"{r['label']:<52} {r['n_train']:>5} {r['n']:>4} {r['median_abs_error_pts']:>13.2f} "
                     f"{r['median_abs_rel_error']:>9.3f} {r['coverage']:>9.2f} "
                     f"{r['median_predicted_over_actual']:>8.3f}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", type=Path, help="also write every direction and held-out stretch as JSON")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--resamples", type=int, default=M.RESAMPLES)
    a = ap.parse_args(argv)
    results = run(load(), a.seed, a.resamples)
    print(show(results), end="")
    if a.json:
        a.json.write_text(json.dumps({
            "_meta": {"what": "out-of-sample test of the per-model meter rates; see tools/holdout.py",
                      "split_at": SPLIT_AT.isoformat(), "cut": M.CUT.isoformat(),
                      "variant": "joint" if JOINT else "zero", "output_multiplier": OUT_MULT,
                      "interval_percentiles": M.INTERVAL, "seed": a.seed, "resamples": a.resamples},
            "results": results}, indent=1, default=float) + "\n", encoding="utf-8")
        print(f"JSON -> {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
