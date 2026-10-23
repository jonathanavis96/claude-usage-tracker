#!/usr/bin/env bash
# Daily on gs: merge probe rows, masterrig's passive history and this host's own
# per-account passive readings into the public JSON, and push it into the
# alldonesites site repo.
#
# Passive is the instrument now (issue #39, 2026-09-16): the probe crontab lines
# are commented out, and tracker.gs_passive's per-account join (dave, jwork --
# both used almost only through this host) is what keeps the published figure
# moving, and since 2026-09-16 it is the published dollar series on its own:
# probe rows never join it (the two read the meter on different scales). The
# probe code and history/probes.jsonl stay in the repo -- publish still reads
# the rows for probed_at, the weekly windows and the class-split fallback --
# nothing here schedules a probe.
#
# history/passive.json arrives in THIS repo via masterrig's own cron pushing
# it here, so pull this repo first. The site checkout is created on first run
# (git@github-cut-site is a second write-only deploy key alias the controller
# sets up) and pulled on later runs. If tracker.publish leaves the JSON
# unchanged, no commit is made in the site repo.
#
# Exit 6 is this wrapper's own: the tracker lock was still held after 10 minutes.
# Exit 9: a blocking invariant withheld the public JSON, so the page kept the last one
# (tracker.supervise counts it a failed run). Until 2026-10-22 that run exited 0, and the
# page sat at 2026-10-13T10:31Z for nine days while every run read as a success.
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1

# One tracker job at a time: a probe and the daily publisher share this checkout.
# The publisher waits for a probe to finish rather than skipping the day.
LOCK="$(cd "$(dirname "$0")/.." && pwd)/.cron.lock"
exec 9>"$LOCK"
if ! flock -w 600 9; then
  echo "daily skipped: could not take $LOCK within 600s" >&2
  exit 6
fi

# One dated line per run: everything below writes undated lines into the cron log.
echo "$(date -u +%FT%TZ) daily.sh: start" >&2

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
# Exit 8: a rebase or merge left in progress (a run killed mid-pull, or by hand). The
# state commits below would land on a detached HEAD and never reach main while the run
# still exits 0.
GD="$(git rev-parse --absolute-git-dir)"
for f in rebase-merge rebase-apply MERGE_HEAD; do
  if [ -e "$GD/$f" ]; then
    echo "error: a $f is in progress in $(pwd): finish or abort it (git rebase --abort / git merge --abort)" >&2
    exit 8
  fi
done
# Exit 7: the pull's rebase stopped on a conflict (a local state commit whose push failed
# against a PR that changed the same file). The rebase is aborted, which also restores an
# autostash, so the checkout is back on main as it was; a person resolves the conflict.
# Any other pull failure (offline) is advisory.
if ! git pull -q --rebase --autostash origin "$BRANCH"; then
  if [ -e "$GD/rebase-merge" ] || [ -e "$GD/rebase-apply" ]; then
    git rebase --abort >/dev/null 2>&1
    echo "error: git pull --rebase failed on a conflict with origin/$BRANCH; rebase aborted, checkout left on" \
         "$BRANCH at $(git rev-parse --short HEAD). Resolve by hand: git pull --rebase origin $BRANCH" >&2
    exit 7
  fi
  echo "warning: git pull --rebase failed, continuing with local state" >&2
fi

SITE="$HOME/all-done-sites-platform"

# The site checkout only ever holds this script's own claude-usage.json commits,
# so they are rebased onto whatever the site repo merged meanwhile (a plain pull
# refuses divergent branches, and every later push is then rejected). A rebase
# that stops is aborted, so the checkout is never left mid-rebase.
site_pull() {
  git -C "$SITE" pull -q --rebase origin main && return 0
  git -C "$SITE" rebase --abort 2>/dev/null
  echo "warning: site repo pull failed, continuing with local state" >&2
  return 1
}

# Push the data commit; a site PR merged since site_pull rejects it, so rebase
# once and retry.
site_push() {
  git -C "$SITE" push -q origin main && return 0
  site_pull && git -C "$SITE" push -q origin main && return 0
  echo "warning: git push to site repo failed, commit made locally only" >&2
  return 1
}

