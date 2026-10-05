# Rates fitted in the five-hour unit; each account at 22 September; the window figure

Date: 2026-10-05. Branch `wf/144-headless-weight` (PR #125), second round. Every figure below
comes from an offline build of the publisher on the committed histories as of 17:00Z
(`history/gs-passive.json` and `history/masterrig-passive.json` with the headless split
backfilled), not from a publish. Labels: Max account 1 is a1, 2 is a2, 3 is a3, 4 is a4.

## Summary

- **The 29 September "+17%" was a pricing artefact, but not Sonnet 5.5's.** The per-family
  rates (`tools/model_rates.py`) were fitted on metered tokens, while the publisher compares
  periods in interactive-equivalent tokens. Max account 4's before side (22 to 29 September)
  is 68% interactive older-Sonnet work, priced at a rate fitted with the headless factor still
  in it. Its after side is 70% headless. Fitted in the publisher's own unit, the rates put
  29 September at +2.1% [−3.1, 7.7]. It is not certified and opens no step. The fit now always
  runs in that unit.
- **22 September reads +20.0% [11.1, 29.6] pooled:** a1 +37.4%, a2 +10.6%, a3 +18.4%. It is
  certified on both meters, and the weekly limit change is +17.6% [5.9, 30.5].
- **a1's excess is the 18 September burst, not a data error.** That one day holds 27 of the
  45 before-side stretches. Counting each day once, a1 reads +2.9%. No exclusion, attribution
  or dating problem carries it (section 2).
- **The window goes from 621M to 897M Opus 5.5 tokens, all of it the headless weight.** The
  rest of #125's 1,055M came from the 29 September artefact, which read the window from five
  post-29 September stretches only.

## 1. 29 September

### Sonnet 5.5's price is not it

Sonnet 5.5's first use is 29 September. Under ADR 0001 rule 10 it is valued at its list ratio
(0.4x Opus 5) while its joint fit there is not separable. Rebuilt on #125's rates:

| Sonnet 5.5 valued at | pooled | a1 | a2 | a3 | a4 |
|---|---|---|---|---|---|
| list, 0.4x (as published) | +16.6% [9.0, 24.8] | +30.5 | −5.0 | +8.9 | +30.2 |
| fitted, 0.529x | +19.3% [11.5, 27.5] | +30.6 | −1.0 | +10.3 | +33.3 |

Its share of credits is small on every account:

| 22 to 29 Sep, then from 29 Sep | Sonnet 5.5 share | headless share |
|---|---|---|
| a1 | 0.000, 0.011 | 0.060, 0.029 |
| a2 | 0.000, 0.179 | 0.112, 0.182 |
| a3 | 0.000, 0.053 | 0.947, 0.942 |
| a4 | 0.000, 0.142 | 0.005, 0.699 |

The account that makes the change is a4. Unweighted, it reads −1.8% at 29 September. Weighted,
it reads +30.2%, because its headless share goes from 0.5% to 70%.

### The family rates were fitted in the wrong unit

`tools/model_rates.py` fitted every family's rate on metered tokens, so a family used mostly
headless took the 1.5x into its rate. Refitted on the same stretches in interactive-equivalent
tokens, at the factor the publisher fits (1.486):

| times Opus 5 | metered fit | interactive-equivalent fit |
|---|---|---|
| Sonnet (older) | 0.384 [0.316, 0.450] | 0.527 [0.469, 0.596] |
| Fable | 2.124 [2.017, 2.250] | 2.954 [2.768, 3.176] |
| Opus 5.5 | 0.611 [0.509, 0.738] | 0.847 [0.711, 1.020] |
| Sonnet 5.5 (provisional) | 0.529 [0.385, 0.756] | 0.904 [0.682, 1.233] |

At the refitted rates, with both new families at list as rule 10 requires:

| | pooled | a1 | a2 | a3 | a4 |
|---|---|---|---|---|---|
| 29 Sep | +2.1% [−3.1, 7.7], not certified | +24.2 | +0.4 | +2.0 | −2.4 |
| 22 Sep | +20.0% [11.1, 29.6], certified | +37.4 | +10.6 | +18.4 | |

a4's before side is mostly older Sonnet. The refit raises that family's rate from 0.384 to
0.527, which brings its interactive work up to the unit its weighted after side is in.

Two checks beside the published rule:

- **Sonnet 5.5 at its refitted 0.904x** (2.3x its list ratio, and above Opus 5.5's 0.847x):
  29 September reads +9.5% [4.1, 15.2], with a1 +25.6, a2 +13.4, a3 +6.6 and a4 +5.5. Rule 10
  keeps it at list, because its joint fit there does not separate it from a limit change.
- **Opus 5.5 at its refitted 0.847x** instead of list 0.8x: 22 September reads +23.6%
  [14.6, 33.3] (a1 +40.8, a2 +14.7, a3 +23.0), and 29 September +4.7%.

### The fix

`tools/model_rates.py` now fits the headless factor from the same histories, exactly as the
publisher does (`five_hour_unit`), and fits every rate on the stretches with headless tokens
counted at it (`stretches_in_five_hour_unit`). The factor used is recorded as
`measured_rates.five_hour_unit`, and the publish shows it as
`five_hour_meter.model_rates_fitted_at`. With the refitted rates, the factor at publish moves
from 1.486 to 1.494, so one daily refit converges. A new family's rate is now fitted in the
unit the change tests compare in, whichever model comes next. ADR 0001 rule 12 records this.

## 2. Every account weighted, and each account at 22 September

### Backfill

The headless split (`headless_tokens`, `headless_turns`) is now in the committed histories for
all four accounts. Each account was re-collected with the branch's collector as of its file's
own `generated_at`: the gs accounts on gs, a1 on masterrig. Every stretch and seven-day step
matched a committed record on every other field (a1: 281 stretches and 479 steps; a2: 167 and
302; a3: 87 and 136; a4: 22 and 42). Each file with the two fields removed is byte-identical
to the committed one.

### a1: +37.4% [19.3, 58.3]

Each test below goes through the publisher's own per-account test (`split_at_candidate`,
`log_ratio_side`):

| test | a1 at 22 Sep |
|---|---|
| as published | +37.4% [19.3, 58.3], 45 before, 9 after |
| split at its 21 Sep 10:54Z step instead | +49.9% [35.1, 66.2] |
| after side against 21 to 22 Sep only | −16.3% [−30.4, 0.6] |
| after side against 18 to 20 Sep only | +42.9% [26.3, 61.6] |
| 18 Sep stretches left out | +15.4% [−1.1, 34.6] |
| its two cloud-session stretches put back | +44.7% [27.0, 64.8] |
| 5-minute writes priced 1.25 : 2 against 1-hour, as list | +27.5% [8.4, 50.0] |
| its own factor (2.43) instead of the pooled one | +38.5% [14.5, 67.5] |
| stretches flagged `unaccounted` left out | +33.4% [16.9, 52.2] |
| each day counted once (geometric mean per day) | +2.9% [−22.2, 36.1], 5 days before, 4 after |

- **Dating.** The 21 September step is real. Moving a1's split to it makes the change bigger,
  not smaller, because the stretches from 21 to 22 September sit at the after side's level.
- **Sub-agent 5-minute writes.** Priced as list prices them, they take 10 points off. That
  would be a coefficient: the meter's price for a 5-minute write has never been measured
  (docs/findings-2026-09-30-per-account-spread.md section 5a put the fitted weight at 0.7).
  So it is not applied.
- **Cloud exclusions.** The two excluded stretches are both on the after side. Putting them
  back raises a1.
- **Attribution.** gs's default login (`~/.claude.json`, used when `CLAUDE_CONFIG_DIR` is
  unset) is the same account as masterrig's: the account ids match. Work under it bills a1's
  meter, but a1's stretches read only masterrig's transcripts. Over 14 to 29 September that
  work is at most 1.0M credits a day, and none on 18 September. The pooled root's unclaimed
  runs are at most 1.0M a day before 1 October. a1's stretches carry about 2M credits each.
  Neither moves the test.
