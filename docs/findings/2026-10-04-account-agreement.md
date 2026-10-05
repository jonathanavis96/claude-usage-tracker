# Why the accounts disagree, and why 22 September does not recover the +20% (2026-10-04)

Issue #139. Investigation only: nothing published changes on this branch. All figures come
from `tools/account_agreement.py` (with `tools/account_transcripts.py`) over the history
committed at f42691a (live JSON of 2026-10-04 19:00Z) and the gs transcripts and meter logs
read on 2026-10-04 at about 20:00Z. Accounts: a1 = masterrig, a2 = jwork, a3 = dave,
a4 = avis. masterrig's transcripts and meter log are not on gs, so every transcript and
crossing check below covers a2 to a4 only.

"At list" values every model family at its input list-price ratio to Opus 5 in
data/prices.json (Sonnet 0.4, Haiku 0.2, Fable 2.0, Opus 5.5 0.8, Sonnet 5.5 0.4), with
cache reads at the fitted cache-read weight and output at 5x input, as the pooled fit does.
"New at list" changes only Opus 5.5 and Sonnet 5.5 and leaves the older families at their
fits.

## Summary

1. **Opus 5.5 at list: +31.7% [20.6, 43.8] pooled.** That interval holds the announced +20%,
   but the accounts do not agree: a1 +53.8%, a2 +17.4%, a3 +3.4%. 29 September (Sonnet 5.5
   at list) reads +4.7% [-1.8, 11.6].
2. **The accounts agree on the seven-day meter and disagree on the five-hour meter.**
   Between exact seven-day crossings, credits per 1% of the **seven-day** meter agree within
   -3% to +9% in every regime (22-29 September: a2 1.13M, a3 1.23M, a4 1.16M). Credits per
   1% of the **five-hour** meter do not (a2 278k, a3 164k, a4 287k). The cause is a meter
   ratio, not a token count. Dave's five-hour meter moves 5.4 to 7.5 points per seven-day
   point, against 4.0 to 5.0 on a2 and a4. Measured against the same work, Dave's five-hour
   percent is 0.55 to 0.8 the size of the others'. No transcript fix closes this, because
   nothing is miscounted: every miscounting hypothesis in the brief was checked and ruled
   out, or bounded at a few percent (section 2).
3. **Windows per week from tiled exact crossings** (section 3). Across 22 September the
   combined change is +5.6% [-4.7, 17.0]: a2 -7.3% [-18.2, 5.0], a3 +38.6% [15.7, 66.0].
   A +20% five-hour window with the weekly limit unchanged predicts -16.7%. Only a2's
   interval reaches that. Across 29 September the combined change is +11.1% [1.9, 21.2].

The premise that accounts on one plan agree per 1% of the five-hour meter does not hold
for Dave's account. They agree per 1% of the seven-day meter instead. Decisions needed are
at the end.

## 1. The 22 and 29 September changes at list price

This is the known-date test exactly as the publisher runs it (`credits.announced_change`:
Huber levels, robust scatter plus rounding, inverse-variance combination, the fitted
unclaimed share). Only the valuation differs. The `fitted` row reproduces the live JSON
exactly.

| 22 Sep, Opus 5.5 | combined | a1 | a2 | a3 | a4 |
|---|---|---|---|---|---|
| fitted (live: Opus 5.5 at 0.609) | +11.3% [1.9, 21.6] | +37.0% [20.6, 55.6] | -4.6% [-19.2, 12.5] | -15.3% [-30.3, 3.0] | no before side |
| new families at list (0.8) | **+31.7% [20.6, 43.8]** | +53.8% [35.9, 74.0] | +17.4% [-1.2, 39.4] | +3.4% [-15.0, 25.7] | no before side |
| every family at list | +33.6% [22.4, 45.9] | +57.1% [38.7, 78.1] | +19.5% [0.6, 41.9] | +4.6% [-13.7, 26.8] | no before side |

n before/after: a1 45/9, a2 41/13, a3 52/13, a4 0/13.

| 29 Sep, Sonnet 5.5 | combined | a1 | a2 | a3 | a4 |
|---|---|---|---|---|---|
| fitted (live: Sonnet 5.5 at 0.516) | -6.6% [-11.1, -2.0] | +21.8% | +3.0% | +12.3% | -15.8% |
| new families at list (0.4) | **+4.7% [-1.8, 11.6]** | +32.8% [14.6, 53.8] | -8.0% [-21.2, 7.3] | +10.9% [-8.7, 34.6] | -1.7% [-10.7, 8.4] |
| every family at list | +3.1% [-3.3, 10.0] | +32.3% | -8.4% | +9.1% | -4.2% |

