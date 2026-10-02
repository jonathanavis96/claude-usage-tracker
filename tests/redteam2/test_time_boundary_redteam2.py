"""Red-team 2: UTC midnight against SAST, month and year rollover, the weekly reset moment.

Checked and holding (no test):
  - meter_log stamps UTC, moonlighter +02:00; every comparison parses both to instants.
  - join.daily_rates and gs_passive's daily pool key days in UTC; health ages are instants.
  - weekly._week_key adds 30 s, so a reset jittering across UTC midnight keys one week;
    month and year rollover are plain date arithmetic (`_iso_week_ending` is ISO-correct
    across 2026-12-28 .. 2027-01-03).
  - bin/daily.sh's notify attempts are keyed on `date -u`; rates_due compares local to local.

The weekly reset moment: tracker/weekly.py marks a week complete at its reset instant
(`resets_at <= now`). tracker/publish.py recomputes `partial` from the week's date alone.
"""
from __future__ import annotations

from datetime import datetime, timezone


from tracker import publish
from tracker.weekly import weekly_windows

RESET = "2026-10-01T16:00:00.412000+00:00"   # a weekly reset, 18:00 SAST
NOW = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)   # four hours after it


def _rows():
    """One week of paired readings that ends at RESET, then the first reading of the next week."""
    rows, util7 = [], 40.0
    for w in range(3):                       # three five-hour windows: 00-05, 05-10, 10-15 UTC
        five_reset = f"2026-10-01T{5 * w + 5:02d}:00:00+00:00"
        for k in range(10):                  # a reading every 30 min, both meters rising
            util7 += 0.3
            minutes = 300 * w + 30 * k
            rows.append({"ts": f"2026-10-01T{minutes // 60:02d}:{minutes % 60:02d}:00+00:00",
                         "five_hour": 2.0 * (k + 1), "five_resets_at": five_reset,
                         "seven_day": float(round(util7)), "seven_resets_at": RESET})
    return rows


def test_week_is_complete_from_its_reset_instant():
    weekly = weekly_windows(_rows(), now=NOW)
    assert weekly["history"], "precondition: the week has enough movement to be published"
    assert weekly["history"][-1]["partial"] is False, "precondition: weekly.py says the week is complete"

    block, _events = publish._weekly_block(weekly, {"current": None, "history": [], "by_window": []}, NOW)
    row = block["passive"]["history"][-1]
    assert row["week_ending"] == "2026-10-01"
    assert row["partial"] is False, ("the week reset at 16:00Z is published as still open at 20:00Z: publish "
                                     "compares the reset's date with today's, not the reset instant with now")


def test_week_open_now_falls_back_to_the_date_and_never_publishes_the_instant():
    old = {"week_ending": "2026-10-01", "windows": 5.0}
    assert publish._week_open_now(old, NOW)["partial"] is True          # no instant: open until midnight
    new = dict(old, resets_at=RESET)
    row = publish._week_open_now(new, NOW)
    assert row["partial"] is False and "resets_at" not in row
    assert publish._week_open_now(new, datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc))["partial"] is True
