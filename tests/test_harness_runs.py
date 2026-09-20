import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.harness_runs import _resolve, attribute, bracket, effort_matrix_row, parse_log, probe_rows, resets
from tracker import credits as C

LOG = """\
prompt 9 (burst of 9): five_hour=7.0 resets_at=2026-09-10T03:30:00.284968+00:00 spent=input=18 output=45
prompt 10: five_hour=8.0 resets_at=2026-09-10T03:30:00.312641+00:00 spent=input=2 output=5
Traceback (most recent call last):
  File "probe.py", line 1, in <module>
urllib.error.HTTPError: HTTP Error 429: Too Many Requests
probe skipped: another tracker job holds the lock
12:03:12Z prompt 1: five_hour=0.0 resets_at=2026-09-15T16:40:00.905880+00:00 spent=input=2 output=5
12:58:20Z prompt 2: five_hour=2.0 resets_at=2026-09-15T16:40:00.310915+00:00 spent=input=2 output=5
jwork claude-opus-5 low: 148419 tokens per 1% (57 prompts, ticks 2->5, skip 1, early tick)
23:50:00Z prompt 1: five_hour=1.0 resets_at=2026-09-14T02:40:00.100000+00:00 spent=input=2 output=5
probe aborted on dave: tick too early
"""


def _series(pairs):
    return [(datetime.fromisoformat(t), v) for t, v in pairs]


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "probe.log"
        self.path.write_text(LOG)
        self.addCleanup(self.dir.cleanup)

    def test_each_run_ends_at_its_own_terminator_and_keeps_its_prompts(self):
        runs = parse_log(self.path, "probe")
        self.assertEqual([r["outcome"] for r in runs], ["crashed", "completed", "aborted"])
        self.assertEqual([len(r["prompts"]) for r in runs], [2, 2, 1])

    def test_a_result_line_carries_the_account_model_and_value_the_probes_row_is_matched_on(self):
        run = parse_log(self.path, "probe")[1]
        self.assertEqual((run["account"], run["model"], run["tokens_per_pct"]),
                         ("jwork", "claude-opus-5", 148419.0))

    def test_an_abort_names_its_account_and_a_crash_names_none(self):
        crashed, _, aborted = parse_log(self.path, "probe")
        self.assertIsNone(crashed["account"])
        self.assertEqual(crashed["detail"], "urllib.error.HTTPError: HTTP Error 429: Too Many Requests")
        self.assertEqual((aborted["account"], aborted["detail"]), ("dave", "tick too early"))

    def test_a_missing_log_is_no_runs_rather_than_an_error(self):
        self.assertEqual(parse_log(Path(self.dir.name) / "absent.log", "probe"), [])


class TimeTests(unittest.TestCase):
    R = datetime(2026, 9, 15, 16, 40, tzinfo=timezone.utc)

    def test_a_clock_time_resolves_into_the_window_its_resets_at_closes(self):
        self.assertEqual(_resolve("12:03:12", self.R), datetime(2026, 9, 15, 12, 3, 12, tzinfo=timezone.utc))

    def test_a_clock_time_before_midnight_resolves_to_the_previous_day(self):
        r = datetime(2026, 9, 14, 2, 40, tzinfo=timezone.utc)
        self.assertEqual(_resolve("23:50:00", r), datetime(2026, 9, 13, 23, 50, tzinfo=timezone.utc))

    def test_a_clock_time_outside_the_window_resolves_to_nothing(self):
        self.assertIsNone(_resolve("05:00:00", self.R))


