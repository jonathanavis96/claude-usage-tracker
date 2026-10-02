"""tracker.health: each check passes on fresh fixtures and names its failure otherwise."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tracker import health

NOW = datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / "history").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "history" / "gs-passive.json").write_text("{}")
        env = {**os.environ, "GIT_COMMITTER_DATE": f"@{int(NOW - 600)} +0000", "GIT_AUTHOR_DATE": f"@{int(NOW - 600)} +0000"}
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "s"],
                       check=True, env=env)
        self.log = self.tmp / "usage_log.jsonl"
        self.log.write_text(json.dumps({"ts": iso(NOW - 3000), "five_hour": {"utilization": 1}}) + "\n" + json.dumps({"ts": iso(NOW - 60), "five_hour": {"utilization": 1}}) + "\n")
        self.state = self.tmp / "state.json"
        self.state.write_text(json.dumps({"last_ok": NOW - 120}))
        self.creds = self.tmp / "creds.json"
        self.creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": int((NOW + 3600) * 1000),
                                                            "refreshTokenExpiresAt": int((NOW + 86400) * 1000)}}))
        self.c = health.Config(usage_log=self.log, state=self.state, repo=self.repo, credentials=self.creds,
                               lock_pidfile=self.tmp / "pid", history_dir=self.repo / "history",
                               logs=(self.tmp / "run.log",))

    def reasons(self):
        return dict(health.run_checks(self.c, NOW))

    def test_all_fresh_is_healthy(self):
        self.assertEqual({k: v for k, v in self.reasons().items() if v}, {})
        self.assertIsNone(health.first_failure(self.c, NOW))

    def test_stale_collection(self):
        self.log.write_text(json.dumps({"ts": iso(NOW - 2 * 3600), "five_hour": {"utilization": 1}}) + "\n")
        self.assertIn("collection: newest meter sample is 2h00m old", self.reasons()["collection"])

    def test_resets_at_style_offsets_are_parsed_not_string_compared(self):
        self.log.write_text(json.dumps({"ts": "2026-10-02T03:59:00+02:00", "five_hour": {"utilization": 1}}) + "\n")
        self.assertIsNone(self.reasons()["collection"])

    def test_garbage_tail_falls_back_to_last_good_line(self):
        with open(self.log, "a") as f:
            f.write("{truncated\n")
        self.assertIsNone(self.reasons()["collection"])

    def test_missing_usage_log(self):
        self.log.unlink()
        self.assertIn("cannot read", self.reasons()["collection"])

    def test_no_run_recorded_and_stale_run(self):
        self.state.unlink()
        self.assertIn("no successful supervised run", self.reasons()["run"])
        self.state.write_text(json.dumps({"last_ok": NOW - 3 * 3600, "last_reason": "exit 1"}))
        self.assertIn("last failure: exit 1", self.reasons()["run"])

    def test_off_main_exit_3_fails_the_run_check(self):
        self.state.write_text(json.dumps({"last_ok": NOW - 3600, "last_fail": NOW - 60, "last_exit": 3}))
        self.assertIn("off main (exit 3)", self.reasons()["run"])
        self.state.write_text(json.dumps({"last_ok": NOW - 30, "last_fail": NOW - 60, "last_exit": 0}))
        self.assertIsNone(self.reasons()["run"])

    def test_meter_log_429_storm(self):
        meter = self.tmp / "meter-dave.log"
        self.c.meter_logs = (meter,)
        self.c.checks = ("meters",)
        good = {"five_hour": {"utilization": 1}}
        rows = [{"ts": iso(NOW - 60 * i), **good} for i in range(50, 0, -1)]
        rows[::2] = [{"ts": r["ts"], "reason": "rate_limited", "retry_after_s": 0} for r in rows[::2]]
        rows += [{"ts": iso(NOW - 5 + i), "reason": "rate_limited", "retry_after_s": 0} for i in range(5)]
        meter.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIn("30 of the last 55 lines", self.reasons()["meters"] or "")
        rows = [{"ts": iso(NOW - 130 * i), **good} for i in range(40, 0, -1)]
        rows[::4] = [{"ts": r["ts"], "reason": "rate_limited", "retry_after_s": 0} for r in rows[::4]]
        meter.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIsNone(self.reasons()["meters"], "a 429 in four, reads 130 s apart: healthy")

    def test_publisher_reads_the_fetched_upstream_not_only_head(self):
        # masterrig fetches hourly but pulls daily: gs's newer commit is only on origin/main.
        up = self.tmp / "upstream"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(up)], check=True)
        (up / "history" / "gs-passive.json").write_text("{\"n\": 1}")
        env = {**os.environ, "GIT_COMMITTER_DATE": f"@{int(NOW - 60)} +0000", "GIT_AUTHOR_DATE": f"@{int(NOW - 60)} +0000"}
        subprocess.run(["git", "-C", str(up), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "p"],
                       check=True, env=env)
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(clone)], check=True)
        subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", str(up)], check=True)
        subprocess.run(["git", "-C", str(clone), "fetch", "-q", "origin"], check=True)
        self.c.repo = clone
        self.c.publisher_max_age_s = 300
        self.assertIsNone(self.reasons()["publisher"])

    def test_failing_site_push_fails_the_publisher_check_until_it_recovers(self):
        def commit(doc):
            (self.repo / "history" / "site-push.json").write_text(json.dumps(doc))
            subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                            "commit", "-qm", "s"], check=True)
        commit({"ok": False, "at": "2026-10-02T01:30:00Z", "last_ok": "2026-10-01T20:00:00Z"})
        self.assertIn("site push is failing", self.reasons()["publisher"] or "")
        commit({"ok": True, "at": "2026-10-02T02:00:00Z", "last_ok": "2026-10-02T02:00:00Z"})
        self.assertIsNone(self.reasons()["publisher"])

    def test_schema_change_on_the_newest_reading_fails_meters(self):
        meter = self.tmp / "meter-dave.log"
        self.c.meter_logs = (meter,)
        self.c.checks = ("meters",)
        meter.write_text(json.dumps({"ts": iso(NOW - 60), "five_hour": {"utilization": 3},
                                     "schema": ["seven_day.resets_at missing"]}) + "\n")
        self.assertIn("usage API changed shape", self.reasons()["meters"] or "")

    def test_stale_publisher(self):
        self.c.publisher_max_age_s = 300
        self.assertIn("publisher: history/gs-passive.json last committed 10m ago", self.reasons()["publisher"])

    def test_token_states(self):
        self.creds.write_text("not json")
        self.assertIn("unreadable", self.reasons()["token"])
        self.creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": int((NOW - 13 * 3600) * 1000)}}))
        self.assertIn("access token expired 13h00m ago", self.reasons()["token"])
        self.creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": int((NOW - 72 * 3600) * 1000),
                                                            "refreshTokenExpiresAt": int((NOW + 86400) * 1000)}}))
        self.assertIsNone(self.reasons()["token"], "three idle days with a live refresh token are not a failure")
        self.creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": int((NOW - 3600) * 1000)}}))
        self.assertIsNone(self.reasons()["token"], "an idle hour inside the grace is not a failure")
        self.creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": int(NOW * 1000),
                                                            "refreshTokenExpiresAt": int((NOW - 1) * 1000)}}))
        self.assertIn("refresh token expired", self.reasons()["token"])

    def test_stale_lock_dead_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        self.c.lock_pidfile.write_text(str(p.pid))
        self.assertIn("stale lock", self.reasons()["lock"])

    def test_live_lock_held_too_long(self):
        self.c.lock_pidfile.write_text(str(os.getpid()))
        os.utime(self.c.lock_pidfile, (NOW - 3 * 3600, NOW - 3 * 3600))
        self.assertIn("has held the lock 3h00m", self.reasons()["lock"])

    def test_sizes(self):
        self.c.max_log_bytes = 10
        (self.tmp / "run.log").write_text("x" * 2048)
        self.assertIn("run.log is 2 KiB", self.reasons()["sizes"])
        self.c.max_log_bytes = 10**6
        self.c.max_history_bytes = 1
        self.assertIn("history/", self.reasons()["sizes"])

    def test_cli_exit_codes(self):
        import time
        now = time.time()
        self.log.write_text(json.dumps({"ts": iso(now - 60), "five_hour": {"utilization": 1}}) + "\n")
        self.state.write_text(json.dumps({"last_ok": now - 60}))
        self.creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": int((now + 3600) * 1000)}}))
        args = ["--usage-log", str(self.log), "--state", str(self.state), "--repo", str(self.repo),
                "--credentials", str(self.creds), "--pidfile", str(self.c.lock_pidfile), "--skip", "publisher"]
        self.assertEqual(health.main(args), 0)
        self.log.unlink()
        self.assertEqual(health.main(args), 1)


if __name__ == "__main__":
    unittest.main()


class GsProfileTest(unittest.TestCase):
    """The gs profile: the supervised publisher run plus the three meter timers' logs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ops = self.tmp / "ops"
        self.ops.mkdir()
        self.state = self.ops / "state.json"
        self.state.write_text(json.dumps({"last_ok": NOW - 120}))
        for a in health.GS_METER_ACCOUNTS:
            self.write_meter(a, NOW - 60)
        self.c = health.gs_config(state=self.state, pidfile=self.ops / "x.pid", ops=self.ops, repo=self.tmp)

    def write_meter(self, account, ts, gap_after=0):
        rows = [{"ts": iso(ts), "account": account, "five_hour": {"utilization": 1.0}}]
        rows += [{"ts": iso(ts + 60 * (i + 1)), "account": account, "error": "HTTP 429", "reason": "rate_limited"}
                 for i in range(gap_after)]
        (self.ops / f"claude-usage-meter-{account}.log").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_fresh_gs_is_healthy_and_skips_masterrig_checks(self):
        names = [n for n, _ in health.run_checks(self.c, NOW)]
        self.assertEqual(names, ["run", "lock", "sizes", "meters"])
        self.assertIsNone(health.first_failure(self.c, NOW))

    def test_masterrig_default_does_not_run_meters(self):
        self.assertNotIn("meters", [n for n, _ in health.run_checks(health.Config(), NOW)])

    def test_gap_lines_do_not_count_as_a_reading(self):
        # dave's last usable read is 20 min old though 429 gaps were logged since.
        self.write_meter("dave", NOW - 1200, gap_after=19)
        self.assertIn("meters: newest usable reading in claude-usage-meter-dave.log is 20m old",
                      health.first_failure(self.c, NOW))

    def test_routine_429s_between_reads_stay_healthy(self):
        self.write_meter("avis", NOW - 300, gap_after=4)
        self.assertIsNone(health.first_failure(self.c, NOW))

    def test_missing_meter_log_fails(self):
        (self.ops / "claude-usage-meter-jwork.log").unlink()
        self.assertIn("meters: cannot read claude-usage-meter-jwork.log", health.first_failure(self.c, NOW))

    def test_every_failing_meter_log_is_named(self):
        self.write_meter("dave", NOW - 1200)
        self.write_meter("avis", NOW - 1800)
        reason = health.first_failure(self.c, NOW)
        self.assertIn("claude-usage-meter-avis.log", reason)
        self.assertIn("claude-usage-meter-dave.log", reason)
        self.assertEqual(reason.count(" | "), 1)

    def test_an_honoured_retry_after_extends_the_age_limit(self):
        rows = [{"ts": iso(NOW - 1800), "account": "jwork", "five_hour": {"utilization": 1.0}},
                {"ts": iso(NOW - 1790), "account": "jwork", "error": "HTTP 429", "reason": "rate_limited",
                 "retry_after_s": 3600}]
        log = self.ops / "claude-usage-meter-jwork.log"
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIsNone(health.first_failure(self.c, NOW))
        rows[1]["retry_after_s"] = 600
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIn("jwork", health.first_failure(self.c, NOW))

    def _idle(self, account, refresh_in_s, read_ago_s):
        cfg = self.tmp / f"cfg-{account}"
        cfg.mkdir(exist_ok=True)
        (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "x", "expiresAt": int((NOW - 3600) * 1000),
            "refreshTokenExpiresAt": int((NOW + refresh_in_s) * 1000)}}))
        self.c.meter_config_dirs = {account: cfg}
        rows = [{"ts": iso(NOW - read_ago_s), "account": account, "five_hour": {"utilization": 1.0}}]
        rows += [{"ts": iso(NOW - read_ago_s + 120 * (i + 1)), "account": account, "error": "AuthExpired",
                  "reason": "auth_expired"} for i in range(5)]
        (self.ops / f"claude-usage-meter-{account}.log").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_idle_token_lapse_passes_until_the_grace_ends(self):
        self._idle("dave", 20 * 86400, 3 * 3600)
        self.assertIsNone(health.first_failure(self.c, NOW))
        self._idle("dave", 20 * 86400, 25 * 3600)
        self.assertIn("token lapsed (idle)", health.first_failure(self.c, NOW))

    def test_idle_token_lapse_keeps_failing_after_the_reading_leaves_the_scan(self):
        # meter_log writes an auth_expired line about every 110 s. After ~41 h the last
        # reading is out of the 256 KB tail health scans; the check must still fail.
        account = "dave"
        cfg = self.tmp / f"cfg-{account}"
        cfg.mkdir(exist_ok=True)
        (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "x", "expiresAt": int((NOW - 3600) * 1000),
            "refreshTokenExpiresAt": int((NOW + 300 * 86400) * 1000)}}))
        self.c.meter_config_dirs = {account: cfg}
        log = self.ops / f"claude-usage-meter-{account}.log"
        for hours in (3, 25, 40, 48, 60, 120, 240):
            with self.subTest(hours=hours):
                start = NOW - hours * 3600
                rows = [{"ts": iso(start), "account": account, "five_hour": {"utilization": 1.0}}]
                rows += [{"ts": iso(t), "account": account, "error": "AuthExpired: token expired, sign in again",
                          "reason": "auth_expired"} for t in range(int(start) + 110, int(NOW), 110)]
                log.write_text("".join(json.dumps(r) + "\n" for r in rows))
                if hours >= 120:
                    self.assertGreater(log.stat().st_size, 262144, "the reading must be outside the scan")
                reason = health.first_failure(self.c, NOW)
                if hours < 24:
                    self.assertIsNone(reason)
                else:
                    self.assertIsNotNone(reason)
                    self.assertIn("claude-usage-meter-dave.log", reason)

    def test_expired_refresh_token_needs_a_login(self):
        self._idle("dave", -3600, 3 * 3600)
        self.assertIn("needs a login", health.first_failure(self.c, NOW))

    def test_publisher_run_is_held_to_thirty_minutes(self):
        self.state.write_text(json.dumps({"last_ok": NOW - 3700, "last_reason": "exit 1 after 1 attempts"}))
        self.assertIn("run: last successful run 1h01m ago", health.first_failure(self.c, NOW))
