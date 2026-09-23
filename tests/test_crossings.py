import unittest
from datetime import datetime, timedelta, timezone

from tracker.crossings import crossings, tokens_between_crossings, windows_per_week_from_crossings
from tracker.samples import Sample
from tracker.turns import Turn

T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def dt(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def sample(m, five, seven=None, resets="R1", seven_resets="W1", source="test"):
    return Sample(dt(m), five, seven, resets, source, seven_resets)


class CrossingsTests(unittest.TestCase):
    def test_single_step_crossing(self):
        samples = [sample(0, 40.0, 10.0), sample(5, 41.0, 10.0)]
        cs = crossings(samples, "five_hour")
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual(c.value, 41)
        self.assertEqual(c.lower, dt(0))
        self.assertEqual(c.upper, dt(5))
        self.assertEqual(c.window_id, "R1")
        self.assertTrue(c.reset_verified)
        self.assertEqual(c.gap, timedelta(minutes=5))

    def test_multi_point_jump_yields_one_crossing_per_integer_same_bracket(self):
        samples = [sample(0, 40.0), sample(5, 43.0)]
        cs = crossings(samples, "five_hour")
        self.assertEqual([c.value for c in cs], [41, 42, 43])
        self.assertTrue(all(c.lower == dt(0) and c.upper == dt(5) for c in cs))

    def test_no_crossing_when_meter_does_not_rise(self):
        samples = [sample(0, 40.0), sample(5, 40.0)]
        self.assertEqual(crossings(samples, "five_hour"), [])

    def test_reset_by_id_change_breaks_pairing(self):
        samples = [sample(0, 99.0, resets="R1"), sample(5, 2.0, resets="R2")]
        self.assertEqual(crossings(samples, "five_hour"), [])

    def test_reset_by_value_drop_breaks_pairing_even_with_same_id(self):
        # A same-second resets_at with a lower reading is still a reset (same_reset is lenient).
        samples = [sample(0, 99.0, resets="R1"), sample(5, 2.0, resets="R1")]
        self.assertEqual(crossings(samples, "five_hour"), [])

    def test_gap_wider_than_max_gap_breaks_pairing(self):
        samples = [sample(0, 40.0), sample(20, 41.0)]
        self.assertEqual(crossings(samples, "five_hour", max_gap=timedelta(minutes=15)), [])

    def test_crossings_never_span_a_reset_across_three_samples(self):
        samples = [sample(0, 98.0, resets="R1"), sample(5, 1.0, resets="R2"), sample(10, 2.0, resets="R2")]
        cs = crossings(samples, "five_hour")
        self.assertEqual([c.value for c in cs], [2])
        self.assertEqual(cs[0].window_id, "R2")

    def test_seven_day_meter_uses_seven_resets_at(self):
        samples = [sample(0, 40.0, 10.0, seven_resets="W1"), sample(5, 41.0, 11.0, seven_resets="W1")]
        cs = crossings(samples, "seven_day")
        self.assertEqual([c.value for c in cs], [11])
        self.assertEqual(cs[0].window_id, "W1")

    def test_missing_seven_day_value_produces_nothing_and_resets_the_chain(self):
        samples = [sample(0, 40.0, None), sample(5, 41.0, 11.0), sample(10, 42.0, 12.0)]
        cs = crossings(samples, "seven_day")
        # The first pair has no seven_day value at all; only the second pair can cross.
        self.assertEqual([c.value for c in cs], [12])

    def test_unsorted_input_is_sorted_first(self):
        samples = [sample(5, 41.0), sample(0, 40.0)]
        cs = crossings(samples, "five_hour")
        self.assertEqual([c.value for c in cs], [41])

    def test_unknown_meter_raises(self):
        with self.assertRaises(ValueError):
            crossings([], "three_day")


def _five_hour_crossings(values_and_minutes):
    """Build five-hour Crossing objects directly spaced `minutes` apart, one per value."""
    samples = [sample(m, v) for m, v in values_and_minutes]
    return crossings(samples, "five_hour")


class WindowsPerWeekFromCrossingsTests(unittest.TestCase):
    def test_definite_five_hour_points_counted_between_seven_day_crossings(self):
        # Seven-day crossings bracketed at [0,10] (value 11) and [50,60] (value 12), k=1 apart.
        seven = crossings([sample(0, 40.0, 10.0), sample(10, 40.0, 11.0),
                           sample(50, 40.0, 11.0), sample(60, 40.0, 12.0)], "seven_day")
        # Five-hour crossings squarely inside [10, 50]: three of them, well clear of both ends.
        five = _five_hour_crossings([(10, 40.0), (20, 41.0), (30, 42.0), (40, 43.0)])
        pairs = windows_per_week_from_crossings(five, seven, k=1)
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual(p["five_hour_points_definite"], 3)  # 41, 42, 43 land inside [0,60]
        self.assertEqual(p["five_hour_points_ambiguous"], 0)
        self.assertEqual(p["windows"], 3.0)
        self.assertEqual(p["windows_bounds"], [3.0, 3.0])

    def test_ambiguous_crossings_widen_the_bounds_not_the_midpoint_by_half(self):
        # Seven-day crossing brackets: [0,10] for value 10->11, [50,60] for value 11->12.
        seven = crossings([sample(0, 40.0, 10.0), sample(10, 40.0, 11.0),
                           sample(50, 40.0, 11.0), sample(60, 40.0, 12.0)], "seven_day")
        # One five-hour crossing overlapping the low bracket [0,10] (its own bracket is [5,15]),
        # one squarely inside, one overlapping the high bracket [50,60] (bracket [45,55]).
        five = crossings([sample(5, 40.0), sample(15, 41.0), sample(30, 41.0), sample(31, 42.0),
                          sample(45, 42.0), sample(55, 43.0)], "five_hour")
        pairs = windows_per_week_from_crossings(five, seven, k=1)
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual(p["five_hour_points_definite"], 1)  # the 42 crossing at [30,31]
        self.assertEqual(p["five_hour_points_ambiguous"], 2)  # the 41 and 43 crossings
        self.assertEqual(p["windows"], 2.0)  # (1 + 2/2) / 1
        self.assertEqual(p["windows_bounds"], [1.0, 3.0])

    def test_overlapping_seven_day_brackets_from_one_multi_point_jump_are_skipped(self):
        # A single pair that jumps by two points yields two crossings sharing one bracket --
        # too close together (zero confirmed span) for a k=1 pair to say anything safe.
        seven = crossings([sample(0, 40.0, 10.0), sample(10, 40.0, 12.0)], "seven_day")
        self.assertEqual(len(seven), 2)
        self.assertEqual(windows_per_week_from_crossings([], seven, k=1), [])

    def test_k_greater_than_available_crossings_yields_nothing(self):
        seven = crossings([sample(0, 40.0, 10.0), sample(10, 40.0, 11.0)], "seven_day")
        self.assertEqual(windows_per_week_from_crossings([], seven, k=5), [])


class TokensBetweenCrossingsTests(unittest.TestCase):
    def _turn(self, m, total):
        return Turn(dt(m), "claude-sonnet-5", total, 0, 0, 0)

    def test_tokens_summed_in_the_confirmed_span_only(self):
        # Two crossings with a gap between their brackets: 41 over [0,10], 42 over [50,60].
        five = crossings([sample(0, 40.0), sample(10, 41.0), sample(50, 41.0), sample(60, 42.0)], "five_hour")
        turns = [self._turn(5, 100), self._turn(30, 200), self._turn(55, 400)]
        out = tokens_between_crossings(five, turns, n=1)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r["tokens"], 200)  # only the turn at minute 30 is inside [10, 50]
        self.assertEqual(r["tokens_per_pct"], 200.0)
        self.assertEqual(r["timing_error_tokens"], {"lower_gap": 100, "upper_gap": 400})

    def test_n_greater_than_one_divides_tokens_per_pct(self):
        # Three separate-bracket crossings 41,42,43; n=2 pairs the first and third.
        five = crossings([sample(0, 40.0), sample(10, 41.0), sample(20, 42.0), sample(30, 43.0)], "five_hour")
        turns = [self._turn(15, 400)]
        out = tokens_between_crossings(five, turns, n=2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["tokens"], 400)
        self.assertEqual(out[0]["tokens_per_pct"], 200.0)

    def test_no_turns_gives_zero_tokens(self):
        five = crossings([sample(0, 40.0), sample(5, 41.0), sample(15, 41.0), sample(20, 42.0)], "five_hour")
        self.assertEqual(tokens_between_crossings(five, [], n=1)[0]["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
