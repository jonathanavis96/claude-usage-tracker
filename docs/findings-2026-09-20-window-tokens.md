# What a five-hour window buys in tokens, 2026-09-20

The page has stated "38M input tokens per 5-hour window" and "7.5M output tokens" for Max 20x on Sonnet. Neither is a measurement of what a window holds. Both are the whole 19.5M-credit window spent on nothing but one token class of one model, and no account's use looks like that. This document records the direct measurement that replaces them, what the old figures actually answered, and why a third figure already on the page — `credits.sessions.<model>.tokens_per_window_at_split`, 630M for Opus — is larger again without either being wrong.

Reproduce section 5 of `python3 tools/reconcile_window.py history/masterrig-passive.json`, or `python3 tools/credits_report.py`. The publisher writes the same figures to `credits.window_tokens`.

## The measurement

The cluster is the one `credits.window_credits` is the median of: the capture-accepted, harness-clean, pure-Opus stretches of every watched account. Eleven of them, ten on jwork and one on Dave; masterrig contributes none, because its nine pure-Opus stretches do not pass the capture gate (they read 1,917 to 86,003 credits per 1% against jwork's 175,934 to 208,197, the phantom cluster the reconciliation describes).

Each stretch carries its own token counts and its own meter movement, so tokens per 1% of the five-hour meter times 100 is a full window, with no rate and no class weight anywhere in it. Per window, medians over the cluster:

| per window, Max 20x | jwork (n=10) | Dave (n=1) | pooled (n=11) |
|---|---|---|---|
| all four classes | 462,184,345 | 539,959,273 | **473,774,890** (398,250,355 to 542,413,890) |
| cache_read | 444,344,115 | 527,998,755 | 455,952,510 |
| cache_write | 15,253,562 | 8,473,800 | 15,101,975 |
| output | 2,776,515 | 3,478,627 | 2,794,500 |
| input | 5,783 | 8,091 | 5,800 |

The interval is the spread of the readings: per account its own lowest and highest per-stretch value, pooled the union of the two account intervals. It is not a confidence interval — there is no error model here, only eleven readings of the same quantity, and two accounts on the same plan that differ from each other by more than either's own spread.

**The per-class row is the finding, not a decoration.** Cache reads are 96.1% of the tokens (95.5% to 97.8%), and cache reads are free in credits. A window is 456M cache reads, 15M cache writes, 2.8M output and 5,800 fresh input tokens. Any single number stated without that shape is a number about some other mix.

## Why "38M input tokens" was a scenario and not a measurement

`credits.per_model.sonnet.tokens_per_window.input` is `window_credits / sonnet_input_rate` = 19,543,887 / 0.5178 = 37,745,862. That is a correct answer to a question nobody asked: *if a five-hour window were spent on nothing whatever but fresh Sonnet input tokens — no cache, no output — how many would fit?* The output figure, 7,549,172, is the same window spent on nothing but Sonnet output.

Two things follow.

1. **The quantity is not what the page's wording claims.** The cluster's own input class is 5,800 tokens per window. The route to 38M does not measure the input class; it prices the whole budget at the input rate. Those figures stay in `credits.per_model`, where each is labelled by the class it prices and by the rate it divided by, and the page's headline moves to `credits.window_tokens`.

2. **It sat beside a chart drawing a different route entirely.** The "Tokens per week" chart reads `rates[model].tokens_per_window`, the legacy list-price derivation on the frozen reference mix — 1,497,961,943 per window for Sonnet — while the card multiplied the credits route by the measured windows per week (37,745,862 × 4.95 = 186,842,017). One page, two routes, an order of magnitude apart, neither labelled. `rates[model].tokens_per_window` now carries `deprecated: "read credits.window_tokens; removed after the page moves"`, and it and `history[model][].tokens_per_window` are removed in a follow-up once the page no longer reads them.

## How this differs from `sessions.<model>.tokens_per_window_at_split` (Opus 630M)

That figure is not wrong and is not the same question. It prices a window at the *passive split* — `history/passive.json`'s `split`, the watched accounts' own token-class shares across all their work — and reports how many tokens of that mix a window holds:

| | cache_read | cache_write | input | output | credits per token | tokens per window |
|---|---|---|---|---|---|---|
| passive split (`sessions`) | 0.9702 | 0.0254 | 0.0001 | 0.0042 | 0.031 | 630,447,968 |
| the clean stretches' own mix | 0.9622 | 0.0319 | 0.0000 | 0.0059 | 0.0409 | 473,774,890 (measured) |

The split is heavier in cache reads (0.9702 against 0.9622) and lighter in output (0.0042 against 0.0059) than the pure-Opus clean stretches are. Cache reads cost nothing and output costs five times input, so a mix shifted that way buys more tokens for the same credits: 0.031 credits per token against 0.0409, and 630M tokens against 474M. Both are arithmetic on the same 19.5M-credit window; they differ only in the mix assumed.

**The direct figure is the measurement.** `window_tokens` divides the stretches' own token counts by their own meter movement and assumes no mix at all; `tokens_per_window_at_split` assumes one and says so (`cache_normalised`, and the split is published in the same object). As a cross-check the two routes agree where they should: pricing the clean stretches' own mix at the Opus anchor gives 0.0409 credits per token, and 19,543,887 / 0.0409 = 477,693,095 — 0.84% from the 473,774,890 measured directly, which is the gap between a median of ratios and a ratio of medians.

## The other families

Only Opus is measured. Every other family's row is the Opus figure converted at the two families' rates, at the same token-class mix, and each says so in its own `conversion` sentence:

| family | rate source | per window | interval |
|---|---|---|---|
| opus | anchor | 473,774,890 | 398,250,355 to 542,413,890 |
| sonnet | measured | 610,013,137 | 319,246,197 to 1,107,023,622 |
| fable | envelope | none: rate not yet identified | 87,462,218 to 379,608,439 |
| haiku | none | none: not measurable, no clean stretch is Haiku-heavy | — |

Sonnet's interval is wide because the measured Sonnet rate's own interval is wide (0.327 to 0.832 credits per input token), and that widens on both sides at once against the window's spread. Fable's fits do not agree within their intervals, so it publishes the envelope and no value; Haiku has nothing to measure a rate from at all, and publishes the sentence saying so. Neither ever publishes the middle of an interval as if it were a number.

`per_week` multiplies each of these by the measured windows per week the page's headline already uses (4.95, interval 4.65 to 5.29, `weekly_windows.max20.current`): 2,345,185,706 Opus tokens a week, 3,019,565,028 Sonnet. With no measured windows per week, `per_week` carries a status and nulls.

## What this does not settle

- **One account of eleven readings is one account.** Ten of the eleven stretches are jwork's; Dave's single stretch reads 17% higher, and the reconciliation's unexplained jwork-against-Dave gap is inside this figure exactly as it is inside `window_credits`. It is published as the interval, not averaged away.
- **The mix is the clean stretches', not "typical use".** These are pure-Opus stretches that moved the meter at least 3%: long, cache-warm sessions. A page stating 474M tokens a window is stating what that kind of work buys.
- **Nothing here tests the Opus anchor.** The pure-Opus figure needs no rate, so the anchor cannot move it; but every converted family's row scales with the anchor, and nothing in our data tests it (`docs/findings-2026-09-20-measured-rates.md`).
