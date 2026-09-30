# Why the per-account 22 September five-hour changes disagree

Issue #133. Written 2026-09-30.

## The figures being explained

The publish at 2026-09-29T23:31Z (history at commit 42c1b43) gives the Opus 5.5 candidate
(first turn 2026-09-22T19:41:49Z; before side from the 14 September cut, after side to
2026-09-29T18:25Z) as:

| | known-date test | stretches before / after | joint fit |
|---|---|---|---|
| Max account 1 | +74.4% [54.0, 97.4] | 45 / 11 | +71.6% |
| Max account 2 | +45.8% [24.3, 71.1] | 41 / 17 | +41.9% |
| Max account 3 | +16.6% [-4.9, 42.9] | 52 / 13 | +14.7% |
| Max account 4 | no before side | 0 / 13 | |
| combined | +53.1% [40.4, 67.1] | | +38.8% [-4.6, 78.4], Opus 5.5 at 0.946x Opus |

Rebuilding the publish from that commit's history files with this branch's code reproduces
every figure above exactly. Everything below is measured against that rebuild. Where a figure
also needs the transcripts, they were read from disk on both hosts on 2026-09-30 at about
12:30Z. On every account and side, the transcripts on disk now give the same credits as the stretch
records (median ratio 1.000 to 1.005). On Max account 1, 11 of 56 stretches hold 1.5% to 22%
more on disk now than when they were recorded.

## Summary

- **No work is counted twice.** No message id is read by two accounts. Sub-agent files are
  not read twice. The one real overlap is the unclaimed pooled work, which is given to two
  accounts at once, but it moves Max account 2 by only 3.4 points (hypothesis 1).
- **The main cause is how the work mix changed.** Opus-family sub-agent work reads about twice
  its credit value against the meter. Its share of the work changes across 22 September in
  opposite directions on the two outlying accounts:
  - Max account 1: mean 0.05 of credits before, 0.34 after;
  - Max account 3: 0.21 before, 0.00 after.

  With that work valued at 0.5x, the three accounts read +35.7%, +29.5% and +29.2%. The
  heterogeneity test then gives p = 0.85, against p = 0.003 now (hypothesis 5a).
- **The per-account intervals are about half as wide as the data supports.** Neighbouring
  stretches carry the same work, so the stretches are not independent (lag-1 autocorrelation
  0.3 to 0.6). Resampling whole days instead of single stretches roughly doubles every
  account's standard error. Under that resampling the three published changes are consistent
  with each other (p = 0.28) even before any revaluation (hypothesis 5b).
- **Max account 1's whole step is before the candidate.** Its after side reads -0.5%
  [-22.4, 27.5] against its own stretches from 21-22 September, and +78.3% against those from
  18-20 September. The level doubled on 21 September at 10:54Z, on Opus 5 work inside one
  Claude Code version (2.1.278). The stretches that make the jump are the first on this
  account dominated by Opus sub-agents.
- **Cloud sessions teleported onto gs make up 23.8% of Max account 2's after-side credits.**
  Removing the four stretches that carry them moves Max account 2 from +45.8% to +35.7%
  (hypothesis 4).

The mechanism behind the Opus sub-agent effect is not found. It is not duplicate turns. It is
not the five-minute cache TTL that sub-agents write with, which explains only a small part.
It shows on every account and on both sides of the candidate. So there is no contained fix,
and this branch commits the findings only. The decisions needed are at the end.

## 1. Unclaimed pooled work counted on two accounts: confirmed, small

`unclaimed_tokens` is the pooled projects root's work that no config dir's `session-env`
claims. Max accounts 2 and 4 read the same pooled root, so both get the same unclaimed turns
for any span they share. In the selection, 26 pairs of their stretches overlap in time with
unclaimed work on both sides of the pair. `unclaimed_shares` fits the two accounts
independently: Max account 2 at 1.0 [0.98, 1.0] and Max account 4 at 0.82 [0.0, 1.0]. That
is 1.82 of one pool.

| Max account 2, at comparison value | before | after |
|---|---|---|
| stretches (with unclaimed work) | 41 (9) | 17 (17) |
| unclaimed credits / own credits | 3.0% | 6.2% |

Max account 2's change at a fixed share for both accounts:

