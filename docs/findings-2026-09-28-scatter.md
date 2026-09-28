# What the per-stretch scatter is made of

Issue #125. Written 2026-09-28. Branch `wf/125-scatter`, on top of PR #98
(`wf/122-five-hour-sides`).

## The question

Every limit-change figure divides a stretch's credits by the meter movement it bought and
compares an account's level before a change with its level after. On PR #98 the joint fit
(`credits.joint_rate_fit`) reported a log residual sd of 0.29 for the 22 September Opus 5.5
candidate: one stretch typically lands about 29% from its account's level, and with about
27 stretches after the change that gave a five-hour change of +30.5% [-2.0, +62.7]. This note
breaks that scatter down by source, fixes what is measurement error, and reports what is
left.

## How it was measured

`tools/scatter.py` takes the stretches the fits use (`credits.announced_change_stretches`:
status accepted, harness runs out, at least the minimum movement, masterrig from its cut-off),
values them the way the publisher does, and takes each stretch's log credits per 1% from its
own account's mean within a regime. The regimes split at the 14 September weekly change and
at every change candidate, so account level and the regime steps are out of the residual. A
source's share is the fall in residual variance when its covariates are regressed out within
account and regime. Rounding is predicted outright from each stretch's own
`credits.rounding_variance` (windows / (6 x delta squared)).

The transcript-side checks (message usage lines, pooled-root attribution, headless runs, the
security plugin) read the `~/.claude*` directories on gs directly and are described in prose
below; they are not in the tool.

## The variance table

295 stretches, all four accounts, on the rebuilt histories with every fix below in place.
Residual variance 0.1237 (sd 0.352) about each account's level within a regime. Per account:
Max account 1 0.065, Max account 2 0.124, Max account 3 0.226, Max account 4 0.084.

| source | covariates | share of residual variance | variance after |
|---|---|---|---|
| whole-percent rounding (predicted, not fitted) | each stretch's `rounding_variance` | 2.0% | |
| capture status (outcome-defined, see below) | unaccounted, surplus | 55.0% | 0.0561 |
| reset verification | unverified, inferred | 0.7% | 0.1237 |
| token-class shares (pricing weights) | output, cache write 1 h, cache write 5 m, cache read per credit | 8.1% | 0.1153 |
| model-family shares (family rates) | Sonnet, Fable, Haiku, Opus 5.5 | 30.4% | 0.0873 |
| fast-session share | `fast_session_tokens` share | 5.7% | 0.1171 |
| hour of day and weekday (UTC) | sin and cos of hour, weekend | 6.5% | 0.1169 |
| stretch duration and window pieces | log hours, windows | 3.1% | 0.1208 |
| turns per credit | log turns per credit | 12.1% | 0.1092 |
| all measured sources together (capture excluded) | | 49.9% | 0.0659 |
| unexplained | | 50.1% | |

The shares overlap (family shares and turns per credit move together, since Sonnet work is
many small turns), which is why the column does not add up; "together" is the joint
regression. Residuals are also autocorrelated within an account's regime (lag-one
correlations from -0.23 to +0.61, most between +0.14 and +0.51): neighbouring stretches share
whatever the missing term is, so it is a slow-moving quantity, not noise per stretch.

### 1. Rounding

The five-hour meter reads whole percent, so each end of a stretch is uncertain by a uniform
half percent. Every selected stretch moves between 10% and 20% (none in the 3 to 5, 5 to 10 or
20+ buckets), where rounding predicts a variance of 0.0024 against an observed 0.1231: 2.0%.
Rounding is real but small at these movements. It is now used as a weight (fix 2), which
matters more for the occasional stretch spread over many window pieces than for the total.

### 2. Unseen work and capture

`capture_status` explains 55%, but it is judged on the same ratio the residual is made of (a
stretch is "unaccounted" because its credits per 1% are low), so it describes the residual
rather than explaining it. Reset verification explains 0.7%: inferred and unverified resets
are not a source.

Three transcript-side causes were checked on gs:

- **Streamed usage lines.** Claude Code writes one transcript line per content block, and a
  response that opens with a thinking block writes that block's line with the stream's
  opening usage (`output_tokens` of a handful); the final count is on a later line with the
  same message id. `tracker/turns.py` kept the first line, so output was undercounted by
  about 3.5x on Sonnet 5 and 1.5x on Opus 5 on gs, and masterrig's stretches gained 22% of
  output when rebuilt. Fixed (fix 1): each usage field is now the largest over the message's
  lines, which matches Claude Code's own per-session cost accounting.
