# The Sonnet rate was too dear, and the reason was the sample, 2026-09-23

`history/model-rates.json` was fitted on 2026-09-20, before the five older models were priced.
At that time any stretch carrying a token from Haiku 4.5, Sonnet 4.6, Opus 4.7, Opus 4.8 or
Opus 5.5 was thrown away whole, and the fits ran on 41 jwork stretches before 14 September, 14
after, and 14 on dave. Pricing those models (PR #74, merged 09:16Z today) takes the same three
fits to 51, 26 and 32. This is what the rates do when they are refitted on the larger sample,
and why the Sonnet figure moved as far as it did.

Refit with the current code:

```
python3 -m tracker.gs_passive --masterrig --out history/masterrig-passive.json
python3 -m tools.model_rates history/masterrig-passive.json --json history/model-rates.json
python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
    --prices data/prices.json --gs-passive history/gs-passive.json \
    --masterrig-passive history/masterrig-passive.json --out /tmp/claude-usage-68.json
python3 tools/credits_report.py --publish-check /tmp/claude-usage-68.json
```

No traffic was sent for any of it. The gs half comes from the committed
`history/gs-passive.json`, which the hourly cron on gs built at 09:30:12Z from a checkout that
had pulled PR #74 at 09:17:06Z, so it is already a post-#74 report; PR #77, which merged at
09:40Z after that build, adds a `reset_source` field and deliberately computes nothing from
inferred resets, so it moves no figure this tool reads. The masterrig half was rebuilt here
because the committed copy is from 01:15Z and predates #74. masterrig is not one of the pooled
accounts, so that rebuild changes only the diagnostic sections, not a published rate.

## What the refit says

Rates in credits per input token, Opus fixed at 10/15 to set the scale, the joint fit at output
5x -- the variant the publisher adopts, in which the cache-read weight is fitted alongside the
rates rather than held at zero. Section 3 of the tool prints all of this.

| account / era | n | Sonnet | Fable | Haiku | cache-read weight | residual median |
|---|---|---|---|---|---|---|
| jwork before 14 Sep | 51 | 0.540 [0.339, 0.605] | 1.401 [1.313, 1.524] | 1.212 [0.000, 4.367] | 0.0114 | 0.050 |
| jwork after 14 Sep | 26 | 0.353 [0.145, 0.765] | 2.099 [1.657, 3.167] | 3.155 [0.625, 13.841] | 0.0209 | 0.059 |
| dave after 14 Sep | 32 | 0.397 [0.323, 0.448] | 1.059 [0.838, 1.378] | 0.000 (pinned) | 0.0000 | 0.111 |
| masterrig before 14 Sep | 179 | 0.007 [0.000, 0.567] | 2.925 [1.787, 5.920] | 0.000 (pinned) | 0.0054 | 0.447 |
| masterrig after 14 Sep | 47 | 0.829 [0.554, 1.349] | 1.832 [1.456, 2.754] | 1.997 [0.000, 4.155] | 0.0000 | 0.170 |

masterrig is in the table and in nothing else. Its residual is 0.447 before the cut against
0.050 to 0.111 on the two gs accounts, which is the phantom meter movement of 2 to 5 September
and the web and phone traffic its transcripts never see; its `capture` column is null, so the
acceptance rule that keeps the gs accounts honest is not applied to it at all. Its two sides
disagree with each other by more than a factor of a hundred on Sonnet. It supports no rate and
drives none: `FIT_ACCOUNTS` in `tools/model_rates.py` has been jwork and dave since the rates
were first fitted, and that is unchanged here.

Pooled across the three gs fits, as the publisher adopts them:

| family | before (2026-09-20) | after (this refit) | reference |
|---|---|---|---|
| opus | 0.6667, the anchor | 0.6667, the anchor | 0.6667 |
| sonnet | 0.5178, 0.777x Opus, [0.327, 0.832] | 0.3967, 0.595x Opus, [0.145, 0.765] | 0.4000 |
| haiku | no value | no value | 0.1333 |
| fable | 1.3068, 1.960x Opus, [1.179, 3.036] | 1.4014, 2.102x Opus, [0.838, 3.167] | absent |

The Sonnet rate falls 23%, from 0.777 times Opus to 0.595. The reference table's own ratio is
0.6, so the measurement and the eight-month-old table now agree to within 1%, where before the
measurement said Sonnet cost 30% more than the table did. Every fit's `opus:sonnet` interval
holds the table's 1.667: jwork before the cut 1.234 [1.101, 1.964], jwork after 1.890 [0.864,
4.209], dave 1.681 [1.489, 2.062]. On 20 September the jwork pre-cut fit read 1.072 [0.899,
1.233] and excluded it.

