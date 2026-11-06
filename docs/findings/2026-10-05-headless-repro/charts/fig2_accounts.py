"""Figure 2: per-account headless factors with 95% intervals (tools/headless_hypotheses.py,
306-step window). Numbers read from repro/fits.json, original_window."""
import os

import style
from forest import forest

f = style.load(os.path.join(style.RECORDED, "fits.json"))["original_window"]
h = f["headline"]
rows = []
for acct in ["Max account 2", "Max account 3", "Max account 4"]:
    a = f["per_account"][acct]
    over = a["steps_over_0.8_headless"]
    note = "%d steps, avg %d%% headless, %d over 80%%" % (a["steps"], round(100 * a["mean_headless_share"]), over)
    rows.append((acct, note, a["factor"], a["ci95"][0], a["ci95"][1], False))
    print(acct, a["factor"], a["ci95"], note)
rows.append(("All three, pooled", "%d steps, with account terms" % f["window"]["steps"],
             h["factor"], h["ci95"][0], h["ci95"][1], True))
forest(rows, "Every account's estimate is above 1x (1.5x to 2.2x)",
       "Headless factor per account: five-hour points per seven-day point, all-headless vs\nall-interactive. Bars are 95% intervals; 1x would mean no difference.",
       style.SOURCE_306, "fig2_accounts.png", pooled=h["factor"], label_w=0.42)
