# Max account 1's before side for the 14 September weekly change (2026-10-05)

wf-145. The question: can Max account 1's before side for the 14 September change reach
further back than 6 September, and over which days is it honestly the same level?

The figures come from offline rebuilds over the inputs committed at 679bdb9, with `now` set
to 2026-10-05 18:01Z. Nothing is published.

## Rules, written down before any 14 September figure was recomputed

The announcements are a test of the result, never an input to it. These rules were fixed
from the meter, the plan history and the capture data alone. Only after that was any
14 September change computed with a wider side.

1. **Where the before side starts.** It starts at the start of the regime that the
   account's own windows-per-week detector places just before the account's own 14 September
   step. That is `weekly_windows.max20.by_account.<label>.regimes[-2].start`: the publisher's
   detector (`detect.weighted_regimes`, series `WINDOWS`, threshold 0.15) on the account's own
   points, with no new tuning. The before side runs to the end of that regime, as it already
   does (`own_weekly_step_end`). Without an own step, the before side keeps today's bounds.
   Max account 1's points enter that series only from `PLAN_CHANGE_AT`, 14 August 17:00Z, when
   the account moved from Max 5x to Max 20x. So no Max 5x window can be part of a Max 20x
   level. For step 1, the detector is also run over the whole series from 13 June, Max 5x
   included, so that every boundary is reported.

2. **A check on the seven-day meter itself.** Any days the extension adds must read the same
   level on the seven-day meter as the days already used. The test is the account against
   itself (`weekly_meter.account_change`): the added days against 6-13 September. If that
   test's 95% interval excludes no change, the extension is not used. This is the
   publisher's own change test, with no new threshold.

3. **Which steps and stretches are clean.** The rules are exactly the ones the publisher
   already applies to every account:
   - cloud-session work is out;
   - harness runs are out;
   - a step overlapping a stretch whose `status` is not `accepted` is out. That is the
     collection-gap gate: capture under 0.10, or an unpriced token.

   One rule applies to Max account 1 alone. The 2-5 September phantom, whole UTC days
   [2 Sep 00:00Z, 6 Sep 00:00Z), is left out because takeoff ran on the account then. This is
   the reason `MASTERRIG_FROM` exists, written as the span it is rather than as a start date.

   **The capture rule for Max account 1 is the `status` gate that every account takes, with
   no stricter `capture_status` gate.** This was justified on the capture data alone, before
   14 September was looked at, by stretch start date:

   | span | stretches | median capture | capture_status unaccounted | status not accepted |
   |---|---|---|---|---|
   | Max account 1, 15 Aug - 1 Sep | 77 | 0.936 | 24 (31%) | 9 |
   | Max account 1, 2-5 Sep (phantom) | 87 | 0.192 | 70 (80%) | 34 |
   | Max account 1, 6-13 Sep (used today) | 41 | 1.006 | 4 (10%) | 0 |
   | Max account 2, 6-13 Sep (used today) | 66 | 0.746 | 33 (50%) | 6 |

   Max account 1's 15 August to 1 September stretches capture as well as Max account 2's
   before side, which the publisher already admits under the `status` gate. A stricter gate
   for Max account 1 alone would be a special case. A stricter gate for every account would
   drop half of Max account 2's before side. The 9 stretches that the `status` gate removes
   between 15 August and 1 September are 18-19 August: 8 stretches holding no tokens, from
   before the account's transcripts survive on disk (cleaned up), plus the 16 July to
   18 August stretch. Every one of them reads capture 0.0. So the gate already removes the
   period with no transcripts, and no date rule is needed for it. The phantom is different.
   Its capture data flags it (median 0.192), but 53 of its 87 stretches pass the `status`
   gate, so it needs its own rule.

4. **The five-hour (weighted) change** takes the same before-side start (rule 1) and the same
   phantom span (rule 3) on Max account 1's stretches. Otherwise it is selected exactly as
   today (`announced_change_stretches`).

**Amendment to rule 1, made before any 14 September figure was computed.** Rule 1's wording
treats a regime's start as a boundary. That is wrong for an account's first regime, because
its start is only where the account's points begin. Max account 2's seven-day steps start on
5 September at 06:19Z, but its first windows-per-week point is at 10:57Z. Read literally,
rule 1 would cut those hours from Max account 2 for a reason that has nothing to do with its
level. The rule as implemented is therefore:

