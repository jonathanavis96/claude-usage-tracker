#!/usr/bin/env bash
# Undo one deploy/install-schedule.sh or deploy/install-gs.sh run on this host.
#
#   deploy/rollback.sh BACKUP_DIR            dry run: show the diff back to crontab.before
#   deploy/rollback.sh BACKUP_DIR --apply    restore crontab.before
#
# BACKUP_DIR is the directory the installer printed ("backup: ..."), under
# ~/.local/state/claude-usage-tracker/deploy-backups/<UTC stamp>-<host>/. Run it on the
# host that made it.
#
# Refuses (exit 3) when the crontab is no longer the one the installer wrote
# (crontab.after): someone changed it since, and restoring would discard that. Pass
# --force to restore anyway. The crontab it replaces is saved first, as
# crontab.pre-rollback-<UTC stamp> in BACKUP_DIR, so a rollback can itself be undone
# with `crontab BACKUP_DIR/crontab.pre-rollback-<stamp>`.
#
# The installers change nothing else (the run log directory they create is left).
# CRONTAB=cmd overrides the crontab binary (tests).
set -uo pipefail
DIR="" APPLY=0 FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) APPLY=0 ;;
    --force) FORCE=1 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) DIR="$1" ;;
  esac
  shift
done
CRONTAB="${CRONTAB:-crontab}"
[ -n "$DIR" ] || { echo "usage: deploy/rollback.sh BACKUP_DIR [--apply] [--force]" >&2; exit 2; }
for f in crontab.before crontab.after manifest.env; do
  [ -f "$DIR/$f" ] || { echo "not a deploy backup: $DIR/$f missing" >&2; exit 2; }
done
host="$(sed -n 's/^host=//p' "$DIR/manifest.env")"
current="$($CRONTAB -l 2>/dev/null || true)"
before="$(cat "$DIR/crontab.before")"
after="$(cat "$DIR/crontab.after")"
echo "rollback of the $host install in $DIR: $([ "$APPLY" = 1 ] && echo APPLY || echo 'DRY RUN, nothing is changed')" >&2
if [ "$current" = "$before" ]; then
  echo "crontab already matches crontab.before: nothing to do" >&2; exit 0
fi
if [ "$current" != "$after" ]; then
  echo "the crontab has changed since the install (it is not crontab.after):" >&2
  diff -u --label "crontab.after" --label "crontab (now)" <(printf '%s\n' "$after") <(printf '%s\n' "$current") >&2
  if [ "$FORCE" = 0 ]; then echo "REFUSE: pass --force to restore crontab.before anyway" >&2; exit 3; fi
fi
diff -u --label "crontab (now)" --label "crontab.before" <(printf '%s\n' "$current") <(printf '%s\n' "$before")
[ "$APPLY" = 1 ] || exit 0
save="$DIR/crontab.pre-rollback-$(date -u +%Y%m%dT%H%M%SZ)"
( umask 077 && printf '%s\n' "$current" > "$save" ) || { echo "cannot save $save, nothing changed" >&2; exit 3; }
printf '%s\n' "$before" | $CRONTAB - || { echo "crontab install failed" >&2; exit 4; }
if [ "$($CRONTAB -l 2>/dev/null || true)" != "$before" ]; then
  echo "the crontab read back differs from crontab.before" >&2; exit 4
fi
echo "restored crontab.before (the replaced crontab is in $save)"
