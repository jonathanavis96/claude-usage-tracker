# Why 49 of jwork's stretches read "unaccounted"

Issue #67. Written 2026-09-23 against `history/gs-passive.json` generated at 08:30Z, the gs
config dirs and meter logs read over ssh, and one temporary clone of the gs checkout under
`/tmp` (deleted afterwards). Read-only throughout: no probe, no burst, nothing that spends
allowance on any account. Nothing on gs was changed -- no symlink, no config dir, no
service.

## The question

`capture_status: unaccounted` means a stretch's transcripts are worth less than 85% of the
account's own recent median per meter percent (`tracker/capture.py`, `TOLERANCE`). On
2026-09-23 jwork had 49 such stretches of 140 -- 49 of the 107 the check judged at all --
carrying 531 of jwork's 1,479 measured meter percent. dave had 10 of 52, 103 of 548
percent. The gate is off (`CAPTURE_GATE = False`), so only 7 of jwork's 49 are withheld
from the file, but `tracker/credits.py` gates on `capture_status` rather than `status`, so
all 49 are out of every credits figure the page states. That is the cost: 36% of jwork's
meter movement and 19% of dave's.

The suggestion on the table was to give jwork its own transcript directory on gs, on the
theory that the pooled-directory filter is dropping jwork's own sessions. It is dropping
sessions, and some of them are jwork's. It is not what makes these stretches unaccounted.

## Where jwork's transcripts actually are

`~/.claude-javiswork/projects` is a symlink to `~/.claude/projects`. Four config dirs
resolve to that one directory: `.claude`, `.claude-jono`, `.claude-avis` and
`.claude-javiswork`. Reading each dir's `.claude.json` shows they are not all one account:

| config dir | signed-in account |
|---|---|
| `.claude-javiswork` | the Jono Work account (jwork) |
| `.claude-avis` | a separate work account |
| `.claude-dave` | the Dave account (dave) |
| `.claude-gmail-monitor` | Jonathan's personal account |
| `.claude`, `.claude-jono` | no `oauthAccount` (signed out) |

So the filter is load-bearing. In jwork's meter window (mtime at or after
2026-09-05T06:14:32Z) the pooled directory holds 3,620 transcripts, and `.claude-avis` -- a
different account -- claims 430 of them, 16,656 turns and 3.09 billion tokens. Counting
those as jwork's would not fix an unaccounted stretch; it would manufacture surplus.

A `find` over `/home/jonathan` and `/tmp` for `session-env` and `claude*/projects`
directories returns five and three respectively, and gs has exactly one user account
(`ls /home` gives `jonathan`). There is no fourth transcript root, no second `HOME`, and no
`CLAUDE_CONFIG_DIR` pointing anywhere else. `.claude-gmail-monitor/projects` is a real
directory the tracker never reads, but it is Jonathan's personal account, not jwork's, and
holds 106 files and 2.2M tokens; it is a blind spot for **masterrig**'s meter, not for this
one.

## The transcripts nobody claims

Claude Code writes `<config dir>/session-env/<sessionId>/` for a session, and that is the
only per-login record of which account a pooled transcript belongs to. Of the 3,620
transcripts in jwork's window, 1,782 are claimed by `.claude-javiswork`, 432 by
`.claude-avis`, 1 by `.claude`, and **1,407 by no config dir at all**.

Those 1,407 are almost all one turn each: 1,518 turns and 39.7M tokens between them,
against jwork's 52,099 turns and 8,316M tokens. By project directory they are 695 under
`-home-jonathan-claude-usage-tracker`, 109 under
`-home-jonathan--local-share-airlock-releases-*`, and the rest one apiece under
`/tmp/filing-judge-*`. Reading them shows what they are: the first line of
`297c1f31-....jsonl` is "Below is a long log of routine office notes. Reply with only the
word DONE" -- the retired prose probe. The others are the airlock bench and a one-shot
filing judge.

The common property is that a one-shot `claude -p` that starts no shell writes no
`session-env` entry. `~/review.sh`, which runs
`CLAUDE_CONFIG_DIR=$HOME/.claude-javiswork nohup claude -p` with `Bash(git diff:*)`
allowed, *is* claimed -- all 56 `-home-jonathan-review-trees-*` transcripts resolve to
`.claude-javiswork`. So headless runs are not dropped as a class; toolless one-shots are.

The tracker's own retired probe is in that set, and it ran under `CLAUDE_CONFIG_DIR`
(`tracker/cli_run.py` `run_prompt`) on whichever account was idle. The asymmetry is
visible: `-home-jonathan-claude-usage-tracker` has 695 unclaimed transcripts in the pooled
directory and 597 in dave's own directory. dave's are counted, because dave's root is not
shared and no filter runs there; jwork's are dropped. In dave's own meter window (from
2026-09-15) that asymmetry is worth 62 files and 1.5M tokens of dave's 4,373M -- 0.03%.

