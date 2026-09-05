"""Syntax-check the gs deployment wrappers (Task 12): bin/probe.sh, bin/daily.sh."""
from __future__ import annotations
import subprocess
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


class TestDeployScriptsSyntax(unittest.TestCase):
    def _check(self, name: str) -> None:
        script = BIN / name
        self.assertTrue(script.exists(), f"{script} missing")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_sh_syntax(self) -> None:
        self._check("probe.sh")

    def test_daily_sh_syntax(self) -> None:
        self._check("daily.sh")


if __name__ == "__main__":
    unittest.main()
