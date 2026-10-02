# Usage-endpoint pollers and the shared reader (2026-10-02)

Every process that calls `https://api.anthropic.com/api/oauth/usage` on masterrig and gs,
measured on 2026-10-02 at about 01:20 UTC. Inventory was read-only: `crontab -l`,
`systemctl --user list-timers`, `ps` and the pollers' own logs. "Tonight" means since
2026-10-01 16:00 UTC (18:00 SAST).

## Inventory

| Host | Poller | Trigger and interval | Account | Refused: tonight | Refused: longer window |
|---|---|---|---|---|---|
| masterrig | `~/.cache/usage_all.py` | ad hoc, run by sessions | `~/.claude` | no log | no log |
| masterrig | `burn_watch.py` (front-end scratchpad, writes `usage.log`) | process loop, 240 s (90 s within 12 points of its threshold) | `~/.claude` | 2/23 = 9% (03:12 and 03:16 SAST) | started 01:42 SAST |
| masterrig | `mis-usage-ceiling.sh` (`mis-usage-ceiling.service`, loop-orchestra) | process loop, 300 s; backoff `300 × (fails+1)`, cap 900 s | the seats' account, resolved each poll via `claude-account.py` | 2/105 = 1.9% | 7/1,883 = 0.4% since 09-25; 93/9,998 = 0.9% since 08-18 |
| masterrig | claude-usage-tracker samplers | none: `passive.sh` (cron :15) only joins logs, it does not call the endpoint | | | |
| gs | `claude-usage-meter-avis.timer` (`tracker.meter_log`) | timer, every 1 min | `~/.claude-avis` | 277/532 = 52% | 5,119/9,428 = 54% (7 d) |
| gs | `claude-usage-meter-dave.timer` | timer, every 1 min | `~/.claude-dave` | 266/532 = 50% | 5,111/9,483 = 54% (7 d) |
| gs | `claude-usage-meter-jwork.timer` | timer, every 1 min | `~/.claude-javiswork` | 301/532 = 57% | 5,325/9,035 = 59% (7 d) |
| gs | `gs-usage-ceiling.timer` (`greenscape-org/scripts/usage-ceiling.py --once --enforce`) | timer, every 5 min | `~/.claude-javiswork` (seat.conf drop-in) | 66/110 = 60% | 2,763/7,447 = 37% since 08-21 |
| gs | `takeoff/bin/usage-guard --once --threshold 99` (cron `*/10`) | cron, every 10 min, all three accounts in one run | javiswork, dave, avis | 12/57 = 21%, 4/54 = 7%, 2/57 = 4% | 9%, 4%, 9% since 09-09 |

Counted from: `usage.log` lines with `5h=None`; `mis-usage-ceiling-systemd.log` READ
FAILURE lines not preceded by a BLIND (account-resolution) line, against `5-hour N%`
lines; meter JSONL lines with `reason: rate_limited`; `gs-usage-ceiling.log` `FAILED`
against `ok` lines; `takeoff/var/usage-cron.log` per-account `error=http 429`. The
masterrig ceiling also logged 682 account-resolution failures since 08-18, which never
reached the endpoint and are excluded above.

Not in scope but seen: `mis-usage-feed.py` (now on pihome, all accounts) and
`mis-window-warden.sh` also call the endpoint; neither runs on masterrig or gs tonight.

## What the numbers say

- **gs is the problem, and the limiter is per host.** In the seven days to 10-02 the
  three gs meters were refused in the same minute far more often than singly: of the
  minutes with a reading from all three, 3,454 had all three refused, 1,431 two, 1,278
  one and 2,517 none. One extra caller of any account costs every account on gs.
- **gs runs the old meter.** gs's `~/claude-usage-tracker` is on `main` at `d92eaed`,
  whose `meter_log.py` has no `MIN_READ_SPACING_S`; the 110 s spacing on
  `burn/20261002-ut-integrate` is not deployed, so each meter still calls every minute.
- Retry-After is nearly always absent or `0` on a refusal (3, 3 and 10 of ~6,000 meter
  429s carried a positive value), so a backoff that only honours the header never backs
  off.
- masterrig's pollers are rarely refused. The burn watcher's two failures (03:12 and
  03:16 SAST) fell between ceiling reads that succeeded (03:11:44 and 03:16:48).

## The shared reader: `tracker/usage_cache.py`

- `python3 -m tracker.usage_cache poll --account A --config-dir D --loop` is the one
  caller for (host, A). A non-blocking flock on `<cache>/A.lock` makes a second poller
  exit `busy` without calling.
- It writes `<cache>/A.json` atomically (temp file, fsync, `os.replace`, directory
  fsync): `fetched_at`, `account`, `identity`, `host`, the raw `buckets`, `last_error`,
  `last_error_reason`, `consecutive_refusals` and `next_allowed_at`.
- Spacing is 110 s after a success. After the n-th 429 in a row the wait is
  `max(Retry-After, 110 × 2^(n−1))`, capped at 1,200 s. A 429 also writes
  `<cache>/_host.json`, which holds every account on the host.
- Readers call `read_cached(account, max_age_s=…)` or
  `python3 -m tracker.usage_cache read --account A --max-age S` (exit 0 fresh, 3 stale,
  4 missing or corrupt). Neither calls the API.
- Cache directory: `$USAGE_CACHE_DIR`, default `~/.cache/claude-usage-tracker/usage`.

## Switch-over, one poller at a time

Nothing below is done yet; existing callers are unchanged on this branch. Start the
pollers first, one user unit per host and account, then move readers.

| Poller | Switch-over |
|---|---|
| gs: start the shared pollers | Three user units: `python3 -m tracker.usage_cache poll --loop --account {avis,dave,jwork} --config-dir ~/.claude-{avis,dave,javiswork}`. |
| gs meters (`tracker.meter_log`) | Add a `--from-cache` path: `read_cached(account)`, and append a line only when `fetched_at` is newer than the log's last line. The meter keeps its log format and identity check and stops calling the API. Stop the three meter timers' network calls the same day. |
| gs `usage-ceiling.py --enforce` | Read `jwork.json` with `--max-age 600`. A stale or missing cache keeps its current fail-open behaviour ("usage read FAILED — failing open"). |
| gs takeoff `usage-guard` | Replace `fetch_usage` per seat with a cache read for `jwork`, `dave` and `avis`. A stale record is an error, as a 429 is today, so `stop=` logic is unchanged. |
| masterrig: start the shared poller | One user unit for the masterrig account: `poll --loop --account jono --config-dir ~/.claude`. If the seats move to another account, start a poller for that account too. |
| masterrig `mis-usage-ceiling.sh` | After it resolves the seats' account, run `python3 -m tracker.usage_cache read --account <label> --max-age 600` instead of `curl`. Exit 3 or 4 is a READ FAILURE, so the blind and backoff paths are unchanged. Remote accounts (`ssh://` credentials) stay as they are until gs's cache is reachable from masterrig. |
| masterrig `burn_watch.py` | Replace `usage()` with a `read --account jono` subprocess or `read_cached`. A stale record logs `5h=None`, as a refusal does today. |
| masterrig `~/.cache/usage_all.py` | Print `read --account jono` output, which includes `age_s`, instead of calling `curl`. |
