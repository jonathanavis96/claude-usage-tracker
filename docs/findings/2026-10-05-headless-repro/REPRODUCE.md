# Reproducing the headless five-hour finding

This folder recomputes the figures of
[`2026-10-05-headless-five-hour.md`](../2026-10-05-headless-five-hour.md), Part 1 of
[`2026-10-05-headless-weight.md`](../2026-10-05-headless-weight.md) and the write-up of
23 October built on them. It needs Python 3.9 or later and numpy, nothing else. It reads only
the files in this folder, so it does not depend on the repository's history or on the
tracker's current code.

From the root of a clone of this repository:

```sh
cd docs/findings/2026-10-05-headless-repro
python3 refit_from_csv.py          # the headline and each account
python3 steps_checks.py            # every other figure from the per-step table
python3 publisher_fit.py           # the tracker's own four-account fit
python3 meter_split.py             # which meter carries it
python3 recorded.py                # the figures that need the private transcripts
```

Each command finishes in under a minute. The expected output is below, command by command.
It was checked on 2026-10-23 from a fresh clone, with Python 3.12.3 and numpy 2.4.2.

## The result

For the same work, headless `claude -p` runs move the five-hour meter about 1.5 times as far
per seven-day point as interactive sessions do: **1.53 [1.41, 1.67]** on 306 steps of three
Max 20x accounts, 14 Sep 12:00Z to 4 Oct 19:53Z.

## What is measured

- **Step.** Each time an account's seven-day meter goes up by one whole percentage point, that
  is one step. Each crossing is placed at the first reading at or above the new value, and
  consecutive crossings tile the timeline with no gaps and no overlaps, so every step holds
  exactly one seven-day point.
- **Five-hour points.** The five-hour meter's one-point crossings placed the same way, counted
  in the step that holds them. A new five-hour window starts from 0 again, and its points count
  too.
- **Meters.** Both meters as the account's usage API reports them, in whole percentages, read a
  median of 130 seconds apart.
- **Work.** Every assistant message with usage in the account's own Claude Code transcripts. A
  message belongs to the step that holds its timestamp. Turns teleported from cloud sessions
  are left out.
- **Headless.** A main-session transcript whose first lines record `entrypoint: sdk-cli`, which
  is a `claude -p` run. Sub-agent transcripts never count as headless.
- **Credits.** Tokens valued at list-price ratios. One Opus 5 input token is 2/3 credit and an
  output token is five times its input. Opus 5.5 is 0.8x Opus 5, Sonnet 5 and 5.5 are 0.4x,
  Haiku 4.5 is 0.2x and Fable 5.1 is 2.0x. Cache writes of both TTLs count as input.
- **Headless share.** The step's headless credits over all its credits. `headless_share` leaves
  cache reads out (the covariate run). `headless_share_incl_cache_reads` counts them at 0.00398
  of an Opus input credit (the hypotheses run). Both give 1.53 [1.41, 1.67].
- **Model.** A quasi-Poisson regression (log link) of five-hour points per step, with terms for
  regime, account and headless share. The factor is exp(coefficient of the share), and its 95%
  interval is exp(b ± 1.96 se), with se scaled by the Pearson dispersion. Regime edges are
  14 Sep 12:00Z (the weekly limit change), 22 Sep 19:41:49Z (the first Opus 5.5 turn) and
  29 Sep 18:25:18Z (the first Sonnet 5.5 use). A per-account fit drops the account terms.
- **Accounts.** The 5 Oct analysis read transcripts on the machine that runs Max accounts 2, 3
  and 4, so those three are the accounts of the per-step table. Max account 1 runs on another
  machine. The tracker's own step records cover all four accounts, so Max account 1 enters the
  tracker's four-account fit (`publisher_fit.py`); only 3.5% of its work there is headless.

## Files

