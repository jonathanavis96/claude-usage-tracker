#!/usr/bin/env bash
# Install or update the gs schedule for the Claude usage tracker. Idempotent: run it
# any number of times and the crontab ends up with exactly one managed block.
#
#   deploy/install-gs.sh --dry-run     preflight, report, and the crontab diff; changes nothing
#   deploy/install-gs.sh               install it (run ON gs, as jonathan): back up, install, check
#   deploy/install-gs.sh --print       print the full crontab it would install, changes nothing
#
# The block runs bin/daily.sh at :00 and :30 under `tracker.supervise --profile gs`:
# flock overlap guard, log rotation, no retries (daily.sh mails subscribers, and runs
# again in 30 min anyway), the gs health check (the publisher run plus the three
# claude-usage-meter-*.timer logs, tracker/health.py `meters`), one alert per incident
# and one recovery, and a dead-man ping to the Uptime Kuma push URL in
# ~/.config/claude-usage-tracker/kuma-push-url after each healthy run.
#
# The meter timers themselves are not changed: they already log every failed read as
# a gap line and retry on the next tick, and the supervised publisher run checks their
# logs every 30 min. This script only compares the installed units with deploy/systemd/
# and prints the diff of any drift.
#
# Any bare line that runs this repo's bin/daily.sh is replaced, so the publisher never
# runs twice. Commented lines and every other crontab line are kept as they were.
#
# Refuses (exit 3, nothing changed) on a checkout that is dirty, off main, mid-rebase,
# lacks the supervisor, or does not contain --expect. Backs the crontab up before
# changing it (deploy/rollback.sh undoes it) and ends with a post-deploy check. Details
# and exit codes: deploy/lib.sh. Morning procedure: docs/reliability-2026-10-02/DEPLOY.md.
#
# Options:
#   --repo DIR      checkout the cron runs from (default: this script's repo)
#   --log FILE      run log (default ~/.paperclip/ops/claude-usage-daily.log)
#   --expect SHA    a commit the checkout must contain
#   --no-check      skip the post-deploy check (tests)
#   CRONTAB=cmd     crontab binary (a test points it at a fake one)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$HOME/.paperclip/ops/claude-usage-daily.log"
MODE=apply EXPECT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) MODE=dry-run ;;
    --print) MODE=print ;;
    --no-check) [ "$MODE" = apply ] && MODE=apply-nocheck ;;
    --repo) REPO="$2"; shift ;;
    --log) LOG="$2"; shift ;;
    --expect) EXPECT="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
# shellcheck source=deploy/lib.sh
declare -F cut_install >/dev/null || . "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# The meter units are not installed here: report drift from deploy/systemd/, read-only.
# shellcheck disable=SC2317  # called from cut_install in deploy/lib.sh
cut_host_checks() {
  local repo="$1" u f drift=0
  [ -d "$repo/deploy/systemd" ] || { say "meter units: $repo/deploy/systemd missing, not compared"; return 0; }
  for f in "$repo"/deploy/systemd/claude-usage-meter-*; do
    u="$(basename "$f")"
    if ! cmp -s "$f" "$HOME/.config/systemd/user/$u"; then
      drift=1; _warn "meter unit $u differs from deploy/systemd/$u (not changed here):"
      diff -u --label "installed $u" --label "deploy/systemd/$u" "$HOME/.config/systemd/user/$u" "$f" >&2
    fi
    case "$u" in *.timer)
      [ "$(systemctl --user is-active "$u" 2>/dev/null)" = active ] || _warn "$u is not active" ;;
    esac
  done
  [ "$drift" = 0 ] && say "meter units: all match deploy/systemd/"
}

BEGIN="# ===== CLAUDE USAGE TRACKER GS (managed by deploy/install-gs.sh) ====="
END="# ===== END CLAUDE USAGE TRACKER GS ====="
BLOCK="$BEGIN
# Publisher every 30 min + gs health check (publisher run, meter timers) + alerting + Kuma ping.
# Docs: $REPO/docs/OPERATIONS.md
0,30 * * * * cd $REPO && /usr/bin/python3 -m tracker.supervise --profile gs --log $LOG -- $REPO/bin/daily.sh >> $LOG.supervise 2>&1
$END"
cut_install gs "$REPO" "$LOG" "$EXPECT" "$MODE" "$BEGIN" "$END" "$REPO/bin/daily.sh" "$BLOCK" "--profile gs"
exit $?
