# Deployment on gs

Everything unattended runs on the `gs` host (ssh alias) under the `jonathan`
user, from a checkout of this repo at `~/claude-usage-tracker` on `main` (the default
branch since 2026-09-08; code changes arrive by pull request, while the cron jobs
commit probe rows and the publisher's price write-back straight to `main`). masterrig runs only the passive join.

## Crontab on gs

```
0 0,12 * * *  /home/jonathan/claude-usage-tracker/bin/probe.sh >> /home/jonathan/.paperclip/ops/claude-usage-probe.log 2>&1
0 6 * * 0     /home/jonathan/claude-usage-tracker/bin/output-probe.sh >> /home/jonathan/.paperclip/ops/claude-usage-output-probe.log 2>&1
30 5 * * *    /home/jonathan/claude-usage-tracker/bin/daily.sh >> /home/jonathan/.paperclip/ops/claude-usage-daily.log 2>&1
```

Installed on gs 2026-09-06 (cron switchover, map ticket #6); the old 03:30/15:30 Sonnet-only slot is retired.

Times are UTC. `probe.sh` is one rotation slot: `tracker/rotate.py` picks
the model (Sonnet 5, Opus 5, Fable 5.1 in turn, from the last prose row in
`history/probes.jsonl`) and its expectation (the last median dollar value
per 1% through the API-list invariant, see `docs/spike-2026-09.md`), runs a
3-tick probe, checks the new row for drift against the median of that
model's last four prose rows (more than 15% away), and on drift reruns once
with 2 ticks and decides: rerun agrees with the median, the first row is an
outlier and is flagged in place (`"outlier": true`, ignored by the publisher
and the rotation); rerun agrees with the first row, a change; neither,
inconclusive. Each row is committed and pushed to `main` as it lands. The
cron line above still runs it weekly; the 00:00 and 12:00 rotation cadence
is the next switchover. `python3 -m tracker.rotate plan` is the dry run: the
flags every model would be probed with and which is next.
`daily.sh` pulls `main` (which also brings `history/passive.json` pushed
from masterrig, whose checkout must likewise sit on `main`: `bin/passive.sh`
commits to whatever branch that checkout has out), runs `tracker.publish`, and commits
`website/public/data/claude-usage.json` into the site checkout at
`~/all-done-sites-platform` on `main`, then pushes. Cloudflare Pages builds
from that push.

## Weekly output class weight

`output-probe.sh` runs Sunday 06:00 UTC: a 5-tick Fable 5.1 probe with
`--payload output`, appended to `history/probes.jsonl` tagged
`"payload": "output"`. It measures how hard the meter charges output tokens
against their list price, not the limit, so the publisher keeps output rows
out of every rate series (`tracker/rows.py`). Monday's `daily.sh` then has
`tracker.publish` recompute `class_weight.output` from that row against the
latest Fable prose row (`tracker/weight.py`), write it to every model in
`data/prices.json` with the pair recorded under `_output_weight`, and commit
and push `data/prices.json` to `main` before publishing the JSON. A weight
more than 30% from the current one is not applied: the refusal is recorded
(so it alerts once, not daily) and Jonathan gets one email through
`tracker/alert.py`. To accept a refused value, edit `class_weight.output` on
every model in `data/prices.json` by hand and commit.

The run costs about 5% of Dave's 5-hour window (9 prompts of a 4,000-word
reply over 5 ticks on 2026-09-06) and about 0.5% of the 7-day window. It
takes the same `.cron.lock` as the other jobs, so it never overlaps a
rotation probe; the 06:00 slot sits between the 00:00 and 12:00 rotation
runs, and a run that starts within 20 minutes of a window reset waits for it.

Output probe exit codes are `probe.sh`'s: 0 ok, 3 no idle account, 4 aborted,
5 lock held. Exits 3 and 4 alert Jonathan; the weight then keeps its current
value until the next Sunday.

## Contributed meter samples

`daily.sh` runs `tracker.contributed` before `tracker.publish`, inside the
same `.cron.lock`: it pulls every stored sample from
`https://alldonesites.com/api/contribute/export` (bearer secret
`NOTIFY_SEND_SECRET` from `~/.claude-usage-notify.env`, the same one the
notify step uses; cursor-paginated, 20 s per page, no retries), appends the
new rows to `history/contributed.jsonl` keyed by `(contributor_id, ts)`
(append-only, nothing already there is rewritten or dropped), and writes the
per-plan `contributed` block to `data/contributed.json`. `tracker.publish
--contributed data/contributed.json` carries that file into the public JSON
unchanged, beside the probe and passive figures; nothing reads it back into
them. Both files go into the daily "publisher state" commit on `main` with
`data/prices.json`. A failed fetch keeps the rows already on disk and logs
one warning; a failed step is a warning and the publish runs with whatever
`data/contributed.json` already holds. To rebuild the block from disk without
touching the site:

```
cd ~/claude-usage-tracker && python3 -m tracker.contributed --offline --history history/contributed.jsonl --out data/contributed.json
```

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

The probe tries Jono Work (`jwork`, `~/.claude-javiswork`) first and Dave
(`~/.claude-dave`) second. An account with a `claude` session on this host that
is not idle (per its `sessions/<pid>.json`) is skipped before its meter is read
(the probe log says which pid), then the meter must hold flat for 120 s. A
session sitting at the prompt does not block the account. What is compared is
the session state between one reading and the next, not the status at the moment
of sampling: the probe aborts if a session is mid-turn, if its `statusUpdatedAt`
moved since the previous reading (a whole turn that came and went in between),
or if one started or exited. That applies across the 120 s meter window as well
as after every reading during the probe. Both are Max 20x and monitor-owned:
never run `/logout` in either directory. Jonathan's own account is never probed.
`claude` lives at `~/.npm-global/bin/claude`, which the wrappers add to `PATH`
because cron does not.

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
`bin/probe.sh` and `bin/output-probe.sh` raise one when the probe exits 3 or
4, `bin/daily.sh` when a new `last_change` is announced, the weekly weight
guard (`tracker/weight.py`, inside `tracker.publish`) when it refuses a
recomputed output class weight, and `bin/probe.sh` after a drift rerun
(outlier, change confirmed, or inconclusive; and a rerun that wrote no row),
all through the same helper. Config
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

A run's total token count is not a stable measure of the task: `claude -p`
sometimes answers in two API turns, and the second re-reads the whole prefix
(Sonnet low, 7 repeats, 2026-09-09):

```
input 2, output 1610, cache_read 19795, cache_write 0     total 21407   one turn
input 4, output 1410, cache_read 39590, cache_write 877   total 41881   two turns
```

So the totals of a cell split into two clusters and their median is a coin
toss. Each run is instead valued in meter dollars the way the probe values a
tick (per class, tokens x list price x class_weight, then x meter_weight from
`data/prices.json`), where the two runs above are close, and the cell
publishes the median over all its runs. The matrix cell keeps the median
total tokens for the page; a top-level `usd` block carries the dollar
median, `_meta.cell_rule` states the rule, and `_meta.cells` records
`median_tokens`, `median_usd`, `runs`, `turns` (estimated as `input // 2`)
and `spread_usd` per cell. The publisher passes `usd` through as
`effort_usd`. To re-derive a matrix measured before this existed, without
sending anything:

```
ssh gs 'cd ~/claude-usage-tracker && python3 -m tracker.calibrate --recompute data/effort_matrix.json'
```

It prices runs from `--prices` (default `data/prices.json`), rewrites the
file in place (`--out` to write elsewhere) and stamps `_meta.recomputed_from`.
