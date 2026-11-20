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

The explanation that fits is a per-account meter. `docs/findings/2026-10-04-account-agreement.md`
(#139) measured it directly: credits per 1% of the **seven-day** meter agree across jwork,
dave and avis within -3% to +9% in every regime, while credits per 1% of the **five-hour** meter
do not (22-29 September: jwork 278k, dave 164k, avis 287k). Dave's five-hour meter moves 5.4
to 7.5 points per seven-day point, against 4.0 to 5.0 on the others, so for the same work
dave's five-hour percent is 0.55 to 0.8 the size of the others'. The holdout predicts
five-hour meter points, so it sees exactly that factor; 0.506 sits a little below that range
because October adds dave stretches it does not cover.

This does not touch the published rates. They are stated relative to the Opus anchor, with
one free scale per account and side of each change, so an account-wide meter factor cancels
out of them (the 2026-09-23 note's point 3).

## 2. The cache-read weight in force is 0

Every published credit figure prices cache reads at `data/prices.json` `_credits.cache_read_weight`,
which is **0** (range [0, 0.015]), read by `tracker/credits.py` `cache_read_weight()` and used
by `tracker/publish.py` and `tools/model_rates.py` (`CACHE_READ_WEIGHT = 0.0`).

The fitted weight is published beside it and prices nothing: `tracker/publish.py` marks the
`cache_read_weight` block `used_in_pricing: false`. In `history/model-rates.json`:

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
- **Against 0.003.** It sits inside dave's interval and outside every other fit's.
- **Against any pooled value.** The accounts still disagree: dave after the cut [0.0025, 0.0055]
  and jwork after the cut [0.0101, 0.0147] do not overlap. The per-account meter factor of
  section 1 cannot be the cause, because each fit has its own scale; this disagreement is not
  explained.

So the condition the September line set, "explain the gap before adopting a pooled weight", is
met for the meter gap (per-account five-hour meter, #139) and is not met for the weight itself
(the per-account weights still disagree). The weight in force stays 0. Moving it to 0.003, to
the pooled 0.0107, or anywhere else changes figures published on alldonesites.com and needs its
own PR under the ADR's "rule for changing these", with the figures before and after, and
Jonathan's go-ahead.

## 4. ADR 0001 entry 14

The 60% dominance rule has been in the code since #81 (2026-09-23): `DOMINANCE_SHARE = 0.60`,
`DOMINANCE_MIN_N = 3` in `tools/model_rates.py`, applied in `adopt`. The ADR still listed it as
pending. It is now entry 14 of the main list. One correction to what the pending entry
expected: it does not gate the published rates. The same day #86 made the pooled fit the source
of every published rate, and since then the per-account fits are diagnostics; the rule sets
only their `qualified` flag in `measured_rates.per_family.<family>.per_fit`. Whether it should
also gate which families the pooled fit may free is a separate decision, not made here.
