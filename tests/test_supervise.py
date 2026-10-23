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
        patcher = mock.patch.object(health, "all_failures", lambda *a, **k: self.health_reason)
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

    def test_masterrig_default_health_watches_a1s_meter_log(self):
        # No health_cfg (the masterrig cron line): the masterrig profile, with `meters` on
        # a1's own log, so a dead a1 feed alerts instead of hiding as the ceiling's did.
        seen = []
        with mock.patch.object(health, "all_failures", lambda cfg, *a, **k: seen.append(cfg)):
            supervise.supervise([PY, "-c", "pass"], lock=self.lock, state_path=self.state, log=self.log,
                                send=self.send, sleep=self.sleeps.append, now=lambda: self.t)
        self.assertIn("meters", seen[0].checks)
        self.assertEqual(seen[0].meter_logs, (health.GS_OPS / "claude-usage-meter-a1.log",))
        self.assertEqual((seen[0].state, seen[0].logs), (self.state, (self.log,)))

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

    def test_a_withheld_publish_exit_9_is_a_failed_run_never_retried(self):
        # bin/daily.sh exits 9 when a blocking invariant withheld the public JSON. Neither
        # profile retries it (masterrig's default is 2 retries), and it counts as a failure:
        # on gs's threshold of 2 the incident opens on the second run, an hour in.
        self.assertEqual(self.run_cmd(9, retries=2, backoff=5, fail_threshold=2), 9)
        self.assertEqual(self.sleeps, [], "a withheld publish withholds again on the same inputs")
        st = self.st()
        self.assertEqual((st["last_exit"], st["consecutive_failures"]), (9, 1))
        self.assertIn("withheld", st["last_reason"])
        self.assertEqual(self.send.sent, [])
        self.t += 1800
        self.run_cmd(9, retries=0, fail_threshold=2)
        self.assertEqual(len(self.send.sent), 1)
        self.assertIn("2 runs in a row failed (the public JSON was withheld", self.send.sent[0])
        self.assertNotIn("last_ok", self.st(), "a withheld run is never a success")

    def test_the_run_check_and_the_failed_runs_are_one_incident(self):
        # A withheld run fails both the runs and the `run` health check. As two kinds, the
        # health line arrived an hour later as a "further problem" about the same runs.
        sent = []
        run = "run: bin/daily.sh withheld the public JSON (exit 9), last successful run 30m ago: ..."
        st = {"consecutive_failures": 1, "last_reason": "withheld"}
        for t in (0, 1800, 3600, 5400, 7200):
            st["consecutive_failures"] += 1
            supervise.decide_alert(st, t, run, 2, 3600.0, lambda m: sent.append(m) or True, "gs")
        self.assertEqual(len(sent), 1, sent)
        self.assertEqual(st["incident_kinds"], ["runs"])
        self.assertEqual(supervise.health_kinds(run), {"runs"})

    # The review of 359b7c1: the supervisor read only health's first failure, so `advisory`
    # (last in gs's order) never alerted while `run` failed, and a blind dave meter ahead of
    # it sent "advisory/tools.model_rates recovered" for a step still crashing.
    ALL_FAILURES = staticmethod(health.all_failures)   # the real one: setUp patches it
    ADVISORY = ("advisory: tools.model_rates has failed on every run for 4h00m (limit 3h00m, last exit 1); "
                "the publish keeps using its previous output")
    DAVE = ("meters: dave is blind, no reading in claude-usage-meter-dave.log for 1h10m (limit 1h00m; "
            "header probe held, no session on this host since its window lapsed): the publisher uses "
            "this account, so what it spends meanwhile is not measured")

    def test_a_blind_meter_beside_a_crashing_step_never_reads_as_the_step_recovering(self):
        sent: list[str] = []
        st = {"consecutive_failures": 0}
        t = 0.0
        seq = [self.ADVISORY] * 4 + [f"{self.DAVE} | {self.ADVISORY}"] * 6 + [self.ADVISORY] * 4
        for reason in seq:
            supervise.decide_alert(st, t, reason, 2, 3600.0, lambda m: sent.append(m) or True, "gs")
            t += 1800
        self.assertEqual(len(sent), 3, sent)
        self.assertIn("needs attention: advisory: tools.model_rates", sent[0])
        self.assertIn("further problem: meters: dave is blind", sent[1])
        self.assertNotIn("tools.model_rates", sent[1], "the further problem names only what is new")
        self.assertIn("meters/dave recovered; still failing: advisory: tools.model_rates", sent[2])
        self.assertFalse(any("advisory/tools.model_rates recovered" in m for m in sent), sent)
        self.assertEqual(st["incident_kinds"], ["advisory/tools.model_rates"])

    def test_a_brief_fault_beside_a_lasting_one_is_neither_a_problem_nor_a_recovery(self):
        # Each kind keeps its own clock: a 30-min dave gap inside an hours-old advisory
        # incident is not stale yet, so it is not a further problem and not a recovery.
        sent: list[str] = []
        st = {"consecutive_failures": 0}
        for t, reason in ((0, self.ADVISORY), (3600, self.ADVISORY), (5400, f"{self.DAVE} | {self.ADVISORY}"),
                          (7200, self.ADVISORY), (9000, self.ADVISORY)):
            supervise.decide_alert(st, t, reason, 2, 3600.0, lambda m: sent.append(m) or True, "gs")
        self.assertEqual(len(sent), 1, sent)
        self.assertNotIn("meters/dave", st["kinds_since"])

    def test_a_stale_page_while_the_runs_fail_is_the_same_incident(self):
        # A withheld publish fails the runs (exit 9) and, two hours on, the page's age: one
        # fault. The page going stale with the runs succeeding is its own (a failed deploy).
        sent: list[str] = []
        page = "page: the published JSON in the site checkout (claude-usage.json) was generated 3h00m ago"
        st = {"consecutive_failures": 1, "last_reason": "withheld", "last_exit": 9}
        for t in range(0, 6 * 3600, 1800):
            st["consecutive_failures"] += 1
            supervise.decide_alert(st, t, page if t >= 3600 else None, 2, 3600.0,
                                   lambda m: sent.append(m) or True, "gs")
        self.assertEqual(len(sent), 1, sent)
        self.assertEqual(supervise.health_kinds(page), {"page/age"})
        st2 = {"consecutive_failures": 0}
        for t in (0, 3600):
            supervise.decide_alert(st2, t, page, 2, 3600.0, lambda m: sent.append(m) or True, "gs")
        self.assertIn("needs attention: page:", sent[-1])

    def test_a_state_from_before_per_kind_clocks_keeps_its_clock(self):
        sent: list[str] = []
        st = {"consecutive_failures": 0, "unhealthy_since": 0}
        supervise.decide_alert(st, 3600, self.ADVISORY, 2, 3600.0, lambda m: sent.append(m) or True, "gs")
        self.assertEqual(len(sent), 1, "the deploy must not restart an hour-old fault's clock")
        self.assertNotIn("unhealthy_since", st)
        self.assertEqual(st["kinds_since"], {"advisory/tools.model_rates": 0})

    def test_the_supervisor_alerts_on_every_failing_check_not_the_first(self):
        # A withheld run fails `run`, and the refit has crashed for hours: both are kinds.
        reasons = [("run", "run: bin/daily.sh withheld the public JSON (exit 9): the page is not updating"),
                   ("page", None), ("advisory", self.ADVISORY)]
        with mock.patch.object(health, "all_failures", self.ALL_FAILURES), \
                mock.patch.object(health, "run_checks", lambda *a, **k: reasons):
            for _ in range(3):
                self.run_cmd(9, retries=0, fail_threshold=2)
                self.t += 1800
        st = self.st()
        self.assertEqual(st["incident_kinds"], ["advisory/tools.model_rates", "runs"])
        self.assertIn("advisory: tools.model_rates", st["last_health"])
        self.assertIn("run: bin/daily.sh withheld", st["last_health"])
        self.assertEqual(len(self.send.sent), 2, self.send.sent)
        self.assertIn("further problem: advisory: tools.model_rates", self.send.sent[1])

    def test_page_and_advisory_reasons_are_their_own_kinds(self):
        self.assertEqual(supervise.health_kinds("page: cannot fetch https://x for 2h10m (grace 2h00m): URLError"),
                         {"page/fetch"})
        self.assertEqual(supervise.health_kinds("page: https://x was generated 3h00m ago (limit 2h00m)"),
                         {"page/age"})
        self.assertEqual(supervise.health_kinds(
            "advisory: tools.model_rates has failed on every run for 4h00m | advisory: tracker.gs_passive has failed"),
            {"advisory/tools.model_rates", "advisory/tracker.gs_passive"})

    def test_one_account_recovering_under_an_open_meters_incident_is_reported(self):
        sent, ups = [], []
        dave = "meters: newest usable reading in claude-usage-meter-dave.log is 1h old (limit 15m)"
        avis = "meters: newest usable reading in claude-usage-meter-avis.log is 1h old (limit 15m)"
        st = {"consecutive_failures": 0}
        supervise.decide_alert(st, 0, f"{dave} | {avis}", 2, 0.0, lambda m: sent.append(m) or True, "gs",
                               send_up=lambda m: ups.append(m) or True)
        self.assertEqual(st["incident_kinds"], ["meters/avis", "meters/dave"])
        supervise.decide_alert(st, 60, avis, 2, 0.0, lambda m: sent.append(m) or True, "gs",
                               send_up=lambda m: ups.append(m) or True)
        self.assertIn("meters/dave recovered", sent[-1])
        self.assertEqual(ups, [], "Kuma must stay down while avis still fails")
        self.assertEqual(st["incident_kinds"], ["meters/avis"])
        supervise.decide_alert(st, 120, None, 2, 0.0, lambda m: sent.append(m) or True, "gs",
                               send_up=lambda m: ups.append(m) or True)
        self.assertEqual(len(ups), 1)
        self.assertNotIn("incident_open", st)

    def test_a_legacy_meters_kind_covers_its_accounts(self):
        sent = []
        st = {"consecutive_failures": 0, "incident_open": True, "incident_since": 0, "incident_kinds": ["meters"],
              "unhealthy_since": 0}
        supervise.decide_alert(st, 60, "meters: 25 of the last 40 lines in claude-usage-meter-jwork.log are 429s",
                               2, 0.0, lambda m: sent.append(m) or True, "gs")
        self.assertEqual(sent, [], "an incident opened before subjects existed sent a spurious message")

    def test_timeout_kills_the_whole_process_group(self):
        import tempfile as _tf
        import time as _t
        d = Path(_tf.mkdtemp())
        pidf = d / "child.pid"
        code = supervise.run_with_retry(
            ["bash", "-c", f"sleep 30 & echo $! > {pidf}; wait"], None, 0, 0, timeout=1)
        self.assertEqual(code, 124)
        pid = int(pidf.read_text())
        deadline = _t.time() + 5
        while _t.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                break
            _t.sleep(0.05)
        else:
            self.fail("the job's grandchild outlived the timeout")

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
        to = self.tmp / "whatsapp-to"
        to.write_text("15550001111\n")
        for out, code, want in (("OK\n", 0, True), ("FAIL\n", 0, False), ("", 255, False)):
            with mock.patch.object(supervise, "whatsapp_to_file", return_value=to), \
                 mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, out, "")) as r:
                self.assertEqual(REAL_WA(out and "msg"), want)
                self.assertEqual(r.call_args.kwargs["input"], out and "msg")
                self.assertTrue(r.call_args.args[0][-1].endswith("wa_send.py 15550001111"))

    def test_whatsapp_recipient_comes_from_config_outside_the_repo(self):
        import subprocess
        to = self.tmp / "whatsapp-to"
        self.assertIsNone(supervise.whatsapp_recipient(to))  # missing
        for bad in ("", "  \n", "not-a-number", "123; rm -rf ~", "1" * 20):
            to.write_text(bad)
            self.assertIsNone(supervise.whatsapp_recipient(to), bad)
        to.write_text("+15550001111\n")
        self.assertEqual(supervise.whatsapp_recipient(to), "+15550001111")
        with mock.patch.object(supervise, "whatsapp_to_file", return_value=self.tmp / "absent"), \
             mock.patch.object(subprocess, "run") as r:
            self.assertFalse(REAL_WA("msg"))
            r.assert_not_called()
        self.assertTrue(str(supervise.whatsapp_to_file()).endswith(".config/claude-usage-tracker/whatsapp-to"))

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
        self.assertEqual(kw["health_cfg"].checks, ("run", "page", "meters", "lock", "sizes", "advisory"))
        self.assertEqual(kw["health_cfg"].page_json, health.GS_SITE / health.SITE_JSON)
        self.assertEqual(kw["health_cfg"].advisory_state, health.GS_ADVISORY)
        self.assertEqual(kw["kuma_file"], supervise.kuma_url_file())

    def test_masterrig_defaults_unchanged(self):
        kw = self.captured("--kuma-url-file", "/x/url")
        self.assertEqual((kw["retries"], kw["fail_threshold"], kw["label"]), (2, 3, "masterrig"))
        # The live page: a bare Config (as tests build) leaves it out.
        cfg = kw["health_cfg"]
        self.assertEqual(cfg.page_url, health.LIVE_PAGE_URL)
        self.assertEqual(cfg.checks, health.MASTERRIG_CHECKS)
        self.assertEqual(cfg.meter_logs, (health.GS_OPS / "claude-usage-meter-a1.log",))
        self.assertEqual(cfg.state, supervise.ROOT / ".supervise-state.json")
        self.assertEqual(cfg.lock_pidfile, supervise.ROOT / ".supervise.pid")
        self.assertEqual(kw["lock"], supervise.ROOT / ".supervise.lock")
        self.assertEqual(kw["kuma_file"], Path("/x/url"))


if __name__ == "__main__":
    unittest.main()
