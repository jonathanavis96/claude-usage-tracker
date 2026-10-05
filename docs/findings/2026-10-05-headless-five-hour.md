# Headless work on the five-hour meter: no counting error found; one figure per change (2026-10-05)

wf-143. Part 1 looks for a collector counting error behind the headless effect in
docs/findings/2026-10-04-account-agreement.md section 4. Part 2 makes the page's change
figures agree with each other. Part 1 numbers come from `python3 -m tools.headless_hypotheses`
over the gs transcripts cached on 2026-10-04 19:53Z and the gs meter logs up to that time.
That is 306 tiled one-point seven-day steps from 14 September 12:00Z: Max account 2 has 137,
Max account 3 has 128 and Max account 4 has 41. Max account 1's transcripts are not on gs.
Part 2 numbers come from `tracker.rebuild_offline` on the inputs of the 2026-10-05 15:01Z
publish, built with main and with this branch.

## Part 1: what each hypothesis does to the headless factor

The headless factor is how much more the five-hour meter moves per seven-day point, per unit
of headless share of a step's credits (quasi-Poisson, with regime and account terms). It is
**1.53x [1.41, 1.67]** at the collector's pricing.

One fact frames every hypothesis. The quantity that carries the effect is five-hour points per
seven-day point, and that is a ratio of two meter readings. No collector price enters it. The
transcripts only label which steps were headless. Repricing a token class moves credits per
1% of both meters together, so it cannot remove a difference between them. A counting error
could only show here by mislabelling steps or by moving work between steps.

**a. Cache-write TTL: ruled out.** Headless and interactive main sessions both write one-hour
cache only. One-hour writes are 100% of their writes on every account, and sub-agents write
five-minute cache only. The one-hour share of credits barely differs between the two
sources: Max account 3 has 0.381 headless against 0.384 interactive. Max account 2 has 0.531
against 0.379, and Max account 4 0.613 against 0.424. Max account 3 still shows the effect,
1.49x [1.33, 1.67] on its own, at identical write shares. The collector prices both TTLs as
input (`credits.input_side`), but that cannot matter here. Under each write pricing the
headless factor is:

| pricing | headless factor | credits per 1% seven-day, headless share under 0.2 / over 0.8 | credits per 1% five-hour, same |
|---|---|---|---|
| collector (writes 1x, 1h no premium) | 1.53 [1.41, 1.67] | 960k / 1,124k | 217k / 159k |
| API (5m 1.25x, 1h 2x, read 0.1x) | 1.52 [1.40, 1.66] | 3,446k / 3,583k | 775k / 507k |
| collector, 1h at 1.6x | 1.54 [1.41, 1.68] | 1,033k / 1,386k | 234k / 195k |
| collector, 1h at 2x | 1.54 [1.41, 1.68] | 1,080k / 1,560k | 244k / 219k |

A one-hour premium brings the five-hour meter's credits per 1% closer across headless shares.
It pushes the seven-day meter's apart by the same amount (1,124k to 1,560k against 960k to
1,080k). The seven-day meter is the one the accounts already agree on.

**b. Per-run overhead: ruled out.**

- The 58 steps over 0.8 headless hold 10.6 runs each, at 98.4k credits per run.
- Each run's first turn is in the transcripts, and its cache write is 11.8% of headless
  credits.
- `dave-delegate` stream totals match their transcripts (2026-10-04 findings, section 2b).
- Adding runs per million credits leaves the headless factor at 1.45x [1.32, 1.60]. The runs
  term is 1.00x [1.00, 1.01] per run per million credits.
- To explain 1.5x, each run would need about 50k credits the transcripts do not hold. The
  seven-day meter would see that spend too. Instead it reads headless steps at 1.16x the
  credits per point, which runs the other way.

**c. Timing: ruled out.** The table re-bins each step's turns with an offset. The factor is
highest with no shift and smaller with any shift, so the turns are already in the right
steps.

| shift | -30 min | -10 min | 0 | +10 min | +30 min |
|---|---|---|---|---|---|
| headless factor | 1.43 | 1.51 | 1.53 | 1.44 | 1.42 |

