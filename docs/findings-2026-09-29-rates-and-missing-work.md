# Family rates, missing work and regime boundaries

Issue #126. Written 2026-09-29. It follows docs/findings-2026-09-28-scatter.md (PR #99),
whose Sonnet finding it corrects.

## The question

PR #99 reported that model-family shares explain 30% of the per-stretch scatter, and that
Sonnet work costs the meter about 2.2 times its credit rate. The brief for #126 asked for the
family rates to be fitted from the data, in a way that cannot absorb a limit change, and used
everywhere. Two addenda asked for a change's state to follow its interval, and for the
across-the-cut comparison to stop at the next change.

## Sonnet's rate: the 2.2x was a selection effect

The tracker already fits every family's rate from the data. `tools/model_rates.py`'s pooled
fit estimates the per-family rates and the cache-read weight together, with a free level per
account and period, over the capture-accepted stretches (`credits.rate_fit_stretches`). The
publisher prices every stretch at that fit. On the rebuilt histories it reads Sonnet at 0.399x
Opus (80% interval 0.325 to 0.465). The "NOT MEASURABLE" lines PR #99 cited are a different
section of that tool: ratios from stretches one family dominates.

The 2.2x came from running the regression over every status-accepted stretch, the selection
the change tests use. That selection keeps stretches that the capture check flags because the
meter moved more than their transcripts explain ("unaccounted") or less ("surplus"). The same
pooled fit, split at every change candidate, over three selections of the same stretches:

| stretches in the fit | n | Sonnet x Opus (80%) | Fable | Opus 5.5 | residual sd |
|---|---|---|---|---|---|
| capture-accepted (the rate fit's own) | 207 | 0.369 (0.291 to 0.435) | 2.136 | 0.524 | 0.152 |
| status-accepted less "unaccounted" | 249 | 0.571 (0.480 to 0.675) | 2.318 | 0.815 | 0.201 |
| every status-accepted stretch | 295 | 0.966 (0.767 to 1.227) | 2.732 | 1.153 | 0.295 |

Every non-Opus family gets dearer as the flagged stretches come in, not Sonnet alone. That
is what work missing from Opus-light stretches, rather than a Sonnet price, would do. The
flagged stretches also come in runs, not at random: on Max account 2, 28 of 135 are
unaccounted, with 14 adjacent pairs where 5.8 would be expected by chance; on Max account 1,
8 of 94 with 4 pairs against 0.7. That is bursts of work the transcripts never saw. Adding
the pooled root's unclaimed work (PR #99) does not remove it: on Max accounts 2 and 4 the
Sonnet coefficient moves from 1.05 to 1.17 with it.

So the rates stay fitted on the capture-accepted stretches, and the missing work is handled
where it does harm, in the change tests.

## What changed

1. **The rate fit's levels split at every change candidate** (`tools/model_rates.py`
   `regime_of`, `group_key`). Each account had one level before 14 September and one after.
   It now has one between each pair of neighbouring boundaries: the cut and every change
   candidate (each family's first use). A stretch that crosses a candidate is in no group. A
   limit change at a candidate moves that level, so it cannot be absorbed into a family's
   rate. A synthetic test builds stretches with Sonnet at 2.2x Opus and a +20% step whose
   after side is Sonnet-heavy. The split fit returns 2.2 and the step (1.2 between the two
   levels), and a fit with one level across the step reads Sonnet at 1.63, about a quarter low. On the
   real stretches the split moves Sonnet from 0.399 to 0.369, Haiku from 0.49 to 0.63 (it is
   still not measurable and keeps its inferred rate, marked in the JSON as before), Fable
   from 2.134 to 2.136, and Opus 5.5 from 0.650 to 0.524. The publisher and the across-cut
   comparison already price everything at this fit, so nothing else had to change for the
   rates to be used everywhere.
2. **Missing work is down-weighted in the known-date test and the joint fit**
   (`tracker/credits.py` `huber_location`, `robust_variance`, `log_ratio_side`,
   `_joint_solve`, `_joint_scatter`). Each side's level is now a Huber M-estimate (tuning
   1.345), not a weighted mean. Each stretch's variance is the robust scatter (normal-
   consistent median absolute deviation, less the mean rounding) plus its own rounding
   variance. A stretch more than 1.345 standard deviations from its level counts for less
   the further out it lands. The weighting is symmetric: a real limit rise reads as
   "surplus" on the capture check, so dropping flagged stretches by status would bias the
   change tests. A real step moves every stretch after it together, so the level follows
   the step. Tests (`MissingWorkTests`): a burst of after-side stretches that lost 60% of their work
   moves the known-date test less than half as far as a plain mean does. With four after
   stretches whose meter moved twice what their tokens explain, the joint fit reads +16.7%
   on a +20% step where least squares reads +9.6%. The rounding weights and every known-rate
   and known-step fixture still pass.