Checking `file-history/`, the other per-session directory, adds nothing: across all five
config dirs it holds one session id that `session-env` does not already have.

## What the 49 stretches are

Every unaccounted stretch was scored against its own evidence: the meter percent it
carries, its turns, the share of its tokens spent on Opus or Fable, the unclaimed tokens
whose timestamps fall inside it, and whether `history/harness-runs.jsonl` records the
tracker driving that account at the time. Causes are assigned in that order, so each
stretch is counted once.

**jwork -- 49 stretches, 531 meter percent**

| cause | stretches | meter % |
|---|---|---|
| no gs transcript of any account in the stretch | 6 | 67 |
| the tracker's own harness run overlaps the stretch | 8 | 91 |
| model mix: list dollars mis-value a non-Opus stretch | 27 | 286 |
| inside the natural stretch-to-stretch spread (capture at or above 0.5) | 7 | 77 |
| unexplained | 1 | 10 |
| **dropped pooled transcripts explain it** | **0** | **0** |

**dave -- 10 stretches, 103 meter percent**: 9 model mix (93 percent), 1 inside the spread
(10 percent). dave has a private transcript directory and no filter at all, and still runs
19% unaccounted. Whatever is doing this is not the pooling.

### Model mix is the big one

`bundle_meter_usd` values a stretch at API list price, where Sonnet input is $3 against
Opus's $15 -- a ratio of 0.2. `history/model-rates.json`, fitted from these same stretches,
measures the meter charging Sonnet at 0.5178 credits per input token against Opus's 0.6667
-- a ratio of 0.78. A Sonnet-heavy stretch therefore reads about four times too cheap in
list dollars per meter percent, which is exactly the shape of "unaccounted".

The correlation between a stretch's capture and its Opus-plus-Fable token share is **0.45
over jwork's 101 judged stretches and 0.79 over dave's 30**. dave's accepted stretches have
a median Opus-plus-Fable share of 0.918 and its unaccounted ones 0.247; jwork's are 0.95
and 0.818.

Re-judging every stretch with `tracker/capture.py`'s own `judge` algorithm restated over
measured credits instead of list dollars (cache reads at weight 0, cache writes at the
input rate, output at five times input) moves jwork from 57 accepted / 49 unaccounted / 1
surplus to **64 accepted / 31 unaccounted / 11 surplus**, and the median capture from 0.801
to 0.930. The same replay reproduces the committed verdicts exactly when fed list dollars,
which is what makes the comparison meaningful. It is not a clean win -- ten stretches cross
to surplus -- and it does almost nothing for dave, for the reason in the next section.

### The six stretches with no transcript at all

2026-09-13 21:47Z to 2026-09-14 00:51Z, six consecutive stretches, 67 meter percent, zero
turns. `publishable()` already names this case. The raw ceiling log shows it is real and
ordinary: five-hour 0% to 30% in ninety minutes in steady 2-5 point steps, a reset at
22:50Z, then climbing again, with seven-day going 82% to 91% and tripping the hard ceiling
at 23:27Z.

Two checks say the movement is jwork's and that gs saw none of it. First, the log is
jwork's: the drop-in `gs-usage-ceiling.service.d/seat.conf`, dated 2026-09-05, sets
`CLAUDE_CONFIG_DIR=%h/.claude-javiswork`, and over the 1,718 minutes where the ceiling log
and `claude-usage-meter-jwork.log` overlap the two agree on the five-hour and seven-day
percent in **1,718 of 1,718** cases. Second, gs was awake and busy at the time -- 22 dave
transcripts and one gmail-monitor transcript fall inside that span -- and not one jwork
transcript exists, claimed or unclaimed. Paperclip was not running either; its `run-logs`
directory has not been written to since 2026-08-22.

That is the Jono Work account being used somewhere other than gs. No transcript can ever
exist for it, on any filter.

### The eight on 2026-09-09

All eight overlap a harness run in `history/harness-runs.jsonl` -- the effort matrix, which
`tracker/credits.py` already excludes by design, on the ground that the tracker was driving
the account rather than observing it. They are correctly out of the credits arithmetic and
their capture verdict is not a fault to fix.

### What the dropped transcripts would have been worth

Adding every unclaimed token whose timestamp falls inside an unaccounted stretch -- the
most generous possible reading, since some of those sessions are not jwork's -- touches 14
of the 49 stretches and lifts **none** of them into the accepted band. The largest single
move is 2026-09-10T10:54Z, capture 0.321 to 0.464, from 197 unclaimed files worth 6.0M
tokens against the stretch's own 13.4M. Every other stretch moves by less than 0.06, and
nine of the fourteen by less than 0.02.

## dave on 20-21 September: a model-mix regime, not a step

Agent B's detector sees a single-account step of -35% on dave on 2026-09-20 whose low side
is entirely `capture_status: unaccounted`. It is not off-host use and it is not fixable in
attribution code.

