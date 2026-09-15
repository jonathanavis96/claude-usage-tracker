import unittest
from datetime import date, datetime, timedelta
from tracker.detect import current_regime_points, detect_changes, detect_weighted_changes, latest_change, pooled_windows


def readings(values, start=datetime(2026, 8, 20), step_days=7):
    return [(start + timedelta(days=i * step_days), v) for i, v in enumerate(values)]


class DetectTests(unittest.TestCase):
    def test_flat_series_no_event(self):
        self.assertEqual(detect_changes(readings([100] * 8)), [])

    def test_step_fires_but_needs_a_confirming_reading(self):
        # readings 0-3 establish the baseline; reading 4 steps 30%, and reading 5
        # confirms it (still >15% from reading 4's own base, same direction). The
        # event keeps reading 4's date and percent, not the confirming reading's.
        r = readings([100, 100, 100, 100, 130, 130, 130])
        ev = detect_changes(r)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].direction, ev[0].percent, ev[0].model), ("increased", 30, "all"))
        self.assertEqual(ev[0].date, r[4][0].date())

    def test_an_unconfirmed_candidate_does_not_fire(self):
        # the real series that motivated this: 1.081, 1.132, 0.682, 1.118 -- the
        # third reading alone looks like a 34% drop, but the fourth snaps back to
        # band, so it was never a real step and nothing should fire.
        r = readings([1.081, 1.132, 0.682, 1.118])
        self.assertEqual(detect_changes(r), [])

    def test_step_inside_probe_quantisation_is_ignored(self):
        # 14% is under the 0.15 threshold -- probe noise, not a real step
        r = readings([100, 100, 100, 100, 114, 114])
        self.assertEqual(detect_changes(r), [])

    def test_fewer_than_three_prior_readings_produces_no_ratio(self):
        # only 2 readings total: index 2 would need history, but there is none yet
        self.assertEqual(detect_changes(readings([100, 100])), [])

    def test_the_newest_reading_cannot_fire_unconfirmed(self):
        # a step on the last reading in the series has no later reading to confirm
        # it, so it cannot fire yet -- it is unconfirmed until the next probe.
        r = readings([100, 100, 100, 100, 130])
        self.assertEqual(detect_changes(r), [])

    def test_does_not_refire_while_still_on_the_new_plateau(self):
        r = readings([100, 100, 100, 100, 200, 200, 200, 200, 200])
        ev = detect_changes(r)
        self.assertEqual(len(ev), 1)

    def test_rearms_once_back_within_threshold_and_fires_on_a_later_step(self):
        r = readings([100, 100, 100, 100, 200, 200, 200, 200, 200, 200, 60, 60])
        ev = detect_changes(r)
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0].direction, "increased")
        self.assertEqual(ev[1].direction, "decreased")

    def test_step_down(self):
        r = readings([50, 50, 50, 50, 30, 30])
        ev = detect_changes(r)
        self.assertEqual((ev[0].direction, ev[0].percent), ("decreased", 40))

    def test_lookback_caps_at_four_readings(self):
        # a long flat run followed by a confirmed outlier step: the median base
        # should use at most the previous 4 readings, not the whole history
        r = readings([100, 100, 100, 100, 100, 100, 300, 300, 100, 100])
        ev = detect_changes(r)
        self.assertGreaterEqual(len(ev), 1)
        self.assertEqual(ev[0].direction, "increased")

    def test_out_of_order_input_is_sorted_by_timestamp(self):
        ordered = readings([100, 100, 100, 100, 140, 140])
        shuffled = [ordered[5], ordered[4], ordered[0], ordered[2], ordered[1], ordered[3]]
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
        # later cut to 4.0 windows/week, confirmed by a fifth week at the same
        # level -- once there is enough max20 history, a real step still fires.
        values = [6.58, 6.35, 6.5, 4.0, 4.0]
        d0 = date(2026, 8, 28)
        r = [(datetime(d0.year, d0.month, d0.day) + timedelta(days=i * 7), v) for i, v in enumerate(values)]
        ev = detect_changes(r)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].direction, "decreased")
        self.assertEqual(ev[0].date, date(2026, 9, 18))

    def test_latest_change_picks_newest(self):
        r = readings([100, 100, 100, 100, 200, 200, 200, 200, 200, 200, 60, 60])
        ev = detect_changes(r)
        self.assertEqual(latest_change(ev).direction, "decreased")
        self.assertIsNone(latest_change([]))