| file | contents |
|---|---|
| `data/steps.csv` | 397 steps of Max accounts 2 to 4, one row each, built from the transcripts and meter logs. `original_window` = 1 marks the 306 steps to 4 Oct. Columns: account, span, regime, `five_hour_points`, `seven_day_points` (always 1), credits, headless share (with and without cache reads), interactive and sub-agent shares, model-family shares, turns, sessions, headless runs, time-of-day shares, effort, concurrency, burst rate. |
| `data/tracker_steps.json` | The tracker's own one-point seven-day steps for all four accounts, as its committed passive history held them on 5 Oct (published 17:32Z) and on 11 Oct (published 23:31Z). Each step has its span, five-hour points, tokens by model and class for all work and for headless work, and a `clean` flag. Also the regime edges and the recorded five-hour windows. Timestamps are UTC. The history of 12 Oct 00:00Z holds the same steps and flags as the 11 Oct one. |
| `data/valuations.json` | The credit rate per token of each model family, as the tracker resolved it from its committed model rates of 5, 11 and 12 Oct, plus the list-price variant. |
| `recorded/fits.json`, `recorded/tables.json` | The output of the re-run of 23 Oct on the private transcripts: every fit and table, including those that cannot be recomputed here. |
| `checksums.json` | sha256 of the data files and of every private input the re-run read. |
| `common.py` | Data loading, the tracker's credit valuation and the quasi-Poisson fit. |
| `refit_from_csv.py`, `steps_checks.py`, `publisher_fit.py`, `meter_split.py`, `recorded.py` | The commands above. |

`data/tracker_steps.json` and `data/valuations.json` were exported with the tracker's own code
(`weekly_meter.clean_steps`, `credits.change_candidates`, `credits.comparison_rate`) from the
committed history files. `publisher_fit.py` then does what `five_hour_weight.headless_factor`
does inside every publish, and `meter_split.py` redoes the meter split from the same steps.
Both were checked against the tracker's own output on 2026-10-23 and agree to the printed digit.

## Commands and expected output

### `python3 refit_from_csv.py`

The headline and each account, from `data/steps.csv`. `--incl` uses the share that counts cache
reads, as the five-hour doc's per-account table does. `--all` extends the window to 11 Oct.

```text
$ python3 refit_from_csv.py
all accounts: 1.53 [1.41, 1.67], 306 steps
Max account 2: 2.17 [1.55, 3.03], 137 steps
Max account 3: 1.48 [1.32, 1.67], 128 steps
Max account 4: 1.53 [0.93, 2.52], 41 steps
$ python3 refit_from_csv.py --incl
all accounts: 1.53 [1.41, 1.67], 306 steps
Max account 2: 2.22 [1.57, 3.14], 137 steps
Max account 3: 1.49 [1.33, 1.67], 128 steps
Max account 4: 1.52 [0.92, 2.49], 41 steps
$ python3 refit_from_csv.py --all
all accounts: 1.49 [1.37, 1.62], 397 steps
Max account 2: 1.76 [1.30, 2.40], 160 steps
Max account 3: 1.49 [1.34, 1.67], 191 steps
Max account 4: 1.08 [0.80, 1.46], 46 steps
$ python3 refit_from_csv.py --all --incl
all accounts: 1.49 [1.37, 1.62], 397 steps
Max account 2: 1.89 [1.36, 2.63], 160 steps
Max account 3: 1.50 [1.34, 1.67], 191 steps
Max account 4: 1.08 [0.80, 1.47], 46 steps
```

### `python3 steps_checks.py`

Every other figure that comes from the per-step table, by section of the write-up. Sections
can be run on their own, for example `python3 steps_checks.py 5d 6`. Section 1's peaks, plan
check and section 5e's resets read the tracker's steps and five-hour windows as well.

