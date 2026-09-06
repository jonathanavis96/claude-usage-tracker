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
        s = {"claude-sonnet-5": series([100] * 10 + [80] * 10)}
        ev = detect_changes(s)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].direction, ev[0].percent, ev[0].model), ("decreased", 20, "claude-sonnet-5"))
        self.assertEqual(ev[0].date, date(2026, 8, 30))

    def test_step_inside_the_probe_quantisation_is_ignored(self):
        # a tick probe measures a window to about one prompt in ten, so 14% is noise
        s = {"claude-sonnet-5": series([100] * 10 + [86] * 10)}
        self.assertEqual(detect_changes(s), [])

    def test_twenty_percent_step_fires_on_the_second_ratio_day(self):
        s = {"claude-sonnet-5": series([100] * 10 + [80] * 10)}
        ev = detect_changes(s)
        # first ratio over threshold is 2026-08-31, the event fires on 2026-09-01
        # and is dated to the start of that day's recent window, the step itself
        self.assertEqual(ev[0].date, date(2026, 8, 30))
        self.assertEqual(detect_changes({"m": series([100] * 10 + [80] * 2)}), [])

    def test_six_day_gap_around_a_genuine_step_does_not_fire(self):
        start = date(2026, 8, 20)
        vals = {start + timedelta(days=i): 100.0 for i in range(6)}
        vals.update({start + timedelta(days=i): 130.0 for i in range(12, 18)})
        self.assertEqual(detect_changes({"claude-sonnet-5": vals}), [])

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
