"""tracker.publish_gate: one alert when the gate starts failing, one recovery, never blocks."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tracker import publish_gate, supervise


class GateTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp()) / "ops" / "gate.json"
        self.sent: list[str] = []
        self.ok = True
        self.result = (0, "137 passed in 9s")
        g = mock.patch.object(supervise, "whatsapp_sender", side_effect=AssertionError("real send"))
        g.start()
        self.addCleanup(g.stop)
        ge = mock.patch.object(supervise, "email_sender", side_effect=AssertionError("real email"))
        ge.start()
        self.addCleanup(ge.stop)

    def send(self, text):
        self.sent.append(text)
        return self.ok

    def run_gate(self, t=1000.0):
        return publish_gate.gate(self.state, self.send, runner=lambda f, to: self.result, now=lambda: t)

    def test_pass_sends_nothing(self):
        self.assertEqual(self.run_gate(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(json.loads(self.state.read_text())["last_result"], "pass")

    def test_one_alert_per_incident_then_one_recovery(self):
        self.result = (1, "11 failed, 300 passed")
        self.assertEqual(self.run_gate(1000), 1)
        self.run_gate(2800)
        self.run_gate(4600)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("test gate failed: 11 failed, 300 passed", self.sent[0])
        self.assertIn("Published anyway", self.sent[0])
        self.result = (0, "ok")
        self.run_gate(1000 + 3600)
        self.run_gate(1000 + 5400)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("passes again after 60 min", self.sent[1])

    def test_failed_send_retries_next_run(self):
        self.result = (1, "1 failed")
        self.ok = False
        self.run_gate()
        self.ok = True
        self.run_gate()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(json.loads(self.state.read_text())["incident_open"])

    def test_real_runner_catches_a_failing_test_and_a_timeout(self):
        d = Path(tempfile.mkdtemp())
        (d / "test_bad.py").write_text("def test_x():\n    assert False\n")
        (d / "test_slow.py").write_text("import time\ndef test_y():\n    time.sleep(30)\n")
        with mock.patch.object(publish_gate, "ROOT", d):
            code, summary = publish_gate.pytest_runner(["test_bad.py"], 60)
            self.assertEqual(code, 1)
            self.assertIn("1 failed", summary)
            code, summary = publish_gate.pytest_runner(["test_slow.py"], 2)
            self.assertEqual((code, summary), (1, "timed out after 2 s"))

    def test_gate_catches_a_bad_price_row(self):
        import json
        import os
        from unittest import mock
        root = Path(__file__).resolve().parent.parent
        table = json.loads((root / "data" / "prices.json").read_text())
        good = publish_gate.pytest_runner(["tests/test_prices_data.py"], 60)
        self.assertEqual(good[0], 0, good[1])
        table["claude-sonnet-5-5"] = {**table["claude-sonnet-5-5"], "output": "10", "cache_read": -1}
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "prices.json"
            bad.write_text(json.dumps(table))
            with mock.patch.dict(os.environ, {"CUT_PRICES_FILE": str(bad)}):
                code, summary = publish_gate.pytest_runner(["tests/test_prices_data.py"], 60)
        self.assertEqual(code, 1)
        self.assertIn("failed", summary)

    def test_gate_files_exist(self):
        for f in publish_gate.GATE_TESTS:
            self.assertTrue((publish_gate.ROOT / f).is_file(), f)


if __name__ == "__main__":
    unittest.main()
