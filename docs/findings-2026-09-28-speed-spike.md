# Max account 1's Opus 5 speed spike, 15 to 22 September

Issue #121. Written 2026-09-28. No code changed: the speeds are real.

## The question

On the page's "Opus 5 by account" chart, Max account 1 reads about 160 to 175 output
tokens a second on most days from 15 to 21 September, with one day at about 75 in between.
Max accounts 2, 3 and 4 read 75 to 95 over the same days, and account 1 read 65 to 85
before 15 September. Fast mode had already been ruled out: every usage record in account
1's transcripts from 14 to 24 September says `"speed": "standard"`.

The publisher run below (`python3 -m tracker.publish ... --out <scratch>`, which writes
the JSON and pushes nothing) gives account 1's daily Opus 5 medians for 10 to 27
September. The format is median tok/s, then requests, then fast-session requests:

| day | Max account 1 | Max account 2 |
|---|---|---|
| 10 Sep | 75 (1,799, 49) | 74 (782, 0) |
| 11 Sep | 76 (222, 0) | 77 (35, 0) |
| 15 Sep | 175 (370, 370) | 76 (419, 0) |
| 16 Sep | 165 (386, 386) | 84 (1,112, 25) |
| 17 Sep | 169 (237, 237) | 78 (1,122, 0) |
| 18 Sep | 74 (2,444, 215) | 75 (1,167, 0) |
| 19 Sep | 156 (361, 306) | not published |
| 20 Sep | 162 (261, 261) | 85 (607, 0) |
| 21 Sep | 165 (936, 935) | not published |
| 22 Sep | 75 (175, 35) | 105 (36, 0) |
| 23 Sep | 78 (629, 0) | 85 (85, 0) |

12 to 14 September and 24 to 27 September have fewer than 30 timed Opus 5 requests on
account 1, so no row is published for them. After 23 September, account 1's Opus work moved
to other models. The table is also the "after" figure, because nothing was changed.

The low day, 18 September, is not an exception. That day's fast requests (215, all from
`claude -p` sessions) are outnumbered by 2,229 normal ones from interactive and Agent SDK
sessions, so the day's median is the normal speed.

## How the series is built

`tracker/speed.py`: each response is timed from the last `user` line before its first
content block to the latest timestamp among its blocks. A response's lines are grouped by
`message.id`, and its output count is the largest `output_tokens` among them. A response
counts only if it has at least 300 output tokens and took 1 to 900 seconds. Message ids
are deduplicated across files, first file wins. Each machine bins the speeds per day,
model, account and entrypoint, and the publisher pools the bins and takes medians. The
inputs checked here were the text-free masterrig extract (20 August to 23 September) and
masterrig's own transcripts, which are still on disk back to about 25 August.

## What was ruled out

- **The wrong model.** All 2,746 fast-session requests and all 3,071 other kept Opus 5
  requests of account 1 from 14 to 23 September carry the raw id `claude-opus-5`. None is
  Opus 5.5, Haiku, or an unknown id mapped to Opus 5. `normalize_model` aliases only Fable
  5 to Fable 5.1.
- **The duration.** On account 1's main-chain interactive requests, 14 to 23 September,
  fast sessions are faster in both parts of the time:
  - time to first block: a median of 3.1 s against 6.1 s;
  - first block to last block: 0.7 s against 1.8 s;
  - output after the first block: 390 tok/s against 162 (2,087 requests against 1,241).

  If the start were being cut short, only the time to first block would shrink. If the end
  were moved early, only the second part would. Both shrink by about the same factor.
  The duplicate block lines from Claude Code 2.1.278 and 2.1.280 are already handled: the
  end is the latest timestamp, not the last line. The output counts are not inflated: every
  response has at most one `usage.iterations` entry. The 23 September findings also measured
  2.37 characters per output token in fast sessions against 2.35 in normal ones.
- **Sub-agent files, resumed sessions, sidechains.** A fast session's sub-agents are fast,
  and a normal session's sub-agents are normal. For example, a 21 September fast session
  has 12 sub-agent files with medians of 166 to 207, while the sub-agents of the 18 and 23
  September normal sessions read 66 to 88. Fast sessions have no more sidechain requests
  than normal ones: 444 of 2,746 fast against 914 of 3,071 normal. Deduplicating message
  ids means a resumed session's copied history is counted once.
- **Pooling or a symlink.** On masterrig, `~/.claude/projects` is a real directory, and no
  other config dir there (`.claude-account3`, `.claude-greenscape`, the rollback dir) has a
  `projects` directory. So no other login's transcripts are read as account 1's. The 2 to 5
  September phantom usage is meter movement with no transcripts behind it. It affects the
  passive stretches, not transcript timing, and it comes before the spike.
- **Client version, effort, entrypoint.** Versions 2.1.272 to 2.1.278 appear on both sides.
  Effort is `low` or `medium` in both groups. Fast sessions occur in both `cli` and `sdk-cli`.

## What the fast sessions have in common

Each Claude Code transcript records an `atis-latch` line. Its value is a token the server
gives the session. The client sends it back on every request as the `x-cc-atis` header,
and one session keeps the same value throughout. It is either empty, a bare 16-hex-digit
id, or a longer `v1.<id>.<...>` form built around the same id. Account 1's sessions carry
one of two ids, called A and B here (the values are not recorded in this repo). Account 2's
sessions carry two others, C and D. The table counts Opus 5 sessions with at least 8 kept
requests, and gives a sub-agent file its parent's value:

| account | token | sessions | fast (median 120 tok/s or more) |
|---|---|---|---|
| 1 | id A, either form | 21 | 20 |
| 1 | id B, either form | 39 | 0 |
| 1 | empty | 55 | 0 |
| 1 | none written (Agent SDK, older versions) | 334 | 0 |
| 2 | id C or D, either form (10 to 23 Sep) | 28 | 0 |
| 2 | empty or none (10 to 23 Sep) | 141 | 0 |

The one id A session that is not fast is 22 September's. It is the session the 23 September
findings saw fall from about 150 to about 75 at 11:03 and stay there. Its median over the
day is 75. Id A first appears on 10 September (the 49 fast `sdk-cli` requests that day) and
last appears on 22 September. Id B covers 27 August to mid-September, and all its sessions
run at normal speed.

## Conclusion

This is not a measurement error. The fast sessions are the Opus 5 sessions that the server
gave id A, and the speed-up applies to both parts of each request's time, not only to one
end. Why the server served those sessions faster is not visible from the client. The
transcripts show only that it assigned them differently. The publisher's method, figures
and caveat ("Some Opus sessions run at about 2.5 times the model's usual speed … Why these
sessions run faster is not known") stand as they are, and no code was changed. The caveat
could say that the fast sessions are the ones the server gave one particular session token,
but no published figure depends on that.

## Checks

- No test files were changed, so no tests were run for this note. ruff is not installed on gs.
- The publisher was run locally, writing to a scratch file. The table above is its output,
  and it matches the live history rows.
- The per-session token classification read masterrig's transcripts in place and gs's
  transcripts for account 2. It used `tracker/speed.py`'s own `requests_in` and `kept`, and
  copied no transcript text off either machine.
