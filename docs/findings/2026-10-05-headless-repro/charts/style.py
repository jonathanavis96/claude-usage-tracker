"""Shared look for the post's figures: opaque light surface, large type, recessive grid."""
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.dirname(HERE)
DATA = os.path.join(FOLDER, "data")
RECORDED = os.path.join(FOLDER, "recorded")

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#7a7974"
GRID = "#e4e3df"
BLUE = "#2a78d6"      # categorical slot 1
ORANGE = "#eb6834"    # categorical slot 2 (the highlighted row)
NEUTRAL = "#b9b8b2"   # reference lines

SOURCE_306 = "Data: 306 seven-day meter steps, three Max 20x accounts, 14 Sep - 4 Oct 2026"

plt.rcParams.update({
    "font.family": "Liberation Sans",
    "font.size": 15,
    "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK2,
    "axes.labelsize": 16,
    "xtick.color": INK2,
    "ytick.color": INK2,
    "xtick.labelsize": 15,
    "ytick.labelsize": 15,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 1.0,
    "grid.linestyle": "-",
    "lines.solid_capstyle": "round",
    "lines.solid_joinstyle": "round",
})


def load(path):
    with open(path) as fh:
        return json.load(fh)


def frame(fig, title, subtitle, source, top_in=1.25, bottom_in=0.95, left=None, right=0.97):
    """Left-aligned title + subtitle above the plot, source line below it (offsets in inches)."""
    h = fig.get_figheight()
    kw = dict(top=1 - top_in / h, bottom=bottom_in / h, right=right)
    if left is not None:
        kw["left"] = left
    fig.subplots_adjust(**kw)
    fig.text(0.02, 1 - 0.22 / h, title, ha="left", va="top", fontsize=21, fontweight="bold", color=INK)
    if subtitle:
        fig.text(0.02, 1 - 0.68 / h, subtitle, ha="left", va="top", fontsize=15, color=INK2, linespacing=1.3)
    fig.text(0.02, 0.18 / h, source, ha="left", va="bottom", fontsize=13, color=MUTED)


def save(fig, name):
    out = os.path.join(HERE, name)
    fig.savefig(out, dpi=200)
    print("wrote", out)