```text
$ python3 steps_checks.py

== 1. Data
  Max account 2: 137 steps, 0 over 80% headless
  Max account 3: 128 steps, 50 over 80% headless
  Max account 4: 41 steps, 8 over 80% headless
  all: 306 steps, 58 over 80% headless (share counting cache reads)
  Max account 4's headless-heavy steps run from 2026-10-01T10:44:20Z to 2026-10-01T11:22:30Z
  highest recorded five-hour window, 14 Sep to 11 Oct (tracker's windows):
    Max account 2: 71%
    Max account 3: 95%
    Max account 4: 55%
  plan check, five-hour points per seven-day point over every tracker step inside one period, 14 Sep 12:00Z to 12 Oct 00:00Z:
    Max account 1: 4.73 (161 steps)
    Max account 2: 4.41 (160 steps)
    Max account 3: 6.32 (192 steps)
    Max account 4: 4.57 (47 steps)

== 2. Method: what 'interactive' holds
  steps under 0.2 headless (224): mean credit shares sub-agent 0.63, interactive main 0.35, headless 0.02
  steps over 0.8 headless (58): mean credit shares sub-agent 0.01, interactive main 0.01, headless 0.99

== 3. Main result
  headless factor: 1.53 [1.41, 1.67], 306 steps
  same, share counting cache reads: 1.53 [1.41, 1.67]
  share 0 to 0.15   215 steps: 4.40 [4.26, 4.55]  (bootstrap within the bin, 4,000 draws, seed 20261023)
  share 0.15 to 0.5  22 steps: 5.50 [5.00, 6.00]  (bootstrap within the bin, 4,000 draws, seed 20261023)
  share 0.5 to 0.9   15 steps: 6.00 [5.27, 6.87]  (bootstrap within the bin, 4,000 draws, seed 20261023)
  share 0.9 to 1     54 steps: 7.04 [6.59, 7.50]  (bootstrap within the bin, 4,000 draws, seed 20261023)
  raw end-to-end ratio 7.04 / 4.40 = 1.60
  plain means to 4 Oct: 57 steps over 0.8 headless average 7.12, 224 under 0.2 average 4.42

== 4. Per account (share counting cache reads, as the hypotheses run)
  Max account 2: 2.22 [1.57, 3.14], 137 steps, mean headless share 0.05, 0 steps over 80% headless
  Max account 3: 1.49 [1.33, 1.67], 128 steps, mean headless share 0.45, 50 steps over 80% headless
  Max account 4: 1.52 [0.92, 2.49], 41 steps, mean headless share 0.22, 8 steps over 80% headless

== 5b. Per-run startup overhead
  58 headless-heavy steps hold 10.6 runs each, at 98.4k credits per run
  runs per million credits, per SD (8.8): 1.03 [1.01, 1.06]; headless beside it 1.45 [1.32, 1.60]

== 5d. Everything else that travels with headless work
  added variable                         that variable      headless beside it that variable alone
  concurrency, mean (log)                0.97 [0.94, 1.00]  1.54 [1.41, 1.68]  0.98 [0.94, 1.01]
  concurrency, peak (log)                1.00 [0.97, 1.03]  1.53 [1.41, 1.67]  1.00 [0.97, 1.04]
  credits per active minute (log)        0.95 [0.92, 0.98]  1.56 [1.43, 1.70]  0.97 [0.94, 1.00]
  busiest minute's share                 0.95 [0.93, 0.98]  1.53 [1.40, 1.67]  0.95 [0.92, 0.98]
  Opus 5.5 share                         0.84 [0.71, 1.01]  1.57 [1.43, 1.71]  1.06 [0.87, 1.29]
  Sonnet share                           1.12 [1.00, 1.26]  1.59 [1.45, 1.74]  0.90 [0.80, 1.02]
  Fable share                            1.03 [0.89, 1.18]  1.54 [1.41, 1.68]  0.90 [0.77, 1.06]
  effort (low 1 to max 5)                1.01 [0.98, 1.04]  1.50 [1.37, 1.66]  1.07 [1.04, 1.10]
  peak hours (weekday 12-18 UTC) share   1.11 [1.04, 1.18]  1.48 [1.36, 1.62]  1.19 [1.11, 1.27]
  weekday 08-12 UTC share                0.87 [0.79, 0.96]  1.58 [1.45, 1.73]  0.98 [0.88, 1.09]
  weekend share                          0.95 [0.87, 1.03]  1.48 [1.33, 1.64]  0.79 [0.73, 0.85]
  all together (peak concurrency out)                       1.52 [1.34, 1.72]
  peak hours with all ten in: 1.09 [1.01, 1.18]
  peak hours beside headless, to 4 Oct, day-block bootstrap over 19 days: 1.11 [1.01, 1.21]; headless beside it 1.48
  peak hours beside headless, to 11 Oct, day-block bootstrap over 26 days: 1.18 [1.09, 1.27]; headless beside it 1.42
  extended to 11 Oct, all together: 1.49 [1.34, 1.67]
  median step concurrency (mean): over 0.8 headless 3.5, under 0.2 2.6
  median step concurrency (peak): over 0.8 headless 5.0, under 0.2 4.0

== 5e. Request counts, sub-agents and window resets
  turns per million credits (log)      1.02 [0.99, 1.05]  headless beside it 1.55 [1.42, 1.69]
  sessions per million credits (log)   1.02 [0.99, 1.05]  headless beside it 1.53 [1.41, 1.67]
  sub-agent share (interactive main base) 0.94 [0.84, 1.05]  headless beside it 1.46 [1.30, 1.65]
  five-hour window resets in the step  1.00 [0.97, 1.03]  headless beside it 1.53 [1.41, 1.67]
  all of 5d plus runs, sub-agent share and turns: 1.42 [1.18, 1.71]
  the ten-variable fit with the Sonnet share counting Sonnet 5.5: 1.53 [1.35, 1.73]
  median turns per million credits: headless-heavy 317, interactive 304
  median credits per turn (k): headless-heavy 3.16, interactive 3.29
  median sessions per million credits: headless-heavy 8.67, interactive 9.96
  mostly interactive main session: 38 steps, 4.63 five-hour points per seven-day point
  mostly sub-agent: 91 steps, 4.24 five-hour points per seven-day point
  mostly headless: 58 steps, 7.09 five-hour points per seven-day point
  resets per step: headless-heavy 0.211, interactive 0.205
  steps with no reset: 1.53 [1.38, 1.69], 245 steps

== 6. Robustness
  day-block bootstrap (19 UTC days, 2,000 resamples): 1.53 [1.38, 1.69]
  no period terms: 1.53 [1.42, 1.65]
  14 to 22 Sep only, one period: 1.53 [1.38, 1.69], 191 steps
  leave out Max account 2: 1.51 [1.36, 1.68], 169 steps
  leave out Max account 3: 1.50 [1.23, 1.83], 178 steps
  leave out Max account 4: 1.54 [1.41, 1.69], 265 steps
  interactive main sessions as the baseline: 1.46 [1.30, 1.65]
  headless run count added: 1.45 [1.32, 1.60]
  everything at once (5e): 1.42 [1.18, 1.71]
  extended to 11 Oct: 1.49 [1.37, 1.62], 397 steps
  extended, day-block bootstrap (26 days): 1.49 [1.30, 1.65]
  extended, all covariates: 1.49 [1.34, 1.67]
  extended, interactive main sessions as the baseline: 1.45 [1.31, 1.61]
  extended, everything at once: 1.39 [1.20, 1.62]
  extended, Max account 2: 1.89 [1.36, 2.63], 160 steps
  extended, Max account 3: 1.50 [1.34, 1.67], 191 steps
  extended, Max account 4: 1.08 [0.80, 1.47], 46 steps
  Max account 4's 5 new steps: highest headless share 0.010
  Max account 2 steps on 7 Oct that moved the seven-day meter a point on under 50k credits (tracker valuation): 6.5k, 6.4k
```

