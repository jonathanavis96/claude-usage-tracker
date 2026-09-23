# masterrig admitted to the rate fits from 6 September, 2026-09-23

masterrig, Jonathan's personal account, has been kept out of the pooled rate fits since they were
first made. `FIT_ACCOUNTS` in `tools/model_rates.py` was `("jwork", "dave")`, and the reason on
record was that the account's meter also counts claude.ai web and phone use that its transcripts
never see. That reason is wrong. Jonathan uses that account only for Claude Code on masterrig. The
large residuals that seemed to show off-host use came from something else: from 2 to 5 September
the takeoff pipeline on gs ran on the personal account, so the meter moved on work that
masterrig's transcripts never held. It was moved off on 5 September
(`docs/findings-2026-09-20-masterrig-stretches.md`).

This change admits masterrig from `2026-09-06T00:00:00Z`, under the same dominance rule as the
other two accounts. It also fixes the capture bootstrap, which had left every masterrig stretch
with `capture: null`. No traffic was sent. Every figure below comes from committed history files.


## Update: masterrig rebuilt and capture-checked (same day)

`history/masterrig-passive.json` was rebuilt on masterrig with this branch's capture fix
(`python3 -m tracker.gs_passive --masterrig --out history/masterrig-passive.json`). From 6
September every masterrig stretch now has a capture: 68 accepted, 15 surplus, 7 unaccounted.
`clean()` no longer exempts masterrig, so it is judged exactly like jwork and dave. Refitted
with the same command as below.

| | masterrig exempt (first version of this change) | masterrig capture-checked (as merged) |
|---|---|---|
| masterrig/pre | n 38, residual median 0.059 | n 36, residual median 0.060, p90 0.116 |
| masterrig/post | n 49, residual median 0.169, p90 0.407 | n 30, residual median 0.054, p90 0.165 |
| masterrig/post Sonnet | 0.884x Opus, qualifies (4 dominant) | 0.448x Opus, not pooled (2 dominant) |
| pooled Sonnet | 0.740x Opus [0.485, 1.533] | 0.595x Opus [0.485, 0.672], dave/post only, unchanged from main |
| pooled Fable | 2.463x Opus [1.820, 4.751], 3 fits | 2.532x Opus [1.798, 4.751], jwork/post and masterrig/pre |

The capture test removes the stretches that made masterrig/post the worst fit. What is left
fits as cleanly as jwork. Sonnet does not move, and the objections in "What argues against
admitting masterrig" below, all of which concern the uncapture-checked post-cut group, no longer
apply to the published figures. They are kept as the record of why the exemption was dropped.

Fable is pooled from two fits that disagree: jwork/post reads 3.149x [2.485, 4.751] and
masterrig/pre reads 1.916x [1.798, 2.095]. One is after the 14 September cut and one before it,
so the disagreement is the same open question as before: whether Fable's rate moved or the
five-hour window did. masterrig/post, capture-checked, reads 1.956x [1.710, 2.192] on 2
Fable-dominant stretches, one short of pooling.

The rebuild also fills masterrig's capture column in the publisher's five-hour window
comparison (`five_hour_window_credits.per_account`, n_with_capture 0 to 228). masterrig's
credits per 1% rise 17.3% across the cut and jwork's fall 6.0%, so the median five-hour change
moves from -6.0% (jwork alone) to +5.7%. The tokens-per-week change the page states moves with it,
from -26.8% to -17.7%. All 10,420 published figures reproduce (`tools/credits_report.py
--publish-check`).

## What the data says about masterrig

From the committed `history/masterrig-passive.json` (built on masterrig at 01:15Z today):

- 28 of masterrig's stretches moved the meter while their transcripts held nothing. Nineteen of
  them fall between 2 and 5 September (UTC start dates: 2 on the 2nd, 4 on the 3rd, 13 on the
  5th). The other nine are in July and August, when the transcripts had already been cleaned up.
- From the cut on, none of its stretches is zero-token. There are 83 of them by UTC start time,
  38 before 14 September and 45 after. The brief's 86 counts by local date: three stretches that
  start in the first hours of 6 September at +02:00 began on the 5th in UTC. All three spent
  something, and the UTC cut leaves them out.
- The brief's count of about 30 Fable-heavy stretches (24 and 6) included cache reads in the
  denominator. By the tool's own definition, raw tokens are input, cache writes and output, with
  cache reads at weight zero. On that definition 10 stretches before 14 September and 5 after
  carry 60% or more on Fable. That is 15, where jwork/post has 3. Both masterrig fits therefore
  qualify for Fable under `DOMINANCE_MIN_N = 3`. With cache reads counted, the recount is 23 and
  6.

## 1. The capture bootstrap (`tracker/capture.py`)

An account's capture reference used to be bootstrapped from the median of its first five priced
stretches. masterrig's first eight are zero-token (cleaned-up transcripts), so the median was
zero and nothing after it could be divided. Every masterrig stretch had `capture: null`, and every
withheld run was `unknown`.