| share | 0 | 0.25 | 0.5 | 0.75 | 1.0 (published) |
|---|---|---|---|---|---|
| Max account 2 | +42.4% | +43.2% | +44.1% | +44.5% | +45.8% |
| combined | +52.6% | +52.9% | +53.1% | +53.0% | +53.1% |

Max account 4 has no before side, so its share moves no change figure. If the two shares must
sum to at most 1, Max account 2's share can be anywhere from 0 to 1. Its change then lies
between +42.4% and +45.8%, which is 3.4 points of a 57.8-point spread between Max accounts 1
and 3.

A further point: on gs, the bare `~/.claude` login and `~/.claude-gmail-monitor` are logged in
to Max account 1, not to any gs account. So "the accounts active in the span" is not only Max
accounts 2 and 4.

## 2. Sub-agent files counted twice: ruled out

All transcripts on gs modified since 14 September were scanned: the pooled root, Max account
3's own root and the gmail-monitor root. That is 4,366 files and 125,163 turns.

- **No session is claimed twice.** No session id appears in more than one config dir's
  `session-env`. This holds for all 10 pairs of `~/.claude`, `-javiswork`, `-dave`, `-avis`
  and `-jono`.
- **No turn is shared between accounts.** 1,236 message ids occur in more than one file. Every
  one of those files belongs to a single owner:
  - 989 are Max account 2 main transcripts from resumed sessions (a different session id,
    the same message);
  - 203 are Max account 4 sub-agent files of one session;
  - 43 are Max account 3 files from before the cut;
  - 1 is a Max account 2 sub-agent turn that also appears in its parent.

  Each is read inside one account's single `iter_turns` call, whose per-read `seen` set drops
  the repeat. None falls between an account's own files and the unclaimed files, which are
  read in a separate call.
- **Parent transcripts carry no sidechain turns.** The gs parent transcripts contain 0
  assistant lines with `isSidechain` set.
- **No file is reachable by two paths.** Max account 3's root holds 4 symlinked sub-agent
  files. They point inside the same root and date from 13 September, before the cut; the
  per-read dedup covers them. masterrig's `~/.claude/projects` holds no symlinks at all.
- **Every turn is its own request.** On gs, every message id in a sub-agent file has its own
  `requestId`: 0 ids share one. No consecutive Opus sub-agent turns repeat each other's usage.
  This check was not run on masterrig.

## 3. Other families mis-valued: ruled out as the cause

Credits by family and token class, as shares of each side's credits. Opus 5.5 is valued at the
joint fit's 0.946x Opus. The other families are at the pooled fit's point estimates: Sonnet
0.363, Haiku 0.701, Fable 2.135. Cache reads are at the fitted weight of 0.0047.

| | credits per 1% | Opus 5.5 | Opus (5, 4.7, 4.8) | Fable | Sonnet | Haiku | cache read |
|---|---|---|---|---|---|---|---|
| Max account 1, before | 183,188 | 0 | 0.440 | 0.338 | 0.135 | 0.002 | 0.084 |
| Max account 1, after | 305,552 | 0.530 | 0.266 | 0 | 0.102 | 0.004 | 0.096 |
| Max account 2, before | 216,345 | 0 | 0.514 | 0.290 | 0.089 | 0.005 | 0.102 |
| Max account 2, after | 329,418 | 0.820 | 0.003 | 0 | 0.071 | 0.003 | 0.101 |
| Max account 3, before | 166,116 | 0 | 0.466 | 0.156 | 0.205 | 0.029 | 0.144 |
| Max account 3, after | 191,826 | 0.780 | 0 | 0.007 | 0.103 | 0 | 0.107 |

Max account 1's after side is not heavy in Sonnet, Haiku or Fable. What it carries that the
others do not is the older Opus family, at 26.6%: by model, Opus 5 is 15.3% of its credits and
Opus 4.7 is 12.9%.

The effect of moving one family's rate (or one token class's weight) on the three changes:

