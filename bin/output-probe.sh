#!/usr/bin/env bash
# Weekly on gs (Sunday 06:00 UTC): a 5-tick Fable 5.1 probe with --payload output.
# This run does not measure the limit; it measures how hard the 5-hour meter
# charges output tokens against their list price (the output class weight, see
# tracker/weight.py and docs/spike-2026-09.md). The row it appends is tagged
# "payload": "output" and the publisher keeps it out of every rate series; the
# next daily publish recomputes class_weight.output from it against the latest
# Fable prose row and writes data/prices.json, or refuses and alerts Jonathan
# when the new weight is more than 30% from the current one.
#
# Exit codes bubble up from tracker.probe: 0 ok, 3 no idle account, 4 aborted.
# Exit 5 is this wrapper's own: another tracker job holds the lock.
# A failed push is logged but does not change the exit code (cron mail should
# reflect the probe's own success/failure, not a transient git hiccup) --
# but the probe row is still lost if push fails, so the log line matters.
#
# tracker.probe needs --expect-tokens-per-pct. For an output payload it only
# sizes each span's opening burst (the reply is a fixed 4,000 words); the
# literal below is the 2026-09-06 12:06 Fable output row, 31,724 tokens per 1%.
set -uo pipefail
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1

# One tracker job at a time: a probe and the daily publisher share this checkout.
LOCK="$(cd "$(dirname "$0")/.." && pwd)/.cron.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "output probe skipped: another tracker job holds $LOCK" >&2
  exit 5
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git pull -q --rebase --autostash origin "$BRANCH" || echo "warning: git pull --rebase failed, continuing with local state" >&2

MODEL=claude-fable-5-1
PAYLOAD=output
TICKS=5
EXPECT=31724

# One email to Jonathan through the site's send endpoint (tracker/alert.py reads
# NOTIFY_ALERT_TO and the bearer secret from ~/.claude-usage-notify.env). Advisory:
# it can never change this script's exit status.
alert_jonathan() {
  python3 -m tracker.alert --subject "$1" --text "$2" || true
}

# The probe's own output is kept (out/ is gitignored) so an alert can quote it.
mkdir -p out
PROBE_LOG=out/output-probe-last.log
python3 -m tracker.probe --model "$MODEL" --effort low --payload "$PAYLOAD" --ticks "$TICKS" \
  --out history/probes.jsonl --expect-tokens-per-pct "$EXPECT" 2>&1 | tee "$PROBE_LOG"
rc=${PIPESTATUS[0]}

case "$rc" in
  3) alert_jonathan "Output probe skipped: no idle account for $MODEL" \
       "$(printf 'tracker.probe --payload output exited 3 (no idle account within the wait). The output class weight keeps its current value until next Sunday.\n\nLast lines:\n%s\n' "$(tail -n 20 "$PROBE_LOG")")" ;;
  4) alert_jonathan "Output probe aborted on $MODEL" \
       "$(printf 'tracker.probe --payload output exited 4 (window reset or a jump it could not explain). No row was written; the output class weight keeps its current value until next Sunday.\n\nLast lines:\n%s\n' "$(tail -n 20 "$PROBE_LOG")")" ;;
esac

if [ "$rc" -eq 0 ]; then
  git add history/probes.jsonl
  if git diff --cached --quiet; then
    echo "nothing to commit"
  else
    git -c user.name=probe -c user.email=probe@gs commit -q -m "Probe $(date -u +%FT%H:%MZ) $MODEL $PAYLOAD"
    if ! git push -q origin "$BRANCH"; then
      echo "warning: git push failed, probe row committed locally only" >&2
    fi
  fi
fi

exit "$rc"
