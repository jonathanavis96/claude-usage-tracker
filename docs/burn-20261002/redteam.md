# Red-team findings, 2026-10-02 (UT-R)

Branch `burn/20261002-ut-redteam`, cut from `burn/20261002-ut-integrate` at 6f9a99b.
Every confirmed failure has a strict xfail in `tests/redteam/` that fails for the stated
reason on that code (checked with `--runxfail`). When a fix lands, its test reports a
strict XPASS; remove the marker then. Run one file at a time:

    timeout 300 nice -n 10 python3 -m pytest tests/redteam/<file> -q

`tests/redteam` has no `__init__.py`, like `tests/chaos`, so `unittest discover` skips it.
Its conftest blocks every non-loopback socket and points HOME at a temp dir.

## Confirmed failures

One line each: where, impact, suggested fix.

**Status (UT-I): all five fixed in `tracker/supervise.py` and `tracker/health.py`; markers removed, 5 passed.** Run log on a full disk falls back to DEVNULL; `write_state` returns False and forces the alert; each health check is wrapped (`check crashed`); runs time out at 45 min (exit 124) and a busy lock past 2 h alerts from the skipped run; a failed send writes `last_alert_error`, `alert_failures` and a stderr line.

### Supervisor (masterrig cron wrapper) — `tests/redteam/test_supervise_redteam.py`

- `tracker/supervise.py:109-121` (`test_disk_full_run_log_does_not_crash_the_supervisor`). Full disk: the run log's flush in `finally: out.close()` raises ENOSPC out of `supervise()`. The job never runs, no state, no alert, every hour. Fix: wrap the log writes; on OSError fall back to `stdout=DEVNULL` and record "run log unwritable" as the failure reason.
- `tracker/supervise.py:88-91` (`test_unwritable_state_still_alerts`). An unwritable state file (full disk, read-only mount) raises before `decide_alert`, and `consecutive_failures` is never persisted, so the 3-in-a-row threshold is unreachable. Fix: catch OSError in `write_state`; when state cannot be saved, send the alert at once with that reason.
- `tracker/supervise.py:189` (`test_crashing_health_check_still_alerts`). Any exception from a health check kills `supervise()` before `decide_alert`. Fix: wrap each check in `health.run_checks` with `except Exception` and return `"<name>: check crashed: <exc>"` as its failure.
- `tracker/supervise.py:164-168` and `:114` (`test_run_hung_for_hours_is_alerted`). A held lock means exit 0 "skipped" before any health check, and the command has no timeout. A `git push` stalled on a half-open connection holds the lock for good: every later run is silent, and health's own `lock` rule (over 2 h) is never evaluated by the code that alerts. Fix: `subprocess.run(..., timeout=...)` (for example 45 min) in `run_with_retry`; on a busy lock, run `check_lock` and alert through the normal incident path when it fails.
- `tracker/supervise.py:142-146` (`test_failed_alert_send_is_recorded`). A False from the sender leaves no trace: nothing on stderr, nothing in state. A dead pihome bridge means every incident is retried in silence for ever. Fix: write `last_alert_error` and a stderr line; after N failed sends, fall back to `tracker.alert` (email).
