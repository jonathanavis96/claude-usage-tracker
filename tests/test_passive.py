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
        rates.update({change + timedelta(days=i): dr(400000) for i in range(15)})
        s = passive_summary(rates, change)
        self.assertAlmostEqual(s["plan_ratio_5x_to_20x"], 0.25)
        self.assertEqual(s["split"]["cache_read"], .7)
        self.assertEqual(len(s["history"]), 29)
        self.assertEqual(s["history"]["2026-08-18"], {"tokens_per_pct": 400000, "interpolated": False})
        self.assertIn("generated_at", s)
        self.assertEqual(s["session_tokens"], {})

    def test_session_tokens_passthrough(self):
        s = passive_summary({}, session_tokens={"claude-sonnet-5": 123456})
        self.assertEqual(s["session_tokens"], {"claude-sonnet-5": 123456})


class StoredHistoryTests(unittest.TestCase):
    """Claude Code deletes old transcripts, so a full rebuild loses the oldest days
    (2026-09-21: history/passive.json fell back from 2026-08-11 to 2026-08-20 and the
    ratio went to None). Days the transcripts no longer reach are kept as stored."""

    change = date(2026, 8, 18)

    def stored(self):
        days = {self.change - timedelta(days=i): 100000 for i in range(1, 15)}
        days.update({self.change + timedelta(days=i): 400000 for i in range(15)})
        return {"history": {d.isoformat(): {"tokens_per_pct": v, "interpolated": False}
                            for d, v in days.items()},
                "plan_ratio_5x_to_20x": 0.25}

    def test_days_before_the_rebuild_reaches_are_kept(self):
        # The transcripts now start 5 days after the change.
        rates = {self.change + timedelta(days=i): dr(500000) for i in range(5, 20)}
        s = passive_summary(rates, self.change, previous=self.stored())
        self.assertEqual(min(s["history"]), (self.change - timedelta(days=14)).isoformat())
        self.assertEqual(s["history"][(self.change - timedelta(days=1)).isoformat()]["tokens_per_pct"], 100000)
        # Days the rebuild still covers are recounted, not kept.
        self.assertEqual(s["history"][(self.change + timedelta(days=5)).isoformat()]["tokens_per_pct"], 500000)
        self.assertIsNotNone(s["plan_ratio_5x_to_20x"])

    def test_a_dropped_day_inside_the_rebuilt_range_is_not_resurrected(self):
        rates = {self.change + timedelta(days=i): dr(500000) for i in (5, 7)}
        s = passive_summary(rates, self.change, previous=self.stored())
        self.assertNotIn((self.change + timedelta(days=6)).isoformat(), s["history"])

    def test_unreadable_previous_is_ignored(self):
        rates = {self.change: dr(400000)}
        for bad in (None, {}, {"history": "x"}, {"history": {"not-a-date": {}}}, [1]):
            s = passive_summary(rates, self.change, previous=bad)
            self.assertEqual(list(s["history"]), [self.change.isoformat()])

    def test_main_merges_the_file_already_at_out(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from tracker import passive
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "passive.json"
            out.write_text(json.dumps(self.stored()))
            rates = {self.change + timedelta(days=i): dr(500000) for i in range(5, 20)}
            with mock.patch.object(passive, "_rebuild", return_value=(rates, {}, None)):
                self.assertEqual(passive.main(["--out", str(out)]), 0)
            written = json.loads(out.read_text())
            self.assertEqual(len(written["history"]), 19 + 15)