- **The pooled projects root.** Max account 2's and Max account 4's config dirs share one
  `projects` directory; the tracker keeps a transcript for an account only when that
  account's `session-env` claims its session id. Headless `claude -p` runs start no shell and
  write no `session-env` entry, so no account claims them: on the current data 1,709 of Max
  account 2's candidate transcripts and 300 of Max account 4's are unclaimed. They are real
  work (the auto-mail filing judge picks a seat per run; the airlock bench inherits its
  caller's config dir) and were dropped, so the meter moved with no tokens behind it. No
  account's work lands in another account's stretch through the claim test; the error was
  missing work, not misattributed work. Fixed (fix 3).
- **The security-guidance plugin.** On gs its reviews call the Messages API over HTTP from
  the hook process, not through a child `claude`, so they write no transcript anywhere; the
  Agent SDK path that produced the `entrypoint: sdk-py` sessions on masterrig is only taken
  for third-party providers. Its log (`<config dir>/security/log.txt`) records each review's
  count and duration but not its tokens. Since each log's start: Max account 2 100 reviews,
  219 s in total; Max account 3 52, 102 s; Max account 4 22, 52 s. About two seconds per
  review is small next to a stretch, but it cannot be measured from disk and is not
  corrected; the plugin's credential and so which bill it lands on was not checked.

### 3. Pricing weights

Regressing the residual on each token class's share of credits explains 8.1%. Refitting the
weights by nonlinear least squares and testing them out of sample (leave one account out, and
fit on one half of the time range to predict the other) gives:

- cache read weight: fitted 0.0115 on all data, 0 to 0.03 across splits, and it improves
  the held-out variance in 3 of 6 splits: two by under 1%, one (Max account 4, 12
  stretches) by a third. The weight of 0 stands.
- output multiplier: fitted 5.03 against 5.0. Stands.
- cache write 1 h against 5 m: fitted 1.55 on all data but 0.19 to 1.73 across splits, and
  worse out of sample in 5 of 6. Not identified; left at list price.

The family rates are a different matter. Model-family shares explain 30.4%, and a free Sonnet
multiplier on the rate the publisher uses fits at 2.19 (2.25, 1.78, 2.61, 2.22 leaving out
each account in turn; 3.28 and 1.80 on the two time halves) and lowers held-out variance in 5
of 6 splits (for example 0.1235 to 0.0982 with Max account 2 held out). Sonnet work costs the
meter about twice what its fitted credit rate says. The history has too few pure-Sonnet
stretches for `tools/model_rates.py` to measure its rate directly (every account/period in
`history/model-rates.json` reads "NOT MEASURABLE" for Sonnet), so it falls back to a list
price ratio. This is not fixed here; see "What is left".

### 4. Fast sessions

The fast-session share explains 5.7%. Fast sessions are real (docs/findings-2026-09-28-speed-spike.md),
so this is a candidate covariate rather than an error; it is not modelled here because only 23 of
436 accepted stretches carry any.

### 5. Time

Hour of day and weekend together explain 6.5% with three covariates, against roughly 1% that
three random covariates would take by chance. Duration and number of window pieces explain
3.1%. Neither is large enough to model yet.

### 6. Sub-agents

Sub-agent transcripts sit under their parent session's directory
(`<session>/subagents/*.jsonl`) and are claimed with it, so their attribution follows the
parent's account by construction (814 of Max account 2's 2,079 kept files are sub-agent
files). The per-stretch sub-agent share is not in the stretch records and was not regressed.

### 7. Turns per credit

Log turns per credit explains 12.1%, and a fixed cost per turn fitted out of sample improves
held-out variance in all 6 splits. It overlaps heavily with the Sonnet share (many small
Sonnet turns), and with both in the joint fit the per-turn term falls by half. It points the
same way as the Sonnet rate: some cost is not proportional to list-price credits.

## Fixes

1. **Final message usage** (`tracker/turns.py`, commit ea8e826). Test:
   `tests/test_turns.py` builds a message whose first line carries the opening usage and
   checks the final count is read.
