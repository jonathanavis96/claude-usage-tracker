# Deployment on gs

Everything unattended runs on the `gs` host (ssh alias) under the `jonathan`
user, from a checkout of this repo at `~/claude-usage-tracker` on the `build`
branch. masterrig runs only the passive join.

## Crontab on gs

```
5 4 * * 1     /home/jonathan/claude-usage-tracker/bin/probe.sh >> /home/jonathan/.paperclip/ops/claude-usage-probe.log 2>&1
30 5 * * *    /home/jonathan/claude-usage-tracker/bin/daily.sh >> /home/jonathan/.paperclip/ops/claude-usage-daily.log 2>&1
```

Times are UTC. `probe.sh` runs weekly and always probes Sonnet 5; every other
model's rate is derived from that one model's dollar value (the API-list
invariant, see `docs/spike-2026-09.md`) rather than probed directly. It
appends to `history/probes.jsonl`, committing and pushing to `build`.
`daily.sh` pulls `build` (which also brings `history/passive.json` pushed
from masterrig), runs `tracker.publish`, and commits
`website/public/data/claude-usage.json` into the site checkout at
`~/all-done-sites-platform` on `main`, then pushes. Cloudflare Pages builds
from that push.

## Crontab on masterrig

```
15 3 * * *  /home/grafe/code/claude-usage-tracker/bin/passive.sh >> /home/grafe/.paperclip/ops/claude-usage-passive.log 2>&1
```

## Keys and aliases on gs

`~/.ssh/config` has two host aliases, each with its own deploy key:

| Alias | Repo | Key | Access |
|---|---|---|---|
| `github-cut` | `jonathanavis96/claude-usage-tracker` | `~/.ssh/claude-usage-tracker` | write |
| `github-cut-site` | `jonathanavis96/all-done-sites-platform` | `~/.ssh/claude-usage-site` | write |

The tracker checkout's `origin` uses `git@github-cut:`. The gs `gh` login is
a different account with no access to either repo; never use it for these.

## Accounts

The probe runs on the Dave account (`~/.claude-dave`) and falls back to Jono
Work (`~/.claude-javiswork`). Both are Max 20x and monitor-owned: never run
`/logout` in either directory. Jonathan's own account is never probed. `claude`
lives at `~/.npm-global/bin/claude`, which the wrappers add to `PATH` because
cron does not.

## Checking it

```
ssh gs 'tail -20 ~/.paperclip/ops/claude-usage-probe.log ~/.paperclip/ops/claude-usage-daily.log'
ssh gs 'tail -3 ~/claude-usage-tracker/history/probes.jsonl'
```

Probe exit codes: 0 ok, 3 no idle account within the wait, 4 aborted (window
reset or a jump the probe cannot explain), 5 another tracker job held the
checkout's .cron.lock. A run costs about 1% of the account's 5-hour
window.

Daily publisher exit codes: 0 ok, 1 publish refused or failed (previous JSON
left in place), 6 the tracker lock was still held after waiting 600s.

## Alerts to Jonathan

`tracker/alert.py` sends one email to Jonathan through the site's
`/api/notify/send` endpoint (its admin `to` mode, bearer-secret guarded).
`bin/probe.sh` raises one when the probe exits 3 or 4, `bin/daily.sh` when a
new `last_change` is announced, and the rotation wrapper and weekly weight
guard raise theirs (outlier, refused weight) through the same helper. Config
is `~/.claude-usage-notify.env` on gs (mode 600, never printed):

```
NOTIFY_SEND_SECRET=...        # already there: the send endpoint's bearer secret
NOTIFY_ALERT_TO=...           # Jonathan's inbox; alerts are skipped until it is set
```

A test alert from gs:

```
cd ~/claude-usage-tracker && python3 -m tracker.alert --subject "Test alert" --text "Hello from gs."
```

It prints `alert: sent ...` on a 2xx, `alert: skipped, ...` when the env file
lacks a key (exit 0), and `warning: alert ... refused` on a non-2xx (exit 1).
The wrappers append `|| true`, so an alert can never fail the job that raised it.

## One-off: effort calibration

```
ssh gs 'cd ~/claude-usage-tracker && PATH=$HOME/.npm-global/bin:$PATH python3 -m tracker.calibrate --config-dir $HOME/.claude-dave --out data/effort_matrix.json'
```

Re-run only when a model is added or effort semantics change; commit the
resulting `data/effort_matrix.json`.