- Valuing Opus 5.5 at list moves the pooled 22 September figure from +11.3% to +31.7%, and
  +20% is inside the interval. The fitted 0.609 is what removed the step, as the brief
  suspected. The step and the Opus 5.5 rate cannot be separated at the fit.
- The accounts still disagree by 50 points. a3's +3.4% is what section 2 explains: the
  five-hour meter itself behaves differently on that account. a1's +53.8% carries its
  Opus sub-agent step of 21 September, which
  docs/findings-2026-09-30-per-account-spread.md already places before the candidate.
- 29 September: at list there is no change (+4.7%, interval includes 0). a1's +32.8% is
  the overnight agent run identified in docs/findings-2026-10-02-one-account-change.md.

Reproduce: `python3 -m tools.account_agreement change`.

## 2. Where the accounts disagree

### 2a. Credits per 1% of the five-hour meter, meter-weighted

Selection as the known-date test: accepted stretches, harness runs and cloud sessions out,
masterrig from 6 September. The figure is sum of credits over sum of percent. Thousands of
credits per 1%.

| | valuation | before 14 Sep | 14-22 Sep | after 22 Sep |
|---|---|---|---|---|
| a1 masterrig | fitted | 163 (392%) | 182 (487%) | 251 (296%) |
| a2 jwork | fitted | 181 (829%) | 218 (420%) | 211 (224%) |
| a3 dave | fitted | none accepted | 163 (548%) | 142 (297%) |
| a4 avis | fitted | | | 235 (235%) |
| a1 masterrig | at list | 157 | 179 | 300 |
| a2 jwork | at list | 178 | 215 | 253 |
| a3 dave | at list | none accepted | 161 | 175 |
| a4 avis | at list | | | 267 |

At list, a3 reads -25% against a2 for 14-22 September and -31% for after 22 September. a4
reads +6% against a2 after 22 September. A weighted regression of each stretch's log credits
per 1% on account and era gives a3 at **-27.7%** and a4 at +10.0% against a2.

These are not the brief's 318k / 322k / 226k, which are all stretches at another
valuation. The ratio between the accounts is the same.

Reproduce: `python3 -m tools.account_agreement eras` and `... mix`.

### 2b. Every miscounting hypothesis, quantified

The transcripts were read as the collector reads them (`_split_transcripts`, one `seen` set
per account) from 14 September: a2 42,867 turns, a3 62,490, a4 38,049, plus 2,208
unclaimed.

**Double counting across accounts: none.**

- 0 message ids are shared between any two accounts' own turns (a2/a3, a2/a4, a3/a4).
- 0 are shared between any account and the unclaimed pool.
- 0 records with identical model, usage and second-level timestamp are shared between
  accounts. This test catches copies with new ids.

**Double counting within an account: handled.** 705 (a2), 43 (a3) and 203 (a4) message ids
occur in more than one file: resumed sessions, and sub-agent files that repeat their parent.
Each account is read through one `seen` set covering every file that feeds its stretches,
so each repeat counts once. The unclaimed pool is read separately, and its ids are disjoint
from every account's (above). For each selected stretch, the transcripts on disk give the
stretch record's credits to within 5% for 147 of 168 stretches. In the other 21 the disk
holds more, which is work written after the stretch was recorded, never less.

**Cross-account delegation: attributed correctly.**

- **`dave-delegate`.** All 233 runs with a surviving stream-json log
  (`~/var/delegates/*.jsonl`) have their transcript under `~/.claude-dave/projects`. None
  is in the pooled dir. Each run's own Claude Code total (`modelUsage` in its `result`
  event) matches its transcript:
  - Opus 5.5: output 9,001,705 vs 9,001,710; cache write 29,825,774 vs 29,830,826; cache
    read 1,522,563,709 vs 1,522,791,862.
  - Opus 5, Sonnet 5 and Sonnet 5.5 match exactly.
  - The only unrecorded spend is 7,218 Haiku input tokens of side calls.

  The launching session sees only the delegate's text result (a tool result), not its
  usage, so nothing is counted twice.
