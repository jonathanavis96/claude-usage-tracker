# Operations

What runs, where, how it heals itself, and what each alert means.

## What runs

| Host | Job | Interval | What it does |
|---|---|---|---|
| masterrig | cron `15 * * * *` → `tracker.supervise` → `bin/passive.sh` | hourly; real work every 20h or when `tracker/` changes on main | passive join, writes `history/passive.json`, `masterrig-passive.json`, `masterrig-speed.json`, commits and pushes "Passive history" |
| masterrig | moonlighter `gate.py` (not this repo) | 30 min | samples the usage meter into `~/.moonlighter/usage_log.jsonl`, the join's meter input |
| gs | `bin/daily.sh` | ~30 min | merges history, publishes the site JSON, commits "Daily publisher state" (`history/gs-passive.json`) |
| gs | `claude-usage-meter-{avis,dave,jwork}.timer` (`deploy/systemd/`) | 1 min | one meter sample per account into `~/.paperclip/ops/claude-usage-meter-*.log`; exit 4 = logged gap, next tick retries; a 429 backs off by `retry_after_s` |

Install or update the masterrig schedule with `deploy/install-schedule.sh` (`--dry-run` first). It is idempotent and replaces the old bare `bin/passive.sh` cron line.

## How it heals

- **Overlap:** `tracker.supervise` takes an exclusive non-blocking `flock` on `.supervise.lock`. A run that finds it held exits 0 and does nothing. The kernel releases the lock when its holder dies, so it cannot go stale; a leftover `.supervise.pid` whose process is dead is removed before the next run.
- **Transient failure:** a non-zero exit is retried twice, after 30 s and 60 s. Each attempt is a fresh process, so it re-reads `~/.claude/.credentials.json` and the checkout.
- **Missed runs:** `bin/passive.sh` makes up a slept-through day on the next hourly tick (its own 20h stamp).
- **Bounded growth:** the run log rotates at 5 MiB, keeping `.1`–`.3`. The health check fails if any log passes 5 MiB or `history/` passes 200 MiB. History is never pruned automatically: every row is an input to the published figure, and dropping rows would change the numbers.

## Health check

`python3 -m tracker.health` prints `ok` and exits 0, or prints the first failure on one line and exits 1. `--all` lists every check.

| Line starts with | Meaning | Usual fix |
|---|---|---|
| `collection:` | newest meter sample older than 1 h | moonlighter's gate is not running, or the meter read is failing |
| `run:` | no successful supervised passive run in 2 h | read the tail of `~/.paperclip/ops/claude-usage-passive.log` |
| `publisher:` | `history/gs-passive.json` not committed in 2 h | gs `daily.sh` is failing or gs is down |
| `token:` | credentials unreadable, signed out, or access token expired more than 12 h ago, or refresh token expired | `claude /login` on masterrig |
| `lock:` | dead pid file, or a run holding the lock over 2 h | a hung join; kill it, the next run clears the pid file |
| `sizes:` | a log or `history/` over its bound | look for a runaway writer |

## Alerts

Sent to Jonathan on WhatsApp through pihome's `wa_send.py`. At most two messages per incident:

- **"needs attention: N runs in a row failed (exit X after 3 attempts)"**: three hourly runs failed even after retries. Something persistent is broken: git push rejected, a join crashing, disk full.
- **"needs attention: <health line>"**: a health check has failed continuously for an hour. The line is the one from the table above.
- **"recovered after N min: ..."**: the incident has closed (a run succeeded and health is clean). Nothing further is needed.

A failed send leaves the incident unopened, so the next hourly run tries again. Set `CUT_ALERT_DRY_RUN=1` or pass `--dry-run` to print alerts instead of sending them. State lives in `.supervise-state.json` in the checkout (gitignored).

## Known gaps

- If cron itself stops, nothing runs to send an alert. An external heartbeat (for example pihome checking `.supervise-state.json` age) would cover that and is not built.
- gs jobs are not yet under `tracker.supervise`. The meter timers already log their gaps; `daily.sh` has its own lock (exit 6).