dave's transcripts are dense right through it: the stretches on the low side carry 554, 851
and 437 turns and 66-171M tokens each. Nothing is missing. What changed is what dave was
running. Through 2026-09-19 dave's stretches are Opus- and Fable-heavy and accepted
(2026-09-19T15:09, 92.5% Opus, capture 1.094; 2026-09-19T16:26, 90.8% Opus, capture 1.238).
From 2026-09-20T00:21 the mix inverts to Sonnet and every stretch on that side reads
unaccounted or unpriced:

| stretch | Sonnet share | capture |
|---|---|---|
| 2026-09-20T00:21 | 0.788 | 0.542 |
| 2026-09-20T15:05 | 0.991 | 0.254 |
| 2026-09-20T16:27 | 0.892 | 0.589 |
| 2026-09-20T22:44 | 0.718 | 0.353 |
| 2026-09-21T17:00 | 0.993 | 0.143 |

and the meter returns to normal capture the moment the mix does: 2026-09-21T11:21 is 93.5%
Opus at capture 0.884, 2026-09-21T12:27 is 96.6% Opus at 0.799, 2026-09-21T14:11 is 100%
Opus at 0.857 -- all accepted. The -35% tracks the Sonnet share, not the clock.

The off-host hypotheses do not survive the counts. `.claude` -- the only other config dir
ever logged in as Dave (its `userID` matches `.claude-dave`'s, and
`greenscape-org/scripts/usage-ceiling.py` says so in its header) -- wrote its last
`session-env` entry on 2026-09-18T20:07 and claims one transcript in the whole window.
Every unclaimed pooled transcript dated 20 or 21 September comes to 337 files and 6.75M
tokens, against roughly 2,000M tokens in dave's own stretches over those two days: 0.3% at
the absolute most, and only if all of it were dave's, which it is not.

Credit valuation does not remove it either, which is the useful part. Priced at the
measured rates, dave's Sonnet-heavy stretches (Sonnet share above 0.85, n=5) read 0.10M
credits per meter percent against 0.16M for its Opus- and Fable-heavy ones (n=15) -- a
ratio of **0.63**. So after correcting for mix at the measured Sonnet rate of 0.777 times
Opus, a residual of the same sign remains, and dave's own data imply a Sonnet rate nearer
0.49 times Opus. The honest reading is that the "step" is the remaining error in the Sonnet
rate, exposed by a change in what dave was running. It is a single-account step that
coincides exactly with a mix change, so it must not reach the published series; PR #72's
two-account rule already refuses it, and it should keep refusing it.

## What changed in the code

`transcript_files()` in `tracker/gs_passive.py`, in two ways, neither of which changes
which transcripts are kept:

1. The filter now fires when the transcripts root is **shared** -- another config dir under
   `home` resolves to the same directory -- as well as when the root itself is a symlink.
   The old condition tested the shape of the link, which happens to hold on gs today. It
   would not hold if `~/.claude-javiswork` were the link instead of
   `~/.claude-javiswork/projects`, or if another login's `projects/` were the link into a
   directory this account owns; in both cases the filter silently did not run and another
   account's tokens were counted as this one's. A test covers the second case.
2. The `own_sessions` meta splits `dropped` into `dropped_to`, naming the config dir that
   claims each dropped transcript, and `unclaimed`, counting the ones no config dir claims.
   One number could not tell those apart and they mean opposite things: `dropped_to` is the
   filter doing its job, `unclaimed` is the blind spot this document measures.

Running the join on gs before and after the change, from a temporary clone, gives identical
verdicts -- jwork `{accepted: 100, unpriced: 33, unaccounted: 7}`, daily median $1.4658 per
1%, cv 0.2683 (n=15); dave `{unpriced: 22, accepted: 30}`, $1.2336, cv 0.3025 (n=4) -- and
the new meta:

```
jwork own_sessions {'kept': 1784, 'dropped': 1840, 'subagent_files': 700,
                    'dropped_to': {'.claude': 1, '.claude-avis': 432}, 'unclaimed': 1407}
```

Nothing moves from unaccounted to accepted, which is the honest answer: no attribution
change can, because attribution is not what is wrong with these stretches.

`transcript_session_id()` was checked and left alone. Every transcript on gs is at one of
two depths -- `<project>/<session>.jsonl` or `<project>/<session>/subagents/agent-*.jsonl`,
4,505 and 2,885 of them in the pooled directory -- so the parent-session rule is right for
all of them, and there are no deeper nestings for it to miss.

## Would a private transcript directory for jwork help?

**No stretch in the current data would be recovered, and no future stretch would move
much.** A private directory removes the ambiguity -- anything written into it is jwork's --
so the toolless one-shot runs would start being counted instead of dropped. That is worth
0.48% of jwork's tokens (39.7M against 8,316M), and adding all of it lifts nothing out of
the unaccounted band, as measured above. It would do nothing at all for the existing 49,
since it cannot be applied retroactively.

