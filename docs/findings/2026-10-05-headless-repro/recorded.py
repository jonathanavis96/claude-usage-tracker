"""Print the figures that need the raw Claude Code transcripts, as recorded by the re-run of
2026-10-23 (recorded/fits.json and recorded/tables.json). These are not recomputed here: the
transcripts stay private. Everything else in the post is recomputed by the other scripts.

    python3 recorded.py
"""
import json

import common as K

fits = json.loads((K.HERE / "recorded" / "fits.json").read_text())
tabs = json.loads((K.HERE / "recorded" / "tables.json").read_text())
o, t = fits["original_window"], tabs["original_window"]
def ci(d):
    return "%.2f [%.2f, %.2f]" % (d.get("factor", d.get("value")), *d["ci95"])

print("5a. Share of credits going to cache writes, by kind of session (to 4 Oct):")
for acct, kinds in t["cache_write_shares"].items():
    print(f"  {acct}: " + "; ".join(
        f"{k} 1h {v['one_hour_write_share_of_credits']:.3f} / 5m {v['five_minute_write_share_of_credits']:.3f}"
        for k, v in kinds.items()))
print("5a. Write pricing (headless factor):")
for row in t["ttl_pricing"]:
    print(f"  {row['pricing']}: {ci(row)}")
print("5b. Per-run overhead: first-turn cache write share of headless credits "
      f"{t['per_run_overhead']['first_turn_cache_write_share_of_headless_credits']:.3f}")
print("5c. Timing shift:")
for row in t["timing_shift"]:
    print(f"  {row['shift']}: {ci(row)}")
print("5d. Other step features (headless beside each, share counting cache reads):")
for row in t["other_variables"]:
    if row["model"].startswith("headless +") and "12-18Z" not in row["model"]:
        print(f"  {row['model']}: {ci(row['headless'])}")
b = t["ttl_pricing"][0]
print("The 5 Oct doc's crude buckets (collector pricing): credits per 1% of the seven-day meter "
      f"{b['credits_per_1pct_seven_day_k']['headless_share_over_0.8']}k (over 0.8 headless) against "
      f"{b['credits_per_1pct_seven_day_k']['headless_share_under_0.2']}k (under 0.2)")
print("Headline with the refit cache-read weight of PR #125 (0.00899): "
      f"{ci(o['headline_at_cache_read_weight_0.00899'])}")
