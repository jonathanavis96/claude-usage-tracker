# Why Max account 1's 14 September readings did not reconcile (wf-155)

Inputs: the committed histories at `a240359` ("Daily publisher state 2026-10-08T12:02Z").
`python3 -m tracker.rebuild_offline --now 2026-10-08T12:02:00+00:00` on `main` reproduces the
live JSON of that hour (platform commit `f0d652b`) in every figure quoted here. Nothing was
published, and nothing sent a prompt to Claude.

The 14 September change itself, its date and its certification are settled and were not
re-examined. This note is about how its size was measured.

## The contradiction

For Max account 1 alone, the live event published three numbers:

| Field | Figure | What it reads |
|---|---|---|
| `windows_per_week_ratio.per_account.a1` | -24.9% [-35.0, -12.9] | five-hour meter windows, 15 Aug to 13 Sep against 13 Sep to 7 Oct |
| `direct_tests.weekly_change.per_account.a1` | -9.1% [-29.3, +16.9] | one-point seven-day steps, 20 Aug to 13 Sep against 18 to 22 Sep |
| `direct_tests.window_change.per_account.a1` | +3.4% [-3.8, +11.0] | stretches, 6 to 13 Sep against 13 to 22 Sep |

Windows per week is the week over the window, so the last two imply (1 - 0.091) / 1.034 - 1 =
-12%, against -24.9%. The tokens-per-week chart also drew its own 14 September step,
-10.75% (1,313,529 to 1,172,293 credits per 1% of the seven-day meter), and
`tokens_per_week_change` stated a fifth figure, -14.6%.

## Rules written before the common-basis figure was computed

These were written to the scratchpad before any 14 September figure on the common basis
existed. None was changed after it was seen.

- **R1 readings.** The collector's one-point seven-day steps (`weekly_steps`), selected by
  `weekly_meter.clean_steps(whole_history=True)`. That drops harness runs, cloud sessions,
  steps over capture-withheld stretches, and Max account 1's takeoff-pipeline days
  (`MASTERRIG_PHANTOM`, 2-6 Sep). All three quantities are read from this one kind of
  reading.
- **R2 valuation.** `comparison_value`, the direct tests' own. A step's five-hour points
  are divided by its headless inflation at the publish's fitted factor (rule 16). Seven-day
  points are not weighted.
- **R3 spans.** The direct weekly test's own sides (`cut_weekly_sides`). The before side
  runs from the account's last certified boundary to the end of its own step. The after
  side runs from its own step to the first candidate after the cut (22 Sep 19:41Z). A step
  enters a side only if it lies wholly inside it.
- **R4 quantities.** Each is computed on the same steps:
  - weekly = credits / seven-day points;
  - window = credits / five-hour points;
  - windows per week = five-hour points / seven-day points.

  So weekly = window x windows per week exactly.
- **R5 interval.** A whole-UTC-day bootstrap of each side (400 draws, fixed seed) and a t
  interval on the log ratio with df = days - 2 (`weekly_meter.account_change`). The same
  method is used for all three quantities.
- **R6.** Nothing is added or dropped after the result is seen.
- **Alternatives declared in advance**, each one change from the common basis:
  - A1: Max account 1's before side from 6 Sep only.
  - A2: raw five-hour points.
  - A3: the after side to the newest reading.
  - A4: windows per week read on meter windows.
  - A5: the before side from the detector's regime start. This turned out to be the same
    as R3, because Max account 1's steps begin on 20 Aug.

## On one basis the three agree

The common basis gives these figures:

| Account | Weekly (measured) | Window | Windows per week (quotient) | Steps / days, before and after |
|---|---|---|---|---|
| Max account 1 | -9.1% [-29.3, +16.9] | +9.0% [-18.8, +46.3] | -16.6% [-25.5, -6.6] | 155 / 19, 94 / 5 |
| Max account 2 | -10.4% [-25.9, +8.4] | +3.2% [-14.9, +25.1] | -13.1% [-18.6, -7.3] | 138 / 9, 94 / 7 |
| Combined at the weekly weights (0.35 / 0.65) | **-9.9% [-22.1, +4.1]** | +5.2% [-9.8, +22.6] | -14.3% [-19.0, -9.4] | |

Per account, and for the combined figures, the log of the weekly change is the sum of the
other two logs (`identity_gap` 0.0). Max account 1's -9.1% is the published direct figure to
the digit. There is no counting error in the seven-day steps. The three numbers disagreed
because each was read over different hours, from a different kind of reading.

## Where the published numbers part from it (Max account 1)

