# The jwork/dave gap and the cache-read weight, 2026-11-20

Closes the question open since 2026-09-23: explain why jwork's rates under-predict dave's
meter before adopting any pooled cache-read weight, then decide 0.003 against 0. Investigation
only: no weight, rate or published figure changes here.

Data: the history committed on `main` at 0bbdf887 (publisher state of 2026-11-19 23:33Z;
`history/model-rates.json` generated 2026-11-19 07:35Z). Commands, read-only:

```
python3 -m tools.holdout --json <file>     # 7 s on MasterRig
```

## 1. The gap is still there, and #137 did not close it

Two explanations were open after `docs/findings-2026-09-23-holdout.md`: dave's meter counting
work its gs transcripts do not hold, or dave's meter being a different size for the same work.
#137 (2026-10-23) counts each account's work on every host, which is the first explanation's
fix. The holdout on today's history, same code, same seed:

| Direction, after 14 September | 2026-09-23 (n) | 2026-11-20 (n) |
|---|---|---|
| jwork -> dave, median predicted / actual | 0.764 (32) | **0.506** (103) |
| dave -> jwork, median predicted / actual | 1.127 (26) | **1.738** (59) |
| jwork -> dave, inside the 80% interval | 0.41 | 0.13 |

Split by the month the held-out stretch starts, jwork -> dave reads 0.692 on dave's 39
September stretches and 0.458 on its 64 October ones. The gap did not shrink once all of
dave's work was counted; it grew. Missing transcripts are not the explanation.

Most of it is the five-hour meter charging headless work more, plus a smaller offset that is
still unexplained. `docs/findings/2026-10-04-account-agreement.md` (#139) measured both:

- Credits per 1% of the **seven-day** meter agree across jwork, dave and avis within -3% to +9%
  in every regime, while credits per 1% of the **five-hour** meter do not (22-29 September:
  jwork 278k, dave 164k, avis 287k).
- Its section 4 traces most of that to headless `claude -p` work: the five-hour meter charges
  it about 1.5x as much as interactive work, on every account, and dave's work is 97% headless
  after 22 September. Allowing for the headless share takes dave's excess from 1.33x to
  **1.10x [1.03, 1.18]**. That last tenth is a per-account offset nobody has explained.

`tools/holdout.py` fits raw transcript tokens, with no headless factor, so its 0.506 carries
both parts: the workload-dependent headless effect and the remaining offset. It cannot say
how much of either is left in the rates the tracker publishes.

What the published rates see is different. `tools/model_rates.py` fits them in the five-hour
unit (`five_hour_unit`, `stretches_in_five_hour_unit`: headless tokens counted at the fitted
headless factor, ADR 0001 rule 12), so the headless effect is modelled before the fit rather
than cancelled by it. The remaining 1.10x is account-wide, and the rates are stated relative
to the Opus anchor with one free scale per account and side of each change, so a constant
offset of that kind cancels out of them (the 2026-09-23 note's point 3). Whether the offset is
constant across workloads has not been tested; a holdout run in the five-hour unit would
test it and is not done here.

## 2. Two cache-read weights are in force: 0 for reference credits, the pooled fit for comparisons

**Reference pricing uses 0.** The stretches' credit figures price cache reads at
`data/prices.json` `_credits.cache_read_weight`, which is **0** (range [0, 0.015]), read by
`tracker/credits.py` `cache_read_weight()` and `tools/model_rates.py` (`CACHE_READ_WEIGHT = 0.0`).
This is what `tracker/publish.py` means when it marks the published `cache_read_weight` block
`used_in_pricing: false`.

**Every comparison uses the pooled fitted weight.** `tracker/credits.py` `comparison_value`, the
valuation every change figure prices a stretch with, values each cache read at the pooled fit's
`cache_read_rate` whenever a pooled fit exists (`pooled_fit_prices`: the pooled
`cache_read_weight` times Opus input). It falls back to the 0 reference weight only without one.
Its callers include the four-account detector (`tracker/four_accounts.py` `valuation`), whose
changes are the published headline and change figures, the five-hour headless factor and the
new-family joint fits in `tracker/publish.py`. ADR 0001 rule 23 keeps this: Opus 5.5's cache
reads share the one fitted cache-read weight. So today the published change figures already
price cache reads at the pooled **0.0107**.

In `history/model-rates.json`:

| Fit | Weight (x Opus input) | 80% interval | n |
|---|---|---|---|
| pooled (`measured_rates.pooled_fit`) | 0.0107 | [0.0086, 0.0137] | all groups |
| dave after the cut | 0.0040 | [0.0025, 0.0055] | 159 |
| jwork after the cut | 0.0123 | [0.0101, 0.0147] | 106 |
| jwork before the cut | 0.0237 | [0.0197, 0.0298] | 55 |
| masterrig after the cut | 0.0079 | [0.0059, 0.0094] | 95 |
| masterrig before the cut | 0.0059 | [0.0002, 0.0107] | 34 |

`measured_rates.cache_read_weight.value` is null with `measurable: false`: the pooled interval
does not reach zero, but its width (0.0137 / 0.0086 = 1.59) is over `MAX_INTERVAL_RATIO` (1.5).
The `section3/<group>/<out5x|out3x>/cache_read_weight` values that read 0.0 belong to the fit
with cache reads held at 0 (the `zero` variant, `_variant` in `tools/model_rates.py`), so they
are 0 by construction; each row's nested `joint` holds the fitted weight.

## 3. Is 0.003 against 0 still a live choice?

Yes, and the data no longer back either side cleanly.

- **Against 0.** In September dave's own fit sat at 0 on the bound (`docs/findings-2026-09-23-cache-read.md`).
  Today no fit puts the weight at 0: every per-account interval sits above it, masterrig
  before the cut only just (0.0002).
- **Against 0.003.** It sits inside dave's interval after the cut and masterrig's before the cut
  ([0.0002, 0.0107], the widest), and outside the pooled fit and the other three.
- **Against any pooled value.** The accounts still disagree: dave after the cut [0.0025, 0.0055]
  and jwork after the cut [0.0101, 0.0147] do not overlap. The account offset of
  section 1 cannot be the cause, because each fit has its own scale; this disagreement is not
  explained.

So the condition the September line set, "explain the gap before adopting a pooled weight", is
mostly met for the meter gap (the headless effect, #139, with a 1.10x offset still unexplained)
and is not met for the weight itself
(the per-account weights still disagree). Nothing changes here: the reference weight stays 0 and
the comparisons keep the pooled fit's weight, as rule 23 has them. The open choice is the
reference weight alone (0.003 against 0); moving it, or taking the comparisons off the pooled
weight, changes figures published on alldonesites.com and needs its own PR under the ADR's
"rule for changing these", with the figures before and after, and Jonathan's go-ahead.

## 4. ADR 0001 entry 14

The 60% dominance rule has been in the code since #81 (2026-09-23): `DOMINANCE_SHARE = 0.60`,
`DOMINANCE_MIN_N = 3` in `tools/model_rates.py`, applied in `adopt`. The ADR still listed it as
pending. It is now entry 14 of the main list. One correction to what the pending entry
expected: it does not gate the published rates. The same day #86 made the pooled fit the source
of every published rate, and since then the per-account fits are diagnostics; the rule sets
only their `qualified` flag in `measured_rates.per_family.<family>.per_fit`. Whether it should
also gate which families the pooled fit may free is a separate decision, not made here.