| valuation | Max account 1 | Max account 2 | Max account 3 | spread, 1 minus 3 |
|---|---|---|---|---|
| published | +74.4% | +45.8% | +16.6% | 57.8 |
| Fable x0.5 / x1.5 | +116.9 / +43.9 | +74.8 / +25.9 | +26.9 / +8.9 | 90.0 / 35.0 |
| Sonnet x0.5 / x1.5 | +76.8 / +72.3 | +48.2 / +45.1 | +23.8 / +10.8 | 53.0 / 61.5 |
| Haiku x0.5 / x1.5 | +74.2 / +74.7 | +45.8 / +46.4 | +17.7 / +14.4 | 56.5 / 60.3 |
| Opus x0.5 / x1.5 | +80.7 / +66.9 | +92.4 / +20.5 | +51.7 / -4.7 | 29.0 / 71.6 |
| Opus 5.5 x0.5 / x1.5 | +28.6 / +116.4 | -12.6 / +105.1 | -28.7 / +61.4 | 57.3 / 55.0 |
| cache read x0 / x2 | +71.4 / +76.6 | +47.7 / +44.6 | +21.8 / +13.6 | 49.6 / 63.0 |
| output x0.6 / x1.4 | +80.9 / +70.0 | +52.4 / +44.2 | +13.8 / +18.2 | 67.1 / 51.8 |

- Sonnet across its whole pooled interval (0.29 to 0.44, about x0.8 to x1.2) moves Max
  account 1 by under 2 points.
- Fable's interval is narrow (2.01 to 2.26), and Fable scales every account in the same
  direction.
- The only single change that closes much of the spread is Opus at half its rate. That would
  make Opus 5.5 1.9x Opus, which neither fit supports.

Cache reads at weight 0 remove 8 of the 58 points.

## 4. Max account 1's transcripts against its meter

The masterrig transcripts were read on masterrig, and the gs ones on gs. Each turn was placed
in the selected stretch whose span holds it.

