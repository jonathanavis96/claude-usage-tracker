"""Red-team 2: the deploy/install-*.sh scripts run twice, or on a host with the old line.

Every crontab here is a file behind a fake `crontab` script in a temp HOME, never a real
one. Checked and holding (no test): running either installer twice changes nothing the
second time, the live masterrig line (`15 * * * * /home/grafe/code/claude-usage-tracker/
bin/passive.sh >> ... 2>&1`, read 2026-10-02) and the documented gs line are both
replaced, commented lines are kept, and the two blocks coexist.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# `crontab -l` fails for a reason other than "no crontab": an unreadable spool, cron's
# own permission check, a crontab binary that cannot reach its daemon.
FAKE = """#!/bin/sh
f="$HOME/crontab.txt"
if [ "$1" = "-l" ]; then
  echo "crontab: cannot open your crontab: Permission denied" >&2
  exit 1
fi
cat > "$f"
"""
TAB = """SHELL=/bin/bash
*/2 * * * * /home/jonathan/code/otto/bin/otto-poll >> /home/jonathan/code/otto/logs/cron.log 2>&1
0,30 * * * * /srv/cut/bin/daily.sh >> /home/jonathan/.paperclip/ops/claude-usage-daily.log 2>&1
15 * * * * /srv/cut/bin/passive.sh >> /home/jonathan/.paperclip/ops/claude-usage-passive.log 2>&1
15 3 * * * cd /home/jonathan/code/auto-mail && bin/prune-var --apply >> var/prune-var.log 2>&1
"""


@pytest.mark.xfail(strict=True, reason="deploy/install-*.sh read a failed `crontab -l` as an empty crontab "
                                       "(`2>/dev/null || true`) and install a crontab of their block alone")
@pytest.mark.parametrize("script", ["install-schedule.sh", "install-gs.sh"])
def test_unreadable_crontab_is_not_replaced(fake_home, script):
    fake = fake_home / "fakecrontab"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    tab = fake_home / "crontab.txt"
    tab.write_text(TAB)
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin", "CRONTAB": str(fake)}
    r = subprocess.run(["bash", str(ROOT / "deploy" / script), "--repo", "/srv/cut"], env=env,
                       capture_output=True, text=True, timeout=60)
    after = tab.read_text()
    lost = [ln for ln in TAB.splitlines() if "/srv/cut/bin/" not in ln and ln not in after]
    assert not lost, (f"{script} exited {r.returncode} ({r.stdout.strip()!r}) after `crontab -l` failed, and "
                      f"the installed crontab lost every other job on the host: {lost}")