def window_points(rows):
    """(window_ending, d5, d7) triples from 'YYYY-MM-DDTHH:MM d5 d7' strings."""
    out = []
    for r in rows:
        ts, d5, d7 = r.split()
        out.append((datetime.fromisoformat(ts), float(d5), float(d7)))
    return out


# The real per-window passive series around the 2026-09-13 weekly cap cut
# (issue #25): five-hour and seven-day meter movement paired inside one
# five-hour window, from ~/.moonlighter/usage_log.jsonl on masterrig.
ISSUE_25_WINDOWS = [
    "2026-09-10T21:29 72 11",   # 6.55
    "2026-09-12T16:30 17 3",    # 5.67
    "2026-09-12T21:30 27 5",    # 5.40
    "2026-09-14T16:30 37 8",    # 4.62  <- first window at the new level
    "2026-09-14T21:30 17 4",    # 4.25
    "2026-09-15T16:30 27 6",    # 4.50
]


class WeightedDetectTests(unittest.TestCase):
    def test_issue_25_step_fires_on_the_data_as_it_stood_on_2026_09_15(self):
        # 116/19 = 6.11 windows before, 81/18 = 4.50 after: a 26% cut. Dated by
        # the first window at the new level, not by any calendar week.
        ev = detect_weighted_changes(window_points(ISSUE_25_WINDOWS))
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].direction, ev[0].percent, ev[0].date), ("decreased", 26, date(2026, 9, 14)))

    def test_issue_25_step_already_fires_on_the_data_as_it_stood_on_2026_09_14(self):
        # Two post-step windows (d7 = 8 + 4 = 12) are enough to confirm: the
        # newest point takes part, nothing waits for a following calendar week.
        ev = detect_weighted_changes(window_points(ISSUE_25_WINDOWS[:5]))
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].direction, ev[0].date), ("decreased", date(2026, 9, 14)))

    def test_one_post_step_window_is_not_enough_to_confirm(self):
        # 09-14T16:30 alone (d7=8) is a candidate but not a confirmation: under
        # MIN_POOL_D7 and only one window.
        self.assertEqual(detect_weighted_changes(window_points(ISSUE_25_WINDOWS[:4])), [])

    def test_issue_25_pre_step_windows_alone_are_flat(self):
        # 6.55, 5.67, 5.40 vary by 18% between single windows, but that is
        # whole-percent rounding on d7 of 3 and 5, not a step.
        self.assertEqual(detect_weighted_changes(window_points(ISSUE_25_WINDOWS[:3])), [])

    def test_small_d7_bucket_cannot_be_a_candidate(self):
        # A d7=2 window reading 3.0 (vs a 6.0 base) is mostly rounding: no vote,
        # and the following normal windows leave the series flat.
        pts = window_points(["2026-09-01T05:00 60 10", "2026-09-01T10:00 60 10",
                             "2026-09-02T05:00 6 2", "2026-09-02T10:00 60 10", "2026-09-02T15:00 60 10"])
        self.assertEqual(detect_weighted_changes(pts), [])

    def test_a_candidate_that_the_pool_does_not_confirm_is_dropped(self):
        # One d7=4 window at 3.0 looks like a 50% cut on its own; pooled with the
        # next window (d7=10 at 6.0) it is 72/14 = 5.14, inside 15% of 6.0.
        pts = window_points(["2026-09-01T05:00 60 10", "2026-09-01T10:00 60 10",
                             "2026-09-02T05:00 12 4", "2026-09-02T10:00 60 10", "2026-09-02T15:00 60 10"])
        self.assertEqual(detect_weighted_changes(pts), [])

    def test_a_single_heavy_window_cannot_confirm_itself(self):
        # d7=12 clears MIN_POOL_D7 on its own but the two meters do not always
        # advance in step within one window, so one window is never a change.
        pts = window_points(["2026-09-01T05:00 60 10", "2026-09-01T10:00 60 10", "2026-09-02T05:00 48 12"])
        self.assertEqual(detect_weighted_changes(pts), [])

    def test_rounding_concession_scales_with_the_bucket_size(self):
        # d7=8 at 37 (4.62, -24% on a 6.11 base) clears the gate: even with half
        # a point conceded (37/7.5 = 4.93) it is 19% down. The same ratio at
        # d7=3 (14/3 = 4.67, conceded 14/2.5 = 5.6, -8%) does not vote.
        base = ["2026-09-10T21:29 72 11", "2026-09-12T16:30 17 3", "2026-09-12T21:30 27 5"]
        strong = window_points(base + ["2026-09-14T16:30 37 8", "2026-09-14T21:30 17 4"])
        weak = window_points(base + ["2026-09-14T16:30 14 3", "2026-09-14T21:30 17 4"])
        self.assertEqual(len(detect_weighted_changes(strong)), 1)
        self.assertEqual(detect_weighted_changes(weak), [])

    def test_base_needs_ten_points_of_seven_day_movement(self):
        pts = window_points(["2026-09-01T05:00 30 5", "2026-09-02T05:00 20 5", "2026-09-02T10:00 20 5",
                             "2026-09-02T15:00 20 5"])
        self.assertEqual(detect_weighted_changes(pts), [])

    def test_does_not_refire_on_the_new_plateau_and_bases_the_next_step_on_it(self):
        pts = window_points(ISSUE_25_WINDOWS + [
            "2026-09-16T16:30 45 10", "2026-09-17T16:30 45 10",   # still 4.5
            "2026-09-18T16:30 72 8", "2026-09-18T21:30 72 8",     # 9.0: doubled
        ])
        ev = detect_weighted_changes(pts)
        self.assertEqual([(e.direction, e.date) for e in ev],
                         [("decreased", date(2026, 9, 14)), ("increased", date(2026, 9, 18))])
        self.assertEqual(ev[1].percent, 100)

    def test_base_is_the_trailing_fourteen_days_of_the_regime(self):
        # A month of 6.0 then a slow drift to 5.4 over weeks never fires (each
        # point sits inside 15% of its own recent base); a 30% step still does.
        slow = [(datetime(2026, 8, 1) + timedelta(days=i), 60.0 - i * 0.2, 10.0) for i in range(30)]
        self.assertEqual(detect_weighted_changes(slow), [])
        stepped = slow + [(datetime(2026, 9, 1), 38.0, 10.0), (datetime(2026, 9, 2), 38.0, 10.0)]
        ev = detect_weighted_changes(stepped)
        self.assertEqual([(e.direction, e.date) for e in ev], [("decreased", date(2026, 9, 1))])

    def test_out_of_order_points_are_sorted(self):
        pts = window_points(ISSUE_25_WINDOWS)
        shuffled = [pts[3], pts[0], pts[5], pts[1], pts[4], pts[2]]
        self.assertEqual(detect_weighted_changes(shuffled), detect_weighted_changes(pts))

    def test_current_regime_points_start_at_the_newest_events_window(self):
        pts = window_points(ISSUE_25_WINDOWS)
        self.assertEqual(current_regime_points(pts), pts[3:])
        self.assertEqual(current_regime_points(pts[:3]), pts[:3])
        # An earlier window on the event's own day is still the old level: not in the regime.
        same_day = window_points(ISSUE_25_WINDOWS[:3] + ["2026-09-14T05:00 30 5"] + ISSUE_25_WINDOWS[3:])
        self.assertEqual(current_regime_points(same_day), same_day[4:])

    def test_pooled_windows_is_total_five_hour_over_total_seven_day(self):
        self.assertAlmostEqual(pooled_windows(window_points(ISSUE_25_WINDOWS[3:])), 4.5)
        self.assertIsNone(pooled_windows([]))
        self.assertIsNone(pooled_windows([(datetime(2026, 9, 1), 5.0, 0.0)]))


if __name__ == "__main__":
    unittest.main()
