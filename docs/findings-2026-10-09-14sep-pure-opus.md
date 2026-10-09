# The 14 September weekly step on pure-Opus seven-day steps, and its freeze (wf-156)

Inputs: the committed histories at `33e6d1b` ("Daily publisher state 2026-10-09T14:02Z"),
read through `python3 -m tracker.rebuild_offline --now 2026-10-09T14:02:00+00:00`. Nothing
was published, and nothing sent a prompt to Claude.

The 14 September change, its date and its existence are settled and are not re-examined.
This note is about its size on the seven-day meter. The published figure (ADR 0001 rule 20)
is -9.9% [-22.1, +4.1]: credits per 1% of the seven-day meter, each account against itself.
Its interval is wide because every day's mixed work is valued in credits (per-model rates,
the cache-read weight), which scatters by about 20% a side. The five-hour window headline
avoids that by reading only pure-Opus stretches in raw tokens, with no rate. This note asks
whether the same reading on the seven-day meter measures the weekly step more tightly.

Anthropic announced a 17% weekly reduction. That is used as a test of the maths only. It is
not an input, not a target and not a page figure.

## Rules written before any 14 September figure was computed

These rules were committed on their own, before the computation below was run. Nothing was
added or dropped after the result was seen.

- **P1 readings.** The collector's one-point seven-day steps (`weekly_steps`), selected as
  rule 20 selects them: `weekly_meter.clean_steps(whole_history=True)`. That drops harness
  runs, cloud sessions, steps over capture-withheld stretches and Max account 1's
  takeoff-pipeline days (`MASTERRIG_PHANTOM`, 2-6 Sep).
- **P2 pure Opus.** A step is pure Opus when it carries tokens and every model id in its
  `tokens` is in the `opus` family (`credits.family`). This is the window headline's own
  test (`pure_family_rows`, `pure_family_of`). Opus 5.5 is its own family (`opus-5-5`), so a
  step holding any Opus 5.5 turn is not pure Opus. Opus 5.5 does not exist before
  22 September, and the after side ends at 22 Sep 19:41Z (P4), so in practice the family is
  Opus 5 on both sides. A step with an unknown or unpriced model is not pure Opus.
- **P3 attributing the meter to a step.** Each step holds exactly one seven-day point: the
  span between two consecutive crossings of the 1% meter, each placed at the upper reading
  of its bracket (`seven_day_steps`). A turn belongs to the step whose [start, end) holds
  its timestamp. Points are whole; no fraction of a point is read.
  - A step with no tokens has no family. It is not pure Opus and is left out, with its point.
  - Exception: a step with no tokens and zero length (start equal to end) is the empty half
    of a two-point jump in one reading. It belongs to the step of the same account that ends
    at its start. That step then holds two points, and the empty step is kept or left out
    with it.
  - Rounding: placing a crossing at its upper reading moves each end of a step by at most
    one sample gap. In a chain these errors cancel. A pure-Opus step chosen from inside a
    chain keeps both its end errors. The error is the same kind on both sides, so it is
    left to the interval.
- **P4 spans.** Rule 20's sides, each account's own: `credits.cut_weekly_sides` through
  `credits.cut_weekly_steps`. The before side runs from the account's last certified
  boundary to the end of its own weekly step's regime. The after side runs from its own step
  to the first change candidate after the cut, 22 Sep 19:41Z. A step enters a side only if
  it lies wholly inside it.
- **P5 quantity.** Tokens per seven-day point: a step's raw token counts summed over all
  four classes (input, output, cache read, cache write; `credits.class_totals`), with no
  rate and no class weight. Headless turns count as they are, because the seven-day meter is
  not weighted (rule 16). The five-hour meter is not read.
- **P6 estimator.** Per side, the pooled level: the side's tokens summed over its
  seven-day points summed. This is rule 20's estimator, `weekly_meter.account_change`.
- **P7 interval.** `weekly_meter.account_change` unchanged: a whole-UTC-day bootstrap of
  each side (400 draws, fixed seed), and a t interval on the log ratio with
  df = days before + days after - 2.
- **P8 accounts and combining.** Max account 1 and Max account 2. Max accounts 3 and 4 enter
  only if `account_change` gives them a figure on both sides. Accounts are combined by
  inverse variance, each against itself, through `weekly_meter.weekly_change`
  (`credits.direct_change`), as rule 20 combines them.
- **P9 tests.** ADR 0001 rule 9 on the combined change: plan-wide (each account left out in
  turn keeps the direction with an interval excluding no change) and the interval test (the
  combined interval excludes no change). Each verdict is reported whether it passes or not.
- **P10 thin sample.** The pure-Opus figure is too thin to publish if, for Max account 1 or
  Max account 2, `account_change` gives no figure, or either side holds fewer than 3 UTC
  days or fewer than 20 seven-day points.
- **P11 which figure is published.** The pure-Opus figure replaces -9.9% only if all three
  hold:
  1. it is not thin (P10);
  2. its combined 95% interval is narrower than rule 20's, measured as the width of the log
     interval (rule 20: ln(1.041 / 0.779) = 0.290);
  3. it passes more of the two rule 9 tests than rule 20 does. Rule 20 passes neither, so
     the pure-Opus figure must pass at least one.

  Otherwise -9.9% [-22.1, +4.1] stays. The point estimate plays no part in the choice. The
  announced -17% plays no part. Whether -17% lies inside each interval is reported as a test
  only.
- **P12 freeze.** Whichever figure P11 picks becomes ADR 0001 rule 21. The 14 September
  weekly size is frozen at it, and is reopened only for a counting error shown with
  evidence, never because another method gives another number.