- The before side starts at the latest certified boundary before the change.
- Here that is the later of two instants:
  - the start of the account's own regime before its step, when a certified step precedes
    that regime;
  - `PLAN_CHANGE_AT`, the start of the Max 20x series for every account.
- For Max account 1 that is `PLAN_CHANGE_AT`. Step 1 below shows the detector certifying a
  boundary at that instant, and none after it.
- For Max account 2 it bounds nothing, because its log starts in September.

5. **Scope.** `MASTERRIG_FROM` keeps its job in the per-model rate fits and in the
   change candidates after 14 September. This change moves only the 14 September before side.

## Step 1: where Max account 1's level changed, 13 June to 14 September

The publisher's weekly detector was run unchanged (`detect.detect_weighted_changes`, series
`WINDOWS`, threshold 0.15) over Max account 1's own windows-per-week points from
`history/passive.json`. The points are the repaired, reset-verified ones, from 13 June:

| series | certified boundaries | regimes (windows per week, rounding interval, points) |
|---|---|---|
| 13 Jun - 5 Oct, Max 5x included | 14 Aug, -40%; 14 Sep, -26% | 13 Jun - 14 Aug 10.86 [10.53, 11.22], 204; 14 Aug - 13 Sep 6.51 [6.19, 6.86], 109; 14 Sep - 5 Oct 4.83 [4.42, 5.31], 63 |
| 13 Jun - 14 Sep | 14 Aug, -40% | 10.86; 6.50 [6.18, 6.84], 110 |
| Max 20x era, as published | 14 Sep, -26% | 15 Aug - 13 Sep 6.50 [6.17, 6.86], 108; 4.83 |
| Max 5x era alone | none | 10.86, 204 |

