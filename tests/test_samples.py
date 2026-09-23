import json
import unittest
from datetime import datetime, timedelta, timezone

from tracker.samples import (
    MixedAccountLog,
    Sample,
    infer_resets,
    merge_samples,
    parse_ceiling_log,
    parse_gs_ceiling_log,
    parse_log,
    parse_meter_log,
    parse_moonlighter,
)

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


# gs's own usage-ceiling.py log: a different line format from masterrig's ceiling.
GS = ['2026-09-05T05:54:05+00:00 HARD CEILING (five-hour) five_hour=100% seven_day=31%',
      '2026-09-05T05:54:05+00:00   pausing every seat',
      '2026-09-05T05:54:06+00:00   paused 0 seat(s)',
      '2026-09-05T06:19:55+00:00 ok five_hour=4% seven_day=19%',
      '2026-09-05T06:25:25+00:00 usage read FAILED — failing open, nothing paused',
      '2026-09-05T06:36:05+00:00 warn five_hour=61% seven_day=19% (hard at 80%/90%)']


class GsCeilingLogTests(unittest.TestCase):
    def test_reads_ok_warn_and_hard_ceiling_lines_and_nothing_else(self):
        s = parse_gs_ceiling_log(GS)
        self.assertEqual([(x.five_hour, x.seven_day) for x in s], [(100.0, 31.0), (4.0, 19.0), (61.0, 19.0)])
        self.assertEqual({x.source for x in s}, {"gs-ceiling"})
        self.assertIsNone(s[0].resets_at)  # this log never records a reset time

    def test_since_drops_the_samples_another_account_wrote(self):
        # The log read ~/.claude until the 2026-09-05 seat.conf drop-in pointed it at jwork.
        since = datetime(2026, 9, 5, 6, 14, 32, tzinfo=timezone.utc)
        s = parse_gs_ceiling_log(GS, since=since)
        self.assertEqual([x.five_hour for x in s], [4.0, 61.0])

    def test_merges_with_the_other_sources(self):
        self.assertEqual(len(merge_samples(parse_gs_ceiling_log(GS))), 3)


def meter_line(ts, five, seven=10.0, identity="aaaa", reset="2026-09-15T23:00:00+00:00"):
    return json.dumps({"ts": ts, "account": "dave", "identity": identity,
                       "five_hour": {"utilization": five, "resets_at": reset},
                       "seven_day": {"utilization": seven, "resets_at": "2026-09-17T23:00:00+00:00"}})


class MeterLogTests(unittest.TestCase):
    def test_reads_resets_and_skips_failed_reads(self):
        lines = [meter_line("2026-09-15T21:00:00+00:00", 3.0),
                 json.dumps({"ts": "2026-09-15T21:05:00+00:00", "account": "dave", "identity": "aaaa",
                             "error": "HTTPError: HTTP Error 429: Too Many Requests"}),
                 meter_line("2026-09-15T21:10:00+00:00", 4.0)]
        s = parse_meter_log(lines)
        self.assertEqual([x.five_hour for x in s], [3.0, 4.0])
        self.assertEqual(s[0].resets_at, "2026-09-15T23:00:00+00:00")
        self.assertEqual(s[0].source, "meter")

    def test_a_log_holding_two_accounts_is_refused_not_averaged(self):
        lines = [meter_line("2026-09-15T21:00:00+00:00", 3.0, identity="aaaa"),
                 meter_line("2026-09-15T21:05:00+00:00", 40.0, identity="bbbb")]
        with self.assertRaises(MixedAccountLog):
            parse_meter_log(lines)


class ParseLogTests(unittest.TestCase):
    """One dispatcher, so an account can name several logs of different formats (issue #52)."""

    def test_each_format_name_reaches_its_own_parser(self):
        cases = {
            "moonlighter": ([meter_line("2026-09-15T21:00:00+00:00", 3.0)], "moonlighter"),
            "meter": ([meter_line("2026-09-15T21:00:00+00:00", 3.0)], "meter"),
            "ceiling": (["2026-09-15T21:00:00+00:00 5-hour 3% / 7-day 10%"], "ceiling"),
            "gs-ceiling": (["2026-09-15T21:00:00+00:00 ok five_hour=3% seven_day=10%"], "gs-ceiling"),
        }
        for fmt, (lines, source) in cases.items():
            with self.subTest(fmt=fmt):
                s = parse_log(fmt, lines)
                self.assertEqual([x.five_hour for x in s], [3.0])
                self.assertEqual(s[0].source, source)

    def test_since_drops_earlier_readings_in_every_format(self):
        cut = datetime(2026, 9, 15, 21, 5, tzinfo=timezone.utc)
        for fmt, lines in (
            ("moonlighter", [meter_line("2026-09-15T21:00:00+00:00", 3.0),
                             meter_line("2026-09-15T21:10:00+00:00", 4.0)]),
            ("ceiling", ["2026-09-15T21:00:00+00:00 5-hour 3% / 7-day 10%",
                         "2026-09-15T21:10:00+00:00 5-hour 4% / 7-day 10%"]),
            ("gs-ceiling", ["2026-09-15T21:00:00+00:00 ok five_hour=3% seven_day=10%",
                            "2026-09-15T21:10:00+00:00 ok five_hour=4% seven_day=10%"]),
        ):
            with self.subTest(fmt=fmt):
                self.assertEqual([x.five_hour for x in parse_log(fmt, lines, cut)], [4.0])

    def test_an_unknown_format_names_itself_and_the_known_ones(self):
        with self.assertRaises(ValueError) as cm:
            parse_log("csv", [])
        self.assertIn("csv", str(cm.exception))
        self.assertIn("moonlighter", str(cm.exception))


