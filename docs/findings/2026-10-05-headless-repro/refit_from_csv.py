"""Refit the headless factor from steps.csv alone (Python 3 and numpy, nothing else).

    python3 refit_from_csv.py               # the 14 Sep - 4 Oct window (306 steps)
    python3 refit_from_csv.py --all         # extended to 11 Oct (397 steps)
    python3 refit_from_csv.py --incl        # share with cache reads in, as the 2026-10-05
                                            # five-hour doc's per-account table
    python3 refit_from_csv.py --all --incl  # per account to 11 Oct

The table is data/steps.csv; another path may be given as the first argument.

Model, as tools/headless_hypotheses.py fits it: five-hour points in each one-point seven-day
step ~ quasi-Poisson(log link), with an intercept, regime dummies, account dummies and the
step's headless share of credits. The factor is exp(coefficient of the share): how much more
the five-hour meter moves per seven-day point when a step's work is all headless rather than
all interactive. The 95% interval is exp(b +/- 1.96 se), se scaled by the Pearson dispersion.
"""
import csv
import math
import os
import sys

import numpy as np


def fit(rows, by_account=True):
    y = np.array([float(r["five_hour_points"]) for r in rows])
    regimes = sorted({r["regime"] for r in rows})[1:]
    accounts = sorted({r["account"] for r in rows})[1:] if by_account else []
    cols = [np.ones(len(rows))]
    cols += [np.array([r["regime"] == k for r in rows], float) for k in regimes]
    cols += [np.array([r["account"] == a for r in rows], float) for a in accounts]
    cols += [np.array([float(r[SHARE]) for r in rows])]
    X = np.column_stack(cols)
    b = np.zeros(X.shape[1])
    b[0] = math.log(y.mean())
    for _ in range(60):                       # IRLS
        mu = np.exp(X @ b)
        z = X @ b + (y - mu) / mu
        b = np.linalg.solve((X * mu[:, None]).T @ X, (X * mu[:, None]).T @ z)
    mu = np.exp(X @ b)
    disp = float(((y - mu) ** 2 / mu).sum() / (len(y) - X.shape[1]))
    se = np.sqrt(np.diag(np.linalg.inv((X * mu[:, None]).T @ X)) * disp)
    return math.exp(b[-1]), math.exp(b[-1] - 1.96 * se[-1]), math.exp(b[-1] + 1.96 * se[-1]), len(rows)


SHARE = "headless_share_incl_cache_reads" if "--incl" in sys.argv else "headless_share"
paths = [a for a in sys.argv[1:] if not a.startswith("--")]
path = paths[0] if paths else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "steps.csv")
with open(path, newline="") as f:
    rows = list(csv.DictReader(f))
if "--all" not in sys.argv:
    rows = [r for r in rows if r["original_window"] == "1"]
print("all accounts: %.2f [%.2f, %.2f], %d steps" % fit(rows))
for a in sorted({r["account"] for r in rows}):
    print("%s: %.2f [%.2f, %.2f], %d steps" % ((a,) + fit([r for r in rows if r["account"] == a], False)))
