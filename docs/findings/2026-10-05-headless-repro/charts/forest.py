"""A horizontal forest plot on a log axis: rows of (label, detail, value, lo, hi, highlight)."""

import style
from style import plt


def forest(rows, title, subtitle, source, out, xlim=(0.8, 3.4), xticks=(1, 1.5, 2, 3),
           pooled=None, label_w=0.36, row_h=0.62, xlabel="Headless factor (log scale)", groups=None):
    n = len(rows) + (len(groups) if groups else 0)
    fig_h = 2.9 + row_h * n
    fig = plt.figure(figsize=(10, fig_h))
    ax = fig.add_axes([label_w, 1.15 / fig_h, 0.97 - label_w - 0.19, 1 - (1.15 + 1.6) / fig_h])
    style.frame(fig, title, subtitle, source, top_in=1.6, bottom_in=1.15)
    ax.set_position([label_w, 1.15 / fig_h, 0.97 - label_w - 0.19, 1 - (1.15 + 1.6) / fig_h])
    ax.set_xscale("log")
    ax.set_xlim(*xlim)
    ax.set_xticks(list(xticks))
    ax.set_xticklabels(["%gx" % t for t in xticks])
    ax.minorticks_off()
    ax.set_yticks([])
    ax.grid(axis="y", visible=False)
    ax.spines["left"].set_visible(False)
    ax.axvline(1.0, color=style.INK2, lw=1.5, zorder=2)
    if pooled is not None:
        ax.axvline(pooled, color=style.ORANGE, lw=1.5, alpha=0.6, zorder=2)
    ypos, y = [], 0
    gi = dict(groups or {})
    for i, r in enumerate(rows):
        if i in gi:
            ax.text(-0.02, y, gi[i], transform=ax.get_yaxis_transform(), ha="right", va="center",
                    fontsize=15, fontweight="bold", color=style.INK2)
            y += 1
        ypos.append(y)
        y += 1
    ax.set_ylim(y - 0.4, -0.7)
    for yy, r in zip(ypos, rows):
        label, detail, v, lo, hi, hl = r
        col = hl if isinstance(hl, str) else (style.ORANGE if hl else style.BLUE)
        hl = hl is True
        ax.plot([lo, hi], [yy, yy], color=col, lw=3 if hl else 2.2, zorder=3)
        ax.scatter([v], [yy], s=150 if hl else 110, color=col, edgecolors=style.SURFACE, linewidths=2, zorder=4)
        dy = -0.17 if detail else 0
        ax.text(-0.02, yy + dy, label, transform=ax.get_yaxis_transform(), ha="right", va="center",
                fontsize=16, color=style.INK, fontweight="bold" if hl else "normal")
        if detail:
            ax.text(-0.02, yy + 0.24, detail, transform=ax.get_yaxis_transform(), ha="right", va="center",
                    fontsize=13, color=style.INK2)
        ax.text(1.02, yy, "%.2f" % v, transform=ax.get_yaxis_transform(), ha="left", va="center",
                fontsize=16, color=style.INK, fontweight="bold" if hl else "normal")
        ax.annotate("%.2f-%.2f" % (lo, hi), (1.02, yy), xycoords=ax.get_yaxis_transform(),
                    xytext=(48, 0), textcoords="offset points", ha="left", va="center",
                    fontsize=13, color=style.INK2)
    ax.set_xlabel(xlabel)
    style.save(fig, out)
    return fig, ax
