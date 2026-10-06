# The two 14 September weekly routes (wf-149)

Inputs: the committed histories at `7fe662c` ("Daily publisher state 2026-10-06T12:01Z").
`python3 -m tracker.rebuild_offline --now 2026-10-06T12:01:00+00:00` on origin/main reproduces
the live 14 September figures exactly. Nothing was published.

## The contradiction

On the live page, 14 September was stated two ways, for Max accounts 1 and 2:

- **Direct route.** Credits per 1% of the seven-day meter, either side of the change
  (`five_hour_window_credits.direct_tests.weekly_change`): Max account 1 -9.0%, Max
  account 2 -10.4%, pooled -9.9% [-22.3, 4.4].
- **Ratio route.** Windows per week times the window (`tokens_per_week_change`): Max
  account 1 -28.0%, Max account 2 -28.2%, pooled -28.0% [-38.6, -15.4].

On the same readings the two routes are one quantity. Weekly credits per 1% of the seven-day
meter equal credits per 1% of the five-hour meter times five-hour points per seven-day point.

## Attribution

Step 0 builds both routes on one set of readings: the direct test's clean seven-day steps on
its own sides (`cut_weekly_sides`). On those steps, `credits / d7 = (credits / d5) x (d5 / d7)`
holds exactly. Each later step adds back one difference between the two published routes,
cumulatively. Every figure is the ratio route's per-account change. The direct route stays at
-9.0% and -10.4% throughout.

| Step | What is added | Max account 1 | Max account 2 |
|---|---|---|---|
| 0 | Common steps, direct sides, metered credits, pooled sums | -9.0 (wpw -19.2 x window +12.6) | -10.4 (wpw -26.6 x window +22.1) |
| 1 | Headless weight on the window factor only | -12.5 (window +8.2) | -25.7 (window +1.3) |
| 2a | Ratio reads cloud-session steps | -12.5 | -25.7 |
| 2b | Ratio reads harness-run steps | -12.5 | -27.7 |
| 2c | Ratio reads capture-withheld steps | -13.1 | -27.8 |
| 2d | Ratio reads the 2-5 September takeoff phantom | -13.0 | -27.8 |
| 3 | Ratio on five-hour meter windows instead of steps (pinned 100% windows dropped whole) | -17.3 (wpw -23.6) | -30.0 (wpw -30.9) |
| 4a | Ratio before side from its own regime start (15 Aug / 5 Sep) instead of the rule 13 start | -17.3 | -30.0 |
| 4b | Ratio after side to its regime end (5-6 Oct) instead of 22 September | -19.4 (wpw -25.5) | -28.5 (wpw -29.4) |
| 5a | Window from the known-date stretches (Max account 1 from 6 September, split at CUT_AT, across-cut pricing), pooled sums | -26.8 (window -1.7) | -29.1 (window +0.4) |
| 5b | Window estimator: median (published) | **-28.0** (window -3.3) | **-28.0** (window +1.9) |

The published values are -28.0% and -28.2%. Step 5b gives -28.0% for Max account 2 because
it compounds the unrounded factors. Of the 19.0-point gap for Max account 1 and the 17.6-point
gap for Max account 2:

- **Headless weight on one factor (step 1): -3.5 and -15.3 points.** Rule 12 counts headless
  `claude -p` tokens at 1.495x on five-hour figures. The meter ratio beside the window counts
  raw five-hour points, and the headless effect lives in its numerator. Max account 2's
  headless share of credits fell from 46% before the cut to 3% after it, and Max account 1's
  from 12% to 4%. A raw meter ratio times a weighted window is neither unit. It is a weighting
  applied to one side only.
- **Readings the weekly selection excludes (step 2): -0.5 and -2.1 points.** Harness runs are
  headless work, so they move the raw ratio for the same reason.
- **Meter windows against one-point steps (step 3): -4.3 and -2.2 points.** Both kinds of
  reading are legitimate. A window that reaches 100% on the seven-day meter is dropped whole.
  Steps tile only between seven-day crossings. Max account 1 has no steps on 14-17 September
  (it has only the 30-minute log), but dropping those windows moves its ratio by only 0.5
  points.
- **Spans (step 4): +0.0 and +0.0 at the start, -2.1 and +1.5 at the end.** The ratio's after
  side ran across the 22 September change, which is certified on both meters, while the
  window and direct sides end there.
- **Window selection and span (step 5a): -7.4 and -0.6 points.** For Max account 1 the window
  was read from 6 September, but the ratio from 15 August. On its five-hour meter late August
  is a different level from 6-13 September (rule 13), and on the same steps the window and
  ratio move in opposite directions: from 14 August, wpw -19.2 and window +12.6; from 6
  September, wpw -10.6 and window +3.1. The weekly level barely moves (-9.0 against -7.8).
  Multiplying the factors from two different before sides leaves out the half that cancels.
  The across-cut pricing (`across_cut_value`) also drops stretches with a family outside the
  pooled fit (1 before, 6 after on Max account 1). The other change figures price those
  stretches (`comparison_value`).
- **Estimator (step 5b): -1.2 and +1.1 points.** A median of per-stretch ratios is not a ratio
  of sums, so the identity does not hold for it.

## Defects and fix

The ratio route broke the identity in four ways: one unit mismatch (step 1), two selections
the other rule set excludes (steps 2, 4b and 5a), and one estimator mismatch (step 5b). ADR
0001 rule 15 fixes them. `cut_ratio_route` reads both factors on the direct test's sides and
selection, with its valuation, using metered tokens and pooled sums. `tokens_per_week_change`
is now that route. Invariant 10 (`weekly_routes_agree`) checks the two routes against each
other. The windows-per-week ratio note (`windows_per_week_ratio`, the event's `percent`) is
unchanged. It is the detector's raw meter record, and the weekly figure no longer reads it.

| 14 September | Direct (before / after) | Ratio route before | Ratio route after |
|---|---|---|---|
| Max account 1 | -9.0% [-29.3, 17.0] | -28.0% [-37.6, -16.8] | -14.8% [-28.4, 1.5] |
| Max account 2 | -10.4% [-26.3, 8.8] | -28.2% [-40.3, -13.1] | -13.2% [-28.0, 5.0] |
| Pooled | -9.9% [-22.3, 4.4] | -28.0% [-38.6, -15.4] | -14.0% [-28.3, 3.1] |

The gap that remains (-5.8 and -2.8 points) is step 3, the two kinds of reading, and it lies
inside both intervals. 22 September (five-hour +19.8%, weekly +17.5%, `last_change`) does not
move: its candidates read neither the across-cut medians nor the ratio note, so no field
outside the two 14 September events' `tokens_per_week_change` changes. The announced -17% was
used as a test only.
