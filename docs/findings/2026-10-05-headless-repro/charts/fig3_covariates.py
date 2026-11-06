"""Figure 3: the headless factor with each other variable added to the fit (covariate table,
tools/headless_hypotheses.py at c87d4c7, 306 steps). Numbers read from repro/tables.json."""
import os

import style
from forest import forest

LABELS = {
    "none": "Nothing added",
    "concurrency, credit-weighted mean (log)": "Concurrency (mean)",
    "concurrency, peak (log)": "Concurrency (peak)",
    "credits per active minute (log)": "Burstiness (credits/active min)",
    "busiest minute's share of the step": "Busiest minute's share",
    "Opus 5.5 share": "Opus 5.5 share",
    "Sonnet share": "Sonnet share",
    "Fable share": "Fable share",
    "effort (low 1 to max 5, credit-weighted)": "Effort setting",
    "weekday 12-18Z share": "Peak hours (wkday 12-18 UTC)",
    "weekday 08-12Z share": "Weekday 08-12 UTC share",
    "weekend share": "Weekend share",
    "all of them together": "All of them together",
}
t = style.load(os.path.join(style.RECORDED, "tables.json"))["original_window"]["covariates"]
assert len(t) == 13
rows = []
for r in t:
    h = r["headless_beside_it"]
    hl = r["added_variable"] == "all of them together"
    rows.append((LABELS[r["added_variable"]], None, h["factor"], h["ci95"][0], h["ci95"][1], hl))
    print(LABELS[r["added_variable"]], h)
forest(rows, "Adding other explanations leaves the headless factor at 1.5x",
       "Headless factor after adding each variable to the fit, one at a time, then\nall together (95% intervals). It stays between 1.48 and 1.59.",
       style.SOURCE_306, "fig3_covariates.png", xlim=(0.95, 1.9), xticks=(1, 1.25, 1.5, 1.75),
       pooled=1.53, label_w=0.40, row_h=0.5)
