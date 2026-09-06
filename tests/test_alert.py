"""tracker.alert: one email to Jonathan through the site's send endpoint.

Nothing here touches the network. The POST is either a recorded fake, or a
local http.server on 127.0.0.1 for the one test that exercises the real
urllib path.
"""
from __future__ import annotations
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tracker.alert import (AlertConfig, SEND_URL, _default_post, alert_body, alert_config,
                           main, read_env_file, send_alert)

NOW = datetime(2026, 9, 6, 14, 30, tzinfo=timezone.utc)


def env_file(tmp: str, text: str) -> Path:
    p = Path(tmp) / ".claude-usage-notify.env"
    p.write_text(text, encoding="utf-8")
    return p


class FakePost:
    """Records one POST and answers with a fixed status."""

    def __init__(self, status=200, reply='{"ok":true}', error: OSError | None = None):
        self.status, self.reply, self.error = status, reply, error
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, body):
        self.calls.append((url, dict(headers), json.loads(body)))
        if self.error:
            raise self.error
        return self.status, self.reply


class EnvFileTests(unittest.TestCase):
    def test_reads_key_from_assignment_not_comment(self):
        with tempfile.TemporaryDirectory() as d:
            p = env_file(d, "# NOTIFY_SEND_SECRET=not-this one is a comment\n"
                            "NOTIFY_SEND_SECRET=s3cr3t\r\n"
                            "export NOTIFY_ALERT_TO='jonathan@example.com'\n\n")
            values = read_env_file(p)
        self.assertEqual(values, {"NOTIFY_SEND_SECRET": "s3cr3t", "NOTIFY_ALERT_TO": "jonathan@example.com"})

    def test_missing_file_is_empty(self):
        self.assertEqual(read_env_file(Path("/nonexistent/.claude-usage-notify.env")), {})

    def test_first_assignment_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = env_file(d, "NOTIFY_SEND_SECRET=first\nNOTIFY_SEND_SECRET=second\n")
            self.assertEqual(read_env_file(p)["NOTIFY_SEND_SECRET"], "first")


class ConfigTests(unittest.TestCase):
    def test_file_supplies_secret_and_address(self):
        with tempfile.TemporaryDirectory() as d:
            p = env_file(d, "NOTIFY_SEND_SECRET=s3cr3t\nNOTIFY_ALERT_TO=jonathan@example.com\n")
            cfg = alert_config(p, environ={})
        self.assertEqual(cfg, AlertConfig(to="jonathan@example.com", secret="s3cr3t", url=SEND_URL))

    def test_environment_overrides_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = env_file(d, "NOTIFY_SEND_SECRET=s3cr3t\nNOTIFY_ALERT_TO=file@example.com\n")
            cfg = alert_config(p, environ={"NOTIFY_ALERT_TO": "env@example.com",
                                           "NOTIFY_SEND_URL": "http://127.0.0.1:1/send"})
        self.assertEqual(cfg.to, "env@example.com")
        self.assertEqual(cfg.url, "http://127.0.0.1:1/send")

    def test_explicit_to_and_url_win(self):
        with tempfile.TemporaryDirectory() as d:
            p = env_file(d, "NOTIFY_SEND_SECRET=s3cr3t\nNOTIFY_ALERT_TO=file@example.com\n")
            cfg = alert_config(p, environ={}, to="flag@example.com", url="http://x.test/s")
        self.assertEqual((cfg.to, cfg.url), ("flag@example.com", "http://x.test/s"))

    def test_missing_secret_is_a_reason_not_a_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = env_file(d, "NOTIFY_ALERT_TO=jonathan@example.com\n")
            reason = alert_config(p, environ={})
        self.assertIsInstance(reason, str)
        self.assertIn("NOTIFY_SEND_SECRET", reason)

    def test_missing_address_is_a_reason_not_a_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = env_file(d, "NOTIFY_SEND_SECRET=s3cr3t\n")
            reason = alert_config(p, environ={})
        self.assertIsInstance(reason, str)
        self.assertIn("NOTIFY_ALERT_TO", reason)


class BodyTests(unittest.TestCase):
    def test_body_prefixes_subject_and_signs_off(self):
        body = alert_body("Outlier on claude-opus-5", "Rerun agreed with the median.\n",
                          "jonathan@example.com", source="gs", now=NOW)
        self.assertEqual(body["to"], "jonathan@example.com")
        self.assertEqual(body["subject"], "Claude usage tracker: Outlier on claude-opus-5")
        self.assertEqual(body["text"],
                         "Rerun agreed with the median.\n\n-- claude-usage-tracker on gs, 2026-09-06T14:30Z")


