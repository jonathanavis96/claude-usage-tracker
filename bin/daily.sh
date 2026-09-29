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

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git pull -q --rebase --autostash origin "$BRANCH" || echo "warning: git pull --rebase failed, continuing with local state" >&2

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
  --out data/contributed.json \
  || echo "warning: tracker.contributed failed with exit $?, publishing with the previous contributed block" >&2

# This host's own per-account passive join (issue #39): dave's and jwork's own
# transcripts (sub-agents included) against their own meters. The capture check
# runs and is recorded per stretch but does not gate (CAPTURE_GATE in
# tracker/gs_passive.py); only a stretch the transcripts leave empty is left
# out. Advisory, like tracker.contributed above -- a failed run publishes with
# whatever history/gs-passive.json this repo already has (or none).
python3 -m tracker.gs_passive \
  --prices data/prices.json \
  --probes history/probes.jsonl \
  --out history/gs-passive.json \
  || echo "warning: tracker.gs_passive failed with exit $?, publishing with the previous gs-passive.json" >&2

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
rates_due() {
  local gen changed
  gen="$(python3 -c 'import datetime, json; g = json.load(open("history/model-rates.json"))["_meta"]["generated_at"]; print(g[:10], int(datetime.datetime.fromisoformat(g).timestamp()))' 2>/dev/null)" || return 0
  [ "${gen% *}" != "$(date +%F)" ] && return 0
  changed="$(git log -1 --first-parent --format=%ct -- tools/model_rates.py history/masterrig-passive.json 2>/dev/null)"
  [ -n "$changed" ] && [ "$changed" -gt "${gen#* }" ]
}
if rates_due; then
  PYTHONPATH=. nice python3 -m tools.model_rates history/masterrig-passive.json \
    --json history/model-rates.json > /dev/null \
    || echo "warning: tools.model_rates failed with exit $?, publishing with the previous model-rates.json" >&2
fi

python3 -m tracker.publish \
  --probes history/probes.jsonl \
  --passive history/passive.json \
  --contributed data/contributed.json \
  --gs-passive history/gs-passive.json \
  --out "$SITE/website/public/data/claude-usage.json"
rc=$?

if [ "$rc" -ne 0 ]; then
  echo "error: tracker.publish failed with exit $rc" >&2
  exit "$rc"
fi

# The publisher rewrites data/prices.json when the weekly output run supplied a
# new output class weight (tracker/weight.py), or to record one it refused, and
# tracker.contributed above appends to history/contributed.jsonl and rewrites
# data/contributed.json. That is tracker state, so it goes back to this repo's
# branch in one commit; the site gets the published JSON below. Advisory: a
# failed push is a warning, the files are still committed locally and the next
# pull --rebase --autostash carries them.
git add data/prices.json
# None of these exist until their first successful run (contributed) or first
# tick of usable transcript+meter data (gs-passive).
for f in history/contributed.jsonl data/contributed.json history/gs-passive.json history/model-rates.json; do
  [ -f "$f" ] && git add "$f"
done
if ! git diff --cached --quiet; then
  git -c user.name=publisher -c user.email=publisher@gs commit -q -m "Daily publisher state $(date -u +%FT%H:%MZ)"
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

# One email to Jonathan through the same send endpoint, via tracker/alert.py
# (which reads NOTIFY_ALERT_TO and the bearer secret from
# ~/.claude-usage-notify.env, and skips with a note when either is missing).
# Advisory, like everything after the publish: it can never change this
# script's exit status. The weekly weight guard (tracker/weight.py, run inside
# tracker.publish above) raises its refused-weight alert in-process through the
# same helper's send_alert.
alert_jonathan() {
  python3 -m tracker.alert --subject "$1" --text "$2" || true
}

