import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker.pace import current_window, duration, main, measure, render, sparkline
from tracker.samples import parse_meter_log

UTC = timezone.utc
SAST = timezone(timedelta(hours=2))
RESET = datetime(2026, 11, 6, 4, 0, tzinfo=UTC)  # a Friday 06:00 SAST weekly reset
START = RESET - timedelta(days=7)


def line(ts, seven, seven_reset=RESET, five=None, five_reset=None, **extra):
    """One usage_log row in moonlighter's shape. `seven_reset` may carry sub-second jitter."""
    d = {"ts": ts if isinstance(ts, str) else ts.isoformat(),
         "seven_day": {"utilization": seven,
                       "resets_at": seven_reset if isinstance(seven_reset, str) or seven_reset is None
                       else seven_reset.isoformat()},
         "five_hour": {"utilization": five,
                       "resets_at": five_reset.isoformat() if five_reset else None}}
    d.update(extra)
    return json.dumps(d)


def samples(*lines):
    return parse_meter_log(list(lines), weekly=True)


def hourly(start_level, per_hour, hours, begin=START + timedelta(days=2)):
    """Readings every hour from `begin`, rising `per_hour` points an hour."""
    return [line(begin + timedelta(hours=h), start_level + per_hour * h) for h in range(hours + 1)]


class CurrentWindowTests(unittest.TestCase):
    def test_takes_the_newest_window_and_tolerates_reset_jitter(self):
        jitter = RESET + timedelta(microseconds=557429)
        s = samples(
            line(START - timedelta(hours=3), 88.0, seven_reset=START),          # last week's window
            line(START + timedelta(hours=5), 4.0, seven_reset=jitter),
            line(START + timedelta(hours=6), 5.0, seven_reset=RESET + timedelta(microseconds=448842)),
        )
        reset, window = current_window(s)
        self.assertEqual(reset, RESET + timedelta(microseconds=448842))
        self.assertEqual([w.seven_day for w in window], [4.0, 5.0])

    def test_none_without_a_usable_reading(self):
        self.assertIsNone(current_window([]))
        s = samples(line(START + timedelta(hours=1), 3.0, seven_reset=None),
                    line(START + timedelta(hours=2), None))
        self.assertIsNone(current_window(s))