## Why the old figure was too dear

Four candidate explanations were tested. Three of them are real but small, and the fourth is the
whole of it.

**The sample was selected on model mix.** This is the answer. Before #74 a stretch was thrown
away for carrying a single token of Sonnet 4.6 or Haiku 4.5 -- and the stretches that carry
those are the mixed, sub-agent-heavy ones, which is to say the Sonnet-heavy ones. The fit was
left with the stretches in which Sonnet is a minority column, and a minority column takes
whatever the majority ones leave it. The same fit on the same accounts with the sample restored
moves Sonnet down on all three: jwork before the cut 0.837x to 0.811x, jwork after 0.753x to
0.529x, dave 0.777x to 0.595x. dave gained the most stretches relative to its size (14 to 32),
and its interval no longer contains the old pooled value at all.

**Collinearity with the cache-read column.** Real, and second-order. The correlation between a
stretch's Sonnet column and its cache-read column is +0.29 on jwork before the cut, +0.37 after
and +0.25 on dave; the Sonnet column's R-squared on all the other columns is 0.21, 0.21 and
0.44, for variance inflation factors of 1.3, 1.3 and 1.8. Fitting the cache-read weight instead
of holding it at zero does move Sonnet down -- jwork before the cut 0.932x to 0.811x, jwork
after 0.710x to 0.529x -- but on dave it moves it not at all, because dave's fitted cache-read
weight is zero. The publisher already adopts the joint variant, so this bias was already out of
the published figure before today.

**Capture-poor stretches.** Real, and it works in the same direction. The standing rule keeps
only capture-accepted stretches on the gs accounts, and dropping that rule raises Sonnet on
every group: jwork before the cut 0.811x to 0.981x on 79 stretches instead of 51, jwork after
0.529x to 1.600x on 42 instead of 26, dave 0.595x to 0.809x on 52 instead of 32. A stretch whose
meter moved more than its transcripts explain loads the unexplained movement onto whichever
column is cheapest to inflate. The published fit is the restricted one, so again this is already
out of it.

**The 14 September cut splitting the data.** Not the cause. Fitting jwork's 77 stretches as one
group across the cut gives Sonnet 0.640x [0.345, 0.796], between the two eras' 0.811x and 0.529x
and on the same side of the old 0.777x. The cut widens the intervals; it does not move the
centre.

## Is the dave reading the outlier?

No. It is the only account with enough Sonnet-carrying stretches to read directly, and it reads
low, which is what the refit now says.

Taking every clean stretch in which Sonnet carries at least 60% of the raw tokens, against those
in which Opus and Fable together carry at least 60%, in input-equivalent tokens per 1% of the
five-hour meter:

| account / era | Sonnet-heavy n | median | Opus/Fable-heavy n | median | implied Sonnet rate |
|---|---|---|---|---|---|
| jwork before 14 Sep | 1 | 275,720 | 43 | 265,064 | 0.96x Opus |
| jwork after 14 Sep | 1 | 255,555 | 19 | 223,901 | 0.88x Opus |
| dave after 14 Sep | 9 | 287,221 | 17 | 199,714 | 0.70x Opus |
| masterrig before 14 Sep | 10 | 139,381 | 130 | 131,264 | 0.94x Opus |
| masterrig after 14 Sep | 4 | 271,425 | 26 | 180,429 | 0.67x Opus |

Only dave and masterrig have three or more Sonnet-heavy stretches, and both read below Opus;
jwork has one on each side of the cut and neither is evidence of anything. There is still no
stretch anywhere in which Sonnet carries 85% or more of the raw tokens -- the highest Sonnet
share of any clean stretch is 0.840 on dave, 0.772 on jwork before the cut and 0.841 on
masterrig -- so the direct reading above is a tilt, not a pure measurement, and the fitted rate
remains the figure to use.

The figure adopted is 0.3967 credits per input token, 0.595 times Opus, interval [0.145, 0.765].
It is the median of three fits whose intervals overlap (`agree: true`), the widest of which is
jwork's 26 post-cut stretches. It is trusted over the old 0.5178 because the old one came from a
sample that had been filtered on the very variable being measured, and because the direct
Sonnet-heavy reading on the two accounts that have one agrees with the new figure and not the
old.

## Haiku is now measurable-looking and still not measurable