### Windows per week: -16.6% on the steps, -24.9% published

Each line below adds one difference to the line above it.

| Step | Windows per week before / after | Change |
|---|---|---|
| Common basis (steps) | 5.903 / 4.926 | -16.6% |
| Meter windows a kept step overlaps, same sides | 6.248 / 4.884 | -21.8% |
| + windows no kept step overlaps: 15-20 Aug (no transcripts, unweighted), 14-17 Sep (30-minute log only) | 6.327 / 4.878 | -22.9% |
| + windows over left-out steps (the 2-5 Sep takeoff pipeline, harness, cloud): 27 windows, before side | 6.327 / 4.878 | -22.9% |
| + after side to 7 Oct, across the certified 22 Sep change | 6.327 / 4.752 | **-24.9%** |

- **Two kinds of reading: 5.2 points.** On the hours both readings cover, meter windows and
  steps agree. For the 31 before-side windows that hold steps, the windows read 6.23 and
  the steps inside them 6.05. After the cut it is 4.93 and 4.76: -20.8% against -21.2%.
  The gap comes from coverage, not from counting. 39 of the steps' 155 before-side points
  straddle a five-hour window boundary and read 5.63. No meter window holds them.
- **Unweighted windows: 1.1 points.** Max account 1 has no transcripts before 20 Aug, so
  rule 16 cannot weight those windows.
- **After side across 22 Sep: 2.0 points.** The window and the week both changed at
  22 Sep (certified). Their quotient drifts down after it, so a side reaching 7 Oct reads
  part of the next regime.
- **Raw five-hour points (A2)** would make it -19.2% on the steps. Rule 16's headless
  weighting is already in every figure above.

The -24.9% is a correct reading of the detector's own record: the meter ratio over the
account's own two regimes. It is not the size of the 14 September change.

### Window: +9.0% on the steps, +3.4% published

| Step | Change |
|---|---|
| Common basis (steps from 20 Aug) | +9.0% |
| Before side from 6 Sep (A1) | +5.8% |
| Stretches instead of steps, known-date estimator (published) | **+3.4%** |

The direct window test reads Max account 1 only from 6 Sep (`MASTERRIG_FROM`). The direct
weekly test reads it from 20 Aug (rule 13). The -12% in the contradiction compounds a week
read from 20 Aug with a window read from 6 Sep. On the steps, Max account 1's window level
moves from 210k credits a point (week of 24 Aug) to 229k (week of 7 Sep). That is why rule
13 treats late August as a separate level on the five-hour meter.

### The chart's -10.75%

`per_week_regimes` stepped from a level pooled over Max accounts 1 and 2 to a level pooled over
Max accounts 1, 2 and 3. Max account 3's meter log starts on 15 September, so it sits only on
the after side. The rows also weigh each account by its seven-day points, and those shares
changed across the cut: Max account 1 went from 33% to 46% of the points. Its level is
lower than Max account 2's.

