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
        self.log.write_text(json.dumps({"ts": iso(NOW - 3000)}) + "\n" + json.dumps({"ts": iso(NOW - 60)}) + "\n")
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
        self.log.write_text(json.dumps({"ts": iso(NOW - 2 * 3600)}) + "\n")
        self.assertIn("collection: newest meter sample is 2h00m old", self.reasons()["collection"])

    def test_resets_at_style_offsets_are_parsed_not_string_compared(self):
        self.log.write_text(json.dumps({"ts": "2026-10-02T03:59:00+02:00"}) + "\n")
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

    def test_meter_log_allows_a_skipped_tick(self):
        meter = self.tmp / "meter-dave.log"
        self.c.meter_logs = (meter,)
        meter.write_text(json.dumps({"ts": iso(NOW - 130)}) + "\n")
        self.assertIsNone(self.reasons()["meter"], "reads ~130 s apart are normal under the 110 s spacing")
        meter.write_text(json.dumps({"ts": iso(NOW - 400)}) + "\n")
        self.assertIn("meter: newest line in meter-dave.log is 6m old", self.reasons()["meter"])
        meter.write_text(json.dumps({"ts": iso(NOW - 400), "reason": "rate_limited", "retry_after_s": 300}) + "\n")
        self.assertIsNone(self.reasons()["meter"], "a 429 gap line extends the limit by its Retry-After")
        rows = [{"ts": iso(NOW - 60 * i), "reason": "rate_limited", "retry_after_s": 0} for i in range(50, 0, -1)]
        rows[1::2] = [{"ts": r["ts"]} for r in rows[1::2]]
        rows += [{"ts": iso(NOW - 5 + i), "reason": "rate_limited", "retry_after_s": 0} for i in range(5)]
        meter.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIn("30 of the last 55 lines", self.reasons()["meter"] or "")
        rows = [{"ts": iso(NOW - 60 * i)} for i in range(40, 0, -1)]
        rows[::4] = [{**r, "reason": "rate_limited", "retry_after_s": 0} for r in rows[::4]]
        meter.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIsNone(self.reasons()["meter"], "a 429 in four is not a storm")
        self.c.meter_logs = ()
        self.assertIsNone(self.reasons()["meter"], "no meter logs configured, nothing to check")

    def test_stale_publisher(self):
        self.c.publisher_max_age_s = 300
        self.assertIn("publisher: history/gs-passive.json last committed 10m ago", self.reasons()["publisher"])

    def test_token_states(self):
        self.creds.write_text("not json")
        self.assertIn("unreadable", self.reasons()["token"])
        self.creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": int((NOW - 13 * 3600) * 1000)}}))
        self.assertIn("access token expired 13h00m ago", self.reasons()["token"])
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
        self.log.write_text(json.dumps({"ts": iso(now - 60)}) + "\n")
        self.state.write_text(json.dumps({"last_ok": now - 60}))
        self.creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": int((now + 3600) * 1000)}}))
        args = ["--usage-log", str(self.log), "--state", str(self.state), "--repo", str(self.repo),
                "--credentials", str(self.creds), "--pidfile", str(self.c.lock_pidfile), "--skip", "publisher"]
        self.assertEqual(health.main(args), 0)
        self.log.unlink()
        self.assertEqual(health.main(args), 1)


if __name__ == "__main__":
    unittest.main()
