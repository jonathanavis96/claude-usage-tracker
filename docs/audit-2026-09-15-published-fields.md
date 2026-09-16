# Audit of every published field, 2026-09-15

Issue #26. Each key of `/data/claude-usage.json` (live copy fetched
2026-09-15, `generated_at` 05:30 UTC) checked against what produces it, what
the page (`website/src/pages/ClaudeUsageTracker.tsx` and
`website/src/lib/claudeUsage.ts` in all-done-sites-platform) says it means,
whether the value was right, and how stale it can get before anything on the
page says so. Findings that are wrong have a ticket each; the rest are notes.

Two page-wide staleness facts frame every row's last column:

- The page's only stale banner fires when `generated_at` is more than 3 days
  old. `daily.sh` publishes every day whether or not anything new arrived, so
  the banner means "the cron job died", never "the numbers are old".
- The publisher's only freshness rule refuses to publish when the newest
  usable prose probe row is more than 10 days old (`MAX_SAMPLE_AGE_DAYS`).
  Nothing checks the passive file's age.

| Field | Produced by | Page claim | Verdict on 2026-09-15 | Staleness behaviour |
|---|---|---|---|---|
| `generated_at` | `build_public_json`, publish time | "Last updated {date}. The daily job has not run since." shown only when older than 3 days | Correct (05:30 UTC daily run). | The only clock the page checks. Fresh every day regardless of data. |
| `last_sample_at` | Newest usable prose row (`tracker/rows.py`: not output, not outlier) | Hero pill "Last sample HH:MM", local time only, no date | Correct value (14 Sep 13:16, the Sonnet rerun). Rendering hides the date, so a five-day-old sample reads as today's. **#32** | Up to 10 days old with no signal (the 09-09 to 09-14 gap, #21, showed nothing). |
| `passive_generated_at` | `history/passive.json` `generated_at`, from masterrig | Not read by the page | Correct (15 Sep 01:15). | Unbounded. Publisher never checks it; page never reads it. Split, sessions and weekly figures freeze silently if masterrig stops. **#33** |
| `plan_measured` | Constant `"max20"` | Type only; not rendered | Correct. | n/a |
| `probe_account_count` | Count of distinct `account` tags on usable prose rows | Not shown on the page. | A count, not a list: account names are never published. | Reflects all history, never stale. |
| `plan_ratios` | Constant `{pro: 0.05, max5: 0.25, max20: 1.0}` | "Pro and Max 5x are scaled from it by Anthropic's published 1:5:20 ratios" | Correct for per-window rows. Per-week rows also multiply by each plan's own measured or assumed `weekly_windows.current`, so they are not 1:5:20 (Max 5x week comes out at 0.45 of a Max 20x week from the live figures). **#35** | Constant. |
| `rate_basis` | Constant `"api_value"` | Not read | Correct. | n/a |
| `rates[model]` | Regime median of the cross-model dollar series, divided by the model's blended price at the passive split | Hero: tokens per window, API value per window, the four-class split, "Source: probe, 8 Sep" | Values internally consistent (99.24 USD per window; Sonnet 295.5M tokens = 99.24 / blended price). `source`/`probed_at` mislabel provenance: the figure is a median of 10 readings across three models, not the named model's own probe. **#36** The split is the passive account's mix and the page says so in "How we measure". | Follows `last_sample_at` (10 days) for the dollar value; follows the passive file (unbounded, #33) for the split. |
| `effort` | `data/effort_matrix.json`, median total tokens per cell (`tracker/calibrate.py`) | "Effort figures come from one calibration task run at every effort level"; used only to scale sessions per window by `effort/medium` | Sonnet low (41,881) is above medium (22,742) because of two-turn runs, so low effort shows fewer sessions than medium. The stable figure, `effort_usd`, is published and unused. **#30** | Static file; one calibration on 2026-09-09. Never marked as dated on the page. |
| `effort_usd` | Same matrix, median meter dollars per cell, recomputed at publish against current prices | Not read by the page | Values monotonic on every model. Unused. | Recomputed daily from a static file. |
| `events` | Window events from `detect_changes` on the dollar series; weekly events from the weekly series | Markers on both charts | Empty, correctly for the window series (0.963 and 0.962 USD per 1%). Wrong for weekly: the 26% cut of 2026-09-13 was undetectable at calendar-week resolution. #25, fixed in PR #28. | As the series they derive from. |
| `last_change` | Newest of the two event lists, with `scope` | Headline. Null renders as "Anthropic hasn't changed Claude's limits since 5 Sep 2026" | Null. The sentence turns no-detection into a measurement and was wrong from 09-13. **#31** The weekly-scope wording ("in the week ending") is wrong once events are dated by day (#29). | Same as `events`. |
| `history[model]` | Step function: one regime, held flat at 99.24 USD; days before the first probe (08-11 to 09-04) marked `held` | Chart with dashed held segment and "Dashed: before measurement began" legend; `source` per day | Correct as a step function given one regime and no window event. 25 of 36 days are `held`, 7 to 10 `derived`, 1 to 4 `probe` per model. Starts 08-11 (passive history start), not the spec's 90 days. `source` means something different here than in `rates` (#36). | Extends to `now` every day, so the flat line grows whether or not a probe ran. |
| `session_tokens` | `tracker/turns.py`: median tokens of one session per model over the last 30 days of Jonathan's own transcripts on masterrig | "about N sessions per window", "Sessions per window/week" | Plausible (Sonnet 522k, Opus 1.49M, Fable 6.73M). Whose sessions is not stated on the page; the caveat's "one account running one workload mix" is the only hint. | Follows the passive file (unbounded, #33). |
| `weekly_windows` | `tracker/weekly.py` on the passive log, split per plan by `PLAN_CHANGE`; probe rows' own series (empty: no probe week clears the floors); pro = max5 with `assumed: true` | Weekly chart; "A week holds about N five-hour windows, measured from a real account"; per-week figures | `max20.current` 6.18 was the median of two pre-cut weeks and overstated the week by about 26% (#25, PR #28). Pro's assumed figure is presented as "measured from a real account" in the hero. **#35** `probe` series correctly empty. Chart legend and table footnote do label Pro as assumed. | Follows the passive file (unbounded, #33). `current` lagged a real step by up to two weeks before #28. |
| `api_price_per_mtok` | `data/prices.json`, including `class_weight` and `meter_weight` | Fallback only, for JSON without `api_value_per_window`; the fallback ignores the weights | Not verified against Anthropic's price list in this audit (a price lookup is out of scope here). Internally consistent with `rates`. | Static file; rewritten weekly by the output-weight run. |
| `contributed` | `tracker/contributed.py` from reader-posted samples | Nothing on the page reads it; `ContributeMeter` only posts | One contributor, one sample on max20; weekly figure null with reason. Harmless but unconsumed. | Refetched daily. |

## Notes without a ticket

- "How we measure" says the meter ticks "twice" per probe (rotation runs are
  3-tick) and that probes run "twice a day" (the crontab says so; the rows
  show one a day at best and a five-day gap, #21). Text drift, not a number.
- The Caveats paragraph's "roughly 2.5x" passive plan ratio is a typed-in
  number with no JSON source; the passive join currently computes 1.03.
  Ticketed as **#34** because it is a published number.
- `contributed`, `effort_usd`, `rate_basis` and `plan_measured` are published
  with no consumer. Not wrong; worth knowing when reading the JSON.

## Tickets filed

- #30 sessions scale by effort token totals (Sonnet low < medium)
- #31 `last_change: null` rendered as a measurement
- #32 probe gap up to ten days invisible; sample pill has no date
- #33 passive-derived figures have no freshness check
- #34 hard-coded "roughly 2.5x" caveat
- #35 Pro's assumed weekly figure shown as measured; per-week rows are not 1:5:20
- #36 "Source: probe, 8 Sep" names the wrong provenance
- #29 (from #25) weekly headline wording after day-dated events
