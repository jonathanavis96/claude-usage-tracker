import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import harness_runs as H
from tools.harness_runs import (_resolve, attribute, bracket, collect, effort_matrix_row,
                               parse_log, probe_rows, resets, same_run)
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


#: One completed run the log timestamps throughout: 2026-09-15, 12:03:12Z to 12:58:20Z.
COMPLETED_LOG = """\
12:03:12Z prompt 1: five_hour=0.0 resets_at=2026-09-15T16:40:00.905880+00:00 spent=input=2 output=5
12:58:20Z prompt 2: five_hour=2.0 resets_at=2026-09-15T16:40:00.310915+00:00 spent=input=2 output=5
jwork claude-opus-5 low: 148419 tokens per 1% (57 prompts, ticks 2->5, skip 1, early tick)
23:50:00Z prompt 1: five_hour=1.0 resets_at=2026-09-14T02:40:00.100000+00:00 spent=input=2 output=5
probe aborted on dave: tick too early
"""


def _probe_row(ts: str, tokens_per_pct: float = 148419.0, account: str = "jwork",
               model: str = "claude-opus-5", elapsed_s: int = 3600) -> dict:
    """A history/probes.jsonl row in the shape probe_rows() hands back."""
    start = datetime.fromisoformat(ts)
    return {"account": account, "start": start, "end": start + timedelta(seconds=elapsed_s),
            "kind": "probe", "outcome": "completed", "model": model, "effort": "low",
            "tokens_per_pct": tokens_per_pct, "precision": "elapsed_s",
            "account_source": "probes.jsonl", "source": "history/probes.jsonl:1"}


class CollectTests(unittest.TestCase):
    """collect(): the log's runs merged with the rows, and what counts as the same run.

    The dedup key is account, model, rounded tokens per 1% *and* proximity in time. Without
    the time condition a run is dropped as a duplicate of another day's row whenever the probe
    happens to read the same rounded tokens per 1% twice, and the dropped run's span then
    excuses no stretch at all -- which is the whole point of the file.
    """

    MATRIX = {"account": "jwork", "start": datetime(2026, 9, 9, 11, 28, 37, tzinfo=timezone.utc),
              "end": datetime(2026, 9, 9, 14, 53, 22, tzinfo=timezone.utc),
              "kind": "effort-matrix", "outcome": "completed", "model": None, "effort": None,
              "tokens_per_pct": None, "precision": "recorded", "account_source": "effort_matrix",
              "source": "data/effort_matrix.json:_meta"}

    def collect(self, rows: list[dict], log: str = COMPLETED_LOG) -> list[dict]:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "probe.log"
            path.write_text(log)
            with mock.patch.object(H, "PROBE_LOG", path), \
                 mock.patch.object(H, "OUTPUT_PROBE_LOG", Path(d) / "absent.log"), \
                 mock.patch.object(H, "meter_series", lambda: {}), \
                 mock.patch.object(H, "probe_rows", lambda: [dict(r) for r in rows]), \
                 mock.patch.object(H, "effort_matrix_row", lambda: dict(self.MATRIX)):
                return collect()

    def test_a_row_of_the_same_window_is_the_same_run_and_is_not_emitted_twice(self):
        rows = self.collect([_probe_row("2026-09-15T12:00:00+00:00")])
        completed = [r for r in rows if r["outcome"] == "completed" and r["kind"] == "probe"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["precision"], "elapsed_s")
        self.assertIn("probe.log:3", completed[0]["source"])

    def test_a_row_three_days_away_is_a_different_run_and_both_are_kept(self):
        """Same account, same model, same rounded tokens per 1% -- and not the same run."""
        rows = self.collect([_probe_row("2026-09-12T12:00:00+00:00")])
        completed = [r for r in rows if r["outcome"] == "completed" and r["kind"] == "probe"]
        self.assertEqual(len(completed), 2)
        self.assertEqual({r["precision"] for r in completed}, {"elapsed_s", "log-clock"})
        log_run = next(r for r in completed if r["precision"] == "log-clock")
        self.assertEqual(log_run["start"], "2026-09-15T12:03:12+00:00")
        self.assertEqual(log_run["end"], "2026-09-15T12:58:20+00:00")

    def test_a_row_of_another_account_or_model_is_never_the_same_run(self):
        for row in (_probe_row("2026-09-15T12:00:00+00:00", account="dave"),
                    _probe_row("2026-09-15T12:00:00+00:00", model="claude-sonnet-5")):
            rows = self.collect([row])
            self.assertEqual(len([r for r in rows if r["kind"] == "probe"
                                  and r["outcome"] == "completed"]), 2)

    def test_a_row_reading_different_tokens_per_pct_is_never_the_same_run(self):
        rows = self.collect([_probe_row("2026-09-15T12:00:00+00:00", tokens_per_pct=9_999.0)])
        self.assertEqual(len([r for r in rows if r["kind"] == "probe"
                              and r["outcome"] == "completed"]), 2)

    def test_the_aborted_run_is_a_row_of_its_own_on_the_account_the_log_names(self):
        rows = self.collect([_probe_row("2026-09-15T12:00:00+00:00")])
        aborted = [r for r in rows if r["outcome"] == "aborted"]
        self.assertEqual(len(aborted), 1)
        self.assertEqual((aborted[0]["account"], aborted[0]["account_source"]), ("dave", "log"))
        self.assertEqual(aborted[0]["detail"], "tick too early")

    def test_every_row_comes_back_sorted_with_its_span_as_an_iso_string(self):
        rows = self.collect([_probe_row("2026-09-12T12:00:00+00:00")])
        starts = [r["start"] for r in rows]
        self.assertEqual(starts, sorted(starts))
        for r in rows:
            self.assertIsInstance(r["start"], str)
            self.assertLessEqual(r["start"], r["end"])
        self.assertIn(self.MATRIX["source"], [r["source"] for r in rows])

    def test_the_proximity_test_itself_is_one_window_wide_either_side(self):
        """The row spans 12:00 to 13:00; a bracket is the same run within one window of it."""
        row = _probe_row("2026-09-15T12:00:00+00:00")

        def near(start: datetime) -> bool:
            return same_run(row, "jwork", "claude-opus-5", 148419.0, start,
                            start + timedelta(hours=1))

        self.assertTrue(near(datetime(2026, 9, 15, 12, 3, 12, tzinfo=timezone.utc)))   # overlaps
        # One window after the row's end, less a minute, is still the same run; a minute the
        # other side of the window is not.
        self.assertTrue(near(row["end"] + H.MATCH_TOLERANCE - timedelta(minutes=1)))
        self.assertFalse(near(row["end"] + H.MATCH_TOLERANCE + timedelta(minutes=1)))
        self.assertTrue(near(row["start"] - H.MATCH_TOLERANCE))
        self.assertFalse(near(row["start"] - H.MATCH_TOLERANCE - timedelta(hours=2)))


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