if [ -d "$SITE" ]; then
  site_pull
else
  git clone -q git@github-cut-site:jonathanavis96/all-done-sites-platform.git "$SITE" \
    || { echo "error: could not clone site repo" >&2; exit 1; }
fi

# One email to Jonathan through the same send endpoint, via tracker/alert.py
# (which reads NOTIFY_ALERT_TO and the bearer secret from
# ~/.claude-usage-notify.env, and skips with a note when either is missing).
# Defined before the advisory steps, which use it for a held refit. It can never
# change this script's exit status. The weekly weight guard (tracker/weight.py, run inside
# tracker.publish below) raises its refused-weight alert in-process through the
# same helper's send_alert.
alert_jonathan() {
  python3 -m tracker.alert --subject "$1" --text "$2" || true
}

# The advisory steps below never stop the publish: a failure is one warning line and the
# publish carries on with the step's previous output. Every outcome is also recorded
# (tracker/advisory.py), so a step that fails on every run becomes its own incident after a
# grace period (tracker.health check `advisory`) rather than a warning line forever: from
# 2026-10-13T19:30Z the model_rates refit crashed on every run, 48 a day, and nothing alerted.
# advisory STEP EXIT WHAT-THE-PUBLISH-USES-INSTEAD
advisory() {
  [ "$2" -eq 0 ] || echo "warning: $1 failed with exit $2, $3" >&2
  python3 -m tracker.advisory --step "$1" --exit "$2" || true
}

# List prices for new models (tracker/list_prices.py, issue #130): a model the
# stretch histories carry with no row in data/prices.json gets the row the pricing
# page lists, so its stretches stop being dropped as unpriced. Runs before every
# step below that reads data/prices.json -- gs_passive values its stretches at
# build time -- and the commit further down already picks the file up. Never
# changes an existing row. Advisory: a failed fetch or an unreadable table is one
# warning line and no change.
python3 -m tracker.list_prices \
  --prices data/prices.json \
  --stretches history/gs-passive.json history/masterrig-passive.json
advisory tracker.list_prices $? "publishing with the current price table"

# Contributed meter samples (tracker/contributed.py): pull every stored sample
# from the site's export endpoint (bearer secret from the notify env file, never
# printed), append the new ones to history/contributed.jsonl, and aggregate the
# per-plan block into data/contributed.json for the publish below. Advisory: a
# failed fetch or aggregation is a warning and the publish carries on with
# whatever data/contributed.json already holds (or none).
python3 -m tracker.contributed \
  --env-file "$HOME/.claude-usage-notify.env" \
  --history history/contributed.jsonl \
  --prices data/prices.json \
  --out data/contributed.json
advisory tracker.contributed $? "publishing with the previous contributed block"

# This host's own per-account passive join (issue #39): dave's and jwork's own
# transcripts (sub-agents included) against their own meters. The capture check
# runs and is recorded per stretch but does not gate (CAPTURE_GATE in
# tracker/gs_passive.py); only a stretch the transcripts leave empty is left
# out. Advisory, like tracker.contributed above -- a failed run publishes with
# whatever history/gs-passive.json this repo already has (or none).
python3 -m tracker.gs_passive \
  --prices data/prices.json \
  --probes history/probes.jsonl \
  --out history/gs-passive.json
advisory tracker.gs_passive $? "publishing with the previous gs-passive.json"

