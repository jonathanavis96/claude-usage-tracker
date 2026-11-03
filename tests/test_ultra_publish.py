"""Defects in tracker/publish.py found by the 2026-11-03 sweep, each pinned by a test."""
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_publish import (EFFORT, PASSIVE, PRICES, _detector_change, _detector_size,
                                detector_run)
from tracker.publish import build_public_json


class EvidencePastTheFiveHourEndTests(unittest.TestCase):
    """A change the detector finds on the seven-day line after the five-hour limit's end
    opens no five-hour regime with readings: the frozen figure is still the regime that holds
    the end, so its `evidence` must describe that regime's readings, not an empty one."""

    NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    END = datetime(2026, 10, 30, 23, 37, tzinfo=timezone.utc)

    def _stretch(self, end):
        return {"status": "accepted", "capture_status": "accepted", "reset_verified": True,
                "start": end.isoformat(), "end": end.isoformat(), "delta_pct": 10, "windows": 1,
                "tokens": {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0,
                                               "cache_write": 400000}}}

    def _gs(self):
        last = (self.NOW - timedelta(hours=1)).isoformat()
        return {"generated_at": last, "accounts": {
            "jwork": {"meter": {"last": last, "five_hour_last": self.END.isoformat(),
                                "weekly_only_since": (self.END + timedelta(minutes=6)).isoformat()},
                      "stretches": [self._stretch(self.END - timedelta(days=d)) for d in (1, 2, 3)]}}}

    def test_a_weekly_change_after_the_end_leaves_the_frozen_figures_evidence_in_place(self):
        before = _detector_change((self.END - timedelta(days=20)).isoformat(), "seven_day", "measured",
                                  {"seven_day": _detector_size(-30.0, -36.0, -24.0),
                                   "five_hour": _detector_size(-30.0, -38.0, -21.0),
                                   "ratio": _detector_size(1.0, -4.0, 6.0)})
        after = _detector_change((self.END + timedelta(days=2)).isoformat(), "seven_day", "provisional",
                                 {"seven_day": _detector_size(12.0, 5.0, 19.0)})
        without = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=self._gs(),
                                    four_accounts_run=detector_run(before))
        with_after = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=self._gs(),
                                       four_accounts_run=detector_run(before, after))
        model = "claude-sonnet-5"
        ev, ref = with_after["rates"][model]["evidence"], without["rates"][model]["evidence"]
        # The frozen figure is the same either way...
        self.assertEqual(with_after["rates"][model]["meter_budget_per_window"],
                         without["rates"][model]["meter_budget_per_window"])
        self.assertGreater(ref["readings"], 0)
        # ...and so is what it rests on: a change past the end holds no five-hour reading.
        for key in ("readings", "stretches", "observed_from", "observed_to", "meter_movement_pct",
                    "account_count"):
            self.assertEqual(ev[key], ref[key], key)
        self.assertEqual(with_after["passive_account_count"], without["passive_account_count"])


if __name__ == "__main__":
    unittest.main()
