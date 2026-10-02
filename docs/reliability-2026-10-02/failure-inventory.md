# Failure inventory, 2026-10-02 (UT-1)

Built from runtime evidence, git history, Jonathan's own messages and GitHub issues,
not from reading the code. Owners: UT-1 collection and publish path, UT-2 Codex audit
of 2026-09-16, UT-3 scheduling, self-healing, health and alerting, UT-4 chaos soak.

## Evidence sources

| Source | What was read |
|---|---|
| masterrig cron log | `~/.paperclip/ops/claude-usage-passive.log`, 115 lines |
| masterrig meter | `~/.moonlighter/usage_log.jsonl`, 4,713 rows from 2026-06-13 to 2026-10-02 |
| Committed records | `history/passive.json` at 12 points in git, `history/masterrig-passive.json`, `history/gs-passive.json` |
| Publisher cadence | 467 "Daily publisher state" commits, 2026-09-09 to 2026-10-01 |
| Live page | `alldonesites.com/data/claude-usage.json`, read at 2026-10-02 00:06Z |
| Jonathan | User turns in `~/.claude/projects/-home-grafe-code-claude-usage-tracker/*.jsonl` and in `-home-grafe-code` turns that mention "usage tracker" |
| Issues | `gh issue list --state all`: 29 issues, all closed |

## Design flaws: themes with 3 or more fixes