# The per-model credit rates (tools/model_rates.py), refitted once a day from
# the passive histories so a new account, a new model family or a provisional rate
# turning final reaches the page with no one running it by hand. About a minute,
# so it runs under nice and only when history/model-rates.json was not already
# generated today (its `generated_at` is local time, so is the date compared), or
# when the fit's code or masterrig's stretch record reached main after it was
# generated: on 2026-09-29 PR #101's regime split merged at 10:46Z and the page
# kept pricing at the 00:01Z fit (docs/findings-2026-09-29-fit-gap.md). Main's
# first-parent history dates a merged change by its merge, not its branch commit.
# Advisory: a failed refit publishes with the previous rates.
#
# A refit that would move a published rate outside its own interval is held, not
# written: the new fit goes to history/model-rates.candidate.json (untracked) and
# the rates in force stay. That is not a crash, and the advisory record counts it as
# a run that worked (tracker/advisory.py HELD_EXITS). Exit 3 is a new hold, or one
# that moves different rates: one alert listing the moves. Exit 4 is the hold
# already reported, which is every later tick until someone adopts or the moves
# change: a log line, no alert. Exit 1 is a crash, a warning line and the advisory
# incident after its grace. docs/OPERATIONS.md has the command that adopts a hold.
rates_held_moves() {
  python3 - <<'PYEOF'
import json
try:
    with open("history/model-rates.candidate.json", encoding="utf-8") as fh:
        held = json.load(fh)["_meta"]["held"]
    print("\n".join(f"  - {m}" for m in held.get("moves") or []) or "  (no moves recorded)")
except (OSError, ValueError, KeyError, TypeError) as exc:
    print(f"  (could not read history/model-rates.candidate.json: {exc})")
PYEOF
}
rates_due() {
  local gen changed
  gen="$(python3 -c 'import datetime, json; g = json.load(open("history/model-rates.json"))["_meta"]["generated_at"]; print(g[:10], int(datetime.datetime.fromisoformat(g).timestamp()))' 2>/dev/null)" || return 0
  [ "${gen% *}" != "$(date +%F)" ] && return 0
  changed="$(git log -1 --first-parent --format=%ct -- tools/model_rates.py history/masterrig-passive.json 2>/dev/null)"
  [ -n "$changed" ] && [ "$changed" -gt "${gen#* }" ]
}
if rates_due; then
  PYTHONPATH=. nice python3 -m tools.model_rates history/masterrig-passive.json \
    --json history/model-rates.json > /dev/null
  rates_rc=$?
  case "$rates_rc" in
    3)
      echo "tools.model_rates: refit held (exit 3), publishing with the rates in force; candidate in history/model-rates.candidate.json" >&2
      alert_jonathan "Model rates refit held, rates in force kept" \
        "$(printf 'The daily refit (tools/model_rates.py) moved published rates beyond their intervals, so it was held: history/model-rates.json is unchanged and the page keeps pricing at it. The new fit is in history/model-rates.candidate.json on gs. It moves:\n\n%s\n\nThis is reported once; later ticks of the same hold stay quiet (exit 4). To adopt it after reading the moves, see docs/OPERATIONS.md (--accept-moves).\n' "$(rates_held_moves)")"
      python3 -m tracker.advisory --step tools.model_rates --exit "$rates_rc" || true ;;
    4)
      echo "tools.model_rates: refit still held (exit 4), already reported; publishing with the rates in force" >&2
      python3 -m tracker.advisory --step tools.model_rates --exit "$rates_rc" || true ;;
    *)
      advisory tools.model_rates "$rates_rc" "publishing with the previous model-rates.json" ;;
  esac
fi

# The four-account limit-change detector (tracker/four_accounts.py, ADR 0001 rule 22), the
# only source of the limit changes the page states. Once per UTC day, on the records known at
# midnight (the gs-passive rebuild above, masterrig's pushed history), under nice: it finds the
# steps every Max account takes together, holds each one 7 daily runs before it is measured,
# and writes history/four-accounts.json, which the publish below copies into the page and the
# state commit carries. Every later tick that day is a no-op (exit 0). Advisory: a failed run
# publishes with the previous state, and the invariants check `detector_ran` alerts once the
# newest run is more than a day old.
nice python3 -m tracker.four_accounts --state history/four-accounts.json
advisory tracker.four_accounts $? "publishing with the previous history/four-accounts.json"

python3 -m tracker.publish \
  --probes history/probes.jsonl \
  --passive history/passive.json \
  --contributed data/contributed.json \
  --gs-passive history/gs-passive.json \
  --four-accounts history/four-accounts.json \
  --out "$SITE/website/public/data/claude-usage.json"
