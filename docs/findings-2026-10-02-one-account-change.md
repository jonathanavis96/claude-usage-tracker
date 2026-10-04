# The 29 September five-hour change: one account's work and an implausible rate (2026-10-02)

> **Ported 2026-10-04 (#136).** This doc was written on branch
> `fix/change-must-hold-on-every-account`, which was never merged; #113 and #114 fixed the
> same headline differently on 2026-10-04. Its two rules are now on main as ADR 0001 rule 8
> (the rate check, "Rate check" below) and rule 9 (plan-wide, "Plan-wide" below),
> re-implemented against the post-#113 code. The figures below are the 2026-10-02 rebuilds
> on that branch's base and are kept as the evidence; one difference in the port is that a
> withheld candidate still opens a regime boundary, which carries the window and does not
> split the run (#113's unmeasured boundary). "On current main" at the end gives the
> 2026-10-04 rebuild.

The page said the five-hour limit rose 36% and the weekly limit 45.7% on 29 September, as
`last_change` and in `events` ("Five-hour limit +36%, weekly limit +45.7% (provisional)").
No such change happened. The figure came from two things: one account's unusual work, and a
joint fit that could only explain the meter by pricing Sonnet 5.5 at almost four times Sonnet
5. This doc sets out that evidence and the two rules added against it, with the published
figures before and after.

All figures come from offline rebuilds of the publisher (`publish.rebuild_public_json`) over
the history committed at a9f7ee1, at the live file's `generated_at`,
2026-10-02T13:01:11.968235Z. The prices file was a copy. "Before" is main's code at a9f7ee1,
and its rebuild gives the live figures quoted here. "After" is this branch.

## The candidate

The candidate is Sonnet 5.5's first use, `credits.five_hour_on_meters.candidates[3]`:
family `sonnet-5-5`, `at` 2026-09-29T18:25:18.212Z (`at_source` first_turn), no
announcement. Its before side starts at the Opus 5.5 candidate, 2026-09-22T19:41:49Z.

## The evidence

**The joint fit prices the new model far from its list price.** The joint fit gives
+36.0% [22.1, 58.1]: Max account 1 +48.0, Max account 2 +14.1, Max account 3 +35.4 and Max
account 4 +36.8. To get there it prices Sonnet 5.5 at `rate_relative_to_base` 3.83
[2.51, 7.20] times Sonnet 5 (1.52x Opus). data/prices.json gives the two models identical
list prices: input 2, output 10, cache write 2.5 and cache read 0.2 per million tokens.

**Sonnet 5.5 is a small part of the after-side work.** The median share of credits on the
after side is 0.0 on Max account 1, 0.099 on Max account 2, 0.038 on Max account 3 and 0.05
on Max account 4. With a share that small, the rate and the limit change trade off almost
freely. The fit's g depends on the rate it picks:

| Sonnet 5.5 rate held at | g, combined | Max account 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| 1.0x Sonnet 5 (list price) | +18.0% | +45.0 | -17.6 | +11.6 | +17.5 |
| 2.0x (top of the band below) | +24.4% | +46.1 | -5.6 | +21.0 | +24.5 |
| 3.83x (the fit) | +36.0% | +48.0 | +14.1 | +35.4 | +36.8 |

These are point estimates at the fit's own scatter, without a bootstrap. At the list price,
most of what is left is Max account 1's.

**The price-free measure shows no change.** The windows-per-week ratio (five-hour over
seven-day meter movement) needs no model rate. Per account it is:

| | ratio after over before | rounding interval |
|---|---|---|
| Max account 1 | 0.92 | [0.53, 1.57] |
| Max account 2 | 1.22 | [0.58, 3.05] |
| Max account 3 | 0.89 | [0.32, 4.49] |
| Max account 4 | 1.25 | [0.71, 2.35] |
| combined | +7.1% | [-42.6, 117.8] |

The weekly +45.7% was never measured on its own. It is the fit's 1.36 times this 1.071.

**Max account 1's rise is one overnight agent run.** Meter dollars per 1% at list prices,
pooled over accepted stretches without cloud sessions. The before side runs from
2026-09-22T19:41:49Z to the candidate, and the after side from the candidate:

| | before | after | change |
|---|---|---|---|
| Max account 1 | 2.251 | 2.909 | +29.2% |
| Max account 2 | 2.231 | 1.954 | -12.4% |
| Max account 3 | 1.575 | 1.747 | +10.9% |
| Max account 4 | 2.144 | 2.456 | +14.6% |

10 of Max account 1's 12 after-side stretches fall in one overnight agent run, from
2026-10-01T23:24Z to 2026-10-02T04:00Z. Those 10 read 3.034. The other 2 read 2.215, which
is -1.6%, at the same Opus 5.5 share. docs/findings-2026-09-30-per-account-spread.md has
already shown one account's agent-heavy work moving a candidate. Its decisions of 2026-09-30
stand.

**A measuring candidate was published as the headline.** At the 2026-10-01T09:31Z publish,
before that run's stretches arrived, the same candidate was `measuring`. It was still
`last_change`, as "Windows per week +26.7%" over [-42.5, 335.4]. The publisher's own rule
(`_latest_change_with_scope`) is that a provisional reading is never `last_change`.
`_with_announced_last_change` promoted any later five-hour meter event, whatever its state.

## The rules

1. **Plan-wide.** A plan limit change reaches every account at the same instant. A
   candidate whose combined figure draws on two or more accounts is refitted with each
   account left out in turn. The refit uses the estimator behind the figure the candidate
   would publish (its `headline`):
   - the joint fit's g, refitted with the rate;
   - the weekly limit change, where that moved more: each refit's g times the
     windows-per-week ratio recombined without the account;
   - the combined windows-per-week ratio, while the fit cannot separate g.

   The change is plan-wide only if every refit keeps its direction and its 95% interval still
   excludes no change. With two accounts, each one alone must show the change. A change
   resting on one account is not plan-wide. The result is recorded on each candidate as
   `plan_wide: {state, estimator, accounts, without: {label: {change_pct, interval_pct,
   holds, ...}}, reason}`.

2. **Rate check.** A joint fit counts as separable only if the new family's
   `rate_relative_to_base` interval overlaps 0.5 to 2 times its list-price ratio to its base
   family in data/prices.json. The ratio is taken in the input price class, because the
   fit's valuation (`comparison_value`) prices a family by its input rate. Input and cache
   writes are priced at that rate, and output at a fixed multiple of it, so the input price
   is the one `rate_relative_to_base` scales. A fit that fails stays unseparated. Its
   `rate_check` records the reason, its g is not published, and its rate is not absorbed.

Then a candidate `applies` only when its headline is plan-wide and the headline's own 95%
interval excludes no change. Applying means it opens a window regime, enters `events`, can
become `last_change` and can reach the daily.sh email. Every other candidate stays in
`credits.five_hour_on_meters.candidates` with its figures, state and `withheld_reason`.

A change that passes still reaches the page at once. Scope is still decided from data. Every
family's first use is still a candidate. The email still waits 24 and 48 hours. The regimes,
per-week figures and `_tokens_per_week_change` read `applies` through
`five_hour_meter_events` and `known_date_changes`, with no case of their own.

## What each rule does on the live history

**29 September (Sonnet 5.5).** The rate check fails: [2.51, 7.20] misses the band
[0.5, 2.0] around the list-price ratio of 1.0. With the fit unseparated, the headline is the
windows-per-week change, +7.1% [-42.6, 117.8]. That figure is not plan-wide. Left out in
turn, it reads:

| account left out | change | interval |
|---|---|---|
| Max account 1 | +19.2% | [-39.4, 174.7] |
| Max account 2 | +4.2% | [-42.8, 102.5] |
| Max account 3 | +8.6% | [-40.0, 106.2] |
| Max account 4 | -1.1% | [-48.3, 109.8] |

Its own interval also includes no change, so it is withheld on both counts.

The plan-wide test alone would not have stopped the +36%. With the rate check switched off,
the fit is separable and every leave-one-out refit holds. The refits, again with the Sonnet
5.5 rate refitted, are:

| account left out | g | interval | Sonnet 5.5 rate |
|---|---|---|---|
| Max account 1 | +25.8% | [11.2, 50.5] | 3.17x [1.96, 6.09] |
| Max account 2 | +37.7% | [23.9, 65.1] | 3.23x [2.02, 8.64] |
| Max account 3 | +38.3% | [18.7, 65.6] | 4.21x [2.66, 7.85] |
| Max account 4 | +36.9% | [16.4, 57.4] | 4.00x [2.48, 9.60] |

Each refit re-prices Sonnet 5.5 at three to four times its list ratio, and that pricing
keeps g up. The rate check is what stops this candidate.

**22 September (Opus 5.5).** The rate passes: 1.10x Opus [0.64, 1.47], against a band of
0.4x to 1.6x around Opus 5.5's input list-price ratio of 0.8. The fit gives +48.3%
[3.8, 88.7] on Max accounts 1 to 3; Max account 4 has no before side. It is not plan-wide:

| account left out | g | interval | Opus 5.5 rate |
|---|---|---|---|
| Max account 1 | -11.1% | [-24.1, 17.5] | 0.63x [0.52, 0.86] |
| Max account 2 | +61.2% | [19.7, 113.8] | 1.25x [0.82, 1.66] |
| Max account 3 | +59.8% | [12.9, 101.4] | 1.09x [0.71, 1.51] |

Without Max account 1 there is no rise. This matches docs/findings-2026-09-30-per-account-spread.md,
where Max account 1's step falls on 21 September, inside its own Opus sub-agent work.

## Before and after

| | before (main) | after (this branch) |
|---|---|---|
| `last_change` | 2026-09-29, sonnet-5-5, five-hour limit +36.0% [22.1, 58.1], weekly +45.7%, provisional | 2026-09-14 weekly event: windows per week down 21%, certified |
| `events` from 2026-09-14 | 14 Sep weekly -21%; 22 Sep "Five-hour limit +48.3%, weekly limit +43.2% (provisional)"; 29 Sep "Five-hour limit +36%, weekly limit +45.7% (provisional)" | 14 Sep weekly -21% only |

The candidates:

| candidate | before: state, change, interval | after: state, change, interval | `plan_wide` after | applies after |
|---|---|---|---|---|
| haiku, 2026-08-21 | measuring, null (wpw +10.3 [-13.3, 40.8]) | the same | failed, one account (a1) | no, before the weekly change |
| fable, 2026-08-28 | measuring, null (wpw -15.4 [-30.8, 2.5]) | the same | failed, one account (a1) | no, before the weekly change |
| opus-5-5, 2026-09-22 | provisional, +48.3 [3.8, 88.7] | provisional, +48.3 [3.8, 88.7] | failed: without a1 -11.1 [-24.1, 17.5] | no, not plan-wide |
| sonnet-5-5, 2026-09-29 | provisional, +36.0 [22.1, 58.1] | measuring, null; headline wpw +7.1 [-42.6, 117.8] | failed: no refit excludes no change | no, not plan-wide, interval includes no change, rate check failed |

Before, every candidate applied once it was measurable. There was no plan-wide record.

`credits.window_tokens.per_week_regimes`, in tokens per week:

| regime | before | after |
|---|---|---|
| to 2026-09-14 | 2,844,869,016 [1,054,824,277, 3,560,513,257] | the same |
| 2026-09-14 to 2026-09-22 | 2,423,328,064 [872,135,672, 3,131,206,550], window 501,060,306, 4.8364 windows per week | |
| 2026-09-22 to 2026-09-29 | 3,470,448,120 [1,248,985,496, 4,484,200,900], factor 1.4321 | |
| 2026-09-29 to now | 5,055,054,732 [1,819,272,273, 6,531,687,031], factor 1.4566 | |
| 2026-09-14 to now | | 2,435,075,806 [881,689,565, 3,125,667,203], window 497,004,961, 4.8995 windows per week |

The published week (`per_week.all`) goes from 5,055,054,732 to 2,435,075,806. The window
goes from 1,010,578,510 to 497,004,961.

**What follows from that.**

- **The window.** The 14 September regime now runs to now. Its after side
  (`five_hour_window_across_cut.after_until`) is no longer cut at 22 September, so it takes
  in every stretch since 14 September:
  - Max account 1 goes from +1.9% to +5.2%;
  - Max account 2 goes from +20.6% to +15.5%;
  - the combined five-hour window across the cut goes from +8.4% to +8.9%.
- **Tokens per week at the 14 September change.** `_tokens_per_week_change` moves from
  -21.0% [-32.2, -7.5] to -20.6% [-31.9, -7.1].
- **Sonnet 5.5's rate.** The joint fit is not absorbed, so Sonnet 5.5's published rate
  falls back to the pooled fit's 0.431x Opus, which is provisional. Before, it was the joint
  fit's 1.52x Opus.

## The 22 September candidate's after side

The 29 September candidate no longer closes a regime, so nothing published is cut at 29
September. The 22 September candidate's own measurement still ends there (`after_until`
2026-09-29T18:25:18Z). Every family's first use is a boundary of the candidate sides
(`candidate_bounds`), published or not. This branch keeps that.

Refitted with its after side running to now instead, the 22 September fit reads +30.6%
[-9.0, 71.8], with Opus 5.5 at 0.87x Opus [0.56, 1.24]. Its own interval then includes no
change, and it is still not plan-wide: without Max account 1 it reads -12.4% [-27.4, 8.2].
Whether a withheld candidate should still bound its neighbours' sides is left open. It
changes no published figure today.

## On current main (2026-10-04, #136)

Offline rebuilds (`publish.rebuild_public_json`) at 2026-10-04T13:00:00+00:00 over the
history and data committed at 07ef5c5, with main's code (231dde6) and with the rules ported.
Main's rebuild matches the 13:01Z publish in every figure below.

| | main | rules 8 and 9 |
|---|---|---|
| headline window (anchor units) | 501,060,306 [341,932,551, 676,394,239] | the same |
| headline window, Opus 5.5, measured directly | 665,278,990 [453,998,330, 898,077,280] | the same |
| `per_week.all` | 2,532,809,741 | the same |
| `last_change` | 2026-09-29, sonnet-5-5, windows per week +8.2% [-42.4, 114.7], measuring | 2026-09-14 weekly event, down 19%, certified |
| `events` | 14 Sep weekly (twice, as on main); 22 Sep windows per week -3.4%, measuring; 29 Sep windows per week +8.2%, measuring | 14 Sep weekly (twice) only |

After #113 neither joint fit is separable: Opus 5.5's rate interval is 0.64x to 1.47x Opus
and Sonnet 5.5's 1.23x to 4.43x Sonnet, both wider than 1.5x end to end. Both pass the rate
check (each overlaps its band), so the span test is what refuses them today. Both candidates
were still published as measuring events, and the 29 September one as `last_change`, because
main published any measurable candidate. Rule 9 withholds both:

- opus-5-5, 2026-09-22: plan-wide test failed (without a1 -2.2% [-30.6, 42.2], without a2
  -4.8% [-33.6, 43.5]) and interval test failed (windows per week -3.4% [-32, 42.8]).
- sonnet-5-5, 2026-09-29: plan-wide test failed (without a1 +16.5% [-39.4, 142.9], a2 +4.8%
  [-43.1, 98.4], a3 +11.3% [-39.6, 117.6], a4 +1.5% [-47.3, 106.6]) and interval test failed
  (+8.2% [-42.4, 114.7]).

Both still open their regime boundaries, which carry the window, so no window, week or
account figure moves. The 14 September change was already announced to subscribers, so the
notify step does not email it again.

The publish-time invariants (`tracker/invariants.py`) all pass on the live file of
2026-10-04T14:00:57Z and on both rebuilds. On the 11:01Z publish of 2026-10-04, before #113,
three fail: the headline (990,515,554 Opus tokens) sits above every account's level (364 to
559 million), two regimes are scaled by changes with no record of rules 8 and 9 and rate
intervals wider than the span test (Sonnet 5.5's also fails the rate check against
data/prices.json), and the 29 September +33.3% step points against its windows-per-week
ratio of 1.0607 [0.5674, 2.0792].
