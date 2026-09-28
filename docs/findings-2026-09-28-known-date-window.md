# The measured 22 September five-hour change reaches the window figure (2026-09-28)

## What changed

**Window regimes.** `credits.window_credits` and `credits.window_tokens` used to split
their pure-Opus cluster only at `cut_at` (2026-09-14T12:00Z, the weekly change). The
known-date test (`credits.announced_change`) has since measured a +37.8% change in the
five-hour window at Opus 5.5's first-seen stretch (2026-09-22T17:03:48Z, interval +14.0%
to +66.7%, state `measured`), and the headline reports it, but the window figure still
showed the pre-22-September window.

Now every `announced_change` candidate that is `measured`, whose interval excludes no
change, and which is dated after `cut_at` opens a new regime from the candidate's own
`at` (`credits.known_date_changes`, `credits.window_regimes`). A regime is stated by its
own cluster (the stretches that started in it) once it holds `MIN_AFTER_CLUSTER` (5)
readings. Below that it is the previous regime's value times the change's combined ratio.
Its interval is the previous interval's low edge times the ratio's low edge, and its high
edge times the ratio's high edge. The window interval is a spread of readings, not a
standard error, so the edges are multiplied rather than standard errors combined. The
published `value` and `interval` are the newest regime's, and `current_source` reads
`previous_regime_scaled_by_known_date_change` (or `known_date_regime_cluster` once the
regime is thick enough). `current_method` says so in words and names the change applied.

Both blocks now publish `regimes`, oldest first and contiguous, in the block's own unit
(credits per window for `window_credits`, reference-mix tokens per window for
`window_tokens`):

```
"regimes": [{"from", "until", "value", "interval", "source"}, ...]
```

The last regime has `until: null`, and its value equals the block's current value. With
no measured known-date change there are the two regimes the 14 September split gives. No
date is typed: the boundary is the candidate record's `at`. The publisher computes
`announced_change` before the window so that the window can read it.

Everything downstream reads the window's `value` (or `credits_per_pct`) and follows it
without a second rule: per-model tokens and API value per window, session counts,
`window_tokens.per_family`, `per_week` and the Fable solved interval. Nothing in the
publisher reads the before cluster directly. `tools/cache_read_profile.py` reads
`before` and `after` for its own report, and those two fields are unchanged.

One caveat carried over from before: `fable_interval.solved` applies the single current
window value to every Fable-heavy stretch, whichever regime the stretch fell in. That
was already true across the 14 September cut, and it now moves with the current window
(times_opus 0.49 to 3.12 before, 0.80 to 4.36 after).

**Notify gate.** `notify_change` in `bin/daily.sh` keyed "new evidence" on the newest
`weekly_windows.passive.by_window[].window_ending` alone. That series stopped at
2026-09-24T18:10 (see below), so `.weekly-change-seen` kept `previous: 2026-09-11` and the
22 September change waited on every hourly publish. The key is now the newer of that
window ending and the newest `account_feeds[].newest_stretch_end`, compared as instants
because the feeds mix UTC offsets. A five-hour change is measured on stretches, and the
weekly windows can stall while stretches keep arriving. The two-consecutive rule, the
same-event-within-a-day rule and the `.notified-change` dedupe are unchanged.

`account_feeds[].newest_stretch_end` counts capture-accepted stretches only. a2's four
newest stretches (2026-09-25T19:36 to 2026-09-27T19:18) have `status` accepted but
`capture_status` "surplus", so a2's feed still reads 2026-09-25T11:37. The gate
therefore advances on a new capture-accepted stretch, which is a stricter test than the
known-date test's own selection (`status`).

## The numbers

These come from an offline rebuild of the live file's inputs at its own `generated_at`
(2026-09-28T11:30Z), through `tools/verify_publish.rebuild`, before and after the change.

`credits.window_tokens.regimes` (reference-mix tokens per five-hour window):

| from | until | value | interval | source |
|---|---|---|---|---|
| null | 2026-09-14T12:00Z | 450,593,800 | 173,516,520 to 542,413,890 | before_cluster |
| 2026-09-14T12:00Z | 2026-09-22T17:03:48Z | 428,965,298 | 165,187,727 to 516,378,023 | before_cluster_scaled_by_five_hour_change |
| 2026-09-22T17:03:48Z | null | 591,114,181 | 188,314,009 to 860,802,164 | previous_regime_scaled_by_known_date_change |

- `window_tokens.all` and the hero figure (`window_tokens.per_family.opus.all`, Max 20x,
  Opus 5) go from **429M** (165M to 516M) to **591M** (188M to 861M) tokens per window.
- `window_credits.value` goes from 18,605,780 to 25,638,765 credits (regimes 19,543,887,
  then 18,605,780, then 25,638,765).
- `current_source` on both blocks: `previous_regime_scaled_by_known_date_change`.

The new regime's own cluster holds fewer than 5 pure-Opus stretches, because use moved
to Opus 5.5 after 22 September. So the figure is the scaled one, and it will switch to
the regime's own cluster by itself once 5 such stretches exist.

**Deploy note.** When run against the real `.weekly-change-seen` (evidence
2026-09-24T18:10, date 2026-09-22), the first hourly publish after this merges sees new
evidence: a1's newest stretch, 2026-09-25T21:38Z. The previous look already showed
2026-09-22, so that publish sends the 22 September change (+38%, scope `five_hour`) to
subscribers. This was demonstrated with a stub curl. Nothing was posted.

## Why weekly_windows.passive.by_window stopped at 2026-09-24T18:10

- `weekly_windows.passive.by_window` is a single account's series. It holds a1
  (masterrig) alone: `tracker/passive.py` builds it on masterrig with
  `weekly.weekly_windows(parse_rows(...))` over the moonlighter usage log only, and it
  arrives in `history/passive.json`. The other accounts' windows, which come from
  gs-passive.json, go to `weekly_windows.max20.by_window` and run to 2026-09-27T22:38
  (a2) and 2026-09-27T16:30 (a3). The gate never read those, so new stretches or windows
  on a2/a3 could not advance it.
- masterrig's own merged feed (`history/masterrig-passive.json`, `join.window_points`
  over the moonlighter log plus the ceiling log) has four windows after 18:10. Three are
  reset-unverified (ceiling log): 2026-09-24T13:05Z, 18:05Z and 2026-09-26T02:27Z. One
  is reset-verified: 2026-09-25T04:09:59Z. `passive.json`, pushed in the same commit,
  has none of them. The first three are expected, because passive.json reads the
  moonlighter log alone. The fourth means the two pairing codes (`weekly.weekly_windows`
  and `join.window_points`) disagree on at least one moonlighter window. Which rows
  cause that cannot be checked from gs, because the log lives on masterrig. A null or
  unparseable row breaks `weekly.py`'s chain, and a seven-day reset near the
  week ending 2026-09-25 is a plausible candidate.
- masterrig's daily push last landed at 2026-09-27 03:15 +02:00 (commit 858b5c1), and
  none has arrived for 2026-09-28. `passive.json`'s daily history marks 2026-09-25 as
  interpolated, so masterrig itself has seen little reset-verified movement since
  2026-09-24.
- None of this is a one-line bug in the publisher. The gate was keyed on one account's
  moonlighter-only series. Reading `weekly_windows.max20.by_window` instead of
  `passive.by_window` would be the one-line alternative. This PR keys on stretch ends as
  well, which covers both. The `weekly.py` / `join.py` disagreement on the 2026-09-25
  window deserves its own look on masterrig, and this PR does not fix it.