rc=$?

if [ "$rc" -ne 0 ]; then
  echo "error: tracker.publish failed with exit $rc" >&2
  exit "$rc"
fi

# The publisher rewrites data/prices.json when the weekly output run supplied a
# new output class weight (tracker/weight.py), or to record one it refused,
# tracker.list_prices above adds a row for a new model in use, and
# tracker.contributed above appends to history/contributed.jsonl and rewrites
# data/contributed.json. That is tracker state, so it goes back to this repo's
# branch in one commit; the site gets the published JSON below. Advisory: a
# failed push is a warning, the files are still committed locally and the next
# pull --rebase --autostash carries them.
# Test gate (tracker/publish_gate.py): the tests that read the data this run is about
# to commit, in one pytest process under 60 s. On 2026-09-29 a new price row broke 11
# tests and was committed and published for days unnoticed. A failure does not block:
# the numbers keep flowing and the gate sends one WhatsApp alert per incident (and one
# when it passes again) through tracker.supervise's sender.
nice python3 -m tracker.publish_gate \
  || echo "warning: publish test gate failed (its alert line is above), publishing anyway" >&2
# Invariants over the JSON just built (tracker/invariants.py): the headline inside its
# accounts, direct figures published as measured, account units, and the four-account
# detector's changes (ADR 0001 rule 22) stated once, drawn where they are and nowhere else.
# Same semantics as the gate: one alert when a check starts failing, one on recovery, plus one
# when the page starts being withheld and a reminder every 24 h while it is; its "invariants
# alert:" line says what it sent. Checks 1-3, 9 and 10 never block (exit 1). Checks 4-8
# (exit 2: the headline is not the detector's newest measured change, a change is not drawn on
# its line, one change has two figures, a chart steps where the detector measured no change, or
# an account is named other than by its label) fail the publish: the last published JSON is
# put back, the site gets no commit, and the run finishes (state commit, notify) and exits 9.
python3 -m tracker.invariants --json "$SITE/website/public/data/claude-usage.json"
inv_rc=$?
withheld=0
if [ "$inv_rc" -eq 2 ]; then
  echo "error: public JSON invariants contradict the page, keeping the last published JSON" >&2
  git -C "$SITE" checkout -q -- website/public/data/claude-usage.json
  withheld=1
elif [ "$inv_rc" -ne 0 ]; then
  echo "warning: public JSON invariants failed, publishing anyway" >&2
fi

# Only these files are added and committed (`--only`): anything else staged in this
# checkout stays staged and is never pushed with the state.
STATE_FILES=(data/prices.json)
git add data/prices.json
# None of these exist until their first successful run (contributed) or first
# tick of usable transcript+meter data (gs-passive).
for f in history/contributed.jsonl data/contributed.json history/gs-passive.json history/model-rates.json \
         history/four-accounts.json; do
  if [ -f "$f" ]; then git add "$f"; STATE_FILES+=("$f"); fi
done
if ! git diff --cached --quiet HEAD -- "${STATE_FILES[@]}"; then
  git -c user.name=publisher -c user.email=publisher@gs commit -q --only \
    -m "Daily publisher state $(date -u +%FT%H:%MZ)" -- "${STATE_FILES[@]}"
  if ! git push -q origin "$BRANCH"; then
    echo "warning: git push of tracker state failed, committed locally only" >&2
  fi
fi

(
  cd "$SITE" || exit 1
  git add website/public/data/claude-usage.json
  if git diff --cached --quiet; then
    echo "no change"
    exit 0
  fi
  git -c user.name="All Done Sites bot" -c user.email="bot@alldonesites.com" \
    commit -q -m "data: refresh claude usage"
  site_push
)
publish_rc=$?