The one boundary between 13 June and 14 September is 14 August. That is Max account 1's
own move from Max 5x to Max 20x: 10.86 / 6.51 = 1.67, the plan ratio in
`WEEKLY_WINDOW_RATIOS`. It is not a change by Anthropic. On the windows-per-week series there
is no boundary inside the Max 20x era before 14 September, and none inside the Max 5x era.
So no boost start shows up in this history. The pre-14 September level is one level from
14 August (rule 1's boundary), and as far back as 13 June on the Max 5x plan.

A boost that raised both limits by the same factor would not show on windows per week. So
the level was also read directly on the seven-day meter, in credits per 1% between exact
crossings (the #121 method). Max account 1's seven-day steps start on 18 August. Its steps
on 18-19 August hold no tokens and fail the `status` gate, so its clean steps start on
20 August. Applied to those 155 clean steps (20 August to 13 September, phantom out), the
publisher's credit detector (`detect.detect_credit_changes`) certifies no boundary. Its
error model was calibrated on five-hour stretches, so its intervals here are wide
([0.81M, 3.11M]), and this is weak evidence either way. Weekly levels by ISO week, in credits
per seven-day point:

| ISO week | credits per point | seven-day points | days |
|---|---|---|---|
| 34 (20-23 Aug) | 1,287,536 | 19 | 3 |
| 35 (24-30 Aug) | 1,340,194 | 64 | 6 |
| 36 (31 Aug - 1 Sep; 2-5 Sep out) | 1,147,072 | 18 | 3 |
| 37 (6-13 Sep) | 1,272,152 | 54 | 7 |

**Rule 2's check passed.** 6-13 September against 20 August to 1 September on the seven-day
meter (`account_change`) reads a ratio of 0.932 [0.708, 1.228]. That is 1,236,417 against
1,326,581 credits per point, 8 days against 11, 67 steps against 88. The interval includes no
change, so the extension is used.

## Steps 2 and 3: the 14 September change, main against this branch

Offline rebuilds of the same committed inputs (679bdb9, `now` 2026-10-05 18:01Z), on main
and on this branch. Nothing is published.

**Weekly limit, seven-day meter, direct (`direct_tests.weekly_change`):**

| | main | branch |
|---|---|---|
| pooled | -8.2% [-20.1, +5.5], not certified | **-9.9% [-22.3, +4.4], not certified** |
| Max account 1 | -5.3% [-24.2, +18.4], n 67/94, 8/5 days | -9.0% [-29.3, +17.0], n 155/94, 19/5 days |
| Max account 1's before level | 1,236,417 | 1,287,607 |
| Max account 2 (unchanged) | -10.4% [-26.2, +8.8], n 138/92, 9/7 days | the same |
| weights, accounts 1 / 2 | 0.44 / 0.56 | 0.36 / 0.64 |
| plan-wide | failed | failed (without account 2 it reads -9.0% [-29.3, 17]) |

Max account 1's interval widens, from se 0.102 to 0.122, even with 11 more days. Its before
days now vary more between themselves. With only 5 after days, the after side was always
the binding constraint.

**Five-hour window, weighted (`direct_tests.window_change`):** unchanged from main, at
+3.1% [-2.0, 8.5], not certified (Max account 1 +3.3% [-3.8, 11.0], n 37/45; Max account 2
+2.9% [-4.3, 10.7], n 78/41).

The branch was first built with the wider before side on the five-hour test too, as rule 4
said. That build read pooled +7.2% [1.1, 13.6], not plan-wide, so not certified. Max account
1 alone read +15.3% [4.6, 27.2], n 104/45. The size of that move led to the same-level check
being run on the five-hour meter. Rule 2 had named only the seven-day meter, so this check
was added after the 14 September figures were seen. On Max account 1's five-hour meter, 6-13
September reads +18.9% [5.8, 33.6] against 20 August to 1 September. That interval excludes
no change, so on that meter the extension is not the same level, and the five-hour test keeps
today's selection. The decision does not touch the weekly figure that the -17% announcement
tests, and the five-hour change is uncertified either way.

The likely cause is unverified. Max account 1's headless share of raw tokens was 24% from
20 August to 1 September and 11% from 6 to 13 September. On the same seven-day steps, its
five-hour points per seven-day point were 6.68 against 5.63. The headless weight (ADR 0001
rule 12) is one pooled factor. If Max account 1's own factor is larger, the weighting would
undercorrect its headless-heavy August.

**Unchanged by the branch:** every other figure in the rebuilt JSON. That includes 22
September (+19.7% [10.9, 29.2] on the window and +17.5% [5.8, 30.4] on the weekly limit,
certified on both, the `last_change`), 29 September, the window (34,787,494 credits),
windows per week (5.13) and tokens per week. The invariants read the same on both builds:
seven pass, and the two warnings `account_units` (Max account 3's window against its direct
reading) and `steps_agree_with_meters` (22 September against the windows-per-week ratio)
are present on main already.

## Against the announcements, as a test

- 14 September, "a 17% reduction": -17% lies inside the branch's pooled interval
  [-22.3, +4.4] and inside each account's own interval. So does no change. The point moves
  from -8.2% to -9.9%, toward -17%, but the change is still not certified. It is not
  plan-wide, and its interval includes zero.
- 22 September, +20% on the five-hour limit: unchanged at +19.7% [10.9, 29.2].

## Step 4: a boundary the page does not show

None that Anthropic made. The one boundary between 13 June and 14 September is 14 August,
Max account 1's own plan move. The tracker already handles it (`PLAN_CHANGE_AT`) and
publishes nothing about it. No boost start shows on windows per week, from 13 June on.
Max account 1's seven-day level shows none from 20 August, the first day it can be read.
The publisher's own cross-check (`window_credits_from_weekly.method`) dates the x1.5
multiplier "from May", which is before every reading here.

One thing on a single account is worth knowing. On Max account 1's five-hour meter, credits
per 1% rose +18.9% [5.8, 33.6] from late August to 6-13 September. Its seven-day level shows
no such move. The account's windows-per-week detector certifies no boundary there either,
because it pools every window. A shift that lives on one account and one meter, alongside a
halving of headless share, reads as a usage-mix effect and not as a limit change.

## So

The boost did not start inside Max account 1's history. Its level is one level from
14 August, the start of its Max 20x plan. On the seven-day meter, 11 more clean days
(20 August to 1 September, 88 steps) read the same level and are now used. The 14 September
weekly change reads -9.9% [-22.3, +4.4] and stays uncertified: it is not plan-wide, and its
interval includes no change. The after side (5 days on Max account 1) is now the limit on
precision, and that only improves if Max account 1's after side grows. It cannot grow,
because 22 September ends it.