- **`dave-agent`** runs with Dave's setup token in `CLAUDE_CODE_OAUTH_TOKEN` and
  `CLAUDE_CONFIG_DIR=~/.claude-dave`, so credentials and transcript agree. A direct child
  process that set another config dir would bill Dave and write its transcript elsewhere.
  Across the 55 `dave-agent` sessions on disk (71 runs, 18-21 September and 2 October),
  their Bash commands do this only in three `claude config list` probes: no work.
- **`tmux`** children take the tmux server's environment
  (`CLAUDE_CONFIG_DIR=~/.claude-javiswork`, no token). Credentials and transcript dir still
  agree.
- **`dclaude`** strips the token. `takeoff/bin/run-build` unsets it.
- **The 1 October swarm.** 392 transcripts under the pooled `-masterrig-swarm` project
  ran on masterrig (a `cwd` under masterrig's home, `swarm/repos/...`) and were copied onto gs with
  `~/.claude-avis/session-env` entries. They are a4's spend:
  - a4's meter moved 51% in the 32 minutes they ran (10:46-11:18Z, about 230k credits per
    1%, in line with a4's other stretches);
  - masterrig's stretch over the same hours holds 2.1M credits in all;
  - so they are not in a1 as well.

**Unattributed or doubly claimed sessions: unchanged since 30 September.** No session id
has a `session-env` entry in two config dirs (a2 drops 1,321 files to `.claude-avis` and 327
to `.claude`; a4 the mirror). The 567 unclaimed files are the post-picker filing judge and
one test run (`tracker/unclaimed.py`). Their 2,208 turns are too few to move a figure.

**Spend with no transcript at all: small.**

- **graphify.** Every graphify LLM call runs `claude -p --no-session-persistence`
  (graphify `llm.py`), so it leaves no transcript. The refresh daemon has run under
  `CLAUDE_CONFIG_DIR=~/.claude-dave` since 2026-09-14T01:16Z, and the PostToolUse dirty
  hook feeds it from every account's edits. Its log shows 355 document extractions and 83
  timed-out workers from 14 September to 4 October, plus relabels.
  - Carrying the ~40% of Dave's credits that its five-hour reading lacks would take about
    600k Sonnet input-equivalent tokens per call.
  - The post-commit hook exits in linked worktrees, which is where delegates commit.
  - This is not measured, because graphify records no usage. It is bounded by section 2c:
    a3's seven-day meter agrees with the others with this spend included.
- **Hooks.** No other hook calls a model. `airlock-belay-run` calls Jev (TypeSafe), not
  Claude. `memory-keeper.py`, cache-guard and airlock make no model calls.
- **Use of Dave's seat elsewhere** (claude.ai, desktop, phone): the meter would count it.
  On gs, 9 five-hour ticks fell in 71 idle UK working hours and 6 in 209 idle off-hours,
  out of 854 ticks (idle means no a3 turn within the previous 15 and next 5 minutes). At
  those rates, such use is about 2% of a3's ticks.

**Work mix: real, but not the cause.** Shares of credits at list, from the transcripts on
disk:

| | per 1% (k) | sub-agent | headless (`sdk-cli`) | interactive | Opus 5.5 | 1h write | 5m write | cache read |
|---|---|---|---|---|---|---|---|---|
| a2 14-22 Sep | 217 | 0.51 | 0.04 | 0.45 | 0.00 | 0.16 | 0.28 | 0.09 |
| a2 after 22 Sep | 258 | 0.46 | 0.13 | 0.40 | 0.83 | 0.21 | 0.23 | 0.12 |
| a3 14-22 Sep | 164 | 0.52 | 0.31 | 0.17 | 0.00 | 0.16 | 0.24 | 0.13 |
| a3 after 22 Sep | 178 | 0.03 | 0.97 | 0.01 | 0.90 | 0.35 | 0.02 | 0.10 |
| a4 after 22 Sep | 278 | 0.52 | 0.24 | 0.24 | 0.57 | 0.23 | 0.27 | 0.15 |

- a3 reads low in 14-22 September at the same sub-agent share as a2 (0.52 against 0.51).
  So sub-agent work is not what separates the accounts.
- The strongest single variable is the headless share. A weighted regression of a stretch's
  log credits per 1% on it has slope -0.48 (se 0.15) within a3 14-22 September, -0.29
  (0.11) within a4, and -0.64 (0.19) within a2 after 22 September.
- Adding the headless share takes a3's offset from -27.7% to -12.0%.
- Fitting meter % against credits split by source, with one scale for all accounts, gives:
  - sub-agent work at 1.14x [0.91, 1.42] of interactive work, so no sub-agent anomaly
    remains;
  - headless work at 1.79x [1.46, 2.24];
  - account residuals of a2 +2.0%, a3 -3.8% and a4 +8.8%.
- That fit is a diagnosis, not a proposal. It is a fitted coefficient of exactly the kind
  ruled out, and section 2c shows what it is really absorbing.
- Pricing cache writes at the API's own multiples (5-minute 1.25x, 1-hour 2x input, reads
  0.1x) does not help: a3 -29.3%, and the fit is worse (SS 367.6 against 245.7).

