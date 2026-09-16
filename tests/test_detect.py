import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tracker.detect import (current_regime_points, detect_changes, detect_smoothed_changes, detect_weighted_changes,
                            latest_change, pooled_windows, weighted_regimes)


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

# weekly_windows() over the whole real ~/.moonlighter/usage_log.jsonl as it
# stood on 2026-09-15 (every five-hour window from 2026-06-13, both plans): the
# weekly_windows block passive.json carries, by_window included.
#
# Regenerated 2026-09-16 from the same log lines (through 2026-09-15T22:00Z) after
# the audit's finding-3 repair: both meters' movement is kept whichever moved,
# and pairs across the endpoint's resets_at jitter are no longer dropped, so the
# fixture holds about 60% more paired movement than the one PR #28 committed.
REAL_WEEKLY = json.loads((Path(__file__).parent / "fixtures" / "passive_weekly_windows_2026-09-15.json")
                         .read_text(encoding="utf-8"))


def real_points(start, end):
    """The real log's (window_ending, d5, d7, pieces) points with start <= window_ending < end (ISO prefixes)."""
    return [(datetime.fromisoformat(w["window_ending"]), w["five_hour_pct"], w["seven_day_pct"], w["pieces"])
            for w in REAL_WEEKLY["by_window"] if start <= w["window_ending"] < end]