# Tell alldonesites.com to email its subscribers, but only about a change we have
# not already announced. Everything below is advisory: a missing env file, no
# network, or a non-2xx answer logs a warning and leaves publish_rc alone. The
# publish is the job; the email is a courtesy on top of it.
notify_change() {
  local json="$SITE/website/public/data/claude-usage.json"
  # The script cd'd to the repo root on entry and the push above ran in a
  # subshell, so $PWD is still that root.
  local state="$PWD/.notified-change"
  local seen="$PWD/.weekly-change-seen"
  local env_file="$HOME/.claude-usage-notify.env"

  [ -f "$json" ] || { echo "notify: $json missing, skipping" >&2; return 0; }

  # One python3 read: record what this publish shows, then emit the POST body, or
  # nothing at all when there is nothing to announce yet. Weekly segmentation is
  # retrospective: on masterrig's 2026-09-15 history a partial post-cut pool
  # certified -19% dated 2026-08-28, and the next day's windows moved it to the
  # real cut on 2026-09-14 (tracker/detect.py, why these constants). So a change is
  # announced only once two consecutive publishes of new weekly evidence show it
  # dated within a day of each other, and never when it is within a day of a date
  # already announced. New evidence means a newer window in weekly_windows.passive,
  # or a newer stretch on any account (account_feeds[].newest_stretch_end): this
  # script publishes every half hour but the histories arrive daily, and a re-read of
  # the same windows and stretches is not a second look. Stretches count as well as
  # windows because a five-hour change (last_change.scope "five_hour") is dated by a
  # model's first turn in the stretches, and the weekly windows can stall for days
  # while stretches keep arriving -- keyed on windows alone, the 2026-09-22 five-hour
  # change waited four days on a window list stuck at 2026-09-24. The observation is
  # recorded before the env file is checked, so it never skips a day.
  #
  # Changes reach the page at once, but the email waits for the data to settle: nothing
  # goes out until the change is 24 hours old (its instant `at`, or the start of its
  # date when it has none, to the publish's generated_at). From 24 hours it goes out if
  # its interval excludes no change -- on the five-hour or the weekly limit change,
  # whichever is larger, as the publisher reads it (a weekly event is certified, so it
  # does); from 48
  # hours it goes out at whatever figure it then has. The email states the measured
  # figure and how settled it is (`state`), never announcement text.
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

  if [ ! -r "$env_file" ]; then
    echo "notify: $env_file missing or unreadable, skipping" >&2
    return 0
  fi

  local secret
  # Anchored so the explanatory comment in the file, which also names the
  # variable, cannot be picked up as the value.
  secret="$(sed -n 's/^NOTIFY_SEND_SECRET=//p' "$env_file" | head -1 | tr -d '\r\n')"
  [ -n "$secret" ] || { echo "notify: no NOTIFY_SEND_SECRET in $env_file, skipping" >&2; return 0; }

  local date
  date="$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["date"])')"

  local status
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 --retry 2 --retry-delay 5 \
    -X POST "https://alldonesites.com/api/notify/send" \
    -H "authorization: Bearer $secret" \
    -H "content-type: application/json" \
    --data-binary "$body" 2>&1)" || true

  case "$status" in
    2??)
      # Recorded only on success, so a failed call simply retries on the next
      # publish. One announced date per line: none is ever announced twice.
      printf '%s\n' "$date" >> "$state"
      echo "notify: announced $date (HTTP $status)"
      ;;
    *)
      echo "warning: notify POST for $date returned '$status', will retry tomorrow" >&2
      ;;
  esac

  # Jonathan hears about every confirmed change, whether or not the list send
  # went through. A failed fan-out retries tomorrow and alerts again, which is
  # the reminder wanted; a successful one is recorded above and never repeats.
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
  alert_jonathan "Observed change: $summary" \
    "$(printf 'The publisher found a new last_change in %s:\n\n%s\n\nSubscriber send via /api/notify/send: HTTP %s\n' "$json" "$body" "$status")"
}

notify_change

exit "$publish_rc"
