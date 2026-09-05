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
            body = _default_fetch("u", {}, sleep=calls.append)
        self.assertEqual(body["five_hour"]["utilization"], 1)
        self.assertEqual(calls, [7.0])
