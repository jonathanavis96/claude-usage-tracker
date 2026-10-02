# Integration of the 2026-10-02 burn (UT-I)

Branch `burn/20261002-ut-integrate`. It holds UT-2, UT-3, UT-4, UT-H, UT-1, UT-5 and the red-team branch UT-R, merged in that order with merge commits, plus the fixes below.

## What merged
- **UT-2**: closure of the 2026-09-16 Codex audit (`docs/burn-20261002/audit-closure.md`) and its regression tests.
- **UT-3**: `tracker/health.py`, `tracker/supervise.py` (flock, retries, log rotation, one alert per incident), `deploy/install-schedule.sh`, `docs/OPERATIONS.md`.
- **UT-4**: the chaos soak harness in `tests/chaos/`.
- **UT-H**: hardening against malformed input, `tracker/atomic.py`, and the 11 tests that also failed on main. They had treated the live price table as fixed data.
- **UT-1**: the failure inventory. `passive.json` keeps days the transcripts no longer reach. `passive.sh` dates its lines and exits 3 when the checkout is off main. Meter reads are at least 110 s apart, and `daily.sh` writes a dated start line.
- **UT-5**: the Uptime Kuma dead-man ping, `--profile gs` and `deploy/install-gs.sh`, the publisher test gate `tracker/publish_gate.py`, the `probe.py` fix for zero `meter_weight`, and the `passive_account_count` freshness fix. It conflicted in `tracker/health.py`, where UT-5's `meters` check and UT-I's meter check were folded into one. A conflict in `docs/OPERATIONS.md` was also resolved.

The only conflict was in `tracker/passive.py` (UT-H's atomic write against UT-1's `_rebuild` split), and both sides were kept. A line-by-line check found every line each branch added in the final tree, except lines that a later fix replaced on purpose.

## What was fixed on this branch
- **All six chaos findings, in `tracker/meter_log.py`.** The xfails are now ordinary tests: `tests/chaos` was 12 passed, 6 xfailed and with `--runxfail` 6 failed, and is now 18 passed.
  1. A torn last line gets a newline before the next reading.
  2. A read-only or full disk exits 4 with a note on stderr, not a traceback.
  3. `flock` on `<log>.lock`: a second run waits, then skips under the 110 s spacing.
  4. A clock up to 20 min behind the last line skips the tick.
  5. A last line more than 20 min in the future (written by a fast clock) is ignored.
  6. Retry-After is capped at 1200 s.
- **Health and supervisor.**
  - `passive.sh` exit 3 (off main) is not retried. It counts as a failed run and alerts on the first occurrence, and `health` fails `run` with "checkout is off main".
  - New `meter` check (`--meter-log`, repeatable). The limit is 180 s, which allows for the 110 s spacing, plus the Retry-After of a trailing 429. The check also fails when more than half of the last 40 or more lines are 429s. That rate went unseen for nine days in the 2026-09-23 storm.
- **Lost days restored.** `history/passive.json` has 2026-07-30 to 2026-08-19 again, from 87aa061 and 9e40788. Where both commits hold a day, the newer one wins. No day already on main was overwritten. The test `tests/test_restore_passive_days.py` proves that a restored day survives a rebuild, and that the 5x-to-20x ratio becomes computable again.

- **False alarms removed.**
  - `health` judged the publisher by HEAD, but masterrig pulls only about once a day. It now also reads the fetched upstream, which before this fix would have alerted every day.
  - An expired access token is no longer a failure while the refresh token is still valid, so a weekend away does not alert.
  - masterrig's Kuma window allows for nights the machine is off.
- **The publish gate catches bad data.** The new `tests/test_prices_data.py` runs in the gate and checks every price row for sanity. On this tree the gate passes (479 passed). A test feeds the gate a deliberately broken row (`output: "10"`, `cache_read: -1`), and the gate fails.
- **The gs alerts reach Jonathan.** An incident is pushed to Kuma as `status=down&msg=<reason>`, and its recovery as `status=up`. This was tested against a fake server on 127.0.0.1.
- **Two tests that failed on main and on every branch now pass.** They are in `tests/test_detect.py` and read the hourly-rebuilt `history/passive.json` (evidence 103 -> 102). They now read a frozen fixture, `tests/fixtures/weekly_windows_2026-09-16.json`, taken from 44471f4.

- **All 11 red-team findings fixed** (`docs/burn-20261002/redteam.md`); every strict xfail in `tests/redteam/` is now a passing test.
  - Supervisor: it survives a full-disk run log, an unwritable state file and a crashing health check. A run is killed at 45 min, and a lock held past 2 h alerts. A failed alert send is recorded.
  - Health: freshness is judged from real readings, not error rows. An odd token file or pid file is reported instead of crashing the check.
  - `bin/passive.sh` exits 5 when its commit or push fails, where it used to exit 0.
  - `bin/daily.sh` records a failing site push in `history/site-push.json`, which fails the publisher check. Before, the page could freeze while health stayed green.

## End-to-end proof
Both trees ran the real `tracker.passive` and `tracker.publish` with the same inputs: a temporary HOME, a copy of the moonlighter and ceiling logs, the real transcripts read-only, and main's committed `history/`. A `sitecustomize` guard blocked all network. No alert config was present.

