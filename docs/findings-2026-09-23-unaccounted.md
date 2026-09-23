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

| config dir | `oauthAccount.emailAddress` |
|---|---|
| `.claude-javiswork` | jono@greenscape.systems |
| `.claude-avis` | avis@greenscape.systems |
| `.claude-dave` | d.jenkinson@greenscape.systems |
| `.claude-gmail-monitor` | jonathanavis96@gmail.com |
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
