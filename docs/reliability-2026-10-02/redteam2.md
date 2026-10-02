# Red-team findings, round 2, 2026-10-02 (UT-R2)

Branch `ut-redteam2`, cut from `ut-integrate` at 0402ef3 (its tip at the time).
It adds only `tests/redteam2/` and this file, and fixes nothing: UT-I2 does the fixing.
Every confirmed failure has a strict xfail in `tests/redteam2/` that fails for the stated
reason on this code (checked with `--runxfail`). When a fix lands, its test reports a
strict XPASS; remove the marker then. Run one file at a time:

    timeout 300 nice -n 10 python3 -m pytest tests/redteam2/<file> -q

`tests/redteam2` has no `__init__.py`, like `tests/redteam`, so `unittest discover` skips it.
Its conftest blocks every non-loopback socket and points HOME at a temp dir. The git tests
run the real `bin/passive.sh` and `bin/daily.sh` in throwaway clones, using stub joins and
local bare repos as `origin` (`tests/redteam2/shellrepo.py`). The crontab tests use a fake
`crontab` script over a file. Every usage read is a stub. No live crontab, unit, Kuma
monitor, ssh host, data repo or API was touched. The live masterrig crontab line was only
read, with `crontab -l`.

UT-R's findings are not repeated here. Its angles were the supervisor's own crashes,
health freshness, publisher exits, the cron line's output, schema changes and the
masterrig reset ids.

Result on 0402ef3: 12 xfailed in 7 files. With `--runxfail`, 12 failed, each on the
assertion its message names.

## Confirmed failures

One entry each: where, the test, the impact and a suggested fix.

### Diverged or conflicted data repo — `tests/redteam2/test_data_repo_redteam2.py`

- `bin/passive.sh:57`, `:69` and `bin/daily.sh:39` (`test_conflicting_pull_leaves_no_rebase_in_progress[masterrig_passive_sh]`, `[gs_daily_sh]`). A `git pull --rebase --autostash` that stops on a conflict is reported as a warning and never aborted, so the checkout stays mid-rebase on a detached HEAD. `site_pull` does abort; these two pulls do not. The trigger is a host with a local commit whose push failed, while main has meanwhile changed the same file. On masterrig that file is `history/passive.json`, which the integrate branch's restored days rewrite. On gs it is `data/prices.json`, which price-row PRs change. Follow-up runs measured in the same harness:
  - **masterrig.** The run exits 5, then exits 3 on every later run ("checkout is on 'HEAD'"). The alert says "run `git checkout main`", which does not end a rebase. Every run after that fails on the leftover `rebase-merge`.
  - **gs.** Every run exits 0. The site keeps publishing, but the tracker state commits never reach main ("destination ... not a full refname"), and masterrig's `passive.json` is never pulled again. gs's supervisor sees exit 0, so Kuma stays green. Only masterrig's `publisher` check notices, after 2 h, and only while masterrig is on.

  Fix: on a failed pull, run `git rebase --abort` (`git stash pop` if an autostash is left). At start, detect a leftover `rebase-merge`/`rebase-apply`, exit with its own code and reason, and alert with the real command.
- `tracker/passive.py:87-90` (`test_unreadable_previous_record_keeps_its_days`). An unparseable `history/passive.json` is read as no record. After the conflict above, it holds conflict markers. Every day the transcripts no longer reach is then dropped, and the file is rewritten with only the recounted days. The test turns `{07-29, 07-30, 10-01}` into `{10-02}`. The 2026-07-30 to 08-19 days that UT-I restored are the days at risk. In the harness, `passive.sh` then `git add`s and commits this file on the detached HEAD. Finishing the rebase by hand (`git rebase --continue`) puts the day-stripped file on main. **This is the one finding that loses data.** Fix: when `--out` exists but does not parse, exit non-zero without writing. Never treat "unreadable" like "absent".

### Two runs overlapping — `tests/redteam2/test_overlap_redteam2.py`

