import unittest
from datetime import datetime, timezone

from tracker.probe import Skip, skip_reason
from tracker.usage_api import Utilization

T0 = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)


def reads(*readings):
    it = iter(readings)
    return lambda: next(it)


class SkipReasonNoReadingTests(unittest.TestCase):
    """The endpoint reports `five_hour.utilization: null` while no window is open (real
    moonlighter rows, 2026-06-13). An idle account read that way, then used during the
    idle window, must be passed over as busy, not crash choose_account."""

    def test_meter_moving_from_no_reading_to_a_reading_is_busy_not_a_crash(self):
        read = reads(Utilization(T0, None, 30.0, None), Utilization(T0, 1.0, 30.0, "2026-11-03T08:00:00+00:00"))
        skip = skip_reason(read, lambda s: None)
        self.assertEqual(skip, Skip("busy", "meter moved no reading -> 1%"))

    def test_no_reading_then_zero_is_busy_too(self):
        # A window opened on first use reads 0% before its first tick.
        read = reads(Utilization(T0, None, 30.0, None), Utilization(T0, 0.0, 30.0, "2026-11-03T08:00:00+00:00"))
        self.assertEqual(skip_reason(read, lambda s: None), Skip("busy", "meter moved no reading -> 0%"))

    def test_a_flat_reading_is_still_formatted_as_before(self):
        read = reads(Utilization(T0, 7.0, 30.0, "r1"), Utilization(T0, 8.0, 30.0, "r1"))
        self.assertEqual(skip_reason(read, lambda s: None), Skip("busy", "meter moved 7% -> 8%"))


if __name__ == "__main__":
    unittest.main()