class WeightedDetectTests(unittest.TestCase):
    def test_the_six_quoted_issue_25_windows_alone_cannot_certify_the_cut(self):
        # 116/19 = 6.11 before, 81/18 = 4.50 after reads as a 26% cut, and this
        # used to fire on these six windows alone. Each side is three separate
        # rounded differences, and three points of rounding on a d7 of 19 or 18
        # let both sides sit near 5.3 (audit 2026-09-16, finding 4): the quoted
        # windows do not carry enough movement to state a change.
        self.assertEqual(detect_weighted_changes(window_points(ISSUE_25_WINDOWS)), [])
        self.assertEqual(detect_weighted_changes(window_points(ISSUE_25_WINDOWS[:5])), [])

    def test_the_real_max20_series_certifies_the_cut_from_the_evening_of_2026_09_14(self):
        # The whole max20 series in the log, not the quoted windows: a fortnight
        # and more of pre-cut movement (d7 290, about 6.5) narrows the old level
        # enough that the first two post-cut windows already separate from it.
        # Dated by the first window at the new level (09-14T11:30, 9/2), with the
        # last old-level window (09-13) as the earliest onset.
        for until, percent in (("2026-09-14T17", 29), ("2026-09-14T22", 31)):
            with self.subTest(until=until):
                ev = detect_weighted_changes(real_points("2026-08-19T03", until))
                self.assertEqual([(e.direction, e.percent, e.date) for e in ev],
                                 [("decreased", percent, date(2026, 9, 14))])
                self.assertEqual((ev[0].onset_earliest, ev[0].confirmed_at), (date(2026, 9, 13), date(2026, 9, 14)))
                self.assertLess(ev[0].after_interval[1], ev[0].before_interval[0])

    def test_the_real_cut_sits_at_the_methods_resolution_on_2026_09_15(self):
        # A day later the post-cut pool still reads about 4.5 against 6.5, but the
        # thin windows that arrived with it (27/6, 1/0) each add a piece of
        # rounding, and its interval's upper end reaches back over the pre-cut
        # interval's lower end. The detector says nothing rather than claim it;
        # the module docstring records this as the method's resolution.
        points = real_points("2026-08-19T03", "2026-09-16")
        self.assertEqual(detect_weighted_changes(points), [])
        cut = next(i for i, p in enumerate(points) if p[0].isoformat().startswith("2026-09-14T11:30"))
        self.assertLess(pooled_windows(points[cut:]) / pooled_windows(points[:cut]) - 1, -0.15)

    def test_one_post_step_window_is_not_enough_to_confirm(self):
        # 09-14T16:30 alone (d7=8) is a candidate but not a confirmation: under
        # MIN_POOL_D7 and only one window.
        self.assertEqual(detect_weighted_changes(window_points(ISSUE_25_WINDOWS[:4])), [])

    def test_issue_25_pre_step_windows_alone_are_flat(self):
        # 6.55, 5.67, 5.40 vary by 18% between single windows, but that is
        # whole-percent rounding on d7 of 3 and 5, not a step.
        self.assertEqual(detect_weighted_changes(window_points(ISSUE_25_WINDOWS[:3])), [])

    def test_a_thin_low_window_between_normal_ones_is_not_a_change(self):
        # A d7=2 window reading 3.0 (vs 6.0) is mostly rounding, and pooled with the
        # normal windows after it the level moves 4.5%: the series is flat.
        pts = window_points(["2026-09-01T05:00 60 10", "2026-09-01T10:00 60 10",
                             "2026-09-02T05:00 6 2", "2026-09-02T10:00 60 10", "2026-09-02T15:00 60 10"])
        self.assertEqual(detect_weighted_changes(pts), [])

    def test_the_real_first_week_of_max20_certifies_nothing(self):
        # The real first week of Max 20x, where a single-window vote floor of 3
        # once published "increased 38%" (08-23) from thin windows. Its pooled
        # sides are a handful of pieces each, and their rounding intervals overlap.
        self.assertEqual(detect_weighted_changes(real_points("2026-08-19T03", "2026-08-25T01")), [])

    def test_the_real_logs_flat_max5_stretch_reports_nothing(self):
        # The longest flat stretch the real log holds: Max 5x from the first
        # logged window to the end of its last full week, 2026-08-14 (calendar
        # weeks 9.5-11.0). Production never detects on max5, which is frozen,
        # so this is an out-of-sample false-positive check on the rounding
        # concession (tracker/detect.py, why ROUNDING_Z is 2). It stops at 08-15
        # because from the evening of 08-14 the windows already read at the Max
        # 20x level (68/10 = 6.80, 75/12 = 6.25): a real step, the plan move.
        self.assertEqual(detect_weighted_changes(real_points("2026-06-13", "2026-08-15")), [])

    def test_a_candidate_that_the_pool_does_not_confirm_is_dropped(self):
        # One d7=8 window at 3.75 is a 37% cut on its own and votes (30/7.5 is
        # still 33% down); pooled with the next window (d7=10 at 6.0) it is
        # 90/18 = 5.0, and 90/17.5 = 5.14 is inside 15% of 6.0.
        pts = window_points(["2026-09-01T05:00 60 10", "2026-09-01T10:00 60 10",
                             "2026-09-02T05:00 30 8", "2026-09-02T10:00 60 10", "2026-09-02T15:00 60 10"])
        self.assertEqual(detect_weighted_changes(pts), [])

    def test_a_single_heavy_window_cannot_confirm_itself(self):
        # d7=12 clears MIN_POOL_D7 on its own but the two meters do not always
        # advance in step within one window, so one window is never a change.
        pts = window_points(["2026-09-01T05:00 60 10", "2026-09-01T10:00 60 10", "2026-09-02T05:00 48 12"])
        self.assertEqual(detect_weighted_changes(pts), [])

    def test_rounding_uncertainty_grows_with_pieces_not_with_a_windows_thinness(self):
        # The same pooled movement after a 6.0 level, 90/20 = 4.5 (-25%), either as
        # two windows or as ten. Two pieces concede two points of rounding to each
        # total (92/18 = 5.11 at most, clear of the old level's 5.47); ten concede
        # 2 x sqrt(10) = 6.3 (96.3/13.7 = 7.04 at most), and the change is not certified.
        base = [(datetime(2026, 9, 1, 5) + timedelta(hours=5 * i), 60.0, 10.0) for i in range(6)]
        later = datetime(2026, 9, 5)
        two = base + [(later + timedelta(hours=5 * i), 45.0, 10.0) for i in range(2)]
        ten = base + [(later + timedelta(hours=5 * i), 9.0, 2.0) for i in range(10)]
        self.assertEqual([(e.direction, e.percent) for e in detect_weighted_changes(two)], [("decreased", 25)])
        self.assertEqual(detect_weighted_changes(ten), [])
        # A point that is itself two pieces (a gap inside one window) counts as two.
        self.assertEqual(detect_weighted_changes(base + [(later, 45.0, 10.0, 5), (later + timedelta(hours=5), 45.0, 10.0, 5)]), [])

    def test_audit_finding_4_a_persistent_cut_in_light_windows_is_found(self):
        # Four windows at 60/10 then twenty-one at 18/4: no single window is heavy,
        # but the pooled evidence of a 25% cut is ample (84 points of d7 after it).
        t0 = datetime(2026, 8, 20)
        points = [(t0 + timedelta(days=i), 60.0, 10.0) for i in range(4)]
        points += [(t0 + timedelta(days=i), 18.0, 4.0) for i in range(4, 25)]
        ev = detect_weighted_changes(points)
        self.assertEqual([(e.direction, e.percent, e.date) for e in ev], [("decreased", 25, date(2026, 8, 24))])
        self.assertEqual((ev[0].onset_earliest, ev[0].evidence_points, ev[0].denominator_pct),
                         (date(2026, 8, 23), 25, 84.0))
        self.assertIsNotNone(ev[0].confirmed_at)
        self.assertEqual([r["windows"] for r in weighted_regimes(points)], [6.0, 4.5])

    def test_audit_finding_4_rounding_alone_does_not_invent_a_step(self):
        # Ten (11, 1) windows then two (47, 8): -47% on the raw ratios, yet every
        # reading is within a point of a constant ratio of 6 (true d7 11/6 and 47/6).
        t0 = datetime(2026, 8, 20)
        points = [(t0 + timedelta(hours=6 * i), 11.0, 1.0) for i in range(10)]
        points += [(t0 + timedelta(hours=6 * i), 47.0, 8.0) for i in range(10, 12)]
        self.assertEqual(detect_weighted_changes(points), [])
        self.assertEqual(len(weighted_regimes(points)), 1)

    def test_base_needs_ten_points_of_seven_day_movement(self):
        pts = window_points(["2026-09-01T05:00 30 5", "2026-09-02T05:00 20 5", "2026-09-02T10:00 20 5",
                             "2026-09-02T15:00 20 5"])
        self.assertEqual(detect_weighted_changes(pts), [])

    def test_does_not_refire_on_the_new_plateau_and_bases_the_next_step_on_it(self):
        # Eight windows at 6.0, eight at 4.5, six at 9.0: two steps, each measured
        # against the level just before it (the second is +100% on 4.5, not +50% on
        # 6.0), and the eight windows on the 4.5 plateau fire nothing further. (The
        # issue #25 windows this test used to extend no longer certify on their own.)
        day = datetime(2026, 9, 1)
        pts = [(day + timedelta(hours=5 * i), 60.0, 10.0) for i in range(8)]
        pts += [(day + timedelta(days=3, hours=5 * i), 45.0, 10.0) for i in range(8)]
        pts += [(day + timedelta(days=6, hours=5 * i), 90.0, 10.0) for i in range(6)]
        ev = detect_weighted_changes(pts)
        self.assertEqual([(e.direction, e.percent, e.date) for e in ev],
                         [("decreased", 25, date(2026, 9, 4)), ("increased", 100, date(2026, 9, 7))])
        self.assertEqual([r["windows"] for r in weighted_regimes(pts)], [6.0, 4.5, 9.0])

    def test_a_slow_drift_never_fires_but_a_step_after_it_does(self):
        # A month drifting from 6.0 to 5.4 never fires (no split of it separates
        # two levels 15% apart); a 30% step on top of it still does.
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
        base = [f"2026-09-0{d}T05:00 60 10" for d in range(1, 9)]
        cut = ["2026-09-10T16:30 30 10", "2026-09-10T21:30 30 10", "2026-09-11T16:30 30 10"]
        pts = window_points(base + cut)
        self.assertEqual(current_regime_points(pts), pts[8:])
        self.assertEqual(current_regime_points(pts[:8]), pts[:8])
        # An earlier window on the event's own day is still the old level: not in the regime.
        same_day = window_points(base + ["2026-09-10T05:00 60 10"] + cut)
        self.assertEqual(current_regime_points(same_day), same_day[9:])

    def test_pooled_windows_is_total_five_hour_over_total_seven_day(self):
        self.assertAlmostEqual(pooled_windows(window_points(ISSUE_25_WINDOWS[3:])), 4.5)
        self.assertIsNone(pooled_windows([]))
        self.assertIsNone(pooled_windows([(datetime(2026, 9, 1), 5.0, 0.0)]))


