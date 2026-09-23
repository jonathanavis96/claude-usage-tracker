# One pooled rate fit, the five-hour comparison on its selection, and no capped weekly windows, 2026-09-23

Three changes to the published figures, plus two additions made in the same PR. No traffic was sent.
Every figure comes from committed history files, and the meter logs on gs and masterrig were read
only. Nothing here was committed from those logs.

## 1. One pooled rate fit replaces the per-group median

**What was wrong.** Before this change, each account and side of 14 September was fitted on its
own. A family's rate was the median of the group fits that passed the dominance rule: at least 3
stretches that are 60% or more that family. Fable qualified in only two groups, jwork/post (3.15x
Opus) and masterrig/pre (1.92x). The published rate was 2.53x Opus, with the union interval
[1.80, 4.75]. The other three groups carry Fable too, but only as part of a mix. That width came
from the pooling method, not from the data.

**The method now** (`tools/model_rates.py`, `pooled_section`, `measured_rates`):

- One set of per-token rates is shared by every group.
- Each group (account x era) has its own credits-per-1% scale.
- The fit minimises, over the rates, the sum across groups and their stretches of
  (log(credits_s / delta_pct_s) minus that group's mean of it)². Here
  credits_s = Σ_f rate_f × (input + cache_write + 5 × output)_f,s + w × cache_reads_s.
- Opus is fixed at 1. Every other rate and w is free and positive.
- The group scale is the group mean, so it is never a parameter. This is the "stable
  account-specific scale cancels" point in `tracker/credits.py`, used as the model rather than
  treated as an obstacle.
- The fit uses Nelder-Mead in log space, written in numpy, because scipy is not a dependency.
  The simplex restarts from its own optimum until the loss stops moving. It is deterministic.
- Intervals come from 600 bootstrap resamples, stratified by group, reported at the 10th and
  90th percentiles.
- A family is published when its interval starts above zero and its high end is under
  `MAX_INTERVAL_RATIO` = 1.5 times its low end.
- A family carried by fewer than 3 stretches is held at its inferred rate (section 4) and is not
  measured.
- The per-group least-squares fits stay in the record as `per_fit` diagnostics. They no longer
  set any rate.

**The selection.** It is `tracker/credits.py` `rate_fit_stretches`, which `model_rates.clean` now
calls:

- the capture test on every account;
- masterrig only from 6 September;
- no stretch that spans the cut.

The one stretch removed by the last rule is masterrig's 2026-09-12T22:51 to 2026-09-18T02:52.
Before this change it was filed as "pre" by its start time.

**The analysis reproduces.** On the committed data at 7f211c1, with Opus 5.5 held at Opus as in the
analysis, the numpy fit gives:

- Fable 2.101x [1.999, 2.227];
- Sonnet 0.567x [0.471, 0.651];
- Haiku point 0.547x, interval reaching zero;
- w 0.0016.

Leaving one account out gives Fable 2.330 without dave, 1.994 without jwork and 2.033 without
masterrig. All of these match the analysis.

**As committed.** These figures use the 15:30Z publisher state: 175 stretches in 5 groups, with
Opus 5.5 held at its inferred 0.8x.

| Rate | Before (published) | After |
|---|---|---|
| Fable | 2.532x [1.798, 4.751] | **2.111x [2.004, 2.249]** |
| Sonnet | 0.595x [0.485, 0.672] | **0.553x [0.455, 0.640]** |
| Haiku | not measurable (no fit dominated by it) | not measurable (80% interval [0.000, 2.33]x); shown inferred, see section 4 |
| Opus 5.5 | not measurable | not measurable (2 stretches carry it); shown inferred, see section 4 |
| Cache-read weight | no value | fit point 0.0018 [0.000, 0.0052]; interval reaches zero, so no value is published |

- **Fable either side of the cut:** pre 2.122x [2.032, 2.250], post 2.096x [1.907, 2.377],
  post/pre 0.988 [0.889, 1.103]. Fable's rate did not change on 14 September.
- **Credits per 1% by group:** dave/post 147,486; jwork/post 186,585; jwork/pre 190,606;
  masterrig/post 153,951; masterrig/pre 161,312.

## 2. The five-hour comparison uses the fits' selection and rates

Before this change, `across_cut` read the `priceable` selection. That selection:

- exempted masterrig from the capture test;
- had no start date for masterrig;
- priced every stretch at the reference table, with Fable held at 1.667 on both sides.

So masterrig's "before" median included the 2 to 5 September takeoff stretches, and read +17.3%
across the cut.

`across_cut` now reads `rate_fit_stretches` and prices every family at the pooled fit's whole
coefficient vector, the fitted cache-read weight included (`pooled_fit_prices`). It falls back to
the old reference pricing only when no pooled fit is on record.

| | Before | After (median, as published) | After (the fit's own geometric group scales) |
|---|---|---|---|
| a1 (masterrig) | +17.3% (n 166 / 34) | **-8.0%** (n 34 / 25) | -4.6% |
| a2 (jwork) | -2.5% (n 50 / 27) | **-0.0%** (n 50 / 26) | -2.1% |
| median of qualifying accounts | +7.4% | **-4.0%** | |

The published figure is the median of each account's median credits per 1%, as before. The fit's
group scales are geometric means, so they read differently. The analysis's bootstrap put jwork at
-2% [-8, +4] and masterrig at -4% [-7, -1]. Either way, the five-hour window did not grow at the
cut.

**Tokens per week across the change:**

- The windows-per-week step is -22.3% on the committed weekly series, and -22.9% once section 3's
  filter is applied.
- Compounded with -4.0%, tokens per week falls **-25.4%** with the code alone, and **-26.0%** once
  the next publish rebuilds the weekly series with section 3's filter. It was -16.6% before this
  change.
- The pure-Opus window is the before cluster scaled by that change, so it falls from
  20,990,134 to **18,762,131 credits**.

**`fable_interval`** still has a consumer on the page: `ClaudeUsageTracker.tsx` prints its
`status`, `input_low` and `input_high`. It now states the pooled figure: 1.3362 to 1.4995 credits
per input token, 2.00x to 2.25x Opus, "measured by one fit pooled across every account". The old
per-stretch solve is kept under `solved`. `window_credits_per_pct` is left out, so the page no
longer says "solved against a window".

## 3. Weekly windows read after the seven-day meter capped are dropped

**The rule.** From the moment the seven-day meter reads 100, the five-hour meter keeps counting
and the weekly meter does not. Any window whose seven-day reading at its end is 100 or more is now
left out of every windows-per-week figure, including the weekly sums. A window that starts at 100
also ends at 100, so it goes too. The rule lives in `tracker/weekly.py` (`SEVEN_DAY_CAP_PCT`,
`capped`) and is applied in three places:

- `weekly_windows`, which builds masterrig's series and contributors' series;
- `probe_weekly_windows`;
- `tracker/join.py` `window_points`, which builds gs's `weekly_by_window`.

**Which accounts reached 100.** I scanned every meter log read-only:

| Account | Readings at 100 or more | Windows dropped |
|---|---|---|
| dave | 15–17 Sep and 21–23 Sep | 2026-09-21T21:30 (25 over 2 = 12.5) |
| jwork | 21–22 Sep | 2026-09-21T16:39 (16 over 4), and two windows at 21:39 and 02:37 with no seven-day movement |
| masterrig | 11 Sep (6 readings in the moonlighter log; none in the ceiling log) | 2026-09-11T02:29 (13 over 1 = 13.0) |
| avis | never | none |

**Dave's 16:30 window stays under the rule as specified.** Over that window the seven-day meter
went from 92 to 98, and it reached 100 only at 17:22, inside the next window. The window reads
51 over 6 = 8.5, and because it ended at 98 the rule keeps it. The results:

- Dave's current falls from 5.64 to **5.50**.
- Leaving the 16:30 window out as well would give 5.30. That would need a rule based on the
  approach to the cap (for example, a window ending within a few points of 100), not the cap
  itself. The director decided on 2026-09-23 against such a rule: a window ending at 98% is not
  capped, and dave stays at 5.50.

**Other figures, once the next publish rebuilds the series:**

| Figure | Before | After |
|---|---|---|
| max20 current | 5.04 | **4.99** |
| a2 (jwork) current | 4.50 | 4.45 |
| a1 (masterrig) current | 5.03 | 5.03 (its 11 Sep window closes the earlier regime: 6.54 to 6.52) |

The committed `history/gs-passive.json` and `history/passive.json` hold windows built by the old
code, so these weekly figures move only when the daily publisher regenerates gs-passive, and when
masterrig's own cron regenerates `history/passive.json` with this code. `main`'s code rebuilt from
the live logs at the committed cutoff reproduces both committed files exactly. The branch's code
removes the windows listed above and changes nothing else. No stretch changed.

## 4. Inferred rates for the families the fit cannot measure

Haiku and Opus 5.5 fail the measurable rule:

- Haiku: clean stretches carry at most 26% Haiku, and the 80% interval reaches zero.
- Opus 5.5: only 2 clean stretches carry any.

Both are now shown on the page at an inferred rate, marked as inferred:

| Family | Inferred from | Rate | Times Opus |
|---|---|---|---|
| Haiku | `reference_table` (data/prices.json `_credits`: 2/15 input, 10/15 output; this is also its list-price ratio to Opus 5) | 0.1333 | 0.200x |
| Opus 5.5 | `list_price` (claude-opus-5-5 $4/$20 against claude-opus-5 $5/$25) | 0.5333 | 0.800x |

**How it is published:**

- `tools/model_rates.py` writes an `inferred` block into each unmeasurable family's row in
  `history/model-rates.json`, computed from `data/prices.json`.
- `tracker/credits.py` `family_rate` publishes it with `rate_source: "inferred"`, no interval and
  no status. The row therefore carries every figure a measured row carries:
  `credits_per_token`, `tokens_per_window` and `api_value_per_window_usd`.
- `measured_rate` keeps what the fit said: the not-measurable sentence under `measured_status`,
  the pooled point estimate and interval under `pooled`, and `inferred_from` with its basis.
- A family that passes the rule switches to `measured` without any code change.

**Where inferred rates do not go.** An inferred rate never values a stretch.
`tracker/gs_passive.py` `stretch_credits` treats it as absent, so the credit series and change
detection move exactly as they did before. Haiku still falls back to the reference table there,
and Opus 5.5 stretches are still unpriced.

**List price is not always the meter:**

| Family | Measured | List price | Reference table | Agrees with list? |
|---|---|---|---|---|
| Fable | 2.11x Opus | 2.0x | — | yes |
| Sonnet | about 0.55x Opus | 0.4x | 0.6x | no |

An inferred row is a placeholder until the fit can see the family, not a measurement.

The per-model row carries `inferred_from` as a top-level key, which is where the site reads it:
`"reference_table"` or `"list_price"` on an inferred row, and null on a measured or anchor row.

One thing this PR does not do: an inferred row takes its `as_of` from the newer of the window and
the fits, as a measured row does. The rate itself has no fit behind it, so the window's date alone
would be more accurate.

The publish check classifies these figures under the `credits` block. They reproduce from
`history/model-rates.json` and `data/prices.json`.

## 5. Avis's stretches are unjudged, and stay so until it has its own reference

Avis (a4) has been sampled since 2026-09-23 09:47Z. At the 15:30Z state it has 2 stretches, both
mostly Opus 5.5, and both have capture status "unjudged". `tracker/capture.py` `judge`
bootstraps an account's reference from the median of its own first `BOOTSTRAP` = 5 priced stretches
that spent anything. Until it has 5, every stretch is unjudged. Nothing is broken. The account is
about half a day old, and at its current rate of use it bootstraps itself within a day or two.

**Judging it against a reference pooled from the other accounts would not be sound:**

1. The accounts' own scales differ by more than the tolerance. On the pooled fit, jwork/post
   reads 186,585 credits per 1%, while dave/post reads 147,486 and masterrig/post 153,951. jwork
   is 21–27% above the other two, and `TOLERANCE` is 15%. A reference pooled from them would
   withhold a whole account's level as "unaccounted" or "surplus" depending on which account it
   was pooled from. That is the reason `capture.py` keeps every reference per account ("the
   reference is the account's own").
2. The reference is in list-dollar meter value per 1%, and avis's mix is mostly Opus 5.5. Opus
   5.5's meter rate relative to its list price is the unknown these stretches are wanted for. A
   borrowed reference assumes it: if the meter charges Opus 5.5 at anything but its list ratio,
   every avis stretch reads off by that ratio. The check would then accept or withhold exactly the
   stretches that measure it, in a direction set by the assumption. That is circular.

`tracker/capture.py` is unchanged. Once avis's own 5-stretch reference exists, its accepted
stretches enter the pooled fit automatically (`pooled_records` takes every account), and Opus
5.5 can become measurable there.

## Weekly scale per group: is jwork's higher reading a capture difference?

Jwork reads about 187,000 to 191,000 credits per 1% of the five-hour meter. Masterrig reads
154,000 to 161,000 and dave 147,000. If jwork's transcripts held tokens its meter never counted,
the rate would read high in exactly this way. Jwork's `projects/` is a symlink into the pooled
`~/.claude/projects`, attributed by a session-env filter, and avis now shares that pool.

A capture over-count and a genuinely larger five-hour window predict different things for the
seven-day meter. Windows per week comes from the meters alone, with no tokens in it:

| Account | Credits per 1% | Windows per week | Weekly credits (credits per 1% x 100 x windows per week) |
|---|---|---|---|
| jwork | 186,585 | 4.45 | 83.0M |
| dave | 147,486 | 5.50 | 81.1M |
| masterrig | 153,951 | 5.03 | 77.4M |

The five-hour scales differ by 27%, but the weekly budgets agree within 7%. That is what a
genuinely larger five-hour window on jwork would look like. A pure over-count would inflate jwork's
weekly credits by the full 27%. Jwork's weekly figure is the highest of the three, by 2–7%, so a
small over-count on top cannot be ruled out. This is an inference from three regimes that do not
align exactly, not a measurement.

**What would settle it:**

- an identical scripted workload run on jwork and dave in the same hour (this needs traffic, so it
  was not done here);
- a check that no session id attributed to jwork also appears in another config dir's
  session-env;
- whether jwork's scale steps at 2026-09-23 09:47, when avis started writing into the same pool.

Nothing was changed for this.

## Reproduce

```
python3 -m tools.model_rates history/masterrig-passive.json --json history/model-rates.json
python3 -m tracker.rebuild_offline --root . --now 2026-09-23T15:30:14+00:00 --out /tmp/x.json
python3 -m tools.credits_report --publish-check /tmp/x.json
```

The publish check reproduces every credits and weekly figure. The only 10 figures that do not
reproduce (`contributed.*` timestamps one second apart, and three `rebuild.*` keys) come from
running it on an offline rebuild, and `main` shows the same 10.
