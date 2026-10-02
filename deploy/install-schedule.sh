#!/usr/bin/env bash
# Install or update the masterrig schedule for the Claude usage tracker. Idempotent:
# run it any number of times and the crontab ends up with exactly one managed block.
#
#   deploy/install-schedule.sh --dry-run   preflight, report, and the crontab diff; changes nothing
#   deploy/install-schedule.sh             install it: back up, install, post-deploy check
#   deploy/install-schedule.sh --print     print the full crontab it would install, changes nothing
#
# The block runs bin/passive.sh hourly at :15 under tracker.supervise (flock overlap
# guard, retry with backoff, log rotation, health check, one WhatsApp alert per incident
# and one recovery). Any bare legacy line that runs this repo's bin/passive.sh is
# replaced, so the job never runs twice. Every other crontab line is kept as it was.
#
# Refuses (exit 3, nothing changed) on a checkout that is dirty, off main, mid-rebase,
# lacks the supervisor, or does not contain --expect. Backs the crontab up before
# changing it (deploy/rollback.sh undoes it) and ends with a post-deploy check. Details
# and exit codes: deploy/lib.sh; operations: docs/OPERATIONS.md.
#
# Options:
#   --repo DIR      checkout the cron runs from (default: this script's repo)
#   --log FILE      run log (default ~/.paperclip/ops/claude-usage-passive.log)
#   --expect SHA    a commit the checkout must contain
#   --no-check      skip the post-deploy check (tests)
#   CRONTAB=cmd     crontab binary (a test points it at a fake one)
#   CUT_PIHOME_SSH  ssh host of the WhatsApp bridge (default pihome; none skips the check)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$HOME/.paperclip/ops/claude-usage-passive.log"
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

# masterrig sends incidents to WhatsApp over ssh to pihome. cron has no ssh agent, so
# check the bridge with cron's environment, read-only.
# shellcheck disable=SC2317  # called from cut_install in deploy/lib.sh
cut_host_checks() {
  local h="${CUT_PIHOME_SSH:-pihome}"
  [ "$h" = none ] && return 0
  if [ -s "$HOME/.config/claude-usage-tracker/whatsapp-to" ]; then
    say "whatsapp recipient: set in ~/.config/claude-usage-tracker/whatsapp-to"
  else
    _warn "no ~/.config/claude-usage-tracker/whatsapp-to: WhatsApp alerts are off, alerts go by email"
  fi
  if env -i HOME="$HOME" PATH=/usr/bin:/bin ssh -o BatchMode=yes -o ConnectTimeout=10 "$h" \
       'test -f /home/grafe/wa-assistant/wa_send.py' 2>/dev/null; then
    say "whatsapp bridge: $h:wa_send.py reachable with cron's environment"
  else
    _warn "$h wa_send.py is not reachable with cron's environment: WhatsApp alerts fall back to email"
  fi
}

BEGIN="# ===== CLAUDE USAGE TRACKER (managed by deploy/install-schedule.sh) ====="
END="# ===== END CLAUDE USAGE TRACKER ====="
BLOCK="$BEGIN
# Hourly passive join + health check + alerting. Docs: $REPO/docs/OPERATIONS.md
15 * * * * cd $REPO && /usr/bin/python3 -m tracker.supervise --log $LOG -- $REPO/bin/passive.sh >> $LOG.supervise 2>&1
$END"
cut_install masterrig "$REPO" "$LOG" "$EXPECT" "$MODE" "$BEGIN" "$END" "$REPO/bin/passive.sh" "$BLOCK" ""
exit $?
