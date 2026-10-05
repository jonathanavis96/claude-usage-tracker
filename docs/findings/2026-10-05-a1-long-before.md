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

5. **Scope.** `MASTERRIG_FROM` keeps its job in the per-model rate fits and in the
   change candidates after 14 September. This change moves only the 14 September before side.