# Record a failing site push in the tracker repo, where masterrig's health check reads it
# (tracker/health.py check_publisher). Without this the tracker side stays fresh while the
# page itself never updates. Written and pushed on every failure and on the first success
# after one; a run that succeeds after a success touches nothing.
SITE_STATUS=history/site-push.json
site_was_ok=1
grep -q '"ok": false' "$SITE_STATUS" 2>/dev/null && site_was_ok=0
if [ "$publish_rc" -ne 0 ] || [ "$site_was_ok" -eq 0 ]; then
  now_utc="$(date -u +%FT%TZ)"
  last_ok="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("last_ok",""))' "$SITE_STATUS" 2>/dev/null || true)"
  if [ "$publish_rc" -eq 0 ]; then
    printf '{"ok": true, "at": "%s", "last_ok": "%s"}\n' "$now_utc" "$now_utc" > "$SITE_STATUS"
  else
    printf '{"ok": false, "at": "%s", "last_ok": "%s"}\n' "$now_utc" "$last_ok" > "$SITE_STATUS"
  fi
  git add "$SITE_STATUS"
  git -c user.name=publisher -c user.email=publisher@gs commit -q --only -m "Site push status $now_utc" -- "$SITE_STATUS" \
    && { git push -q origin "$BRANCH" || echo "warning: git push of the site push status failed" >&2; }
fi

