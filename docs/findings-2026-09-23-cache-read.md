# The cache-read weight, profiled, 2026-09-23

The three fits the publisher pools disagree about the cache-read weight. With the weight fitted
jointly with the per-family rates (`history/model-rates.json`, table "What the refit says" in
`docs/findings-2026-09-23-sonnet-rate.md`), jwork before 14 September puts it at 0.0114, jwork
after at 0.0209, and dave after at 0.0. `data/prices.json` publishes 0 with the range
[0, 0.015]. dave's 0 is on the non-negativity bound, so it could mean the weight is zero or that
dave's stretches cannot see it and the bound is what put it there. This note tells the two apart.

Reproduce with:

```
python3 -m tools.cache_read_profile history/masterrig-passive.json \
    --now 2026-09-23T12:00:00+00:00 --extra-weight 0.0114 --extra-weight 0.015
```

No traffic was sent. The stretches, the exclusion list, the family grouping and the output-5x
input-equivalent tokens are `tools/model_rates.py`'s own (imported, not copied), so the fit at a
fixed weight is that tool's joint fit with the cache-read coefficient held instead of fitted:
the cache reads join the Opus column at w times their count. As in that tool, the weight is one
number, charged at the Opus input rate on every family's cache reads. The profile's own minima
reproduce the joint fits exactly (0.0114, 0.0209, 0.0000), which is the check that nothing
changed but the weight being held. The bootstrap is the same one: seed 20260920, 600 resamples,
80% intervals.

## 1. The profile

Residual sum of squares (meter percent squared) and median absolute relative residual at each
fixed weight, with the rates it lands on (credits per input token, Opus fixed at 10/15). The
figure in brackets is the RSS above the fit's own best weight.

**jwork before 14 September** (n = 51, own best 0.0114, RSS 30.30)

| weight | RSS | median abs rel | Sonnet | Haiku | Fable |
|---|---|---|---|---|---|
| 0.000 | 55.75 (+25.45) | 0.0554 | 0.622 | 1.513 | 1.268 |
| 0.005 | 37.00 (+6.69) | 0.0507 | 0.582 | 1.362 | 1.322 |
| 0.010 | 30.57 (+0.26) | 0.0517 | 0.548 | 1.241 | 1.384 |
| 0.015 | 31.89 (+1.58) | 0.0427 | 0.521 | 1.143 | 1.450 |
| 0.020 | 38.02 (+7.72) | 0.0509 | 0.497 | 1.062 | 1.521 |
| 0.025 | 47.07 (+16.76) | 0.0614 | 0.478 | 0.996 | 1.594 |
| 0.030 | 57.80 (+27.49) | 0.0647 | 0.461 | 0.941 | 1.671 |

**jwork after 14 September** (n = 26, own best 0.0209, RSS 44.38)

| weight | RSS | median abs rel | Sonnet | Haiku | Fable |
|---|---|---|---|---|---|
| 0.000 | 65.89 (+21.52) | 0.1371 | 0.473 | 1.391 | 1.698 |
| 0.005 | 53.97 (+9.59) | 0.1056 | 0.420 | 1.763 | 1.758 |
| 0.010 | 47.91 (+3.54) | 0.0836 | 0.386 | 2.176 | 1.847 |
| 0.015 | 45.20 (+0.83) | 0.0746 | 0.365 | 2.615 | 1.955 |
| 0.020 | 44.39 (+0.01) | 0.0616 | 0.354 | 3.073 | 2.077 |
| 0.025 | 44.65 (+0.28) | 0.0636 | 0.349 | 3.545 | 2.208 |
| 0.030 | 45.51 (+1.13) | 0.0664 | 0.350 | 4.027 | 2.347 |

**dave after 14 September** (n = 32, own best 0.0000, RSS 110.41)

| weight | RSS | median abs rel | Sonnet | Haiku | Fable |
|---|---|---|---|---|---|
| 0.000 | 110.41 (+0.00) | 0.1114 | 0.397 | 0.000 | 1.059 |
| 0.005 | 136.20 (+25.79) | 0.1235 | 0.383 | 0.000 | 1.041 |
| 0.010 | 172.14 (+61.73) | 0.1338 | 0.382 | 0.000 | 1.065 |
| 0.015 | 209.30 (+98.89) | 0.1490 | 0.391 | 0.000 | 1.120 |
| 0.020 | 244.14 (+133.73) | 0.1747 | 0.407 | 0.000 | 1.197 |
| 0.025 | 275.54 (+165.13) | 0.1788 | 0.428 | 0.000 | 1.292 |
| 0.030 | 303.37 (+192.96) | 0.1971 | 0.453 | 0.000 | 1.400 |

