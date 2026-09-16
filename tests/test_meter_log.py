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
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("Unit=claude-usage-meter-dave.service", timer)
        self.assertIn("WantedBy=timers.target", timer)
