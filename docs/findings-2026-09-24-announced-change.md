# Known-date change test at each model's first-seen time

2026-09-24. Code: `tracker/credits.py` (`change_candidates`, `announced_change`),
`tracker/publish.py` (`credits.announced_change`, `events`, `last_change`),
`tools/announced_change_sim.py`, `tests/test_announced_change.py`.

## Why

The unknown-date detector (`tracker/detect.py:detect_credit_changes`) stays as it is. At the
per-stretch scatter measured on the committed history, a +20% step was certified within 40
post-change stretches in 6 of 40 simulated trials on one account and 1 of 40 on another.
A test at a known date needs fewer stretches, as the 14 September five-hour comparison
(`five_hour_window_change`) already does.

## Method

- Candidates come from the data. Every credit family's earliest stretch across all accounts
  is a candidate change point; the candidate instant is that stretch's start, so the
  family's first traffic lies between `at` and `first_seen_stretch_end`. A family present in
  the earliest stretch on record is not a candidate. No date is typed in.
- Announcements are a list (`ANNOUNCEMENTS`). A five-hour announcement dated within one day
  of a candidate is attached to it as `announcement`. It adds quotes and sources; it does not
  create a candidate, and a candidate with none is tested the same way.
- Accounts are every account in `history/gs-passive.json` and `history/masterrig-passive.json`.
  Stretches: `status` accepted, harness runs removed, at least 3% of meter movement, the
  personal account from 6 September. Reset-unverified stretches are admitted and counted per
  account and side. A stretch that cannot be valued in credits is counted as unpriced.
- Each stretch is valued with `gs_passive.stretch_credits`, the valuation credit detection
  uses, and read as log credits per 1%.
- The before side runs from the previous boundary (the 14 September weekly change or an
  earlier candidate) to the candidate; the after side from the candidate to the next
  boundary. A stretch spanning a boundary is on neither side.
- Per account: the ratio of the two sides' geometric means, with a 95% t interval on the
  logs, scatter pooled over both sides. An account needs 5 stretches before and 1 after to be
  combined; one without both sides is listed with its counts.
- Combined: a weighted mean of the accounts' log ratios, weights 1 / se squared, with a t
  interval on the combined standard error.
- States: `measuring` below 5 stretches after on every combined account, `provisional` at 5
  to 9, `measured` at 10 or more on at least one.
- A candidate that is `measured` with a combined interval excluding 1.0 is added to the
  published `events` as a `kind: "change"` record with `scope: "five_hour"` and
  `known_date_test: true`, and becomes `last_change` when it is dated later than the current
  one.

## Simulation

`python3 -m tools.announced_change_sim --trials 400`, on the committed history at 5319157.
Each account's before side at the Opus 5.5 candidate is its own committed stretches; 10
after-stretches of 10 points each are drawn by resampling that account's own residuals, shifted
by the step. Seeds 0 to 399.

| account | stretches before | log sd |
|---|---|---|
| a1 | 32 | 0.220 |
| a2 | 41 | 0.507 |
| a3 | 51 | 0.437 |

| step | combined interval excludes 1.0 | a1 alone | a2 alone | a3 alone |
|---|---|---|---|---|
| +20% | 85.2% | 67.8% | 14.2% | 23.5% |
| 0% | 2.5% | 1.2% | 1.8% | 2.0% |

The acceptance test (`tests/test_announced_change.py`, 200 seeded trials) requires at least
70% at +20% and at most 10% at 0%, with all three accounts at 10 stretches after.

## Opus 5.5 candidate on the committed history

Candidate instant 2026-09-22T17:03:48Z, first seen on a1 in a stretch ending
2026-09-23T08:26:32Z. Before side from 2026-09-14T12:00Z. Announcement attached: +20%,
five-hour scope, dated 2026-09-22.

On `main` today every stretch holding Opus 5.5 tokens is unpriced by `stretch_credits`, so
the after sides are empty and the state is `measuring`:

| account | before | after | after unpriced | before reset-unverified |
|---|---|---|---|---|
| a1 | 32 | 0 | 5 | 32 |
| a2 | 41 | 0 | 3 | 16 |
| a3 | 51 | 0 | 0 | 6 |
| a4 | 0 | 0 | 12 | 0 |

With Opus 5.5 valued at 0.8x the Opus anchor (the list-price ratio PR A adds to
`stretch_credits`), the same history reads:

| account | before | after | after reset-unverified | after unpriced | change | 95% interval |
|---|---|---|---|---|---|---|
| a1 | 32 | 2 | 2 | 3 | +61.7% | +17.3% to +122.9% |
| a2 | 41 | 3 | 2 | 0 | +16.8% | -36.1% to +113.5% |
| a3 | 51 | 0 | 0 | 0 | not combined | |
| a4 | 0 | 11 | 2 | 1 | not combined | |

Combined over a1 and a2: +50.7%, interval +14.1% to +98.9%. State `measuring`: no account
has 5 stretches after. a4 has no stretch before the candidate. a1's three unpriced
after-stretches each hold a `<synthetic>` model id, which is in no credit family.
