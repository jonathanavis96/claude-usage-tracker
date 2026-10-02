#!/usr/bin/env bash
# Install or update the gs schedule for the Claude usage tracker. Idempotent: run it
# any number of times and the crontab ends up with exactly one managed block.
#
#   deploy/install-gs.sh --dry-run     print the crontab it would install, change nothing
#   deploy/install-gs.sh               install it (run ON gs, as jonathan)
#
# The block runs bin/daily.sh at :00 and :30 under `tracker.supervise --profile gs`:
# flock overlap guard, log rotation, no retries (daily.sh mails subscribers, and runs
# again in 30 min anyway), the gs health check (the publisher run plus the three
# claude-usage-meter-*.timer logs, tracker/health.py `meters`), one WhatsApp alert per
# incident and one recovery, and a dead-man ping to the Uptime Kuma push URL in
# ~/.config/claude-usage-tracker/kuma-push-url after each healthy run.
#
# The meter timers themselves are not changed: they already log every failed read as
# a gap line and retry on the next tick, and the supervised publisher run checks their
# logs every 30 min, so a stopped timer or an account whose reads all fail alerts once
# its newest usable reading is 15 min old and has stayed unhealthy for an hour.
#
# Any bare line that runs this repo's bin/daily.sh is replaced, so the publisher never
# runs twice. Commented lines and every other crontab line are kept as they were.
#
# Overrides, for tests:
#   --repo DIR      checkout the cron runs from (default: this script's repo)
#   --log FILE      run log (default ~/.paperclip/ops/claude-usage-daily.log)
#   CRONTAB=cmd     crontab binary (a test points it at a fake one)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$HOME/.paperclip/ops/claude-usage-daily.log"
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
BEGIN="# ===== CLAUDE USAGE TRACKER GS (managed by deploy/install-gs.sh) ====="
END="# ===== END CLAUDE USAGE TRACKER GS ====="

current="$($CRONTAB -l 2>/dev/null || true)"
kept="$(printf '%s\n' "$current" | awk -v b="$BEGIN" -v e="$END" -v p="$REPO/bin/daily.sh" '
  $0 == b {skip=1; next}
  $0 == e {skip=0; next}
  skip {next}
  index($0, p) && $0 !~ /^[[:space:]]*#/ {next}
  {print}' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')"

block="$BEGIN
# Publisher every 30 min + gs health check (publisher run, meter timers) + alerting + Kuma ping.
# Docs: $REPO/docs/OPERATIONS.md
0,30 * * * * cd $REPO && /usr/bin/python3 -m tracker.supervise --profile gs --log $LOG -- $REPO/bin/daily.sh >> $LOG.supervise 2>&1
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
