# Headless work counted at its measured five-hour weight (2026-10-05)

wf-144, following Jonathan's ruling that the headless effect found in wf-143 is the five-hour
meter's own behaviour and should be used. The five-hour figures now count headless work at a
factor fitted at every publish (ADR 0001 rule 12). The seven-day meter is not weighted.

Part 1 uses the gs transcripts cached on 2026-10-04 19:53Z: 306 seven-day steps from
14 September (`python3 -m tools.headless_hypotheses covariates`).

Part 2 numbers are offline rebuilds with `now` 2026-10-05 15:31Z of the inputs committed at
da63a25, built with main and with this branch. The one difference in the inputs is
`headless_tokens` and `headless_turns`. Those were injected into every stretch and seven-day
step by re-running the new collector on gs (Max accounts 2-4) and on masterrig (Max account 1)
up to each history file's own `generated_at`. Every stretch and step it rebuilt matched the
committed ones exactly, tokens and five-hour points included. On the committed histories,
which carry no headless split, the branch reproduces main exactly. The only difference is the
new `five_hour_meter` block, which reads "not measured".

## Part 1: it is headless, not something that travels with it

The headless factor, on its own and beside each variable (quasi-Poisson, regime and account
terms). Continuous variables are per standard deviation. Shares run 0 to 1.

| added variable | that variable | headless beside it |
|---|---|---|
| none | | **1.53 [1.41, 1.67]** |
| concurrency, credit-weighted mean (log) | 0.97 [0.94, 1.00] | 1.54 [1.41, 1.68] |
| concurrency, peak (log) | 1.00 [0.97, 1.03] | 1.53 [1.41, 1.67] |
| credits per active minute (log) | 0.95 [0.92, 0.98] | 1.56 [1.43, 1.70] |
| busiest minute's share of the step | 0.95 [0.93, 0.98] | 1.53 [1.40, 1.67] |
| Opus 5.5 share | 0.84 [0.71, 1.01] | 1.57 [1.43, 1.71] |
| Sonnet share | 1.12 [1.00, 1.26] | 1.59 [1.45, 1.74] |
| Fable share | 1.03 [0.89, 1.18] | 1.54 [1.41, 1.68] |
| effort (low 1 to max 5, credit-weighted) | 1.01 [0.98, 1.04] | 1.50 [1.37, 1.66] |
| weekday 12-18Z share | 1.11 [1.04, 1.18] | 1.48 [1.36, 1.62] |
| weekday 08-12Z share | 0.87 [0.79, 0.96] | 1.58 [1.45, 1.73] |
| weekend share | 0.95 [0.87, 1.03] | 1.48 [1.33, 1.64] |
| **all of them together** | | **1.52 [1.34, 1.72]** |

- **Concurrency and burstiness do not carry it.** Headless steps do run a little wider: the
  median credit-weighted concurrency is 3.5 against 2.6, and the peak 5 against 4. But
  concurrency, beside headless, is 0.97x, and burstiness goes the other way (0.95x). The
  weight is therefore built on headless.
- **Effort and model mix do not carry it either.** Effort is 1.07x on its own and 1.01x
  beside headless.
- **Time of day.** The weekday 12:00-18:00Z share (US morning) reads:
  - 1.19x [1.11, 1.27] on its own;
  - 1.11x [1.04, 1.18] beside headless;
  - **1.09x [1.01, 1.18] with everything in**, so it barely survives;
  - weekday 08:00-12:00Z reads 0.87x beside headless, and weekends 0.95x.

  The interval comes from a quasi-Poisson fit over 306 steps on three accounts. It does not
  allow for steps on the same day being correlated, so it is too narrow. **This does not
  support publishing a time-of-day effect.** It is a lead, worth a day-block bootstrap once
  more weeks are in.

## Part 2: the weight, measured at every publish

`five_hour_meter.headless_factor` on the 2026-10-05 inputs: **1.486 [1.381, 1.599]**, from
659 clean steps on all four accounts. Per account:

| | factor | mean headless share | steps |
|---|---|---|---|
| Max account 1 | 2.66 [1.43, 4.93] | 0.035 | 211 |
| Max account 2 | 1.38 [1.26, 1.51] | 0.254 | 273 |
| Max account 3 | 1.50 [1.34, 1.68] | 0.472 | 134 |
| Max account 4 | 1.53 [0.92, 2.51] | 0.223 | 41 |