- **The before side.** 18 September holds 27 of the 45 before-side stretches, from one day of
  very fast work (10 to 15% of the five-hour meter in 10 to 25 minutes) at about 205k credits
  per 1%. The after side sits at about 310k. a1's Opus sub-agent share goes from under 0.05
  before to 0.34 after. That is the effect #108 measured at about 2x and could not explain,
  and Jonathan ruled out a separate sub-agent rate (2026-09-30).

**Not fixed.** No exclusion, attribution or dating error carries a1's excess. It comes from
how much one day's work weighs and from a work-mix change, and correcting either needs a
coefficient. The plan-wide test already allows for it: without a1, 22 September reads +13.1%
[3.1, 24.0] and still holds.

### a3 against a2

| a3 at 22 Sep | |
|---|---|
| as published (pooled factor 1.494) | +18.4% [0.5, 39.5] |
| its own factor (1.499) | +18.4% [0.8, 39.2] |
| unweighted (main) | +3.4% |
| Opus 5.5 at its refitted 0.847x | +23.0% |
| 5-minute writes 1.25 : 2 | +26.2% |

The weighting accounts for a3's whole move from main: its headless share goes from 0.30
before 22 September to 0.95 after. Its own factor equals the pooled one. a2 reads +10.6%
[−1.3, 24.0], down from +20.6% in #125's build. Its before side is 30% Fable, which the
refitted rates price at 2.95x Opus 5 instead of 2.12x. a2 and a3's intervals overlap.