Two notes on this output:

- The ten-variable fit (5d, "all together") counts only Sonnet 5 in the Sonnet share, as the
  covariate run does, and gives the tool's 1.52 [1.34, 1.72]. The "everything at once" fits
  (5e and 6) were built by hand with Sonnet 5.5 counted in the Sonnet share too, which gives
  1.53 [1.35, 1.73] for the ten variables alone.
- The bootstraps use fixed seeds (20261023), so they print the same interval every run.

### `python3 publisher_fit.py`

The tracker's publish-time fit: all four accounts, the tracker's clean steps from 5 Sep, regimes
at the 14 Sep cut and each model family's first use, work valued at the tracker's fitted
rates. `--to 2026-10-11` uses the history and rates published on 11 Oct.

```text
$ python3 publisher_fit.py
pooled: 1.486 [1.381, 1.599], 659 steps
Max account 1: 2.655 [1.431, 4.928], 211 steps, mean headless share 0.035
Max account 2: 1.376 [1.258, 1.506], 273 steps, mean headless share 0.254
Max account 3: 1.496 [1.335, 1.676], 134 steps, mean headless share 0.472
Max account 4: 1.525 [0.924, 2.514], 41 steps, mean headless share 0.223
$ python3 publisher_fit.py --to 2026-10-11
pooled: 1.484 [1.384, 1.592], 738 steps
Max account 1: 2.460 [1.403, 4.313], 214 steps, mean headless share 0.039
Max account 2: 1.389 [1.270, 1.519], 287 steps, mean headless share 0.230
Max account 3: 1.504 [1.345, 1.681], 191 steps, mean headless share 0.627
Max account 4: 1.085 [0.800, 1.472], 46 steps, mean headless share 0.196
```

### `python3 meter_split.py`

The tracker's clean steps of Max accounts 2 to 4 from 14 Sep, valued at the rates of 12 Oct.
Three fits share the same rows and terms: the ratio (five-hour points per step), (a) five-hour
points per credit and (b) seven-day points per credit. `--json FILE` also writes every fit:
per account, leave one out, the day-block bootstrap, sandwich intervals, the linear form and
the full class-weight profile.