| # | Theme | Fix commits | Status |
|---|---|---|---|
| D1 | `resets_at` compared as strings, but it jitters | 37e1c0e, aa27a90, 84b4e05 (2026-09-06), then UT-H's `same_reset` tz fix | Fixed: `tracker/usage_api.py:128` `same_reset` with a 60 s tolerance. UT-H owns the remaining tz work |
| D2 | 429 handling on the usage endpoint | 3a1b4a0, b0460ed, b80bf94, 65074c9, 1ec0f93 | Fixed on main: `usage_api._default_fetch` backoff, plus the sampler's skip-while-backing-off (`tracker/meter_log.py:133`) |
| D3 | Git races between the masterrig push, the gs publisher and the site repo | 5b616b0, a91e5d3, 3a094a7, a369e83, 899556a | Fixed on main: pull --rebase --autostash before the joins and before the commit, `site_pull` and `site_push` in `bin/daily.sh` |
| D4 | masterrig's record lags the code on main | 3a094a7, c819917, b7c677d (#95, #129) | Fixed on main: the `.passive-last-ok` stamp holds the tree of tracker/ (`bin/passive.sh:33`) |
| D5 | Stale or placeholder data reaching the page | 07f2877, #32, #33, b26559a (#85) | Fixed on main: `account_feeds` states (`tracker/publish.py:1820`), feed ages at `publish.py:89-93` |
| D6 | **A record rebuilt from a source that ages out loses its history** | 7e67493 (#63) kept masterrig's stretches; `tracker/speed.py` keeps old rows. Never applied to `tracker/passive.py` | **Open, F1 below** |

## Failure modes

### F1. passive.json loses days and the 5x-to-20x ratio as transcripts age out (fixed on ut-1-forensics, 4086e7c)
- Symptom: the committed `history/passive.json` started at 2026-07-30 (09-05), 08-07 (09-06),
  08-11 (09-09 to 09-19), then 08-20 from 2026-09-21 onward. Its `plan_ratio_5x_to_20x`
  went 0.3865, 0.423, 0.4468, 0.6696, 1.0287, 0.6612, 0.2492 and has been `None` since
  2026-09-21. The cron log shows "ratio None" on every run since then.
- Root cause: `tracker/passive.py:49-77` rebuilds the whole record from
  `~/.claude/projects` on every run. masterrig's oldest transcript turn today is
  2026-08-20 10:52Z: Claude Code's 30-day transcript cleanup deleted everything older
  (`cleanupPeriodDays` is now 3650, set too late for July and early August). Every day
  whose transcripts are gone drops out, and the ratio's "before 14 Aug" side went with them.
  The ratio swung for the same reason before it vanished: each run saw fewer pre-cut days.
- Impact: the publisher does not read the ratio. It reads `history` keys only as the
  earliest day for the held fill (`tracker/publish.py:625`), so the page's back-fill
  start moved from 30 July to 20 August.
- Fix: merge with the stored record. Days earlier than the first day the transcripts can
  still produce are kept as stored. Days the transcripts still cover are recounted.
- Not recovered: the days already lost (2026-07-30 to 2026-08-19). They are in git, in
  `87aa061:history/passive.json` (from 07-30) and `9e40788:history/passive.json` (from 08-11).
  Once the fix is on main, merging those days into `history/passive.json` once makes them stick.
  This is left to the integrator because automation rewrites that file on main every day.

### F2. The cron logs have no timestamps (fixed on ut-1-forensics, ee71d1e and the daily.sh commit)
- Symptom: `claude-usage-passive.log` holds 115 lines, none with a date. The 2026-09-?? line
  `fatal: couldn't find remote ref crossing-detection` (the checkout was on a feature branch,
  so the cron pulled and pushed that branch) cannot be dated from the log at all.
- Root cause: `bin/passive.sh` and the Python modules print without stamps.
- The same holds on gs: `claude-usage-daily.log` has 3,265 undated lines, so its 18 site-push
  failures and the 2026-10-01 DNS outage cannot be dated from it.
- Fix: passive.sh stamps every line it writes itself. daily.sh writes one dated line at the start of each run.

### F3. passive.sh follows whatever branch the live checkout has checked out (fixed on ut-1-forensics, ee71d1e)
- Symptom: the log line above. The cron pushed `crossing-detection` to origin
  ("Create a pull request for 'crossing-detection'"), so that day's masterrig record never reached main.
- Root cause: `bin/passive.sh:30` `BRANCH="$(git rev-parse --abbrev-ref HEAD)"`.
- Fix: refuse to run off main (a dated line and exit 3), so a feature checkout never publishes a record to a side branch.

### F4. Missing masterrig days when the machine sleeps (fixed on main)
- Symptom: no "Passive history" commit for 2026-09-20 or 2026-09-22. The moonlighter meter
  has gaps of 12.0 h (09-22), 6.5 h (09-24), 10.5 h (09-28). Jonathan, 2026-09-28 13:52:
  "fix max account 1 catching up after sleep. Make sure that the last sample measurement is
  actually measuring every half an hour".
- Root cause: a daily cron that the machine slept through.
- Status: fixed in b7c677d (#95): hourly cron with a 20-hour age rule. The meter's own gaps
  are the machine being off and cannot be recovered.

### F5. Change alert sent every half hour when the subscriber send fails (fixed on main, #109)
- Six identical alerts on 2026-10-01. Fixed in eba1852 (`bin/daily.sh`, once per UTC day).

### F6. Page reads stale while data is fresh (fixed on main)
- Jonathan, 2026-09-16 14:04: "Last sample 16 Sept, 03:15 why does it say this then".
  2026-09-29 11:18: "the page is still stale" (the deploy was blocked by auto mode, not a tracker fault).
- Fixed: `meter_read_at` (`tracker/publish.py:728`) and `account_feeds`.

### F7. Publisher gaps of 24 h before 2026-09-16 (fixed on main)
- The publisher ran once a day until the half-hour cron of 2026-09-16 (#6). Since then no
  gap between "Daily publisher state" commits exceeds 90 minutes.

### F8. The wide change intervals (not a collection fault)
- Jonathan, 2026-09-28 19:32: "five-hour −2% to +63% ... surely thats a bug?". Traced in
  `docs/findings-2026-09-29-*`: rounding of the 1% meter and missing work. Model work, out of scope tonight.

### F9. On gs, 55% of meter reads are 429s (fixed on ut-1-forensics, 5e895f2)
- Symptom: `~/.paperclip/ops/claude-usage-meter-{avis,dave,jwork}.log` on gs. Until
  2026-09-22 each account logged about 262 good reads a day and no 429s. From 2026-09-23,
  each logs about 600 good reads and 650 to 800 `rate_limited` lines a day. That is 18,784
  429 lines with `retry_after_s: 0.0` in all, about every other tick
  (2026-10-01 12:00-13:59 for dave: `xx.x.x.x.xx.x.x.x.xxx...`). 15 more carried Retry-After of about 3,600 s.
- Root cause: #78 (1ec0f93, 2026-09-23) moved the timers to one tick a minute
  (`deploy/systemd/*.timer`, `OnUnitActiveSec=1min`). The endpoint lets about one call in two
  minutes through per account and answers the rest with a 429 carrying Retry-After 0. The
  backoff in `tracker/meter_log.py:_backoff_remaining_s` reads that 0 as "no wait".
- Impact on data: `parse_meter_log` skips error lines, so no wrong numbers, but there are real
  gaps. A run of Retry-After 0 answers escalates to a Retry-After 3600 block. jwork on
  2026-09-26 went from about one 429 every other minute (14:00 to 14:27Z) to a 3,600 s block
  at 14:28Z, with no good read until 16:29Z. Gaps over 15 minutes since 2026-09-23: avis 26,
  dave 25, jwork 18 (up to 123 min). Nearly all of them start with a run of 429s.
- Fix: a tick within 110 s of the last good read or 429 skips the call (`MIN_READ_SPACING_S`).
  The timers stay at one minute, so a good read still comes about every 130 s.
- Needs a deploy on gs (a `git pull` in `~/claude-usage-tracker`). Nothing to restart, since each tick is a oneshot.

### F10. gs DNS outage, 2026-10-01 about 17:23Z (environment, self-healed)
- 45 `URLError ... Temporary failure in name resolution` lines per account. The publisher log
  shows the matching contributed-export, site-pull and tracker-push failures. Both gs
  checkouts were level with origin at 00:15Z on 2026-10-02. No action.

### Live state at 2026-10-02 00:06Z
- Page `generated_at` 00:02Z. a1 (masterrig) fed 21:15Z, a2 and a3 fresh, a4 idle. Publisher cadence holding.

## Open items by owner

| Item | Owner |
|---|---|
| F1 passive.json history merge | UT-1, done. Restoring the lost days is left to the integrator |
| F2 timestamps in both cron logs | UT-1, done |
| F3 passive.sh off-main guard | UT-1, done |
| F9 meter read spacing on gs | UT-1, done. Needs a gs pull to take effect |
| Health check for the 429 rate and for an undated or stopped log | UT-3 |
