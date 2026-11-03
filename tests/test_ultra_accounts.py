"""tracker.four_accounts: edge cases of the daily command and the search.

Each test here pins a defect found on 2026-11-03: it failed before its fix.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from tracker import four_accounts as F

PRICES = Path(__file__).resolve().parent.parent / "data" / "prices.json"


def _block(day: str) -> dict:
    return {"schema": F.SCHEMA, "method": F.METHOD, "day": day,
            "data_until": f"{day}T00:00:00+00:00", "rule": F.RULE, "valuation": F.VALUATION,
            "headless_factor": {"value": None}, "interval_coverage": {}, "accounts": {},
            "changes": [], "withdrawn": [], "not_limit_changes": [], "lines": {}, "levels": {},
            "replayed": True}


def _argv(tmp: Path, state: Path, now: str, *extra: str) -> list[str]:
    none = str(tmp / "none.json")
    return ["--state", str(state), "--now", now, "--prices", str(PRICES),
            "--gs-passive", none, "--masterrig-passive", none, "--model-rates", none, *extra]


class NowIsAUtcInstantTest(unittest.TestCase):
    """`--now` with a UTC offset names an instant: the run is that instant's UTC day."""

    def test_an_offset_instant_runs_as_its_utc_day(self):
        # 01:00 at +02:00 on 23 October is 23:00 UTC on 22 October: the data known at
        # midnight UTC that day is 22 October's, never 23 October's (which is in its future).
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            state = tmp / "four-accounts.json"
            rebuilt = F.advance(None, _block("2026-10-22"))
            with mock.patch.object(F, "first_day", return_value=date(2026, 9, 1)), \
                    mock.patch.object(F, "replay", return_value=rebuilt) as replay, \
                    mock.patch.object(F, "run") as run, \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                code = F.main(_argv(tmp, state, "2026-10-23T01:00:00+02:00"))
            run.assert_not_called()
            self.assertEqual(code, 0)
            self.assertEqual(replay.call_args.args[2], date(2026, 10, 22))


class NoDayToReplayTest(unittest.TestCase):
    """A rebuild whose first possible day is after today has no run to build: it says so and
    leaves the state as it was, never writing `null` over it or crashing on it."""

    def test_replay_before_the_first_possible_day_keeps_the_state(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            state = tmp / "four-accounts.json"
            F.write_state(state, F.advance(None, _block("2026-10-20")))
            before = state.read_text(encoding="utf-8")
            err = io.StringIO()
            with mock.patch.object(F, "first_day", return_value=date(2026, 10, 25)), \
                    redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = F.main(_argv(tmp, state, "2026-10-23T13:30:00+00:00", "--replay"))
            self.assertEqual(code, 0)
            self.assertEqual(state.read_text(encoding="utf-8"), before)
            self.assertIn("2026-10-25", err.getvalue())

    def test_a_missing_state_with_no_day_to_run_is_not_written_as_null(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            state = tmp / "four-accounts.json"
            with mock.patch.object(F, "first_day", return_value=date(2026, 10, 25)), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                code = F.main(_argv(tmp, state, "2026-10-23T13:30:00+00:00"))
            self.assertEqual(code, 0)
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
