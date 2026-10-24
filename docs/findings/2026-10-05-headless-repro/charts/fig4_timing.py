"""Figure 4: timing-shift check. Each step's transcript turns are re-binned into meter steps
with an offset; the factor is highest with no offset. Numbers read from repro/tables.json
(tools/headless_hypotheses.py at da63a25, 306 steps)."""
import os

import style
from style import plt

t = style.load(os.path.join(style.RECORDED, "tables.json"))["original_window"]["timing_shift"]
xs = {"-30 min": -30, "-10 min": -10, "0": 0, "+10 min": 10, "+30 min": 30}
pts = [(xs[r["shift"]], r["factor"], r["ci95"][0], r["ci95"][1]) for r in t]
for p in pts:
    print(p)

fig, ax = plt.subplots(figsize=(10, 6.2))
style.frame(fig, "The effect is strongest with the timestamps as recorded",
            "Headless factor when every transcript turn is re-binned into meter steps with this\noffset (95% intervals). Every offset gives a lower estimate; the intervals overlap.",
            style.SOURCE_306, top_in=1.55, bottom_in=1.25, left=0.11)
ax.plot([p[0] for p in pts], [p[1] for p in pts], color=style.BLUE, lw=2, zorder=3)
for x, v, lo, hi in pts:
    col = style.ORANGE if x == 0 else style.BLUE
    ax.plot([x, x], [lo, hi], color=col, lw=2.5 if x == 0 else 2, zorder=3)
    ax.scatter([x], [v], s=150 if x == 0 else 110, color=col, edgecolors=style.SURFACE, linewidths=2, zorder=4)
    ax.annotate("%.2f" % v, (x, hi), xytext=(0, 8), textcoords="offset points", ha="center", va="bottom",
                fontsize=17, color=style.INK, fontweight="bold" if x == 0 else "normal")
ax.set_xticks([-30, -20, -10, 0, 10, 20, 30])
ax.set_xticklabels(["-30", "-20", "-10", "0", "+10", "+20", "+30"])
ax.set_xlim(-35, 35)
ax.set_ylim(1.0, 1.8)
ax.set_yticks([1.0, 1.2, 1.4, 1.6, 1.8])
ax.set_yticklabels(["1.0x", "1.2x", "1.4x", "1.6x", "1.8x"])
ax.set_xlabel("Offset applied to transcript timestamps (minutes; 0 = as recorded)")
ax.set_ylabel("Headless factor")
ax.grid(axis="x", visible=False)
style.save(fig, "fig4_timing.png")
