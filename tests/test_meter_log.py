import hashlib
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from tracker.meter_log import NO_RETRY_FETCH, account_identity, main, sample
from tracker.samples import parse_meter_log, parse_moonlighter
from tracker.weekly import parse_row

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "deploy" / "systemd"
NOW = datetime(2026, 9, 15, 21, 0, 3, tzinfo=timezone.utc)
BODY = {"five_hour": {"utilization": 12.0, "resets_at": "2026-09-15T23:00:00.4+00:00"},
        "seven_day": {"utilization": 30.0, "resets_at": "2026-09-17T23:00:00.9+00:00"}}
TOKEN = "SECRET-TOKEN-never-logged"


def config_dir(root: Path, uuid: str | None = "uuid-dave") -> Path:
    cfg = root / "cfg"
    cfg.mkdir()
    doc = {"oauthAccount": {"accountUuid": uuid}} if uuid else {}
    (cfg / ".claude.json").write_text(json.dumps(doc))
    (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN}}))
    return cfg


class IdentityTests(unittest.TestCase):
    def test_identity_is_a_short_hash_of_the_account_uuid_not_the_uuid(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = config_dir(Path(d))
            self.assertEqual(account_identity(cfg), hashlib.sha256(b"uuid-dave").hexdigest()[:12])

    def test_a_signed_out_config_dir_has_no_identity(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(account_identity(config_dir(Path(d), uuid=None)))


class SampleTests(unittest.TestCase):
    def test_one_reading_appends_one_line_the_existing_parsers_read(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "ops" / "meter.log"
            rc = sample(cfg, "dave", log, fetch=lambda url, headers: BODY, now=lambda: NOW)
            self.assertEqual(rc, 0)
            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertEqual((row["account"], row["identity"]), ("dave", account_identity(cfg)))
            # moonlighter's usage_log.jsonl shape, so the passive join and the weekly pairing read it unchanged
            self.assertEqual(parse_moonlighter(lines)[0].five_hour, 12.0)
            self.assertIsNotNone(parse_row(row))
            self.assertEqual(parse_meter_log(lines)[0].resets_at, "2026-09-15T23:00:00.4+00:00")

    def test_a_failed_read_is_logged_without_the_token_and_exits_4(self):
        def fail(url, headers):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
            self.assertEqual(sample(cfg, "dave", log, fetch=fail, now=lambda: NOW), 4)
            text = log.read_text()
            self.assertIn("429", json.loads(text)["error"])
            self.assertNotIn(TOKEN, text)
            self.assertEqual(parse_meter_log(text.splitlines()), [])

    def test_refuses_to_write_a_second_account_into_one_log(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
            other = json.dumps({"ts": "2026-09-15T20:55:00+00:00", "account": "jwork", "identity": "0123456789ab",
                                "five_hour": {"utilization": 50.0, "resets_at": None}})
            log.write_text(other + "\n")
            rc = sample(cfg, "dave", log, fetch=lambda url, headers: BODY, now=lambda: NOW)
            self.assertEqual(rc, 2)
            self.assertEqual(log.read_text(), other + "\n")

    def test_refuses_a_signed_out_config_dir_rather_than_write_an_unlabelled_line(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d), uuid=None), Path(d) / "meter.log"
            self.assertEqual(sample(cfg, "dave", log, fetch=lambda url, headers: BODY, now=lambda: NOW), 2)
            self.assertFalse(log.exists())

    def test_a_401_with_the_same_token_is_logged_as_auth_expired_not_a_generic_gap(self):
        def fail(url, headers):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
            rc = sample(cfg, "dave", log, fetch=fail, now=lambda: NOW)
            self.assertEqual(rc, 4)
            row = json.loads(log.read_text())
            self.assertEqual(row["reason"], "auth_expired")
            self.assertIn("401", row["error"])
            self.assertNotIn(TOKEN, log.read_text())

    def test_a_401_with_a_refreshed_credentials_file_retries_once_and_samples(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
            calls = []

            def fetch(url, headers):
                calls.append(headers["Authorization"])
                if len(calls) == 1:
                    (cfg / ".credentials.json").write_text(
                        json.dumps({"claudeAiOauth": {"accessToken": "fresh-token"}}))
                    raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
                return BODY
            rc = sample(cfg, "dave", log, fetch=fetch, now=lambda: NOW)
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [f"Bearer {TOKEN}", "Bearer fresh-token"])
            self.assertEqual(len(log.read_text().splitlines()), 1)  # the retry, not a logged gap

    def test_a_429_is_logged_rate_limited_with_its_retry_after(self):
        def fail(url, headers):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "300"}, None)
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
            rc = sample(cfg, "dave", log, fetch=fail, now=lambda: NOW)
            self.assertEqual(rc, 4)
            row = json.loads(log.read_text())
            self.assertEqual(row["reason"], "rate_limited")
            self.assertEqual(row["retry_after_s"], 300.0)

    def test_a_tick_inside_a_still_running_429_backoff_skips_the_network_call(self):
        calls = []

        def fetch(url, headers):
            calls.append(1)
            return BODY
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(json.dumps({"ts": "2026-09-15T20:58:00+00:00", "account": "dave",
                                       "identity": account_identity(cfg), "error": "HTTPError: 429",
                                       "reason": "rate_limited", "retry_after_s": 300}) + "\n")
            # NOW is 2026-09-15T21:00:03Z, 123s after the gap; 300s backoff has not elapsed.
            rc = sample(cfg, "dave", log, fetch=fetch, now=lambda: NOW)
            self.assertEqual(rc, 4)
            self.assertEqual(calls, [])  # skipped the tick entirely: no network call was made
            self.assertEqual(len(log.read_text().splitlines()), 1)  # nothing new written

    def test_a_tick_after_the_429_backoff_has_elapsed_samples_normally(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(json.dumps({"ts": "2026-09-15T20:00:00+00:00", "account": "dave",
                                       "identity": account_identity(cfg), "error": "HTTPError: 429",
                                       "reason": "rate_limited", "retry_after_s": 300}) + "\n")
            rc = sample(cfg, "dave", log, fetch=lambda url, headers: BODY, now=lambda: NOW)
            self.assertEqual(rc, 0)
            self.assertEqual(len(log.read_text().splitlines()), 2)

    def test_the_timer_path_never_sits_in_429_backoff(self):
        # read_usage's own default retries a 429 for up to ~15 minutes; a five-minute
        # sampler must give up and let the next tick try.
        self.assertEqual(NO_RETRY_FETCH.keywords, {"max_retries": 0})

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
            rc = main(["--config-dir", str(cfg), "--account", "dave", "--log", str(log)],
                      fetch=lambda url, headers: BODY, now=lambda: NOW)
            self.assertEqual(rc, 0)
            self.assertEqual(len(parse_meter_log(log.read_text().splitlines())), 1)


class UnitFileTests(unittest.TestCase):
    def test_dave_sampler_reads_dave_into_its_own_log(self):
        service = (UNITS / "claude-usage-meter-dave.service").read_text()
        self.assertIn("-m tracker.meter_log", service)
        self.assertIn("--config-dir %h/.claude-dave", service)
        self.assertIn("--account dave", service)
        self.assertIn("--log %h/.paperclip/ops/claude-usage-meter-dave.log", service)
        self.assertNotIn("gs-usage-ceiling.log", service)
        self.assertNotIn("--enforce", service)  # a meter read, never a seat pause
        self.assertIn("SuccessExitStatus=0 4", service)
        self.assertIn("WorkingDirectory=%h/claude-usage-tracker", service)

    def test_dave_timer_matches_the_jwork_cadence(self):
        timer = (UNITS / "claude-usage-meter-dave.timer").read_text()
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Unit=claude-usage-meter-dave.service", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_avis_sampler_reads_avis_into_its_own_log(self):
        service = (UNITS / "claude-usage-meter-avis.service").read_text()
        self.assertIn("-m tracker.meter_log", service)
        self.assertIn("--config-dir %h/.claude-avis", service)
        self.assertIn("--account avis", service)
        self.assertIn("--log %h/.paperclip/ops/claude-usage-meter-avis.log", service)
        self.assertIn("SuccessExitStatus=0 4", service)
        self.assertIn("WorkingDirectory=%h/claude-usage-tracker", service)

    def test_avis_timer_samples_every_minute(self):
        timer = (UNITS / "claude-usage-meter-avis.timer").read_text()
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Unit=claude-usage-meter-avis.service", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_jwork_timer_samples_every_minute(self):
        # 1-minute sampling (issue #66): shrinks each crossing's timing error bound (bounded
        # by the sample gap) from ~5min to ~1min, at negligible extra log size (~150
        # bytes/line, ~216KB/day/account against a 5x lower interval).
        timer = (UNITS / "claude-usage-meter-jwork.timer").read_text()
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Unit=claude-usage-meter-jwork.service", timer)
        self.assertIn("WantedBy=timers.target", timer)
