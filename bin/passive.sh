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
#
# Safe to run every hour: it exits 0 at once unless its last successful run is more than
# 20 hours old, so a run the machine slept through is made up at the next hourly tick
# rather than the next day. `.passive-last-ok` in the checkout (gitignored) is the stamp:
# written only after a commit that pushed, or a run with nothing to commit, and it holds
# the git tree of tracker/ the joins ran on. A fresh stamp still runs when origin's
# tracker/ differs from that tree: the join code changed on main, and the record must be
# recounted now, not up to 20 hours later. On 2026-09-29 PR #99's token count reached gs's
# record at once and masterrig's not for a day, and the page's 22 September figure read
# +6.3% where the same data counted one way reads +29.8% (docs/findings-2026-09-29-fit-gap.md).
# A failed fetch keeps the plain age rule. `--force` skips the check.
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.."
STAMP=.passive-last-ok
MAX_AGE=$((20 * 3600))
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
# Every line this wrapper writes carries the UTC time: the cron log is otherwise undated.
say() { printf '%s passive.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
# Exit 8: a rebase or merge left in progress (by a run killed mid-pull, or by hand). Every
# later run would fail on it, and `git checkout main` does not end it.
GD="$(git rev-parse --absolute-git-dir)"
for f in rebase-merge rebase-apply MERGE_HEAD; do
  if [ -e "$GD/$f" ]; then
    say "error: a $f is in progress in $(pwd): finish or abort it (\`git rebase --abort\` / \`git merge --abort\`) before the record can run"
    exit 8
  fi
done
# Exit 7: a pull whose rebase stopped on a conflict. The rebase is aborted (which also
# restores an autostash), so the checkout is back on main as it was; a person resolves
# the conflict. Any other pull failure (offline) returns 1 and is advisory.
pull_rebase() {
  if git pull -q --rebase --autostash origin "$BRANCH"; then return 0; fi
  if [ -e "$GD/rebase-merge" ] || [ -e "$GD/rebase-apply" ]; then
    git rebase --abort >/dev/null 2>&1 || true
    say "error: git pull --rebase failed on a conflict with origin/$BRANCH; rebase aborted, checkout left on" \
        "$BRANCH at $(git rev-parse --short HEAD). Resolve by hand: git pull --rebase origin $BRANCH"
    exit 7
  fi
  return 1
}
# The record belongs on main. A checkout left on a feature branch once had this cron
# pull and push `crossing-detection`, and that day's record never reached main.
if [ "$BRANCH" != "main" ]; then
  say "checkout is on '$BRANCH', not main: refusing to join or push"
  exit 3
fi
# The tree of tracker/ at a commit, or nothing when it has none.
join_code() { git rev-parse -q --verify "$1:tracker" 2>/dev/null || true; }
if [ "${1:-}" != "--force" ] && [ -f "$STAMP" ] \
    && [ $(( $(date +%s) - $(stat -c %Y "$STAMP") )) -lt "$MAX_AGE" ]; then
  git fetch -q origin "$BRANCH" 2>/dev/null || true
  main_code="$(join_code "origin/$BRANCH")"
  if [ -z "$main_code" ] || [ "$main_code" = "$(cat "$STAMP")" ]; then
    exit 0
  fi
  say "tracker/ changed on origin/$BRANCH since the last record: joining again now"
fi
# Take main's code before the joins, not only before the commit: the joins run whatever
# tracker/ this checkout holds, and a pull only after them left each day's masterrig
# record one run behind main. PR #99 changed how tracker/turns.py counts a message's
# tokens, and masterrig's next record would still have been counted the old way while
# gs's were counted the new way, in one before-and-after comparison. A failed pull is
# advisory: the day's history is still written, on the code already here.
# Exit 4: tracker/ has uncommitted changes. The joins would run on someone's work in
# progress and push a record counted by it to main of a public repo. This checkout is
# also the one people and agents work in.
if [ -n "$(git status --porcelain --untracked-files=no -- tracker)" ]; then
  say "error: tracker/ has uncommitted changes in $(pwd): commit or stash them; the record waits"
  exit 4
fi
pull_rebase || say "warning: git pull --rebase before the joins failed, joining with the local code"
joined_with="$(join_code HEAD)"
say "joining on $BRANCH at $(git rev-parse --short HEAD)"
python3 -m tracker.passive --out history/passive.json
# Never let the stretch record's failure cost the day's passive.json, which the page reads.
python3 -m tracker.gs_passive --masterrig --out history/masterrig-passive.json \
  || say "warning: masterrig stretch join failed, history/masterrig-passive.json not updated"
# Model speed (tracker/speed.py): this host's transcripts are only here, so its daily rows
# are appended here; old days are kept as stored, never recomputed. Advisory like the above.
nice -n 10 python3 -m tracker.speed --masterrig --history history/masterrig-speed.json \
  || say "warning: speed rows failed, history/masterrig-speed.json not updated"
# gs pushes probe rows to the same branch, so rebase onto them before committing.
pull_rebase || say "warning: git pull --rebase failed, continuing with local state"
# Only the record's own files are added and committed (`--only`): anything else someone
# has staged in this checkout stays staged and is never pushed with the record.
RECORD=()
for f in history/passive.json history/masterrig-passive.json history/masterrig-speed.json; do
  if [ -e "$f" ]; then git add -- "$f"; RECORD+=("$f"); fi
done
if [ "${#RECORD[@]}" = 0 ] || git diff --cached --quiet HEAD -- "${RECORD[@]}"; then
  printf '%s' "$joined_with" > "$STAMP"
  exit 0
fi
# Exit 5: the record did not reach main (commit or push failed). tracker.supervise retries,
# then alerts at three failed runs in a row; a 0 here would read as a healthy run.
git -c user.name=tracker -c user.email=tracker@local commit -q --only -m "Passive history $(date -u +%F)" \
    -- "${RECORD[@]}" \
  || { say "error: git commit failed, the record was not committed"; exit 5; }
if git push -q origin "$BRANCH" \
    || { pull_rebase && git push -q origin "$BRANCH"; }; then
  printf '%s' "$joined_with" > "$STAMP"
else
  say "error: git push failed after a rebase retry, commit made locally only"
  exit 5
fi
