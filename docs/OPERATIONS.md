# Operations

What runs, where, how it heals itself, and what each alert means.

## What runs

| Host | Job | Interval | What it does |
|---|---|---|---|
| masterrig | cron `15 * * * *` → `tracker.supervise` → `bin/passive.sh` | hourly; real work every 20h or when `tracker/` changes on main | passive join, writes `history/passive.json`, `masterrig-passive.json`, `masterrig-speed.json`, commits and pushes "Passive history" |
| masterrig | moonlighter `gate.py` (not this repo) | 30 min | samples the usage meter into `~/.moonlighter/usage_log.jsonl`, the join's meter input |
| gs | cron `0,30 * * * *` → `tracker.supervise --profile gs` → `bin/daily.sh` | 30 min | merges history, runs the four-account detector on the day's first run (`tracker.four_accounts`), runs the publish test gate, publishes the site JSON, commits "Daily publisher state" (`history/gs-passive.json`, `history/four-accounts.json`) |
| gs | `claude-usage-meter-{avis,dave,jwork}.timer` (`deploy/systemd/`) | 1 min | one meter sample per account into `~/.paperclip/ops/claude-usage-meter-*.log`; exit 4 = logged gap, next tick retries; a 429 backs off by `retry_after_s` |

Install or update the masterrig schedule with `deploy/install-schedule.sh` (`--dry-run` first). It is idempotent and replaces the old bare `bin/passive.sh` cron line.

On gs, run `deploy/install-gs.sh --dry-run`, then `deploy/install-gs.sh`, from `~/claude-usage-tracker`. It replaces the bare `bin/daily.sh` line with the supervised one and leaves every other line alone. The meter timers are not changed: the supervised publisher run checks their logs every 30 minutes (health check `meters`).

Both installers refuse (exit 3) on a checkout that is dirty, off main or predates the supervisor, when `crontab -l` fails for any reason other than "no crontab for <user>", or when the merged crontab would drop any line other than the tracker's own, print the exact crontab diff on `--dry-run`, back the crontab up to `~/.local/state/claude-usage-tracker/deploy-backups/` before changing it (`deploy/rollback.sh <dir>` restores it), and end with a post-deploy check. `scripts/deploy.sh` runs both from masterrig. The details are in `deploy/lib.sh`.

## How it heals

- **Overlap:** `tracker.supervise` takes an exclusive non-blocking `flock` on `.supervise.lock`. A run that finds it held exits 0 and does nothing. The kernel releases the lock when its holder dies, so it cannot go stale; a leftover `.supervise.pid` whose process is dead is removed before the next run.
- **Transient failure:** a non-zero exit is retried twice, after 30 s and 60 s. Each attempt is a fresh process, so it re-reads `~/.claude/.credentials.json` and the checkout.
- **Missed runs:** `bin/passive.sh` makes up a slept-through day on the next hourly tick (its own 20h stamp).
- **Bounded growth:** the run log rotates at 5 MiB, keeping `.1`–`.3`. The health check fails if any log passes 5 MiB or `history/` passes 200 MiB. History is never pruned automatically: every row is an input to the published figure, and dropping rows would change the numbers.

## Health check

`python3 -m tracker.health` (on gs: `python3 -m tracker.health --profile gs`) prints `ok` and exits 0, or prints the first failure on one line and exits 1. `--all` lists every check.

| Line starts with | Meaning | Usual fix |
|---|---|---|
| `collection:` | newest meter sample older than 1 h | moonlighter's gate is not running, or the meter read is failing |
| `run:` | no successful supervised passive run in 2 h | read the tail of `~/.paperclip/ops/claude-usage-passive.log` |
| `publisher:` | `history/gs-passive.json` not committed in 2 h | gs `daily.sh` is failing or gs is down |
| `token:` | credentials unreadable, signed out, or access token expired more than 12 h ago, or refresh token expired | `claude /login` on masterrig |
| `lock:` | dead pid file, or a run holding the lock over 2 h | a hung join; kill it, the next run clears the pid file |
| `sizes:` | a log or `history/` over its bound | look for a runaway writer |
| `meters:` (gs; masterrig for a1) | a `claude-usage-meter-*.log` has had no usable reading (a line with `five_hour`, not a logged 429 gap) for 15 min plus any Retry-After it is honouring, or is unreadable. Every failing log is named, joined with " \| ", and each account is its own incident. An account whose reads are all `auth_expired` while its refresh token is valid is idle and passes for 24 h ("token lapsed (idle)" after that) | `systemctl --user status claude-usage-meter-<account>.timer`; "needs a login" means the refresh token expired; "token lapsed (idle)" means use that account once so Claude Code refreshes it |