if __name__ == "__main__":
    unittest.main()


class SmoothedDetectTests(unittest.TestCase):
    """detect_smoothed_changes: a rolling week's median against the regime before it."""

    def daily(self, values, start=datetime(2026, 9, 5)):
        return readings(values, start=start, step_days=1)

    def test_a_noisy_flat_passive_series_fires_nothing(self):
        # jwork's real per-day figures 2026-09-05..15 with sub-agents counted, scaled: cv 0.15.
        r = self.daily([1.91, 1.81, 1.52, 2.21, 2.20, 1.97, 1.88, 1.73, 1.39, 1.80, 2.05, 1.70])
        self.assertEqual(detect_changes(r)[:1] and True, True)  # the old rule fires on this series
        self.assertEqual(detect_smoothed_changes(r), [])

    def test_a_sustained_step_fires_and_is_dated_where_the_new_level_began(self):
        r = self.daily([100] * 8 + [140] * 8)
        ev = detect_smoothed_changes(r)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].direction, ev[0].percent), ("increased", 40))
        self.assertEqual(ev[0].date, r[8][0].date())

    def test_a_two_day_spike_does_not_fire(self):
        r = self.daily([100] * 8 + [160, 160] + [100] * 6)
        self.assertEqual(detect_smoothed_changes(r), [])

    def test_needs_min_points_on_both_sides(self):
        self.assertEqual(detect_smoothed_changes(self.daily([100, 100, 100, 150, 150, 150, 150])), [])

    def test_does_not_refire_within_the_new_regime(self):
        r = self.daily([100] * 8 + [70] * 20)
        ev = detect_smoothed_changes(r)
        self.assertEqual([(e.direction, e.percent) for e in ev], [("decreased", 30)])
