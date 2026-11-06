"""Figure 1: five-hour points per seven-day point against each step's headless share.

Raw steps (faint, jittered: the meters read whole percentages), binned means with 95%
bootstrap intervals (steps resampled within each bin), and the fitted curve. The curve is
the quasi-Poisson fit that tools/headless_hypotheses.py runs (regime and account terms plus
headless share), averaged over the 306 steps at each share, so its end-to-start ratio is
exactly the fitted factor.
"""
import csv
import math
import os

import numpy as np

import style
from style import plt

ROWS = [r for r in csv.DictReader(open(os.path.join(style.DATA, "steps.csv"), newline=""))
        if r["original_window"] == "1"]
assert len(ROWS) == 306
y = np.array([float(r["five_hour_points"]) for r in ROWS])
assert all(r["seven_day_points"] == "1" for r in ROWS)
s = np.array([float(r["headless_share"]) for r in ROWS])

# design matrix exactly as refit_from_csv.py builds it
regimes = sorted({r["regime"] for r in ROWS})[1:]
accounts = sorted({r["account"] for r in ROWS})[1:]
cols = [np.ones(len(ROWS))]
cols += [np.array([r["regime"] == k for r in ROWS], float) for k in regimes]
cols += [np.array([r["account"] == a for r in ROWS], float) for a in accounts]
cols += [s]
X = np.column_stack(cols)
b = np.zeros(X.shape[1])
b[0] = math.log(y.mean())
for _ in range(60):
    mu = np.exp(X @ b)
    z = X @ b + (y - mu) / mu
    b = np.linalg.solve((X * mu[:, None]).T @ X, (X * mu[:, None]).T @ z)
mu = np.exp(X @ b)
disp = float(((y - mu) ** 2 / mu).sum() / (len(y) - X.shape[1]))
se = float(np.sqrt(np.diag(np.linalg.inv((X * mu[:, None]).T @ X)) * disp)[-1])
factor, lo, hi = math.exp(b[-1]), math.exp(b[-1] - 1.96 * se), math.exp(b[-1] + 1.96 * se)
base = np.exp(X[:, :-1] @ b[:-1])          # each step's all-interactive expectation
grid = np.linspace(0, 1, 101)
curve = np.array([base.mean() * math.exp(b[-1] * g) for g in grid])
print("factor %.2f [%.2f, %.2f]; curve %.2f -> %.2f" % (factor, lo, hi, curve[0], curve[-1]))

# bins
edges = [("0-0.15", lambda v: v <= 0.15),
         ("0.15-0.5", lambda v: 0.15 < v <= 0.5), ("0.5-0.9", lambda v: 0.5 < v <= 0.9),
         ("0.9-1 (all headless)", lambda v: v > 0.9)]
rng = np.random.default_rng(20261023)
bins = []
for name, test in edges:
    m = np.array([test(v) for v in s])
    yy, ss = y[m], s[m]
    boots = [yy[rng.integers(0, len(yy), len(yy))].mean() for _ in range(4000)]
    bins.append(dict(name=name, n=int(m.sum()), x=float(ss.mean()), mean=float(yy.mean()),
                     lo=float(np.percentile(boots, 2.5)), hi=float(np.percentile(boots, 97.5))))
for bn in bins:
    print("%-22s n=%3d x=%.3f mean=%.2f [%.2f, %.2f]" % (bn["name"], bn["n"], bn["x"], bn["mean"], bn["lo"], bn["hi"]))

fig, ax = plt.subplots(figsize=(10, 6.4))
jx = s + rng.uniform(-0.008, 0.008, len(s))
jy = y + rng.uniform(-0.18, 0.18, len(y))
ax.scatter(jx, jy, s=16, color=style.BLUE, alpha=0.16, linewidths=0, zorder=2)
ax.plot(grid, curve, color=style.ORANGE, lw=2.5, zorder=3)
for bn in bins:
    ax.plot([bn["x"], bn["x"]], [bn["lo"], bn["hi"]], color=style.INK, lw=2, zorder=4)
    ax.scatter([bn["x"]], [bn["mean"]], s=110, color=style.INK, edgecolors=style.SURFACE,
               linewidths=2, zorder=5)
    ax.annotate("%d steps" % bn["n"], (bn["x"], bn["hi"]), xytext=(0, 8), textcoords="offset points",
                ha="center", va="bottom", fontsize=14, color=style.INK2,
                bbox=dict(boxstyle="round,pad=0.15", fc=style.SURFACE, ec="none", alpha=0.85))

ax.annotate("%.1f" % curve[0], (0, curve[0]), xytext=(-12, 0), textcoords="offset points",
            ha="right", va="center", fontsize=16, fontweight="bold", color=style.INK)
ax.annotate("%.1f" % curve[-1], (1, curve[-1]), xytext=(12, 0), textcoords="offset points",
            ha="left", va="center", fontsize=16, fontweight="bold", color=style.INK)
LX, TX = 0.02, 0.09
ax.text(TX, 11.2, "Fitted: %.2fx  (95%% CI %.2f to %.2f)" % (factor, lo, hi),
        fontsize=16, color=style.INK, ha="left", va="center")
ax.plot([LX - 0.03, LX + 0.03], [11.2, 11.2], color=style.ORANGE, lw=2.5)
ax.scatter([LX], [10.4], s=110, color=style.INK, edgecolors=style.SURFACE, linewidths=2)
ax.text(TX, 10.4, "Binned mean, 95% interval", fontsize=15, color=style.INK2, va="center")
ax.scatter([LX], [9.6], s=40, color=style.BLUE, alpha=0.35, linewidths=0)
ax.text(TX, 9.6, "One step (jittered)", fontsize=15, color=style.INK2, va="center")

ax.set_xlim(-0.09, 1.09)
ax.set_ylim(0, 12)
ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
ax.set_yticks(range(0, 12, 2))
ax.set_xlabel("Headless (claude -p) share of the step's work")
ax.set_ylabel("Five-hour points per seven-day point")
ax.grid(axis="x", visible=False)
style.frame(fig, "Headless work moves the five-hour meter about 1.5x as far",
            "Five-hour meter points added for each 1% of the seven-day meter, by how much of the\nwork was headless. Curve: fit with limit-period and account terms.",
            style.SOURCE_306, top_in=1.5, bottom_in=1.15, left=0.09)
style.save(fig, "fig1_headline.png")