On gs, `run:` means the supervised `bin/daily.sh` has not succeeded in an hour (its state is `~/.paperclip/ops/claude-usage-daily-state.json`, its log `~/.paperclip/ops/claude-usage-daily.log`). gs has no `collection`, `publisher` or `token` check: there is no moonlighter log there, the publisher is the job itself, and the meter check covers each account's sign-in.

## Alerts

Sent to Jonathan on WhatsApp through pihome's `wa_send.py`, to the number in `~/.config/claude-usage-tracker/whatsapp-to` (one line, digits, outside the repo; no file means no WhatsApp); when that fails the same text goes by email through `tracker/alert.py` (`~/.claude-usage-notify.env`). gs has no ssh route to pihome, so gs alerts always arrive by email. The label in the message (`masterrig`, `gs`) says which host. At most two messages per incident:

- **"needs attention: N runs in a row failed (exit X after 3 attempts)"**: three hourly runs failed even after retries. Something persistent is broken: git push rejected, a join crashing, disk full.
- **"needs attention: <health line>"**: a health check has failed continuously for an hour. The line is the one from the table above.
- **"recovered after N min: ..."**: the incident has closed (a run succeeded and health is clean). Nothing further is needed.

- **"(gs publisher) test gate failed: ... Published anyway"**: the tests that read the data `bin/daily.sh` is about to commit (`tracker/publish_gate.py`, about 25 s, limit 60 s) failed. The publish still went out, so the page keeps updating; usually a new price row or a data shape the code does not handle yet. Run the command in the message on gs. **"test gate passes again"** closes it. Its state is `~/.paperclip/ops/claude-usage-publish-gate.json`.

A failed send leaves the incident unopened, so the next hourly run tries again. Set `CUT_ALERT_DRY_RUN=1` or pass `--dry-run` to print alerts instead of sending them. State lives in `.supervise-state.json` in the checkout (gitignored).

## Dead-man switch (Uptime Kuma)

If cron, the host or the supervisor itself stops, nothing above can run to raise an alert. So after every run that exits 0 with health clean, `tracker.supervise` GETs an Uptime Kuma push URL, and Kuma raises the alert when the pings stop. The URL is read from `~/.config/claude-usage-tracker/kuma-push-url` on each host (outside the repo; never commit it, it carries the push token). No file means no ping and no error; a failed ping is recorded as `last_ping` in the state file and never fails the run.

One-time setup, once per host:

1. In Uptime Kuma, **Add New Monitor** → type **Push**. Name it `Claude usage tracker (masterrig)` or `(gs)`.
2. **Heartbeat Interval** must exceed the job interval with margin, because a run can be skipped (overlap guard), retried, or slow:
   - masterrig runs hourly at :15, but masterrig is a desktop that is off or asleep some nights: its moonlighter log shows gaps of 6 h, 6.5 h, 10.5 h and 12 h in the 30 days to 2026-10-02 (09-22, 09-24, 09-27, 09-28), and reboots on 09-27 and 09-28. A 90-minute window would raise a false DOWN alert on every one of those nights. Set **Heartbeat Interval 43200 s (12 h), Retries 1**: DOWN after about 24 h of silence. That is longer than any observed off period, and the data loses nothing in a night off: `bin/passive.sh` makes the day up at the first hourly run after boot (20-hour rule). A real outage (cron gone, checkout broken, machine never back) still alerts within a day, and a failing run alerts at once through the supervisor (status=down push), not through this timer.
   - gs runs every 30 min, and the publisher plus its test gate take a few minutes: set **2700 s** (45 min).
