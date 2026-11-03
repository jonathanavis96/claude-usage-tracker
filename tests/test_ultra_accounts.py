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


if __name__ == "__main__":
    unittest.main()
