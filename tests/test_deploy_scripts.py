"""Syntax-check the gs deployment wrappers (Task 12): bin/probe.sh, bin/daily.sh.

Also exercises daily.sh's notify_change step offline, with a stub curl on PATH,
because that step must never be able to fail the publish it runs after.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import tempfile
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


class TestDailyNotifyChange(unittest.TestCase):
    """Run bin/daily.sh's notify_change function in isolation."""

    @classmethod
    def setUpClass(cls) -> None:
        text = (BIN / "daily.sh").read_text(encoding="utf-8")
        match = re.search(r"^notify_change\(\) \{.*?^\}$", text, re.S | re.M)
        assert match, "notify_change not found in bin/daily.sh"
        cls.func = match.group(0)

    def _run(self, *, last_change, notified=None, env_file=True, curl_status="200"):
        """Return (returncode, stdout, stderr, state-file contents or None)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "site" / "website" / "public" / "data"
            data.mkdir(parents=True)
            (data / "claude-usage.json").write_text(
                json.dumps({"last_change": last_change}), encoding="utf-8"
            )
            home = root / "home"
            home.mkdir()
            if env_file:
                (home / ".claude-usage-notify.env").write_text(
                    "# names NOTIFY_SEND_SECRET in a comment first\nNOTIFY_SEND_SECRET=s3cr3t\n",
                    encoding="utf-8",
                )
            # A stub curl that never touches the network. It records the request
            # so the payload can be asserted, and prints the status curl -w would.
            stub = root / "bin"
            stub.mkdir()
            curl = stub / "curl"
            curl.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$REQUEST_LOG\"\n"
                f"printf '{curl_status}'\n",
                encoding="utf-8",
            )
            curl.chmod(0o755)

            cwd = root / "repo"
            cwd.mkdir()
            if notified is not None:
                (cwd / ".notified-change").write_text(notified + "\n", encoding="utf-8")

            env = dict(os.environ)
            env.update(
                HOME=str(home),
                SITE=str(root / "site"),
                PATH=f"{stub}{os.pathsep}{env['PATH']}",
                REQUEST_LOG=str(root / "request.log"),
            )
            proc = subprocess.run(
                ["bash", "-c", f"set -uo pipefail\n{self.func}\nnotify_change"],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
            )
            state_path = cwd / ".notified-change"
            state = state_path.read_text(encoding="utf-8").strip() if state_path.exists() else None
            log_path = root / "request.log"
            request = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            return proc, state, request

    CHANGE = {"date": "2026-09-11", "direction": "increased", "percent": 7, "model": "claude-opus-5"}

    def test_posts_a_new_change_and_records_it(self) -> None:
        proc, state, request = self._run(last_change=self.CHANGE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(state, "2026-09-11")
        self.assertIn("https://alldonesites.com/api/notify/send", request)
        # The secret must come from the assignment line, not the comment above it.
        self.assertIn("authorization: Bearer s3cr3t", request)
        payload = json.loads(next(ln for ln in request.splitlines() if ln.startswith("{")))
        self.assertEqual(
            payload,
            {"date": "2026-09-11", "direction": "increased", "percent": 7, "model": "claude-opus-5"},
        )

    def test_skips_a_change_already_announced(self) -> None:
        proc, _state, request = self._run(last_change=self.CHANGE, notified="2026-09-11")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(request, "", "no request should be made for an announced change")

    def test_skips_when_there_is_no_change(self) -> None:
        proc, state, request = self._run(last_change=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(state)
        self.assertEqual(request, "")

    def test_skips_when_the_env_file_is_missing(self) -> None:
        proc, state, request = self._run(last_change=self.CHANGE, env_file=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(state)
        self.assertEqual(request, "")

    def test_a_non_2xx_answer_is_a_warning_and_retries_tomorrow(self) -> None:
        proc, state, _request = self._run(last_change=self.CHANGE, curl_status="500")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(state, "a failed POST must not be recorded as announced")
        self.assertIn("will retry tomorrow", proc.stderr)

    def test_incomplete_change_is_skipped(self) -> None:
        proc, state, request = self._run(last_change={"date": "2026-09-11"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(state)
        self.assertEqual(request, "")


if __name__ == "__main__":
    unittest.main()