Max account 1 has almost no headless work, so its own factor is loose. The other three
agree.

### What it changes (main to branch)

| | main | branch |
|---|---|---|
| headline | weekly +30% on 22 Sep | **limits +17% on 29 Sep** (five-hour) |
| window chart, 14 / 22 / 29 Sep | +11% / none / none | −2% / **+37%** / **+17%** |
| tokens-per-week chart, 14 / 22 / 29 Sep | −11% / +30% / none | −11% / +30% / none |
| windows-per-week chart, 14 / 22 / 29 Sep | −20% / none / none | −10% / −5% / −14% |
| Opus 5.5 window (headline) | 621M | 1,055M interactive-equivalent |
| Opus 5.5 tokens per week | 3,727M | 3,012M |
| windows per week now | 4.6 | 2.9 |
| invariants 1-9 | all pass | 7 pass; account_units and steps_agree_with_meters fail (advisory) |

**22 September five-hour change, now plan-wide:**

- pooled: **+37.0% [25.3, 49.7]**, which certifies;
- per account: Max account 1 +56.0%, Max account 2 +20.6%, Max account 3 +29.1% (was +3.4%);
- left out in turn: without Max account 1 +24.5% [10.7, 40.0]; without Max account 2 +45.0%;
  without Max account 3 +39.9%. Every one holds.

Max account 4's first meter reading is 2026-09-23 09:47Z, so it has no before side and
cannot enter a 22 September test.

**29 September (the first use of Sonnet 5.5, no announcement) now also certifies:**

- pooled: **+16.6% [9.0, 24.8]**;
- per account: Max account 1 +30.5%, Max account 2 −5.0%, Max account 3 +8.9%, Max account 4
  +30.2%;
- left out in turn, every reading holds, from +10.3% to +25.0%.

The weekly change there, +6.7% [0.6, 13.1], is not certified. So the window steps and the
week does not.

**14 September:** the direct window test drops from +11.7% [6.6, 17.1] to **+3.8% [−1.9, 9.8]**,
not certified. The window chart's 14 September step goes from +11% to −2%. The weekly test is
unchanged at −5.6% [−19.9, 11.1], not certified.

### Two data-path fixes this needed

Two certified five-hour changes in a row had never happened before, and it exposed the
mismatch #124 fixed for the week:

- **Window.** The 22 September regime is the previous one scaled by its certified change. The
  29 September regime is its own Opus 5.5 cluster, converted at the family rate. So the chart
  stepped −6.4% where the change was certified at +16.6%. A regime opened by a certified
  change and measured in a new family now goes through a bridge rate, as a run already did.
  The step is then the certified change, and the headline stays the regime's direct reading.
- **Week.** At a change certified on the window alone, the week is now one level measured over
  both sides (rule 11: a certified change steps only what it was certified on). Before, the
  tokens-per-week chart stepped +6.3% at 29 September.

### Still failing (advisory, not blocking)

- **`steps_agree_with_meters`.** It compares each five-hour step with the unweighted meter
  ratio (five-hour over seven-day points), and that ratio carries the headless effect itself.
  The 22 and 29 September ratios (1.07 and 1.06) read as −6% five-hour. Weighting that ratio
  needs a headless split on each window point, which the collector does not record yet.
- **`account_units`.** Max account 3's relative window from 14 to 22 September is 610M Opus
  tokens; its direct reading is 891M, 46% apart. The 14 September regime is the pre-cut cluster
  scaled by the cut's −1.7%, while both accounts read their direct windows well above it. The
  same gap was at 24% on main.

## Against the announcements (a test, not an input)

- **22 September, five-hour +20% announced: misses high.** The measured +37.0% [25.3, 49.7]
  excludes +20%. The weekly limit rose +29.6% [15.6, 45.4] at the same time.
- **14 September, weekly −17% announced: still missed.** The chart steps −11%, and the direct
  test is −5.6% [−19.9, 11.1], not certified.
- **29 September: no announcement,** yet the five-hour limit certifies at +16.6% [9.0, 24.8].
