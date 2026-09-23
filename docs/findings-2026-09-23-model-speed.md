# Model speed from the transcripts, and why one machine's Opus 5 ran at 170 tokens a second

> **Correction, 2026-09-23.** This document explains the fast sessions below as Claude
> Code's Opus fast mode. That explanation is withdrawn. Jonathan has never used fast mode.
> masterrig's Claude Code settings carry no fast-mode setting, and the account is not
> eligible for usage credits (`overageCreditGrantCache` reads `eligible: false, available:
> false`), which fast mode needs ("Until they're on, /fast reports 'Fast mode requires usage
> credits'", code.claude.com/docs/en/fast-mode). And PR #88's own before and after table
> (docs/findings-2026-09-23-fast-mode-stretches.md) shows the meter counted those sessions'
> tokens, at least in part: taking them out turned some surplus stretches accepted, but
> turned others unaccounted (19 Sep 22:20-23:36 capture 1.99 to 0.68; 21 Sep 10:54-13:26
> 1.62 to 0.43; 21 Sep 13:26-19:26 1.63 to 0.25; 20 Sep 00:16-01:07 1.12 to 0.59).
>
> What was measured stands: some Opus sessions run about 2.5 times faster than others on the
> same model, account and settings, with nothing between, mostly on a1 and a little on a2.
> What causes it is not known. The code now calls them fast sessions: a session whose
> running median speed is at least FAST_FACTOR (1.6) times the model's median. They are
> real measured speeds, so their requests are now included in the daily speed figures at
> every level (daily, by account, by entrypoint), and a day when answers came much faster
> shows as a jump in the ordinary line. Each published day counts them in
> `fast_session_requests`; there is no longer a separate series. The cause is still
> unknown. The rest of this document is kept as written, so its figures below are the ones
> with fast sessions left out.

Issue #105. Written 2026-09-23. Sources: a text-free extract of every masterrig transcript
line from 20 August to 23 September (305,185 lines: timestamps, message ids, model, usage,
block types and sizes, no message text), and gs's own transcripts for Max accounts a2, a3
and a4, read in place. Accounts are named by their published labels only: a1 is the
account used on masterrig, a2 to a4 are the accounts used on gs.

## The question

A prototype timed each API response from the last `user` line before its first content
block to its last content block, and divided output tokens by that time. For Opus 5 on a1's
interactive sessions (`entrypoint: cli`) it read about 65 tokens a second from 20 August to
3 September, about 170 on 15 to 17 and 19 to 21 September, about 70 on 18 September, and
210 falling to 91 across the morning of 22 September. gs's accounts read 80 to 100 in the
same hours, and a1's Agent SDK seats (`sdk-ts`) 71 to 76 throughout. Every one of a1's
163,556 lines with a speed field says `"speed": "standard"`. Before any speed is published,
that split had to be explained.

## Answer

Two things, one an artefact and one real.

1. **An artefact, now corrected: duplicate block lines in Claude Code 2.1.278 and 2.1.280.**
   On those versions some responses have their blocks written a second time after the last
   one, with `output_tokens: 0` and the *first* block's timestamp (1,069 of 2,963 a1 cli
   Opus 5 responses on 2.1.278, 65 of 1,173 on 2.1.280, none on any earlier version). The
   prototype took the last line in the file as the end of the response, so for those
   responses the end moved back to the first block and the speed went up. Taking the
   latest timestamp instead (tracker/speed.py `requests_in`) brings a1's 22 September down
   from 112 to 75 for the day, and the morning from "210 falling to 91" to 153 at 10:00, 76
   at 11:00 and 70 at 12:00. It also narrows the gap between gs and a1's standard
   sessions: with the correction gs's Opus 5 reads 65 to 105 a day on a2 and 71 to 83 on
   a3, against 54 to 85 on a1.

