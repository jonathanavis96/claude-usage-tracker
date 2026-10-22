#!/usr/bin/env bash
# Hourly on masterrig, from bin/passive.sh before its 20-hour gate: write text-free extracts of
# every account used here other than this host's own login (tracker/extract.py), and rsync them
# to gs, where tracker.gs_passive joins each to its account by identity on the next half-hour
# run. jwork, dave and avis have all been run on masterrig since October, and until this their
# meters moved on gs with no tokens behind them (the 2026-10-22 capture diagnosis).
#
# What leaves masterrig: per-turn timestamps, message and session ids, model, token counts,
# entrypoint and sidechain flags, and the project directory names -- no text of any kind. It
# goes to a private directory on gs, never to this public repo.
#
# Advisory: always exits 0, so the passive join that follows is never held up. A failed extract
# or an unreachable gs is one dated warning line on stderr; the next hour ships whatever changed
# since (the extractor rewrites only changed sources, and rsync sends only the difference).
# Never `--delete`: gs keeps every extract it has been sent, so history counted there stays
# counted even if a transcript here is removed.
#
#   SHIP_EXTRACTS_DEST  rsync destination (default gs:.paperclip/ops/remote-transcripts/<host>/)
#   SHIP_EXTRACTS_OUT   local extract directory (default ~/.cache/claude-usage-tracker/extracts/<host>)
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 0
say() { printf '%s ship-extracts.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
HOST="$(hostname -s | tr '[:upper:]' '[:lower:]')"
OUT="${SHIP_EXTRACTS_OUT:-$HOME/.cache/claude-usage-tracker/extracts/$HOST}"
DEST="${SHIP_EXTRACTS_DEST:-gs:.paperclip/ops/remote-transcripts/$HOST/}"
mkdir -p "$OUT" || { say "warning: cannot create $OUT; nothing shipped"; exit 0; }
if ! summary="$(nice -n 10 python3 -m tracker.extract --out "$OUT" --host "$HOST")"; then
  say "warning: tracker.extract failed; nothing shipped this hour"
  exit 0
fi
# Owner-only on gs (the extracts name session ids and project directories). The state file is
# the extractor's own bookkeeping and stays here.
if rsync -az --mkpath --chmod=Du=rwx,Dgo=,Fu=rw,Fgo= --exclude=.extract-state.json \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=15" "$OUT/" "$DEST"; then
  say "shipped to $DEST: $(printf '%s' "$summary" | tr '\n' ';')"
else
  say "warning: rsync to $DEST failed (exit $?); the next run ships it"
fi
exit 0
