import unittest
from datetime import datetime, timezone, timedelta, date
from tracker.samples import Sample
from tracker.turns import Turn
from tracker.join import build_intervals, daily_rates

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
def S(mins, fh, reset="r1"): return Sample(T0 + timedelta(minutes=mins), fh, None, reset, "ceiling")
def T(mins, model="claude-sonnet-5", cr=100_000, inp=1000, out=500, cw=0):
    return Turn(T0 + timedelta(minutes=mins), model, inp, out, cr, cw)

class IntervalTests(unittest.TestCase):
    def test_basic_interval_and_attribution(self):
        iv = build_intervals([S(0, 10), S(5, 12)], [T(1), T(2), T(3)])
        self.assertEqual(len(iv), 1)
        self.assertEqual(iv[0].delta_pct, 2.0)
        self.assertEqual(iv[0].tokens["cache_read"], 300_000)
        self.assertEqual(iv[0].model, "claude-sonnet-5")

    def test_mixed_interval_has_no_model(self):
        iv = build_intervals([S(0, 10), S(5, 12)], [T(1), T(2, model="claude-opus-5")])
        self.assertIsNone(iv[0].model)

    def test_zero_delta_pooled_into_next(self):
        iv = build_intervals([S(0, 10), S(5, 10), S(10, 11)], [T(1), T(7)])
        self.assertEqual(len(iv), 1)
        self.assertEqual((iv[0].start, iv[0].end, iv[0].delta_pct), (S(0, 10).ts, S(10, 11).ts, 1.0))
        self.assertEqual(iv[0].tokens["cache_read"], 200_000)

    def test_reset_straddle_dropped(self):
        iv = build_intervals([S(0, 90), S(5, 3, reset="r2"), S(10, 5, reset="r2")], [T(1), T(7)])
        self.assertEqual(len(iv), 1)
        self.assertEqual(iv[0].start, S(5, 3).ts)

    def test_utilization_drop_without_reset_field_is_a_reset(self):
        iv = build_intervals([S(0, 90, reset=None), S(5, 3, reset=None)], [T(1)])
        self.assertEqual(iv, [])

class DailyTests(unittest.TestCase):
    def test_median_and_interpolation(self):
        samples = [S(i * 5, 10 + i) for i in range(7)]           # 6 intervals of 1% on day 1
        turns = [T(i * 5 + 1, cr=100_000 * (i + 1)) for i in range(6)]
        day2 = [Sample(T0 + timedelta(days=1, minutes=m), 10 + m // 5, None, "r9", "ceiling") for m in (0, 5)]
        iv = build_intervals(samples + day2, turns + [Turn(T0 + timedelta(days=1, minutes=1), "claude-sonnet-5", 0, 0, 5, 0)])
        rates = daily_rates(iv)
        d1 = rates[date(2026, 9, 1)]
        self.assertEqual(d1.n, 6)
        self.assertFalse(d1.interpolated)
        # tokens per interval: 101.5k, 201.5k, ... 601.5k -> median of 6 = (301500+401500)/2
        self.assertAlmostEqual(d1.tokens_per_pct, 351_500)
        self.assertIn("claude-sonnet-5", d1.per_model)
        self.assertAlmostEqual(sum(d1.split.values()), 1.0)
        d2 = rates[date(2026, 9, 2)]
        self.assertTrue(d2.interpolated)
        self.assertAlmostEqual(d2.tokens_per_pct, d1.tokens_per_pct)
