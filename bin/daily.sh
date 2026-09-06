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
LOCK=/tmp/claude-usage-tracker.lock
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
