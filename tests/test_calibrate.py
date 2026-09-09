import unittest
from datetime import datetime, timezone

from tracker.calibrate import calibrate
from tracker.cli_run import RunUsage
from tracker.usage_api import Utilization


class CalTests(unittest.TestCase):
    def test_matrix_is_median_of_repeats(self):
        calls = []

        def run(prompt, model, effort):
            calls.append((model, effort))
            n = {"low": 1000, "high": 3000}[effort] + (len(calls) % 3) * 10
            return RunUsage(model, 100, n - 100, 0, 0, 0.0, 1.0)

        util = iter([Utilization(datetime.now(timezone.utc), 10.0, 5.0, "r")] * 100)
        m = calibrate(["claude-sonnet-5"], ["low", "high"], 3, run, lambda: next(util), lambda s: None)
        self.assertEqual(len(calls), 6)
        self.assertEqual(m["claude-sonnet-5"]["low"], 1010)
        self.assertEqual(m["claude-sonnet-5"]["high"], 3010)
        self.assertEqual(m["_meta"]["repeats"], 3)

    def test_records_reads_once_per_cell_not_per_run(self):
        reads = []

        def read():
            reads.append(1)
            return Utilization(datetime.now(timezone.utc), 10.0, 5.0, "r")

        def run(prompt, model, effort):
            return RunUsage(model, 100, 900, 0, 0, 0.0, 1.0)

        calibrate(["m1", "m2"], ["low", "high"], 3, run, read, lambda s: None)
        # 2 models * 2 efforts * (before + after) = 8 reads, never one per run
        self.assertEqual(len(reads), 8)

    def test_per_class_tokens_recorded_per_run(self):
        def run(prompt, model, effort):
            return RunUsage(model, 111, 222, 333, 444, 0.0, 1.0)

        util = iter([Utilization(datetime.now(timezone.utc), 10.0, 5.0, "r")] * 100)
        m = calibrate(["m1"], ["low"], 2, run, lambda: next(util), lambda s: None)
        runs = m["_meta"]["runs"]["m1/low"]
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0], {"input": 111, "output": 222, "cache_read": 333, "cache_write": 444, "total": 1110})

    def test_usage_delta_recorded_per_cell(self):
        seq = iter([
            Utilization(datetime.now(timezone.utc), 10.0, 5.0, "r"),
            Utilization(datetime.now(timezone.utc), 12.5, 5.0, "r"),
        ])

        def run(prompt, model, effort):
            return RunUsage(model, 100, 900, 0, 0, 0.0, 1.0)

        m = calibrate(["m1"], ["low"], 1, run, lambda: next(seq), lambda s: None)
        self.assertEqual(m["_meta"]["usage_deltas"]["m1/low"], 2.5)


if __name__ == "__main__":
    unittest.main()


class FailureTests(unittest.TestCase):
    def test_failing_cell_is_skipped_and_others_survive(self):
        from tracker.calibrate import calibrate
        from tracker.cli_run import RunUsage
        from tracker.usage_api import Utilization
        from datetime import datetime, timezone
        def run(prompt, model, effort):
            if effort == "high":
                raise RuntimeError("boom")
            return RunUsage(model, 1, 2, 3, 4, 0.0, 1.0)
        read = lambda: Utilization(datetime.now(timezone.utc), 0.0, 0.0, None)  # noqa: E731
        seen = []
        m = calibrate(["m"], ["low", "high"], 2, run, read, lambda s: None, checkpoint=lambda x: seen.append(len(x["_meta"]["runs"])))
        self.assertEqual(m["m"], {"low": 10})
        self.assertEqual(len(m["_meta"]["failed"]), 2)
        self.assertEqual(seen, [1, 2])