**d. Other variables: none takes the effect.**

- Weekday 12:00-18:00Z (US morning) share: 1.19x [1.11, 1.27] on its own. With the headless
  share beside it, it is 1.11x, and headless stays at 1.48x [1.36, 1.62].
- Turns, sessions per million credits and step length leave headless at 1.51x to 1.56x.
- Per account: Max account 2 2.22x [1.57, 3.14] (mean headless share 0.05), Max account 3
  1.49x [1.33, 1.67] (0.45), Max account 4 1.52x [0.92, 2.49] (0.22).

**Result:** no counting error in the collector explains the headless effect. Nothing in the
pricing or the collector changes here, as the brief requires. So the 22 September five-hour
change is the same before and after this branch:

- pooled: +31.8% [20.7, 43.8];
- Max account 1 +53.8%, Max account 2 +17.4%, Max account 3 +3.4%;
- without Max account 1 it reads +11.1% [-2.2, 26.2], so it is still withheld under rule 9.

## Part 2: one figure per change

On the live JSON of 2026-10-05 15:01Z the page said three different things about 22 September:

- the headline said weekly +30% (`last_change.change_pct` 29.6, certified on the weekly
  limit);
- the tokens-per-week chart stepped +32%;
- the windows-per-week chart stepped +32%;
- the window chart drew no step.

Two causes:

1. **Tokens per week.** The chart draws `per_week_regimes` levels, each pooled over whichever
   accounts had seven-day steps in it. Max account 4 starts on 23 September, so the after
   level has a different mix from the before level. The headline is the certified change:
   each account against itself, combined by inverse variance. Fix (`chain_certified_weeks`):
   every row before a boundary certified on the weekly limit is scaled so the step there is
   the certified change. This is the same move the window's history makes (`bridge_rate`).
   The newest row stays its own direct level, so the hero figures do not move. `week_bridge`
   records the factor (1.0184) and each row's direct value.
2. **Windows per week.** At 22 September the window change was measured and withheld. The
   window is carried across that boundary, which does not show it unchanged. Dividing the new
   week by the carried window drew the whole weekly change (+32%) as a windows-per-week step.
   The meter ratio in the same seven-day steps is flat (4.97 to 4.96, `windows_per_week_meters`).
   Fix (`carry_across_withheld_windows`): the row after such a boundary carries the windows
   per week of the row before. The pooled row and each account's row are both covered.

| | main | branch |
|---|---|---|
| headline | weekly +30% on 22 Sep | weekly +30% on 22 Sep |
| window chart, 14 / 22 Sep | +11% / none | +11% / none |
| tokens-per-week chart, 14 / 22 Sep | -11% / +32% | -11% / +30% |
| windows-per-week chart, 14 / 22 Sep | -20% / +32% | -20% / none |
| windows per week now | 6.0 | 4.6 |
| Opus 5.5 window (headline) | 621M | 621M |
| Opus 5.5 tokens per week | 3,727M | 3,727M |
| account windows-per-week lines at 22 Sep (Max account 1 / 2 / 3) | +47% / +23% / +36% | none |

At 14 September the windows-per-week step is the one implied by the other two:
0.885 / 1.112 - 1 = -20.4%.

Three new invariants (`tracker/invariants.py` 7-9) fail the publish when the page states two
figures for one change. On the live JSON all three fail; on the branch build all nine pass.

## Against the announcements (a test, not an input)

- **22 September, five-hour +20% announced: still missed.** The pooled measurement is +31.8%
  [20.7, 43.8], which holds +20%, but it fails the plan-wide test. The page draws no
  five-hour step there. Meanwhile the weekly limit is certified at +29.6% [15.6, 45.4], and
  the meter ratio says windows per week did not move. Together these point to both limits
  rising by about the same amount. That is not what was announced for the weekly limit.
- **14 September, weekly -17% announced: still missed.** The tokens-per-week chart steps -11%
  (pooled levels, -11.5%). The direct weekly test reads -5.6% [-19.9, 11.1] and is not
  certified. Its interval holds -17%, but it also holds no change.