- `bin/passive.sh:79` (`test_cron_run_does_not_commit_someone_elses_staged_work`). The masterrig cron runs in `~/code/claude-usage-tracker`, the checkout people and agents work in. The live line was read on 2026-10-02. `git commit` without a pathspec commits the whole index, and `--autostash` keeps staged changes staged. A session that has staged a new file and a change to `tracker/` on main therefore gets both pushed to main of this **public** repo, as "Passive history <date>". The joins also ran on that uncommitted `tracker/` code. Precondition: a full run, which happens about daily or when `tracker/` changes on origin. The supervisor's flock does not cover a person. Fix: `git commit --only -- history/passive.json history/masterrig-passive.json history/masterrig-speed.json`. Before the pull, refuse with its own exit code when the index or `tracker/` is dirty. `bin/daily.sh:166` has the same whole-index commit on gs (read, not tested).

### OAuth token expiry, each of the 3 accounts — `tests/redteam2/test_oauth_redteam2.py`

- `tracker/health.py:295-331` (`test_idle_account_token_lapse_is_not_a_meter_failure[avis]`, `[dave]`, `[jwork]`). meter_log never refreshes a token (usage_api.AuthExpired), so an account nobody uses on gs for about 8 h reads `auth_expired` on every tick until someone uses it again, even though its refresh token is good. In the test it is valid for 20 more days. `meters` fails after 15 min without a usable reading, and the supervisor opens a gs incident an hour later (Kuma down, email). The test's readings are written by the real `meter_log.sample`. The idle stretch is 3 h, and all three accounts fail the same way. INTEGRATION.md says "a weekend away does not alert", which is true only of masterrig's `token` check. The OPERATIONS.md action for `meters` ("a signed-out account's config dir needs a login") is wrong for this case. Fix: treat a log whose recent lines are all `auth_expired` as its own state. Read that account's `refreshTokenExpiresAt` and fail only when the refresh token has expired, or after a long grace such as 24 h. Name it "token lapsed (idle)", not a stale meter.

### Incident masking within one check — `tests/redteam2/test_incident_redteam2.py`

- `tracker/supervise.py:251` (`test_second_account_fault_during_meter_incident_is_alerted`). UT-I's incident kinds are check names. On gs, all three meter logs are the one kind `meters`, and the check names only the first failing log. Suppose dave's meter fails and an incident opens, and then avis's meter stops too. Nothing is sent about avis. When dave is fixed, no recovery is sent either, because `meters` still fails, now on avis. The fix for dave looks as if it did not work, and avis is never named. Combined with the OAuth finding above, one idle account hides a real meter fault on the others for as long as it stays idle. On masterrig, `publisher` covers both a stale gs commit and a failing site push in the same way. Fix: key kinds by check plus subject (the log name, or the publisher sub-reason). Have the meters check return every failing log, not the first.

### Crontab installers — `tests/redteam2/test_install_redteam2.py`

- `deploy/install-schedule.sh:34`, `deploy/install-gs.sh:44` (`test_unreadable_crontab_is_not_replaced[install-schedule.sh]`, `[install-gs.sh]`). `$CRONTAB -l 2>/dev/null || true` reads any failure as an empty crontab. A `crontab -l` that fails for a reason other than "no crontab for <user>" (permission denied, an unreadable spool) installs a crontab holding only the managed block. Both scripts print "schedule installed" and exit 0, and every other job on the host is gone: on gs that includes otto's poller and auto-mail's prune. The likelihood is low, and the impact is the whole host's schedule. Fix: capture the stderr. Treat exit 1 with `no crontab for` as empty, abort on anything else, and back up the old crontab to `~/.paperclip/ops/crontab.<stamp>` before writing.

### Usage API refusing requests — `tests/redteam2/test_rate_limit_redteam2.py`

- `tracker/meter_log.py:222` (`test_measured_one_hour_retry_after_is_honoured`). The chaos fix caps every Retry-After at 1200 s. The endpoint's real long answer is 3,600 s: failure-inventory F9 found 15 of them, and "a run of Retry-After 0 answers escalates to a Retry-After 3600 block". So the sampler calls twice inside the server's block, at about +20 and +41 min. Each call is one the server asked not to get, and the inventory shows the endpoint answering persistence with longer blocks. Gaps of up to 123 min were measured. Fix: honour Retry-After up to a ceiling that covers the measured value, such as 2 h, and keep the cap only against absurd values like 86400.