### 2c. The cause: Dave's five-hour meter, not Dave's tokens

The test needs no stretch. For every one-point step of the seven-day meter, take the
account's own credits and the five-hour points crossed. The steps are tiled between
consecutive exact seven-day crossings, each crossing placed at the upper reading of its
bracket. Harness runs and recorded cloud-session spans are left out. Intervals resample
whole UTC days. Credits are at list.

| regime | account | seven-day points (days) | credits per 1% seven-day | credits per 1% five-hour | five-hour per seven-day point |
|---|---|---|---|---|---|
| 14-22 Sep | a2 | 94 (7) | 0.95M [0.81, 1.14] | 217k [185, 256] | 4.39 [4.29, 4.66] |
| 14-22 Sep | a3 | 97 (4) | **0.92M** [0.81, 1.04] | 169k [132, 196] | **5.41** [4.99, 6.95] |
| 22-29 Sep | a2 | 28 (6) | 1.13M [1.06, 1.18] | 278k [229, 309] | 4.07 [3.69, 4.67] |
| 22-29 Sep | a3 | 16 (2) | **1.23M** [1.20, 1.27] | 164k [155, 170] | **7.50** [7.10, 8.17] |
| 22-29 Sep | a4 | 29 (3) | 1.16M [0.96, 1.19] | 287k [275, 319] | 4.03 [3.00, 4.11] |
| from 29 Sep | a2 | 12 (5) | 1.28M [1.18, 1.45] | 256k [239, 274] | 5.00 [4.87, 5.38] |
| from 29 Sep | a3 | 16 (6) | **1.26M** [1.13, 1.42] | 182k [147, 229] | **6.94** [6.07, 7.76] |
| from 29 Sep | a4 | 13 (2) | 1.16M [1.14, 1.25] | 225k [213, 313] | 5.15 [4.00, 5.36] |

- **Per 1% of the seven-day meter, the three accounts agree within a few percent in every
  regime:** a3 against a2 -3%, +9% and -2%; a4 against a2 +3% and -9%.
- **Per 1% of the five-hour meter, a3 is 22% to 41% below a2.** The ratio of the two meters
  accounts for all of it: a3's five-hour meter crosses 1.23x, 1.84x and 1.39x as many
  points per seven-day point as a2's.
- So, measured against the same work, a3's five-hour percent is 0.81, 0.54 and 0.72 the
  size of a2's. The same work moves both meters, and a3's seven-day meter reads it like
  everyone else's.
- So the work is counted correctly, and a3's **five-hour limit is smaller relative to its
  weekly limit** than a2's or a4's.
- This also explains the headless effect in 2b: a3's work is the headless work.

All three are `organizationRateLimitTier: default_claude_max_20x`. In the `oauthAccount`
records, a3 differs in two flags: `hasExtraUsageEnabled: true` (a2 and a4 false) and
`penguinModeOrgEnabled: true` (fast mode at org level). Fast-mode turns appear only on a2
(7 stretches, 35 turns). Whether extra usage or some other account-level setting changes
the five-hour window is not determinable from disk.

Caveats:

- a3's seven-day meter sits at 100% for long periods (extra usage). That leaves only 2 days
  of crossings in 22-29 September and 4 in 14-22 September, so its intervals are narrow
  because there are few days, not because the estimate is precise.