Before today Haiku had no fitted coefficient at all and the file said so. Now that Haiku 4.5 is
priced, Haiku tokens reach the design matrix and two of the three fits return a coefficient for
it: jwork before the cut 1.212 [0.000, 4.367] and jwork after 3.155 [0.625, 13.841], pooling to
2.183 -- 3.3 times Opus, twenty-five times the reference table, with an interval running to
[0.00x, 20.76x].

That is not a measurement. Haiku carries at most 0.257 of any clean stretch's raw tokens (dave;
0.133 on jwork) and none at all in 75 of the 109 stretches. A bootstrap interval whose lower end
is zero is the fit saying that the resampled data contain draws in which the other columns
absorb the family whole.

`tools/model_rates.py` now withholds both the value and the interval for any family whose pooled
interval reaches zero, with a status sentence saying why, and the fits' own coefficients stay on
the record in `per_fit`. Withholding the interval as well as the value is deliberate:
`tracker/gs_passive.py` prices a stretch at the midpoint of a published interval where a family
has no single value, so publishing Haiku's [0.000, 13.841] would have charged 6.92 credits a
token -- ten times Opus and fifty times the reference -- on the third of gs's stretches that
carry any Haiku, and would have flipped every model's per-stretch `rate_source` from
`reference_table` to `interval_midpoint`. With the guard in place, nothing about credit
detection changes.

## What moved on the page

Recomputed by running the publisher twice over the same committed history files, changing only
`history/model-rates.json`.

| published figure | before | after |
|---|---|---|
| `credits.per_model.sonnet.credits_per_token.input` | 0.51778 | 0.39669 |
| `credits.per_model.sonnet.tokens_per_window.input.value` | 35,762,561 | 46,678,284 |
| `credits.per_model.sonnet.tokens_per_window.input.interval` | [19,885,609, 59,912,788] | [21,618,991, 135,280,151] |
| `credits.per_model.sonnet.api_value_per_window_usd.input.value` | $71.53 | $93.36 |
| `credits.window_tokens.per_family.sonnet.all.value` | 559,384,249 | 730,123,794 |
| `credits.window_tokens.per_week.per_family.sonnet.all.value` | 2,836,078,142 | 3,701,727,636 |
| `credits.sessions.claude-sonnet-5.credits_per_token_at_split` | 0.02480 | 0.01900 |
| `credits.sessions.claude-sonnet-5.per_window.value` | 1,180.0 | 1,540.1 |
| `credits.sessions.claude-sonnet-5.per_week.value` | 5,982.5 | 7,808.5 |
| `credits.per_model.fable.credits_per_token.input` | 1.30680 | 1.40137 |
| `credits.per_model.fable.tokens_per_window.input.value` | 14,169,717 | 13,213,462 |
| `credits.window_tokens.per_family.fable.all.value` | 221,637,280 | 206,679,904 |
| `credits.sessions.claude-fable-5-1.per_window.value` | 43.9 | 40.9 |
| `credits.per_model.haiku.*` | status sentence, no figures | status sentence, no figures |
| `credits.per_model.opus.*` | unchanged | unchanged |
| `rates.*.evidence.credit_detection.rate_sources` | unchanged | unchanged |

The Opus rows cannot move: Opus is the anchor, and every rate is measured against it. The Haiku
rows carry a different sentence and the same absence of numbers. 262 leaves of the published
JSON differ in total, all of them inside the `credits` block, and `tools/credits_report.py
--publish-check` reproduces all 10,071 figures of the new file from the history files.

Nothing outside the `credits` block moves. The published `last_change` event, the
per-model `rates.*.evidence` blocks and every detection figure are byte-identical before
and after, so the refit is a change to what a credit buys per model and not to what the
tracker says happened.

The intervals widen, most of all Sonnet's, and that is honest: the refit's widest fit is jwork's
26 post-cut stretches, whose Sonnet interval alone runs [0.145, 0.765].

## Left open

The cache-read weight still has no single value. Its three fits are dave 0.0000, jwork before
the cut 0.0114 and jwork after 0.0209, and the union of their intervals is [0.0000, 0.0473] --
wider than the [0.0000, 0.0189] of 20 September, because jwork's post-cut interval widened with
the sample. `data/prices.json` still holds 0 with the range [0, 0.015], and the fits still
straddle it. dave fitting exactly zero while jwork fits 0.011 to 0.021 is the one disagreement
in the file that no amount of extra data has yet closed.

Fable's fits still disagree -- dave 1.588x Opus, jwork before the cut 2.102x, jwork after 3.149x
-- and the published rate is still the median with the union of the intervals, for the reason
the row's own `why` gives: the data cannot say whether Fable's rate moved across 14 September or
the five-hour window did.