Now the bootstrap batch runs until it holds five stretches that spent something, and only their
rates set the reference. The zero ones in the batch are still judged against the result. They
read as `unaccounted` with a capture of 0, and together they form a `collection_gap` run. That is
what they are. An account without five stretches that spent anything stays `unjudged`, as before.

On gs nothing changes. `python3 -m tracker.gs_passive --until 2026-09-19T00:00:00+00:00` was
built from the unchanged code and from the new code, and the two outputs were diffed with
`generated_at` removed. They differ only in jwork's transcript inventory counts (`files`, `kept`,
`dropped`, one or two files each). Those counts come from live transcript directories that grew
between the two runs; a second pair of runs a minute later differed again by one file. No
stretch, verdict, capture, run or rate differs.

## 2. The fits (`tools/model_rates.py`)

`MASTERRIG_FROM = 2026-09-06T00:00:00+00:00` is applied in `clean()`, so no masterrig stretch
before it enters any section. `FIT_ACCOUNTS` is now `("jwork", "dave", "masterrig")`. The
14 September era split is unchanged, so masterrig contributes `masterrig/pre` (6 to 14 September)
and `masterrig/post`. The 60% dominance rule applies to it exactly as it does to the others.
masterrig takes the same capture test as jwork and dave. See the update below: the exemption
this section first kept was dropped once masterrig's file was rebuilt with the capture fix.

Refitted exactly as `docs/findings-2026-09-23-sonnet-rate.md` shows, except that the masterrig
file is the committed 01:15Z one and was not rebuilt:

```
python3 -m tools.model_rates history/masterrig-passive.json --json history/model-rates.json
```

### Per fit

Joint fit (cache-read weight fitted), output at 5x, credits per input token, Opus fixed at 0.6667.
"Dominant" is the number of stretches carrying 60% or more of their raw tokens on the family. A
fit is pooled for a family only with 3 or more. A bold figure is a pooled one.

| fit | n | dominant O / S / F / H | Sonnet | Fable | Haiku | cache-read weight | residual median | residual p90 |
|---|---|---|---|---|---|---|---|---|
| jwork/pre | 51 | 37 / 1 / 2 / 0 | 0.540 [0.339, 0.605] | 1.401 [1.313, 1.524] | 1.212 [0.000, 4.367] | 0.0114 | 0.050 | 0.132 |
| jwork/post | 26 | 10 / 1 / 3 / 0 | 0.353 [0.145, 0.765] | **2.099 [1.657, 3.167]** | 3.155 [0.625, 13.841] | 0.0209 | 0.059 | 0.207 |
| dave/post | 32 | 13 / 9 / 0 / 0 | **0.397 [0.323, 0.448]** | 1.059 [0.838, 1.378] | 0.000 [0.000, 0.588] | 0.0000 | 0.111 | 0.265 |
| masterrig/pre | 38 | 8 / 0 / 10 / 0 | 0.000 [0.000, 0.149] | **1.277 [1.214, 1.398]** | 1.411 [0.000, 4.339] | 0.0077 | 0.059 | 0.137 |
| masterrig/post | 45 | 9 / 4 / 5 / 0 | **0.766 [0.458, 1.241]** | **1.719 [1.317, 2.422]** | 1.506 [0.000, 3.523] | 0.0000 | 0.169 | 0.407 |

No fit has a Haiku-dominant stretch, so Haiku is still withheld. The Opus 5.5 column is still
empty everywhere.

### Pooled, before and after

"Before" is the `history/model-rates.json` committed on main (12:26Z today). "After" is this
refit. The pooled value is the median of the qualifying fits, and the interval is the union of
their intervals.

| family | before | after | reference |
|---|---|---|---|
| opus | 0.6667, the anchor (3 fits) | 0.6667, the anchor (5 fits) | 0.6667 |
| sonnet | 0.3967, 0.595x Opus, [0.323, 0.448]; 1 fit (dave/post) | 0.5815, 0.872x Opus, [0.323, 1.241]; 2 fits (dave/post, masterrig/post), `agree: false` | 0.4000, 0.600x |
| fable | 2.0991, 3.149x Opus, [1.657, 3.167]; 1 fit (jwork/post) | 1.7194, 2.579x Opus, [1.214, 3.167]; 3 fits (jwork/post, masterrig/pre, masterrig/post), `agree: false` | absent |
| haiku | not measurable, no fit has 3 stretches 60% or more Haiku | unchanged | 0.1333, 0.200x |
| opus-5-5 | not measurable, no clean stretch is Opus 5.5-heavy | unchanged | absent |
| cache-read weight | no value (fits disagree), [0.0000, 0.0473], 3 fits | no value (fits disagree), [0.0000, 0.0473], 5 fits | 0, range [0, 0.015] |

`tools/credits_report.py --publish-check` reproduces all 10,391 figures of a page built with the
new file (`python3 -m tracker.publish` with the command line from the Sonnet-rate note).

## What moved and why

