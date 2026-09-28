# The 22 September change: why the page says about 2%, and the announcement gate removed

Seat 122, 2026-09-28. Inputs: the committed history at `d30cdef` plus the 17:30Z state commit.
The rebuilds below used `python3 -m tracker.rebuild_offline --now 2026-09-28T17:00:00+00:00`.
Nothing was pushed to the site.

## The question

The page shows the 22 September change as about +2% on the five-hour window, which is about
−2% on windows per week. A direct computation from the committed history gave about +15.6%.
That computation used the tracker's own `_side` estimator on
`accounts.<name>.weekly_by_window`. Max account 1 came from `history/masterrig-passive.json`
and Max account 2 from `history/gs-passive.json`, with the before side starting at
14 Sep 00:00Z and the instant at 22 Sep 17:03:48Z. The task was to find where the two figures
part company.

## Where they diverge

The published figure is not shrunk anywhere downstream. `five_hour_on_meters` has two
inputs: the by_window rows the publisher builds (`weekly_windows.max20.by_window`) and the
candidate's instant and bounds. Recomputed directly from those, it gives +2.0% exactly. So the
gap lies between the two sets of windows. The steps below close it one at a time. Each figure
is the five-hour reading, 1 / (windows-per-week ratio) − 1. "combined" is the tracker's own
`combine_log_ratios`. "pooled" is every account's windows summed. The bootstrap resamples
windows within each side and account, 4,000 draws.

| step | per account (before/after windows, change) | combined | pooled | bootstrap 95% (pooled) |
|---|---|---|---|---|
| 0. the task's windows | Max 1: 19/13, +16.5%; Max 2: 36/24, +13.7% | +15.3% | +15.4% | +2% to +28% |
| 1. Max 2's before side starts at its own weekly step, 14 Sep 13:05:31Z | Max 1 unchanged; Max 2: 35/24, +10.2% | +13.8% | +13.9% | +1% to +26% |
| 2. instant at the first Opus 5.5 turn, 22 Sep 19:41:49Z; windows ending within 5 h after it are left out | Max 1: 20/11, +14.7%; Max 2: 36/22, +7.7% | +11.5% | +11.9% | −1% to +24% |
| 3. Max 1 from `history/passive.json`, before side from 14 Sep 12:00Z | Max 1: 31/8, +4.8%; Max 2 unchanged | +6.2% | +8.4% | −6% to +20% |
| 4. Max 3 joins (published) | as step 3, plus Max 3: 15/4, −23.3% | **+2.0%** | +6.9% | −9% to +23% |

For step 0 this analysis gets Max account 1 at 5.16 → 4.43 (+16.5%). The task gave
5.19 → 4.42 (+17.4%). The small difference is most likely one boundary row.

Why each step is right, or at least what it rests on:

1. **Max account 2's before start.** The weekly cut reached Max account 2 at its own
   seven-day reset. Its certified step (`by_account.a2.regimes`) runs from 14 Sep 13:05:31Z.
   The window ending 14 Sep 03:45:24Z belongs to the old weekly cap (the 6.4
   windows-per-week regime), so it cannot sit on the before side of a five-hour change.
   The task's 00:00Z start was wrong for this account. For Max account 1 the start makes no
   difference: its own step came on 11 Sep, so the later of that and 14 Sep 12:00Z applies.
2. **The instant.** 17:03:48Z is the *start of the stretch* that holds Max account 1's first
   Opus 5.5 turn: that stretch runs from 19:03:48+02:00. The turn itself is stamped
   19:41:49Z, and the candidate uses the turn since `first_turns` has been recorded. A window
   that ends within five hours of the instant began before it, so it is on neither side. At
   17:03:48Z Max account 1's window ending 17:35Z (11 over 3) would count as "after", even
   though most of it ran before the new model's first use.