### Alternatives declared in advance

Each is one change from P1-P9. They are reported, and none of them can be chosen.

- **B1 median.** Per side, the median of each step's tokens per point (the window
  headline's estimator), with the same day bootstrap and t interval on the log ratio.
- **B2 empty steps kept.** Steps with no tokens and non-zero length are kept at zero tokens,
  as `weekly_meter.valued` keeps them in credits.
- **B3 credits on the same steps.** The pure-Opus steps valued in credits by
  `credits.comparison_value`, the rule 20 valuation. This separates the effect of choosing
  the steps from the effect of reading tokens.
- **B4 rule 20's weights.** The two accounts combined at rule 20's weights (Max account 1
  0.35, Max account 2 0.65) instead of their own inverse variances.
- **B5 Max account 1 from 6 September.** Rule 20's A1: Max account 1's before side starts on
  6 Sep.

## Results

Computed after the rules above were committed (`a0d76ae`), on the rebuild of `33e6d1b`. The
first candidate after the cut is 22 Sep 19:41:49Z, so every after side ends there.

### Pure-Opus steps on each side (P1-P4)

| Account | Before: steps / points / days | After: steps / points / days | Figure |
|---|---|---|---|
| Max account 1 | 8 / 8 / 6 | 0 / 0 / 0 | none: no after side |
| Max account 2 | 15 / 15 / 2 | 3 / 3 / 1 | none: one day after (`WEEKLY_MIN_DAYS` is 2) |
| Max account 3 | 0 / 0 / 0 | 6 / 6 / 2 | none: no before side (P8) |

Rule 20 reads 155 and 138 steps before and 94 and 94 after on the same sides. Pure-Opus
steps are 5% of Max account 1's before side and 11% of Max account 2's. After the cut they
are 0 of 94 and 3 of 94. Which other families the remaining steps hold was not tabulated.

No account gives a figure, so there is no combined change, no interval and nothing for
rule 9 to test. P10 calls the sample thin on every count: Max account 1 has no account
change and no after side, and Max account 2 has no account change, 2 days before and 1 after,
and 15 and 3 points.

### Alternatives

None of the alternatives gives a figure either, for the same reason:

- **B1 median**: needs 2 days a side. Max account 2 has 1 day after, Max account 1 none.
- **B2 empty steps kept**: adds one step to Max account 1's before side (9) and one to Max
  account 3's after side (7). Nothing else changes.
- **B3 credits on the same steps**: the same steps, so the same counts.
- **B4 rule 20's weights**: no account change to weight.
- **B5 Max account 1 from 6 Sep**: Max account 1's before side falls to 0 steps.

### Rule 20 on the same inputs

| Account | Weekly change | Steps / days, before and after | Weight |
|---|---|---|---|
| Max account 1 | -9.1% [-29.4, +17.1] | 155 / 19, 94 / 5 | 0.345 |
| Max account 2 | -10.2% [-25.8, +8.5] | 138 / 9, 94 / 7 | 0.655 |
| Combined | **-9.9% [-22.1, +4.3]** | | |

- Plan-wide: failed. Without Max account 1 it reads -10.2% [-25.8, +8.5]; without Max
  account 2, -9.1% [-29.4, +17.1]. Neither interval excludes no change.
- Interval test: failed. [-22.1, +4.3] includes no change.
- Its log width is 0.292.

Against the 2026-10-08 record (-9.9% [-22.1, +4.1], Max account 2 -10.4% [-25.9, +8.4]),
the steps are the same and the credit valuation moved with the day's refit of the rates.
The combined size did not move at one decimal.

### Which figure is published (P11)

The pure-Opus figure fails condition 1 (thin) and has no interval for conditions 2 and 3.
Rule 20's -9.9% stays, and ADR 0001 rule 21 freezes it there.

### The announced -17%, as a test only

- Rule 20's combined interval [-22.1, +4.3] holds -17%.
- Max account 1's [-29.4, +17.1] and Max account 2's [-25.8, +8.5] each hold it.
- The pure-Opus reading has no interval, so it cannot be tested against -17%.

## The freeze

ADR 0001 rule 21 fixes the 14 September weekly size at -9.9%. It is reopened only for a
counting error shown with evidence. `tracker/invariants.py` check 12
(`fourteen_sep_weekly_frozen`, advisory) fails when the direct weekly change, the
`tokens_per_week_change` or the tokens-per-week chart's step at the event's marker leaves
-9.9% at one decimal. `tests/test_invariants.py` fails when the ADR's figure and
`FROZEN_14SEP_WEEKLY_PCT` differ. On the live page of 2026-10-09 15:02Z all three read -9.9%
(the chart's step -9.900000%), and the check passes.

No publisher code changed. The figure is still recomputed each publish, so a refit can move
it, and check 12 then says so.

## Unresolved

- **The freeze is checked, not enforced.** The publisher recomputes the 14 September size
  every hour, and its credit valuation follows each refit of the rates. Today's refit moved
  Max account 2 from -10.4% to -10.2% without moving the combined -9.9%. A larger refit
  could move it to -9.8% or -10.0%. Check 12 is advisory, so the page would publish the
  moved figure and alert. Pinning the published figure in the publisher, or making the
  check blocking, is a separate decision.
- **A tighter weekly reading needs another selection.** Pure-Opus one-point steps are too
  rare after the cut to read the week on. A reading that avoids per-model rates on mixed
  steps would need a different unit (for example, steps where one family carries at least
  a set share of the tokens), and rule 21 means it could only ever cross-check -9.9%.
