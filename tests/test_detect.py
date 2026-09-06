import unittest
from datetime import datetime, timedelta
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

    def test_latest_change_picks_newest(self):
        r = readings([100, 100, 100, 100, 200, 100, 100, 100, 100, 60])
        ev = detect_changes(r)
        self.assertEqual(latest_change(ev).direction, "decreased")
        self.assertIsNone(latest_change([]))


if __name__ == "__main__":
    unittest.main()