3. **Max account 1's instrument.** This is the largest single step, and it is a choice of
   instrument, not a computing error. The publisher has always read Max account 1 from
   `history/passive.json` `weekly_windows.by_window` (`_account_window_dicts`, reset-verified
   points only), because that is the series its weekly step is certified on. Two differences
   separate that file from `masterrig-passive.json`:
   - `masterrig-passive.json` pairs the merged moonlighter and ceiling logs. It has no windows
     at all between 13 Sep 14:30Z and 18 Sep 02:28Z, where the meter log has fifteen.
   - Most of its rows are not reset-verified.

   Over the same span, 18 Sep onward, the two still disagree: 4.99 → 4.74 (+5.3%) against
   5.12 → 4.46 (+14.7%). The after side is thin in both. Its seven-day movement totals 19
   points in one file and 24 in the other, and every point is a whole percent. So the two
   instruments are consistent with each other within their rounding. This change does not
   switch instruments.
4. **Max account 3.** It has 15 windows before, from 18 Sep, and 4 after. Its windows per week
   *rose* from 5.5 to 7.17. The combine weights each account by the inverse square of its
   rounding-interval half-width, which gives Max account 3 12%. That is enough to pull the
   combined figure from +6.2% to +2.0%. Max account 3 meets the existing rule (5 before, 1
   after). Pooling the windows instead would give +6.9%. Neither the rule nor the weighting
   changes here.

On the committed history, the honest answer is therefore somewhere between about −9% and
+23%, centred near +2% to +7% depending on how the accounts are combined. Every step's
interval includes no change. The +15.6% figure depended on:

- a pre-cut window on Max account 2's before side;
- a stretch-start instant with no straddle exclusion;
- the less complete of Max account 1's two logs;
- leaving Max account 3 out.

## What was wrong regardless: the announcement gate

`five_hour_on_meters` skipped every candidate without a five-hour announcement. It then
applied the five-hour reading (weekly cap unchanged) because the announcement said so. So an
announcement decided both *whether* a candidate was measured and *how* it was read.
`windows_per_week_ratio_note` also published a worked split computed from the announced 17%
weekly cut.

## The fix

- **Every candidate is measured.** Candidates are each model family's first use, as
  before: haiku, fable and opus-5-5 today. The per-account before side still starts at the
  later of the previous boundary and the account's own certified weekly step. The after side
  now also ends at the account's own pre-step regime end where the candidate precedes that
  step. Without that, Max account 1's fable after side would run across its 11 September cut.
- **Scope is decided from data** (`scope`, `credits.meter_scope`), from a joint fit over mixed
  work (`credits.joint_rate_fit`). This replaces the first version of this PR, which waited
  for 10 stretches with no Opus 5.5 in them. Of 39 accepted stretches starting after 22 Sep 19:41Z (history at 17:30Z; the brief counted 40 and 30), 29
  carry more than 10% output from other models: Sonnet 5 sub-agents vary the mix from stretch
  to stretch. Per stretch the model is

  meter % = (known + r × new) / (L × g)

  - `known` is the credits of models whose rate the candidate did not touch.
  - `new` is the candidate family's tokens valued at its base family's rate. The base family
    is the family's head where the table has it (Opus 5.5 → Opus), else the Opus anchor.
  - L is the account's own credits per 1%, from its before side.
  - g is the five-hour limit change.

  r and g are found by least squares on the log residuals. Their intervals come from a
  percentile bootstrap: 200 draws, stretches resampled within account and side, fixed seed.
  The fit is `separable` when r's 95% interval spans at most 3× and touches neither search
  bound (0.05× to 20×). A mix too uniform fails this, and `reason` says so.

  The weekly limit's change is g × the windows-per-week ratio. That holds because credits per
  1% of the weekly meter equal credits per 1% of the five-hour meter times five-hour over
  seven-day movement. Its interval combines g's bootstrap interval with a bootstrap interval
  on the ratio (`windows_per_week_ratio_bootstrap_interval`), treating both as independent and
  log-normal. The scope is then:
  - `five_hour` when only g's interval excludes no change;
  - `weekly` when only the weekly interval excludes no change;
  - `both` when both do;
  - `undetermined` otherwise.
