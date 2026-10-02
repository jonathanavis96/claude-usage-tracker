"""deploy/install-gs.sh against a fake crontab in a temporary HOME: never gs's real one."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "install-gs.sh"
FAKE = """#!/bin/sh
f="$HOME/crontab.txt"
if [ "$1" = "-l" ]; then [ -f "$f" ] && cat "$f" || exit 1; else cat > "$f"; fi
"""
# The shape of gs's crontab as read on 2026-10-02 (other jobs abridged).
GS_TAB = """SHELL=/bin/bash
*/2 * * * * /home/jonathan/code/otto/bin/otto-poll >> /home/jonathan/code/otto/logs/cron.log 2>&1
0,30 * * * * /srv/cut/bin/daily.sh >> /home/jonathan/.paperclip/ops/claude-usage-daily.log 2>&1
# probes stopped 2026-09-16 (passive is the instrument): 0 0,12 * * * /srv/cut/bin/probe.sh >> x 2>&1
15 3 * * * cd /home/jonathan/code/auto-mail && bin/prune-var --apply >> var/prune-var.log 2>&1
"""


class InstallGsTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        fake = self.home / "fakecrontab"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "CRONTAB": str(fake)}
        self.tab = self.home / "crontab.txt"

    def run_it(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), "--repo", "/srv/cut", *args], env=self.env,
                              capture_output=True, text=True, check=True)

    def test_dry_run_changes_nothing(self):
        self.tab.write_text(GS_TAB)
        out = self.run_it("--dry-run").stdout
        self.assertIn("tracker.supervise --profile gs", out)
        self.assertNotIn(">> /home/jonathan/.paperclip/ops/claude-usage-daily.log", out)
        self.assertEqual(self.tab.read_text(), GS_TAB)
        self.assertFalse((self.home / ".paperclip").exists())

    def test_wraps_the_publisher_keeps_everything_else_and_is_idempotent(self):
        self.tab.write_text(GS_TAB)
        self.assertIn("installed", self.run_it().stdout)
        first = self.tab.read_text()
        for line in GS_TAB.splitlines():
            if "bin/daily.sh" not in line:
                self.assertIn(line, first)
        daily = [ln for ln in first.splitlines() if "bin/daily.sh" in ln and not ln.startswith("#")]
        self.assertEqual(len(daily), 1)
        self.assertTrue(daily[0].startswith("0,30 * * * * cd /srv/cut && /usr/bin/python3 -m tracker.supervise "
                                            "--profile gs --log "))
        self.assertIn("-- /srv/cut/bin/daily.sh", daily[0])
        self.assertIn("already up to date", self.run_it().stdout)
        self.assertEqual(self.tab.read_text(), first)
        self.assertTrue((self.home / ".paperclip" / "ops").is_dir())

    def test_coexists_with_the_masterrig_block(self):
        self.tab.write_text(GS_TAB)
        subprocess.run(["bash", str(ROOT / "deploy" / "install-schedule.sh"), "--repo", "/srv/cut"],
                       env=self.env, capture_output=True, text=True, check=True)
        self.run_it()
        tab = self.tab.read_text()
        self.assertEqual(tab.count("tracker.supervise"), 2)
        self.run_it()
        self.assertEqual(self.tab.read_text(), tab)

    def test_bad_argument(self):
        r = subprocess.run(["bash", str(SCRIPT), "--nope"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