2. **Real: Opus fast mode, which the transcripts record as standard.** After the
   correction a1's fast days still read 156 to 175 (15 Sep 175, 16 Sep 165, 17 Sep 169,
   19 Sep 156, 20 Sep 162, 21 Sep 164; slow days 54 to 85, 62 to 69 on all but three). Everything about them matches
   Claude Code's fast mode, whose documentation (code.claude.com/docs/en/fast-mode, read
   2026-09-23) says:

   - "Fast mode is a high-speed configuration for Claude Opus, making the model up to 2.5x
     faster" -- the fast sessions run 2.5 to 2.9 times the standard ones (below);
   - "fast mode is available via usage credits only and not included in the subscription
     rate limits" -- which is why masterrig's capture check read 19 Sep 13:21 to 20 Sep
     01:36 as *surplus*: those tokens are in the transcripts but never moved the meter;
   - "When you hit the fast mode rate limit: Fast mode automatically falls back to standard
     speed" -- which is what the one session on 22 September does at about 11:03, going from
     around 150 to around 75 and staying there;
   - in `-p` mode fast mode works "only in a session launched with fast mode in its
     `--settings` value" -- which fits 18 September, when a1's `claude -p` sessions from one
     orchestrator (sdk-cli) ran at 161 to 177 while its interactive sessions in the same
     hours ran at 60 to 79.

   Nothing in a line says fast mode was on. `usage.speed` reads `standard`, `service_tier`
   reads `standard`, and `inference_geo` reads `not_available` on both kinds.

## How the candidates were tested

Opus 5, a1, `entrypoint: cli`, responses of 300 or more output tokens taking 1 to 900
seconds. "Fast days" are 15-17 and 19-21 September; "slow days" 20 August to 3 September.

**1. A measurement artefact (version, block timing, the output count).**

- Version does not split it. 2.1.272 to 2.1.274 and 2.1.278 are fast in a1's cli sessions,
  2.1.275 and 2.1.276 slow, but on 18 September 2.1.275 and 2.1.276 are fast in a1's sdk-cli
  sessions, and 2.1.278 is slow on 22 September after 11:03 and on gs.
- A clock that does not depend on the trigger at all agrees with the trigger. Inside one
  response, the time between the second-last block and a final `tool_use` block of at least
  600 characters, divided into that block's characters, is 674 characters a second on fast
  days against 233 on slow days (852 and 1,742 responses), 2.9 times; the trigger-based
  speed is 166 against 66, 2.5 times. Per version, the same in-response rate is 215 to 270
  on every slow version and 652 to 686 on 2.1.272, 2.1.274 and 2.1.278.
- The output count is final and the same kind of number on both. Where a response's lines
  disagree on `output_tokens` (12,609 responses across every model, the zero lines above
  left out), the count only rises through the response and the last block carries the
  largest, so the largest is the final count. Tool-only responses of 300 to 800 tokens
  carry 2.37 characters per output token on fast days and 2.35 on slow ones, so the tokens
  are not being counted differently.

**2. A wrong trigger (parallel tool calls, sidechains, a late `user` line).**

- Parallel tool results (more than one `user` line between two responses) are 4.7% of fast
  requests and 6.6% of slow ones.
- Sidechain responses are 444 of 2,551 fast and 58 of 5,047 slow, but the trigger is now
  kept per `isSidechain` value, so a sidechain line never starts a main-chain request, and
  the speed is bimodal by session in main-chain sessions alone.
- A late `user` line would shorten only the time to first block; the in-response clock
  above does not use the trigger and shows the same ratio.

**3. Response shape.**

- Median output 693 tokens on fast days, 616 on slow. In the same 500 to 1,000 token band,
  fast days read 165 and slow days 66 (992 and 1,999 requests).
- Thinking in 66% of fast responses and 68% of slow; 1.06 tool calls per tool-only response
  against 1.27; effort `low` on nearly all of both.

**4. Account or serving.** It is the same account on fast and slow days (a1's meter moved
normally on 18 September), and on 18 September both speeds ran in the same hours from the
same machine. Speed is fixed per session and bimodal: of the 95 transcripts from 12
September with at least 8 timed Opus 5 requests, 63 have a median of 60 to 88 and 32 of 143
to 207, and none falls between. The 22 September session, which switches part way, is in
the lower group on its median. So it is a per-session setting, and fast mode is the
documented one that matches.

## What is published

