"""Red-team 2: the deploy/install-*.sh scripts run twice, or on a host with the old line.

Every crontab here is a file behind a fake `crontab` script in a temp HOME, never a real
one. Checked and holding (no test): running either installer twice changes nothing the
second time, the live masterrig line (`15 * * * * /home/grafe/code/claude-usage-tracker/
bin/passive.sh >> ... 2>&1`, read 2026-10-02) and the documented gs line are both
replaced, commented lines are kept, and the two blocks coexist.

The checkout is a real git repo on main (tests/_deploy_fixture.py), so preflight passes
and a run reaches the point where it would write the crontab.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests._deploy_fixture import make_checkout  # noqa: E402

# `crontab -l` fails for a reason other than "no crontab": an unreadable spool, cron's
# own permission check, a crontab binary that cannot reach its daemon.
FAKE = """#!/bin/sh
f="$HOME/crontab.txt"
if [ "$1" = "-l" ]; then
  echo "crontab: cannot open your crontab: Permission denied" >&2
  exit 1
fi
echo written >> "$HOME/writes.txt"
cat > "$f"
"""
TAB = """SHELL=/bin/bash
*/2 * * * * /home/jonathan/code/otto/bin/otto-poll >> /home/jonathan/code/otto/logs/cron.log 2>&1
0,30 * * * * {repo}/bin/daily.sh >> /home/jonathan/.paperclip/ops/claude-usage-daily.log 2>&1
15 * * * * {repo}/bin/passive.sh >> /home/jonathan/.paperclip/ops/claude-usage-passive.log 2>&1
15 3 * * * cd /home/jonathan/code/auto-mail && bin/prune-var --apply >> var/prune-var.log 2>&1
"""


@pytest.mark.parametrize("mode", [[], ["--no-check"], ["--dry-run"], ["--print"]])
@pytest.mark.parametrize("script", ["install-schedule.sh", "install-gs.sh"])
def test_unreadable_crontab_is_not_replaced(fake_home, script, mode):
    repo = make_checkout(fake_home / "cut")
    fake = fake_home / "fakecrontab"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    tab = fake_home / "crontab.txt"
    tab.write_text(TAB.format(repo=repo))
    before = tab.read_text()
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin", "CRONTAB": str(fake), "CUT_PIHOME_SSH": "none"}
    r = subprocess.run(["bash", str(ROOT / "deploy" / script), "--repo", str(repo), *mode], env=env,
                       capture_output=True, text=True, timeout=60)
    assert tab.read_text() == before, f"{script} {mode} rewrote the crontab after `crontab -l` failed"
    assert not (fake_home / "writes.txt").exists(), f"{script} {mode} called `crontab -` after `crontab -l` failed"
    assert r.returncode == 3, f"{script} {mode} exited {r.returncode}, not 3 (refused): {r.stderr[-400:]!r}"
    assert "Permission denied" in r.stderr and "cannot read the current crontab" in r.stderr
    assert r.stdout.strip() == "", f"{script} {mode} printed a crontab or a diff: {r.stdout[:200]!r}"