3. **A change's state follows its interval** (`credits.change_state`). It is measuring while
   the 95% interval includes no change, provisional once it excludes it, and measured once
   it is also 10 points or less either side of its centre. The stretch counts no longer set
   it, and the notify step's 24 and 48 hour rules only time the email. Tests cover each
   state in each block, the live interval [-10.9, +38.1] (measuring), and a candidate
   published one hour or five days after it happened keeping the same state.
4. **The across-the-cut comparison stops at the next change** (`credits.side_between`,
   `across_cut(changes=...)`). The after side of 14 September used to run to the present,
   through the 22 September change. It now runs to the first `known_date_changes` instant.
   `side_between` bounds any regime's sides by its neighbouring change instants, so a later
   change needs no new code. The publisher now computes `five_hour_on_meters` before
   `across_cut`. A three-regime test checks that the middle regime's figure takes nothing
   from the third.

The PR #99 write-up carries a dated correction of its Sonnet paragraph.

## Before and after

Both builds use the same stretch files: gs's regenerated on 2026-09-28 with main's code (usage
fix and unclaimed work in), and masterrig's regenerated on 2026-09-28 with the same code.
"Before" is main at 9b5b6ba, with `tools.model_rates` run on those files and then
`tracker.publish --out` to a scratch file. "After" is this branch, run the same way. Nothing
was pushed. The 22 September Opus 5.5 candidate:

| | before | after |
|---|---|---|
| joint fit residual sd (plain, after side) | 0.258 | 0.267 |
| joint fit scatter sd (robust, rounding out) | 0.397 | 0.241 |
| r, Opus 5.5 vs Opus 5 | 0.693 [0.418, 0.967] | 0.795 [0.376, 1.044] |
| five-hour limit change | +24.0% [-11.8, +56.1] | +29.8% [-19.9, +65.1] |
| weekly limit change | +23.8% [-11.4, +73.0] | +29.6% [-13.3, +93.6] |
| state | measured (count rule) | measuring |
| known-date test | +28.0% [10.4, 48.4] | +40.2% [27.1, 54.7], provisional |

The residual sd is the plain standard deviation of the after side's residuals, kept so the
column stays comparable. It barely moves, because the stretches with missing work are still
in it; the robust scatter the fit now weights by drops by 40%. The known-date test, which
prices Opus 5.5 at a fixed rate, narrows from a half-width of 19 points to 14.

The joint fit's intervals do not narrow; they widen a little. Its width is not per-stretch
scatter but how well the mix separates Opus 5.5's rate from the limit change. With r held at
its point estimate, the bootstrap gives the five-hour change an interval of [+9.9, +33.1]
(plain) or [+17.8, +45.5] (robust); with r free, [-17.7, +63.6] or [-19.3, +68.9]. Only
stretches whose Opus 5.5 share varies more, or an independent reading of Opus 5.5's rate,
will narrow it.

Levels for the three periods, with their intervals as published:

| period | window credits, before | after | tokens per week, before | after | windows per week (both) |
|---|---|---|---|---|---|
| to 14 Sep | 19,543,887 [8,600,327, 20,819,693] | same | 2,921,785,377 | same | 6.484 [6.224, 6.764] |
| 14 to 22 Sep | 21,439,644 [9,434,558, 22,839,204] | 21,341,924 [9,391,557, 22,735,105] | 2,488,758,114 | 2,477,414,640 | 5.035 [4.762, 5.335] |
| from 22 Sep | 26,585,158 [8,321,280, 35,651,997] | 27,701,818 [7,522,637, 37,535,659] | 3,081,082,545 | 3,210,481,632 | 5.027 [3.223, 8.425] |

Window tokens (the Opus-anchored window) move the same way as window credits: 494,301,399 to
492,048,430 for 14 to 22 September and 612,933,735 to 638,678,862 from 22 September. Windows
per week is read off the meters alone and does not move.

Across the cut, with the after side stopped at 22 September: Max account 1 goes from +4.8% (31
stretches after) to +1.5% (26), Max account 2 from +14.6% (41) to +17.0% (29), and the 14
September window marker (their median) from +9.7% to +9.25%, published as +9.2%. Isolated
on main's rates, the bound alone takes Max account 1 from +4.8% to +2.1% and Max account 2
from +14.6% to +14.9%. The #127 findings' +19.0% to +13.5% and +6.4% to +2.9% came from PR
#100's history files and were not reproduced on these.

A five-hour interval of plus or minus 10% from the joint fit, at its current width and
scaling as one over the square root of the stretches after, needs about 432 stretches after
the change: about 87 days at the current rate, against 54 days before. The known-date
test is already within 14 points of that and its interval excludes no change.

## What is left

The joint fit is limited by separating Opus 5.5's rate from the limit change. More mixed
stretches will narrow it slowly; an independent reading of Opus 5.5's rate would narrow it
at once. The unseen work behind the flagged stretches is now down-weighted, not explained;
where it comes from is still open.
