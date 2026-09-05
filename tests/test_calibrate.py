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
