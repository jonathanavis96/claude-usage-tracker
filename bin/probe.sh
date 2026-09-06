#!/usr/bin/env bash
# Twice daily on gs (03:30 and 15:30 UTC): a 5-tick probe, always claude-sonnet-5. Every other model's
# rate is derived from this one model's dollar value (see docs/spike-2026-09.md
# and tracker/publish.py) rather than probed directly. Appends to
# history/probes.jsonl and pushes it to this private repo (build branch)
# so masterrig's daily publisher can see it.
#
# Exit codes bubble up from tracker.probe: 0 ok, 3 no idle account, 4 aborted.
# Exit 5 is this wrapper's own: another tracker job holds the lock.
# A failed push is logged but does not change the exit code (cron mail should
# reflect the probe's own success/failure, not a transient git hiccup) --
# but the probe row is still lost if push fails, so the log line matters.
#
# tracker.probe needs --expect-tokens-per-pct (the last published Sonnet rate): it
# sizes the prompt to a tenth of a tick and each span's opening burst to 80% of
# the span. Defaults are 3 measured ticks after 1 skipped span; a window reset
# within 20 min is waited for so the run starts at 0.0. The literal below is
# the 2026-09-06 09:39 Sonnet row; tracker/rotate.py replaces it with the
# rotation's own expectation.
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

MODEL=claude-sonnet-5
EXPECT=468000

python3 -m tracker.probe --model "$MODEL" --effort low --out history/probes.jsonl \
  --expect-tokens-per-pct "$EXPECT"
rc=$?

if [ "$rc" -eq 0 ]; then
  git add history/probes.jsonl
  if git diff --cached --quiet; then
    echo "nothing to commit"
  else
    git -c user.name=probe -c user.email=probe@gs commit -q -m "Probe $(date -u +%FT%H:%MZ) $MODEL"
    if ! git push -q origin "$BRANCH"; then
      echo "warning: git push failed, probe row committed locally only" >&2
    fi
  fi
fi

exit "$rc"
