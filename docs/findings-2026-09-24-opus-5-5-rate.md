# Opus 5.5's rate, and rates that follow the data on their own, 2026-09-24

Four changes to how the per-model credit rates are fitted, published and used. Opus 5.5 now
has a published rate, marked provisional. Change detection now counts the stretches that
carry Opus 5.5 tokens, where before it dropped them. New accounts and new model ids join the
fits without anyone editing the code, and the publisher refits the rates every day.

## Opus 5.5 is published as a provisional rate

The pooled fit now runs over every account. Opus 5.5 appears in 9 of its stretches, from
three groups: avis, jwork and masterrig, all after 14 Sep. The fit puts Opus 5.5 at 0.873x
Opus, with an 80% interval of [0.627x, 1.094x]. The high end of that interval is 1.75x the
low end, and the published-rate rule needs under 1.5x, so until now the rate was withheld.

`tools/model_rates.py` now publishes a provisional rate for a family newer than the January
reference table when that family's interval is under `PROVISIONAL_MAX_RATIO = 3.0`. Today
that means Opus 5.5. It also covers any family created automatically for a new model id (see
below). The row carries `provisional: true`, the value, the interval, and this status
sentence:

    provisional: Opus 5.5 0.873x Opus, 80% interval [0.627x, 1.094x], 1.75x wide end to end;
    a rate is final when its interval is under 1.5x

The flag clears on the first refit whose interval is under 1.5x. Sonnet, Haiku and Fable
keep the single rule, and Haiku is still withheld.

## Detection values Opus 5.5 at its list-price ratio

Until now `tracker/gs_passive.py` `stretch_credits` returned `(None, "unpriced")` for every
stretch holding any `claude-opus-5-5` tokens. Change detection dropped all of those
stretches. The function now tries four rate sources in this order: the measured rate, the
midpoint of the interval, the reference table, and the family's API list-price ratio to the
Opus anchor from `data/prices.json`. The last one is a new rate source, `inferred_list_price`.
For Opus 5.5 that ratio is 0.8x: $4 against $5 per million input tokens.

A provisional rate does not value a stretch. Every Opus 5.5 stretch comes after the
22 September five-hour change, so a rate fitted from them would absorb that change. The list
ratio is fixed in advance. A family moves to its measured rate once that rate is no longer
provisional.

Claude Code's `<synthetic>` placeholder rows carry the model id `<synthetic>` with zero
tokens, and they belong to no family. Before this change, one of them made its whole stretch
unpriced. A bundle with zero tokens is now skipped. A `<synthetic>` bundle that carries
tokens is still unpriced, and it is named in the list described below.

## New accounts and new models need no edit

- **Fit accounts.** The hard-coded `FIT_ACCOUNTS = ("jwork", "dave", "masterrig")` is gone.
  `fit_accounts` returns every account in the passive sources: today avis, dave, jwork and
  masterrig. masterrig still enters only from `MASTERRIG_FROM`. avis has 4 clean stretches,
  below `MIN_FIT_N = 8`, so it gets no per-account fit yet but does enter the pooled fit.
  `tools/holdout.py` and `tools/cache_read_profile.py` now take their accounts and groups
  from the data too.
- **Model families.** Each family in `data/prices.json` `_credits.per_family` now has a
  `members` list of the model ids already filed under it. A current-style id
  (`claude-<name>-<version>`, optionally with a date suffix) that is in no `members` list
  becomes its own family in `tracker/credits.py` `family()`. The family is keyed by the id
  without `claude-` and without the date, so `claude-sonnet-5-5` becomes `sonnet-5-5`.
  Previously a substring match would have credited it at Sonnet 5's rate.
  `tools/model_rates.py` fits the new family from the first run that sees it, and detection
  values it at its list-price ratio until then. It cannot be valued if `data/prices.json`
  has no price for it.
- **Visible drops.** The publisher's `evidence.credit_detection` has a new `unpriced_models`
  list. It names every model id that no rate can value and whose stretches are left out of
  detection. The list is empty today.
