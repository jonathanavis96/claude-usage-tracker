# Opus 5.5 as its own family, and the 60% dominance rule, 2026-09-23

Two changes to how the meter's per-model rates are fitted and credited. Neither published
family is withheld as a result: Sonnet keeps its rate and gets a much narrower interval, and
Fable's rate rises from 2.10 to 3.15 times Opus, because the one fit that actually has
Fable-heavy stretches is the dearest of the three. Haiku stays withheld, now for a different
stated reason. Opus 5.5 is a new family with no tokens in any committed stretch, so it is
withheld as not measurable.

## Opus 5.5

Claude Code 2.1.280 resolves the `opus` alias to `claude-opus-5-5`. masterrig has run it
since 2026-09-22 19:41Z, and every gs account now runs it by default. `tracker/credits.py`
matched credit families by the first `matches` string that was a substring of the model id,
and `opus` is a substring of `claude-opus-5-5`, so Opus 5.5 was credited at Opus 5's rate. It
lists at 0.8x Opus 5's prices. If the meter charges it differently, the per-model figures
would show a phantom change as its share of the traffic grows, which is the same failure as
the Sonnet phantom of 20 September.

- `data/prices.json` `_credits.per_family` has a new `opus-5-5` row (`matches: "opus-5-5"`,
  role `reference`). Its input and output rates are null because the January 2026 Shellac
  table predates the model, and its `measured` field says so. Opus 5 (`opus`) is still the
  anchor that defines the unit. `list_price_model` maps `opus-5-5` to `claude-opus-5-5`.
- `tracker/credits.py` `family()` now picks the longest `matches` that is a substring, so
  `claude-opus-5-5` maps to `opus-5-5` while `claude-opus-5`, `-4-8` and `-4-7` stay `opus`.
  The result no longer depends on the order of the table's keys, and the tests check it both
  ways round.
- `tools/model_rates.py` fits `opus-5-5` like any non-anchor family. There are no
  `claude-opus-5-5` tokens in `history/gs-passive.json` or `history/masterrig-passive.json`,
  so the family never reaches `MIN_NONZERO` stretches, no fit includes its column, and it is
  withheld with `not measurable, no clean stretch is Opus 5.5-heavy`. An all-zero column is
  covered by a test. Its measured rate will appear once stretches from after 22 September
  reach the committed history and at least three of them are 60% or more Opus 5.5.
- The pricing `_source` and the `tracker/turns.py` comment called Opus 5.5 one of "the five
  older models". It is the newest Opus. Both now say that five models were added on
  2026-09-23: Opus 5.5, plus the four older ones (Opus 4.8, Opus 4.7, Sonnet 4.6 and Haiku
  4.5).

Published effect: `rates.claude-opus-5-5.credits_family` reads `opus-5-5` instead of `opus`,
and `credits.per_model`, `credits.window_tokens.per_family` and `credits.measured_rates` each
gain an `opus-5-5` row that has a status sentence and no figures.

## The dominance rule

`docs/findings-2026-09-23-sonnet-rate.md` found that the old Sonnet rate was too dear
because the sample left Sonnet as a minority column, and a minority column takes whatever the
majority columns leave. The Fable fits had the same shape. dave's highest Fable share in any
clean stretch is 0.46, and its Fable coefficient of 1.059 [0.838, 1.378] was still one of the
three that were pooled.

`tools/model_rates.py` now pools a fit into family f's rate only if at least
`DOMINANCE_MIN_N = 3` of that fit's stretches carry `DOMINANCE_SHARE = 0.60` or more of their
raw tokens on f. The Opus anchor is exempt. Every fit that returns a coefficient stays in
`per_fit` with `dominant_n`, `qualified` and a `qualification` sentence. `n_fits` counts the
pooled fits and `n_fits_fitted` counts every fit. A family that no fit qualifies for is
withheld (value and interval both null) with
`not measurable, no fit has 3 clean stretches that are 60% or more <family>`.

One qualifying fit is enough to publish. Before this change a family needed two contributing
fits before it got a value. With the rule in place, requiring two would have withheld both
Sonnet and Fable, even though each has one fit that measures it directly. It would also have
contradicted the rule's own definition, under which a family is withheld only when no fit
qualifies.

Stretches that carry 60% or more of their raw tokens on each family, per fit (clean stretches,
committed history):

| fit | n | Opus | Sonnet | Haiku | Fable | Opus 5.5 |
|---|---|---|---|---|---|---|
| jwork before 14 Sep | 51 | 37 | 1 | 0 | 2 | 0 |
| jwork after 14 Sep | 26 | 10 | 1 | 0 | **3** | 0 |
| dave after 14 Sep | 32 | 13 | **9** | 0 | 0 | 0 |
| masterrig before 14 Sep (not pooled) | 179 | 58 | 10 | 0 | 32 | 0 |
| masterrig after 14 Sep (not pooled) | 45 | 9 | 4 | 0 | 5 | 0 |

Which fits qualify:

| family | qualifies | left out (dominant stretches of n) |
|---|---|---|
| opus | all three (anchor, exempt) | none |
| sonnet | dave/post | jwork/pre (1 of 51), jwork/post (1 of 26) |
| haiku | none | jwork/pre (0 of 51), jwork/post (0 of 26) |
| fable | jwork/post | jwork/pre (2 of 51), dave/post (0 of 32) |
| opus-5-5 | none (no fit includes it) | none |

masterrig remains outside `FIT_ACCOUNTS`, as before. Its rows above are for comparison only.

## Before and after

Adopted rates in credits per input token, from `history/model-rates.json`
`measured_rates.per_family`: the joint fit at output 5x, with Opus fixed at 10/15.

