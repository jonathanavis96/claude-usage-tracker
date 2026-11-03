import unittest
from datetime import datetime, timezone
from itertools import chain, repeat

from tracker.cli_run import RunUsage
from tracker.probe import ProbeAbort, Skip, run_tick_probe, skip_reason
from tracker.usage_api import Utilization

T0 = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)


def reads(*readings):
    it = iter(readings)
    return lambda: next(it)


class SkipReasonNoReadingTests(unittest.TestCase):
    """The endpoint reports `five_hour.utilization: null` while no window is open (real
    moonlighter rows, 2026-06-13). An idle account read that way, then used during the
    idle window, must be passed over as busy, not crash choose_account."""

    def test_meter_moving_from_no_reading_to_a_reading_is_busy_not_a_crash(self):
        read = reads(Utilization(T0, None, 30.0, None), Utilization(T0, 1.0, 30.0, "2026-11-03T08:00:00+00:00"))
        skip = skip_reason(read, lambda s: None)
        self.assertEqual(skip, Skip("busy", "meter moved no reading -> 1%"))

    def test_no_reading_then_zero_is_busy_too(self):
        # A window opened on first use reads 0% before its first tick.
        read = reads(Utilization(T0, None, 30.0, None), Utilization(T0, 0.0, 30.0, "2026-11-03T08:00:00+00:00"))
        self.assertEqual(skip_reason(read, lambda s: None), Skip("busy", "meter moved no reading -> 0%"))

    def test_a_flat_reading_is_still_formatted_as_before(self):
        read = reads(Utilization(T0, 7.0, 30.0, "r1"), Utilization(T0, 8.0, 30.0, "r1"))
        self.assertEqual(skip_reason(read, lambda s: None), Skip("busy", "meter moved 7% -> 8%"))


class WeeklyOnlyAccountTests(unittest.TestCase):
    """ADR 0001 rule 24: accounts are losing the five-hour limit (`"five_hour": null`,
    weekly_only). A tick probe there can never see a tick, so it must refuse before it
    sends a prompt rather than spend max_prompts of the owner's week finding that out."""

    def test_a_weekly_only_account_is_refused_before_any_prompt(self):
        sent = []

        def run(i):
            sent.append(i)
            return RunUsage("claude-sonnet-5", 2, 5, 11_473, 40_000, 0.001, 3.0)

        def read():
            return Utilization(T0, None, 40.0, None, "2026-11-07T00:00:00+00:00", weekly_only=True)

        with self.assertRaises(ProbeAbort) as cm:
            run_tick_probe("claude-sonnet-5", "low", "p", read, run, sleep=lambda s: None, now=lambda: T0,
                           max_prompts=80, expect_tokens_per_pct=600_000, payload_words=12_000)
        self.assertEqual(sent, [])
        self.assertIn("weekly-only", str(cm.exception))

    def test_an_account_that_turns_weekly_only_at_the_awaited_reset_is_refused_before_any_prompt(self):
        # The probe waits for a reset 10 minutes out; the window after it never opens.
        it = chain([Utilization(T0, 50.0, 40.0, "2026-11-03T03:10:00+00:00")],
                   repeat(Utilization(T0, None, 40.0, None, "2026-11-07T00:00:00+00:00", weekly_only=True)))
        sent = []

        def run(i):
            sent.append(i)
            return RunUsage("claude-sonnet-5", 2, 5, 11_473, 40_000, 0.001, 3.0)

        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", lambda: next(it), run, sleep=lambda s: None,
                           now=lambda: T0, expect_tokens_per_pct=600_000, payload_words=12_000)
        self.assertEqual(sent, [])

    def test_a_null_reading_with_no_window_open_still_probes(self):
        # Not weekly-only: the bucket is there with a null utilization until a window opens.
        vals = iter([None, 0.0, 1.0, 2.0])

        def read():
            v = next(vals)
            return Utilization(T0, v, 30.0, None if v is None else "2026-11-03T08:00:00+00:00")

        def run(i):
            return RunUsage("claude-sonnet-5", 100, 400, 19_500, 0, 0.001, 3.0)

        r = run_tick_probe("claude-sonnet-5", "low", "p", read, run, sleep=lambda s: None, now=lambda: T0,
                           ticks=1, skip=0)
        self.assertEqual((r.tick_from, r.tick_to), (1, 2))


if __name__ == "__main__":
    unittest.main()