`claude-usage.json` gets a top-level `speed` block (tracker/speed.py `speed_block`). Per
model id, per UTC day: median output tokens a second with its interquartile range, the
number of requests, the number left out as fast mode, and the median time to first block
with its interquartile range. The same per account label (`by_account`), per entrypoint
(`by_entrypoint`) and per both (`by_account_entrypoint`, like with like). A day, or a day
of a split, with fewer than 30 requests of a model is left out. `method` and `caveats` are
plain English.

**Fast mode is left out by its timing.** On an Opus model, a request is counted as fast mode
when the median speed of the nine requests around it in its session is at least 1.6 times
the model's median over the scan. With 1.6, a1's Opus 5 cli days of 15 to 17 and 20 to 21
September drop out whole (370, 386, 237, 261 and 935 requests left out, under 30 standard
ones left), as does a1's sdk-cli on 18 September (215); 19 September keeps 55 standard
requests at 84 and leaves out 306, 22 September keeps 139 at 73 and leaves out 36, and a1's
sdk-cli on 10 September leaves out 49. On gs it leaves out 25 of a2's 998 cli requests on 16
September and nothing else. No non-Opus model is ever flagged.

**Input speed is published as time to first block, not as a rate.** A robust fit (least
absolute deviations) of each request's duration against its uncached input
(`input_tokens + cache_creation_input_tokens`) and output tokens, per model and day, over
both machines' requests, with a 20-draw bootstrap: of 87 model-days with at least 300
requests, the input rate's 80% interval was under 1.5 times end to end on 6, unbounded above
on 13, and 2.7 times at the median. The six tight days disagree several-fold with each other
(Sonnet 5: about 13,600 on 6 September, about 44,000 on 17 September). Claude Code's uncached
input per request is small -- a median of about 1,200 tokens on a1's Agent SDK seats -- so
the time to read it is lost in the queueing and output time. Time to first block is from
the trigger to the end of the response's first block, so it includes writing that block.

**History.** Claude Code deletes transcripts after 30 days. Each machine appends daily rows
to a committed file and recomputes only the last two days (tracker/speed.py
`RECOMPUTE_DAYS`): masterrig in history/masterrig-speed.json from bin/passive.sh, gs in
history/gs-passive.json's `speed` from the hourly tracker.gs_passive run. Each row keeps its
speeds as a histogram in bins 2% apart so the publisher can pool the two machines and any
split and still take a real median. history/masterrig-speed.json was seeded from the
extract: 224 rows, 20 August to 23 September (the extract has no lines before 20 August).
gs's rows are first written by the first hourly run after this change, from whatever gs
transcripts exist then (the oldest now give rows from 23 August).

## What the corrected series says (to 23 September)

Median output tokens a second per day, pooled over every account, fast mode out:

| model | range over the days published | last days |
|---|---|---|
| Opus 5 | 60 to 85 | 78, 79 (22, 23 Sep) |
| Opus 5.5 | 95 to 98 | 98, 95 (22, 23 Sep) |
| Sonnet 5 | 75 to 99 | 94, 99 (22, 23 Sep) |
| Fable 5.1 | 63 to 79 | 68, 70 (22, 23 Sep) |
| Opus 4.7 | 61 to 68 | 65, 66 (22, 23 Sep) |

By account, once fast mode is out, the daily medians overlap: Opus 5 reads 54 to 85 on a1,
65 to 105 on a2 and 71 to 83 on a3; Fable 5.1 62 to 75 on a1, 56 to 80 on a2 and 66 to 74 on
a3. Opus 5.5 so far reads 97 to 98 on a1, 104 on a2 and 88 on a4, one or two days each.

## Checks

- tests/test_speed.py: the trigger (last `user` line, parallel tool results, a `user` line
  between blocks), multi-block responses and the duplicate-line artefact, sidechains,
  no-trigger, filters, dedupe across files and calls, the extract reader, the fast-mode
  flag (including a mid-session fallback and non-Opus), daily aggregation and the 30-request
  rule, pooling two histories, merging and the recompute window, the command-line append,
  and that the block carries labels only.
- `python3 tools/credits_report.py --publish-check` on a publish built from a refreshed
  gs-passive.json and the seeded history/masterrig-speed.json: all 18,328 figures reproduce,
  7,701 of them in `speed`.
