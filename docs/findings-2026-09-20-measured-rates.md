# The per-model rates the page divides by, measured, 2026-09-20

Every per-model figure the public JSON states is now the measured five-hour window divided by a
rate measured on the meter, not by a row of the January 2026 reference table. This is the record
of those rates: what each one is, its interval, how many fits stand behind it, the command that
produced it, the reference figure beside it, and every published figure that moved.

This is the second follow-up of `docs/findings-2026-09-20-model-rates.md` ("Adopt the rate
table") answered, and answered differently from the way that document proposed: the measured
rates are **not** written into `data/prices.json`, which stays a table of reference rates. They
live in `history/model-rates.json`, computed by the tool and committed with the command that
rebuilds them, so a measurement is never frozen into a reference table.

No traffic was sent for this. Every figure below comes from `history/gs-passive.json`,
`history/masterrig-passive.json`, `history/harness-runs.jsonl`, `history/probes.jsonl`,
`data/effort_matrix.json` and `data/prices.json`, all already in the repository.

Reproduce everything with:

```
python3 -m tools.model_rates history/masterrig-passive.json                       # sections 0 to 6
python3 -m tools.model_rates history/masterrig-passive.json --json history/model-rates.json
python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
    --gs-passive history/gs-passive.json --out /tmp/claude-usage-56.json
python3 tools/credits_report.py --publish-check /tmp/claude-usage-56.json
```

The second command's output is committed as `history/model-rates.json`, and the command is in
that file's own `_meta.command`. `tracker/credits.py` reads the file's `measured_rates` block and
nothing else from it; the publisher divides by what it finds there.

## The rule this change applies

A reference figure is shown beside a measured one and never used in the arithmetic. Before this
change the page's `credits.per_model.<family>.tokens_per_window` divided the measured window by a
per-model rate from `data/prices.json`'s `_credits` block -- the she-llac.com table of January
2026, with unversioned model rows. PR #66 measured the meter's own Opus:Sonnet ratio at
1.081 [0.886, 1.280] on the 41 pre-14-September stretches of the account with a usable capture
column, an interval that excludes the table's 1.667, and the table's rate was published all the
same. Three rows of four on the page were therefore reference figures stated as measurements.

`_credits` is now marked `role: reference` and keeps every row. Two things in it are still used
and both say so on the row: **Opus**, which is the unit anchor, and the cache-read weight, which
was measured on masterrig rather than taken from the article.

## 1. Opus is the anchor, and this work cannot test it

Credits are defined here with Opus as the unit: 10/15 credits per input token, five times that
per output token. Every other rate below is measured **relative to** it -- the fit rescales its
own Opus coefficient to 10/15, which is what makes the other coefficients read in credits. So
the Opus row publishes `rate_source: "reference"` and `anchor: true`, and carries the sentence
saying that if this one row is wrong every credit figure on the page scales with it and nothing
in our data would show it. Calling it "measured" would be a claim this work cannot support.

## 2. The joint fit: the cache-read weight is fitted, not held at zero

The mixed fit of PR #66 held cache reads at a weight of 0, as the reference table reads
literally. On these accounts a Sonnet-heavy stretch is also a cache-read-heavy one -- sub-agent
traffic -- so a Sonnet rate fitted that way can absorb the cache-read charge, and the hostile
review of 2026-09-20 put the weight at 0.005 to 0.015 rather than at nothing. The fit now carries
the stretch's cache reads as a column of its own, so one weight for the meter is estimated
jointly with the per-model rates. Both fits are computed and printed for every group; the joint
one is what the publisher adopts.

Per account and per side of 2026-09-14, at output 5x, cache reads at 0 above the joint fit:

| Fit | n | Cache reads | Window (credits per 1%) | Sonnet rate | Fable rate | Cache-read weight | Residual \|rel\| median |
|---|---|---|---|---|---|---|---|
| a2, before 14 Sep | 41 | at 0 | 189,609 [185,050, 193,757] | 0.617 [0.521, 0.752] | 1.170 [1.079, 1.285] | held at 0 | 0.055 |
| a2, before 14 Sep | 41 | **fitted** | 231,411 [220,309, 255,872] | **0.558 [0.365, 0.635]** | 1.307 [1.236, 1.434] | **0.0118 [0.0088, 0.0189]** | 0.044 |
| a2, after 14 Sep | 14 | at 0 | 270,028 [244,017, 293,986] | 0.525 [0.345, 0.831] | 2.532 [2.216, 2.883] | held at 0 | 0.065 |
| a2, after 14 Sep | 14 | **fitted** | 314,164 [278,700, 353,120] | **0.502 [0.327, 0.832]** | 2.641 [2.197, 3.036] | **0.0088 [0.0007, 0.0161]** | 0.050 |
| a3, after 14 Sep | 14 | at 0 | 165,647 [157,477, 175,991] | 0.518 [0.359, 0.672] | 1.284 [1.165, 1.895] | held at 0 | 0.060 |
| a3, after 14 Sep | 14 | **fitted** | 165,647 [159,510, 176,910] | **0.518 [0.358, 0.662]** | 1.284 [1.179, 1.869] | **0.0000 [0.0000, 0.00003]** | 0.060 |

`a2` and `a3` are the published labels of the two gs accounts (the third, `a1`, has no usable
capture column and its fit is reported for completeness and never adopted: its joint fit's
residual |rel| median is 0.447 before the cut against 0.044 to 0.060 here, which is the phantom
meter movement showing up as fit error).

Three things follow, and the third is the headline.

**The weight is real on one account and zero on another.** a2 fits 0.0118 and 0.0088 with
intervals that exclude zero; a3 fits exactly zero, with the constraint binding (the unconstrained
coefficient is negative there). Those intervals do not overlap, so there is no single measured
weight to pool: `measured_rates.cache_read_weight` publishes no value, the union interval
[0.0000, 0.0189] and the fits behind it. The publisher still prices every stretch with cache
reads at `data/prices.json`'s `cache_read_weight`, which is 0, and the block says so
(`used_in_pricing: false`). Changing the pricing weight would move the window itself, which is a
larger change than this one and belongs with the reconciliation.

**Fitting the weight lowers the Sonnet rate and raises the window.** On a2's 41 pre-cut
stretches the Sonnet rate falls from 0.617 to 0.558 and the fitted window rises from 189,609 to
231,411 credits per 1%, with the residual |rel| median improving from 0.055 to 0.044. The
cache-read charge was being carried by the other columns, exactly as the review said it would be.

**The joint fit no longer excludes the table's 1.667 for Opus:Sonnet.** Stated plainly, because
it was the premise of this change:

| Fit | Opus ÷ Sonnet, cache reads at 0 | Joint fit | 1.667 |
|---|---|---|---|
| a2, before | 1.081 [0.886, 1.280] -- excludes 1.667 | 1.195 [1.049, 1.820] | **inside** |
| a2, after | 1.271 [0.803, 1.933] | 1.328 [0.802, 2.041] | inside |
| a3, after | 1.288 [0.988, 1.815] | 1.288 [1.005, 1.846] | inside |

So the one interval that excluded the table's ratio excluded it only while cache reads were held
at zero. With the weight fitted, all three intervals contain 1.667 and all three point estimates
are still below it (1.195 to 1.328), and all three are far below the API list ratio of 2.5, which
no interval reaches. The measured Sonnet rate remains above the table's 0.400 in every fit; what
it no longer does is contradict the table's ratio. The page divides by the measurement either
way, and now publishes the interval that says how much it knows.

## 3. The rates the page now divides by

From `history/model-rates.json` `measured_rates`, pooled over the three gs fits at output 5x. A
family's rate is the median of the per-fit point estimates with the union of their intervals, and
only where every fit's interval for it overlaps every other's; where they do not, the family
keeps the interval and publishes no value.

| Family | `rate_source` | Rate, credits per input token | Interval | n | Reference figure beside it | Status |
|---|---|---|---|---|---|---|
| Opus | reference | 0.6667 (10/15) | none | -- | 10/15, the same row | the unit anchor; not tested here |
| Sonnet | measured | **0.5178** | [0.3266, 0.8316] | 3 fits, 69 stretches | 0.400 (6/15) | measured; 0.400 is inside the interval and below all three point estimates |
| Haiku | measured | -- | -- | 0 fits | 0.133 (2/15) | **not measurable, no clean stretch is Haiku-heavy** |
| Fable | measured | -- | [1.1792, 3.0356] | 3 fits, 69 stretches | absent from the table | **rate not yet identified** |

Output rates are five times input throughout, which is both the reference table's ratio for every
family in it and the ratio the fit assumes. For Fable the output ratio is still not separable, so
the published output interval spans the cheaper candidate against the low edge and the dearer
against the high one (3x and 5x), as the row has done since PR #65.

**Sonnet**, per fit: 0.558 [0.365, 0.635] (n=41), 0.502 [0.327, 0.832] (n=14), 0.518
[0.358, 0.662] (n=14). The intervals overlap, so they pool: 0.5178, union [0.3266, 0.8316]. At
output 3x the same pooling gives 0.459 [0.303, 0.891], and with cache reads held at zero 0.525
[0.345, 0.831]; both are in `history/model-rates.json` (`adopt_output_3x`,
`adopt_cache_read_zero`) and neither is published.

**Haiku** is not measurable and the row says so instead of carrying a number. The highest Haiku
share of any clean stretch on a fitted account is **0.000** -- Haiku appears in no gs fit at all
-- and the highest anywhere is 0.111, on the account that prices nothing absolute. The reference
figure of 2/15 is still published beside the sentence, for the page to draw; nothing divides by
it, so no Haiku token figure is published at all.

**Fable** has three fits that disagree by more than their intervals: 1.960 [1.854, 2.151] times
Opus before 14 September, 3.962 [3.296, 4.553] after on the same account, and 1.927
[1.769, 2.803] on the other. The pre/post pair does not overlap, so the row publishes the union
interval and the words "rate not yet identified" -- a median of the three would be a rate no fit
measured. The data cannot say whether Fable's rate moved or the five-hour window did: the window
fitted on the same stretches moves the same way (231,411 to 314,164 credits per 1%).

Fable's rate is also solved a second way, per Fable-heavy stretch against the measured window
(`credits.fable_interval`, PR #65), which gives 1.19 to 2.36 credits per token. That block is
still published, unchanged, beside the fit. The fit's interval contains the solve's and extends
above it. The per-model row divides by the fit, because the fit is the instrument that also
produces Sonnet's rate and the only one that can say whether the two sides of 14 September
agree.

## 4. What moved on the page

`credits` grows from 496 published leaves to 911: 52 figures moved, 6 left, 421 are new. The
measured five-hour window itself did **not** move -- it is 19,543,887 credits [17,250,018,
20,819,693] over n=11, computed from pure-Opus stretches that touch only the anchor -- and
neither did `five_hour_window_across_cut`, `window_credits_from_weekly`, `fable_interval`, the
Opus rows or any dollar-derived figure.

**Sonnet: −22.7% of its tokens per window, and it gains an interval.**

| Figure | Old (reference rate) | New (measured rate) |
|---|---|---|
| `per_model.sonnet.credits_per_token.input` | 0.4 | 0.5177756137802535 |
| `per_model.sonnet.credits_per_token.output` | 2.0 | 2.5888780689012676 |
| `per_model.sonnet.credits_per_token_interval.input` | null | [0.3266, 0.8316] |
| `per_model.sonnet.tokens_per_window.input.value` | 48,859,718 | 37,745,862 |
| `per_model.sonnet.tokens_per_window.input.interval` | [43,125,045, 52,049,232] | [20,741,988, 63,737,007] |
| `per_model.sonnet.tokens_per_window.output.value` | 9,771,944 | 7,549,172 |
| `per_model.sonnet.tokens_per_window.output.interval` | [8,625,009, 10,409,846] | [4,148,398, 12,747,401] |
| `per_model.sonnet.api_value_per_window_usd.input.value` | 97.72 | 75.49 |
| `per_model.sonnet.api_value_per_window_usd.input.interval` | [86.25, 104.1] | [41.48, 127.47] |
| `per_model.sonnet.api_value_per_window_usd.output.value` | 97.72 | 75.49 |
| `per_model.sonnet.api_value_per_window_usd.output.interval` | [86.25, 104.1] | [41.48, 127.47] |
| `sessions.claude-sonnet-5.credits_per_token_at_split` | 0.0186 | 0.02407657 |
| `sessions.claude-sonnet-5.tokens_per_window_at_split.value` | 1,050,746,613 | 811,738,973 |
| `sessions.claude-sonnet-5.per_window.value` | 1,817.6 | 1,404.2 |
| `sessions.claude-sonnet-5.per_week.value` | 8,979.1 | 6,936.6 |

The new interval is much wider than the old one, and honestly so: the old interval carried only
the window's spread, because the rate it divided by was a table row with no uncertainty at all.

**Haiku: the number goes, the sentence arrives.** Six figures that were numbers are now null with
a status, and four interval endpoints are gone because there is no rate to invert.

| Figure | Old | New |
|---|---|---|
| `per_model.haiku.credits_per_token.input` | 0.1333 | null |
| `per_model.haiku.credits_per_token.output` | 0.6667 | null |
| `per_model.haiku.tokens_per_window.input.value` | 146,579,152 | null, status "not measurable, no clean stretch is Haiku-heavy" |
| `per_model.haiku.tokens_per_window.output.value` | 29,315,830 | null, same status |
| `per_model.haiku.tokens_per_window.input.interval` | [129,375,135, 156,147,698] | removed: no rate to invert |
| `per_model.haiku.tokens_per_window.output.interval` | [25,875,027, 31,229,540] | removed: no rate to invert |
| `per_model.haiku.status` | null | "not measurable, no clean stretch is Haiku-heavy" |

**Fable: the interval is the fit's, not the solve's.**

| Figure | Old (solved) | New (fitted) |
|---|---|---|
| `per_model.fable.credits_per_token_interval.input` | [1.1935, 2.3631] | [1.1792, 3.0356] |
| `per_model.fable.credits_per_token_interval.output` | [3.5805, 11.8155] | [3.5377, 15.1780] |
| `per_model.fable.tokens_per_window.input.interval` | [7,299,741, 17,444,234] | [5,682,574, 17,655,447] |
| `per_model.fable.tokens_per_window.output.interval` | [1,459,948, 5,814,745] | [1,136,515, 5,885,149] |
| `per_model.fable.api_value_per_window_usd.input.interval` | [73.0, 174.44] | [56.83, 176.55] |
| `per_model.fable.api_value_per_window_usd.output.interval` | [73.0, 290.74] | [56.83, 294.26] |
| `sessions.claude-fable-5-1.per_window.interval` | [22.9, 66.8] | [17.8, 67.6] |
| `sessions.claude-fable-5-1.per_week.interval` | [113.2, 330.1] | [88.1, 334.1] |
| `sessions.claude-fable-5-1.tokens_per_window_at_split.interval` | [156,983,678, 457,853,905] | [122,205,900, 463,397,558] |
| `sessions.claude-fable-5-1.credits_per_token_at_split_interval` | [0.04547235, 0.10988415] | [0.04492836, 0.14115536] |

`rates.source` gains the sentence saying the table is the reference and `measured_rates` holds
what the page divides by. Nothing else that was published changed.

**New:** `credits.measured_rates` (the rates, their intervals, the fits behind them with account
labels rather than names, and the fitted cache-read weight); `rate_source`, `anchor`,
`reference_rate` and `measured_rate` on every `per_model` row; `rate_source` on every `sessions`
row; `rates.role` and `rates.used_in_pricing`; and `credits.effort_credits`.

## 5. The effort matrix in credits

`credits.effort_credits` prices each effort cell's own runs at the same measured rates, cache
writes on the input side and cache reads at the published weight, and publishes the median over
the cell's runs with `percent_of_window` beside it. Opus cells read `rate_source: "reference"`
(the anchor), Sonnet's read `measured`, and Fable's publish the interval and the status sentence
with no value, exactly as the per-model rows do.

| Cell | Median credits | Interval | Percent of a five-hour window |
|---|---|---|---|
| Opus low | 10,228 | -- | 0.05 [0.05, 0.06] |
| Opus medium | 14,305 | -- | 0.07 [0.07, 0.08] |
| Opus max | 62,955 | -- | 0.32 [0.30, 0.36] |
| Sonnet low | 4,454 | [2,810, 7,154] | 0.02 [0.01, 0.04] |
| Sonnet medium | 6,986 | [4,407, 11,221] | 0.04 [0.02, 0.07] |
| Sonnet max | 70,449 | [44,444, 113,154] | 0.36 [0.21, 0.66] |
| Fable low | no value | [7,693, 33,003] | no value, [0.04, 0.19] |
| Fable max | no value | [112,011, 448,586] | no value, [0.54, 2.60] |

One thing this makes visible immediately, and it is **not resolved here**: the matrix's own
recorded meter movement per cell (`data/effort_matrix.json` `_meta.usage_deltas`) is two to five
times what the same runs price at. Opus medium recorded 2.0% of the meter over seven runs where
the credits predict 0.49%; Opus xhigh 8.0% against 1.54%; Sonnet max 13.0% against 2.52%. Those
deltas are whole-percent readings taken across each cell's wall-clock time on a live account, so
they bound the cells rather than measure them -- but a factor of two to five is larger than that
explains, and nothing in this change accounts for it. It is worth its own look.

## 6. Limits

- **Every rate is relative to Opus, and Opus is the reference table's.** Nothing here tests the
  absolute scale. Section 1 says what follows if that row is wrong.
- **One reference rate is still in an arithmetic, and it is not a figure the page states.**
  `tracker.credits.price_tokens` prices a stretch's tokens with the reference table, which
  `window_credits` (pure Opus, so the anchor only), `across_cut` and the Fable solve all use.
  Those two keep the reference table on purpose: the measured rates are outputs of a fit over
  those same stretches, and Haiku has no measured rate at all, so pricing a stretch with a
  missing rate would silently drop it from the selection instead of publishing its absence. The
  across-the-cut comparison holds one table on both sides by design and its level is not a figure
  the page states. This is the one place left and it is stated on the row, in the module
  docstring and here.
- **The fitted cache-read weight is published but not priced with.** The stretches are still
  priced at a weight of 0. The two accounts' fitted weights do not agree, and adopting either
  would move the window, `across_cut` and the Fable solve at once.
- **The joint fit prices a cache read at one rate for every family**, where
  `tracker.credits.input_side` scales a cache read by the family's own input rate. A family whose
  cache reads are dearer or cheaper than Opus's loads that difference onto the shared column.
  One column rather than four because cache reads are about 97% of a stretch's tokens and the
  families' shares of them move together; four columns would be four collinear columns of the
  same traffic.
- **The fits assume one input rate and one output rate per family**, with output a fixed multiple
  of input (5, with 3 computed beside it). A model whose output ratio differs loads that
  difference onto its input rate.
- **The meter reads whole percent**, so every stretch carries about ±5% of quantisation at these
  delta_pct values; that is the floor under every per-stretch figure.
- **An offline rebuild reads the running checkout's measured rates.** `tracker/rebuild_offline.py`
  passes the archive's own price table but has no `model_rates` argument yet, so a rebuild of an
  old archive divides by today's rates. `tracker.publish.build_public_json` takes the block as a
  parameter, so this is one line in a change that owns that file.
- **gs has no ruff.** `ruff` runs locally before the gate.

## 7. Verification

```
$ python3 -m unittest tests.test_credits tests.test_credits_report tests.test_publish \
      tests.test_model_rates tests.test_harness_runs tests.test_rebuild_offline
Ran 270 tests in 0.505s
OK

$ python3 -m unittest discover -s tests -t .          # the whole suite: data/prices.json moved
Ran 820 tests in 8.696s
OK (skipped=1)

$ python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
      --gs-passive history/gs-passive.json --out /tmp/claude-usage-56.json
$ python3 tools/credits_report.py --publish-check /tmp/claude-usage-56.json
All 911 figures reproduce from the history files.
```

The publish check reads the measured rates from `history/model-rates.json`, never from the
published JSON, so a per-model row published at a rate the committed fit does not hold fails it;
`tests/test_credits.py` covers both that and a row published at the reference rate instead.

`python3 -m tools.harness_runs --print` reproduces the committed `history/harness-runs.jsonl`
byte for byte after the dedup change below.

## 8. Also in this change: the run-file dedup now includes time

The gate on PR #66 left a note: `tools/harness_runs.py` `collect()` matched a completed run in
the ops log against a `history/probes.jsonl` row on account, model and rounded tokens per 1%
only. Those three repeat -- the probe runs the same model on the same account night after night
-- so two runs that happened to read the same rounded tokens per 1% would collapse into one, and
the dropped run's span would then excuse no stretch, which is the whole purpose of the file. The
key now also requires the two spans to sit within one five-hour window of each other
(`same_run`, `MATCH_TOLERANCE`). The log run's span is a bracket that can be a whole window wide,
which is why the test is proximity rather than containment. `tests/test_harness_runs.py` covers
`collect()` itself: a row of the same window is deduplicated, a row three days away is not, a row
of another account, another model or another reading never is, and the aborted run in the same
log still becomes a row of its own.

## 9. 2026-09-20 follow-up: disagreeing fits publish a value now, not a status sentence

`tools.model_rates.adopt` used to withhold a family's rate entirely when its per-fit intervals
did not all overlap (`agree: False`): the family kept the union of the per-fit intervals but no
point value, and the publisher printed `"rate not yet identified"` in its place. Fable is the
family this hit -- its jwork-pre, jwork-post and dave-post fits disagree by more than their own
intervals -- and the published page carried that sentence instead of a figure.

The rule now publishes a value whether or not the fits agree: the median of the per-fit rates,
with the union of their intervals as before, `status: null`, and `agree: false` kept on the
record along with a `why` sentence naming the disagreement. `agree` and `why` are what a reader
checks to see how much to trust the median; they no longer gate whether a number appears at all.
`NOT_IDENTIFIED` is gone from `tools/model_rates.py` -- the rate-not-yet-identified shape (an
interval with no value) could never actually occur once `n_fits >= 2` implies a median, so the
constant was dead code. `NOT_MEASURABLE` (Haiku: no clean stretch is Haiku-heavy at all) is
unchanged.

Regenerating `history/model-rates.json` from the same command as before now publishes Fable at
**1.3068 credits per input token, 1.960x Opus**, interval **[1.179, 3.036]** (**1.769x to
4.553x** Opus), from three fits -- dave/post 1.927x [1.769, 2.803], jwork/post 3.962x
[3.296, 4.553], jwork/pre 1.960x [1.854, 2.151] -- median 1.960x, union interval as stated.
`agree` is `false`. Every consumer of `measured_rates.per_family` (`tracker/credits.py`'s
`family_rate`, `window_tokens`'s per-family conversion, `tracker/publish.py`'s per-model rate
row) reads `input`/`interval`/`status` generically and needed no change: a family with a value
and an interval was always the primary published shape, and Fable now takes it. The Fable line
of the tokens-per-window block accordingly carries a token figure and an interval rather than
the status sentence.

The masterrig ungated joint fit (`tools/masterrig_fable.py`, PR #70, Fable/Opus 2.56, 80%
interval 2.07 to 3.45 on 70 rows since 2026-09-06) was considered as a fourth Fable fit point.
It is not wired in: it is a different pipeline on a different account (its own claude-opus-4-7
column, its own row gate, no `capture_status` filter, `tools/rounding_feasibility.py`'s LP) that
this file's own `FIT_ACCOUNTS = ("jwork", "dave")` and `group_fits` do not accept a masterrig
entry from, and masterrig's own fit through *this* file's ordinary route is explicitly excluded
from adoption (§6, residual median 0.455 against 0.055-0.065). Folding its ratio in as a fourth
point would mean adapting `adopt`/`group_fits` to accept a fit from outside `s3`'s own shape,
which is more than the small change this pass scoped for. The published Fable rate stays the
three gs fits above.

Tests: `tests/test_model_rates.py`'s `PoolingTests` covers the new median-and-union shape
directly (`test_fits_that_do_not_agree_still_publish_the_median_with_the_union_of_their_intervals`,
`test_a_family_no_fit_carries_is_not_measurable_and_a_disagreeing_one_still_publishes`);
`tests/test_credits.py`'s `SolvedFableCarryThroughTests` and `PublishedBlockTests` were updated
from asserting the status sentence to asserting the published value and interval.
