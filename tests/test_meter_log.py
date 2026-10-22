import hashlib
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tracker.meter_log import MASTERRIG_METER_LOG, NO_RETRY_FETCH, account_identity, claude_json, main, sample
from tracker.samples import parse_meter_log, parse_moonlighter
from tracker.weekly import parse_row

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "deploy" / "systemd"
MASTERRIG_UNITS = ROOT / "deploy" / "systemd-masterrig"
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

    def default_home(self, root: Path, stale: str | None, live: str | None) -> Path:
        """A home whose default ~/.claude holds a stale in-dir .claude.json, as masterrig's does."""
        cfg = root / ".claude"
        cfg.mkdir()
        (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN}}))
        if stale:
            (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": stale}}))
        if live:
            (root / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": live}}))
        return cfg

    def test_the_default_config_dir_is_labelled_from_the_home_level_file(self):
        # Without CLAUDE_CONFIG_DIR Claude Code keeps ~/.claude.json live; ~/.claude/.claude.json
        # is a leftover that does not follow a re-login.
        with tempfile.TemporaryDirectory() as d:
            cfg = self.default_home(Path(d), stale="old-account", live="new-login")
            self.assertEqual(claude_json(cfg, home=Path(d)), Path(d) / ".claude.json")
            self.assertEqual(account_identity(cfg, home=Path(d)), hashlib.sha256(b"new-login").hexdigest()[:12])

    def test_the_default_config_dir_without_a_home_level_file_is_signed_out(self):
        # The leftover is never a fallback: it would label readings with an account that may be gone.
        with tempfile.TemporaryDirectory() as d:
            cfg = self.default_home(Path(d), stale="old-account", live=None)
            self.assertIsNone(account_identity(cfg, home=Path(d)))

    def test_an_explicit_config_dir_keeps_its_own_file(self):
        # The gs seats (CLAUDE_CONFIG_DIR=~/.claude-dave and the rest): the home-level file is another login.
        with tempfile.TemporaryDirectory() as d:
            cfg = config_dir(Path(d))
            (Path(d) / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-home"}}))
            self.assertEqual(account_identity(cfg, home=Path(d)), hashlib.sha256(b"uuid-dave").hexdigest()[:12])

    def test_a_relogin_on_the_default_dir_is_refused_not_mixed_into_the_log(self):
        # a1's unit: --config-dir %h/.claude. ~/.claude signs into another account; the in-dir
        # leftover still names the old one. The sampler must see the switch and refuse.
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HOME": d}):
            cfg = self.default_home(Path(d), stale="old-account", live="old-account")
            log = Path(d) / "ops" / MASTERRIG_METER_LOG
            self.assertEqual(sample(cfg, "a1", log, fetch=lambda url, headers: BODY, now=lambda: NOW), 0)
            (Path(d) / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "new-login"}}))
            before = log.read_text()
            later = lambda: NOW.replace(minute=10)  # noqa: E731 - past MIN_READ_SPACING_S
            self.assertEqual(sample(cfg, "a1", log, fetch=lambda url, headers: BODY, now=later), 2)
            self.assertEqual(log.read_text(), before)


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

    def _log_with(self, d, line_ts, **fields):
        cfg, log = config_dir(Path(d)), Path(d) / "meter.log"
        line = {"ts": line_ts, "account": "dave", "identity": account_identity(cfg)}
        line.update(fields)
        log.write_text(json.dumps(line) + "\n")
        return cfg, log

    def _counting_fetch(self, calls):
        def fetch(url, headers):
            calls.append(1)
            return BODY
        return fetch

    def test_a_tick_soon_after_a_good_read_skips_the_call(self):
        # gs, 2026-09-23 to 2026-10-01: one read a minute per account drew a 429 with
        # Retry-After: 0 on every other tick, 55% of all lines. Two minutes apart pass.
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cfg, log = self._log_with(d, "2026-09-15T20:59:00+00:00", **BODY)  # 63s before NOW
            rc = sample(cfg, "dave", log, fetch=self._counting_fetch(calls), now=lambda: NOW)
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])
            self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_a_tick_two_minutes_after_a_good_read_samples(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cfg, log = self._log_with(d, "2026-09-15T20:57:53+00:00", **BODY)  # 130s before NOW
            self.assertEqual(sample(cfg, "dave", log, fetch=self._counting_fetch(calls), now=lambda: NOW), 0)
            self.assertEqual(calls, [1])

    def test_a_429_with_retry_after_zero_still_skips_the_next_tick(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cfg, log = self._log_with(d, "2026-09-15T20:59:00+00:00", error="HTTPError: 429",
                                      reason="rate_limited", retry_after_s=0.0)
            sample(cfg, "dave", log, fetch=self._counting_fetch(calls), now=lambda: NOW)
            self.assertEqual(calls, [])

    def test_a_last_line_stamped_in_the_future_never_blocks_reading(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cfg, log = self._log_with(d, "2026-09-16T21:00:00+00:00", **BODY)  # a day ahead of NOW
            sample(cfg, "dave", log, fetch=self._counting_fetch(calls), now=lambda: NOW)
            self.assertEqual(calls, [1])

    def test_a_network_error_does_not_delay_the_next_tick(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cfg, log = self._log_with(d, "2026-09-15T20:59:00+00:00",
                                      error="URLError: Temporary failure in name resolution")
            self.assertEqual(sample(cfg, "dave", log, fetch=self._counting_fetch(calls), now=lambda: NOW), 0)
            self.assertEqual(calls, [1])

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
        # Dave's seat is a setup-token seat since 2026-10-12: read from rate-limit headers.
        self.assertIn("--token-env GS_DAVE_CLAUDE_OAUTH_TOKEN", service)
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

    def test_a1_sampler_reads_masterrigs_own_account_into_the_log_the_joins_read(self):
        # masterrig's checkout is ~/code/claude-usage-tracker, not gs's ~/claude-usage-tracker.
        service = (MASTERRIG_UNITS / "claude-usage-meter-a1.service").read_text()
        self.assertIn("-m tracker.meter_log", service)
        self.assertIn("--config-dir %h/.claude ", service)
        self.assertIn("--account a1", service)
        self.assertIn(f"--log %h/.paperclip/ops/{MASTERRIG_METER_LOG}", service)
        self.assertNotIn("--token-env", service)  # a /login seat: the usage endpoint reads it
        self.assertIn("SuccessExitStatus=0 4", service)
        self.assertIn("WorkingDirectory=%h/code/claude-usage-tracker", service)

    def test_a1_timer_samples_every_minute(self):
        timer = (MASTERRIG_UNITS / "claude-usage-meter-a1.timer").read_text()
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Unit=claude-usage-meter-a1.service", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_the_a1_units_stay_out_of_the_gs_unit_set(self):
        # deploy/install-gs.sh compares every deploy/systemd/claude-usage-meter-* with gs's
        # installed units; an a1 unit there would read as drift on every gs install.
        self.assertEqual(sorted(p.name for p in UNITS.glob("claude-usage-meter-a1*")), [])


HEADERS = {"anthropic-ratelimit-unified-5h-utilization": "0.05",
           "anthropic-ratelimit-unified-5h-reset": str(int(datetime(2026, 9, 15, 23, tzinfo=timezone.utc).timestamp())),
           "anthropic-ratelimit-unified-7d-utilization": "0.22",
           "anthropic-ratelimit-unified-7d-reset": str(int(datetime(2026, 9, 17, 23, tzinfo=timezone.utc).timestamp())),
           "anthropic-ratelimit-unified-status": "allowed"}
SETUP_TOKEN = "fake-setup-token-never-logged"


class HeaderModeTests(unittest.TestCase):
    """--token-env: a setup-token seat read from rate-limit headers, never from .credentials.json."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        root = Path(self.d.name)
        self.cfg = config_dir(root)
        # The credentials file is blank on a revoked seat; header mode must not need it at all.
        (self.cfg / ".credentials.json").unlink()
        self.log = root / "meter.log"
        self.env = root / "env"
        self.env.write_text(f"GS_FAKE_TOKEN={SETUP_TOKEN}\n")
        self.calls = []

    def post(self, headers=HEADERS, error=None):
        def _post(url, hdrs, body):
            self.calls.append(json.loads(body))
            if error:
                raise urllib.error.HTTPError(url, error, "x", {"Retry-After": "120"} if error == 429 else {}, None)
            return headers
        return _post

    def run_sample(self, post, now=NOW, model="claude-haiku-5-5"):
        return sample(self.cfg, "dave", self.log, now=lambda: now, token_env="GS_FAKE_TOKEN",
                      env_file=self.env, model=model, post=post)

    def write(self, *rows):
        self.log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def reading(self, ts, five_reset="2026-09-15T23:00:00+00:00", seven=22.0,
                seven_reset="2026-09-17T23:00:00+00:00"):
        return {"ts": ts, "account": "dave", "identity": account_identity(self.cfg), "source": "ratelimit_headers",
                "five_hour": {"utilization": 5.0, "resets_at": five_reset},
                "seven_day": {"utilization": seven, "resets_at": seven_reset}}

    def test_a_reading_is_logged_in_the_endpoint_shape_with_its_source(self):
        self.assertEqual(self.run_sample(self.post(), model="claude-haiku-5-5"), 0)
        text = self.log.read_text()
        self.assertNotIn(SETUP_TOKEN, text)
        row = json.loads(text)
        self.assertEqual((row["five_hour"]["utilization"], row["seven_day"]["utilization"]), (5.0, 22.0))
        self.assertEqual(row["five_hour"]["resets_at"], "2026-09-15T23:00:00+00:00")
        self.assertEqual((row["source"], row["model"]), ("ratelimit_headers", "claude-haiku-5-5"))
        self.assertEqual(row["ratelimit"], {"status": "allowed"})
        self.assertEqual(parse_meter_log(text.splitlines())[0].resets_at, "2026-09-15T23:00:00+00:00")
        self.assertIsNotNone(parse_row(row))
        self.assertEqual(self.calls[0]["max_tokens"], 1)

    def test_probes_are_spaced_five_minutes_apart(self):
        self.write(self.reading("2026-09-15T20:56:03+00:00"))   # 240 s before NOW
        self.assertEqual(self.run_sample(self.post()), 0)
        self.assertEqual(self.calls, [])
        self.write(self.reading("2026-09-15T20:54:03+00:00"))   # 360 s before NOW
        self.assertEqual(self.run_sample(self.post()), 0)
        self.assertEqual(len(self.calls), 1)

    def test_401_is_token_rejected_logged_and_spaced(self):
        self.assertEqual(self.run_sample(self.post(error=401)), 4)
        row = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(row["reason"], "token_rejected")
        self.assertNotIn(SETUP_TOKEN, self.log.read_text())
        later = datetime(2026, 9, 15, 21, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(self.run_sample(self.post(), now=later), 0)
        self.assertEqual(len(self.calls), 1)   # still inside the 300 s spacing

    def test_429_is_rate_limited_with_its_retry_after(self):
        self.assertEqual(self.run_sample(self.post(error=429)), 4)
        row = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual((row["reason"], row["retry_after_s"]), ("rate_limited", 120.0))

    def test_a_missing_token_is_a_gap_naming_the_variable(self):
        self.env.write_text("SOMETHING_ELSE=1\n")
        self.assertEqual(self.run_sample(self.post()), 4)
        self.assertIn("GS_FAKE_TOKEN", json.loads(self.log.read_text())["error"])
        self.assertEqual(self.calls, [])

    def test_a_lapsed_window_is_not_probed_until_a_session_runs(self):
        # Last reading's window reset at 20:00; NOW is 21:00.
        self.write(self.reading("2026-09-15T19:30:00+00:00", five_reset="2026-09-15T20:00:00+00:00"))
        sessions = self.cfg / "sessions"
        sessions.mkdir()
        before = datetime(2026, 9, 15, 19, 45, tzinfo=timezone.utc).timestamp()
        os.utime(sessions, (before, before))
        self.assertEqual(self.run_sample(self.post()), 0)
        self.assertEqual(self.calls, [])
        after = datetime(2026, 9, 15, 20, 30, tzinfo=timezone.utc).timestamp()
        os.utime(sessions, (after, after))   # the warden's anchor (or anyone's claude run) started a window
        self.assertEqual(self.run_sample(self.post()), 0)
        self.assertEqual(len(self.calls), 1)

    def test_a_capped_week_is_not_probed_until_its_reset(self):
        self.write(self.reading("2026-09-15T20:00:00+00:00", seven=100.0))
        self.assertEqual(self.run_sample(self.post()), 0)
        self.assertEqual(self.calls, [])
        after_reset = datetime(2026, 9, 17, 23, 5, tzinfo=timezone.utc)
        # The five-hour window lapsed long before; a session (the warden's anchor) starts the next.
        (self.cfg / "sessions").mkdir()
        self.assertEqual(self.run_sample(self.post(), now=after_reset), 0)
        self.assertEqual(len(self.calls), 1)

    def test_cli_flags(self):
        import tracker.meter_log as ml
        with mock.patch.object(ml, "read_header_usage",
                               lambda tok, model, post=None, now=None: __import__("tracker.header_usage", fromlist=["x"])
                               .parse_ratelimit_headers(HEADERS, NOW)):
            rc = main(["--config-dir", str(self.cfg), "--account", "dave", "--log", str(self.log),
                       "--token-env", "GS_FAKE_TOKEN", "--env-file", str(self.env), "--model", "claude-haiku-5-5"],
                      now=lambda: NOW)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.log.read_text())["model"], "claude-haiku-5-5")


class LastLineTests(unittest.TestCase):
    def test_reads_the_last_object_line_and_skips_non_objects(self):
        from tracker.meter_log import _last_line
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "m.log")
            log.write_text("".join(json.dumps({"n": i, "pad": "x" * 200}) + "\n" for i in range(500))
                           + "42\n\xff-torn")
            self.assertEqual(_last_line(log)["n"], 499)

    def test_empty_or_missing_log_is_none(self):
        from tracker.meter_log import _last_line
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_last_line(Path(d, "nope")))
            Path(d, "e").write_text("")
            self.assertIsNone(_last_line(Path(d, "e")))
