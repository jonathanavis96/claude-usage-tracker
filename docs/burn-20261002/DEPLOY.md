# Deploy the 2026-10-02 fixes (the morning page)

This is the only page needed to make the fixes live. It replaces the "Deploy steps" list in `INTEGRATION.md`, and the table at the end maps each of those steps to one here.

The fixes do nothing until both hosts run the new code and both crontabs carry the supervised lines. **masterrig** is this WSL box, with its checkout at `~/code/claude-usage-tracker`. **gs** is reached with `ssh gs`, and its checkout is `~/claude-usage-tracker`. No systemd unit changes: the three gs meter units already match `deploy/systemd/`, as checked on 2026-10-02.

Every command below runs on masterrig unless it starts with `ssh gs`.

## 1. Ship
Say "ship it" to run `ship-to-main` on **`burn/20261002-ut-deploy`**. It holds all of `burn/20261002-ut-integrate` plus the hardened installers, so shipping it ships both.

## 2. Pull on both hosts
```
cd ~/code/claude-usage-tracker && git checkout main && git pull --rebase origin main
ssh gs 'cd ~/claude-usage-tracker && flock -w 600 .cron.lock git pull --rebase origin main'
```
On gs, the pull waits on `.cron.lock`, the lock `bin/daily.sh` holds, so it cannot run in the middle of a publish. The pull alone puts the 110 s meter spacing live on gs (it ends the 429 storm) along with the publish gate. No restart is needed: the meter timers start a fresh `python3` every minute.

## 3. Dry run, then review
```
cd ~/code/claude-usage-tracker && scripts/deploy.sh
```
This changes nothing on either host. For each host it prints a report on stderr and the exact crontab diff. Check four things:
- Each host ends with **`RESULT: a real run would proceed`**. A `REFUSE:` line names its cause: not pulled, dirty, off main, or missing the expected commit. Fix that cause and run the dry run again.
- The masterrig diff removes exactly one line, `-15 * * * * …/bin/passive.sh >> …claude-usage-passive.log 2>&1`, and adds one block between `# ===== CLAUDE USAGE TRACKER` markers.
- The gs diff removes exactly one line, `-0,30 * * * * …/bin/daily.sh >> …claude-usage-daily.log 2>&1`, and adds one block between `# ===== CLAUDE USAGE TRACKER GS` markers. The commented probe lines stay.
- No other `-` or `+` lines.

The 2026-10-02 01:16Z dry run produced exactly those two diffs. Both hosts refused only because they had not pulled yet.

## 4. Apply
```
scripts/deploy.sh --apply
```
For each host, the run does the following:
- refuses again if the checkout is wrong;
- backs up the crontab to `~/.local/state/claude-usage-tracker/deploy-backups/<UTC stamp>-<host>/` on that host;
- installs the crontab and reads it back;
- runs the post-deploy check. That is one supervisor run of `/bin/true` with the real health checks and Kuma file, followed by `tracker.health --all`.

Expect `RESULT: installed and checked` for each host. Before the Kuma step, `last_ping=None` is correct.

Two health lines may still fail at this point, and they are expected:
- **`run:`** clears after the first scheduled run (masterrig at :15, gs at :00 and :30).
- **gs `meters: N of the last 40 lines … are 429s`**. At 01:16Z, 23 to 26 of the last 40 lines per account were 429s. The new spacing should push that below half within about half an hour. If it is still failing an hour after the pull, the supervisor alerts, and that alert is real: the storm did not end.

Running it a second time changes nothing and makes no new backup.

## 5. Uptime Kuma monitors (do these right after step 4, not before)
A push monitor that gets no pushes alerts after its window, so create each monitor only when its host is about to ping it. Kuma runs on pihome, port 3001.