# Tell alldonesites.com to email its subscribers, but only about a change we have
# not already announced. Everything below is advisory: a missing env file, no
# network, or a non-2xx answer logs a warning and leaves publish_rc alone. The
# publish is the job; the email is a courtesy on top of it.
notify_change() {
  local json="$SITE/website/public/data/claude-usage.json"
  # The script cd'd to the repo root on entry and the push above ran in a
  # subshell, so $PWD is still that root.
  local state="$PWD/.notified-change"
  local attempts="$PWD/.notify-attempts"
  local seen="$PWD/.weekly-change-seen"
  local env_file="$HOME/.claude-usage-notify.env"

  [ -f "$json" ] || { echo "notify: $json missing, skipping" >&2; return 0; }

  # One python3 read: record what this publish shows, then emit the POST body, or
  # nothing at all when there is nothing to announce yet. `last_change` is the four-account
  # detector's newest measured change (tracker/four_accounts.py, ADR 0001 rule 22): found on
  # 7 daily runs in a row before it reaches the page, and never moved or withdrawn after, so
  # the guards below rarely wait. They stay as guards: a change is announced only once two
  # consecutive publishes of new evidence show it dated within a day of each other, and
  # never when it is within a day of a date already announced. New evidence means a newer
  # window in weekly_windows.passive, or a newer stretch on any account
  # (account_feeds[].newest_stretch_end): this script publishes every half hour but the
  # histories arrive daily, and a re-read of the same windows and stretches is not a second
  # look. The observation is recorded before the env file is checked, so it never skips a day.
  #
  # The email also waits for the change to be 24 hours old (its instant `at`, or the start
  # of its date when it has none, to the publish's generated_at), and to 48 hours while the
  # interval of its own figure includes no change. The email states the measured figure and
  # its state, never announcement text.
  local body
  body="$(SEEN="$seen" NOTIFIED="$state" python3 - "$json" <<'PYEOF'
import json, os, sys
from datetime import date, datetime, timezone

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        public = json.load(fh)
except (OSError, ValueError) as exc:
    print(f"notify: could not read {sys.argv[1]}: {exc}", file=sys.stderr)
    raise SystemExit(0)
if not isinstance(public, dict):
    raise SystemExit(0)
change = public.get("last_change")
required = ("date", "direction", "percent")
shown = None
if isinstance(change, dict):
    if change.get("provisional") or change.get("legacy_uncertain"):
        print("notify: change evidence is provisional or legacy-uncertain, skipping", file=sys.stderr)
    elif any(change.get(k) is None for k in required):
        print("notify: last_change is missing date/direction/percent, skipping", file=sys.stderr)
    else:
        shown = change["date"]


def same_event(a, b):
    try:
        return abs((date.fromisoformat(str(a)[:10]) - date.fromisoformat(str(b)[:10])).days) <= 1
    except ValueError:
        return False


def instant(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def newest(stamps):
    """The newest stamp by instant (the feeds mix UTC offsets), as the stamp itself."""
    dated = [(instant(s), str(s)) for s in stamps if s and instant(s) is not None]
    dated = [(t if t.tzinfo else t.replace(tzinfo=timezone.utc), s) for t, s in dated]
    return max(dated)[1] if dated else None


windows = ((public.get("weekly_windows") or {}).get("passive") or {}).get("by_window") or []
feeds = public.get("account_feeds") or {}
evidence = (newest([w.get("window_ending") for w in windows if isinstance(w, dict)]
                   + [f.get("newest_stretch_end") for f in (feeds.values() if isinstance(feeds, dict) else [])
                      if isinstance(f, dict)])
            or public.get("passive_generated_at") or public.get("generated_at"))
try:
    with open(os.environ["SEEN"], encoding="utf-8") as fh:
        seen = json.load(fh)
except (OSError, ValueError):
    seen = {}
if not isinstance(seen, dict):
    seen = {}
if seen.get("evidence") != evidence:
    seen = {"evidence": evidence, "date": shown, "previous": seen.get("date")}
else:
    seen["date"] = shown
with open(os.environ["SEEN"], "w", encoding="utf-8") as fh:
    fh.write(json.dumps(seen) + "\n")

if shown is None:
    raise SystemExit(0)
if not same_event(seen.get("previous"), shown):
    print(f"notify: change on {shown} is not yet on two consecutive publishes of new evidence, waiting",
          file=sys.stderr)
    raise SystemExit(0)


def utc(stamp):
    t = instant(stamp)
    return None if t is None else (t if t.tzinfo else t.replace(tzinfo=timezone.utc))


now = utc(public.get("generated_at")) or datetime.now(timezone.utc)
since = utc(change.get("at")) or utc(f"{change['date']}T00:00:00+00:00")
age_hours = (now - since).total_seconds() / 3600 if since else 0.0
if age_hours < 24:
    print(f"notify: change on {shown} is {age_hours:.1f} hours old, under 24, waiting", file=sys.stderr)
    raise SystemExit(0)
if age_hours < 48 and change.get("interval_excludes_no_change") is False:
    print(f"notify: change on {shown} is {age_hours:.1f} hours old and its interval still "
          "includes no change, waiting for 48", file=sys.stderr)
    raise SystemExit(0)
try:
    with open(os.environ["NOTIFIED"], encoding="utf-8") as fh:
        announced = [line.strip() for line in fh if line.strip()]
except OSError:
    announced = []
if any(same_event(d, shown) for d in announced):
    raise SystemExit(0)
payload = {k: change[k] for k in required}
if change.get("model"):
    payload["model"] = change["model"]
for key in ("scope", "metric", "change_pct", "interval_pct", "weekly_limit_change_pct",
            "weekly_limit_change_interval_pct", "state"):
    if change.get(key) is not None:
        payload[key] = change[key]
print(json.dumps(payload))
PYEOF
)"
  [ -n "$body" ] || return 0

  local date
  date="$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["date"])')"
  local summary
  summary="$(BODY="$body" python3 - <<'PYEOF'
import json, os
c = json.loads(os.environ["BODY"])
model = f" ({c['model']})" if c.get("model") else ""
figure = ""
if c.get("change_pct") is not None:
    figure = f", {c['change_pct']:+g}%"
    if c.get("interval_pct"):
        figure += f" [{c['interval_pct'][0]:+g}, {c['interval_pct'][1]:+g}]"
    figure += f" {c.get('metric') or ''}".rstrip()
if c.get("weekly_limit_change_pct") is not None:
    figure += f", weekly limit {c['weekly_limit_change_pct']:+g}%"
    if c.get("weekly_limit_change_interval_pct"):
        lo, hi = c["weekly_limit_change_interval_pct"]
        figure += f" [{lo:+g}, {hi:+g}]"
state = f", {c['state']}" if c.get("state") else ""
print(f"{c['direction']} {c['percent']}% on {c['date']}{model}{figure}{state}")
PYEOF
)"

  # Subscribers are emailed a change that is not provisional (checked above) and whose
  # `state`, when it has one, is "measured". The publisher puts nothing but the four-account
  # detector's measured changes in last_change (its candidates and provisional changes stay in
  # `four_accounts`), so this is a guard: on 2026-10-17 an opus-4-6 "-43%" record of the old
  # change machinery carried `provisional: false` while its state was "provisional", and would
  # have been emailed. A record with no state is emailed as before (the director's ruling of
  # 2026-10-23). Jonathan still hears about a held change, once per change and state (a line in
  # .notify-attempts), and it is emailed if it later reads "measured".
  local change_state
  change_state="$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state") or "")')"
  if [ -n "$change_state" ] && [ "$change_state" != "measured" ]; then
    echo "notify: change on $date is $change_state, not measured: subscribers are not emailed" >&2
    if ! { [ -f "$attempts" ] && grep -qxF "$date held:$change_state" "$attempts"; }; then
      printf '%s held:%s\n' "$date" "$change_state" >> "$attempts"
      alert_jonathan "Observed change, not emailed ($change_state): $summary" \
        "$(printf 'The publisher found a new last_change in %s:\n\n%s\n\nIts state is %s, not measured, so subscribers were not emailed. They will be if it reads measured.\n' "$json" "$body" "$change_state")"
    fi
    return 0
  fi

  if [ ! -r "$env_file" ]; then
    echo "notify: $env_file missing or unreadable, skipping" >&2
    return 0
  fi

  local secret
  # Anchored so the explanatory comment in the file, which also names the
  # variable, cannot be picked up as the value.
  secret="$(sed -n 's/^NOTIFY_SEND_SECRET=//p' "$env_file" | head -1 | tr -d '\r\n')"
  [ -n "$secret" ] || { echo "notify: no NOTIFY_SEND_SECRET in $env_file, skipping" >&2; return 0; }

  # A failed send is tried, and alerted, at most once per UTC day for a given
  # change date: one "DATE UTC_DAY" line per attempt, written after the POST
  # whatever its status. Without this a failing endpoint was POSTed and alerted
  # every half hour (six identical alerts for the 29 Sep change on 2026-10-01).
  local today
  today="$(date -u +%F)"
  if [ -f "$attempts" ] && grep -qxF "$date $today" "$attempts"; then
    echo "notify: send for $date already attempted today, waiting" >&2
    return 0
  fi

  local status
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 --retry 2 --retry-delay 5 \
    -X POST "https://alldonesites.com/api/notify/send" \
    -H "authorization: Bearer $secret" \
    -H "content-type: application/json" \
    --data-binary "$body" 2>&1)" || true
  printf '%s %s\n' "$date" "$today" >> "$attempts"

  case "$status" in
    2??)
      # Recorded only on success, so a failed call retries on the first publish
      # of the next UTC day. One announced date per line: none is ever announced
      # twice.
      printf '%s\n' "$date" >> "$state"
      echo "notify: announced $date (HTTP $status)"
      ;;
    *)
      echo "warning: notify POST for $date returned '$status', will retry tomorrow" >&2
      ;;
  esac

  # Jonathan hears about every confirmed change, whether or not the list send
  # went through. A failed fan-out retries on the first publish of the next UTC
  # day and alerts again, once, which is the reminder wanted; later publishes
  # that day stop at the attempt check above. A successful one is recorded above
  # and never repeats.
  alert_jonathan "Observed change: $summary" \
    "$(printf 'The publisher found a new last_change in %s:\n\n%s\n\nSubscriber send via /api/notify/send: HTTP %s\n' "$json" "$body" "$status")"
}

notify_change

# A withheld publish is a failed run, after everything above has had its turn: the state
# commit still lands, and notify_change read the last published JSON, which it has already
# seen. A failed site push keeps its own exit.
if [ "$withheld" -eq 1 ] && [ "$publish_rc" -eq 0 ]; then
  echo "error: run withheld the public JSON (exit 9): the page still shows the last published JSON" >&2
  exit 9
fi
exit "$publish_rc"
