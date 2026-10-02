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
        guarde = mock.patch.object(supervise, "email_sender", side_effect=AssertionError("real email"))
        guarde.start()
        self.addCleanup(guarde.stop)

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
        self.assertEqual(self.run_cmd(5, retries=2, backoff=5), 5)
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

    def test_off_main_exit_3_is_not_retried_and_alerts_at_once(self):
        self.assertEqual(self.run_cmd(3, retries=2, backoff=5), 3)
        self.assertEqual(self.sleeps, [], "a checkout off main is not healed by retrying")
        st = self.st()
        self.assertEqual((st["last_exit"], st["consecutive_failures"]), (3, 1))
        self.assertIn("off main", st["last_reason"])
        self.assertEqual(len(self.send.sent), 1)
        self.assertIn("off main", self.send.sent[0])
        self.run_cmd(0)
        self.assertIn("recovered", self.send.sent[-1])

    def test_pull_conflict_and_leftover_rebase_are_not_retried_and_alert_at_once(self):
        for code, words in ((7, "conflict"), (8, "rebase --abort")):
            with self.subTest(code=code):
                self.send.sent.clear()
                self.assertEqual(self.run_cmd(code, retries=2, backoff=5), code)
                self.assertEqual(self.sleeps, [])
                self.assertIn(words, self.st()["last_reason"])
                self.assertEqual(len(self.send.sent), 1)
                self.assertIn(words, self.send.sent[0])
                self.run_cmd(0)
                self.assertIn("recovered", self.send.sent[-1])

    def test_dirty_tracker_exit_4_is_named_and_waits_for_the_threshold(self):
        self.run_cmd(4, retries=0)
        self.assertIn("uncommitted", self.st()["last_reason"])
        self.assertEqual(self.send.sent, [])

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

    def test_default_falls_back_to_email_when_whatsapp_fails(self):
        with mock.patch.dict(os.environ, {"CUT_ALERT_DRY_RUN": ""}):
            self.assertIs(supervise.default_sender(), supervise.whatsapp_or_email_sender)
        with mock.patch.object(supervise, "whatsapp_sender", return_value=False), \
             mock.patch.object(supervise, "email_sender", return_value=True) as e:
            self.assertTrue(supervise.whatsapp_or_email_sender("x"))
            e.assert_called_once_with("x")
        with mock.patch.object(supervise, "whatsapp_sender", return_value=True), \
             mock.patch.object(supervise, "email_sender") as e:
            self.assertTrue(supervise.whatsapp_or_email_sender("x"))
            e.assert_not_called()

    def test_email_sender_unconfigured_or_unreachable_is_false(self):
        from tracker import alert
        with mock.patch.object(alert, "alert_config", return_value="no secret"):
            self.assertFalse(REAL_EMAIL("x"))
        cfg = alert.AlertConfig(to="t@example.invalid", secret="s", url="http://127.0.0.1:9/")
        with mock.patch.object(alert, "alert_config", return_value=cfg), \
             mock.patch.object(alert, "send_alert", return_value=(200, "ok")) as s:
            self.assertTrue(REAL_EMAIL("Claude usage tracker (gs) needs attention: x"))
            self.assertEqual(s.call_args.args[0], "Claude usage tracker (gs) needs attention")
        with mock.patch.object(alert, "alert_config", return_value=cfg), \
             mock.patch.object(alert, "send_alert", side_effect=OSError):
            self.assertFalse(REAL_EMAIL("x"))


REAL_WA = supervise.whatsapp_sender
REAL_EMAIL = supervise.email_sender


class ProfileArgsTest(unittest.TestCase):
    def captured(self, *argv):
        with mock.patch.object(supervise, "supervise", return_value=0) as m:
            supervise.main([*argv, "--", "true"])
        return m.call_args.kwargs

    def test_gs_profile_defaults(self):
        kw = self.captured("--profile", "gs")
        self.assertEqual((kw["lock"], kw["state_path"], kw["log"]), (health.GS_LOCK, health.GS_STATE, health.GS_LOG))
        self.assertEqual((kw["retries"], kw["fail_threshold"], kw["label"]), (0, 2, "gs"))
        self.assertEqual(kw["health_cfg"].checks, ("run", "meters", "lock", "sizes"))
        self.assertEqual(kw["kuma_file"], supervise.kuma_url_file())

    def test_masterrig_defaults_unchanged(self):
        kw = self.captured("--kuma-url-file", "/x/url")
        self.assertEqual((kw["retries"], kw["fail_threshold"], kw["label"]), (2, 3, "masterrig"))
        self.assertIsNone(kw["health_cfg"])
        self.assertEqual(kw["lock"], supervise.ROOT / ".supervise.lock")
        self.assertEqual(kw["kuma_file"], Path("/x/url"))


if __name__ == "__main__":
    unittest.main()
