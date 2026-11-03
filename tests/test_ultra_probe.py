import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from itertools import chain, repeat

from tracker.cli_run import RunUsage
from tracker.probe import (
    ProbeAbort,
    ProbeResult,
    Skip,
    append_result,
    choose_account,
    run_tick_probe,
    skip_reason,
)
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


class ChooseAccountWeeklyOnlyTests(unittest.TestCase):
    """While the probe accounts lose the five-hour limit one at a time, an idle weekly-only
    account must be passed over for one that still has a five-hour meter, not picked
    first every slot (its flat null meter reads as idle) and aborted."""

    DAVE = Path("/h/.claude-dave")
    JWORK = Path("/h/.claude-javiswork")

    def test_a_weekly_only_account_falls_through_to_the_next(self):
        reads = {"jwork": lambda: Utilization(T0, None, 40.0, None, "2026-11-07T00:00:00+00:00", weekly_only=True),
                 "dave": lambda: Utilization(T0, 7.0, 40.0, "r1")}
        logged, skipped, slept = [], {}, []
        acc = choose_account([("jwork", self.JWORK), ("dave", self.DAVE)], lambda n: reads[n], slept.append,
                             max_wait_s=0, retry_s=900, processes=list, log=logged.append, skipped=skipped)
        self.assertEqual(acc, ("dave", self.DAVE))
        self.assertEqual(skipped, {"jwork": Skip("weekly-only", "no five-hour limit, so no tick to measure")})
        self.assertIn("jwork weekly-only: no five-hour limit, so no tick to measure", logged)
        self.assertEqual(slept, [120])  # dave's idle window only: the weekly-only skip spends none

    def test_when_every_account_is_weekly_only_it_gives_up_at_once(self):
        # Waiting cannot give an account its five-hour limit back, and bin/probe.sh holds the
        # tracker lock while it waits: four hours of retries would starve the daily publisher.
        read = lambda: Utilization(T0, None, 40.0, None, weekly_only=True)  # noqa: E731
        slept, skipped = [], {}
        acc = choose_account([("jwork", self.JWORK), ("dave", self.DAVE)], lambda n: read, slept.append,
                             max_wait_s=4 * 3600, retry_s=900, now=lambda: T0, processes=list,
                             log=lambda line: None, skipped=skipped)
        self.assertIsNone(acc)
        self.assertEqual(slept, [])
        self.assertEqual({s.kind for s in skipped.values()}, {"weekly-only"})

    def test_a_busy_account_beside_a_weekly_only_one_is_still_waited_for(self):
        reads = {"jwork": lambda: Utilization(T0, None, 40.0, None, weekly_only=True),
                 "dave": lambda: Utilization(T0, 7.0, 40.0, "r1")}
        slept = []
        acc = choose_account([("jwork", self.JWORK), ("dave", self.DAVE)], lambda n: reads[n], slept.append,
                             max_wait_s=900, retry_s=900, now=lambda: T0,
                             processes=lambda: [(491402, self.DAVE)], log=lambda line: None)
        self.assertIsNone(acc)
        self.assertEqual(slept, [900])

    def test_a_weekly_only_account_with_a_session_open_is_weekly_only_not_busy(self):
        # Code review round 5: the free pid check came first, so a weekly-only account with a
        # session open read as busy, and choose_account waited four hours for an account it
        # could never probe. Its meter is read too, so it is known for what it is.
        # Round 7: through a quick peek (no 429 retries), not the probe's retrying read.
        read = lambda: Utilization(T0, None, 40.0, None, weekly_only=True)  # noqa: E731
        slept, skipped, history = [], {}, []
        acc = choose_account([("jwork", self.JWORK), ("dave", self.DAVE)], lambda n: read, slept.append,
                             max_wait_s=4 * 3600, retry_s=900, now=lambda: T0,
                             processes=lambda: [(491402, self.DAVE)], log=lambda line: None,
                             skipped=skipped, history=history, peek_for=lambda n: read)
        self.assertIsNone(acc)
        self.assertEqual(slept, [])
        self.assertEqual({s.kind for s in skipped.values()}, {"weekly-only"})
        self.assertEqual([s.kind for s in history], ["weekly-only", "weekly-only"])

    def test_a_busy_account_is_peeked_at_most_once_and_never_with_the_retrying_read(self):
        # Code review round 7: the probe's read retries a 429 until the run's deadline, so a
        # busy account's extra read could spend the whole budget; and it cost a call a round.
        def retrying():
            raise AssertionError("the retrying read was used for a busy account")
        peeks = []

        def peek():
            peeks.append(1)
            return Utilization(T0, 7.0, 40.0, "r1")
        skipped = {}
        choose_account([("dave", self.DAVE)], lambda n: retrying, lambda s: None, max_wait_s=1800,
                       retry_s=900, now=lambda: T0, processes=lambda: [(491402, self.DAVE)],
                       log=lambda line: None, skipped=skipped, peek_for=lambda n: peek)
        self.assertEqual(skipped["dave"].kind, "busy")
        self.assertEqual(len(peeks), 1)  # three rounds, one peek

    def test_a_failed_peek_on_a_busy_account_leaves_it_busy_and_tries_the_next(self):
        # Code review rounds 6 and 7: the peek is best effort; whatever it raises (a 429, a
        # 401, a reply cut short) the account stays a plain busy skip and the next is tried.
        import http.client

        from tracker.probe import ProbeAbort

        def failing(exc):
            def read():
                raise exc
            return read
        for exc in (OSError("HTTP Error 429"), ProbeAbort("deadline"), ValueError("no utilization"),
                    http.client.IncompleteRead(b"")):
            reads = {"jwork": lambda: Utilization(T0, 7.0, 40.0, "r1"), "dave": failing(AssertionError("read"))}
            skipped = {}
            acc = choose_account([("dave", self.DAVE), ("jwork", self.JWORK)], lambda n: reads[n],
                                 lambda s: None, max_wait_s=0, retry_s=900, now=lambda: T0,
                                 processes=lambda: [(491402, self.DAVE)], log=lambda line: None,
                                 skipped=skipped, peek_for=lambda n, exc=exc: failing(exc))
            self.assertEqual(acc, ("jwork", self.JWORK), exc)
            self.assertEqual(skipped["dave"].kind, "busy")

    def test_the_peek_fetch_is_one_try_inside_the_runs_budget(self):
        # Code review round 9: the peek went straight to the endpoint, outside the deadline
        # that keeps the probe out of the next cron slot.
        from tracker.probe import ProbeAbort, deadline_peek_fetch
        calls = []

        def fetch(url, headers, sleep, max_retries):
            calls.append(max_retries)
            return {"ok": True}
        now = [T0]
        go = deadline_peek_fetch(lambda: now[0], T0 + timedelta(minutes=5), lambda s: None, fetch=fetch)
        self.assertEqual(go("u", {}), {"ok": True})
        self.assertEqual(calls, [0])
        now[0] = T0 + timedelta(minutes=5)
        with self.assertRaises(ProbeAbort):
            go("u", {})
        self.assertEqual(calls, [0])

    def test_a_weekly_only_account_is_read_once_per_wait(self):
        # Code review round 12: every retry round re-read a weekly-only account through the
        # 429-retrying fetch, though waiting cannot clear it.
        reads = []

        def weekly_only():
            reads.append(1)
            return Utilization(T0, None, 40.0, None, weekly_only=True)
        rd = {"jwork": weekly_only, "dave": lambda: Utilization(T0, 7.0, 40.0, "r1")}
        slept, skipped = [], {}
        acc = choose_account([("jwork", self.JWORK), ("dave", self.DAVE)], lambda n: rd[n], slept.append,
                             max_wait_s=1800, retry_s=900, now=lambda: T0,
                             processes=lambda: [(491402, self.DAVE)], log=lambda line: None, skipped=skipped)
        self.assertIsNone(acc)
        self.assertEqual(slept, [900, 900])  # three rounds, waiting for the busy one
        self.assertEqual(len(reads), 1)
        self.assertEqual(skipped["jwork"].kind, "weekly-only")

    def test_a_busy_account_with_a_five_hour_limit_is_still_busy(self):
        reads = {"jwork": lambda: Utilization(T0, 7.0, 40.0, "r1"), "dave": lambda: Utilization(T0, 7.0, 40.0, "r1")}
        skipped = {}
        choose_account([("dave", self.DAVE)], lambda n: reads[n], lambda s: None, max_wait_s=0, retry_s=900,
                       now=lambda: T0, processes=lambda: [(491402, self.DAVE)], log=lambda line: None,
                       skipped=skipped)
        self.assertEqual(skipped["dave"].kind, "busy")
        self.assertIn("491402", skipped["dave"].why)

    def test_busy_reason_alone_still_reads_a_flat_weekly_only_meter_as_idle(self):
        from tracker.probe import busy_reason
        read = lambda: Utilization(T0, None, 40.0, None, weekly_only=True)  # noqa: E731
        self.assertIsNone(busy_reason(read, lambda s: None))


class AppendAfterUnterminatedLineTests(unittest.TestCase):
    """A history whose last line has no newline (a hand edit, a torn write) must not have
    the new row glued onto it: `{...}{...}` is one unparseable line, and load_probes then
    refuses the whole file, which stops tracker.rotate and the publisher."""

    def result(self):
        return ProbeResult(T0, "claude-sonnet-5", "low", 40_000.0,
                           {"input": 2, "output": 5, "cache_read": 0, "cache_write": 0}, 9, 11, 14, 600.0)

    def test_the_new_row_starts_on_its_own_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "probes.jsonl")
            p.write_text('{"ts": "2026-10-01T00:00:00+00:00", "model": "claude-opus-5"}', encoding="utf-8")
            append_result(p, self.result(), account="dave")
            rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["model"] for r in rows], ["claude-opus-5", "claude-sonnet-5"])

    def test_a_terminated_or_missing_file_gets_no_blank_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "probes.jsonl")
            append_result(p, self.result(), account="dave")
            append_result(p, self.result(), account="jwork")
            text = p.read_text(encoding="utf-8")
        self.assertEqual(len(text.splitlines()), 2)
        self.assertNotIn("\n\n", text)


if __name__ == "__main__":
    unittest.main()