| Reading | Step |
|---|---|
| Published (Max accounts 1+2 before, 1+2+3 after) | -10.75% |
| Max accounts 1+2 both sides, pooled | -11.48% |
| Same accounts, before-side weights (the rows' own steps, Max account 1 from 6 Sep) | -10.38% |
| Each account against itself, the direct test's steps (rule 20) | **-9.90%** |

Mixing accounts moved the step by about 0.85 points. The weekly bridge factor
(`week_bridge` 1.04373, for 22 Sep) scales both rows equally, so it does not move the
14 September step.

### What else was checked

- **Seven-day reset handling.** Max account 1's reset (18 Sep 04:00Z) falls inside its after
  side. Steps never span a reset, by construction (`seven_day_steps` tiles within one
  segment).
- **Rounding at the 1% meter.** Steps hold exactly one seven-day point each, so the
  rounding is at the steps' two chain ends only.
- **Double counting and sub-agents.** On the hours both readings cover, they agree to
  within 3% a side, and the identity holds exactly on the steps.
- **Cross-account delegation.** The steps carry only the account's own transcripts. Steps
  where the meter moved on work the host did not see are already left out by the capture
  gate (R1).

None of these moves a figure.

## Alternatives for the corrected step

| Basis | Combined weekly | Max account 1 | Max account 2 |
|---|---|---|---|
| **Rule 20: common basis (R1-R5)** | **-9.9% [-22.1, +4.1]** | -9.1% | -10.4% |
| A1: Max account 1 from 6 Sep | -8.0% [-19.5, +5.1] | -5.2% | -10.4% |
| The chart rows' own steps, same accounts, before-side weights | -10.4% | -9.9% | -10.6% |
| A3: after side to 7 Oct (reads the 22 Sep change; not defensible) | -4.6% [-15.1, +7.2] | +1.7% | -7.0% |

Every defensible basis puts the weekly change between -8% and -10.4%, with an interval that
reaches past zero. The announced -17% is inside it. The -29% and -30% from PR #90 came from
the ratio routes that PR #127 found compounded a headless-weighted window into a raw meter
ratio. No reading here reproduces them.

## The fix: ADR 0001 rule 20

- **Same accounts on the chart.** `chain_certified_weeks` bridges the 14 September row of
  `per_week_regimes` to the direct weekly test's combined change, each account against
  itself. The chart's step is now -9.90%, and the label reads -10%. The 22 September step
  stays its certified +17.4%.
- **Reconciles by identity.** `cut_ratio_route` now reads the direct test's own steps
  (`weekly_meter.reconcile`). It publishes, per account, the weekly change, the window and
  windows per week on the same days. They are combined at one set of weights, so
  weekly = window x windows per week holds per account and combined.
- **The figure is still the direct weekly change.** `tokens_per_week_change` states the
  direct weekly change and carries the other two as its factors. It is never built from
  them. It moves from -14.6% to -9.9%.
- **Tests stated, not passed.** On rule 9 the weekly change fails both tests. It fails
  plan-wide: without Max account 1 it reads -10.4% [-25.9, +8.4], and without Max account 2
  it reads -9.1% [-29.3, +16.9]. It also fails the interval test: [-22.1, +4.1] includes no
  change. On the same steps, windows per week passes both: -14.3% [-19.0, -9.4]. Max account
  1 alone reads -16.6% and Max account 2 alone -13.1%, and each interval excludes zero.
  The window fails both, at +5.2% [-9.8, +22.6].

  So the seven-day steps show that the meters' ratio moved on both accounts. The credits per
  seven-day point cannot separate a 10% fall from none: valuing each day's work in credits
  scatters by about 20% a side. The page should say the weekly size is measured, not
  certified.
- **Check 10.** `weekly_routes_agree` also fails when the tokens-per-week chart's step at
  the event's marker is not the direct combined change. On `main`'s JSON it fails with
  -10.75% against -9.9%. On this branch it passes.

### Figures before and after (same inputs, 2026-10-08 12:02Z)

| Field | main | rule 20 |
|---|---|---|
| `per_week_regimes[0].value` (bridged) | 3,401,520,525 | 3,369,340,315 |
| 14 Sep tokens-per-week step | -10.75% | -9.90% |
| `per_week_regimes[0].week_bridge.factor` | 1.04373 | 1.033856 |
| `events[0].change_pct` (windows-per-week chart step, rule 17) | -10.2% (10) | -9.4% (9) |
| `events[0].interval_pct` | [-41.2, +42.2] | [-40.7, +43.5] |
| `tokens_per_week_change.signed_pct` | -14.7% [-28.8, +2.7] | -9.9% [-22.1, +4.1] |
| `tokens_per_week_change.windows_per_week_pct` | -18.8% | -14.3% |
| `tokens_per_week_change.five_hour_window_pct` | +5.1% | +5.2% |
| `last_change` | unchanged | unchanged |
| per-model rates and status | unchanged | unchanged |
| `window_tokens.regimes` (tokens per window) | unchanged | unchanged |
| 22 Sep steps (+17.4% week) | unchanged | unchanged |

Invariant results: checks 7-9 (blocking) pass on both. Checks 4 (`account_units`) and 11
(`detected_windows_per_week_agree`) are advisory and fail on both with the same rows; check
11's gap widens from 0.245 to 0.254 in log terms.

## Unresolved

- **Two windows-per-week figures for 14 September remain.** The windows-per-week chart's step
  (the event's `change_pct`, rule 17) is the bridged week over the window chart's own step.
  The window step is -0.6% (686.3M to 682.2M tokens, the across-cut medians from 6 Sep). So
  the chart's step reads -9.4%. On the steps, the same accounts read -14.3% [-19.0, -9.4].
  The two windows differ by estimator and span, and both include no change. Making the chart
  agree means reading the window regimes' 14 September step on the steps too. That is a
  window rule, so it was left for its own PR.
- **The detector's -24.9% for Max account 1** (`windows_per_week_ratio`, the event's
  `evidence`) is still its own regime record. It is published beside the figures as
  evidence and no figure reads it.
- **Check 11 still fails.** Max account 1's detected 6.33 from 15 Aug comes from the same
  windows. This PR does not tune it.
