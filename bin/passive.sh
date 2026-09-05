#!/usr/bin/env bash
# Daily on masterrig: passive join, commit history/passive.json, push. Safe to run when gs is down.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m tracker.passive --out history/passive.json
git add history/passive.json
git -c user.name=tracker -c user.email=tracker@local commit -q -m "Passive history $(date -u +%F)" || exit 0
git push -q origin HEAD