class AttributionTests(unittest.TestCase):
    # jwork's meter drops from 41% to 0% between these two samples, so its window ends five
    # hours after one of them, which is the bracket a run's resets_at has to fall inside.
    JWORK = _series([("2026-09-09T22:29:44+00:00", 41), ("2026-09-09T22:35:06+00:00", 0),
                     ("2026-09-10T00:02:18+00:00", 7), ("2026-09-10T00:24:15+00:00", 8),
                     ("2026-09-10T00:57:44+00:00", 12)])

    def _run(self, resets_at, readings=(7.0, 8.0)):
        return {"prompts": [{"line": 1, "clock": None, "five_hour": v,
                             "resets_at": datetime.fromisoformat(resets_at)} for v in readings]}

    def test_the_window_the_meter_implies_is_the_sampling_gap_wide(self):
        self.assertEqual(resets(self.JWORK), [(datetime.fromisoformat("2026-09-10T03:29:44+00:00"),
                                               datetime.fromisoformat("2026-09-10T03:35:06+00:00"))])

    def test_a_run_whose_window_matches_jworks_reset_is_jworks(self):
        self.assertEqual(attribute(self._run("2026-09-10T03:30:00+00:00"), {"jwork": self.JWORK}),
                         ("jwork", "meter-match"))

    def test_a_run_whose_window_does_not_match_belongs_to_the_other_account(self):
        account, how = attribute(self._run("2026-09-10T02:30:00+00:00"), {"jwork": self.JWORK})
        self.assertEqual(account, "dave")
        self.assertEqual(how, "meter-match")

    def test_an_uncovered_window_is_assumed_rather_than_matched(self):
        self.assertEqual(attribute(self._run("2026-09-01T03:30:00+00:00"), {"jwork": self.JWORK})[1],
                         "assumed")

    def test_an_undated_run_is_bracketed_by_the_meter_not_by_the_whole_window(self):
        start, end, how = bracket(self._run("2026-09-10T03:30:00+00:00"), "jwork", {"jwork": self.JWORK})
        self.assertEqual(how, "meter-bracket")
        self.assertEqual(start, datetime.fromisoformat("2026-09-10T00:02:18+00:00"))
        self.assertEqual(end, datetime.fromisoformat("2026-09-10T00:57:44+00:00"))

    def test_without_a_meter_the_bracket_is_the_whole_five_hour_window(self):
        start, end, how = bracket(self._run("2026-09-10T03:30:00+00:00"), "dave", {})
        self.assertEqual(how, "five-hour-window")
        self.assertEqual(end - start, timedelta(hours=5))

    def test_a_run_the_log_timestamped_throughout_uses_those_times(self):
        run = {"prompts": [{"line": 1, "clock": "12:03:12", "five_hour": 0.0,
                            "resets_at": datetime.fromisoformat("2026-09-15T16:40:00+00:00")},
                           {"line": 2, "clock": "12:58:20", "five_hour": 2.0,
                            "resets_at": datetime.fromisoformat("2026-09-15T16:40:00+00:00")}]}
        start, end, how = bracket(run, "jwork", {})
        self.assertEqual(how, "log-clock")
        self.assertEqual((start.hour, start.minute, end.hour, end.minute), (12, 3, 12, 58))


class RowShapeTests(unittest.TestCase):
    """What this tool writes is what tracker/credits.py reads, field for field."""

    def _written(self, rows):
        path = Path(self.dir.name) / "harness-runs.jsonl"
        path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
        return C.harness_runs(path)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_a_probe_row_survives_the_round_trip_with_its_span_intact(self):
        rows = probe_rows()
        if not rows:
            self.skipTest("no committed history/probes.jsonl")
        runs = self._written(rows)
        self.assertEqual(len(runs), len(rows))
        by_start = {r.start: r for r in runs}
        for row in rows:
            run = by_start[row["start"]]
            self.assertEqual((run.account, run.end), (row["account"], row["end"]))
            self.assertIn(row["model"], run.reason)

    def test_the_effort_matrix_row_survives_it_too(self):
        row = effort_matrix_row()
        runs = self._written([row])
        self.assertEqual([(r.account, r.start, r.end) for r in runs],
                         [(row["account"], row["start"], row["end"])])


if __name__ == "__main__":
    unittest.main()
