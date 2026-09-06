import unittest
from datetime import datetime, timezone

from tracker.weekly import parse_rows, probe_weekly_windows, weekly_windows


def row(ts, five, five_resets, seven, seven_resets):
    return {"ts": ts, "seven_day": {"utilization": seven, "resets_at": seven_resets},
            "five_hour": {"utilization": five, "resets_at": five_resets}}


class ParseRowsTests(unittest.TestCase):
    def test_skips_null_utilization_and_resets(self):
        lines = [
            '{"ts": "x", "seven_day": {"utilization": null, "resets_at": "r"}, "five_hour": {"utilization": 1, "resets_at": "r"}}',
            '{"ts": "x", "seven_day": {"utilization": 1, "resets_at": null}, "five_hour": {"utilization": 1, "resets_at": "r"}}',
            'not json',
            '',
        ]
        self.assertEqual(parse_rows(lines), [None, None, None])

    def test_parses_valid_row(self):
        import json
        d = row("2026-09-01T00:00:00+00:00", 17.0, "2026-09-06T16:59:59+00:00",
                54.0, "2026-09-11T03:59:59+00:00")
        rows = parse_rows([json.dumps(d)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["five_hour"], 17.0)
        self.assertEqual(rows[0]["seven_day"], 54.0)


class WeeklyWindowsTests(unittest.TestCase):
    def test_single_week_bucket(self):
        rows = [
            {"ts": "t0", "five_hour": 10.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 20.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
            {"ts": "t1", "five_hour": 70.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 30.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
        ]
        now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        result = weekly_windows(rows, now=now)
        self.assertEqual(result["history"], [
            {"week_ending": "2026-09-04", "windows": 6.0, "five_hour_pct": 60.0, "seven_day_pct": 10.0},
        ])
        self.assertEqual(result["current"], 6.0)

    def test_thin_week_skipped(self):
        rows = [
            {"ts": "t0", "five_hour": 10.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 20.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
            {"ts": "t1", "five_hour": 20.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 25.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
        ]
        result = weekly_windows(rows)
        self.assertEqual(result["history"], [])
        self.assertIsNone(result["current"])

    def test_different_five_hour_window_breaks_pairing(self):
        rows = [
            {"ts": "t0", "five_hour": 90.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 20.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
            {"ts": "t1", "five_hour": 5.0, "five_resets_at": "2026-09-06T21:59:59+00:00",
             "seven_day": 25.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
        ]
        result = weekly_windows(rows)
        self.assertEqual(result["history"], [])

    def test_none_row_resets_pairing_across_gap(self):
        rows = [
            {"ts": "t0", "five_hour": 10.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 20.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
            None,
            {"ts": "t1", "five_hour": 80.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 30.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
        ]
        result = weekly_windows(rows)
        self.assertEqual(result["history"], [])

    def test_negative_or_zero_deltas_excluded(self):
        rows = [
            {"ts": "t0", "five_hour": 50.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 30.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
            {"ts": "t1", "five_hour": 40.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 25.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
        ]
        result = weekly_windows(rows)
        self.assertEqual(result["history"], [])

    def test_current_excludes_incomplete_newest_week(self):
        rows = [
            {"ts": "t0", "five_hour": 10.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 20.0, "seven_resets_at": "2026-08-28T03:59:59+00:00"},
            {"ts": "t1", "five_hour": 70.0, "five_resets_at": "2026-09-06T16:59:59+00:00",
             "seven_day": 30.0, "seven_resets_at": "2026-08-28T03:59:59+00:00"},
            {"ts": "t2", "five_hour": 10.0, "five_resets_at": "2026-09-13T16:59:59+00:00",
             "seven_day": 20.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
            {"ts": "t3", "five_hour": 90.0, "five_resets_at": "2026-09-13T16:59:59+00:00",
             "seven_day": 30.0, "seven_resets_at": "2026-09-04T03:59:59+00:00"},
        ]
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)  # only the 08-28 week has already reset
        result = weekly_windows(rows, now=now)
        self.assertEqual(len(result["history"]), 2)
        self.assertEqual(result["current"], 6.0)  # median of the single complete week

    def test_real_log_matches_hand_validated_ranges(self):
        with open("/home/grafe/.moonlighter/usage_log.jsonl", encoding="utf-8") as f:
            rows = parse_rows(f)
        result = weekly_windows(rows)
        by_week = {h["week_ending"]: h["windows"] for h in result["history"]}
        for wk in ("2026-06-19", "2026-06-26", "2026-07-03", "2026-07-10", "2026-07-17",
                   "2026-07-24", "2026-07-31", "2026-08-07", "2026-08-14"):
            self.assertGreaterEqual(by_week[wk], 9.5, wk)
            self.assertLessEqual(by_week[wk], 11.1, wk)
        for wk in ("2026-08-21", "2026-08-28", "2026-09-04"):
            self.assertGreaterEqual(by_week[wk], 6.3, wk)
            self.assertLessEqual(by_week[wk], 6.8, wk)
        self.assertAlmostEqual(by_week["2026-09-11"], 5.6, delta=0.1)


def probe_row(ts, fhb, fha, sdb, sda, seven_resets=None):
    return {"ts": ts, "five_hour_before": fhb, "five_hour_after": fha,
            "seven_day_before": sdb, "seven_day_after": sda,
            **({"seven_day_resets_at": seven_resets} if seven_resets else {})}


class ProbeWeeklyWindowsTests(unittest.TestCase):
    def test_rows_missing_five_hour_fields_are_skipped(self):
        rows = [{"ts": "2026-09-01T00:00:00+00:00", "seven_day_before": 10.0, "seven_day_after": 12.0}]
        self.assertEqual(probe_weekly_windows(rows), {"current": None, "history": []})

    def test_negative_five_hour_delta_skipped_as_reset_crossing(self):
        rows = [probe_row("2026-09-01T00:00:00+00:00", 90.0, 5.0, 10.0, 12.0)]
        self.assertEqual(probe_weekly_windows(rows), {"current": None, "history": []})

    def test_negative_seven_day_delta_skipped(self):
        rows = [probe_row("2026-09-01T00:00:00+00:00", 10.0, 40.0, 12.0, 10.0)]
        self.assertEqual(probe_weekly_windows(rows), {"current": None, "history": []})

    def test_below_min_five_hour_pct_skipped(self):
        rows = [probe_row("2026-09-01T00:00:00+00:00", 10.0, 15.0, 10.0, 12.0)]
        self.assertEqual(probe_weekly_windows(rows), {"current": None, "history": []})

    def test_buckets_by_seven_day_resets_at_when_present(self):
        rows = [
            probe_row("2026-09-01T00:00:00+00:00", 10.0, 35.0, 10.0, 12.0, "2026-09-04T03:59:59+00:00"),
            probe_row("2026-09-02T00:00:00+00:00", 10.0, 40.0, 10.0, 13.0, "2026-09-04T03:59:59+00:00"),
        ]
        now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        result = probe_weekly_windows(rows, now=now)
        self.assertEqual(result["history"], [
            {"week_ending": "2026-09-04", "windows": 11.0, "five_hour_pct": 55.0, "seven_day_pct": 5.0},
        ])
        self.assertEqual(result["current"], 11.0)

    def test_falls_back_to_iso_week_ending_without_resets_at(self):
        # 2026-09-01 is a Tuesday in ISO week ending Sunday 2026-09-06.
        rows = [probe_row("2026-09-01T00:00:00+00:00", 10.0, 35.0, 10.0, 12.0)]
        now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        result = probe_weekly_windows(rows, now=now)
        self.assertEqual(result["history"], [
            {"week_ending": "2026-09-06", "windows": 12.5, "five_hour_pct": 25.0, "seven_day_pct": 2.0},
        ])

    def test_current_excludes_incomplete_week(self):
        rows = [probe_row("2026-09-01T00:00:00+00:00", 10.0, 35.0, 10.0, 12.0, "2026-09-04T03:59:59+00:00")]
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)  # before the week has ended
        result = probe_weekly_windows(rows, now=now)
        self.assertEqual(len(result["history"]), 1)
        self.assertIsNone(result["current"])


if __name__ == "__main__":
    unittest.main()
