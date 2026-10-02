#!/usr/bin/env bash
# Run both schedule installers, masterrig here and gs over ssh, with one expected commit.
# A thin wrapper: all the work (preflight, diff, backup, install, post-deploy check) is
# in deploy/install-schedule.sh, deploy/install-gs.sh and deploy/lib.sh.
#
#   scripts/deploy.sh                       dry run on both hosts (the default)
#   scripts/deploy.sh --apply               install on both hosts
#   scripts/deploy.sh --host gs --apply     one host (masterrig | gs | all)
#   scripts/deploy.sh --expect SHA          the commit both checkouts must contain
#
# Run it on masterrig. --expect defaults to the newest commit in this checkout that
# touched tracker/, bin/ or deploy/, so both hosts must have pulled the same code.
# When gs has not pulled the new installer yet, a dry run previews gs with this
# checkout's installer piped over ssh (nothing is copied to gs); --apply refuses.
#
# Environment: CUT_MASTERRIG_REPO (default: this checkout), CUT_GS_SSH (default gs),
# CUT_GS_REPO (default ~/claude-usage-tracker on gs).
# Exit: the first non-zero installer exit (see deploy/lib.sh), 2 on usage.
set -uo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOSTS="masterrig gs" MODE=--dry-run EXPECT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) case "${2:-}" in masterrig|gs) HOSTS="$2" ;; all) ;; *) echo "--host masterrig|gs|all" >&2; exit 2 ;; esac; shift ;;
    --dry-run) MODE=--dry-run ;;
    --apply) MODE="" ;;
    --expect) EXPECT="${2:-}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$EXPECT" ] || EXPECT="$(git -C "$SELF" log -1 --format=%H -- tracker bin deploy)"
GS="${CUT_GS_SSH:-gs}"
GSREPO="${CUT_GS_REPO:-\$HOME/claude-usage-tracker}"
SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=15 "$GS")
echo "deploy: ${MODE:---apply}, expected commit ${EXPECT:0:12}, scripts from $SELF" >&2
rc=0
for h in $HOSTS; do
  case "$h" in
    masterrig)
      bash "$SELF/deploy/install-schedule.sh" --repo "${CUT_MASTERRIG_REPO:-$SELF}" --expect "$EXPECT" $MODE
      r=$? ;;
    gs)
      if "${SSH[@]}" "test -f $GSREPO/deploy/lib.sh"; then
        "${SSH[@]}" "cd $GSREPO && bash deploy/install-gs.sh --expect $EXPECT $MODE"
        r=$?
      elif [ -n "$MODE" ]; then
        echo "gs has not pulled the new installer: previewing with this checkout's copy, piped over ssh" >&2
        cat "$SELF/deploy/lib.sh" "$SELF/deploy/install-gs.sh" \
          | "${SSH[@]}" "bash -s -- --repo $GSREPO --expect $EXPECT --dry-run"
        r=$?
      else
        echo "REFUSE: gs has no deploy/lib.sh in $GSREPO: pull main on gs first" >&2
        r=3
      fi ;;
  esac
  [ "$rc" = 0 ] && rc=$r
done
exit "$rc"
