"""tracker.header_usage: meters read off rate-limit headers, token from one env-file variable."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tracker import header_usage
from tracker.header_usage import (
    MESSAGES_URL,
    TokenRejected,
    load_token,
    parse_ratelimit_headers,
    read_header_usage,
)

NOW = datetime(2026, 10, 12, 19, 0, tzinfo=timezone.utc)
TOKEN = "fake-setup-token-not-real"
HEADERS = {
    "Anthropic-Ratelimit-Unified-5h-Utilization": "0.05",
    "anthropic-ratelimit-unified-5h-reset": "1791840600",
    "anthropic-ratelimit-unified-7d-utilization": "0.22",
    "anthropic-ratelimit-unified-7d-reset": "1792200000",
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-overage-status": "rejected",
    "content-type": "application/json",
}


class ParseTests(unittest.TestCase):
    def test_fractions_become_percent_and_epochs_become_iso_utc(self):
        u, extras, problems = parse_ratelimit_headers(HEADERS, NOW)
        self.assertEqual((u.five_hour, u.seven_day), (5.0, 22.0))
        self.assertEqual(u.five_hour_resets_at, "2026-10-12T21:30:00+00:00")
        self.assertEqual(datetime.fromisoformat(u.seven_day_resets_at).timestamp(), 1792200000)
        self.assertEqual(u.ts, NOW)
        self.assertEqual(problems, [])
        self.assertEqual(extras["representative-claim"], "five_hour")
        self.assertEqual(extras["status"], "allowed")
        self.assertNotIn("content-type", extras)

    def test_a_missing_header_is_a_named_schema_problem_not_a_zero(self):
        h = {k: v for k, v in HEADERS.items() if "7d-utilization" not in k}
        u, _, problems = parse_ratelimit_headers(h, NOW)
        self.assertIsNone(u.seven_day)
        self.assertEqual(problems, ["anthropic-ratelimit-unified-7d-utilization missing"])

    def test_garbage_values_read_as_none(self):
        u, _, _ = parse_ratelimit_headers({"anthropic-ratelimit-unified-5h-utilization": "n/a",
                                           "anthropic-ratelimit-unified-5h-reset": "soon"}, NOW)
        self.assertIsNone(u.five_hour)
        self.assertIsNone(u.five_hour_resets_at)


class ReadTests(unittest.TestCase):
    def test_one_token_call_with_the_measured_request_shape(self):
        sent = {}

        def post(url, headers, body):
            sent.update(url=url, headers=headers, body=json.loads(body))
            return HEADERS
        u, _, _ = read_header_usage(TOKEN, "claude-haiku-5-5", post=post, now=lambda: NOW)
        self.assertEqual(u.five_hour, 5.0)
        self.assertEqual(sent["url"], MESSAGES_URL)
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(sent["headers"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(sent["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(sent["body"]["max_tokens"], 1)
        self.assertEqual(sent["body"]["model"], "claude-haiku-5-5")
        self.assertIn("Claude Code", sent["body"]["system"])

    def test_model_is_configurable_and_defaults_to_haiku(self):
        self.assertEqual(header_usage.DEFAULT_MODEL, "claude-haiku-5-5")
        seen = []
        read_header_usage(TOKEN, "claude-sonnet-5-5", post=lambda u, h, b: seen.append(json.loads(b)) or HEADERS)
        self.assertEqual(seen[0]["model"], "claude-sonnet-5-5")

    def test_401_is_token_rejected_and_429_stays_an_http_error(self):
        def raises(code):
            def post(url, headers, body):
                raise urllib.error.HTTPError(url, code, "x", {}, None)
            return post
        with self.assertRaises(TokenRejected) as cm:
            read_header_usage(TOKEN, post=raises(401))
        self.assertNotIn(TOKEN, str(cm.exception))
        with self.assertRaises(urllib.error.HTTPError) as cm2:
            read_header_usage(TOKEN, post=raises(429))
        self.assertEqual(cm2.exception.code, 429)


class LoadTokenTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.env = Path(self.d.name) / "env"
        self.addCleanup(self.d.cleanup)

    def test_reads_only_the_named_variable_quoted_or_exported(self):
        self.env.write_text("# c\nOTHER=nope\nexport GS_X_TOKEN=\"fake-a\"\nGS_Y_TOKEN='fake-b'\nGS_Z_TOKEN=fake-c\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_token("GS_X_TOKEN", self.env), "fake-a")
            self.assertEqual(load_token("GS_Y_TOKEN", self.env), "fake-b")
            self.assertEqual(load_token("GS_Z_TOKEN", self.env), "fake-c")

    def test_environment_wins_over_the_file(self):
        self.env.write_text("GS_X_TOKEN=fake-file\n")
        with mock.patch.dict(os.environ, {"GS_X_TOKEN": "fake-env"}):
            self.assertEqual(load_token("GS_X_TOKEN", self.env), "fake-env")

    def test_missing_variable_names_the_variable_never_a_value(self):
        self.env.write_text("GS_OTHER=fake-secret-value\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LookupError) as cm:
                load_token("GS_X_TOKEN", self.env)
            self.assertIn("GS_X_TOKEN", str(cm.exception))
            self.assertNotIn("fake-secret-value", str(cm.exception))
            with self.assertRaises(LookupError):
                load_token("GS_X_TOKEN", Path(self.d.name) / "absent")

    def test_cli_prints_figures_and_never_the_token(self):
        self.env.write_text(f"GS_X_TOKEN={TOKEN}\n")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(header_usage, "_default_post", lambda u, h, b: HEADERS), redirect_stdout(out):
            self.assertEqual(header_usage.main(["--token-env", "GS_X_TOKEN", "--env-file", str(self.env)]), 0)
        self.assertNotIn(TOKEN, out.getvalue())
        doc = json.loads(out.getvalue())
        self.assertEqual((doc["five_hour"]["utilization"], doc["seven_day"]["utilization"]), (5.0, 22.0))


if __name__ == "__main__":
    unittest.main()