### Weekly reset moment — `tests/redteam2/test_time_boundary_redteam2.py`

- `tracker/publish.py:1669` (`test_week_is_complete_from_its_reset_instant`). `weekly.py` marks a week complete at its reset instant (`resets_at <= now`). `publish` recomputes `partial` as `week_ending >= now.date()`. A week whose reset is at 16:00Z is therefore published as still open until 00:00Z, up to 24 h on a reset late in the UTC day. The flag is all that is wrong; what the page does with `partial` lives in alldonesites. Fix: carry the reset instant (`_resets_at`) into passive.json's history rows and compare instants.

## Checked and holding (no test needed)

- **Installers run twice, and on the old lines.** On a fake crontab holding the live masterrig line (`15 * * * * /home/grafe/code/claude-usage-tracker/bin/passive.sh >> ... 2>&1`, read with `crontab -l`) and the gs lines documented in `docs/deploy-gs.md` (two-space separator, absolute path), each installer ran twice: the first run printed "schedule installed" and the second "schedule already up to date". Each bare line was replaced by one supervised line, the probe line was kept, and both blocks coexist. `tests/test_install_*.py` cover the same.
- **Both hosts pushing one repo.** masterrig writes only `history/passive.json`, `masterrig-passive.json` and `masterrig-speed.json`. gs writes `data/`, `gs-passive.json`, `model-rates.json`, `contributed.jsonl` and `site-push.json`. No file is shared, so a push race is resolved by the one rebase-and-retry. Only a third writer, a PR, conflicts (the findings above).
- **Two supervised runs, two meter ticks, a hand-run daily.sh.** These are covered by `.supervise.lock`, `<log>.lock` and `.cron.lock` (600 s wait).
- **UTC against SAST, month and year rollover.** Every stamp is parsed to an instant before comparison. `join.daily_rates`, gs_passive's daily pool and `contributed` buckets are UTC. `weekly._week_key`'s +30 s keeps a reset that jitters across UTC midnight in one week. `_iso_week_ending` is ISO-correct across 2026-12-28 to 2027-01-03. `daily.sh` keys notify attempts on `date -u`. `rates_due` compares local with local on one host; a file refitted on masterrig, at SAST, can only trigger one extra refit on gs.
- **Interrupted state writes.** These were checked:
  - `passive.json`, the page JSON, the supervisor state and the gate state go through tmp plus `os.replace`, so a kill leaves the old file.
  - meter logs and `contributed.jsonl` add a newline before appending after a torn line.
  - A truncated `.passive-last-ok` only causes one extra join.
  - A truncated `.weekly-change-seen` only resets the two-publish confirmation, which delays an announcement by one new-evidence publish.
  - An unreadable supervisor state reads as `{}`. That costs one duplicate alert at most, and `last_ok` is rewritten before health reads it.

  The interrupted write that does harm is a host stopping mid-rebase, which leaves the same state as the conflict finding above, and the same fix covers it.

## Read, not turned into tests