| work | where its transcript lies | account it bills | share of Max account 1's credits, before / after | Max account 1 with it corrected |
|---|---|---|---|---|
| Mission Control seats (paperclip workspaces and worktrees) | masterrig `~/.claude/projects` | "account 3" in masterrig's seat-auth README, which is gs's `~/.claude-javiswork` login, i.e. Max account 2; the state on 14-22 Sep is not recorded on disk | 11.9% / 0 | +90.2% [52.5, 137.4] if it billed Max account 2 |
| gs `~/.claude` and `~/.claude-gmail-monitor` sessions | gs | Max account 1 (same account uuid as masterrig's login) | +0.13% / +0.41% missing | +75.1% |
| cloud sessions teleported onto masterrig (`remoteSourced` lines) | masterrig | not determinable | 0 / 5.1% | see below |

The seat work lowers Max account 1's before side if anything. Moving it to Max account 2 would
raise Max account 1's change, so it does not explain the high reading. The same seat turns are
12.3% of Max account 2's before-side credits by span. Adding them there leaves Max account 2
at +45.8%, because they fall in a few long stretches that the Huber estimate weighs down.

Masterrig's meter also counts claude.ai on the web, the phone app and any other machine. No
file records that usage.

Nothing else on masterrig changes at the candidate. By credits, the Claude Code version is
2.1.275 to 2.1.278 before and 2.1.280 to 2.1.284 after, which is the release that resolves
`opus` to Opus 5.5. The step inside the before side, on 21 September at 10:54Z, happens within
2.1.278.

### Cloud sessions (`remoteSourced`)

Transcript lines marked `"remoteSourced": true` are copies of cloud sessions pulled onto the
host by `claude --teleport`. On gs they sit in `cgrab` working-directory projects under Max
account 2's login, which claims them through `session-env`. The on-demand notes record cloud
sessions on that login from 25 September. There are 2,443 such turns on gs and 325 on
masterrig. All fall on the after side:

| stretch (Max account 2) | credits per 1% | cloud share of credits | credits per 1% without the cloud turns |
|---|---|---|---|
| 25 Sep 11:47 to 19:36 | 373,300 | 0.86 | 52,774 |
| 25 Sep 19:36 to 26 Sep 19:42 | 617,427 | 0.96 | 26,872 |
| 26 Sep 19:42 to 27 Sep 08:52 | 525,152 | 0.53 | 244,494 |
| 27 Sep 08:52 to 19:18 | 387,942 | 0.45 | 215,030 |
| the other 13 after-side stretches, range | 155,331 to 394,079 | 0 | |

The meter moved on this work: without it, two of the stretches read 27k and 53k credits per 1%.
But it reads well above the account's other stretches. Fitting each of the four to the median
of the other 13 puts the cloud turns at roughly 0.1 to 0.7 of their credit value. The
duplicate checks above find no repeated turns among them.

Whether cloud-session work bills the five-hour meter at the full rate is not determinable from
these four stretches. Removing the six stretches that carry cloud turns (four on Max account 2,
two on Max account 1) gives:

- Max account 1: +65.2% [45.6, 87.5]
- Max account 2: +35.7% [14.9, 60.1]
- combined: +45.6% [33.3, 59.1]

## 5. What the numbers point to

### 5a. Opus sub-agent work reads about twice its credit value

Each stretch's sub-agent share was measured from the transcripts: the credits from
`<session>/subagents/*.jsonl` turns in its span, over all its credits. The Opus family (Opus 5
and Opus 5.5 sub-agents) and the other families are kept apart.

| mean share of credits | Opus sub-agents, before | after | other sub-agents, before | after |
|---|---|---|---|---|
| Max account 1 | 0.05 | 0.34 | 0.19 | 0.11 |
| Max account 2 | 0.36 | 0.41 | 0.14 | 0.06 |
| Max account 3 | 0.21 | 0.00 | 0.25 | 0.03 |
| Max account 4 | | 0.18 | | 0.63 |

Within each account and side, log credits per 1% rises with the Opus sub-agent share. The slope
is 0.640, se 0.110, over 192 stretches. It holds on each account alone, on each side alone,
and with any one account left out:

| subset | slope (se) |
|---|---|
| Max account 1 / 2 / 3 / 4 alone | 0.68 (0.28) / 0.36 (0.17) / 1.37 (0.21) / 0.47 (0.27) |
| without Max account 1 / 2 / 3 / 4 | 0.64 (0.13) / 1.02 (0.15) / 0.42 (0.13) / 0.65 (0.12) |
| before side / after side | 0.88 (0.15) / 0.25 (0.13) |

Revaluing Opus sub-agent work by a factor gives the following. SS is the within-side sum of
squares of log credits per 1% (26.61 as published). The per-account changes here use equal
rounding variance and no unclaimed share, so the first row reads 73.3 / 42.8 / 16.7, not the
published 74.4 / 45.8 / 16.6.

| Opus sub-agent value | SS | Max account 1 | Max account 2 | Max account 3 | heterogeneity Q (p) |
|---|---|---|---|---|---|
| x1.0 | 26.61 | +73.3% [52.9, 96.5] | +42.8% [20.0, 69.9] | +16.7% [-4.8, 43.1] | 11.64 (0.003) |
| x0.8 | 24.33 | | | | |
| x0.6 | 22.90 | | | | |
| x0.5 | 22.70 | +35.7% | +29.5% | +29.2% | 0.32 (0.85) |
| x0.4 | 23.04 | | | | |
| x0.5, other sub-agents x1.45 (joint best) | 21.54 | +32.5% [15.3, 52.2] | +26.3% [11.1, 43.6] | +18.0% [1.0, 37.7] | |

At x0.5 the three accounts pool to +31.5% [21.5, 42.4]. The joint fit was not rerun: its
valuation cannot take a per-turn adjustment without the collector recording sub-agent tokens
separately.

Explanations checked and found not to be it:

- **Duplicate turns.** None were found (section 2).
- **Cache TTL alone.** Main sessions write their cache with a one-hour TTL and sub-agents with
  the five-minute TTL. On gs, the 1h share of main-session cache writes is 0.99 to 1.00 for
  every family but Haiku (0.64), and masterrig's CLI sessions are the same. Sub-agent writes
  are 0.00 1h on both hosts. At list price a one-hour write costs 1.6x a five-minute one, and the
  valuation prices both as input. Pricing them 2 : 1.25 moves the three changes to +66.0%,
  +47.5% and +28.7%, and the intervals then overlap. But fitting a five-minute weight to the
  within-side scatter gives 0.7 and lowers SS only from 26.61 to 26.11. Sonnet sub-agents
  also write at five minutes, and their fitted value is above 1, not below.
- **Context length.** Measured as the credit-weighted share of turns above 200k tokens of
  context, the within-side correlation is r = 0.10.
- **Claude Code version.** Max account 1's step happens within 2.1.278 (section 4).

Max account 1's step on 21 September is where its Opus sub-agent work begins. The two stretches
from 10:54Z and 13:26Z read 370,124 and 385,732 credits per 1%, against a geometric mean of 165,785 for
18-20 September. They are 62% and 68% sub-agent work, mostly Opus 5, from one project's run.
Before 21 September, Max account 1's Opus sub-agent share of credits averages under 0.05. Max account 3's after side has no Opus sub-agent work at all.

| Max account 1 | n | geometric mean credits per 1% |
|---|---|---|
| before, 18-20 Sep | 40 | 165,785 |
| before, 21-22 Sep | 5 | 297,780 |
| after | 11 | 299,726 |

### 5b. The intervals treat neighbouring stretches as independent

Consecutive stretches share sessions and work, so their residuals are correlated. Lag-1
autocorrelation within a side:

| | before | after | days before / after |
|---|---|---|---|
| Max account 1 | 0.60 | -0.58 | 5 / 4 |
| Max account 2 | 0.34 | 0.38 | 7 / 7 |
| Max account 3 | 0.40 | 0.29 | 5 / 2 |

A bootstrap that resamples whole UTC days within each side, on the mean of the logs:

| | iid se (published method) | day-block se | day-block 95% |
|---|---|---|---|
| Max account 1 | 0.063 | 0.137 | [+11.1, +90.6] |
| Max account 2 | 0.087 | 0.147 | [+14.2, +104.5] |
| Max account 3 | 0.102 | 0.157 | [-2.3, +79.7] (2 days after) |
| heterogeneity Q (p) | 11.64 (0.003) | 2.51 (0.285) | |
| pooled | +51.9% [38.9, 66.0] | +49.9% [27.1, 76.9] | |

With Opus sub-agent work at x0.5 and day blocks, the three read +42.9%, +48.7% and +38.3%
(Q = 0.17, p = 0.92), pooled +42.7% [23.4, 65.1].

## The causes, in order of size

1. **Opus-family sub-agent work reads about twice its credit value, and its share changes
   across the candidate differently on each account.** This accounts for most of the spread.
   At x0.5, Max account 1 falls from +73.3% to +35.7% and Max account 3 rises from +16.7% to
   +29.2%.
2. **The per-account intervals are too narrow by about 2x.** The disagreement is
   "significant" (p = 0.003) only under the iid t interval. Neighbouring stretches carry the
   same work, and resampling by day gives p = 0.28.
3. **Cloud-session transcripts make up 23.8% of Max account 2's after side** and read above
   the account's other work: -10.1 points on Max account 2 with those stretches removed.
4. **Unclaimed work is given to two accounts at once**: at most -3.4 points on Max account 2.

Ruled out: sub-agent or transcript double counting across or within accounts, sidechain
lines, Sonnet, Haiku or Fable rates, the cache-read weight, the Claude Code version, and
Max account 1 transcript work billed elsewhere. The seat work is on the before side and would
widen the spread if moved.

## Decisions needed

None of the four has a contained fix that is not a judgement call.

1. **Opus sub-agent valuation.** Should sub-agent tokens be recorded apart from main-session
   tokens per stretch (collector change on both hosts, `tracker/gs_passive.py`), and given
   their own coefficient in the pooled fit? The data put Opus sub-agents at about half their
   credit value. Why the meter charges them less is not known, so a fixed 0.5 would be a
   guess. A fitted coefficient would be a measurement.
2. **Interval method.** Should `log_ratio_side` (and the joint fit's bootstrap) resample
   blocks of neighbouring stretches, by UTC day or by session, instead of single stretches?
   This roughly doubles the per-account standard errors. It moves the combined interval from
   [40.4, 67.1] towards [27, 77], and with it the published state.
3. **Cloud sessions.** Should `remoteSourced` turns be recorded separately per stretch, like
   `unclaimed_tokens`, with a fitted share, instead of counted in full?
4. **Unclaimed shares.** Should the shares be fitted jointly across the accounts that read one
   pooled root, constrained to sum to at most 1? Max account 1's logins on gs also draw on
   that pool.
