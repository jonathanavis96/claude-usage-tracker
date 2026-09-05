import unittest
from datetime import date, timedelta
from tracker.join import DailyRate
from tracker.passive import passive_summary


def dr(v, split=None, interp=False):
    return DailyRate(v, 6, interp, {}, split or {"input": .1, "output": .1, "cache_read": .7, "cache_write": .1})


class PassiveTests(unittest.TestCase):
    def test_ratio_and_split_from_last_14_days(self):
        change = date(2026, 8, 18)
        rates = {change - timedelta(days=i): dr(100000) for i in range(1, 15)}
        rates.update({change + timedelta(days=i): dr(400000) for i in range(0, 15)})
        s = passive_summary(rates, change)
        self.assertAlmostEqual(s["plan_ratio_5x_to_20x"], 0.25)
        self.assertEqual(s["split"]["cache_read"], .7)
        self.assertEqual(len(s["history"]), 29)
        self.assertEqual(s["history"]["2026-08-18"], {"tokens_per_pct": 400000, "interpolated": False})
        self.assertIn("generated_at", s)