### Against the announced +20% (a test, not an input)

Pooled +20.0% [11.1, 29.6] contains it, and so does each account's interval: a1 [19.3, 58.3],
a2 [−1.3, 24.0] and a3 [0.5, 39.5].

## 3. The window: 621M to 1,055M to 897M

The published Opus 5.5 window is the direct median of the pure Opus 5.5 stretches in the
current regime. It is read off their token counts, with no rate in it.

- **Main, 621M:** the 9 pure stretches from 22 September on, unweighted.
- **Headless weight alone, 621M to 897M (x1.445):** the same 9 stretches. Eight are a3's
  wholly headless runs, which count at x1.494. One is a2's interactive stretch from
  25 September (803M), which is unchanged. The median moves from 621M to 896M.
- **#125's build, 897M to 1,055M (x1.176):** the 29 September artefact split the regime, so
  the window was read from the five stretches after 29 September only, all a3's headless runs.
  With the artefact gone, that split is gone.

The 22 September step being applied does not change this figure, because it is a direct
reading; it moves only the anchor-unit regimes behind the chart. The anchor's pre-cut cluster
(11 a2 Opus 5 stretches, all headless) moves x1.494 from 450.6M to 673.2M. A rate conversion
plays no part in any of these.

**For an interactive user:** an interactive Claude Code session on a Max 20x plan can spend
about 897M Opus 5.5 tokens per five-hour window [678M, 1,342M], most of them cache reads. a2's
interactive readings sit inside that: 803M, and 927M from a cloud-session stretch the window
leaves out. A headless `claude -p` run gets
about 1 / 1.494 of it, about 600M.

## Invariants on the branch build

7 of 9 pass. The two that fail are advisory and do not block the publish:

- **`steps_agree_with_meters`.** The 22 September +20% against a windows-per-week ratio of
  1.069 [0.750, 1.601]. The ratio is read from per-window meter readings, which carry no
  headless split; a3's headless share going from 0.30 to 0.95 moves it the other way.
- **`account_units`.** a3's 14 to 22 September window is 610M against a direct reading of
  896M, from a single wholly headless stretch.