2. **Rounding weights** (`tracker/credits.py`, commit fdc72c5). `log_ratio_side` and
   `joint_rate_fit` weight each stretch by 1 / (scatter + its own rounding variance), with the
   scatter measured from the same stretches less their mean rounding, and publish
   `scatter_sd` and `rounding_sd` beside the residual sd. Tests in
   `tests/test_announced_change.py` (`RoundingWeightTests`): a rounding-heavy stretch pulls
   the estimate less than a third as far as unweighted.
3. **Unclaimed pooled-root work** (`tracker/gs_passive.py`, `tracker/credits.py`, commit
   fcd1ffd). Each stretch on a pooled account records the unclaimed work in its span as
   `unclaimed_tokens`. The known-date test and the joint fit measure each account's share of
   it, the s in [0, 1] that minimises the scatter of log((credits + s x unclaimed) / meter %)
   either side of the candidate, with a bootstrap interval, and add that share to the
   stretch's credits. On the current data Max account 2's share is 1.0 [0.92, 1.0] (24 of 56
   stretches carry some); Max account 4's is 1.0 but its interval is [0, 1], 10 of 12
   stretches. Tests: `tests/test_gs_passive.py` (headless work kept beside the stretch, not
   in it) and `tests/test_announced_change.py` (`UnclaimedShareTests`: work the meter carried
   is found and its scatter removed; work it did not carry gets no share).

Nothing on the website changes shape; the published JSON gains `unclaimed_share`, `scatter_sd`
and `rounding_sd` fields inside the change candidates.

## Before and after

Rebuilt locally from the committed histories with gs's and masterrig's stretch files
regenerated by each version of the code (`tracker.gs_passive`, `tracker.publish --out` to a
scratch file); nothing pushed. The 22 September Opus 5.5 candidate:

| | PR #98 as is | + usage fix | + rounding weights | + unclaimed work |
|---|---|---|---|---|
| joint fit residual sd | 0.291 | 0.293 | 0.294 | 0.258 |
| r, Opus 5.5 vs Opus 5 | 0.762 [0.468, 1.103] | 0.768 [0.481, 1.163] | 0.771 [0.486, 1.167] | 0.691 [0.416, 0.968] |
| five-hour change | +30.7% [-1.7, +62.8] | +35.9% [-4.7, +82.2] | +36.4% [-4.7, +82.6] | +23.9% [-12.2, +56.0] |
| weekly change | +28.1% [-8.2, +78.8] | +33.2% [-9.9, +96.8] | +33.7% [-9.6, +97.7] | +23.4% [-11.9, +72.8] |
| known-date test | +31.5% [11.7, 54.8] | +38.5% [17.9, 62.7] | +39.1% [18.3, 63.5] | +28.0% [10.4, 48.5] |
| stretches after (joint fit) | 39 | 39 | 39 | 42 |

Rounding weights on their own (without the usage fix) move the five-hour figure from +30.7%
to +31.2% with the same interval: rounding is not where the width comes from. The usage fix
is a correction, not an improvement in precision: output tokens were undercounted, more for
Sonnet than Opus, and counting them properly changes each stretch's mix and widens the
interval. The unclaimed-work fix is the one that removes scatter: residual sd 0.294 to 0.258.

For a five-hour interval of plus or minus 10%, the fit's current half-width on the log scale
(0.287 with 30 stretches after in 6.0 days, against 0.325 before fix 3) has to shrink by
about 1 / sqrt(n): `tools/scatter.py` puts it at about 273 stretches after, about 55 days
at the current rate. Before these fixes the same arithmetic said 41 days; that figure rested
on undercounted output.

## What is left

About half the residual variance is not explained by anything measured, and it is slow-moving
(autocorrelated within an account's regime). The largest measured candidate still in it is
the Sonnet rate: fitting a Sonnet multiplier in the joint fit (on the data before fix 3) moved
the residual sd from 0.294 to 0.285, r to 0.926 [0.597, 1.374] and the five-hour change to
+49.2% [+15.6, +82.3]. That is a family-rate question for `tools/model_rates.py` and the joint
fit together, and big enough to move the headline, so it is left for its own issue rather
than folded in here. The security plugin's reviews are unmeasured, and the fast-session and
time-of-day terms are small candidates for covariates once there is more data.
