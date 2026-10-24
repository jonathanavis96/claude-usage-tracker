"""Figure 5: how the 1.5x splits between the two meters. Three fits on the same clean steps,
each with regime and account terms: five-hour points per credit, seven-day points per credit,
and their ratio (five-hour points per seven-day point). Numbers computed by ../meter_split.py."""
import sys

import style
from forest import forest

sys.path.insert(0, style.FOLDER)
import meter_split  # noqa: E402


def summary(w):
    m = meter_split.window(meter_split.WINDOWS[w])["fitted/clean"]
    return {"steps": m["steps"],
            "five_hour_points_per_credit_headless_over_interactive": m["five_hour_per_work"],
            "seven_day_points_per_credit_headless_over_interactive": m["seven_day_per_work"],
            "ratio_five_hour_per_seven_day": m["ratio"]}


main = summary("14Sep-4Oct")
chk = summary("14Sep-11Oct")
GRAY = "#8a8985"


def rows_for(d, color):
    out = []
    for key, label in [("five_hour_points_per_credit_headless_over_interactive", "Five-hour meter, per credit"),
                       ("seven_day_points_per_credit_headless_over_interactive", "Seven-day meter, per credit"),
                       ("ratio_five_hour_per_seven_day", "Ratio: five-hour per seven-day")]:
        v, lo, hi = d[key]
        out.append((label, None, v, lo, hi, color))
        print(label, v, lo, hi)
    return out


rows = rows_for(main, style.BLUE) + rows_for(chk, GRAY)
forest(rows, "The extra lands mostly on the five-hour meter",
       "Meter points per credit of work, headless vs interactive (95% intervals). Credits are the\ntracker's token-weighted cost estimate; the split shifts if those weights change.",
       "Data: %d seven-day meter steps, three Max 20x accounts, 14 Sep - 4 Oct 2026; check: %d steps to 11 Oct"
       % (main["steps"], chk["steps"]),
       "fig5_split.png", xlim=(0.8, 2.2), xticks=(0.8, 1, 1.25, 1.5, 2), label_w=0.40, row_h=0.55,
       groups={0: "14 Sep - 4 Oct (%d steps)" % main["steps"], 3: "Check: to 11 Oct (%d steps)" % chk["steps"]})
