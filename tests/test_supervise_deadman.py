"""The dead-man ping: an Uptime Kuma push GET after each healthy run, against a fake
server on 127.0.0.1. Never a real Kuma, never the real WhatsApp sender."""
from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tracker import health, supervise

PY = sys.executable


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []
    status = 200

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


class DeadmanTest(unittest.TestCase):
    def setUp(self):
        _Handler.hits = []
        _Handler.status = 200
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = Path(tempfile.mkdtemp())
        self.url_file = self.tmp / "kuma-push-url"
        self.url_file.write_text(f"http://127.0.0.1:{self.server.server_port}/api/push/TOKEN?status=up&msg=OK\n")
        self.health_reason = None
        p = mock.patch.object(health, "first_failure", lambda *a, **k: self.health_reason)
        p.start()
        self.addCleanup(p.stop)
        g = mock.patch.object(supervise, "whatsapp_sender", side_effect=AssertionError("real send"))
        g.start()
        self.addCleanup(g.stop)
        ge = mock.patch.object(supervise, "email_sender", side_effect=AssertionError("real email"))
        ge.start()
        self.addCleanup(ge.stop)
        self.sent: list[str] = []

    def run_cmd(self, code: int, kuma_file: Path | None) -> dict:
        state = self.tmp / "state.json"
        supervise.supervise([PY, "-c", f"raise SystemExit({code})"], lock=self.tmp / "s.lock", state_path=state,
                            log=None, send=lambda t: self.sent.append(t) or True, retries=0,
                            health_cfg=health.Config(), sleep=lambda s: None, kuma_file=kuma_file)
        return json.loads(state.read_text())

    def test_healthy_run_pings_once(self):
        st = self.run_cmd(0, self.url_file)
        self.assertEqual(_Handler.hits, ["/api/push/TOKEN?status=up&msg=OK"])
        self.assertEqual(st["last_ping"], "ok")

    def test_failed_run_does_not_ping(self):
        self.run_cmd(1, self.url_file)
        self.assertEqual(_Handler.hits, [])

    def test_unhealthy_run_does_not_ping(self):
        self.health_reason = "collection: stale"
        self.run_cmd(0, self.url_file)
        self.assertEqual(_Handler.hits, [])

    def test_missing_file_means_no_ping_and_no_error(self):
        st = self.run_cmd(0, self.tmp / "absent")
        self.assertEqual(_Handler.hits, [])
        self.assertNotIn("last_ping", st)

    def test_empty_file_means_no_ping(self):
        self.url_file.write_text("  \n")
        self.assertIsNone(supervise.ping_deadman(self.url_file))

    def test_server_error_is_recorded_not_raised(self):
        _Handler.status = 500
        st = self.run_cmd(0, self.url_file)
        self.assertTrue(st["last_ping"].startswith("failed"))
        self.assertEqual(self.sent, [])

    def test_unreachable_server_is_recorded_not_raised(self):
        self.url_file.write_text("http://127.0.0.1:9/api/push/x\n")
        self.assertTrue(supervise.ping_deadman(self.url_file, timeout=2).startswith("failed"))

    def test_non_http_content_is_refused(self):
        self.url_file.write_text("file:///etc/passwd\n")
        self.assertIn("http(s)", supervise.ping_deadman(self.url_file))

    def test_default_file_is_outside_the_repo(self):
        with mock.patch.dict("os.environ", {"HOME": str(self.tmp)}):
            self.assertEqual(supervise.kuma_url_file(),
                             self.tmp / ".config" / "claude-usage-tracker" / "kuma-push-url")
        self.assertNotIn(str(supervise.ROOT), str(supervise.kuma_url_file()))


if __name__ == "__main__":
    unittest.main()