- **Daily refit.** `bin/daily.sh` runs `tools.model_rates` under `nice` before the
  publisher, once a day. It skips the run when `history/model-rates.json` was already
  generated that day. It commits `history/model-rates.json` with the data rows. If the refit
  fails, a warning is printed and the publisher uses the previous rates.

## What moved on the page

To get these figures the publisher was run twice over the same committed history files.
The first run was at `main` (52ad06b) with its committed `history/model-rates.json` from
2026-09-23 20:12. The second was on this branch after the refit.

```
python3 -m tools.model_rates history/masterrig-passive.json --json history/model-rates.json
python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
    --prices data/prices.json --gs-passive history/gs-passive.json \
    --masterrig-passive history/masterrig-passive.json --out <scratch>/after.json
python3 tools/credits_report.py --publish-check <scratch>/after.json
```

| published figure | before | after |
|---|---|---|
| `credits.per_model.opus-5-5.rate_source` | `inferred` (list price) | `measured`, provisional |
| `credits.per_model.opus-5-5.credits_per_token.input` | 0.53333 | 0.58223 |
| `credits.per_model.opus-5-5.credits_per_token_interval.input` | none | [0.41772, 0.72952] |
| `credits.per_model.opus-5-5.tokens_per_window.input` | 35,178,996 [15,480,589, 37,475,449] | 32,257,978 [11,329,271, 47,897,949] |
| `credits.per_model.opus-5-5.api_value_per_window_usd.input` | $140.72 [$61.92, $149.90] | $129.03 [$45.32, $191.59] |
| `credits.sessions.claude-opus-5-5.per_window` | 245.6 [108.1, 261.7] | 225.3 [79.1, 334.5] |
| `credits.window_tokens.per_family.opus-5-5.all` | 540,712,560 [208,219,824, 650,896,667] | 495,815,569 [152,383,026, 831,921,092] |
| `credits.measured_rates.per_family.sonnet.times_opus` | 0.553 [0.455, 0.640] | 0.540 [0.449, 0.631] |
| `credits.measured_rates.per_family.fable.times_opus` | 2.111 [2.004, 2.249] | 2.101 [2.007, 2.236] |
| `credits.measured_rates.fits_pooled` | 5 groups | 6 groups (a4/post added) |
| `credit_detection.points` | 70 | 82 |
| `credit_detection.meter_pct` | 731.0 | 854.0 |
| `credit_detection.rate_sources` | anchor 3, measured 49, reference_table 18 | anchor 3, measured 50, reference_table 18, inferred_list_price 11 |
| `credit_detection.unpriced_models` | absent | [] |
| `credits.announced_change`, Opus 5.5 candidate, state | measuring | provisional |
| same, change | none | +39.2% [+7.1%, +80.8%], a1 and a2 combined |
| same, a1 | 32 before, 0 after, 5 after unpriced | 45 before, 5 after, +45.1% [+8.0%, +94.8%] |
| same, a2 | 41 before, 0 after, 3 after unpriced | 41 before, 3 after, +16.8% [-36.3%, +114.3%] |
| same, a3 | 51 before, 0 after | 52 before, 0 after |
| same, a4 | 0 before, 12 after unpriced | 0 before, 12 after |
| `last_change` | weekly, decreased 26%, 2026-09-11 | unchanged |
| `last_change.five_hour_window_credits.per_account.a2` | 26 after, 0.0% | 28 after, +0.2% |
| `last_change.five_hour_window_credits.per_account.a4` | 0 after | 5 after |

This change did not move the Sonnet and Fable rates. Refitting the same history with the
code at `main` gives the same 0.540 and 2.101, because the committed file was a day old.
There are 12 new detection points. 10 are Opus 5.5 stretches valued at the list ratio. The
other 2 had been dropped because of an empty `<synthetic>` bundle, and 1 of those 2 also
holds Opus 5.5. The empty-`<synthetic>` rule is also why the announced-change before sides
grow: a1 from 32 to 45 stretches and a3 from 51 to 52.
`tools/credits_report.py --publish-check` reproduces all 19,239 figures in the new file from
the history files.

## Inputs

No traffic was sent. The inputs were the committed `history/gs-passive.json` (143 jwork, 52
dave and 12 avis stretches) and the committed `history/masterrig-passive.json`, used without
a rebuild.