def rl(ts, five, resets_at=None):
    """A bare reset-less sample: only what `infer_resets` looks at."""
    return Sample(datetime.fromisoformat(ts), five, None, resets_at, "gs-ceiling")


class InferResetsTests(unittest.TestCase):
    def test_a_used_window_gets_the_projected_reset_five_hours_after_its_start(self):
        samples = [rl("2026-09-16T21:25:00+00:00", 34.0),
                   rl("2026-09-16T21:31:00+00:00", 0.0),     # drop: reset in the gap
                   rl("2026-09-16T22:31:00+00:00", 1.0),     # window shows real use
                   rl("2026-09-16T23:00:00+00:00", 2.0)]
        out = infer_resets(samples)
        self.assertIsNone(out[0].resets_at)  # nothing anchors the window before the first drop
        want = "2026-09-17T02:31:00+00:00"
        self.assertEqual(out[1].resets_at, want)
        self.assertEqual(out[2].resets_at, want)
        self.assertEqual(out[3].resets_at, want)

    def test_a_window_that_never_shows_use_is_left_unresolved(self):
        samples = [rl("2026-09-16T21:25:00+00:00", 34.0),
                   rl("2026-09-16T21:31:00+00:00", 0.0),
                   rl("2026-09-16T22:31:00+00:00", 0.0),
                   rl("2026-09-17T02:00:00+00:00", 0.0)]  # still zero right up to the horizon
        out = infer_resets(samples)
        self.assertTrue(all(s.resets_at is None for s in out))

    def test_a_drop_across_a_gap_over_five_hours_is_left_unresolved(self):
        samples = [rl("2026-09-16T10:00:00+00:00", 40.0),
                   rl("2026-09-16T16:00:01+00:00", 1.0),  # gap just over five hours
                   rl("2026-09-16T17:00:00+00:00", 5.0)]
        out = infer_resets(samples)
        self.assertTrue(all(s.resets_at is None for s in out))

    def test_a_recorded_reset_is_never_overwritten(self):
        samples = [rl("2026-09-16T21:25:00+00:00", 34.0),
                   rl("2026-09-16T21:31:00+00:00", 0.0, resets_at="2026-09-17T02:29:59+00:00"),
                   rl("2026-09-16T22:31:00+00:00", 1.0)]
        out = infer_resets(samples)
        self.assertEqual(out[1].resets_at, "2026-09-17T02:29:59+00:00")  # untouched

    def test_a_second_drop_anchors_the_next_window_independently(self):
        samples = [rl("2026-09-16T21:25:00+00:00", 34.0),
                   rl("2026-09-16T21:31:00+00:00", 0.0),
                   rl("2026-09-16T22:31:00+00:00", 1.0),
                   rl("2026-09-17T02:29:00+00:00", 15.0),
                   rl("2026-09-17T02:33:00+00:00", 0.0),   # second reset
                   rl("2026-09-17T02:40:00+00:00", 1.0)]
        out = infer_resets(samples)
        self.assertEqual(out[2].resets_at, "2026-09-17T02:31:00+00:00")
        self.assertEqual(out[4].resets_at, "2026-09-17T07:33:00+00:00")
        self.assertEqual(out[5].resets_at, "2026-09-17T07:33:00+00:00")

    def test_a_plateau_pinned_at_the_same_value_widens_the_gap_it_is_measured_from(self):
        # Real data: a meter pinned at 100% for a run before the visible drop can already
        # be on the far side of the true reset; the five-hour bound has to cover that whole
        # flat run, not just the last hop into the drop.
        samples = [rl("2026-09-16T10:00:00+00:00", 100.0),
                   rl("2026-09-16T12:00:00+00:00", 100.0),
                   rl("2026-09-16T14:00:00+00:00", 100.0),
                   rl("2026-09-16T18:00:00+00:00", 100.0),   # plateau now spans > 5h from 10:00
                   rl("2026-09-16T18:30:00+00:00", 5.0),     # drop
                   rl("2026-09-16T19:00:00+00:00", 6.0)]
        out = infer_resets(samples)
        self.assertTrue(all(s.resets_at is None for s in out))

    def test_returns_a_new_list_and_does_not_mutate_the_input(self):
        samples = [rl("2026-09-16T21:25:00+00:00", 34.0),
                   rl("2026-09-16T21:31:00+00:00", 0.0),
                   rl("2026-09-16T22:31:00+00:00", 1.0)]
        infer_resets(samples)
        self.assertTrue(all(s.resets_at is None for s in samples))
