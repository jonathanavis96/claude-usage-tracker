import unittest
from datetime import datetime, timezone

from tracker.crossings import Crossing, exact_delta, extract_all, extract_crossings
from tracker.samples import Sample, parse_meter_log

RESET_A = "2026-09-15T23:00:00.400000+00:00"
RESET_A_JITTER = "2026-09-15T23:00:00.862545+00:00"  # same window, sub-second jitter
RESET_B = "2026-09-16T04:00:00.400000+00:00"  # a genuinely different window


def ts(minute: int) -> datetime:
    return datetime(2026, 9, 15, 21, minute, 0, tzinfo=timezone.utc)


def sample(minute: int, five_hour: float, seven_day: float | None = 30.0,
           resets_at: str | None = RESET_A, seven_resets_at: str | None = "2026-09-17T23:00:00+00:00") -> Sample:
    return Sample(ts(minute), five_hour, seven_day, resets_at, "meter", seven_resets_at)


class CleanCrossingTests(unittest.TestCase):
    def test_a_one_percent_step_is_one_clean_crossing(self):
        samples = [sample(0, 5.0), sample(5, 6.0)]
        crossings = extract_crossings(samples, "five_hour")
        self.assertEqual(len(crossings), 1)
        c = crossings[0]
        self.assertEqual((c.meter, c.level, c.step, c.clean), ("five_hour", 6, 1, True))
        self.assertEqual((c.before_ts, c.after_ts), (ts(0), ts(5)))
        self.assertEqual(c.bracket_width_s, 300.0)

    def test_no_movement_is_no_crossing(self):
        samples = [sample(0, 5.0), sample(5, 5.0)]
        self.assertEqual(extract_crossings(samples, "five_hour"), [])


class MultiPercentJumpTests(unittest.TestCase):
    def test_a_multi_percent_jump_yields_one_crossing_per_level_sharing_the_bracket(self):
        samples = [sample(0, 5.0), sample(5, 8.0)]
        crossings = extract_crossings(samples, "five_hour")
        self.assertEqual([c.level for c in crossings], [6, 7, 8])
        self.assertTrue(all(c.step == 3 and not c.clean for c in crossings))
        self.assertTrue(all((c.before_ts, c.after_ts) == (ts(0), ts(5)) for c in crossings))


class ResetTests(unittest.TestCase):
    def test_a_window_reset_produces_no_crossing_even_though_the_value_drops(self):
        # five_hour drops from 90 to 2 and the reset stamp genuinely changes: a new window.
        samples = [sample(0, 90.0, resets_at=RESET_A), sample(5, 2.0, resets_at=RESET_B)]
        self.assertEqual(extract_crossings(samples, "five_hour"), [])

    def test_jittered_reset_timestamps_are_still_the_same_window(self):
        # resets_at jitters by sub-second amounts between reads (usage_api.same_reset);
        # a naive string compare would wrongly treat this pair as a reset and drop the crossing.
        samples = [sample(0, 5.0, resets_at=RESET_A), sample(5, 6.0, resets_at=RESET_A_JITTER)]
        crossings = extract_crossings(samples, "five_hour")
        self.assertEqual([c.level for c in crossings], [6])

    def test_a_value_drop_with_no_reset_stamp_change_is_still_treated_as_a_reset(self):
        # Some resets aren't caught by the stamp alone; join.py's _is_reset also treats
        # any drop as a reset, and this mirrors that.
        samples = [sample(0, 90.0, resets_at=RESET_A), sample(5, 2.0, resets_at=RESET_A)]
        self.assertEqual(extract_crossings(samples, "five_hour"), [])


class ExactDeltaTests(unittest.TestCase):
    def test_exact_delta_between_two_crossings_in_the_same_window(self):
        samples = [sample(0, 6.0), sample(5, 6.0),  # already at 6, no crossing recorded here
                   sample(10, 16.0)]
        crossings = extract_crossings(samples, "five_hour")
        # 6 -> 16 in one bracket: ten levels (7..16) share one wide bracket.
        low = next(c for c in crossings if c.level == 7)
        high = next(c for c in crossings if c.level == 16)
        delta = exact_delta(low, high)
        self.assertEqual(delta.percent, 9)  # exact: 16 - 7, no +-0.5 quantisation at either end
        self.assertGreaterEqual(delta.uncertainty_s, 0)

    def test_exact_delta_between_clean_crossings_has_tight_bounded_uncertainty(self):
        samples = [sample(0, 5.0), sample(5, 6.0), sample(10, 7.0)]
        crossings = extract_crossings(samples, "five_hour")
        low, high = crossings[0], crossings[1]
        delta = exact_delta(low, high)
        self.assertEqual(delta.percent, 1)
        # both brackets are 5 minutes wide, so the timing uncertainty is bounded by 2x that
        self.assertLessEqual(delta.uncertainty_s, 600.0)

    def test_exact_delta_refuses_crossings_from_different_windows(self):
        low = Crossing("five_hour", 6, ts(0), ts(5), 1, RESET_A)
        high = Crossing("five_hour", 7, ts(10), ts(15), 1, RESET_B)
        with self.assertRaises(ValueError):
            exact_delta(low, high)

    def test_exact_delta_refuses_crossings_from_different_meters(self):
        low = Crossing("five_hour", 6, ts(0), ts(5), 1, RESET_A)
        high = Crossing("seven_day", 7, ts(10), ts(15), 1, RESET_A)
        with self.assertRaises(ValueError):
            exact_delta(low, high)


class ErrorLineTests(unittest.TestCase):
    def test_error_and_gap_lines_are_skipped_and_never_produce_a_crossing(self):
        lines = [
            '{"ts": "2026-09-15T21:00:00+00:00", "account": "dave", "identity": "id1", '
            '"five_hour": {"utilization": 5.0, "resets_at": "%s"}, '
            '"seven_day": {"utilization": 30.0, "resets_at": "2026-09-17T23:00:00+00:00"}}' % RESET_A,
            '{"ts": "2026-09-15T21:05:00+00:00", "account": "dave", "identity": "id1", '
            '"error": "HTTPError: 429"}',
            '{"ts": "2026-09-15T21:10:00+00:00", "account": "dave", "identity": "id1", '
            '"five_hour": {"utilization": 6.0, "resets_at": "%s"}, '
            '"seven_day": {"utilization": 30.0, "resets_at": "2026-09-17T23:00:00+00:00"}}' % RESET_A_JITTER,
        ]
        samples = parse_meter_log(lines)
        self.assertEqual(len(samples), 2)  # the error line produced no Sample at all
        crossings = extract_crossings(samples, "five_hour")
        self.assertEqual(len(crossings), 1)
        self.assertEqual(crossings[0].level, 6)
        # the bracket spans the surviving readings, silently absorbing the gap where the
        # error line was -- it is not pretending the failed read never happened, it just
        # has nothing narrower to bound the crossing with.
        self.assertEqual(crossings[0].before_ts.minute, 0)
        self.assertEqual(crossings[0].after_ts.minute, 10)


class ExtractAllTests(unittest.TestCase):
    def test_extract_all_returns_both_meters(self):
        samples = [sample(0, 5.0, seven_day=20.0), sample(5, 6.0, seven_day=20.0)]
        result = extract_all(samples)
        self.assertEqual(set(result), {"five_hour", "seven_day"})
        self.assertEqual([c.level for c in result["five_hour"]], [6])
        self.assertEqual(result["seven_day"], [])

    def test_unknown_meter_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_crossings([], "not_a_meter")


if __name__ == "__main__":
    unittest.main()