Sonnet rose from 0.595x to 0.872x Opus. Before this change only dave/post had enough
Sonnet-heavy stretches to pool. masterrig/post has 4, so it qualifies, and its coefficient is
1.150x Opus. The pooled figure is the median of two fits whose intervals do not overlap:
dave/post [0.323, 0.448] and masterrig/post [0.458, 1.241]. So the median is simply the midpoint
of the two.

Fable fell from 3.149x to 2.579x Opus. jwork/post (3.149x) is no longer its only fit. Both
masterrig fits qualify: masterrig/pre at 1.915x and masterrig/post at 2.579x. The median is now
masterrig/post's value. The interval's upper end is still jwork/post's. Its lower end falls to
masterrig/pre's 1.214.

masterrig repeats jwork's step across 14 September. Its Fable coefficient rises from 1.277 to
1.719, and jwork's rises from 1.401 (not pooled, 2 dominant stretches) to 2.099. A second account
now shows the same direction. The data still cannot say whether Fable's rate moved or the
five-hour window did.

Nothing else moved. Haiku and Opus 5.5 stay withheld. The cache-read weight gains two points
(masterrig/pre 0.0077, masterrig/post 0.0000), and its union interval does not change.

## What depends on masterrig rebuilding its stretch file

The committed `history/masterrig-passive.json` was built before the capture fix and before
PR #74 priced the older models. That is why 69 of its 83 stretches from 6 September have
`capture_status: unpriced` and the other 14 have `surplus` against a zero reference. The rebuild
runs on masterrig after this merges, not here. When it does:

- The capture column fills for masterrig's stretches.
- No figure in `history/model-rates.json` changes because of that alone. masterrig is still
  exempt from the capture test in `clean()`, so the same 83 stretches enter the fits whatever
  their capture says. Every masterrig figure above (both fits, and through them the pooled Sonnet
  and Fable rates and intervals, and the cache-read weight's points) comes from stretches that
  have not been capture-checked.
- On the page, `five_hour_window_credits.per_account` has one pseudonymous account with
  stretches on both sides of the cut and `n_with_capture: 0`, which is masterrig's shape. Its
  count, and anything else the publisher reads from masterrig's capture column, will change.

Dropping masterrig's capture exemption after the rebuild, so that it is judged like jwork and
dave, is the natural next step. It is not part of this change.

## The gmail-monitor leak

`~/.claude-gmail-monitor` on gs is signed into the same personal account. It spends about 45k
Haiku cache-write tokens a day, which is about 0.03% of a five-hour window. masterrig's meter
counts that spend, but masterrig's transcripts do not hold it. Spread over a day's stretches this
is far below the whole-percent floor of any single stretch, and far below the residuals in the
table above. It only adds Haiku, which no fit prices. It remains a known leak and is not
corrected for.

## What argues against admitting masterrig

These points come from the same data, and they are real.

**masterrig/post fits worse than any other group.** Its residual median is 0.169, against
0.050 and 0.059 on jwork and 0.111 on dave. Its p90 is 0.407, against 0.132 to 0.265 on the
others. masterrig/pre is as clean as jwork (0.059 median, 0.137 p90). So the takeoff-period
explanation accounts for the old 0.447 residual, but it does not account for the post-cut group.
Something about masterrig after 14 September is noisier than anything on gs. The acceptance rule
that would catch capture-poor stretches is not applied to it.

**Its Sonnet coefficient is the kind the capture rule exists to remove.** The Sonnet-rate note
found that dropping the capture rule raises Sonnet on every gs group: dave went from 0.595x to
0.809x, and jwork after the cut went from 0.529x to 1.600x. masterrig/post's 1.150x, from
uncapture-checked stretches, is in that direction and of that size.

**Its own Sonnet-heavy stretches disagree with its fitted Sonnet rate.** Read directly, with no
fit, masterrig/post's 4 Sonnet-heavy stretches carry a median of 271,425 input-equivalent tokens
per 1%, and its 24 Opus/Fable-heavy stretches carry 176,565. That puts Sonnet at 0.65x Opus. This
agrees with dave's 0.70x and the reference table's 0.60x, not with the fit's 1.15x. The fit's
`opus:sonnet` interval, [0.535, 1.449], is the only one of the four that excludes the table's
1.667.

**Its two eras disagree on Sonnet by more than their intervals allow.** masterrig/pre fits Sonnet
at 0.000 [0.000, 0.149]. It has no Sonnet-dominant stretch, so it is not pooled.

**The pooled Sonnet rate now rests on two fits that do not overlap.** The published 0.872x is
the median of 0.595x and 1.150x. It is not a figure either account measured.

In short, admitting masterrig brings a second Fable-heavy account into the fits, and on Fable it
behaves like jwork. On Sonnet, the one masterrig fit that qualifies disagrees with its own direct
reading, with dave, and with the reference table, and it comes from stretches that no capture
check has seen. The Sonnet figure should be re-read once masterrig's capture column is filled and
its exemption is reconsidered.
