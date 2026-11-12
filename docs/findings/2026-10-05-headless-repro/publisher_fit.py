"""The tracker's own publish-time headless factor (the `five_hour_meter` block), recomputed from
the step snapshot in data/ with Python 3 and numpy.

    python3 publisher_fit.py                 # history of 5 Oct, rates of 5 Oct: 659 steps, 1.486 [1.381, 1.599]
    python3 publisher_fit.py --to 2026-10-11 # history and rates of 11 Oct:      738 steps, 1.484 [1.384, 1.592]

It does what tracker/five_hour_weight.py `headless_factor` does inside every publish: take the
clean one-point seven-day steps of all four accounts (data/tracker_steps.json, `clean`), keep
those with a headless split and credits, drop any that span a regime edge, value each step's
work and its headless work at the rates of that day (data/valuations.json), and fit
five-hour points per step ~ quasi-Poisson(regime + account + headless share). The factor is
exp(coefficient of the share); the interval is exp(b +/- 1.96 se) with the Pearson dispersion.
Each account is also fitted alone (no account terms) when it has at least 20 steps and its
share's standard deviation is at least 0.05. Nothing is written.
"""
import argparse
from bisect import bisect_right

import numpy as np

import common as K

RUNS = {"2026-10-05": ("history-2026-10-05", "rates-2026-10-05"),
        "2026-10-11": ("history-2026-10-11", "rates-2026-10-11")}

ap = argparse.ArgumentParser()
ap.add_argument("--to", choices=sorted(RUNS), default="2026-10-05")
a = ap.parse_args()
snap_name, rate_set = RUNS[a.to]
snap = K.tracker_steps(snap_name)
value = K.comparison_value(rate_set)
edges = sorted(K.ts(e) for e in snap["regime_edges"])

rows = []
for label, body in snap["accounts"].items():
    for st in body["steps"]:
        if not st["clean"] or st["headless_tokens"] is None:
            continue
        total = value(K.as_dict(st["tokens"]))
        head = value(K.as_dict(st["headless_tokens"])) if st["headless_tokens"] else 0.0
        if not total or head is None:
            continue
        start, end = K.ts(st["start"]), K.ts(st["end"])
        k = bisect_right(edges, start)
        if k != bisect_right(edges, end - (end - start) / 1e6):
            continue  # spans a regime edge
        rows.append({"account": label, "regime": k, "d5": float(st["d5"]),
                     "share": min(max(head / total, 0.0), 1.0)})


def fit(rs, by_account):
    cols = [np.ones(len(rs))] + K.dummies(rs, "regime") + (K.dummies(rs, "account") if by_account else [])
    X = np.column_stack(cols + [np.array([r["share"] for r in rs])])
    return K.factor(K.quasi_poisson([r["d5"] for r in rs], X))


f = fit(rows, True)
print("pooled: %s, %d steps" % (K.fmt(f, 3), len(rows)))
for label in sorted({r["account"] for r in rows}):
    own = [r for r in rows if r["account"] == label]
    s = np.array([r["share"] for r in own])
    line = "%s: " % label
    if len(own) >= 20 and s.std() >= 0.05:
        line += K.fmt(fit(own, False), 3) + ", "
    else:
        line += "not fitted alone, "
    print(line + "%d steps, mean headless share %.3f" % (len(own), s.mean()))