```text
$ python3 meter_split.py
Section 7 main table (tracker valuation, clean steps, regime and account terms):
  14Sep-4Oct, 302 steps: ratio 1.537 [1.408, 1.678]; (a) five-hour per credit 1.628 [1.356, 1.953]; (b) seven-day per credit 1.049 [0.930, 1.184]; (a)/(b) 1.551 (+0.9%)
  14Sep-11Oct, 386 steps: ratio 1.512 [1.390, 1.645]; (a) five-hour per credit 1.541 [1.295, 1.835]; (b) seven-day per credit 0.998 [0.895, 1.112]; (a)/(b) 1.545 (+2.2%)
Ratio on every step to 4 Oct (not only clean): 1.535 [1.407, 1.675], 306 steps
Max account 3, 2026-09-18 to 2026-09-21, no model:
  interactive (share<0.2)  64 steps, 1,214k credits per 7d point, 253k per 5h point, 4.80 5h per 7d point
  headless (share>0.8)     19 steps, 1,093k credits per 7d point, 148k per 5h point, 7.37 5h per 7d point
  per credit, headless moved the seven-day meter 1.11x and the five-hour meter 1.71x as far
Doc's crude buckets (5 Oct rates): seven-day factor 0.859 pooled; 38 of 58 headless-heavy steps after 22 Sep; 156 of 224 interactive steps before it; within 14-22 Sep the seven-day factor is 0.99
Token weights (to 4 Oct, clean): (a) five-hour / (b) seven-day
  cache reads at 0       1.432 / 0.912   (ratio 1.538)
  cache reads at 0.01    1.585 / 1.015   (ratio 1.536)
  cache reads at 0.03    1.792 / 1.158   (ratio 1.530)
  cache reads at 0.1     2.094 / 1.370   (ratio 1.521)
  one-hour writes at 2x  1.301 / 0.831   (ratio 1.544)
  API ratios             1.922 / 1.250   (ratio 1.527)
  the ratio across every valuation, both windows: 1.49 to 1.54
Weights the meters' own data prefer (best joint grid point): (a) / (b)
  14Sep-4Oct: 1.87 / 1.21; over the joint 95% region (a) [1.61, 2.16], (b) [1.03, 1.41]
  14Sep-11Oct: 1.76 / 1.14; over the joint 95% region (a) [1.52, 1.94], (b) [0.98, 1.26]
Token-class shares in the ratio fit (to 4 Oct): headless 1.520 [1.354, 1.706]; 5-minute writes 1.095 [0.806, 1.487], 1-hour writes 1.298 [0.851, 1.979], cache reads 1.388 [0.964, 2.000]
Single-model steps (one family at least 99.9% of the work), to 4 Oct: 42
```

### `python3 recorded.py`

These figures need the raw transcripts, which stay private: the cache-write shares and write
pricings (5a), the first-turn share (5b), the timing shifts (5c) and three step features (5d).
The script prints them as the re-run of 23 Oct recorded them.

```text
$ python3 recorded.py
5a. Share of credits going to cache writes, by kind of session (to 4 Oct):
  Max account 2: interactive main session 1h 0.379 / 5m 0.000; sub-agent 1h 0.000 / 5m 0.592; headless (claude -p) main session 1h 0.531 / 5m 0.000
  Max account 3: interactive main session 1h 0.384 / 5m 0.000; sub-agent 1h 0.000 / 5m 0.558; headless (claude -p) main session 1h 0.381 / 5m 0.000
  Max account 4: interactive main session 1h 0.424 / 5m 0.000; sub-agent 1h 0.000 / 5m 0.630; headless (claude -p) main session 1h 0.613 / 5m 0.000
5a. Write pricing (headless factor):
  collector (writes 1x, 1h no premium): 1.53 [1.41, 1.67]
  API (5m 1.25x, 1h 2x, read 0.1x): 1.52 [1.40, 1.66]
  collector, 1h at 1.6x: 1.54 [1.41, 1.68]
  collector, 1h at 2x: 1.54 [1.41, 1.68]
5b. Per-run overhead: first-turn cache write share of headless credits 0.118
5c. Timing shift:
  -30 min: 1.43 [1.31, 1.57]
  -10 min: 1.51 [1.39, 1.64]
  0: 1.53 [1.41, 1.67]
  +10 min: 1.44 [1.31, 1.58]
  +30 min: 1.42 [1.30, 1.56]
5d. Other step features (headless beside each, share counting cache reads):
  headless + turns per million credits: 1.56 [1.42, 1.70]
  headless + sessions per million credits: 1.51 [1.38, 1.64]
  headless + log step length: 1.54 [1.41, 1.68]
The 5 Oct doc's crude buckets (collector pricing): credits per 1% of the seven-day meter 1124k (over 0.8 headless) against 960k (under 0.2)
Headline with the refit cache-read weight of PR #125 (0.00899): 1.53 [1.40, 1.67]
```