class MeasureTests(unittest.TestCase):
    def test_slow_week_loses_points(self):
        # 20% at Sunday 04:00 UTC rising 0.25 points an hour for a day: 26% on Monday 04:00,
        # 96 h before the reset -> 26 + 0.25 * 96 = 50% at the reset, 50 points unused.
        p = measure(samples(*hourly(20.0, 0.25, 24)), now=START + timedelta(days=3, minutes=5))
        self.assertAlmostEqual(p.utilization, 26.0)
        self.assertAlmostEqual(p.rate, 0.25)
        left = 96 - 5 / 60  # every figure is from now, 5 min after the reading
        self.assertAlmostEqual(p.projected, 26.0 + 0.25 * left)
        self.assertAlmostEqual(p.unused, 74.0 - 0.25 * left)
        self.assertIsNone(p.full_at)
        self.assertAlmostEqual(p.needed_rate, 74.0 / left)
        self.assertEqual(p.verdict, "USE IT OR LOSE IT")
        text = render(p, tz=SAST)
        self.assertIn("USE IT OR LOSE IT: about 50% at the reset, so 50 points expire unused.", text)
        self.assertIn("To use it all  0.77%/h from here (3.1x your pace), about 19 points a day.", text)
        self.assertIn("resets Fri 6 Nov 06:00 (in 3d 23h)", text)

    def test_hot_week_runs_dry_before_the_reset(self):
        # 60% rising 1.5 points an hour: 96% after 24 h, 100% about 2h40m later, long before the reset.
        p = measure(samples(*hourly(60.0, 1.5, 24)), now=START + timedelta(days=3))
        self.assertEqual(p.verdict, "RUNNING HOT")
        self.assertEqual(p.projected, 100.0)
        reading = START + timedelta(days=3)
        self.assertEqual(p.full_at, reading + timedelta(hours=4 / 1.5))
        self.assertIn("RUNNING HOT: 100% on Mon 2 Nov 08:40, 3d 21h before the reset.", render(p, tz=SAST))

    def test_on_target(self):
        # 50% rising 0.5 an hour with 96 h left lands on 98%.
        p = measure(samples(*hourly(38.0, 0.5, 24)), now=START + timedelta(days=3))
        self.assertAlmostEqual(p.projected, 98.0)
        self.assertEqual(p.verdict, "ON TARGET")
        self.assertNotIn("To use it all", render(p, tz=SAST))

    def test_maxed(self):
        p = measure(samples(line(START + timedelta(days=5), 100.0)), now=START + timedelta(days=5))
        self.assertEqual(p.verdict, "MAXED")
        self.assertIn("MAXED: nothing left until the reset, in 2d 0h.", render(p, tz=SAST))

    def test_after_the_reset_with_no_new_reading(self):
        p = measure(samples(*hourly(40.0, 0.5, 4)), now=RESET + timedelta(minutes=30))
        self.assertTrue(p.window_over)
        self.assertEqual(p.verdict, "RESET")
        self.assertIn("RESET: the window reset 30m ago", render(p, tz=SAST))

    def test_replay_ignores_readings_after_now(self):
        lines = hourly(20.0, 1.0, 48)
        p = measure(samples(*lines), now=START + timedelta(days=2, hours=10))
        self.assertAlmostEqual(p.utilization, 30.0)
        self.assertEqual(p.reading_at, START + timedelta(days=2, hours=10))

    def test_one_reading_is_paced_from_the_window_start(self):
        # The meter read 0 at the window start: 12% one day in is 0.5 points an hour.
        p = measure(samples(line(START + timedelta(days=1), 12.0)), now=START + timedelta(days=1))
        self.assertAlmostEqual(p.rate, 0.5)
        self.assertEqual(p.rate_since, START)

    def test_pace_never_runs_backwards(self):
        # A meter that dips (a rounding wobble) gives a zero pace, not a negative one.
        p = measure(samples(line(START + timedelta(days=1), 12.0), line(START + timedelta(days=2), 11.0)),
                    now=START + timedelta(days=2), lookback_hours=24)
        self.assertEqual(p.rate, 0.0)
        self.assertAlmostEqual(p.projected, 11.0)

    def test_naive_and_aware_stamps_mix(self):
        naive = (START + timedelta(days=1)).replace(tzinfo=None).isoformat()
        aware = (START + timedelta(days=1, hours=1)).astimezone(SAST).isoformat()
        p = measure(samples(line(naive, 10.0), line(aware, 11.0)), now=START + timedelta(days=1, hours=1),
                    lookback_hours=1)
        self.assertAlmostEqual(p.rate, 1.0)

    def test_weekly_only_reading_counts_and_shows_no_five_hour_meter(self):
        p = measure(samples(line(START + timedelta(days=1), 12.0, five=None)), now=START + timedelta(days=1))
        self.assertIsNone(p.five_hour)
        self.assertIn("5-hour meter   none at the latest reading", render(p, tz=SAST))

    def test_five_hour_meter_is_shown_when_present(self):
        five_reset = START + timedelta(days=1, hours=3)
        p = measure(samples(line(START + timedelta(days=1), 12.0, five=37.0, five_reset=five_reset)),
                    now=START + timedelta(days=1))
        self.assertIn("5-hour meter   37%, resets Sat 31 Oct 09:00", render(p, tz=SAST))

    def test_a_stale_log_paces_from_now_and_says_how_old_the_reading_is(self):
        # Code review round 1: the last reading is Monday's 40% and it is now Thursday
        # afternoon, 12 h before the reset. "From here" must mean from now: 60 points in 12 h.
        p = measure(samples(line(START + timedelta(days=3), 40.0)), now=RESET - timedelta(hours=12))
        self.assertTrue(p.stale)
        self.assertAlmostEqual(p.needed_rate, 5.0)
        text = render(p, tz=SAST)
        self.assertIn("Latest reading 3d 12h old: the figures assume nothing has changed since.", text)
        self.assertIn("To use it all  5.00%/h from here", text)

    def test_a_stale_log_projects_from_now_not_from_the_reading(self):
        # Code review round 3: 60% then 80% a day later, last read 36 h before now. The pace
        # (20 points a day) from the reading reached 100% ten hours before now, while the
        # meter is assumed to still read 80%. Held from now, 80% reaches 100% a day after now.
        now = START + timedelta(days=4, hours=12)
        p = measure(samples(line(START + timedelta(days=2), 60.0), line(START + timedelta(days=3), 80.0)),
                    now=now)
        self.assertTrue(p.stale)
        self.assertEqual(p.verdict, "RUNNING HOT")
        self.assertEqual(p.full_at, now + timedelta(days=1))
        self.assertGreater(p.full_at, p.now)

    def test_a_stale_log_projects_the_rest_of_the_week_from_now(self):
        # 10% read on day 2 at +0.5%/h; 60 h later it is 12 h before the reset. Projected
        # from now: 10 + 0.5 * 12 = 16%, not 10 + 0.5 * 72 from the reading.
        p = measure(samples(*hourly(0.0, 0.5, 20, begin=START + timedelta(days=2))),
                    now=RESET - timedelta(hours=12), lookback_hours=20)
        self.assertAlmostEqual(p.rate, 0.5)
        self.assertAlmostEqual(p.projected, 16.0)

    def test_a_stale_log_draws_the_hours_already_gone_as_past(self):
        # Slices between the last reading and now have passed: they carry the level forward
        # and only slices after now are still to come.
        p = measure(samples(line(START + timedelta(days=1, hours=1), 20.0)),
                    now=START + timedelta(days=4, hours=1), slices=7)
        self.assertEqual(p.timeline, (0.0, 20.0, 20.0, 20.0, 20.0, None, None))

    def test_a_fresh_reading_is_not_stale(self):
        p = measure(samples(*hourly(20.0, 0.25, 24)), now=START + timedelta(days=3, minutes=5))
        self.assertFalse(p.stale)
        self.assertNotIn("Latest reading", render(p, tz=SAST))

    def test_lookback_must_be_positive(self):
        with self.assertRaises(ValueError):
            measure([], lookback_hours=0)

    def test_timeline_carries_forward_and_marks_the_future(self):
        p = measure(samples(line(START + timedelta(days=1, hours=1), 20.0),
                            line(START + timedelta(days=3, hours=1), 60.0)),
                    now=START + timedelta(days=3, hours=1), slices=7)
        # day 0 holds no reading (the start's 0%), day 1 reads 20, day 2 carries 20 forward,
        # day 3 reads 60, days 4-6 are still to come.
        self.assertEqual(p.timeline, (0.0, 20.0, 20.0, 60.0, None, None, None))
        self.assertEqual(sparkline(p.timeline), "▁▂▂▅···")