- An earlier version of this method counted only five-hour crossings wholly inside the gap
  between two seven-day crossings. It dropped the work of any burst whose crossings shared
  one sample pair (a4's swarm read 0.45M per 1% that way). The tiled version does not.

### 2d. How close the correction gets

On credits per 1% of the seven-day meter the accounts already agree within a few percent:
-3% to +9% (a3), -9% to +3% (a4). That is the brief's target. On credits per 1% of the
five-hour meter they cannot agree while the meter ratio differs. a3's five-hour reading
needs a3's own windows-per-week ratio, not a transcript fix. Valuing a3's five-hour credits
per 1% times its windows per week over a2's (5.41/4.39, 7.50/4.07, 6.94/5.00) puts it at
-4%, +9% and -1% against a2.

Not resolved:

- a1 cannot be checked this way from gs (no meter log or transcripts here).
- What sets a3's window is not known.
- The within-account headless slopes on a2 and a4 are smaller than a3's offset but are not
  zero. Whether they are the same effect in miniature (a seven-day check per stretch is not
  possible) is open.

## 3. Windows per week from exact crossings

`windows_per_week`: d5 over d7 over each regime's whole span, from the tiled one-point
seven-day steps (meter only, no transcripts, so harness runs and cloud work count as they
did on the meters). Per account, day-block bootstrap. The per-account log changes are
combined by inverse variance, since the accounts' levels differ and a4 starts only on
23 September.

| | 14-22 Sep | 22-29 Sep | from 29 Sep |
|---|---|---|---|
| a2 | 4.40 [4.29, 4.67] (95 points, 7 days) | 4.13 [3.73, 4.72] (30, 7) | 5.00 [4.87, 5.38] (12, 5) |
| a3 | 5.41 [4.99, 6.95] (97, 4) | 7.50 [7.10, 8.17] (16, 2) | 6.94 [6.06, 7.77] (16, 6) |
| a4 | | 4.03 [3.00, 4.11] (29, 3) | 5.15 [4.00, 5.36] (13, 2) |

| change | a2 | a3 | a4 | combined |
|---|---|---|---|---|
| 22 Sep | -7.3% [-18.2, 5.0] | +38.6% [15.7, 66.0] | no before side | +5.6% [-4.7, 17.0] |
| 29 Sep | +22.8% [8.1, 39.5] | -7.5% [-19.7, 6.6] | +27.7% [3.0, 58.4] | +11.1% [1.9, 21.2] |

The changes are from `meters`, the same tiled steps with harness runs and cloud spans out,
each account's interval read as log-normal from its day bootstrap. With nothing left out
(`wpw`), 22 September reads a2 -6.1% [-16.2, 7.2] and a3 +38.6% [6.8, 62.1].

The published figures were 0.966 [0.680, 1.428] and 1.077 [0.573, 2.135], so these intervals
are roughly a fifth to a tenth as wide.

A +20% five-hour window with the weekly limit unchanged predicts windows per week x 1/1.2,
**-16.7%**. On 22 September:

- a2's -7.3% [-18.2, 5.0] only reaches it at its edge.
- a3 moves the other way, by +38.6%: a3's own five-hour to seven-day ratio changed at
  22 September.
- The combined +5.6% [-4.7, 17.0] excludes -16.7%.

The seven-day meter suggests why. Credits per 1% of the seven-day meter rose across
22 September on both accounts with a before side: a2 +18.8% [-0.4, 41.6], a3 +34.1%
[17.5, 53.0], combined +28.4% [15.5, 42.6], at list. The five-hour level on a2 rose +28.2%
[2.8, 59.8]. If both limits rose by about a fifth, windows per week stays put, which is what
a2 shows.

Across 29 September the weekly level is flat (combined +5.2% [-1.6, 12.5]), while windows
per week rises on a2 and a4 (+22.8%, +27.7%). Five-hour credits per 1% fall: a2 -8.2%, a4
-21.5%, combined -8.1% [-17.8, 2.7]. There was no announcement for that date.

Reproduce: `python3 -m tools.account_agreement wpw` and `... meters`.

## Decisions needed

1. **Credits per 1% of the five-hour meter cannot be required to agree across accounts.**
   On a3 the five-hour meter runs 1.2x to 1.8x faster against the seven-day meter, with the
   transcripts complete. Options:
   - compare and fit accounts per 1% of the seven-day meter, which agrees;
   - or scale each account's five-hour readings by its own measured windows per week;
   - or leave a3 out of five-hour figures until its window is understood.

   Any of these is an ADR 0001 rule change and goes in its own PR.
2. **The 22 September test at list price.** With Opus 5.5 at 0.8 the pooled step is +31.7%
   [20.6, 43.8], and the seven-day meter says the weekly limit rose by a similar amount.
   This only says how the maths would read with list prices as an input; announcements are
   a test, not an input. Whether the valuation should hold a new family at list rather than
   fit it at first use is a separate rule change.
3. **a3's account flags.** Whether `hasExtraUsageEnabled` (or anything else on that
   account) shrinks the five-hour window is a question for whoever administers that seat.
   The data cannot settle it.