- `bin/daily.sh:166`: the same whole-index `git commit` as passive.sh. Less likely on gs, where nobody works in `~/claude-usage-tracker`, but `javis-greenscape` commits exist.
- `tracker/supervise.py:216-219`: the 45-min `timeout` kills bash only, and a hung `git push` grandchild keeps running. On gs that orphan holds daily.sh's fd 9 (`.cron.lock`, opened by `exec 9>` and inherited), so while it lives every later daily.sh waits 600 s and exits 6. That alerts after two runs, with the reason "exit 6 after 1 attempts", which names neither the lock nor the orphan. Fix: run the job in its own process group and kill the group (`start_new_session=True`, `os.killpg`).
- `tracker/gs_passive.py:734`: `passive_dollar_readings(by="day")` keys on `s["end"][:10]`, the stamp's own local date, though its docstring says "per UTC day". masterrig's stretches are `+02:00` (`history/masterrig-passive.json`). gs's are `+00:00`: on the committed `history/gs-passive.json`, none of 252 accepted stretches has a local date that differs from its UTC date. Nothing scheduled passes masterrig stretches through it, so this is latent. Fix: `.astimezone(timezone.utc).date()`, as at line 528.
- `tracker/supervise.py:160`: `write_state` uses a fixed `<state>.tmp`, the pattern `tracker/atomic.py`'s docstring records as a bug. Its only concurrent writer is a skipped run's `_alert_if_hung`, which writes only past 2 h of a held lock.
- `tracker/usage_api.py:121-124`: a 401 whose re-read token changed, followed by a second 401, is raised as a plain HTTPError. meter_log then logs it without `reason`, so it is not spaced, and that account calls every minute until the token works.

## Ranked by impact

1. A conflicted pull is never aborted. masterrig is stuck with the wrong advice, and gs exits 0 while its state stops reaching main (`test_conflicting_pull_leaves_no_rebase_in_progress[masterrig_passive_sh]`, `[gs_daily_sh]`).
2. The passive join drops every kept day when its record does not parse. Finishing that rebase puts it on main and loses the restored days (`test_unreadable_previous_record_keeps_its_days`).
3. The hourly record commit pushes whatever else is staged in the cron checkout to main of a public repo (`test_cron_run_does_not_commit_someone_elses_staged_work`).
4. An idle account's lapsed token opens a gs incident, for each of the 3 accounts (`test_idle_account_token_lapse_is_not_a_meter_failure[avis]`, `[dave]`, `[jwork]`).
5. One `meters` incident hides every other account's meter fault and withholds the first one's recovery (`test_second_account_fault_during_meter_incident_is_alerted`).
6. An installer wipes the host's crontab when `crontab -l` fails. The likelihood is low and the damage is total (`test_unreadable_crontab_is_not_replaced[install-schedule.sh]`, `[install-gs.sh]`).
7. A measured 3,600 s Retry-After is cut to 1,200 s, so the sampler calls twice inside the block (`test_measured_one_hour_retry_after_is_honoured`).
8. A week stays `partial` on the page until UTC midnight after its reset (`test_week_is_complete_from_its_reset_instant`).

## Fixed (all eight, plus four of the read-only items)

Every strict xfail above is now a passing test; `tests/redteam2/` has no xfail left.

1. Conflicted pull: `bin/passive.sh` and `bin/daily.sh` abort a rebase that stopped on a conflict and exit 7 with the checkout back on main and clean; a rebase or merge already in progress exits 8 at start. The supervisor names both and alerts on the first run.
2. Unreadable record: `tracker.passive` exits 2 without writing when `--out` exists but does not parse.
3. Staged work: both wrappers commit with `git commit --only -- <their files>`; `passive.sh` refuses (exit 4) to join on uncommitted `tracker/` code.
4. Idle token: `meters` passes an account whose reads are all `auth_expired` while its refresh token is valid, for 24 h; it fails with "needs a login" once the refresh token has expired. With no readable expiry the plain age rule applies.
5. Incident masking: `meters` reports every failing log; incident kinds are check plus subject (`meters/<account>`, `publisher/commit`, `publisher/site-push`); a partial recovery is reported while Kuma stays down.
6. Crontab wipe: a `crontab -l` failure other than "no crontab for <user>" refuses (exit 3) in every mode before anything is printed or written, and a merge that would drop any non-tracker line refuses too. `deploy/rollback.sh` reads the crontab the same way.
7. Retry-After: `meter_log` honours up to 2 h; `meters` extends its age limit by the Retry-After being honoured.
8. Week partial: weekly rows keep `resets_at`; publish compares instants (date fallback for older rows) and drops the field from the page.

Read-only items also fixed: the supervisor kills the job's process group on timeout; its state file goes through `write_text_atomic`; `passive_dollar_readings` keys UTC days; a second 401 with a re-read token is `AuthExpired`.

