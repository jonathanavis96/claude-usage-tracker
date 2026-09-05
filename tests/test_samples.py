import unittest
from datetime import timedelta
from tracker.samples import parse_moonlighter, parse_ceiling_log, merge_samples

ML = ['{"ts": "2026-06-13T00:51:22.743614+02:00", "seven_day": {"utilization": 23.0, "resets_at": "2026-06-19T04:00:00Z"}, "seven_day_sonnet": {"utilization": 5.0, "resets_at": null}, "five_hour": {"utilization": 40.0, "resets_at": "2026-06-13T01:30:00Z"}}',
      '{"ts": "2026-06-13T01:30:02+02:00", "seven_day": {"utilization": 25.0, "resets_at": null}, "seven_day_sonnet": {"utilization": null, "resets_at": null}, "five_hour": {"utilization": null, "resets_at": null}}',
      'not json']
CL = ['2026-08-18T17:05:10+02:00 usage ceiling armed for systemd (x): warn 60% / hard 80%',
      '2026-08-18T17:07:19+02:00 5-hour 25% / 7-day 82% (warn 60% / hard 80% / week 90%)',
      '2026-08-18T17:08:19+02:00 READ FAILURE (1 consecutive) -- usage endpoint unreadable']

class SampleTests(unittest.TestCase):
    def test_moonlighter_skips_null_and_bad_lines(self):
        s = parse_moonlighter(ML)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0].five_hour, 40.0)
        self.assertEqual(s[0].resets_at, "2026-06-13T01:30:00Z")
        self.assertEqual(s[0].ts.tzinfo.utcoffset(None), timedelta(hours=2))
        self.assertEqual(s[0].source, "moonlighter")

    def test_ceiling_log_only_reading_lines(self):
        s = parse_ceiling_log(CL)
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0].five_hour, s[0].seven_day, s[0].resets_at, s[0].source), (25.0, 82.0, None, "ceiling"))

    def test_merge_sorts_and_dedups_to_minute(self):
        a = parse_moonlighter(['{"ts": "2026-08-18T17:07:40+02:00", "five_hour": {"utilization": 26.0, "resets_at": null}, "seven_day": {"utilization": 1.0}}'])
        b = parse_ceiling_log(CL)
        m = merge_samples(a, b)
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].source, "ceiling")