| | masterrig | gs |
|---|---|---|
| Monitor type | Push | Push |
| Friendly name | `Claude usage tracker (masterrig)` | `Claude usage tracker (gs)` |
| Heartbeat Interval | **43200 s** (12 h) | **2700 s** (45 min) |
| Retries | **1**: DOWN after about 24 h of silence | **2**: DOWN after about 2.25 h |
| Notification | the one that already reaches your WhatsApp (the same as your other monitors) | same |
| Push URL host | as Kuma shows it (`http://pihome:3001/api/push/…` answers from masterrig) | **change the host to `100.105.0.13:3001`** (pihome's tailnet IP). gs cannot resolve `pihome`. From gs, `http://100.105.0.13:3001/` answered 302 on 2026-10-02. |

The intervals are justified in `docs/OPERATIONS.md` → "Dead-man switch". masterrig is a desktop and is off or asleep some nights.

Write each URL on its host, mode 600. Read it in without echoing, so the token stays out of shell history:
```
mkdir -p ~/.config/claude-usage-tracker && read -rs URL && printf '%s\n' "$URL" > ~/.config/claude-usage-tracker/kuma-push-url && chmod 600 ~/.config/claude-usage-tracker/kuma-push-url; unset URL
ssh -t gs 'mkdir -p ~/.config/claude-usage-tracker && read -rs URL && printf "%s\n" "$URL" > ~/.config/claude-usage-tracker/kuma-push-url && chmod 600 ~/.config/claude-usage-tracker/kuma-push-url'
```
Then run `scripts/deploy.sh --apply` again. Nothing is reinstalled. The post-deploy check sends the first ping on a healthy host. Its report should show `kuma push URL: present … answers 302` and `last_ping='ok'`, and both monitors should turn UP.

If gs is still failing `meters:` at this point, it does not ping yet. The gs monitor then waits about 2.25 h before it goes DOWN, which is enough time for the 429 rate to clear.

## 6. Confirm an alert reaches WhatsApp
The Kuma path is the main one on both hosts. Run this on each host, from its checkout (on gs: `ssh gs 'cd ~/claude-usage-tracker && …'`):
```
python3 -c 'from tracker.supervise import ping_deadman, kuma_url_file as f; print(ping_deadman(f(), status="down", msg="deploy test, ignore"))'
```
The command prints `ok`. Kuma marks the monitor DOWN (it may show PENDING first while retries remain), and the Kuma notification arrives on WhatsApp. Then close the test:
```
python3 -c 'from tracker.supervise import ping_deadman, kuma_url_file as f; print(ping_deadman(f(), status="up", msg="deploy test over"))'
```
masterrig's fallback when Kuma is unreachable is the pihome bridge. The dry run confirmed it is reachable from cron's environment. To test it:
```
cd ~/code/claude-usage-tracker && python3 -c 'from tracker.supervise import whatsapp_sender; print(whatsapp_sender("Claude usage tracker (masterrig): test alert, ignore"))'
```
It prints `True`, and the message arrives.

**Email fallback on masterrig is not configured.** `~/.claude-usage-notify.env` there has `NOTIFY_SEND_SECRET` but no `NOTIFY_ALERT_TO`, so the last fallback cannot send. Add the line `NOTIFY_ALERT_TO=<your address>` to that file. gs already has both keys.

## 7. Verify after the first scheduled runs
```
cd ~/code/claude-usage-tracker && python3 -m tracker.health --all
ssh gs 'cd ~/claude-usage-tracker && python3 -m tracker.health --profile gs --all'
python3 -c 'import json; s=json.load(open(".supervise-state.json")); print(s.get("last_exit"), s.get("last_health"), s.get("last_ping"))'
ssh gs 'python3 -c "import json,os; s=json.load(open(os.path.expanduser(\"~/.paperclip/ops/claude-usage-daily-state.json\"))); print(s.get(\"last_exit\"), s.get(\"last_health\"), s.get(\"last_ping\"))"'
```
Each health command prints `ok` on every line. Each state file shows `0 ok ok`. Both Kuma monitors are UP.

## Rollback
Each install prints its backup directory as `backup: …`. On the host that made it:
```
deploy/rollback.sh <backup dir>            # dry run: shows the diff back to the old crontab
deploy/rollback.sh <backup dir> --apply    # restores it; the crontab it replaces is kept beside the backup
ssh gs 'cd ~/claude-usage-tracker && deploy/rollback.sh <backup dir> --apply'
```
If the crontab has changed since the install, the rollback refuses, and `--force` restores the backup anyway. Pause the Kuma monitor first, because a rolled-back host stops pinging it. The code itself is rolled back with a revert on main, and both hosts pull that within the hour.

## Mapping to INTEGRATION.md "Deploy steps"
| INTEGRATION step | Here |
|---|---|
| 1. Ship `burn/20261002-ut-integrate` | 1. Ship `burn/20261002-ut-deploy` (contains it) |
| 2. gs `git pull`, `deploy/install-gs.sh --dry-run`, then install | 2 (pull under `.cron.lock`), 3 and 4 (`scripts/deploy.sh` runs `install-gs.sh` over ssh) |
| 3. masterrig `git pull`, `deploy/install-schedule.sh --dry-run`, then install | 2, 3 and 4 |
| 4. Kuma push monitors, URL files | 5, with the gs URL host fixed to `100.105.0.13:3001` |
| 5. Confirm health, `last_ping`, Kuma UP | 4 (post-deploy check), 6 (test alert) and 7 |

## What the installers check, and their exit codes
These are documented in `deploy/lib.sh`. Exit codes: 0 done, 3 refused (nothing changed), 4 installed but the post-deploy check failed, 5 the crontab changed between being read and being installed (run it again). Tests: `tests/test_deploy_hardening.py`, `tests/test_install_schedule.py` and `tests/test_install_gs.py`. They use a temporary HOME and a fake `crontab`.