3. **Retries: 2** on gs, so one missed heartbeat is not an alert: DOWN after about 2.25 h of silence. gs is a server and is always on. masterrig: Retries 1 as above.
   An interval equal to the job interval raises false DOWN alerts: the Airlock Guard push monitor had a 300 s window on a job that pings every 300-330 s and did exactly that on 2026-10-01.
4. Copy the push URL Kuma shows and write it on the host: `mkdir -p ~/.config/claude-usage-tracker && printf '%s\n' '<push URL>' > ~/.config/claude-usage-tracker/kuma-push-url && chmod 600 ~/.config/claude-usage-tracker/kuma-push-url`.
5. Check: after the next scheduled run, `last_ping` is `"ok"` in `.supervise-state.json` (masterrig) or `~/.paperclip/ops/claude-usage-daily-state.json` (gs), and the Kuma monitor is UP.

A ping goes only on a healthy run, so a run that keeps failing also goes silent and Kuma alerts on it too, after its own window; the WhatsApp/email alert normally arrives first.

## Known gaps

- `bin/daily.sh` keeps its own lock (exit 6 after waiting 10 min for a probe); under the supervisor that counts as a failed run.

## Failure handling

- `bin/passive.sh` exits 3 when the checkout is not on `main`. The supervisor does not retry it, records `last_exit: 3` with the reason "checkout is off main", and sends the alert on that first run. `tracker.health` fails its `run` check with "the checkout is off main (exit 3)" until a run succeeds. Fix: `git checkout main` in the live checkout.
- `tracker.meter_log` skips a tick within 110 s of the last call (exit 0, no line), so per-account reads land about 130 s apart. The `meters` check (gs, and a1's log on masterrig) allows 15 min since the newest good reading, well above that, and also fails when more than half of a log's last 40 or more lines are 429s.
- `bin/passive.sh` and `bin/daily.sh` exit 7 when a pull's rebase stops on a conflict: the rebase is aborted, so the checkout is back on `main` and clean, and the supervisor alerts on that first run without retrying. Fix: `git pull --rebase origin main` in that checkout and resolve the conflict by hand. Both exit 8 at start when a rebase or merge is already in progress (`git rebase --abort`). `bin/passive.sh` exits 4 when `tracker/` has uncommitted changes, since the joins would run on that code; the supervisor alerts after three such runs. Both wrappers commit only their own files (`git commit --only`), so anything else staged in the checkout is never pushed.
- `tracker.passive` exits 2 without writing when `history/passive.json` exists but does not parse: an unreadable record is never treated as an absent one. Restore it with `git checkout -- history/passive.json`.
- `tracker.meter_log` honours a logged Retry-After up to 2 h (the endpoint's measured long block is 3,600 s).
- A supervised run that times out is killed with its whole process group.
- `tools.model_rates` (the daily refit in `bin/daily.sh`) exits 1 on a crash, 2 on a usage error, 3 when it holds a refit and 4 when that hold is still standing. A hold means the new fit moved a published rate outside its own interval (or a measured rate fell back to provisional or to no value, or the unit anchor lost its stretches): the new fit is written to `history/model-rates.candidate.json` with the reasons in `_meta.held.moves`, and `history/model-rates.json` stays in force. Exit 3 is a new hold, or one that moves different rates, and is the one to alert on. Exit 4 is the same hold again: on a tick with the same code, masterrig history and rates in force on the same day, it returns at once without refitting; on a later refit that moves the same rates, the candidate is refreshed with that refit's figures. Not holds: a provisional rate that loses its value (it values no stretch, and the page falls back to the family's inferred rate, marked inferred), and a rate whose interval in force is collapsed at zero (wholly under `RATE_FLOOR`, 1e-4x Opus: the log-space fit drove it to zero in every resample, as Haiku 5.5 and the pooled Haiku were on 2026-10-23), unless the refit's own interval measures it away from zero. A rate at zero is never published, final or provisional. Rates in the move sentences carry three significant figures, so a tiny rate reads 8.37e-14x, not 0.000x. To adopt a held fit after reading its moves, on gs in `~/claude-usage-tracker`: `PYTHONPATH=. nice python3 -m tools.model_rates history/masterrig-passive.json --json history/model-rates.json --accept-moves`. The moves accepted are recorded in `_meta.accepted_moves`, and the candidate file is removed.

## Loud failures (2026-10-22)

From 2026-10-13T11:00Z the page stayed on its 13 October JSON for nine days. Blocking invariants withheld it on every run, `bin/daily.sh` exited 0, health said ok and Kuma kept getting "up". Each way the page can go stale now fails something.

**Exit 9: withheld publish.** When `tracker.invariants` exits 2, `bin/daily.sh` puts the last published JSON back and finishes the run: it still commits the tracker state and runs notify. It then exits 9. `tracker.supervise` counts that as a failed run and never retries it, on either profile. The `run` health check fails at once with "bin/daily.sh withheld the public JSON (exit 9)". On gs the incident opens on the second withheld run in a row, about an hour in, and Kuma is pushed "down". The `run` check and the failed runs count as one incident, not two.

**Invariant alerts** (`publish_gate.track_incident`):

- One alert goes out when an incident opens.
- Another, "the public JSON is WITHHELD … withheld since <UTC>", goes out when an already-open incident starts blocking.
- That alert repeats every 24 h while the page stays withheld.
- Nothing is sent when the set of failing checks changes; it changed 19 times between 4 and 22 October.
- A withhold that clears and comes back within 24 h of the last message waits for the reminder.
- The daily log shows what was actually sent, on its `invariants alert:` line. The old "(alerted)" claim is gone.

**New and changed health checks:**

| Line starts with | Host | Meaning |
|---|---|---|
| `page:` | gs | the site checkout's `claude-usage.json` has a `generated_at` older than 2 h, or cannot be read |
| `page:` | masterrig | the live `https://alldonesites.com/data/claude-usage.json` is older than 2 h. "cannot fetch … for Nh" is a separate incident, raised only after 2 h of failed fetches in a row. The time of the first failure is kept in `~/.cache/claude-usage-tracker/page-fetch.json`. |
| `advisory:` | gs | one of `tracker.list_prices`, `tracker.contributed`, `tracker.gs_passive`, `tools.model_rates` or `tracker.four_accounts` has failed on every run for 3 h. Each step is its own incident. Outcomes are recorded in `~/.paperclip/ops/claude-usage-advisory.json` (`tracker/advisory.py`). |
| `meters: <account> is blind` | gs | an account that appears in `history/gs-passive.json` has had no meter reading for 1 h. This applies even when the cause is an idle token lapse, a held header probe or an honoured Retry-After, because use from another host goes unmeasured during the gap. |

The supervisor reads every failing check (`health.all_failures`), not only the first, and each kind (`advisory/<step>`, `meters/<account>`, `page/age`, …) keeps its own clock in the state file's `kinds_since`. A kind is alerted once it has failed for `--stale-after`, as a "further problem" inside an open incident, and named as recovered when it clears while something else still fails. A later check is no longer hidden behind an earlier one: with only the first failure, `advisory` never alerted while the page was withheld. `page/age` while the runs fail is the same incident as the runs.

The masterrig profile (`health.masterrig_config`) checks the live page and a1's meter feed, `claude-usage-meter-a1.log`, with `meters` (see "a1 meter on masterrig" below). The ceiling log it replaces stopped on 2026-10-18 and is not checked.

The supervisor alerts on a health failure only after it has lasted `--stale-after` (1 h), so a meter has to go 2 h without a reading before anyone hears about it. On the gs logs from 1 to 22 October there were nine such gaps, about one every 2.4 days. Three already failed under the old checks: the 2 October outage on avis and dave, and jwork's 41.7 h on 19-20 October. The other six are new alerts, all dave's overnight probe hold (3.7-5.4 h). Gaps of 1.1-1.3 h, usually a Retry-After, end before the alert would fire.

**Subscriber email:** a change is emailed only when it is not `provisional` and its `state`, if it has one, is "measured". A change whose state is anything else (measuring, provisional) is not POSTed and not recorded in `.notified-change`. Jonathan gets one "Observed change, not emailed (<state>)" alert per change and state, keyed by a `<date> held:<state>` line in `.notify-attempts`, and the change is emailed if it later reads "measured". A change with no `state` at all is a weekly detector event (the live 2026-09-13 `windows_per_week` event has none; the 2026-09-14 weekly cut was one) and is emailed as before (director's ruling, 2026-10-23). `tests/test_deploy_scripts.py::test_a_weekly_detector_change_with_no_state_is_emailed` pins that.

**Held refit** (`tools.model_rates` exit 3 or 4, from `fix/model-rates-refit`): not a crash. Exit 3, a new hold, sends Jonathan one "Model rates refit held" alert listing `_meta.held.moves` from `history/model-rates.candidate.json` (untracked, in `.gitignore`). Exit 4, the hold already reported, is one log line and no alert. `tracker/advisory.py` records both as runs that worked (`HELD_EXITS`), so a standing hold never opens an `advisory:` incident; exit 1 still does after 3 h.

**Deploy (director, after merge).** No crontab, systemd or key changes. Both crons already run `tracker.supervise` from the live checkouts, so the code takes effect once each checkout pulls main.

1. gs: the next `bin/daily.sh` run pulls main itself. Then check: `ssh gs 'cd ~/claude-usage-tracker && python3 -m tracker.health --profile gs --all; tail -5 ~/.paperclip/ops/claude-usage-daily.log; cat ~/.paperclip/ops/claude-usage-daily-state.json'`. While the page is withheld, expect `last_exit: 9`, a `run:` line about exit 9, `page:` failing on the 13 October JSON, and on the next run an `invariants alert: withheld alert sent` line, because the open incident has no `withheld_alerted_at` yet.
2. masterrig: `bin/passive.sh` pulls main when its 20-hour stamp or new `tracker/` code on main says to run. Check with `cd ~/code/claude-usage-tracker && python3 -m tracker.health --all`. Expect `page: https://alldonesites.com/… was generated …` until gs publishes again.
3. Expect alerts within about an hour of each host running the new code: gs Kuma down, the gs invariants "WITHHELD" email, the gs `advisory: tools.model_rates` further problem while the refit still crashes, and the masterrig page incident. Those alerts are the intended result. They clear when the page publishes and the refit runs clean.

## Capture on every host (2026-10-23)

Each metered account's work is counted wherever it ran, and gs's archived history is read in place.

| Host | What | Where |
|---|---|---|
| masterrig | `bin/ship-extracts.sh`, called by `bin/passive.sh` every hour before its 20-hour gate | writes text-free extracts of every account used on masterrig except its own login (`tracker/extract.py`; jwork, dave and avis today) to `~/.cache/claude-usage-tracker/extracts/masterrig/`, then `rsync -az --mkpath` (never `--delete`) to gs `~/.paperclip/ops/remote-transcripts/masterrig/`, owner-only |
| gs | `tracker.gs_passive` (inside `bin/daily.sh`, no new flags) | reads each account's live `projects/`, the cached archive of that same directory, and every `~/.paperclip/ops/remote-transcripts/<host>/<identity>/` matching the account, deduplicated by message id |
| gs | archive cache | `~/archive/transcripts-*/<subtree>/*.tar.zst` streamed once into `~/.cache/claude-usage-tracker/transcript-archive/` (427 MB for the 2026-10-15 archive, 66 s to build, 1 s to check after); `<subtree>` `claude` is `~/.claude/projects`, `claude-dave` is `~/.claude-dave/projects` |

An extract keeps per-turn timestamps, message and session ids, model, token counts, `entrypoint`, `isSidechain`, `remoteSourced` and the project directory names. No message text, tool input or output, or prompt leaves masterrig, and nothing goes to this repo. A config dir's account is its `oauthAccount` hash (tracker/meter_log.py `account_identity`), the home `~/.claude.json` for a bare `~/.claude`, or for a dir with only a `.oauth-token` (masterrig `~/.claude-dave`, used by `~/bin/dave-agent`) `tok-` plus 12 hex characters of the token's SHA-256. gs matches that fingerprint through the tracker's own meter unit (`--config-dir ~/.claude-dave --token-env GS_DAVE_CLAUDE_OAUTH_TOKEN`); masterrig's dave token fingerprint matches gs dave's account this way. A shipped identity that matches no gs account is never dropped silently: `history/gs-passive.json` counts it under `remote_unjoined` (`extracts`, `files`), and gs_passive's stderr, in the private daily log, names its host and identity. The public record never carries an identity hash, a token fingerprint or another host's name: each account's `transcripts.sources` sums every host's shipment into one `remote` entry with a `hosts` count (`tests/test_gs_passive.py::OtherSourcesTests::test_the_public_record_carries_no_identity_token_fingerprint_or_host_name`).

A session that two accounts' config dirs both reach (masterrig `~/.claude-avis/projects/<a project>` links into `~/.claude/projects`) is split turn by turn: each turn goes to the account with the latest claim before it (session-env and file-history birth times, `sessions/<pid>.json` `startedAt`, `history.jsonl` timestamps). A session launched with `CLAUDE_CODE_OAUTH_TOKEN` injected from outside (Mission Control seats) cannot be told apart from its dir's own login and is counted as the dir's.

### Deploy, once, after the merge

masterrig (cron reaches gs through the `gs` ssh alias, `~/.ssh/greenscape`, with no agent; checked with `SSH_AUTH_SOCK= ssh -o BatchMode=yes gs true`):

```
ssh gs 'mkdir -p ~/.paperclip/ops/remote-transcripts && chmod 700 ~/.paperclip/ops/remote-transcripts'
cd ~/code/claude-usage-tracker && git pull --rebase origin main && bin/ship-extracts.sh
```

The first shipment is every file back to each config dir's start (about 84 MB before rsync compression, 25 s); later ones send only what changed. No cron line changes: the existing `15 * * * *` supervised `bin/passive.sh` calls it.

gs, from `~/claude-usage-tracker` after the next `bin/daily.sh` has pulled main, or by hand before it:

```
nice python3 -m tracker.extract --archive      # builds ~/.cache/claude-usage-tracker/transcript-archive once
```

Skipping this is safe: the first `bin/daily.sh` run builds the cache itself, 66 s longer than usual.

Ship the masterrig extracts first, then let `bin/daily.sh` run: the first gs-passive record written by this code carries `sources_version: 2`, so the turn-count guard below compares nothing on that run and starts guarding from the next.

Do not prune `~/.claude*/projects`, `~/archive/transcripts-*` or `~/.paperclip/ops/remote-transcripts` on gs: they are tracker inputs, and every stretch is rebuilt from them each run.

### Transcript-loss guard

`tracker.gs_passive` (full runs, gs and `--masterrig`) compares each account's transcript turn count with the record it is about to replace. Turns only grow, so a fall of 5% and at least 500 turns is lost transcripts, as on 2026-10-15 when a disk cleanup took jwork from 91,131 turns to 18,209 and the next run rewrote September with no tokens. It then:

- prints one stderr line per account starting `INCIDENT transcript-loss gs_passive:`, naming the fall and every source (`live`, `archive:<dir>/<subtree>`, `remote`) whose turns fell;
- does not write `--out`, and exits 3. `bin/daily.sh` already treats a failed gs_passive as a warning and publishes on the previous `history/gs-passive.json`, so the page keeps the last good history.

It repeats every run until the transcripts are back (restore them, or put the archive under `~/archive/transcripts-*`, which is read in place), or until the loss is accepted on purpose:

```
cd ~/claude-usage-tracker && python3 -m tracker.gs_passive --prices data/prices.json --probes history/probes.jsonl --out history/gs-passive.json --accept-loss
```

A deliberate recount (a change to how transcripts are found or attributed) bumps `SOURCES_VERSION` in `tracker/gs_passive.py`, which skips the comparison for one run.

## a1 meter on masterrig

masterrig's own account (a1, `~/.claude`) is read by `tracker.meter_log`, the sampler gs runs for jwork, dave and avis, from a systemd user timer: `deploy/systemd-masterrig/claude-usage-meter-a1.{service,timer}`, one tick a minute, about 130 s between reads after the sampler's 110 s spacing. It writes `~/.paperclip/ops/claude-usage-meter-a1.log`, which `tracker.gs_passive --masterrig` and `tracker.passive` merge with moonlighter's and the ceiling's logs (`masterrig_account`). The units sit outside `deploy/systemd/`, which `deploy/install-gs.sh` compares with gs's installed units.

Why: `tracker/join.py` pairs no two readings more than `MAX_PAIR_GAP` (15 min) apart, and moonlighter reads every 30 min. The only closer reader was Mission Control's `mis-usage-ceiling.service` (every 5 min, `~/.paperclip/ops/mis-usage-ceiling-systemd.log`). That service asks the Paperclip board API which account the seats are on before it reads the usage endpoint. From 2026-10-18 00:49Z the API answered 401, and later HTTP 0 when Paperclip was down. The ceiling logged `BLIND` and never read the meter again: its last reading is 2026-10-18 00:44:41Z. Paperclip was stopped at 2026-10-20 19:43Z and the ceiling at 19:57Z, and both units are disabled. Moonlighter, on the same login, read on throughout. a1's last stretch ends 2026-10-17 11:14Z. The new sampler asks nothing but the usage endpoint.

Install, once, on masterrig, **before this branch merges**. The sampler's code is already on main, so the timer runs as is. After the merge the masterrig health check watches this log (below), so merging first would fail `meters` until the timer is in, and the supervisor would alert after an hour:

```bash
cd ~/code/claude-usage-tracker
mkdir -p ~/.config/systemd/user ~/.paperclip/ops
install -m 644 deploy/systemd-masterrig/claude-usage-meter-a1.service deploy/systemd-masterrig/claude-usage-meter-a1.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-usage-meter-a1.timer
```

Check, five minutes later:

```bash
systemctl --user list-timers claude-usage-meter-a1.timer --no-pager
journalctl --user -u claude-usage-meter-a1.service -n 5 --no-pager   # "a1: five_hour=N% seven_day=M"
cd ~/code/claude-usage-tracker && python3 -c "
from itertools import pairwise; from pathlib import Path
from tracker.samples import parse_meter_log
s = parse_meter_log(Path.home().joinpath('.paperclip/ops/claude-usage-meter-a1.log').read_text().splitlines())
print(len(s), 'readings; widest gap', max((b.ts - a.ts for a, b in pairwise(s)), default=None))"
```

Expect at least two readings and a widest gap of about two minutes. A refusal (exit 2) means `~/.claude` is signed out, or the log holds another account. `error` lines with `reason: rate_limited` are 429s, and the sampler waits out their Retry-After by itself.

The joins pick the log up on the first `bin/passive.sh` run after the change reaches main, because a change to `tracker/` triggers a rejoin. Stretches form again from the first pair of readings.

Do not re-enable `mis-usage-ceiling.service` for the tracker. It belongs to Mission Control, and the tracker no longer needs it.

Health: the masterrig profile (`tracker.health`, run by the hourly supervisor) checks this log with `meters`, as gs checks its three: no usable reading in 15 min fails, so a stopped timer, a refusal loop or a run of 429s alerts. An idle a1 whose access token lapsed is told from a dead feed by `~/.claude/.credentials.json`'s refresh-token expiry. Check by hand with `python3 -m tracker.health --all`.

Identity: the unit passes `--config-dir %h/.claude`, the default dir. Without `CLAUDE_CONFIG_DIR`, Claude Code keeps its live config in `~/.claude.json`, beside the dir, so the sampler labels a1's lines from that file. `~/.claude/.claude.json` is a stale leftover (on 2026-10-23 last written 2026-10-19, 32 keys against 95). Both named the same account that day, so the label did not change, and a re-login to another account now refuses (exit 2) instead of mixing two meters in one log.
