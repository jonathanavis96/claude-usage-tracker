"""tracker.supervise: overlap guard, retry, rotation, one alert per incident and one recovery."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tracker import health, supervise

PY = sys.executable


class FakeSender:
    def __init__(self, ok: bool = True):
        self.sent: list[str] = []
        self.ok = ok

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


class SuperviseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / "s.lock"
        self.state = self.tmp / "state.json"
        self.log = self.tmp / "run.log"
        self.send = FakeSender()
        self.health_reason: str | None = None
        self.t = 1_000_000.0
        self.sleeps: list[float] = []
        patcher = mock.patch.object(health, "first_failure", lambda *a, **k: self.health_reason)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Belt and braces: a test must never reach the real WhatsApp path.
        guard = mock.patch.object(supervise, "whatsapp_sender", side_effect=AssertionError("real send"))
        guard.start()
        self.addCleanup(guard.stop)

    def run_cmd(self, code: int, **kw) -> int:
        return supervise.supervise([PY, "-c", f"print('hi'); raise SystemExit({code})"], lock=self.lock,
                                   state_path=self.state, log=self.log, send=self.send,
                                   health_cfg=health.Config(), sleep=self.sleeps.append,
                                   now=lambda: self.t, **kw)

    def st(self) -> dict:
        return json.loads(self.state.read_text())

    def test_success_records_last_ok_and_logs_output(self):
        self.assertEqual(self.run_cmd(0), 0)
        self.assertEqual(self.st()["last_ok"], self.t)
        self.assertIn("hi", self.log.read_text())
        self.assertFalse(self.lock.with_suffix(".pid").exists())
        self.assertEqual(self.send.sent, [])

    def test_retries_with_exponential_backoff(self):
        self.assertEqual(self.run_cmd(3, retries=2, backoff=5), 3)
        self.assertEqual(self.sleeps, [5, 10])
        self.assertEqual(self.log.read_text().count("attempt"), 3)
        self.assertEqual(self.st()["consecutive_failures"], 1)

    def test_transient_failure_healed_by_retry(self):
        flag = self.tmp / "flag"
        cmd = [PY, "-c", f"import os,sys; p={str(flag)!r}; e=os.path.exists(p); open(p,'w'); sys.exit(0 if e else 1)"]
        code = supervise.supervise(cmd, lock=self.lock, state_path=self.state, log=self.log, send=self.send,
                                   health_cfg=health.Config(), sleep=self.sleeps.append, now=lambda: self.t)
        self.assertEqual(code, 0)
        self.assertEqual(self.st()["consecutive_failures"], 0)

    def test_overlapping_run_is_skipped(self):
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertEqual(self.run_cmd(1), 0)
            self.assertFalse(self.state.exists(), "a skipped run must not touch state")
        finally:
            os.close(fd)

    def test_stale_pidfile_is_cleared(self):
        self.lock.with_suffix(".pid").write_text("999999999")
        self.assertTrue(supervise.clear_stale_pidfile(self.lock.with_suffix(".pid")))
        self.lock.with_suffix(".pid").write_text(str(os.getppid()))
        self.assertFalse(supervise.clear_stale_pidfile(self.lock.with_suffix(".pid")))

    def test_one_alert_after_three_failures_then_one_recovery(self):
        for _ in range(5):
            self.run_cmd(1, retries=0)
            self.t += 3600
        self.assertEqual(len(self.send.sent), 1)
        self.assertIn("3 runs in a row failed", self.send.sent[0])
        self.run_cmd(0)
        self.run_cmd(0)
        self.assertEqual(len(self.send.sent), 2)
        self.assertIn("recovered", self.send.sent[1])
        self.assertNotIn("incident_open", self.st())

    def test_two_failures_do_not_alert(self):
        self.run_cmd(1, retries=0)
        self.run_cmd(1, retries=0)
        self.run_cmd(0)
        self.assertEqual(self.send.sent, [])

    def test_stale_data_alerts_after_an_hour_not_before(self):
        self.health_reason = "collection: newest meter sample is 2h00m old"
        self.run_cmd(0)
        self.t += 1800
        self.run_cmd(0)
        self.assertEqual(self.send.sent, [])
        self.t += 1800
        self.run_cmd(0)
        self.t += 1800
        self.run_cmd(0)
        self.assertEqual(len(self.send.sent), 1)
        self.assertIn("collection", self.send.sent[0])
        self.health_reason = None
        self.run_cmd(0)
        self.assertEqual(len(self.send.sent), 2)

    def test_brief_blip_never_alerts(self):
        self.health_reason = "publisher: stale"
        self.run_cmd(0)
        self.health_reason = None
        self.t += 7200
        self.run_cmd(0)
        self.assertEqual(self.send.sent, [])

    def test_failed_send_is_retried_next_run(self):
        self.send.ok = False
        for _ in range(3):
            self.run_cmd(1, retries=0)
        self.assertNotIn("incident_open", self.st())
        self.send.ok = True
        self.run_cmd(1, retries=0)
        self.assertTrue(self.st()["incident_open"])
        self.assertEqual(len(self.send.sent), 2)

    def test_log_rotation_keeps_n(self):
        for i in range(5):
            self.log.write_text("x" * 100)
            supervise.rotate_log(self.log, 10, keep=3)
        names = sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("run.log"))
        self.assertEqual(names, ["run.log.1", "run.log.2", "run.log.3"])
        self.log.write_text("x")
        self.assertFalse(supervise.rotate_log(self.log, 10, keep=3))

    def test_dry_run_env_selects_printing_sender(self):
        with mock.patch.dict(os.environ, {"CUT_ALERT_DRY_RUN": "1"}):
            self.assertIs(supervise.default_sender(), supervise.dry_run_sender)

    def test_whatsapp_sender_reads_ok_and_fail(self):
        import subprocess
        for out, code, want in (("OK\n", 0, True), ("FAIL\n", 0, False), ("", 255, False)):
            with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, out, "")) as r:
                self.assertEqual(REAL_WA(out and "msg"), want)
                self.assertEqual(r.call_args.kwargs["input"], out and "msg")


REAL_WA = supervise.whatsapp_sender

if __name__ == "__main__":
    unittest.main()