| family | before (sonnet-rate refit, earlier today) | after (this change) | reference |
|---|---|---|---|
| opus | 0.6667, the anchor | 0.6667, the anchor | 0.6667 |
| sonnet | 0.3967, 0.595x Opus, [0.145, 0.765] = [0.217x, 1.147x] | 0.3967, 0.595x Opus, [0.323, 0.448] = [0.485x, 0.672x] | 0.4000 |
| haiku | no value (pooled interval reaches zero) | no value (no fit is Haiku-dominant) | 0.1333 |
| fable | 1.4014, 2.102x Opus, [0.838, 3.167] = [1.257x, 4.751x] | 2.0991, 3.149x Opus, [1.657, 3.167] = [2.485x, 4.751x] | absent |
| opus-5-5 | not a family (credited as opus) | no value (no clean stretch is Opus 5.5-heavy) | absent |

**Sonnet.** The value does not move, because the median of the three fits was already dave's
0.3967. The interval narrows from the union of three fits to dave's own [0.323, 0.448].
jwork's two fits are the ones that widened it, and each has one Sonnet-dominant stretch. Its
lower end was jwork/post's 0.145, and its upper end was jwork/post's 0.765. The table's 0.4000
is still inside the interval.

**Fable.** Pooling now uses only jwork/post, the one fit with three stretches that are 60% or
more Fable. That fit reads 2.099 [1.657, 3.167], so Fable moves from 2.10 to 3.15 times Opus.
Both of the fits left out read cheaper: jwork/pre at 1.401 [1.313, 1.524], with two
Fable-dominant stretches, and dave/post at 1.059 [0.838, 1.378], with none. The median of
three used to land on jwork/pre and the union reached down to dave's 0.838. Neither of those
fits contains a stretch in which Fable carries the meter movement often enough to measure
it. The upper end of the interval is unchanged, and `agree` is now trivially true because
only one fit is pooled.

Fable's value rests on exactly `DOMINANCE_MIN_N` stretches. A single reclassified stretch in
jwork/post would take Fable to withheld. Equally, one more Fable-dominant stretch in jwork/pre
would bring 1.401 back into the pool, and the published value would become the median of two
(1.750). The question from 20 September is still open: the data still cannot say whether
Fable's rate moved across 14 September or the five-hour window did. The rule changes which
fits are allowed to answer it, and it does not answer it.

**Haiku.** Before this change Haiku was withheld because its pooled interval reached zero.
Now no fit qualifies at all (0 Haiku-dominant stretches anywhere, highest share 0.257), so the
status sentence changes and the absence of figures does not.

## What moved on the page

Recomputed by running the publisher over the same committed history files twice: once at
`main` (b0a8874) and once on this branch.

```
python3 -m tools.model_rates history/masterrig-passive.json --json history/model-rates.json
python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
    --prices data/prices.json --gs-passive history/gs-passive.json \
    --masterrig-passive history/masterrig-passive.json --out <scratch>/after.json
python3 tools/credits_report.py --publish-check <scratch>/after.json
```

| published figure | before | after |
|---|---|---|
| `credits.per_model.fable.credits_per_token.input` | 1.40137 | 2.09908 |
| `credits.per_model.fable.tokens_per_window.input.value` | 13,213,462 | 8,821,480 |
| `credits.per_model.fable.tokens_per_window.input.interval` | [5,221,635, 23,355,483] | [5,221,635, 11,812,282] |
| `credits.per_model.fable.api_value_per_window_usd.input.value` | $132.13 | $88.21 |
| `credits.window_tokens.per_family.fable.all.value` | 206,679,904 | 137,982,197 |
| `credits.sessions.claude-fable-5-1.per_window` | 40.9 [16.2, 89.1] | 27.3 [16.2, 45.0] |
| `credits.per_model.sonnet.credits_per_token.input` | 0.39669 | 0.39669 |
| `credits.per_model.sonnet.tokens_per_window.input.interval` | [21,618,991, 135,280,151] | [36,937,793, 60,531,923] |
| `credits.per_model.sonnet.api_value_per_window_usd.input.interval` | [$43.24, $270.56] | [$73.88, $121.06] |
| `credits.sessions.claude-sonnet-5.per_window` | 1,540.1 [713.3, 4,463.5] | 1,540.1 [1,218.8, 1,997.2] |
| `credits.per_model.haiku.*` | status sentence, no figures | different status sentence, no figures |
| `credits.per_model.opus-5-5.*` | absent | status sentence, no figures |
| `rates.claude-opus-5-5.credits_family` | `opus` | `opus-5-5` |
| `credits.per_model.opus.*` | unchanged figures | unchanged figures (new per-fit fields) |

Of the 270 leaves that differ, 263 are inside the `credits` block. Five are the
`meter_weight_source` wording of the five added models, one is `rates.claude-opus-5-5.credits_family`,
and one is `generated_at`. The `last_change` event and the `rate_sources` in credit detection are
identical. `tools/credits_report.py --publish-check` reproduces all 10,263 figures of the new
file from the history files.

## Inputs

No traffic was sent. The gs half is the committed `history/gs-passive.json`, and the masterrig
half is the committed `history/masterrig-passive.json`, used as it stands: the tool did not
need a rebuild, and masterrig is not pooled. That committed masterrig file yields 45 clean
stretches after 14 September, against the 47 behind the earlier refit today. Both
exclusion lists (probes-only and harness-runs) give 224 clean masterrig stretches where the
earlier refit had 226, so the two came out of the stretch file rather than out of this change.
They move only masterrig's diagnostic rows in section 3, not any adopted rate.