What it would cost is cross-account session resume, which is why
`~/.claude-javiswork/projects` was pooled on 2026-08-23: a session started under one login
can be resumed under another while they share a directory. Splitting it also splits the
resume history at the cut.

What it would genuinely buy is insurance rather than measurement. 3.09 billion tokens of
the avis account sit in the same directory as jwork's, separated only by a `session-env`
heuristic that this document shows is already incomplete in one direction. If it ever fails
in the other direction the join would read another account's work as jwork's and say
nothing.

That is Jonathan's call, not this branch's, and nothing on gs was touched. On the evidence
here it should not be made for the sake of issue #67, because it does not close it.

## What would close it

In rough order of the meter percent it would recover for jwork:

- **Value stretches in measured credits, not list dollars** (286 of 531 percent), and then
  keep fitting the Sonnet rate -- dave's 20 September regime says the current one is still
  about 1.6 times too high.
- **Widen or replace the 15% tolerance** (77 percent). The module docstring already says
  the band is narrower than the quantity's own 15-20% stretch-to-stretch spread, so a share
  of "unaccounted" is the check calling its own noise a fault.
- **Leave the harness-run stretches alone** (91 percent). They are correctly excluded.
- **Accept that 67 percent is off-gs use** and say so on the page rather than trying to
  recover it.

## Commands the figures come from

- `python3 -m tracker.gs_passive --out ...` on a temporary clone of the gs checkout under
  `/tmp`, before and after the change, deleted afterwards.
- A manifest script run on gs over `~/.claude/projects`, `~/.claude-dave/projects` and
  `~/.claude-gmail-monitor/projects` and the five `session-env` directories, emitting per
  file its session id, turn count, first and last assistant timestamp, per-class tokens and
  model counts; every file, claim and token figure above is a count over that manifest.
- `tracker/capture.py`'s `judge` replayed over `history/gs-passive.json` under list dollars
  and under `history/model-rates.json`'s measured rates.
- `grep` over `~/.paperclip/ops/gs-usage-ceiling.log` and `claude-usage-meter-jwork.log`,
  and `systemctl --user cat gs-usage-ceiling.service`.

## Re-run at the refitted rates, 2026-09-23

PR #79 refitted the meter's per-model rates: Sonnet from 0.5178 to 0.3967 credits per
input token (0.777x Opus to 0.595x), Fable from 1.3068 to 1.4014, Opus unchanged at 0.6667
as the anchor. This section re-runs the method above at the new rates. The sections above
are left as they were written.

### What in the method reads the rates

Two scripts produced the figures above, and only one of them depends on the rates.

- The cause ladder (off gs, dropped transcripts, harness run, model mix, spread,
  unexplained, in that order) runs over the stretches `history/gs-passive.json` marks
  `unaccounted`. That verdict is `tracker/capture.py`'s, in list dollars, and the ladder's
  model-mix rung is an Opus-plus-Fable token share below 0.85. Nothing in it reads
  `history/model-rates.json`. Re-run unchanged on the same file it gives the same 27 / 8 /
  6 / 7 / 1, whatever the rates are.
- The credit re-judge replays `capture.py`'s `judge` over each stretch valued at the
  measured rates (cache reads at weight 0, cache writes at the input rate, output at five
  times input). That is the step the refit moves, and it is what "the new rates explain a
  stretch" has to mean: valued in credits, the stretch's meter movement falls inside the
  account's normal spread, so `judge` accepts it.

