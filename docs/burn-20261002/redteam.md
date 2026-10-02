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
- `tracker/supervise.py:138-151` (`test_new_fault_during_open_incident_is_alerted`). While an incident is open, nothing else is sent, whatever the new reason. One lasting health line (a token idle past its grace, or the stale-HEAD publisher reading below) swallows every later, different fault, including three failed runs in a row. Fix: keep the set of open reasons in state, and send one more message when a new reason joins it.

**Status (UT-I): all fixed, markers removed.** Health: freshness comes from the newest line with a utilization (`check_collection`, `check_meter` = `check_meters`); a non-object `claudeAiOauth` and an overflowing pid file are reported, not raised. `bin/passive.sh` exits 5 when the commit or the push fails, so the supervisor retries and alerts.

### Health check — `tests/redteam/test_health_redteam.py`

- `tracker/health.py:188-212` (`test_meter_log_of_only_auth_errors_is_unhealthy`). `check_meter` judges freshness by the newest line of any kind. One account with a revoked token writes a fresh `auth_expired` line every minute (`meter_log.py:314`; error lines skip the 110 s spacing too), so the check passes while the account has had no reading for hours. The units count exit 4 as success (`SuccessExitStatus=0 4`) and nothing on gs runs this check anyway; the only sign is the page flipping that account to `stopped` after 6 h. Fix: take freshness from the newest line with a `five_hour.utilization`, and schedule `tracker.health --meter-log` on gs. Caution: an access token also lapses on an account nobody has used for about 8 h (only Claude Code refreshes it), so `auth_expired` alone cannot tell "revoked" from "idle". Alert on hours without a reading (for example 24 h), and publish an `auth_expired` feed state on the page rather than plain `stopped`.
- `tracker/health.py:102-112` (`test_collection_error_row_is_not_a_fresh_sample`). Same flaw in `check_collection`: an error row with a `ts` counts as a fresh sample. Fix: as above.
- `tracker/health.py:153-156` (`test_token_check_survives_unexpected_credentials_shape`). A non-object `claudeAiOauth` raises AttributeError outside the try; the CLI crashes, and under supervise this is the crash above. Fix: `if not isinstance(oauth, dict): return "token: ... unexpected shape"`.
- `tracker/health.py:231`, `:217` (`test_lock_check_survives_corrupt_pidfile`). A corrupt all-digit pid file overflows `os.kill`; uncaught. Low likelihood. Fix: catch `OverflowError`/`ValueError` in `pid_alive` and report the pid file as stale.

### masterrig publisher (`bin/passive.sh`) — `tests/redteam/test_passive_sh_redteam.py`

- `tracker/health.py:137-148` with `bin/passive.sh:42-48` (`test_publisher_check_reads_what_was_fetched`). `check_publisher` runs `git log` on the checkout's HEAD, but passive.sh moves HEAD only on a full run, about once in 20 h. Its hourly early exit only fetches. So 2 h after each full run, health reports gs's publisher stale while `origin/main` holds a commit minutes old. Under supervise that is one false "needs attention" a day, and while that incident is open no other alert goes out, so a real failure in those hours is hidden. This is new on the integrate branch and not deployed yet. Fix: `git log -1 --format=%ct origin/main -- history/gs-passive.json` after a `git fetch` (the early exit already fetches).

- `bin/passive.sh:78-83` (`test_unpushed_record_is_not_reported_as_success[push_rejected]`, `[origin_unreachable]`). A push rejected after the rebase retry (branch protection, a non-fast-forward the rebase cannot fix, a revoked deploy key) or a network that is down ends in a warning and exit 0. supervise records success, health stays green, no alert; masterrig's record stops reaching main and the page shows a1 `stopped` only after 36 h. Fix: `exit 5` after the warning (supervise then retries and alerts at 3 in a row); or treat "stamp older than 26 h" as a health failure.
- `bin/passive.sh:77` (`test_failed_commit_is_not_reported_as_success`). `git commit ... || exit 0` turns a failed commit (stale `.git/index.lock`, full disk, a hook) into success, every hour, for good. Fix: `|| { say "commit failed"; exit 5; }`.

### gs publisher (`bin/daily.sh`) — `tests/redteam/test_daily_sh_redteam.py`

- **Fixed (UT-I):** `bin/daily.sh` writes and pushes `history/site-push.json` on every failed site push and on the first success after one; `check_publisher` fails while it says `"ok": false`. Marker removed.
- `tracker/health.py:137-148` with `bin/daily.sh:157-174` (`test_rejected_site_push_is_seen_by_health`). The only publisher check is the age of the last tracker-repo commit to `history/gs-passive.json`. daily.sh commits and pushes that before the site push. When the site push fails on every run (revoked `github-cut-site` key, branch protection, a divergence the rebase cannot fix), the tracker side stays fresh, health stays green and the public page freezes. gs is not under supervise, so daily.sh's exit 1 reaches only its cron log. **Highest-impact silent failure found.** Fix: check what readers see. Read `generated_at` from the site repo's `website/public/data/claude-usage.json` at `origin/main` (masterrig can `git ls-remote`/fetch it) or from the live URL, and fail over 2 h. Also run daily.sh under `tracker.supervise --label gs`.

