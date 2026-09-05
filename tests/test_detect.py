import unittest
from datetime import date, timedelta
from tracker.detect import detect_changes, latest_change

def series(values, start=date(2026, 8, 20)):
    return {start + timedelta(days=i): v for i, v in enumerate(values)}

class DetectTests(unittest.TestCase):
    def test_flat_series_no_event(self):
        s = {"claude-sonnet-5": series([100] * 20)}
        self.assertEqual(detect_changes(s), [])

    def test_step_down_detected_once_with_percent(self):
        s = {"claude-sonnet-5": series([100] * 10 + [86] * 10)}
        ev = detect_changes(s)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].direction, ev[0].percent, ev[0].model), ("decreased", 14, "claude-sonnet-5"))
        self.assertEqual(ev[0].date, date(2026, 8, 30))

    def test_one_day_blip_ignored(self):
        s = {"claude-sonnet-5": series([100] * 10 + [70] + [100] * 9)}
        self.assertEqual(detect_changes(s), [])

    def test_small_drift_ignored(self):
        s = {"claude-sonnet-5": series([100] * 10 + [97] * 10)}
        self.assertEqual(detect_changes(s), [])

    def test_step_up(self):
        s = {"claude-opus-5": series([50] * 10 + [60] * 10)}
        ev = detect_changes(s)
        self.assertEqual((ev[0].direction, ev[0].percent), ("increased", 20))

    def test_latest_change_picks_newest(self):
        s = {"claude-sonnet-5": series([100] * 10 + [80] * 10 + [100] * 10)}
        ev = detect_changes(s)
        self.assertEqual(len(ev), 2)
        self.assertEqual(latest_change(ev).direction, "increased")
        self.assertIsNone(latest_change([]))