## 4. Part 2: hunting a miscount in the five-hour count for a3 (2026-10-05)

Decision 2 came back as "can't explain, feel like it's still being miscounted". The
transcripts agree on the seven-day meter, so this section looks for the miscount on the
meter side: in the log, in how five-hour points are counted, and in how resets and the
weekly cap are handled.

All figures are from 14 September 12:00Z on, from the gs meter logs read on 2026-10-05.
Reproduce: `python3 -m tools.account_agreement fivehour`.

### Summary

**No miscount was found in the sampler, the log, the reset handling, window-start rounding
or the weekly-at-100% spans.** Each is checked below and none moves a3's ratio by more than
a few percent.

What does move it is the kind of work:

- In each seven-day step, five-hour points rise with the share of that step's credits that
  comes from headless `claude -p` runs (`entrypoint: sdk-cli`). This holds on every
  account, not only a3.
- At a low headless share, the three accounts agree within ±10%.
- Credits per 1% of the seven-day meter do not depend on the headless share. Credits per 1%
  of the five-hour meter do.
- So Anthropic's five-hour meter charges headless work about **1.5x** as much as
  interactive work, measured against the seven-day meter.
- a3's work is 97% headless after 22 September. Allowing for the headless share takes a3's
  excess from **1.33x** to **1.10x [1.03, 1.18]**.

So a3's ratio comes mostly into line, but not entirely. About 10% is left unexplained.

The headless effect is in the meters themselves. Both counts are whole-percent meter
crossings, and the transcripts only label which steps were headless. So it is not a
miscount the tracker can fix in its own counting. It is a real difference in what the
five-hour limit buys for headless work.

### 4a. Sampler attribution: clean

- Each log is written only by its own systemd timer: `tracker.meter_log --config-dir
  ~/.claude-<acct> --account <acct>`, once a minute.
- The token is read from that config dir's credentials file. The identity is a hash of
  that dir's account uuid.
- Each log carries exactly one identity across every row, and the three identities are
  distinct.
- No log has duplicate timestamps or out-of-order rows.
- Two to three pairs per log are under 20 s apart: retries, not a second writer.
- Every five-hour value is a whole number (a3: 0 of 8,899 readings are not).
- Coverage, as the share of time between readings at most 15 minutes apart, is a3 97%,
  95% and 88% by regime, against a2 100%, 92% and 99% and a4 93% and 85%.
- The many error rows are the endpoint's `rate_limited` answers. They are spread evenly
  across accounts (a2 7,356, a3 6,430, a4 6,524).

### 4b. Window-start rounding: wrong direction, small

If the meter showed 1% as soon as any usage landed, every window would carry up to a point
of bias, and accounts with many small windows would read high. a3 is the opposite:

| windows | 14-22 Sep | 22-29 Sep | from 29 Sep | median peak, by regime |
|---|---|---|---|---|
| a2 | 36 | 24 | 17 | 8, 5.5, 2 |
| a3 | 18 | 6 | 15 | 25.5, 29, 4 |
| a4 | | 28 | 18 | 0, 0 |

- a3 opens 3.2 windows a day against a2's 4.2 and a4's 4.3, and its windows run fuller.
- Almost every window's first reading is 0: a2 71 of 77, a3 36 of 39, a4 45 of 46.

Leaving out each window's first one or two five-hour points (five-hour points per
seven-day point, by regime):

| | all | first point out | first two out |
|---|---|---|---|
| a2 | 4.40 / 4.13 / 5.00 | 4.19 / 3.87 / 4.17 | 3.97 / 3.60 / 3.33 |
| a3 | 5.41 / 7.50 / 6.94 | 5.28 / 7.31 / 6.62 | 5.13 / 7.12 / 6.25 |
| a4 | — / 4.03 / 5.15 | — / 3.86 / 5.08 | — / 3.69 / 5.00 |

Every account falls by a similar amount, and a3 stays 1.2x to 1.9x above a2.

### 4c. Reset handling: correct

- **Resets match the transcripts.** For a window opened by the account's own work, the
  recorded reset falls 4.94 h (median; p10 4.86, p90 5.00) after a3's first turn after the
  previous reset. a2 reads 4.95 h and a4 4.93 h. A five-hour window starting at that turn,
  with its reset on the hour, gives exactly this.
