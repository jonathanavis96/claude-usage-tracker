import unittest
from datetime import date, datetime, timedelta
from tracker.detect import detect_changes, latest_change


def readings(values, start=datetime(2026, 8, 20), step_days=7):
    return [(start + timedelta(days=i * step_days), v) for i, v in enumerate(values)]


class DetectTests(unittest.TestCase):
    def test_flat_series_no_event(self):
        self.assertEqual(detect_changes(readings([100] * 8)), [])

    def test_step_fires_on_the_reading_it_lands_on_no_persistence_needed(self):
        # readings 0-3 establish the baseline; reading 4 steps 30% -- a single
        # sample is precise enough to fire immediately, no run of 2 required.
        r = readings([100, 100, 100, 100, 130, 130, 130])
        ev = detect_changes(r)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].direction, ev[0].percent, ev[0].model), ("increased", 30, "all"))
        self.assertEqual(ev[0].date, r[4][0].date())

    def test_step_inside_probe_quantisation_is_ignored(self):
        # 14% is under the 0.15 threshold -- probe noise, not a real step
        r = readings([100, 100, 100, 100, 114])
        self.assertEqual(detect_changes(r), [])

    def test_fewer_than_three_prior_readings_produces_no_ratio(self):
        # only 2 readings total: index 2 would need history, but there is none yet
        self.assertEqual(detect_changes(readings([100, 100])), [])

    def test_does_not_refire_while_still_on_the_new_plateau(self):
        r = readings([100, 100, 100, 100, 200, 200, 200, 200, 200])
        ev = detect_changes(r)
        self.assertEqual(len(ev), 1)

    def test_rearms_once_back_within_threshold_and_fires_on_a_later_step(self):
        r = readings([100, 100, 100, 100, 200, 100, 100, 100, 100, 60])
        ev = detect_changes(r)
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0].direction, "increased")
        self.assertEqual(ev[1].direction, "decreased")

    def test_step_down(self):
        r = readings([50, 50, 50, 50, 30])
        ev = detect_changes(r)
        self.assertEqual((ev[0].direction, ev[0].percent), ("decreased", 40))

    def test_lookback_caps_at_four_readings(self):
        # a long flat run followed by an outlier then a return: the median base
        # should use at most the previous 4 readings, not the whole history
        r = readings([100, 100, 100, 100, 100, 100, 300, 100])
        ev = detect_changes(r)
        # the 300 fires (ratio vs median of prior 4 = 100), then the following
        # 100 is a huge drop relative to a base still containing the 300 spike
        self.assertGreaterEqual(len(ev), 1)
        self.assertEqual(ev[0].direction, "increased")

    def test_out_of_order_input_is_sorted_by_timestamp(self):
        ordered = readings([100, 100, 100, 100, 140])
        shuffled = [ordered[4], ordered[0], ordered[2], ordered[1], ordered[3]]
        self.assertEqual(detect_changes(shuffled), detect_changes(ordered))

    def test_max20_weekly_windows_real_history_has_no_event(self):
        # The 9.5-11 windows/week plateau (weeks ending on or before 2026-08-14)
        # is Max 5x, frozen history from before Jonathan's plan change -- it is
        # never run through detection at all (tracker/publish.py splits weekly
        # windows by plan and only detects on the live max20 series). The real
        # max20 series is just the two post-change weeks, 6.58 and 6.35: too
        # short (detect_changes needs MIN_HISTORY=3 prior readings) to produce
        # any event, so the 2026-08-18 plan change itself is correctly never
        # reported as a weekly-limit change.
        values = [6.58, 6.35]
        d0 = date(2026, 8, 28)
        r = [(datetime(d0.year, d0.month, d0.day) + timedelta(days=i * 7), v) for i, v in enumerate(values)]
        self.assertEqual(detect_changes(r), [])

    def test_max20_weekly_windows_synthetic_drop_fires_an_event(self):
        # Same real max20 start, extended with two more weeks and a genuine
        # later cut to 4.0 windows/week -- once there is enough max20 history,
        # a real step still fires.
        values = [6.58, 6.35, 6.5, 4.0]
        d0 = date(2026, 8, 28)
        r = [(datetime(d0.year, d0.month, d0.day) + timedelta(days=i * 7), v) for i, v in enumerate(values)]
        ev = detect_changes(r)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].direction, "decreased")
        self.assertEqual(ev[0].date, date(2026, 9, 18))

    def test_latest_change_picks_newest(self):
        r = readings([100, 100, 100, 100, 200, 100, 100, 100, 100, 60])
        ev = detect_changes(r)
        self.assertEqual(latest_change(ev).direction, "decreased")
        self.assertIsNone(latest_change([]))


if __name__ == "__main__":
    unittest.main()