class FormatTests(unittest.TestCase):
    def test_sparkline_ends(self):
        self.assertEqual(sparkline([0.0, 50.0, 100.0, 140.0, None]), "▁▅██·")

    def test_duration(self):
        self.assertEqual(duration(timedelta(days=2, hours=23, minutes=59)), "2d 23h")
        self.assertEqual(duration(timedelta(hours=15, minutes=4)), "15h 04m")
        self.assertEqual(duration(timedelta(minutes=40, seconds=59)), "40m")
        self.assertEqual(duration(timedelta(seconds=-5)), "0m")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = Path(self.dir.name) / "usage_log.jsonl"

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = main(list(argv), out=out)
        return code, out.getvalue(), err.getvalue()

    def test_json_report(self):
        self.log.write_text("\n".join(hourly(20.0, 0.25, 24)) + "\nnot json\n")
        code, out, _ = self.run_main(str(self.log), "--now", (START + timedelta(days=3)).isoformat(), "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["verdict"], "USE IT OR LOSE IT")
        self.assertEqual(d["projected_at_reset"], 50.0)
        self.assertEqual(d["unused_at_reset"], 50.0)
        self.assertEqual(len(d["timeline"]), 28)

    def test_missing_log(self):
        code, _, err = self.run_main(str(self.log))
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_log_without_a_weekly_reading(self):
        self.log.write_text(line(START + timedelta(days=1), None) + "\n")
        code, _, err = self.run_main(str(self.log))
        self.assertEqual(code, 1)
        self.assertIn("no seven-day reading", err)

    def test_two_accounts_are_refused(self):
        self.log.write_text(line(START + timedelta(days=1), 10.0, identity="a") + "\n"
                            + line(START + timedelta(days=1, hours=1), 11.0, identity="b") + "\n")
        code, _, err = self.run_main(str(self.log))
        self.assertEqual(code, 2)
        self.assertIn("2 accounts", err)

    def test_bad_arguments(self):
        self.log.write_text(line(START + timedelta(days=1), 10.0) + "\n")
        self.assertEqual(self.run_main(str(self.log), "--now", "yesterday")[0], 2)
        self.assertEqual(self.run_main(str(self.log), "--lookback", "0")[0], 2)


    def test_each_reads_every_log_as_its_own_account(self):
        slow, hot, empty = (Path(self.dir.name) / n for n in ("meter-a.log", "meter-b.log", "meter-c.log"))
        slow.write_text("\n".join(hourly(20.0, 0.25, 24)) + "\n")
        hot.write_text("\n".join(hourly(60.0, 1.5, 24)) + "\n")
        empty.write_text("")
        now = (START + timedelta(days=3)).isoformat()
        code, out, _ = self.run_main(str(slow), str(hot), str(empty), str(self.log), "--each", "--now", now)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertRegex(lines[0], r"^meter-a\.log\s+26%  \S{28}  USE IT OR LOSE IT: about 50% at the reset, "
                                   r"50 points unused, needs 0\.77%/h$")
        self.assertRegex(lines[1], r"^meter-b\.log\s+96%  \S{28}  RUNNING HOT: 100% 3d 21h before the reset$")
        self.assertIn("no seven-day reading", lines[2])
        self.assertIn("cannot read", lines[3])

    def test_each_as_json_names_every_log(self):
        self.log.write_text("\n".join(hourly(20.0, 0.25, 24)) + "\n")
        missing = Path(self.dir.name) / "absent.log"
        code, out, _ = self.run_main(str(self.log), str(missing), "--each", "--json",
                                     "--now", (START + timedelta(days=3)).isoformat())
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([r["log"] for r in rows], [str(self.log), str(missing)])
        self.assertEqual(rows[0]["verdict"], "USE IT OR LOSE IT")
        self.assertIn("cannot read", rows[1]["error"])

    def test_each_with_nothing_measurable_fails(self):
        self.log.write_text("")
        self.assertEqual(self.run_main(str(self.log), "--each")[0], 1)

if __name__ == "__main__":
    unittest.main()