- **No reset is missed or misread.** a3 shows 63 resets, a2 85 and a4 51, and none is
  less than 4.95 h after the one before.
- **No transient dips.** A drop within one `resets_at` to a non-zero value would be
  counted as a new window and its points counted twice. There are 0 such dips on every
  account.
  - The non-zero drops that do occur (a3 2, a2 4, for example 73 to 1) are resets that fell
    inside a sample gap.
  - `crossings` does not count across them, which loses at most the new window's first
    point or two.
  - Every drop to 0 on a3 falls at its recorded reset. On a2, one is 3 minutes early.

### 4d. Weekly at 100% with extra usage: excluded already

- a3's seven-day meter sits at 100% for days at a time (extra usage on).
- At 100% the seven-day meter cannot cross, so a tiled step never spans that time, and no
  five-hour point from it enters a ratio.
- Leaving out steps at a seven-day value of 95 or more changes a3 from 5.41 to 5.18 in
  14-22 September and not at all after.
- a3 shows no seven-day crossings from 24 to 27 September because it did no work then: its
  transcripts hold 0.0, 0.0 and 0.7M credits on 25 to 27 September.

### 4e. What differs: headless work moves the five-hour meter more

Five-hour points per seven-day point, by the headless share of the step's credits. Seven-day
points are in brackets.

| headless share | a2 | a3 | a4 | all |
|---|---|---|---|---|
| under 0.2 | 4.33 (129) | 4.80 (64) | 4.03 (31) | 4.42 (224) |
| 0.2 to 0.8 | 5.38 (8) | 5.86 (14) | 5.00 (2) | 5.62 (24) |
| over 0.8 | | 7.24 (50) | 6.12 (8) | 7.09 (58) |

Pooled over the accounts:

- Steps under 0.2 headless cost 1.00M credits per seven-day point and 226k per five-hour
  point.
- Steps over 0.8 cost 1.15M per seven-day point and 163k per five-hour point.
- So the seven-day meter charges both kinds of work alike, and the five-hour meter charges
  headless work about 1.4x more per credit.

A quasi-Poisson regression of five-hour points per seven-day step, on regime and account:

| model | a3 | a4 | headless share 0 to 1 | deviance |
|---|---|---|---|---|
| account | 1.33x [1.25, 1.42] | 0.93x [0.83, 1.04] | | 116.6 |
| account + headless | **1.10x [1.03, 1.18]** | 0.94x [0.85, 1.04] | **1.53x [1.41, 1.67]** | 90.5 |

The headless share is the only work variable that does this.

- Measured per standard deviation, on the same model, none of these does it:
  - the cache-read share: 0.97x;
  - the output share: 1.03x;
  - the five-minute write share: 0.91x;
  - turns per credit: 0.99x.

  With any of them in place of the headless share, a3 stays at 1.29x to 1.34x.
- The sub-agent share (0.94x) and the Opus 5.5 share (0.84x) add nothing once the headless
  share is in.
- The time of day does not do it either: a2's ratio is 4.1 to 4.8 in every weekday band
  and at weekends.

Pooled, a3's weekend steps (mostly 19-20 September, sub-agent-heavy) read 5.05, against
a2's 4.81. Its weekday headless-heavy steps read 6 to 8.

This is also the "headless reads about 1.8x" of section 2b, seen from the meter side.

### What it means

- The tracker is not miscounting a3's five-hour points. Each check of the counting path
  holds, and the effect appears on a2 and a4 whenever they run headless work.
- What differs is how Anthropic's five-hour meter treats headless `claude -p` traffic: about
  1.5x interactive work, with the seven-day meter unmoved.
- What causes it is not visible from here. It does not follow token classes, turn rates or
  the time of day. It does follow the client.
- After allowing for it, a3 reads 1.10x [1.03, 1.18] a2. That remainder is not explained.

Any change to how five-hour figures handle headless work is an ADR 0001 rule change, so it
gets its own PR. Nothing published changes here.

## Files

- `tools/account_agreement.py`: `change`, `eras`, `checks`, `mix`, `meters`, `wpw`, `fivehour`.
- `tools/account_transcripts.py`: reads the gs transcripts as the collector does, kept per
  file; delegate run totals.

Both are read-only over `~/.claude*`, `~/var/delegates` and the meter logs. The transcript
read is cached at `~/.cache/account-agreement/turns.pkl`.
