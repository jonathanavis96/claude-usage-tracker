#!/usr/bin/env bash
# Daily on masterrig: passive join, commit history/passive.json, push. Safe to run when gs is down.
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.."
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
python3 -m tracker.passive --out history/passive.json
# gs pushes probe rows to the same branch, so rebase onto them before committing.
git pull -q --rebase --autostash origin "$BRANCH" || echo "warning: git pull --rebase failed, continuing with local state" >&2
git add history/passive.json
git -c user.name=tracker -c user.email=tracker@local commit -q -m "Passive history $(date -u +%F)" || exit 0
if ! git push -q origin "$BRANCH"; then
  if git pull -q --rebase origin "$BRANCH" && git push -q origin "$BRANCH"; then
    :
  else
    echo "warning: git push failed after a rebase retry, commit made locally only" >&2
  fi
fi
