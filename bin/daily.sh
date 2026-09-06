#!/usr/bin/env bash
# Daily on gs: merge probe rows and passive history into the public JSON, and
# push it into the alldonesites site repo.
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
if [ -d "$SITE" ]; then
  git -C "$SITE" pull -q origin main || echo "warning: site repo pull failed, continuing with local state" >&2
else
  git clone -q git@github-cut-site:jonathanavis96/all-done-sites-platform.git "$SITE" \
    || { echo "error: could not clone site repo" >&2; exit 1; }
fi

python3 -m tracker.publish \
  --probes history/probes.jsonl \
  --passive history/passive.json \
  --out "$SITE/website/public/data/claude-usage.json"
rc=$?

if [ "$rc" -ne 0 ]; then
  echo "error: tracker.publish failed with exit $rc" >&2
  exit "$rc"
fi

# The publisher rewrites data/prices.json when the weekly output run supplied a
# new output class weight (tracker/weight.py), or to record one it refused. That
# is tracker state, so it goes back to this repo's branch; the site gets the
# published JSON below. Advisory: a failed push is a warning, the file is still
# committed locally and the next pull --rebase --autostash carries it.
git add data/prices.json
if ! git diff --cached --quiet; then
  git -c user.name=publisher -c user.email=publisher@gs commit -q -m "Output class weight $(date -u +%FT%H:%MZ)"
  if ! git push -q origin "$BRANCH"; then
    echo "warning: git push of data/prices.json failed, committed locally only" >&2
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
  if ! git push -q origin main; then
    echo "warning: git push to site repo failed, commit made locally only" >&2
  fi
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
  local env_file="$HOME/.claude-usage-notify.env"

  [ -f "$json" ] || { echo "notify: $json missing, skipping" >&2; return 0; }
  if [ ! -r "$env_file" ]; then
    echo "notify: $env_file missing or unreadable, skipping" >&2
    return 0
  fi

  local secret
  # Anchored so the explanatory comment in the file, which also names the
  # variable, cannot be picked up as the value.
  secret="$(sed -n 's/^NOTIFY_SEND_SECRET=//p' "$env_file" | head -1 | tr -d '\r\n')"
  [ -n "$secret" ] || { echo "notify: no NOTIFY_SEND_SECRET in $env_file, skipping" >&2; return 0; }

  # One python3 read: emit the POST body, or nothing at all when last_change is
  # null or already announced.
  local body
  body="$(NOTIFIED="$(cat "$state" 2>/dev/null || true)" python3 - "$json" <<'PYEOF'
import json, os, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        change = json.load(fh).get("last_change")
except (OSError, ValueError) as exc:
    print(f"notify: could not read {sys.argv[1]}: {exc}", file=sys.stderr)
    raise SystemExit(0)
if not isinstance(change, dict):
    raise SystemExit(0)
date = change.get("date")
if not date or date == os.environ.get("NOTIFIED", "").strip():
    raise SystemExit(0)
required = ("date", "direction", "percent")
if any(change.get(k) is None for k in required):
    print("notify: last_change is missing date/direction/percent, skipping", file=sys.stderr)
    raise SystemExit(0)
payload = {k: change[k] for k in required}
if change.get("model"):
    payload["model"] = change["model"]
print(json.dumps(payload))
PYEOF
)"
  [ -n "$body" ] || return 0

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
      # Recorded only on success, so a failed call simply retries tomorrow.
      printf '%s\n' "$date" > "$state"
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
print(f"{c['direction']} {c['percent']}% on {c['date']}{model}")
PYEOF
)"
  alert_jonathan "Change confirmed: $summary" \
    "$(printf 'The publisher found a new last_change in %s:\n\n%s\n\nSubscriber send via /api/notify/send: HTTP %s\n' "$json" "$body" "$status")"
}

notify_change

exit "$publish_rc"
