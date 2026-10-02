#!/usr/bin/env bash
# Install or update the masterrig schedule for the Claude usage tracker. Idempotent:
# run it any number of times and the crontab ends up with exactly one managed block.
#
#   deploy/install-schedule.sh --dry-run     print the crontab it would install, change nothing
#   deploy/install-schedule.sh               install it
#
# The block runs bin/passive.sh hourly at :15 under tracker.supervise (flock overlap
# guard, retry with backoff, log rotation, health check, one WhatsApp alert per incident
# and one recovery). Any bare legacy line that runs this repo's bin/passive.sh is
# replaced, so the job never runs twice. Every other crontab line is kept as it was.
#
# Overrides, for tests and other hosts:
#   --repo DIR      checkout the cron runs from (default: this script's repo)
#   --log FILE      run log (default ~/.paperclip/ops/claude-usage-passive.log)
#   CRONTAB=cmd     crontab binary (a test points it at a fake one)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$HOME/.paperclip/ops/claude-usage-passive.log"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --repo) REPO="$2"; shift ;;
    --log) LOG="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
CRONTAB="${CRONTAB:-crontab}"
BEGIN="# ===== CLAUDE USAGE TRACKER (managed by deploy/install-schedule.sh) ====="
END="# ===== END CLAUDE USAGE TRACKER ====="

current="$($CRONTAB -l 2>/dev/null || true)"
kept="$(printf '%s\n' "$current" | awk -v b="$BEGIN" -v e="$END" -v p="$REPO/bin/passive.sh" '
  $0 == b {skip=1; next}
  $0 == e {skip=0; next}
  skip {next}
  index($0, p) && $0 !~ /^[[:space:]]*#/ {next}
  {print}' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')"

block="$BEGIN
# Hourly passive join + health check + alerting. Docs: $REPO/docs/OPERATIONS.md
15 * * * * cd $REPO && /usr/bin/python3 -m tracker.supervise --log $LOG -- $REPO/bin/passive.sh >/dev/null 2>&1
$END"

new="$(printf '%s\n%s\n' "$kept" "$block" | sed '/./,$!d')"
if [ "$DRY" = 1 ]; then
  printf '%s\n' "$new"
  exit 0
fi
mkdir -p "$(dirname "$LOG")"
if [ "$new" = "$current" ]; then
  echo "schedule already up to date"
  exit 0
fi
printf '%s\n' "$new" | $CRONTAB -
echo "schedule installed"