So the re-run feeds each stretch's credit capture back through the same ladder. A stretch
the credit judge accepts is explained by valuation; one it still calls unaccounted gets the
ladder's cause, with the credit capture standing in for the list-dollar one at the
dropped-transcripts and spread rungs. Both runs use the 08:30Z `history/gs-passive.json`
the original classification used (commit `a366108`), so the stretch set is the same 49.
The old rates are `history/model-rates.json` as of `c5118ab^`, the new ones as of
`c5118ab` (PR #79). The list-dollar replay reproduces the committed verdict on all 107
judged jwork stretches and all 30 judged dave ones.

### jwork: 12 of the 27 model-mix stretches are explained at the new rates

| cause | original (list $) | credits, old rates | credits, new rates |
|---|---|---|---|
| accepted under credits (explained by valuation) | -- | 18 (195%) | **14 (152%)** |
| no gs transcript (off gs) | 6 (67%) | 6 (67%) | 6 (67%) |
| the tracker's own harness run | 8 (91%) | 8 (91%) | 8 (91%) |
| model mix | 27 (286%) | 11 (113%) | **15 (156%)** |
| inside the spread (capture at or above 0.5) | 7 (77%) | 5 (55%) | 5 (55%) |
| unexplained | 1 (10%) | 1 (10%) | 1 (10%) |
| dropped pooled transcripts | 0 | 0 | 0 |
| **still unaccounted** | **49 (531%)** | **31 (336%)** | **35 (379%)** |

Of the 27 model-mix stretches, **12 (130 meter percent) are explained at the new rates**
and 15 (156 percent) stay unaccounted. The 15 do not become anything else: every rung above
model mix still fails for them (they have transcripts, no harness run overlaps them, and no
dropped transcripts lift them), and each is under 0.85 Opus-plus-Fable, so the ladder still
names model mix. What that now means is "non-Opus work that measured credits at the
current rates still value too low", not list-dollar mis-valuation. Two of the seven spread
stretches are also accepted under credits, at both rate sets.

The refit explains **fewer** of them than the old rates did, not more: 16 of the 27 were
accepted at the old rates, and four -- 2026-09-12T15:16Z, 2026-09-16T14:50Z,
2026-09-17T13:05Z and 2026-09-17T14:05Z -- fall back to unaccounted at the new ones. No
stretch moves the other way. That is the direction a lower Sonnet rate has to push: it
values Sonnet-heavy stretches at fewer credits, so their capture drops. What the refit does
fix is the other tail. Across all of jwork's stretches the credit judge goes from 64
accepted / 31 unaccounted / 11 surplus at the old rates to **69 accepted / 35 unaccounted /
2 surplus** at the new ones, and the median capture from 0.930 to 0.892. Nine of the eleven
surplus stretches (ten of them accepted in list dollars) are accepted at the new rates. No
stretch accepted in list dollars turns unaccounted under credits at either rate set.

Per stretch (the figure in brackets is the stretch's capture under that valuation;
"changed by refit" marks a class that differs between the old and new rates):

| stretch start | meter % | Opus+Fable share | cause (list $) | credits, old rates | credits, new rates | changed by refit |
|---|---|---|---|---|---|---|
| 2026-09-05T12:14Z | 11 | 0.90 | spread | **accepted** (0.86) | **accepted** (0.83) |  |
| 2026-09-05T13:20Z | 11 | 0.72 | model mix | **accepted** (0.94) | **accepted** (0.86) |  |
| 2026-09-05T13:41Z | 12 | 0.84 | model mix | **accepted** (0.84) | **accepted** (0.81) |  |
| 2026-09-05T17:09Z | 12 | 0.69 | model mix | **accepted** (0.82) | **accepted** (0.78) |  |
| 2026-09-05T17:47Z | 12 | 0.72 | model mix | **accepted** (0.89) | **accepted** (0.88) |  |
| 2026-09-05T18:30Z | 11 | 0.51 | model mix | **accepted** (0.88) | **accepted** (0.86) |  |
| 2026-09-05T21:14Z | 10 | 0.26 | model mix | **accepted** (0.86) | **accepted** (0.79) |  |
| 2026-09-05T23:58Z | 13 | 0.98 | spread | spread (0.75) | spread (0.75) |  |
| 2026-09-08T13:12Z | 10 | 0.86 | spread | spread (0.63) | spread (0.62) |  |
| 2026-09-09T10:47Z | 10 | 1.00 | harness run | harness run (0.61) | harness run (0.61) |  |
| 2026-09-09T11:58Z | 10 | 1.00 | harness run | harness run (0.67) | harness run (0.67) |  |
| 2026-09-09T12:20Z | 11 | 1.00 | harness run | harness run (0.49) | harness run (0.49) |  |
| 2026-09-09T12:52Z | 13 | 1.00 | harness run | harness run (0.47) | harness run (0.47) |  |
| 2026-09-09T13:30Z | 11 | 1.00 | harness run | harness run (0.45) | harness run (0.45) |  |
| 2026-09-09T13:47Z | 14 | 1.00 | harness run | harness run (0.31) | harness run (0.31) |  |
| 2026-09-09T14:09Z | 12 | 1.00 | harness run | harness run (0.29) | harness run (0.28) |  |
| 2026-09-09T14:36Z | 10 | 1.00 | harness run | harness run (0.32) | harness run (0.32) |  |
| 2026-09-10T09:43Z | 12 | 0.96 | spread | spread (0.62) | spread (0.65) |  |
| 2026-09-10T10:21Z | 10 | 0.80 | model mix | model mix (0.37) | model mix (0.35) |  |
| 2026-09-10T10:54Z | 10 | 0.94 | unexplained | unexplained (0.41) | unexplained (0.42) |  |
| 2026-09-10T11:21Z | 10 | 0.44 | model mix | model mix (0.71) | model mix (0.73) |  |
| 2026-09-10T12:16Z | 10 | 0.58 | model mix | model mix (0.23) | model mix (0.23) |  |
| 2026-09-10T12:44Z | 10 | 0.59 | model mix | model mix (0.50) | model mix (0.49) |  |
| 2026-09-10T17:54Z | 11 | 0.84 | model mix | **accepted** (0.77) | **accepted** (0.77) |  |
| 2026-09-11T09:47Z | 11 | 0.84 | model mix | **accepted** (0.88) | **accepted** (0.90) |  |
| 2026-09-11T10:35Z | 10 | 0.83 | model mix | model mix (0.41) | model mix (0.40) |  |
| 2026-09-11T11:12Z | 10 | 0.82 | model mix | model mix (0.57) | model mix (0.56) |  |
| 2026-09-11T12:32Z | 10 | 0.85 | spread | spread (0.64) | spread (0.65) |  |
| 2026-09-11T13:42Z | 10 | 0.82 | model mix | model mix (0.43) | model mix (0.42) |  |
| 2026-09-12T15:16Z | 10 | 0.84 | model mix | **accepted** (0.77) | model mix (0.75) | yes |
| 2026-09-12T21:53Z | 10 | 0.98 | spread | spread (0.76) | spread (0.76) |  |
| 2026-09-13T21:52Z | 10 | - | off gs | off gs (0.00) | off gs (0.00) |  |
| 2026-09-13T22:18Z | 10 | - | off gs | off gs (0.00) | off gs (0.00) |  |
| 2026-09-13T22:50Z | 13 | - | off gs | off gs (0.00) | off gs (0.00) |  |
| 2026-09-13T23:22Z | 11 | - | off gs | off gs (0.00) | off gs (0.00) |  |
| 2026-09-13T23:48Z | 12 | - | off gs | off gs (0.00) | off gs (0.00) |  |
| 2026-09-14T00:20Z | 11 | - | off gs | off gs (0.00) | off gs (0.00) |  |
| 2026-09-15T16:11Z | 10 | 0.41 | model mix | **accepted** (0.83) | **accepted** (0.75) |  |
| 2026-09-16T14:50Z | 10 | 0.70 | model mix | **accepted** (0.81) | model mix (0.73) | yes |
| 2026-09-16T15:23Z | 11 | 0.87 | spread | **accepted** (1.03) | **accepted** (0.90) |  |
| 2026-09-17T10:09Z | 12 | 0.30 | model mix | model mix (0.47) | model mix (0.38) |  |
| 2026-09-17T13:05Z | 12 | 0.48 | model mix | **accepted** (0.89) | model mix (0.72) | yes |
| 2026-09-17T14:05Z | 11 | 0.23 | model mix | **accepted** (0.89) | model mix (0.64) | yes |
| 2026-09-17T15:06Z | 11 | 0.27 | model mix | model mix (0.24) | model mix (0.17) |  |
| 2026-09-17T15:17Z | 10 | 0.31 | model mix | **accepted** (1.11) | **accepted** (0.82) |  |
| 2026-09-17T15:50Z | 10 | 0.13 | model mix | model mix (0.22) | model mix (0.16) |  |
| 2026-09-17T16:01Z | 10 | 0.22 | model mix | model mix (0.50) | model mix (0.40) |  |
| 2026-09-17T16:34Z | 10 | 0.43 | model mix | **accepted** (0.83) | **accepted** (0.69) |  |
| 2026-09-17T22:48Z | 10 | 0.76 | model mix | **accepted** (1.06) | **accepted** (0.90) |  |

### dave: 2 of 9

dave's 10 were classified the first time (9 model mix, 1 spread), and the same method
applies unchanged, so it was re-run the same way. At the new rates **2 of the 9 model-mix
stretches (20 meter percent) are explained** and 7 (73 percent) stay model mix; the old
rates explained 3. 2026-09-20T22:16Z is the one the refit loses. The spread stretch stays
where it was. Across all of dave's stretches the credit judge gives 18 accepted / 10
unaccounted / 1 surplus at the new rates against 19 / 9 / 1 at the old, with 23 not judged
either way (they carry unpriced tokens, or tokens of a family with no measured rate). Two
stretches accepted in list dollars, 2026-09-21T12:27Z and 2026-09-21T14:11Z, read
unaccounted under credits at both rate sets (0.73 and 0.75 at the new rates); both are over
0.9 Opus, so the ladder puts them in the spread.

| stretch start | meter % | Opus+Fable share | cause (list $) | credits, old rates | credits, new rates | changed by refit |
|---|---|---|---|---|---|---|
| 2026-09-20T00:21Z | 11 | 0.21 | model mix | model mix (0.62) | model mix (0.59) |  |
| 2026-09-20T01:47Z | 10 | 0.14 | model mix | model mix (0.61) | model mix (0.55) |  |
| 2026-09-20T02:31Z | 10 | 0.37 | model mix | **accepted** (0.83) | **accepted** (0.79) |  |
| 2026-09-20T15:05Z | 11 | 0.01 | model mix | model mix (0.48) | model mix (0.38) |  |
| 2026-09-20T16:27Z | 10 | 0.11 | model mix | **accepted** (0.94) | **accepted** (0.81) |  |
| 2026-09-20T22:16Z | 10 | 0.34 | model mix | **accepted** (0.84) | model mix (0.76) | yes |
| 2026-09-20T22:44Z | 10 | 0.28 | model mix | model mix (0.53) | model mix (0.46) |  |
| 2026-09-21T12:05Z | 10 | 0.92 | spread | spread (0.72) | spread (0.73) |  |
| 2026-09-21T15:05Z | 11 | 0.55 | model mix | model mix (0.68) | model mix (0.62) |  |
| 2026-09-21T17:00Z | 10 | 0.01 | model mix | model mix (0.18) | model mix (0.15) |  |

### What this changes in the conclusions above

- The model-mix bucket shrinks under measured credits at either rate set, but the refit
  shrinks it less, not more: 15 jwork stretches and 156 meter percent remain, against 11
  and 113 at the old rates. "Value stretches in measured credits" still recovers more
  meter percent than anything else in "What would close it", but 130 percent of the 286,
  not all of it.
- The residual pulls against the refit. The refit wants Sonnet cheaper and it clears the
  surplus tail; the 15 remaining stretches, all below 0.85 Opus-plus-Fable, would need
  non-Opus work valued dearer to be accepted. One input rate per family cannot satisfy
  both sets, so the next thing to test is how the valuation weights cache writes, cache
  reads and output on those stretches, rather than a further move in the Sonnet input
  rate.
- The earlier remark that dave's data imply a Sonnet rate near 0.49x Opus pointed the same
  way as the refit, which moved to 0.595x. dave's own model-mix residual is still 7 of 9
  stretches.
- The off-gs, harness-run, spread and unexplained buckets do not depend on the rates and are
  unchanged.

### The committed file has moved since 08:30Z

`history/gs-passive.json` is rewritten hourly. At 11:30Z (commit `b0a8874`) the price table
covers the 33 jwork and 22 dave stretches that were unpriced at 08:30Z, so the list-dollar verdicts
differ: jwork 82 accepted / 50 unaccounted / 8 surplus, dave 32 / 16 / 4. Run on that file,
the ladder gives jwork 23 model mix (242%), 8 harness run (91%), 6 off gs (67%), 10 spread
(108%) and 3 unexplained (31%); dave 15 model mix (160%) and 1 spread (10%). At the new
rates 7 of jwork's 23 model-mix stretches are accepted under credits, 15 stay model mix and
1 is not judged; for dave 1 of 15 is accepted, 7 stay and 7 are not judged. The per-stretch
answer to the refit is the same on both files, because each stretch's tokens and meter
movement are the same; only the list-dollar starting set differs.

### Commands

The two scripts behind the original section were not committed; they were recovered from
the session that wrote it. `rerun.py` below is both of them joined, with the input paths as
arguments and nothing else changed. The manifest is the per-transcript manifest of the gs
config dirs described in "Commands the figures come from"; it only feeds the
dropped-transcripts rung, which fires for no stretch at either rate set. Run from the
repository root, since the script imports `tracker.capture` and `tracker.credits` and reads
`history/harness-runs.jsonl`:

```
git show a366108:history/gs-passive.json > gsp-0830.json
git show c5118ab^:history/model-rates.json > rates-old.json
git show c5118ab:history/model-rates.json > rates-new.json
python3 rerun.py gsp-0830.json rates-old.json manifest.json out-0830-old.json
python3 rerun.py gsp-0830.json rates-new.json manifest.json out-0830-new.json
python3 rerun.py history/gs-passive.json rates-old.json manifest.json out-head-old.json
python3 rerun.py history/gs-passive.json rates-new.json manifest.json out-head-new.json
```

<details>
<summary><code>rerun.py</code></summary>

```python
"""PR #76's classify.py and revalue.py, unchanged in method, with the input paths as arguments.

usage: rerun.py <gs-passive.json> <model-rates.json> <manifest.json> <out.json>
Writes, per account, every stretch's committed verdict, its replayed list-dollar and
measured-credit verdicts, and the cause classify.py assigns to each unaccounted one.
"""
import json, sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median

REPO = Path.cwd()
sys.path.insert(0, str(REPO))
from tracker.capture import TOLERANCE, BOOTSTRAP, MIN_REFERENCE, LOOKBACK  # noqa: E402
from tracker.credits import family, load_credits, harness_runs  # noqa: E402

gsp, rates_path, man_path, out_path = map(Path, sys.argv[1:5])
rpt = json.loads(gsp.read_text())
mr = json.loads(rates_path.read_text())["measured_rates"]
man = json.loads(man_path.read_text())
credits = load_credits()
RATE = {f: v["input"] for f, v in mr["per_family"].items() if v.get("input")}
OUT_MULT = mr.get("output_multiplier", 5)
runs = harness_runs(REPO / "history/harness-runs.jsonl")
env = man["env"]


def T(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def owner(f):
    o = [c for c, e in env.items() if f["sid"] in e]
    return o[0] if o else None


pooled = [f for f in man["files"] if f["tag"] == "pooled" and f["turns"] and f["last"]]
unowned = [f for f in pooled if owner(f) is None]


def tok_in(files, a, b):
    return sum(sum(f["tokens"].values()) for f in files if a <= T(f["last"]) <= b)


def opus_share(s):
    tot = Counter()
    for m, t in s["tokens"].items():
        tot[m] += sum(v for c, v in t.items() if c != "cache_write_1h")
    g = sum(tot.values())
    return None if not g else sum(v for m, v in tot.items() if "opus" in m or "fable" in m) / g


# ---- revalue.py
def value(st, mode):
    if mode == "list":
        return st["usd"]
    total = 0.0
    for m, tok in st["tokens"].items():
        r = RATE.get(family(m, credits))
        if r is None:
            return None
        inp = tok.get("input", 0) + tok.get("cache_write", 0)
        total += inp * r + tok.get("output", 0) * r * OUT_MULT
    return total


def judge(rows, mode):
    pool, k = [], 0
    priced = [r for r in rows if r["unpriced_tokens"] == 0 and value(r, mode) is not None]
    verdicts = {}
    while k < len(priced):
        if len(pool) < MIN_REFERENCE:
            batch = priced[k:k + BOOTSTRAP]
            if len(batch) < BOOTSTRAP:
                break
            ref = median(value(r, mode) / r["delta_pct"] for r in batch)
            k += BOOTSTRAP
        else:
            batch = [priced[k]]
            ref = median(pool[-LOOKBACK:])
            k += 1
        for r in batch:
            v = value(r, mode)
            per = v / r["delta_pct"]
            lo = v / (r["delta_pct"] + r["windows"])
            hi = v / (r["delta_pct"] - r["windows"]) if r["delta_pct"] > r["windows"] else float("inf")
            if hi < ref * (1 - TOLERANCE):
                s = "unaccounted"
            elif lo > ref * (1 + TOLERANCE):
                s = "surplus"
            else:
                s = "accepted"
                pool.append(per)
            verdicts[r["start"]] = (s, per / ref)
    return verdicts


# ---- classify.py's cause ladder, over a given capture figure
def cause(name, s, cap):
    a, b = T(s["start"]), T(s["end"])
    own = sum(sum(v for c, v in t.items() if c != "cache_write_1h") for t in s["tokens"].values())
    drop = tok_in(unowned, a, b) if name == "jwork" else 0
    newcap = cap * (1 + drop / own) if own else None
    h = any(r.account == name and r.start < b and r.end > a for r in runs)
    os_ = opus_share(s)
    if s["turns"] == 0:
        return "off-gs"
    if newcap is not None and newcap >= 0.85:
        return "dropped"
    if h:
        return "harness"
    if os_ is not None and os_ < 0.85:
        return "model-mix"
    if cap >= 0.5:
        return "spread"
    return "unexplained"


out = {}
for name in ("jwork", "dave"):
    rows = sorted(rpt["accounts"][name]["stretches"], key=lambda s: s["end"])
    lst, mea = judge(rows, "list"), judge(rows, "measured")
    recs = []
    for s in rows:
        rec = {"start": s["start"], "end": s["end"], "delta_pct": s["delta_pct"], "turns": s["turns"],
               "opus_share": opus_share(s), "committed": s["capture_status"], "capture": s.get("capture"),
               "list": lst.get(s["start"]), "measured": mea.get(s["start"])}
        if s["capture_status"] == "unaccounted":
            rec["cause_committed"] = cause(name, s, s["capture"])
        if rec["measured"] and rec["measured"][0] == "unaccounted":
            rec["cause_measured"] = cause(name, s, rec["measured"][1])
        recs.append(rec)
    out[name] = recs
    agree = sum(1 for r in recs if r["list"] and r["list"][0] == r["committed"])
    print(f"== {name}: {len(recs)} stretches; list replay agrees with committed on {agree} "
          f"of {sum(1 for r in recs if r['list'])} judged")
    for key, label in (("committed", "committed verdict"), ("measured", "measured-credit verdict")):
        c = Counter((r[key] if key == "committed" else (r[key][0] if r[key] else "unjudged")) for r in recs)
        print(f"   {label}: {dict(c)}")
    for key in ("cause_committed", "cause_measured"):
        c, p = Counter(), Counter()
        for r in recs:
            if key in r:
                c[r[key]] += 1
                p[r[key]] += r["delta_pct"]
        print(f"   {key}: " + ", ".join(f"{k} {c[k]} ({p[k]:.0f}%)" for k in sorted(c)))
Path(out_path).write_text(json.dumps(out, indent=1))
```

</details>