dave's profile is not flat. It is the steepest of the three: its RSS nearly doubles between 0
and 0.015, where jwork's moves by a few points either side of its minimum. The bound is not
hiding a weight dave cannot see. dave also has the leverage to see one: its cache reads run 15.7
to 45.2 times its input-equivalent tokens (10th to 90th percentile, median 26.2), against 13.5 to
29.7 on jwork before the cut and 18.9 to 45.6 after, so its stretches vary the cache-read share
as much as jwork's do.

jwork after the cut is the flat one. Its RSS moves by less than 1.2 across 0.015 to 0.030, and
its Haiku coefficient triples over the same range, which is the cache-read column trading
against a family column the fit cannot pin down.

## 2. Does dave contradict jwork?

For each fit, the RSS increase from its own best weight to each other fit's weight, beside two
readings of the same 600 bootstrap draws: the spread of the fit's own RSS (the width of its 80%
interval at its own best weight), and a paired test, the share of draws in which the other
weight fits the resampled stretches at least as well as the fit's own. A share of 10% or more
(the tool's 80% interval rule) is "cannot distinguish"; below it, "contradicts".

| fit | own weight | RSS spread | at | RSS increase | increase / spread | draws fitting as well | verdict |
|---|---|---|---|---|---|---|---|
| dave/post | 0.0000 | 74.68 | jwork/pre 0.0114 | +71.97 | 0.96 | 1.0% | contradicts |
| dave/post | 0.0000 | 74.68 | jwork/post 0.0209 | +139.48 | 1.87 | 0.2% | contradicts |
| jwork/pre | 0.0114 | 12.78 | dave/post 0.0000 | +25.45 | 1.99 | 0.2% | contradicts |
| jwork/pre | 0.0114 | 12.78 | jwork/post 0.0209 | +9.13 | 0.71 | 18.5% | cannot distinguish |
| jwork/post | 0.0209 | 41.88 | dave/post 0.0000 | +21.52 | 0.51 | 2.7% | contradicts |
| jwork/post | 0.0209 | 41.88 | jwork/pre 0.0114 | +2.54 | 0.06 | 17.8% | cannot distinguish |

**dave contradicts jwork.** Moving dave from 0 to jwork's 0.0114 adds 72 to its RSS, about the
whole width of its own error's bootstrap spread (0.96 of it), and to 0.0209 adds 139, nearly
twice the spread. The paired test is sharper, because the spread mixes in how much the error
level moves with which stretches are drawn: in only 1.0% of dave's draws does 0.0114 fit as well
as 0, and in 0.2% does 0.0209. The contradiction runs both ways. Both jwork fits exclude dave's
0 (0.2% and 2.7% of draws), while the two jwork fits cannot tell each other apart.

So the answer to the question is not "dave cannot distinguish". dave sees the weight and puts it
at zero, jwork sees it and puts it at 0.011 to 0.021, and each account's data exclude the
other's value. One cache-read weight shared by the three fits is not what the data show. Whatever
separates the accounts (dave's residual is twice jwork's at every weight; something the
transcripts do not carry, or a charge that is not proportional to cache reads) is loading onto
the one column that differs between them.

## 3. One weight across the three fits

One weight, the family rates per fit, the weight minimising the RSS summed over the fits. The
interval resamples each fit's stretches within that fit.

| pooling | weight | 80% interval | RSS jwork/pre | RSS jwork/post | RSS dave/post | total |
|---|---|---|---|---|---|---|
| each fit at its own weight | -- | -- | 30.30 | 44.38 | 110.41 | 185.09 |
| summed RSS | **0.0030** | [0.0000, 0.0069] | 42.78 | 57.90 | 123.73 | 224.41 |
| variance-weighted | 0.0085 | [0.0063, 0.0128] | 31.49 | 49.26 | 161.17 | 241.92 |

62 of the 600 summed-RSS draws land on 0, none on the search bound of 0.06.

Plain summed RSS lets the noisiest fit steer: dave's residual variance at its own best weight is
4.09 against jwork's 0.66 and 2.11, so dave carries most of the squared error and the pooled
weight sits near dave's end. Dividing each fit's RSS by its own residual variance, as a
heteroscedastic fit would, moves the shared weight to 0.0085. Both are reported because the
choice between them is a choice of how much to trust dave's stretches, and nothing measured here
makes it. Either interval is narrower than the disagreement it averages over, and that is not
precision: the bootstrap resamples stretches within each account, and the disagreement is between
accounts. Neither interval holds all three fits' own values, and both exclude jwork after the cut.

Rates at the summed-RSS pooled weight: Sonnet 0.597 (jwork/pre), 0.439 (jwork/post), 0.387
(dave/post); Fable 1.299, 1.730, 1.042.

## 4. What the published figures do

The publisher (`tracker.publish.rebuild_public_json`, the path `main` and the publish check
share) rebuilt at 2026-09-23T12:00Z from a scratch copy of `data/prices.json` with only
`_credits.cache_read_weight` overridden. The per-family rates stay the ones
`history/model-rates.json` publishes, so these are the weight's own effect. The committed
`data/prices.json` was not written, and a test checks that.

| figure | w = 0 | 0.0030 (pooled) | 0.0085 (variance-weighted) | 0.0114 | 0.015 | 0.02 |
|---|---|---|---|---|---|---|
| `window_credits` (current) | 18,516,982 | +9.1% | +26.9% | +34.7% | +44.4% | +61.1% |
| `window_credits.before` | 19,698,917 | +4.5% | +12.7% | +17.1% | +22.6% | +30.2% |
| headline `window_tokens.all` | 434,453,284 | +4.5% | +12.7% | +15.0% | +17.8% | +23.7% |
| `window_tokens.per_week.all` | 2,202,678,150 | +4.5% | +12.7% | +15.0% | +17.8% | +23.7% |
| `window_tokens.per_family` sonnet | 730,123,794 | +4.5% | +12.7% | +15.0% | +17.8% | +23.7% |
| `window_tokens.per_family` fable | 206,679,904 | +4.5% | +12.7% | +15.0% | +17.8% | +23.7% |
| `per_model.opus.tokens_per_window.input` | 27,775,473 | +9.1% | +26.9% | +34.7% | +44.4% | +61.1% |
| `per_model.sonnet.tokens_per_window.input` | 46,678,284 | +9.1% | +26.9% | +34.7% | +44.4% | +61.1% |
| `per_model.fable.tokens_per_window.input` | 13,213,462 | +9.1% | +26.9% | +34.7% | +44.4% | +61.1% |
| `per_model.*.tokens_per_window.output` | Opus 5,555,095; Sonnet 9,335,657; Fable 2,642,692 | +9.1% | +26.9% | +34.7% | +44.4% | +61.1% |

Haiku publishes null at every weight (its rate is not identified). The headline interval moves
with its value: [374,355,333, 509,869,057] at 0, [391,081,848, 532,650,440] at 0.003,
[463,165,162, 630,827,354] at 0.02.

Every per-model tokens-per-window figure is the current window in credits over a rate, so they
all move together, by the current window's change. The current window moves more than the before
cluster because the five-hour change across the cut is itself priced at the weight: the current
value is 0.940 of the before value at 0, 0.982 at 0.003 and 1.163 at 0.02. The headline counts
the pure-Opus window's own tokens, which no weight changes, so it moves only through that factor:
0.982 / 0.940 is its +4.5% at 0.003, and 1.163 / 0.940 its +23.7% at 0.02.

The weekly cross-check (`window_credits_from_weekly`, the announced weekly cap over the measured
windows per week) does not move with the weight: 19,290,116 before the cut and 20,545,685 after.
Against it the published current window reads -10% at 0, -2% at 0.003, +14% at 0.0085 and +45%
at 0.02, and the before cluster +2%, +7%, +15% and +33%. That cap is in the January reference's
credit unit, which counts cache reads as free, so the check leans towards a low weight by
construction and is corroboration, not an independent measurement.

## Recommendation

The publisher should adopt a cache-read weight of **0.003**, the summed-RSS pooled weight, and
keep the range **[0, 0.015]**. The profile settles the question it was built for: dave's 0 is a
measurement, not blindness. dave's error rises faster with the weight than either jwork fit's,
and it excludes jwork's 0.011 and 0.021 in 99% or more of its bootstrap draws, so dave
contradicts jwork rather than failing to distinguish. It follows that the three fits do not
measure one weight, and the pooled value is a compromise between accounts that disagree rather
than a shared measurement. 0.003 is the compromise that fits the three fits' stretches with the
least total error in meter points, sits inside the range already published, and moves the page
least (+9.1% on every per-model tokens-per-window figure, +4.5% on the headline). It also brings
the current window within 2% of the weekly cross-check, from 10% below it at 0. The range should
stay [0, 0.015] and not narrow to the pooled interval [0, 0.0069]: that interval resamples within
accounts and so understates a disagreement between them, and [0, 0.015] already holds the
variance-weighted pooled interval [0.0063, 0.0128] and jwork before the cut's own 0.0114. Only
jwork after the cut (0.0209, the smallest fit, its own interval [0.012, 0.047]) lies outside it,
and at 0.02 the published window reads 45% above the weekly cross-check. This PR does not change
the adopted value. If it is adopted, the rates in `history/model-rates.json` were fitted with the
weight free, not at 0.003, and would need refitting with it held; section 3's rates are those.