class TwoTurnTests(unittest.TestCase):
    """`claude -p` sometimes answers in two API turns; the second re-reads the prefix, doubling the total.

    Sonnet low on gs, 2026-09-09: one turn = input 2, cache_read 19795 (total 21407);
    two turns = input 4, cache_read 39590, cache_write 877 (total 41881). Valued on the
    meter the two are close, so each cell publishes the median meter dollars over all runs.
    """
    PRICES = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5,
                                  "meter_weight": 1.0,
                                  "class_weight": {"input": 1.0, "output": 1.8, "cache_read": 1.0, "cache_write": 1.0}}}

    # (input, output, cache_read, cache_write): the seven live Sonnet low runs
    SONNET_LOW = [
        (4, 1978, 39590, 1253),  # total 42825
        (2, 1713, 19795, 0),     # 21510
        (2, 1720, 19795, 0),     # 21517
        (4, 1997, 39590, 1455),  # 43046
        (2, 1610, 19795, 0),     # 21407
        (4, 3726, 39590, 2542),  # 45862
        (4, 1410, 39590, 877),   # 41881
    ]

    @staticmethod
    def _run_from(seq):
        it = iter(seq)

        def run(prompt, model, effort):
            i, o, cr, cw = next(it)
            return RunUsage(model, i, o, cr, cw, 0.0, 1.0)
        return run

    @staticmethod
    def _read():
        return Utilization(datetime.now(timezone.utc), 10.0, 5.0, "r")

    def test_run_usd_is_meter_dollars(self):
        from tracker.calibrate import run_usd
        price = self.PRICES["claude-sonnet-5"]
        one = {"input": 2, "output": 1610, "cache_read": 19795, "cache_write": 0}
        two = {"input": 4, "output": 1410, "cache_read": 39590, "cache_write": 877}
        # (2*2 + 1610*10*1.8 + 19795*0.2) / 1e6 ; (4*2 + 1410*10*1.8 + 39590*0.2 + 877*2.5) / 1e6
        self.assertAlmostEqual(run_usd(one, price), 0.032943, places=6)
        self.assertAlmostEqual(run_usd(two, price), 0.0354985, places=6)
        self.assertAlmostEqual(run_usd(one, {**price, "meter_weight": 2.0}), 0.065886, places=6)

    def test_cell_publishes_median_usd_over_all_runs_and_keeps_median_tokens(self):
        m = calibrate(["claude-sonnet-5"], ["low"], 7, self._run_from(self.SONNET_LOW), self._read,
                      lambda s: None, prices=self.PRICES)
        self.assertEqual(m["claude-sonnet-5"]["low"], 41881)  # token median, kept for the page
        cell = m["_meta"]["cells"]["claude-sonnet-5/low"]
        self.assertEqual(cell["median_tokens"], 41881)
        self.assertEqual(cell["runs"], 7)
        self.assertEqual(cell["turns"], [2, 1, 1, 2, 1, 2, 2])
        self.assertAlmostEqual(cell["median_usd"], 0.0354985, places=5)
        self.assertAlmostEqual(m["usd"]["claude-sonnet-5"]["low"], cell["median_usd"])
        # max is the 45862-run (output 3726), min the 21407-run
        self.assertAlmostEqual(cell["spread_usd"], 0.081349 / 0.032943, places=3)
        self.assertNotIn("fallback", cell)
        self.assertNotIn("first_send_runs", cell)
        self.assertIn("meter-dollar", m["_meta"]["cell_rule"])

    def test_dollar_median_is_stable_where_token_median_is_not(self):
        mostly_one_turn = self.SONNET_LOW[1:3] + self.SONNET_LOW[3:6] + [self.SONNET_LOW[1]]  # 4 one-turn, 2 two-turn
        a = calibrate(["claude-sonnet-5"], ["low"], 7, self._run_from(self.SONNET_LOW), self._read,
                      lambda s: None, prices=self.PRICES)
        b = calibrate(["claude-sonnet-5"], ["low"], 6, self._run_from(mostly_one_turn), self._read,
                      lambda s: None, prices=self.PRICES)
        tok_a, tok_b = a["claude-sonnet-5"]["low"], b["claude-sonnet-5"]["low"]
        usd_a, usd_b = a["usd"]["claude-sonnet-5"]["low"], b["usd"]["claude-sonnet-5"]["low"]
        self.assertGreater(tok_a / tok_b, 1.3)  # token medians sit on different clusters
        self.assertLess(max(usd_a, usd_b) / min(usd_a, usd_b), 1.1)  # dollar medians agree within 10%

    def test_model_without_price_gets_tokens_but_no_usd(self):
        m = calibrate(["m"], ["low"], 2, self._run_from(self.SONNET_LOW), self._read, lambda s: None,
                      prices=self.PRICES)
        self.assertEqual(m["m"]["low"], round((42825 + 21510) / 2))
        self.assertNotIn("m", m["usd"])
        self.assertIsNone(m["_meta"]["cells"]["m/low"]["median_usd"])

    def test_recompute_matches_live_run(self):
        import json
        import tempfile
        from pathlib import Path
        from tracker.calibrate import main, recompute

        live = calibrate(["claude-sonnet-5"], ["low", "high"], 7,
                         self._run_from(self.SONNET_LOW + self.SONNET_LOW[::-1]), self._read, lambda s: None,
                         prices=self.PRICES)
        # A file written by the old code: no cells, no rule, no usd.
        stale = json.loads(json.dumps(live))
        del stale["_meta"]["cells"]
        del stale["_meta"]["cell_rule"]
        del stale["usd"]

        got = recompute(json.loads(json.dumps(stale)), self.PRICES)
        for key in ("claude-sonnet-5", "usd"):
            self.assertEqual(got[key], live[key])
        for key in ("cells", "cell_rule", "runs"):
            self.assertEqual(got["_meta"][key], live["_meta"][key])

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "matrix.json"
            src.write_text(json.dumps(stale))
            prices = Path(d) / "prices.json"
            prices.write_text(json.dumps({"_source": "test", **self.PRICES}))
            self.assertEqual(main(["--recompute", str(src), "--prices", str(prices)]), 0)
            on_disk = json.loads(src.read_text())
            self.assertEqual(on_disk["claude-sonnet-5"], live["claude-sonnet-5"])
            self.assertEqual(on_disk["usd"], live["usd"])
            self.assertEqual(on_disk["_meta"]["cells"], live["_meta"]["cells"])
            self.assertEqual(on_disk["_meta"]["recomputed_from"], str(src))
            self.assertNotIn("matrix.tmp", [p.name for p in Path(d).iterdir()])

            out = Path(d) / "other.json"
            self.assertEqual(main(["--recompute", str(src), "--prices", str(prices), "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["usd"], live["usd"])
