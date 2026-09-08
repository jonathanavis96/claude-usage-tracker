#!/usr/bin/env bash
# Scheduled probe on gs: one rotation slot. A thin caller of tracker.rotate, which
# picks the model (Sonnet, Opus, Fable in turn, from the last prose row in
# history/probes.jsonl) and its expectation (the last median dollar value per 1%
# through the dollar invariant, see tracker/rotate.py and docs/spike-2026-09.md).
#
#   run      tracker.probe with the rotation's flags (3 ticks after 1 skipped span)
#   check    tracker.rotate check: the new row against the median of that model's
#            last four prose rows; more than 15% away is drift
#   rerun    on drift, one 2-tick confirmation run on the same model
#   decide   tracker.rotate decide: rerun agrees with the median -> the first row is
#            an outlier, flagged in place; agrees with the first row -> a change;
#            neither -> inconclusive, nothing flagged
#   alert    one email to Jonathan for an outlier, a change or an inconclusive pair
#
# Each row is committed and pushed as soon as it lands (the checkout's branch, main, private repo)
# so masterrig's daily publisher can see it; the outlier flag is a second commit.
#
# Exit codes bubble up from tracker.probe: 0 ok, 3 no idle account, 4 aborted; a
# failed rerun exits with the rerun's code (the first row is already committed).
# Exit 5 is this wrapper's own: another tracker job holds the lock. Exit 1: the
# rotation could not supply flags (no usable prose row in history).
# A failed push is logged but does not change the exit code (cron mail should
# reflect the probe's own success/failure, not a transient git hiccup) --
# but the probe row is still lost if push fails, so the log line matters.
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1

# One tracker job at a time: a probe and the daily publisher share this checkout.
LOCK="$(cd "$(dirname "$0")/.." && pwd)/.cron.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "probe skipped: another tracker job holds $LOCK" >&2
  exit 5
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git pull -q --rebase --autostash origin "$BRANCH" || echo "warning: git pull --rebase failed, continuing with local state" >&2

HISTORY=history/probes.jsonl

# One email to Jonathan through the site's send endpoint (tracker/alert.py reads
# NOTIFY_ALERT_TO and the bearer secret from ~/.claude-usage-notify.env). Advisory:
# it can never change this script's exit status.
alert_jonathan() {
  python3 -m tracker.alert --subject "$1" --text "$2" || true
}

# The probe's own output is kept (out/ is gitignored) so an alert can quote it.
mkdir -p out
PROBE_LOG=out/probe-last.log

# run_probe <flags from tracker.rotate...>: one tracker.probe run, its exit code returned.
run_probe() {
  python3 -m tracker.probe --effort low --out "$HISTORY" "$@" 2>&1 | tee "$PROBE_LOG"
  return "${PIPESTATUS[0]}"
}

# commit_history <message>: commit and push the history file if it changed.
commit_history() {
  git add "$HISTORY"
  if git diff --cached --quiet; then
    echo "nothing to commit"
    return 0
  fi
  git -c user.name=probe -c user.email=probe@gs commit -q -m "$1"
  if ! git push -q origin "$BRANCH"; then
    echo "warning: git push failed, probe row committed locally only" >&2
  fi
}

# report_failure <model> <rc> <what>: the alerts for a probe that wrote no row.
report_failure() {
  case "$2" in
    3) alert_jonathan "Probe skipped: no idle account for $1" \
         "$(printf '%s: tracker.probe exited 3 (no idle account within the wait).\n\nLast lines:\n%s\n' "$3" "$(tail -n 20 "$PROBE_LOG")")" ;;
    4) alert_jonathan "Probe aborted on $1" \
         "$(printf '%s: tracker.probe exited 4 (window reset or a jump it could not explain). No row was written.\n\nLast lines:\n%s\n' "$3" "$(tail -n 20 "$PROBE_LOG")")" ;;
  esac
}

# The rotation's flags: "--model M --expect-tokens-per-pct N". Word-split on purpose.
FLAGS="$(python3 -m tracker.rotate flags --history "$HISTORY")" || exit 1
# shellcheck disable=SC2086
set -- $FLAGS
MODEL="$2"

run_probe "$@"
rc=$?
if [ "$rc" -ne 0 ]; then
  report_failure "$MODEL" "$rc" "Rotation run"
  exit "$rc"
fi
commit_history "Probe $(date -u +%FT%H:%MZ) $MODEL"

verdict="$(python3 -m tracker.rotate check --history "$HISTORY")"
check_rc=$?
echo "drift check: $verdict"
if [ "$check_rc" -ne 10 ]; then
  exit 0
fi

# Drift: one 2-tick rerun on the same model, then let the pair decide.
RERUN_FLAGS="$(python3 -m tracker.rotate flags --rerun --history "$HISTORY")" || exit 1
# shellcheck disable=SC2086
set -- $RERUN_FLAGS
run_probe "$@"
rc=$?
if [ "$rc" -ne 0 ]; then
  report_failure "$MODEL" "$rc" "Drift rerun"
  alert_jonathan "Drift on $MODEL unconfirmed: rerun exited $rc" \
    "$(printf 'First run: %s\n\nThe rerun wrote no row, so the drifted row stands unflagged until the next %s slot.\n' "$verdict" "$MODEL")"
  exit "$rc"
fi
commit_history "Probe rerun $(date -u +%FT%H:%MZ) $MODEL"

decision="$(python3 -m tracker.rotate decide --history "$HISTORY")" || {
  alert_jonathan "Drift on $MODEL: decide failed" \
    "$(printf 'First run: %s\n\ntracker.rotate decide could not judge the pair; nothing flagged.\n' "$verdict")"
  exit 0
}
echo "decision: $decision"
commit_history "Probe $(date -u +%FT%H:%MZ) $MODEL: ${decision%% *}"

case "${decision%% *}" in
  outlier) alert_jonathan "Outlier on $MODEL" \
             "$(printf 'First run: %s\n\n%s\n\nThe flagged row stays in history and the publisher and rotation ignore it.\n' "$verdict" "$decision")" ;;
  change) alert_jonathan "Change confirmed on $MODEL" \
            "$(printf 'First run: %s\n\n%s\n\nBoth rows stand; the publisher will step the page on its next run.\n' "$verdict" "$decision")" ;;
  *) alert_jonathan "Drift on $MODEL inconclusive" \
       "$(printf 'First run: %s\n\n%s\n\nNothing flagged; both rows enter the medians. Worth a look.\n' "$verdict" "$decision")" ;;
esac

exit 0