class SendTests(unittest.TestCase):
    CFG = AlertConfig(to="jonathan@example.com", secret="s3cr3t", url="http://127.0.0.1:1/send")

    def test_posts_admin_payload_with_bearer_secret(self):
        post = FakePost()
        status, reply = send_alert("Change confirmed", "Sonnet 5 up 7%", self.CFG, post=post,
                                   source="gs", now=NOW)
        self.assertEqual((status, reply), (200, '{"ok":true}'))
        url, headers, body = post.calls[0]
        self.assertEqual(url, "http://127.0.0.1:1/send")
        self.assertEqual(headers["authorization"], "Bearer s3cr3t")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(body["to"], "jonathan@example.com")
        self.assertEqual(body["subject"], "Claude usage tracker: Change confirmed")
        self.assertTrue(body["text"].startswith("Sonnet 5 up 7%\n\n-- claude-usage-tracker on gs, "))
        self.assertEqual(set(body), {"to", "subject", "text"})


class MainTests(unittest.TestCase):
    def _main(self, argv, post, env_text="NOTIFY_SEND_SECRET=s3cr3t\nNOTIFY_ALERT_TO=jonathan@example.com\n"):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            p = env_file(d, env_text)
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(["--env-file", str(p), "--source", "gs", *argv], post=post, environ={})
        return rc, out.getvalue(), err.getvalue()

    def test_sent(self):
        post = FakePost()
        rc, out, err = self._main(["--subject", "Test alert", "--text", "hello"], post)
        self.assertEqual(rc, 0, err)
        self.assertIn("alert: sent 'Test alert' to jonathan@example.com (HTTP 200)", out)
        self.assertEqual(post.calls[0][2]["subject"], "Claude usage tracker: Test alert")
        self.assertNotIn("s3cr3t", out + err)

    def test_unconfigured_is_a_skip_with_exit_0(self):
        post = FakePost()
        rc, out, err = self._main(["--subject", "x", "--text", "y"], post, env_text="# nothing here\n")
        self.assertEqual(rc, 0)
        self.assertIn("alert: skipped", err)
        self.assertEqual(post.calls, [])

    def test_refused_post_is_a_warning_with_exit_1(self):
        post = FakePost(status=401, reply='{"error":"unauthorized"}')
        rc, _out, err = self._main(["--subject", "x", "--text", "y"], post)
        self.assertEqual(rc, 1)
        self.assertIn("warning: alert 'x' refused: HTTP 401", err)
        self.assertNotIn("s3cr3t", err)

    def test_unreachable_endpoint_is_a_warning_with_exit_1(self):
        post = FakePost(error=OSError("connection refused"))
        rc, _out, err = self._main(["--subject", "x", "--text", "y"], post)
        self.assertEqual(rc, 1)
        self.assertIn("warning: alert 'x' not sent: connection refused", err)

    def test_text_comes_from_stdin_when_flag_omitted(self):
        import sys
        post = FakePost()
        stdin, sys.stdin = sys.stdin, io.StringIO("multi\nline body\n")
        try:
            rc, _out, err = self._main(["--subject", "x"], post)
        finally:
            sys.stdin = stdin
        self.assertEqual(rc, 0, err)
        self.assertTrue(post.calls[0][2]["text"].startswith("multi\nline body\n\n-- "))


class RealPostTests(unittest.TestCase):
    """_default_post against a local server: the header and body arrive as sent, and a
    non-2xx answer comes back as a status rather than an exception."""

    def test_default_post_round_trip(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                seen["auth"] = self.headers.get("authorization")
                seen["body"] = json.loads(self.rfile.read(length))
                status = 401 if seen["auth"] != "Bearer s3cr3t" else 200
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}' if status == 200 else b'{"error":"unauthorized"}')

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            cfg = AlertConfig(to="jonathan@example.com", secret="s3cr3t",
                              url=f"http://127.0.0.1:{srv.server_port}/api/notify/send")
            status, reply = send_alert("Round trip", "body", cfg, source="gs", now=NOW)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(reply), {"ok": True})
            self.assertEqual(seen["auth"], "Bearer s3cr3t")
            self.assertEqual(seen["body"]["to"], "jonathan@example.com")
            status, _ = _default_post(cfg.url, {"authorization": "Bearer wrong",
                                                "content-type": "application/json"}, b"{}")
            self.assertEqual(status, 401)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