## Two step sets

The docs quote per-account factors from two different fits. Keep them apart:

| | tool (`tools/headless_hypotheses.py`) | tracker (`tracker/five_hour_weight.py`) |
|---|---|---|
| steps | 306 tiled steps, 14 Sep 12:00Z to 4 Oct 19:53Z | 659 clean steps, 5 Sep to 5 Oct |
| accounts | Max accounts 2, 3 and 4 | Max accounts 1 to 4 |
| regimes | 14 Sep, 22 Sep, 29 Sep | 14 Sep, then each model family's first use |
| headless share valued at | list-price ratios | the fitted model rates |
| pooled | 1.53 [1.41, 1.67] | 1.486 [1.381, 1.599] |
| Max account 1 | not in this fit (runs on another machine) | 2.655 [1.431, 4.928], 211 steps, mean share 0.035 |
| Max account 2 | 2.22 [1.57, 3.14], 137 steps, mean share 0.05 | 1.376 [1.258, 1.506], 273 steps, mean share 0.254 |
| Max account 3 | 1.49 [1.33, 1.67], 128 steps, mean share 0.45 | 1.496 [1.335, 1.676], 134 steps, mean share 0.472 |
| Max account 4 | 1.52 [0.92, 2.49], 41 steps, mean share 0.22 | 1.525 [0.924, 2.514], 41 steps, mean share 0.223 |

Max account 2's tracker steps start on 5 Sep, before the tool's window. That is why its step
count and mean share differ.

## How the per-step table was made

The tool reads two private inputs on the machine that runs Max accounts 2, 3 and 4:

1. **Transcripts.** The JSONL files Claude Code writes per session, attributed to accounts by
   the same rules the tracker's collector uses. The tool caches them. The cache behind the
   5 Oct docs was written on 2026-10-04 at 20:06Z and holds 143,406 assistant messages with
   usage: 42,867 for Max account 2, 62,490 for Max account 3 and 38,049 for Max account 4. The
   last is stamped 2026-10-04 19:53:23Z.
2. **Meter logs.** One log per account of five-hour and seven-day utilization.

On 23 Oct the tool was re-run unchanged: the hypotheses with the code merged in PR #124 and the
covariates with the code merged in PR #125. Every number in Part 1 of both docs reproduced to
the printed digit, and so did the weight doc's per-account table in Part 2. The hypotheses run
depends on the model rates committed with its code through the cache-read weight: with the
refit of PR #125 (0.00899) the headline reads 1.53 [1.40, 1.67] (`recorded.py`).

On 2026-10-15, transcripts older than 14 days were moved into compressed per-project
archives. The cache still holds every message, but the tool labels a transcript headless by
reading the transcript's own first lines. So the re-run streamed each archived transcript's
first lines out of the archive and gave the tool those labels: 1,240 of the 1,371 labels came
from the archive and 131 from files still on disk.

**Validation.** The whole turn set was rebuilt from the files on disk plus the archive, using the
collector's own attribution rules. Over the cache's span it is identical to the 2026-10-04 cache:
every message, every token count and every file attribution, on all three accounts, and all
143,406 effort values. The tool's output on the rebuilt set is identical to its output on the
cache, every fit and every step. The window to 11 Oct uses the rebuilt set; it stops at the last
message before 12 Oct (2026-10-11 22:23:57Z), because from 12 Oct the accounts were also used
from a machine whose transcripts were not read.

`data/steps.csv` is the tool's step table from that run. `checksums.json` holds the sha256 of
each private input (the transcript cache, the archive manifest, each meter log cut at the two
window ends, and the tracker history and rate files the exports came from).

## Caveats

- **Max account 3 carries the headless evidence.** It has 50 of the 58 steps over 0.8 headless.
  Max account 4's 8 such steps all fall between 10:44Z and 11:22Z on 1 Oct. Max account 2 has
  none.
- **The model intervals assume independent steps.** Steps on the same day are not. The day-block
  bootstrap widens [1.41, 1.67] to [1.38, 1.69].
- **Which meter moves depends on valuation.** The ratio does not: 1.49 to 1.54 under every
  valuation tried.
