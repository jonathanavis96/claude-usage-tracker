"""deploy/install-schedule.sh against a fake crontab in a temporary HOME: never the live one."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "install-schedule.sh"
FAKE = """#!/bin/sh
f="$HOME/crontab.txt"
if [ "$1" = "-l" ]; then [ -f "$f" ] && cat "$f" || exit 1; else cat > "$f"; fi
"""


class InstallScheduleTest(unittest.TestCase):
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
        self.tab.write_text("0 0 * * * other\n")
        out = self.run_it("--dry-run").stdout
        self.assertIn("tracker.supervise", out)
        self.assertEqual(self.tab.read_text(), "0 0 * * * other\n")

    def test_replaces_legacy_line_keeps_others_and_is_idempotent(self):
        self.tab.write_text("0 0 * * * other\n15 * * * * /srv/cut/bin/passive.sh >> x.log 2>&1\n# /srv/cut/bin/passive.sh comment kept\n")
        self.assertIn("installed", self.run_it().stdout)
        first = self.tab.read_text()
        self.assertIn("0 0 * * * other", first)
        self.assertIn("# /srv/cut/bin/passive.sh comment kept", first)
        self.assertNotIn(">> x.log", first)
        self.assertEqual(first.count("tracker.supervise"), 1)
        self.assertIn("already up to date", self.run_it().stdout)
        self.assertEqual(self.tab.read_text(), first)
        self.assertTrue((self.home / ".paperclip" / "ops").is_dir())

    def test_empty_crontab(self):
        self.run_it()
        self.assertEqual(self.tab.read_text().count("CLAUDE USAGE TRACKER"), 2)


if __name__ == "__main__":
    unittest.main()
