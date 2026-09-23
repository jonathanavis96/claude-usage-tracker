#!/usr/bin/env bash
# Daily on masterrig: passive join, commit history/passive.json,
# history/masterrig-passive.json and history/masterrig-speed.json, push. Safe to run when gs is down.
#
# Two joins over the same logs, both kept (issue #52). tracker.passive is the
# original: 1% intervals, one tokens-per-percent number per UTC day, which is what
# the publisher reads. tracker.gs_passive --masterrig is the gs record shape -- every
# stretch with its own per-model token counts, runs, daily pooling and capture verdict
# -- so masterrig's longest-in-the-tracker meter history keeps the detail the daily
# number throws away. It is written whatever the capture check says about it; that
# meter counts web, phone and other machines, so the stretches carry usage this host's
# transcripts cannot see, and each one's `capture` is how a reader tells. Fast-session
# requests (tracker/speed.py) count in every stretch's tokens like any other, and are also
# recorded per stretch as `fast_session_tokens` (tracker/join.py), for diagnosis only.
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.."
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
python3 -m tracker.passive --out history/passive.json
# Never let the stretch record's failure cost the day's passive.json, which the page reads.
python3 -m tracker.gs_passive --masterrig --out history/masterrig-passive.json \
  || echo "warning: masterrig stretch join failed, history/masterrig-passive.json not updated" >&2
# Model speed (tracker/speed.py): this host's transcripts are only here, so its daily rows
# are appended here; old days are kept as stored, never recomputed. Advisory like the above.
nice -n 10 python3 -m tracker.speed --masterrig --history history/masterrig-speed.json \
  || echo "warning: speed rows failed, history/masterrig-speed.json not updated" >&2
# gs pushes probe rows to the same branch, so rebase onto them before committing.
git pull -q --rebase --autostash origin "$BRANCH" || echo "warning: git pull --rebase failed, continuing with local state" >&2
git add history/passive.json
git add history/masterrig-passive.json 2>/dev/null || true
git add history/masterrig-speed.json 2>/dev/null || true
git -c user.name=tracker -c user.email=tracker@local commit -q -m "Passive history $(date -u +%F)" || exit 0
if ! git push -q origin "$BRANCH"; then
  if git pull -q --rebase origin "$BRANCH" && git push -q origin "$BRANCH"; then
    :
  else
    echo "warning: git push failed after a rebase retry, commit made locally only" >&2
  fi
fi
