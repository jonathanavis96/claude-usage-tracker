import json
import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from tracker.usage_api import parse_usage, read_usage

BODY = {"five_hour": {"utilization": 24.0, "resets_at": "2026-09-06T02:00:00.3Z"},
        "seven_day": {"utilization": 82.0, "resets_at": "2026-09-11T03:59:59Z"},
        "seven_day_sonnet": {"utilization": None, "resets_at": None}}

class ParseTests(unittest.TestCase):
    def test_parse(self):
        now = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)
        u = parse_usage(BODY, now)
        self.assertEqual(u.five_hour, 24.0)
        self.assertEqual(u.seven_day, 82.0)
        self.assertEqual(u.five_hour_resets_at, "2026-09-06T02:00:00.3Z")
        self.assertEqual(u.seven_day_resets_at, "2026-09-11T03:59:59Z")
        self.assertEqual(u.ts, now)

    def test_read_uses_token_from_config_dir(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok123"}}))
            seen = {}
            def fetch(url, headers):
                seen["url"], seen["headers"] = url, headers
                return BODY
            u = read_usage(Path(d), fetch=fetch, now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
            self.assertEqual(seen["url"], "https://api.anthropic.com/api/oauth/usage")
            self.assertEqual(seen["headers"]["Authorization"], "Bearer tok123")
            self.assertEqual(seen["headers"]["anthropic-beta"], "oauth-2025-04-20")
            self.assertEqual(u.five_hour, 24.0)

    def test_missing_token_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                read_usage(Path(d), fetch=lambda u, h: BODY)


class Retry429Tests(unittest.TestCase):
    def test_429_retries_then_succeeds(self):
        import io
        import urllib.error
        from unittest import mock
        from tracker.usage_api import _default_fetch
        calls = []
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "7"}, io.BytesIO(b""))
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"five_hour": {"utilization": 1, "resets_at": "x"}}'
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok]):
            body = _default_fetch("https://x.test/u", {}, sleep=calls.append)
        self.assertEqual(body["five_hour"]["utilization"], 1)
        self.assertEqual(calls, [30.0])  # header 7 < first backoff 30

    def test_429_retries_indefinitely_with_doubling_backoff_capped_at_20_minutes(self):
        # 30, 60, 120, 240, 480 (RETRY_429_S), then doubling (960, 1200) capped at
        # RETRY_429_MAX_S; with no max_retries the loop keeps going past that.
        import io
        import urllib.error
        from unittest import mock
        from tracker.usage_api import RETRY_429_MAX_S, _default_fetch
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b""))
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"five_hour": {"utilization": 1, "resets_at": "x"}}'
        calls = []
        with mock.patch("urllib.request.urlopen", side_effect=[err] * 8 + [ok]):
            body = _default_fetch("https://x.test/u", {}, sleep=calls.append)
        self.assertEqual(body["five_hour"]["utilization"], 1)
        self.assertEqual(calls, [30, 60, 120, 240, 480, 960, 1200, 1200])
        self.assertEqual(RETRY_429_MAX_S, 1200)

    def test_retry_after_larger_than_backoff_is_also_capped_at_20_minutes(self):
        import io
        import urllib.error
        from unittest import mock
        from tracker.usage_api import _default_fetch
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "9999"}, io.BytesIO(b""))
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"five_hour": {"utilization": 1, "resets_at": "x"}}'
        calls = []
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok]):
            _default_fetch("https://x.test/u", {}, sleep=calls.append)
        self.assertEqual(calls, [1200])

    def test_a_sleep_that_raises_stops_the_loop_and_propagates(self):
        # tracker/probe.py's deadline_sleep raises ProbeAbort once the run's wall-clock
        # budget is gone; that must stop retrying rather than being swallowed.
        import io
        import urllib.error
        from unittest import mock
        from tracker.usage_api import _default_fetch

        class Abort(Exception):
            pass

        def sleep(_seconds):
            raise Abort("deadline")

        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=[err, err]):
            with self.assertRaises(Abort):
                _default_fetch("https://x.test/u", {}, sleep=sleep)

    def test_bounded_path_raises_after_max_retries(self):
        # read_usage's default fetch (no caller-supplied deadline) must not spin
        # forever: it gives up after a bounded number of 429s and re-raises.
        import io
        import urllib.error
        from unittest import mock
        from tracker.usage_api import _default_fetch
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b""))
        calls = []
        with mock.patch("urllib.request.urlopen", side_effect=[err] * 3):
            with self.assertRaises(urllib.error.HTTPError):
                _default_fetch("https://x.test/u", {}, sleep=calls.append, max_retries=2)
        self.assertEqual(calls, [30, 60])

    def test_read_usage_with_no_fetch_bounds_retries(self):
        # read_usage's own default fetch path (no explicit fetch callable) must not
        # retry forever: it calls _default_fetch with max_retries bounded to the
        # length of RETRY_429_S. _default_fetch's own indefinite-retry behaviour is
        # covered above; this only checks read_usage wires the bound through.
        from unittest import mock
        from tracker.usage_api import RETRY_429_S
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok123"}}))
            with mock.patch("tracker.usage_api._default_fetch", return_value=BODY) as m:
                read_usage(Path(d))
        self.assertEqual(m.call_args.kwargs.get("max_retries"), len(RETRY_429_S))

    def test_non_429_error_raises_immediately(self):
        import io
        import urllib.error
        from unittest import mock
        from tracker.usage_api import _default_fetch
        err = urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=[err]):
            with self.assertRaises(urllib.error.HTTPError):
                _default_fetch("https://x.test/u", {}, sleep=lambda s: None)
