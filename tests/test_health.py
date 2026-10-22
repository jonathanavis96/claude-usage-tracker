"""tracker.health: each check passes on fresh fixtures and names its failure otherwise."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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
                "--credentials", str(self.creds), "--pidfile", str(self.c.lock_pidfile), "--skip", "publisher",
                "--page-url", ""]
        self.assertEqual(health.main(args), 0)
        self.log.unlink()
        self.assertEqual(health.main(args), 1)

    def test_the_masterrig_cli_checks_the_live_page_by_default(self):
        c = health.config_from_args(health.build_parser().parse_args([]))
        self.assertEqual(c.page_url, health.LIVE_PAGE_URL)
        c = health.config_from_args(health.build_parser().parse_args(["--page-url", ""]))
        self.assertIsNone(c.page_url)

    def test_a_withheld_publish_fails_the_run_check_at_once(self):
        # bin/daily.sh exit 9: the run finished 30 min ago, so the age rule read it as healthy.
        self.state.write_text(json.dumps({"last_ok": NOW - 1800, "last_fail": NOW - 60, "last_exit": 9}))
        self.assertIn("run: bin/daily.sh withheld the public JSON (exit 9), last successful run 30m ago",
                      self.reasons()["run"])
        self.state.write_text(json.dumps({"last_ok": NOW - 30, "last_fail": NOW - 60, "last_exit": 0}))
        self.assertIsNone(self.reasons()["run"])

    def test_masterrig_reads_no_ceiling_feed(self):
        # The ceiling log stopped on 2026-10-18 (mis-usage-ceiling.service: not-found) and
        # a1's meter log replaces it (fix/a1-meter-feed's `meters` check). A collection
        # check on the dead log failed forever and, first in the order, hid every other
        # masterrig check (review of 359b7c1).
        self.assertFalse(hasattr(health, "CEILING_LOG"))
        self.assertNotIn("--ceiling-log", health.build_parser().format_help())
        self.assertIsNone(health.check_collection(self.c, NOW))

    # -- the page readers get (masterrig: the live URL) ----------------------------------

    def page(self, body=None, error=None):
        def fetch(url, timeout):
            if error:
                raise error
            return json.dumps(body).encode()
        p = mock.patch.object(health, "fetch_page", fetch)
        p.start()
        self.addCleanup(p.stop)
        self.c.page_url = "https://example.invalid/data/claude-usage.json"
        self.c.page_fetch_state = self.tmp / "page-fetch.json"

    def test_a_fresh_live_page_passes_and_a_stale_one_fails(self):
        self.page({"generated_at": iso(NOW - 1800)})
        self.assertIsNone(self.reasons()["page"])
        mock.patch.stopall()
        # The live page on 2026-10-22: 2026-10-13T10:31:29Z, nine days old.
        self.page({"generated_at": "2026-10-13T10:31:29.045996Z"})
        reason = health.check_page(self.c, datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc).timestamp())
        self.assertIn("was generated 225h28m ago (limit 2h00m): the page is not updating", reason)

    def test_a_failing_fetch_waits_out_its_grace_then_is_its_own_reason(self):
        import urllib.error
        self.page(error=urllib.error.URLError("no route"))
        self.assertIsNone(health.check_page(self.c, NOW), "the first failure is inside the grace")
        self.assertIsNone(health.check_page(self.c, NOW + 3600))
        reason = health.check_page(self.c, NOW + 2 * 3600 + 60)
        self.assertIn("page: cannot fetch https://example.invalid/data/claude-usage.json for 2h01m", reason)
        mock.patch.stopall()
        # One good fetch clears the stamp: the next failure starts a new grace.
        self.page({"generated_at": iso(NOW + 3 * 3600)})
        self.assertIsNone(health.check_page(self.c, NOW + 3 * 3600))
        self.assertFalse(self.c.page_fetch_state.exists())
        mock.patch.stopall()
        self.page(error=urllib.error.URLError("no route"))
        self.assertIsNone(health.check_page(self.c, NOW + 4 * 3600))

    def test_a_bare_config_reads_no_page(self):
        self.assertIsNone(health.check_page(health.Config(), NOW))


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
        self.site = self.tmp / "site"
        self.publish(NOW - 600)
        self.c = health.gs_config(state=self.state, pidfile=self.ops / "x.pid", ops=self.ops, repo=self.tmp,
                                  site=self.site)

    def publish(self, generated_at):
        page = self.site / health.SITE_JSON
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(json.dumps({"generated_at": iso(generated_at)}))

    def uses(self, *accounts):
        """The publisher's stretch record holds these accounts (history/gs-passive.json)."""
        (self.tmp / "history").mkdir(exist_ok=True)
        (self.tmp / "history" / "gs-passive.json").write_text(json.dumps({"accounts": {
            a: {"account": a, "meter": {"log": f"/home/x/.paperclip/ops/claude-usage-meter-{a}.log"}}
            for a in accounts}}))

    def write_meter(self, account, ts, gap_after=0):
        rows = [{"ts": iso(ts), "account": account, "five_hour": {"utilization": 1.0}}]
        rows += [{"ts": iso(ts + 60 * (i + 1)), "account": account, "error": "HTTP 429", "reason": "rate_limited"}
                 for i in range(gap_after)]
        (self.ops / f"claude-usage-meter-{account}.log").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_fresh_gs_is_healthy_and_skips_masterrig_checks(self):
        names = [n for n, _ in health.run_checks(self.c, NOW)]
        self.assertEqual(names, ["run", "page", "lock", "sizes", "meters", "advisory"])
        self.assertIsNone(health.first_failure(self.c, NOW))

    def test_masterrig_default_does_not_run_meters(self):
        names = [n for n, _ in health.run_checks(health.Config(), NOW)]
        self.assertNotIn("meters", names)
        self.assertNotIn("advisory", names)

    # -- the published JSON's age --------------------------------------------------------

    def test_a_page_withheld_past_two_hours_fails(self):
        # The withheld page put back the 2026-10-13T10:31Z JSON on every run for nine days.
        self.publish(NOW - 2 * 3600 - 60)
        self.assertIn("page: the published JSON in the site checkout (claude-usage.json) was generated "
                      "2h01m ago (limit 2h00m)", health.first_failure(self.c, NOW))
        self.publish(NOW - 3600)
        self.assertIsNone(health.first_failure(self.c, NOW))

    def test_an_unreadable_page_fails(self):
        (self.site / health.SITE_JSON).write_text("{not json")
        self.assertIn("page: cannot read the published JSON", health.first_failure(self.c, NOW))

    def test_a_withheld_run_is_named_before_the_page_it_left_stale(self):
        self.publish(NOW - 5 * 3600)
        self.state.write_text(json.dumps({"last_ok": NOW - 5 * 3600, "last_fail": NOW - 60, "last_exit": 9}))
        self.assertTrue(health.first_failure(self.c, NOW).startswith("run: bin/daily.sh withheld the public JSON"))

    # -- a blind meter on an account the publisher uses ------------------------------------

    def test_a_used_accounts_idle_lapse_is_blind_after_an_hour(self):
        # An idle lapse (only auth_expired lines after the last reading) passed for 24 h; jwork
        # read nothing for 41.7 h on 19-20 Oct.
        self.uses("dave", "jwork", "avis")
        self._idle("dave", 20 * 86400, 50 * 60)
        self.assertIsNone(health.first_failure(self.c, NOW), "inside the hour")
        self._idle("dave", 20 * 86400, 3 * 3600)
        reason = health.first_failure(self.c, NOW)
        self.assertIn("meters: dave is blind, no reading in claude-usage-meter-dave.log for 3h00m (limit 1h00m; "
                      "token lapsed (idle)", reason)
        self.assertIn("the publisher uses this account", reason)

    def test_an_account_the_publisher_does_not_use_keeps_the_idle_grace(self):
        self.uses("jwork", "avis")
        self._idle("dave", 20 * 86400, 3 * 3600)
        self.assertIsNone(health.first_failure(self.c, NOW))

    def test_a_used_accounts_held_header_probe_is_blind_after_an_hour(self):
        # dave, 2026-10-22 01:09-06:34Z: the probe held while masterrig used dave for 3,238 messages.
        self.uses("dave")
        self.write_header_meter(NOW - 50 * 60, five_reset=NOW - 3600)
        self.assertIsNone(health.first_failure(self.c, NOW))
        self.write_header_meter(NOW - 5 * 3600 - 25 * 60, five_reset=NOW - 3600)
        self.assertIn("meters: dave is blind, no reading in claude-usage-meter-dave.log for 5h25m "
                      "(limit 1h00m; header probe held", health.first_failure(self.c, NOW))

    def test_a_used_account_honouring_a_long_retry_after_is_blind_after_an_hour(self):
        self.uses("jwork")
        rows = [{"ts": iso(NOW - 3700), "account": "jwork", "five_hour": {"utilization": 1.0}},
                {"ts": iso(NOW - 3690), "account": "jwork", "error": "HTTP 429", "reason": "rate_limited",
                 "retry_after_s": 7200}]
        (self.ops / "claude-usage-meter-jwork.log").write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIn("jwork is blind", health.first_failure(self.c, NOW))

    def test_the_used_accounts_come_from_the_record_by_label_or_meter_log(self):
        self.uses("a2")   # a label the log name does not carry: matched by its meter log path
        (self.tmp / "history" / "gs-passive.json").write_text(json.dumps({"accounts": {
            "a2": {"meter": {"log": "/srv/ops/claude-usage-meter-dave.log"}}}}))
        self.assertEqual(health._publisher_accounts(self.c, self.ops / "claude-usage-meter-dave.log"),
                         {"a2", "dave"})
        (self.tmp / "history" / "gs-passive.json").write_text("{not json")
        self.assertEqual(health._publisher_accounts(self.c, self.ops / "claude-usage-meter-dave.log"), set())

    # -- advisory steps of bin/daily.sh --------------------------------------------------------

    def test_an_advisory_step_failing_past_its_grace_fails_by_name(self):
        from tracker import advisory
        state = self.ops / health.GS_ADVISORY.name
        advisory.record(state, "tracker.list_prices", 0, NOW - 5 * 3600)
        # The refit crashed on every run from 2026-10-13T19:30Z.
        for k in range(10, -1, -1):
            advisory.record(state, "tools.model_rates", 1, NOW - k * 1800)
        self.assertIn("advisory: tools.model_rates has failed on every run for 5h00m (limit 3h00m, last exit 1)",
                      health.first_failure(self.c, NOW))
        advisory.record(state, "tools.model_rates", 0, NOW)
        self.assertIsNone(health.first_failure(self.c, NOW))

    def test_all_failures_names_the_advisory_step_behind_a_withheld_run(self):
        # The supervisor read first_failure: while `run` failed (the page withheld), the
        # refit crashing for days behind it never reached an alert.
        from tracker import advisory
        state = self.ops / health.GS_ADVISORY.name
        for k in range(10, -1, -1):
            advisory.record(state, "tools.model_rates", 1, NOW - k * 1800)
        self.state.write_text(json.dumps({"last_ok": NOW - 5 * 3600, "last_fail": NOW - 60, "last_exit": 9}))
        self.publish(NOW - 5 * 3600)
        first = health.first_failure(self.c, NOW)
        every = health.all_failures(self.c, NOW, skip=("lock",))
        self.assertNotIn("advisory", first)
        parts = every.split(" | ")
        self.assertEqual([p.split(":")[0] for p in parts], ["run", "page", "advisory"])
        self.assertTrue(parts[2].startswith("advisory: tools.model_rates has failed on every run"))
        advisory.record(state, "tools.model_rates", 0, NOW)
        self.state.write_text(json.dumps({"last_ok": NOW - 60}))
        self.publish(NOW - 60)
        self.assertIsNone(health.all_failures(self.c, NOW))

    def test_an_advisory_step_inside_its_grace_or_no_longer_run_passes(self):
        from tracker import advisory
        state = self.ops / health.GS_ADVISORY.name
        advisory.record(state, "tracker.gs_passive", 2, NOW - 2 * 3600)
        advisory.record(state, "tracker.gs_passive", 2, NOW)
        self.assertIsNone(health.first_failure(self.c, NOW), "two hours is inside the grace")
        self.assertIsNone(health.check_advisory(self.c, NOW + 27 * 3600), "not run for a day: not failing")

    def test_gap_lines_do_not_count_as_a_reading(self):
        # dave's last usable read is 20 min old though 429 gaps were logged since.
        self.write_meter("dave", NOW - 1200, gap_after=19)
        self.assertIn("meters: newest usable reading in claude-usage-meter-dave.log is 20m old",
                      health.first_failure(self.c, NOW))

    def write_header_meter(self, ts, five_reset, seven=22.0, seven_reset=NOW + 86400, trailing=()):
        rows = [{"ts": iso(ts), "account": "dave", "source": "ratelimit_headers",
                 "five_hour": {"utilization": 5.0, "resets_at": iso(five_reset)},
                 "seven_day": {"utilization": seven, "resets_at": iso(seven_reset)}}]
        rows += list(trailing)
        (self.ops / "claude-usage-meter-dave.log").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_a_held_header_probe_is_not_stale(self):
        # Window lapsed 2 h ago and meter_log holds its probe until a session runs: no lines, no failure.
        self.write_header_meter(NOW - 3 * 3600, five_reset=NOW - 2 * 3600)
        self.assertIsNone(health.first_failure(self.c, NOW))
        # Seven-day meter at 100% until tomorrow: held as well.
        self.write_header_meter(NOW - 3 * 3600, five_reset=NOW + 3600, seven=100.0)
        self.assertIsNone(health.first_failure(self.c, NOW))

    def test_a_header_probe_in_a_live_window_still_goes_stale(self):
        self.write_header_meter(NOW - 1800, five_reset=NOW + 3600)
        self.assertIn("claude-usage-meter-dave.log is 30m old", health.first_failure(self.c, NOW))

    def test_a_rejected_setup_token_is_named(self):
        gaps = [{"ts": iso(NOW - 1200 + 300 * i), "account": "dave", "error": "TokenRejected: 401",
                 "reason": "token_rejected"} for i in range(4)]
        self.write_header_meter(NOW - 1800, five_reset=NOW - 600, trailing=gaps)
        self.assertIn("setup token was refused (401)", health.first_failure(self.c, NOW))

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