- `origin/main` and this branch produced identical `passive.json` (3191 leaves) and identical `claude-usage.json` (23340 leaves). Only `generated_at` differed.
- The intended difference: seeded with this branch's restored `passive.json`, `passive.json` gains exactly the 21 restored days. `plan_ratio_5x_to_20x` goes from None to 0.2532. `claude-usage.json` changes only in its per-model `history` series, which starts on 2026-07-30 instead of 2026-08-20, and in `passive_generated_at`. Every other top-level key is identical..
- The red-team reset-boundary fixes were rerun on fresh inputs, after `merge_samples` began preferring reset-bearing readings and `_is_reset` began splitting at a known reset. `passive.json` then differs on 5 days, the intended change:
  - 2026-08-28: 3,830,801 to 4,410,225 tokens per %, +15%;
  - 2026-09-03, 09-04, 09-09 and 09-29: within ±1.2% each.

  `claude-usage.json` stays identical across all 23,340 leaves.
- The supervisor (dry-run sender) and health CLI were run against a fake `passive.sh` that exits 3. Result: exit 3, no retry, one dry-run alert naming "checkout is off main", and `health` exit 1 with the same reason.

## Test results
FULL_RESULTS

## Still open
- **The 429 storm on gs needs a `git pull` on gs to take effect** (step 2 below). Until then gs keeps its old code.
- The supervisor's alert reads "1 runs in a row failed" for exit 3. The wording is cosmetic.
- F8 (wide change intervals) is model work, out of scope.
- `history/passive.json` changes on main about every hour. If the merge conflicts on it, take main's file and run `python3 tools/restore_passive_days.py` again. It is idempotent and only adds missing days.
- The `alldonesites` page (the only consumer of `claude-usage.json`) needs no change. The end-to-end run shows the same schema, and only the history series extends back further.

## Deploy steps (morning, for Jonathan)
**Use [`DEPLOY.md`](DEPLOY.md) instead.** It is the single morning page: one dry run and one `--apply` for both hosts, with backups, rollback, the gs Kuma URL fix (gs cannot resolve `pihome`) and a table mapping each step below to its own. The list below is kept as UT-I wrote it.

Every fix below that needs a live change takes effect through these steps: the gs pull, the two installers (they replace the bare cron lines, which fixes the discarded supervisor output) and the Kuma monitors. Nothing else needs a manual change.
1. **Ship.** Say "ship it" to run `ship-to-main` on `burn/20261002-ut-integrate`.
2. **gs.** `ssh gs 'cd ~/claude-usage-tracker && git pull'`. This takes the 110 s meter spacing (which ends the 429 storm), the gate and the Kuma push live. Then, on gs: `deploy/install-gs.sh --dry-run`. Check that it replaces the bare `bin/daily.sh` line with one managed block, then run `deploy/install-gs.sh`.
3. **masterrig.** `cd ~/code/claude-usage-tracker && git pull && deploy/install-schedule.sh --dry-run`. Check that it prints one managed block that replaces the bare `bin/passive.sh` line, then run `deploy/install-schedule.sh`.
4. **Uptime Kuma.** Create two Push monitors, following `docs/OPERATIONS.md` → "Dead-man switch". **gs**: Heartbeat Interval 2700 s, Retries 2. **masterrig**: Heartbeat Interval 43200 s, Retries 1. masterrig is off or asleep some nights (gaps of 6 to 12 h, four times in the last 30 days), so a short window would raise a false alarm every such night. Write each push URL to `~/.config/claude-usage-tracker/kuma-push-url` (chmod 600) on its host.
5. **Confirm health.** `python3 -m tracker.health --all` on masterrig should print `ok` on every line. On gs, run `python3 -m tracker.health --profile gs --all`. After the next scheduled run, check that `last_ping` is `"ok"` in each host's state file (`.supervise-state.json` on masterrig, `~/.paperclip/ops/claude-usage-daily-state.json` on gs) and that both Kuma monitors are UP.

### What alerts you afterwards, and how
WhatsApp (pihome `wa_send.py`) sends one message when an incident opens and one when it recovers. Nothing repeats while an incident stays open. An incident opens on any of these:
- three supervised runs fail in a row;
- one run exits 3 because the checkout is off main;
- `passive.sh` exits 5 (commit or push failed) three runs in a row;
- the site push is failing (`history/site-push.json`), so the page is frozen;
- a run hangs with the lock held for over 2 h;
- a `health` failure lasts an hour: collection stale, publisher not committing for 2 h, token expired past 12 h or refresh token expired, a stale lock, oversized logs or history, a stale meter log, or a 429 storm.
On a host with a Kuma push URL, the incident goes to Kuma first as `status=down` with the reason, so Kuma's notification is the alert. Recovery is sent as `status=up`. WhatsApp, then email, are used only when Kuma is unreachable. gs cannot reach pihome, so there it is Kuma, then email.
- **Kuma itself** alerts when a host's pings stop: about 2.25 h on gs, about 24 h on masterrig.
- The gs **publish gate** sends one alert when the data tests start failing, for example a bad price row, and one when they pass again. It never blocks the publish.

Nothing else alerts. Each message is something that is really broken and names the command to check it.