- **The fit is generic.** Every candidate (each family's first use) is fitted in time order
  (`credits.absorb_new_family_rates`). A separable fit replaces that family's row in the rates
  every stretch is valued at: `rate_source` becomes `joint_fit_at_first_use`, and the replaced
  row is kept under `replaced`. So a later candidate's known work is priced at the earlier
  ones' fitted rates, and nothing is hand-typed. The replacement happens in the publisher on
  every publish, over `history/model-rates.json`'s rows. `tools/model_rates.py` itself is
  unchanged. Haiku and fable have no before side on a fitted account (Max account 1's stretches
  count from 6 Sep), so today only Opus 5.5 is fitted.
- **Today's joint fit, Opus 5.5 at 22 Sep 19:41:49Z:**
  - r = 0.758× Opus 5 [0.466, 1.100]. It is absorbed as the Opus 5.5 rate, replacing the
    pooled fit's 0.776× [0.708, 0.857], which assumed no limit change.
  - Five-hour limit change g = +30.5% [−2.0%, +62.7%]; log residual sd 0.29.
  - Weekly limit change g × 0.9802 = +27.9% [−8.4%, +78.7%].
  - Per account (before/after stretches, median Opus 5.5 share of the after side's credits,
    own g at the pooled r):
    - Max account 1: 45/10, 0.67, +35.1%.
    - Max account 2: 41/14, 0.93, +31.7%.
    - Max account 3: 52/3, 0.88, +11.7%.
    - Max account 4 has no before side.
  - With their own windows-per-week ratios, the per-account weekly changes are about +29%,
    +22% and +46% (point values).
  - The credits cross-check (`announced_change`), now pricing Opus 5.5 at the fitted rate,
    reads +31.4% [+11.6, +54.7].

  Both limits look to have risen by about the same amount, which is why windows per week
  barely moved (−2%). Neither interval excludes no change yet, so the scope is `undetermined`
  (`scope.reason`: "neither limit's change excludes no change yet"). The fit is separable,
  and the tracker will decide on its own as stretches arrive.
- **While the scope is undetermined, only windows per week steps.** The window regime and
  the week carry the previous regime's value: `per_week_factor` 1, `per_week_source`
  `previous_regime_carried_scope_undetermined`, and the window source
  `previous_regime_unscaled_scope_not_five_hour`, however thick the regime's own cluster.
  Windows per week chains by the paired ratio. Once the scope is decided:
  - `five_hour` scales the window by 1 / ratio and holds the week;
  - `weekly` holds the window and moves the week by the ratio;
  - `both` scales the window by g and the week by g × ratio.
- **Changes apply at once; the email waits.**
  - A candidate after the weekly cut `applies` as soon as one reading after it exists on a
    combined account, in state `measuring` upward. It opens a regime and enters `events` and
    `last_change`, and every publish recomputes it. The event carries `state` (also as
    `evidence_quality`) and the candidate instant `at`.
  - `bin/daily.sh` sends nothing until the change is 24 hours old. The age runs from `at`, or
    from the start of the change's date when it has none, to the publish's `generated_at`.
    From 24 hours it sends if the interval excludes no change; weekly events are certified,
    so they count as excluding it. From 48 hours it sends at the figure it then has.
  - The two-publish rule and `.notified-change` are unchanged: that file holds 2026-09-22, so
    this change is never sent again.
  - The payload now carries `scope`, `metric`, `change_pct`, `interval_pct` and `state`, and
    never the announcement. Jonathan's alert line quotes the figure, interval and state.
- `windows_per_week_ratio_note.consistent_with` loses the "announced 17% weekly cut" split.
  The five-hour-only and weekly-only splits stay.
- Announcements remain in the JSON only as reference metadata: `announcement` on candidates
  and `announced` on events.

## Before and after (rebuilt from the same inputs, `now` 2026-09-28T17:00Z)

"main" is `d30cdef` plus the 17:30Z state. "PR v1" is this PR's first commit (74f5302).

| figure | main | PR v1 | now |
|---|---|---|---|
| 22 Sep event | "Five-hour window +2.0%", scope five_hour, [−36.1, +51.0] | "Windows per week −2.0% (scope undetermined)", [−33.8, +56.5] | same headline; `state` measured, `at` 19:41:49Z; `scope_test` carries the joint fit |
| scope basis | announcement | Opus-5.5-free stretches: 0 after, undetermined | joint fit: g +30.5% [−2.0, +62.7], weekly +27.9% [−8.4, +78.7], undetermined |
| Opus 5.5 rate | 0.776× Opus (pooled fit, measured) | same | **0.758× [0.466, 1.100]**, `joint_fit_at_first_use` |
| per account (windows per week, before → after) | Max 1 4.96 → 4.74; Max 2 4.58 → 4.25; Max 3 5.50 → 7.17; Max 4 after only (3.88) | unchanged | unchanged |
| per-account five-hour reading | Max 1 +4.8%, Max 2 +7.7%, Max 3 −23.3% | unchanged | unchanged; joint fit g: +35.1%, +31.7%, +11.7% |
| window regimes (tokens) | 450.6M → 431.7M → 440.3M | 450.6M → 431.7M → 431.7M | 450.6M → 431.7M → 431.7M (carried) |
| window regimes (credits) | 19.54M → 18.72M → 19.10M | 19.54M → 18.72M → 18.72M | 19.54M → 18.72M → 18.72M |
| `per_week_regimes` | 2.922B → 2.173B → 2.173B (factor 0.9998) | 2.922B → 2.173B → 2.130B (factor 0.9802) | 2.922B → 2.173B → **2.173B** (factor 1, carried) |
| windows per week by regime | 6.48 → 5.03 → 4.94 (chained; pooled 4.44) | unchanged | unchanged |
| `weekly_windows.max20.regimes` | 6.48 (to 10 Sep) → 4.76 (from 11 Sep) | unchanged | unchanged |
| hero `per_week` | 2.173B | 2.130B | 2.173B |
| Opus 5.5 sessions per week | 870.5 [347.0, 1017.4] | 870.5 | 891.3 [270.5, 1546.7] (fitted rate's wider interval) |
| haiku and fable candidates | not listed | measured, undetermined, not applying | same; joint fit has no before side |

## Checks

- The hourly republish recomputes from history each time. `bin/daily.sh` runs
  `tracker.publish` on `history/*.json`, and `build_public_json` reads no earlier output.
  Only the notify step compares publishes, to decide whether to email, and it computes no
  figure.
- New tests:
  - A by_window fixture with a known +20% five-hour step (windows per week 5 → 5/1.2)
    publishes +20.0%, both scope-decided and under the undetermined five-hour reading.
  - The committed history, rebuilt with `ANNOUNCEMENTS` emptied, is identical apart from the
    `announcement`/`announced` metadata.
  - The joint fit on synthetic mixed stretches recovers a known rate (0.6×) and a +20%
    five-hour step within its intervals, published as five_hour +20%. A weekly-only step
    (−20%) is recovered as `weekly`. A uniform mix is reported as not separable, with the
    reason. The fit is reproducible, and a separable fit is absorbed into the rates.
  - A candidate applies from one reading after, in every state. An undetermined change
    carries the window and the week.
  - The notify gate waits under 24 hours; sends from 24 hours with a settled interval; waits
    between 24 and 48 hours with an unsettled one; sends at 48 hours at the figure then;
    never repeats a notified date; and sends no announcement text.
  - Further tests cover the own-step bound on the after side.
- Remaining hand-typed values, not changed here:
  - `CUT_AT` (14 Sep 12:00Z, the announced date of the weekly change) still bounds the
    regimes and the 14 September split.
  - `WEEKLY_CAP_MULTIPLIER` and the reference table's announced multipliers still feed the
    reference cross-check (`window_credits_from_weekly`, `reference.shortfall`).

  Both are labelled policy, but they are dates and figures the tracker did not measure.