**Status (UT-I): cron line and schema fixed, markers removed.** Both installers append the supervisor's own output to `<log>.supervise`. `meter_log` marks a line with `schema` when a bucket or key is missing (stderr warning too), keeps every other top-level bucket as sent, and the `meters` health check fails on a `schema` reading.

### Cron line — `tests/redteam/test_cron_line_redteam.py`

- `deploy/install-schedule.sh:127` (`test_supervisor_crash_leaves_a_trace`). The cron line ends in `>/dev/null 2>&1`. The job's own output goes to `--log`, but everything `tracker.supervise` prints itself ("skipped: previous run still going", and every traceback from the supervisor crashes above) is thrown away, and cron mail never fires. Fix: `>> $LOG.supervise 2>&1` (or into `$LOG`), and have supervise stamp its own lines. Ran with a cron-like env (HOME and PATH=/usr/bin:/bin only): the absolute `/usr/bin/python3` and `cd $REPO` resolve, so the minimal PATH itself is not a fault.

### Usage API schema change — `tests/redteam/test_schema_redteam.py`

- `tracker/usage_api.py:26-29` (`test_renamed_resets_at_is_noticed`). A renamed `resets_at` is read as null and logged at exit 0. Null is also what an idle window legitimately returns (889 of 4,714 moonlighter rows), so nothing can tell. Every new gs stretch becomes `reset_verified: false`, `tracker/publish.py:258` drops it, and the dollar series freezes on old evidence. Fix: in `_bucket`, warn (stderr plus a `schema` field on the line) when the key is absent, or when `utilization > 0` with a null `resets_at`. Have health fail on `schema` lines.
- `tracker/usage_api.py:26-29` (`test_missing_seven_day_bucket_is_noticed`). A renamed or moved weekly bucket gives `seven_day=None` at exit 0. `tracker/join.py:325` then skips every pair, and weekly windows and weekly change detection end silently. Fix: as above. A body with no `seven_day` key is a schema change, not a reading.
- `tracker/meter_log.py:105-108` (`test_new_bucket_is_kept_in_the_log`). `sample_line` keeps only `five_hour` and `seven_day`. `seven_day_sonnet` today, and a Fable weekly or a `limits[]` list tomorrow, are dropped at the source and cannot be recovered later. moonlighter keeps the whole body. Fix: store the raw body's other top-level keys under `extra`.
- Control that passes: null five-hour utilization is already a gap (exit 4), never a zero.

**Status (UT-I): fixed, markers removed.** `merge_samples` prefers the reading with a reset id in a shared minute; `_is_reset` splits when the later reading is past the earlier one's `resets_at` (+5 s). `tests/test_masterrig_passive.py` asserted the old, opposite behaviour and was corrected. These two change masterrig's published numbers; measured in INTEGRATION.md.

### Reset boundaries on masterrig's merged meter — `tests/redteam/test_reset_boundary_redteam.py`

- `tracker/samples.py:20` (`test_merge_keeps_the_reset_bearing_reading`). `_PRIORITY` makes the reset-less ceiling reading win a shared minute over moonlighter's, the opposite of `tracker/gs_passive.py:142` ("reset-bearing sources winning"). On masterrig's real logs, 281 reset-bearing readings are dropped. Fix: `"moonlighter": 0, "ceiling": 1`, or prefer whichever sample has `resets_at`.
- `tracker/join.py:59-62` (`test_known_reset_between_readings_splits_the_interval`). `same_reset` returns True when either side lacks `resets_at`, so with 69% of masterrig's series coming from the ceiling log, a reset between readings is detected only by a drop. A moonlighter reading saying "ends 10:00" followed by a higher ceiling reading at 10:03 is paired across the reset. Measured on the real logs: at most 17 straddling pairs with prior use between 2026-06-13 and 2026-10-02, mostly flat, so the effect on the numbers is small. Fix: in `_is_reset`, treat `a.resets_at <= b.ts` (parsed, with the 60 s tolerance) as a reset, and carry each moonlighter `resets_at` onto the ceiling readings inside its window before the join.
- Checked and holding: `resets_at` jitter. On 3,305 consecutive same-window moonlighter pairs the largest jitter is 1.9 s (one 599 s shift splits a window, which loses a pair but does not mis-attribute one). UTC `resets_at` against SAST `ts` is fine: every stamp is offset-aware and compared as an instant, and day buckets use UTC (`join.py:97`).


**Status (UT-I): incident masking fixed.** `decide_alert` tracks the incident's kinds (`runs`, or the failing check's name); a new kind during an open incident sends one further message. Marker removed.
