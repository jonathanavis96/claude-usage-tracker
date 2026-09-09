import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tracker.usage_api import Utilization
from tracker.cli_run import RunUsage
from tracker.probe import run_tick_probe, is_idle, choose_account, append_result, ProbeAbort

T0 = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)

def util_seq(values, reset="r1"):
    it = iter(values)
    def read():
        v = next(it)
        return Utilization(T0, v, 30.0, reset if v is not None else None)
    return read

def runner(tokens=20_000):
    def run(_i=0):
        return RunUsage("claude-sonnet-5", 100, 400, tokens - 500, 0, 0.001, 3.0)
    return run

class PromptTests(unittest.TestCase):
    def test_probe_prompt_is_deterministic_and_unique_per_index(self):
        from tracker.probe import probe_prompt
        a, b, c = probe_prompt("s", 0, 50), probe_prompt("s", 0, 50), probe_prompt("s", 1, 50)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a.split()), len(c.split()))

    def test_probe_prompt_is_prose(self):
        from tracker.probe import probe_prompt, PROBE_PROMPT
        prompt = probe_prompt("s", 0, 100)
        body = prompt[len(PROBE_PROMPT):]
        lines = body.splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue(line.endswith("."), f"line does not end with a full stop: {line!r}")
        self.assertGreaterEqual(len(body.split()), 100)


class OutputPromptTests(unittest.TestCase):
    def test_output_prompt_is_deterministic_and_unique_per_index(self):
        from tracker.probe import output_prompt
        a, b, c = output_prompt("s", 0), output_prompt("s", 0), output_prompt("s", 1)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_output_prompt_is_under_200_words(self):
        from tracker.probe import output_prompt
        for i in range(5):
            self.assertLess(len(output_prompt("s", i).split()), 200)

    def test_output_prompt_mentions_the_4000_word_target(self):
        from tracker.probe import output_prompt
        self.assertIn("4,000 words", output_prompt("s", 0))


class JitterTests(unittest.TestCase):
    def test_is_idle_ignores_sub_minute_resets_at_jitter(self):
        from tracker.probe import is_idle
        from tracker.usage_api import Utilization
        from datetime import datetime, timezone
        rs = iter(["2026-09-06T02:29:59.965637+00:00", "2026-09-06T02:30:00.148993+00:00"])
        read = lambda: Utilization(datetime.now(timezone.utc), 0.0, 0.0, next(rs))  # noqa: E731
        self.assertTrue(is_idle(read, lambda s: None))


class TickTests(unittest.TestCase):
    def test_two_ticks_give_tokens_per_pct(self):
        # readings after each prompt: 10,10,11(tick1),11,11,11,12(tick2)
        read = util_seq([10, 10, 10, 11, 11, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=1, skip=0)
        self.assertEqual(r.prompts, 7)
        self.assertEqual((r.tick_from, r.tick_to), (11, 12))
        self.assertEqual(r.tokens_per_pct, 80_000)  # 4 prompts between ticks
        self.assertEqual(r.tokens["cache_read"], 4 * 19_500)

    def test_three_tick_span_sums_and_divides_by_ticks(self):
        # tick1 at 11, then three separate 1% jumps (12, 13, 14): span = 3.
        read = util_seq([10, 11, 12, 13, 14])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=3, skip=0)
        self.assertEqual(r.prompts, 4)
        self.assertEqual((r.tick_from, r.tick_to), (11, 14))
        self.assertEqual(r.tokens_per_pct, 20_000)
        self.assertEqual(r.tokens["cache_read"], 3 * 19_500)

    def test_readings_recorded_once_per_prompt(self):
        read = util_seq([10, 10, 10, 11, 11, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=1, skip=0)
        self.assertEqual(len(r.readings), r.prompts)
        self.assertEqual(r.readings[0]["tokens"], {"input": 100, "output": 400, "cache_read": 19_500, "cache_write": 0})
        self.assertEqual(r.readings[-1]["five_hour"], 12)

    def test_reset_mid_probe_aborts(self):
        vals = [90, 90, 91, 91, 2]
        it = iter(vals)
        def read():
            v = next(it)
            return Utilization(T0, v, 30.0, "r1" if v > 5 else "r2")
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0, skip=0)

    def test_too_many_prompts_aborts(self):
        read = util_seq([10] * 100)
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0, max_prompts=5, skip=0)

    def test_tick_faster_than_prompts_explain_aborts(self):
        # a jump of 3% after one 20k prompt cannot be ours
        read = util_seq([10, 10, 11, 14])
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0, skip=0)

class IdleTests(unittest.TestCase):
    def test_idle_when_two_readings_match(self):
        slept = []
        self.assertTrue(is_idle(util_seq([7, 7]), slept.append))
        self.assertEqual(slept, [120])

    def test_busy_when_moved(self):
        self.assertFalse(is_idle(util_seq([7, 8]), lambda s: None))

    def test_choose_account_falls_back_then_waits(self):
        reads = {"dave": util_seq([7, 8, 8, 8]), "jono": util_seq([3, 4, 4, 4])}
        slept = []
        acc = choose_account([("dave", Path("/d")), ("jono", Path("/j"))], lambda name: reads[name], slept.append, max_wait_s=3600, retry_s=900)
        self.assertEqual(acc, ("dave", Path("/d")))
        self.assertIn(900, slept)

    def test_choose_account_gives_up(self):
        reads = {"dave": util_seq([1, 2] * 50)}
        acc = choose_account([("dave", Path("/d"))], lambda n: reads[n], lambda s: None, max_wait_s=1800, retry_s=900)
        self.assertIsNone(acc)

class AppendTests(unittest.TestCase):
    def test_append_writes_jsonl(self):
        read = util_seq([10, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=1, skip=0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "probes.jsonl")
            append_result(p, r, account="dave")
            row = json.loads(p.read_text().splitlines()[0])
        self.assertEqual(row["account"], "dave")
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertEqual(row["tokens_per_pct"], 40_000)
        self.assertEqual(row["ts"], "2026-09-06T08:00:00+00:00")


class DeadlineTests(unittest.TestCase):
    def test_probe_aborts_once_the_deadline_passes(self):
        from datetime import timedelta
        clock = [T0]
        def now():
            clock[0] += timedelta(minutes=30)
            return clock[0]
        with self.assertRaises(ProbeAbort) as e:
            run_tick_probe("claude-sonnet-5", "low", "p", util_seq([10] * 20), runner(),
                           sleep=lambda s: None, now=now, deadline=T0 + timedelta(hours=1), skip=0)
        self.assertIn("deadline", str(e.exception))

    def test_choose_account_returns_none_past_the_deadline(self):
        from datetime import timedelta
        acc = choose_account([("dave", Path("/d"))], lambda n: util_seq([1, 1]), lambda s: None,
                             max_wait_s=3600, retry_s=900, now=lambda: T0 + timedelta(hours=2),
                             deadline=T0 + timedelta(hours=1))
        self.assertIsNone(acc)


class EarlyTickTests(unittest.TestCase):
    PRICE = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}

    def test_tick_our_prompts_cannot_pay_for_aborts(self):
        read = util_seq([10, 10, 10, 11, 11, 11, 11, 12])
        with self.assertRaises(ProbeAbort) as e:
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None,
                           now=lambda: T0, usd_per_token=self.PRICE, ticks=1, skip=0)
        self.assertIn("too early", str(e.exception))

    def test_tick_we_paid_for_is_accepted_and_records_seven_day(self):
        read = util_seq([10, 10, 10, 11, 11, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(tokens=1_000_000),
                           sleep=lambda s: None, now=lambda: T0, usd_per_token=self.PRICE, ticks=1, skip=0)
        self.assertEqual((r.tick_from, r.tick_to), (11, 12))
        self.assertEqual((r.seven_day_before, r.seven_day_after), (30.0, 30.0))
        self.assertEqual((r.five_hour_before, r.five_hour_after), (10, 12))


class PayloadCliTests(unittest.TestCase):
    def test_payload_output_reaches_prompt_builder(self):
        """--payload output must make main()'s run() call output_prompt, not probe_prompt.

        main() has real network/subprocess dependencies (choose_account, read_usage,
        run_prompt), so those are faked out here (the existing fakes pattern used across
        this suite); run_tick_probe itself is faked too, and used only to capture what the
        `run` callable it was handed actually produces.
        """
        import json
        import tempfile
        import tracker.probe as probe_mod
        import tracker.cli_run as cli_run_mod
        import tracker.usage_api as usage_api_mod
        from tracker.usage_api import Utilization

        captured = {}

        def fake_choose_account(accounts, read_for, sleep, max_wait_s=None, retry_s=None,
                                now=None, deadline=None):
            return accounts[0]

        def fake_read_usage(cfg, fetch=None):
            return Utilization(T0, 10.0, 30.0, "r1")

        def fake_run_prompt(prompt_text, model, effort, cfg):
            captured["prompt"] = prompt_text
            return RunUsage(model, 10, 10, 10, 0, 0.001, 3.0)

        def fake_run_tick_probe(model, effort, prompt, read, run, sleep, now, **kwargs):
            captured["payload_kwarg"] = kwargs.get("payload")
            run(0)
            raise ProbeAbort("stop-test")

        orig_choose = probe_mod.choose_account
        orig_run_tick = probe_mod.run_tick_probe
        orig_run_prompt = cli_run_mod.run_prompt
        orig_read_usage = usage_api_mod.read_usage
        probe_mod.choose_account = fake_choose_account
        probe_mod.run_tick_probe = fake_run_tick_probe
        cli_run_mod.run_prompt = fake_run_prompt
        usage_api_mod.read_usage = fake_read_usage
        try:
            with tempfile.TemporaryDirectory() as d:
                prices = Path(d, "prices.json")
                prices.write_text(json.dumps(
                    {"claude-fable": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}}))
                rc = probe_mod.main(["--model", "claude-fable", "--payload", "output",
                                    "--expect-tokens-per-pct", "31724",
                                    "--account", f"dave={d}", "--prices", str(prices)])
        finally:
            probe_mod.choose_account = orig_choose
            probe_mod.run_tick_probe = orig_run_tick
            cli_run_mod.run_prompt = orig_run_prompt
            usage_api_mod.read_usage = orig_read_usage

        self.assertEqual(rc, 4)  # ProbeAbort path
        self.assertEqual(captured["payload_kwarg"], "output")
        self.assertIn("4,000 words", captured["prompt"])


class SkipTests(unittest.TestCase):
    def test_skip_discards_the_first_span_and_measures_from_the_next_tick(self):
        # before=10; p1->11 (tick1); p2->11; p3->12 (skipped span done, measuring starts);
        # p4->12; p5->12; p6->13 (one measured tick).
        read = util_seq([10, 11, 11, 12, 12, 12, 13])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=1, skip=1)
        self.assertEqual(r.prompts, 6)
        self.assertEqual((r.tick_from, r.tick_to), (12, 13))
        self.assertEqual(r.tokens["cache_read"], 3 * 19_500)  # p4, p5, p6 only
        self.assertEqual(r.tokens_per_pct, 60_000)
        self.assertEqual(r.skip, 1)
        self.assertFalse(r.early_tick)

    def test_skip_two_then_three_measured_ticks(self):
        read = util_seq([10, 11, 12, 13, 13, 14, 15, 16])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=3, skip=2)
        self.assertEqual((r.tick_from, r.tick_to), (13, 16))
        self.assertEqual(r.prompts, 7)
        self.assertEqual(r.tokens["cache_read"], 4 * 19_500)  # prompts 4..7
        self.assertEqual(r.tokens_per_pct, 4 * 20_000 / 3)

    def test_skip_zero_is_the_old_behaviour(self):
        read = util_seq([10, 10, 10, 11, 11, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=1, skip=0)
        self.assertEqual((r.tick_from, r.tick_to, r.tokens_per_pct), (11, 12, 80_000))

    def test_skip_still_respects_max_prompts(self):
        read = util_seq([10, 11] + [11] * 20)
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=1, skip=1, max_prompts=6)

    def test_skip_row_has_skip_and_tick_from(self):
        read = util_seq([10, 11, 12, 13])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=1, skip=1)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "probes.jsonl")
            append_result(p, r, account="dave")
            row = json.loads(p.read_text().splitlines()[0])
        self.assertEqual((row["skip"], row["tick_from"], row["tick_to"]), (1, 12, 13))


def concurrent_runner(tokens=20_000, parallel_indexes=()):
    """A run() that records every index and, for the indexes in `parallel_indexes`, proves
    they are all in flight at once by making each wait at a barrier for the others."""
    import threading
    import time
    seen, in_flight, peak = [], [0], [0]
    lock = threading.Lock()
    barrier = threading.Barrier(len(parallel_indexes), timeout=5) if parallel_indexes else None
    def run(i):
        with lock:
            seen.append(i)
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        if barrier is not None and i in parallel_indexes:
            barrier.wait()  # raises BrokenBarrierError unless every listed index arrives
        else:
            time.sleep(0.01)
        with lock:
            in_flight[0] -= 1
        return RunUsage("claude-sonnet-5", 100, 400, tokens - 500, 0, 0.001, 3.0)
    run.seen, run.peak = seen, peak
    return run




# Standard burst setup: expect 100k per 1% at 20k per prompt is a 5-prompt span, so the
# opening burst is 80% of 5 = 4 prompts, and 50% of 5 = 2 after an early tick. The
# pre-run estimate (5,000 words at 3.47 tokens per word = 17,350) sizes the very first
# burst to floor(0.8 * 100k / 17,350) = 4 as well.
EXPECT = 100_000
WORDS = 2_400  # 2,400 x 3.47 + FIXED_PROMPT_TOKENS = 19,808 per prompt: first burst floor(0.8 * 100k / 19,808) = 4


def burst_probe(read, run, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("now", lambda: T0)
    kw.setdefault("expect_tokens_per_pct", EXPECT)
    kw.setdefault("payload_words", WORDS)
    return run_tick_probe("claude-sonnet-5", "low", "p", read, run, **kw)


def bursts(r):
    return [x.get("burst") for x in r.readings]


class PromptSizeTests(unittest.TestCase):
    def test_one_prompt_is_a_twelfth_of_a_tick_after_fixed_overhead(self):
        # 527,647 is roughly the last published Sonnet rate: large enough that the
        # target survives subtracting FIXED_PROMPT_TOKENS without hitting MIN_PAYLOAD_WORDS,
        # so this checks the un-clamped formula end to end.
        from tracker.probe import payload_words_for, TOKENS_PER_WORD, PROMPTS_PER_TICK, FIXED_PROMPT_TOKENS
        expect = 527_647
        words = payload_words_for(expect)
        self.assertEqual(words, round((expect / PROMPTS_PER_TICK - FIXED_PROMPT_TOKENS) / TOKENS_PER_WORD))
        self.assertEqual(words, 9_363)
        total_tokens = words * TOKENS_PER_WORD + FIXED_PROMPT_TOKENS
        self.assertAlmostEqual(total_tokens / expect, 1 / PROMPTS_PER_TICK, places=3)

    def test_prompt_size_is_clamped_to_the_word_range(self):
        from tracker.probe import payload_words_for
        self.assertEqual(payload_words_for(20_000_000), 12_000)
        self.assertEqual(payload_words_for(20_000), 500)

    def test_low_expectation_keeps_at_least_eight_prompts_per_span(self):
        # 166,705 is the Fable rate that produced the early-tick bug on 2026-09-09. After
        # the fixed overhead the payload is small (about 695 words) but not clamped, so a
        # span is 12 prompts at expectation and still >= 8 when the true rate is 30% lower.
        from tracker.probe import payload_words_for, TOKENS_PER_WORD, FIXED_PROMPT_TOKENS, MIN_PAYLOAD_WORDS
        words = payload_words_for(166_705)
        self.assertGreater(words, MIN_PAYLOAD_WORDS)
        per_prompt = words * TOKENS_PER_WORD + FIXED_PROMPT_TOKENS
        self.assertGreaterEqual(int(0.7 * 166_705 // per_prompt), 8)
        # At the 113k actually measured that day the clamp applies and the span is still >= 8.
        words = payload_words_for(113_303)
        self.assertEqual(words, MIN_PAYLOAD_WORDS)
        self.assertGreaterEqual(int(113_303 // (words * TOKENS_PER_WORD + FIXED_PROMPT_TOKENS)), 8)

    def test_output_prompt_takes_a_reply_size(self):
        from tracker.probe import output_prompt, OUTPUT_REPLY_WORDS
        self.assertEqual(OUTPUT_REPLY_WORDS, 4_000)
        self.assertIn("2,500 words", output_prompt("s", 0, 2_500))


class BurstFirstSpanTests(unittest.TestCase):
    def test_alignment_and_each_span_open_with_an_80_percent_burst_then_singles(self):
        # before=10; alignment: burst4->10, 10, 11 (tick1); measured: burst4->11, 11, 12.
        run = concurrent_runner(parallel_indexes={0, 1, 2, 3})
        r = burst_probe(util_seq([10, 10, 10, 11, 11, 11, 12]), run, ticks=1, skip=0)
        self.assertEqual(bursts(r), [4, None, None, 4, None, None])
        self.assertEqual(run.peak[0], 4)
        self.assertEqual(sorted(run.seen), list(range(12)))
        self.assertEqual(r.prompts, 12)
        self.assertEqual((r.tick_from, r.tick_to), (11, 12))
        self.assertEqual(r.tokens["cache_read"], 6 * 19_500)
        self.assertEqual(r.tokens_per_pct, 120_000)
        self.assertFalse(r.early_tick)

    def test_skip_span_opens_with_the_same_burst(self):
        # before=10; burst4->10, 11 (tick1); skip span: burst4->11, 12 (tick_from);
        # measured: burst4->12, 13.
        r = burst_probe(util_seq([10, 10, 11, 11, 12, 12, 13]), concurrent_runner(), ticks=1, skip=1)
        self.assertEqual(bursts(r), [4, None, 4, None, 4, None])
        self.assertEqual((r.tick_from, r.tick_to), (12, 13))
        self.assertEqual(r.tokens["cache_read"], 5 * 19_500)
        self.assertEqual(r.tokens_per_pct, 100_000)

    def test_burst_size_uses_the_observed_tokens_per_prompt_once_seen(self):
        # 40k per prompt: a 2.5-prompt span, so bursts after the first are floor(0.8*2.5)=2;
        # the first burst is sized from the word estimate plus overhead (19,808): floor(0.8*100k/19,808)=4.
        r = burst_probe(util_seq([10, 10, 11, 11, 12]), concurrent_runner(tokens=40_000), ticks=1, skip=0)
        self.assertEqual(bursts(r), [4, None, 2, None])

    def test_no_expectation_means_no_bursts(self):
        run = concurrent_runner()
        r = burst_probe(util_seq([10, 11, 11, 12]), run, ticks=1, skip=0, expect_tokens_per_pct=None)
        self.assertEqual(run.peak[0], 1)
        self.assertTrue(all("burst" not in x for x in r.readings))

    def test_burst_of_one_is_a_single(self):
        # 60k per prompt at 100k per 1%: floor(0.8 * 1.67) = 1, so no burst reading.
        run = concurrent_runner(tokens=60_000)
        r = burst_probe(util_seq([10, 10, 11, 11, 12]), run, ticks=1, skip=0, payload_words=12_000)
        self.assertEqual(run.peak[0], 1)
        self.assertTrue(all("burst" not in x for x in r.readings))

    def test_burst_capped_by_max_prompts(self):
        run = concurrent_runner()
        with self.assertRaises(ProbeAbort):
            burst_probe(util_seq([10, 10]), run, ticks=1, skip=0, max_prompts=3)
        self.assertEqual((run.peak[0], sorted(run.seen)), (3, [0, 1, 2]))

    def test_jump_larger_than_the_burst_still_aborts(self):
        with self.assertRaises(ProbeAbort) as e:
            burst_probe(util_seq([10, 15]), concurrent_runner(), ticks=1, skip=0)
        self.assertIn("not idle", str(e.exception))

    def test_burst_run_failure_propagates(self):
        def run(i):
            if i == 2:
                raise RuntimeError("claude exited 1")
            return runner()(i)
        with self.assertRaises(RuntimeError):
            burst_probe(util_seq([10, 11]), run, ticks=1, skip=0)


class EarlyTickFlagTests(unittest.TestCase):
    def test_tick_during_a_measured_burst_flags_early_tick_and_halves_later_bursts(self):
        # before=10; burst4->10, 11 (tick1=tick_from); burst4->12 (early: 1 of 2 measured);
        # burst2->12, 12, 13 (done). Measured spend: 4 + 2 + 2 prompts over 2%.
        r = burst_probe(util_seq([10, 10, 11, 12, 12, 12, 13]), concurrent_runner(), ticks=2, skip=0)
        self.assertEqual(bursts(r), [4, None, 4, 2, None, None])
        self.assertTrue(r.early_tick)
        self.assertEqual((r.tick_from, r.tick_to), (11, 13))
        self.assertEqual(r.tokens_per_pct, 8 * 20_000 / 2)

    def test_tick_during_the_skip_span_burst_also_flags(self):
        # before=10; burst4->10, 11 (tick1); skip span: burst4->12 (early, tick_from);
        # measured: burst2->12, 13.
        r = burst_probe(util_seq([10, 10, 11, 12, 12, 13]), concurrent_runner(), ticks=1, skip=1)
        self.assertEqual(bursts(r), [4, None, 4, 2, None])
        self.assertTrue(r.early_tick)
        self.assertEqual((r.tick_from, r.tick_to), (12, 13))

    def test_tick_during_the_alignment_burst_is_not_early_when_a_span_is_skipped(self):
        # The alignment span is a partial, so a tick inside its burst says nothing about
        # the limit, and the span it corrupts is the one skip discards anyway.
        r = burst_probe(util_seq([10, 11, 11, 12, 12, 13]), concurrent_runner(), ticks=1, skip=1)
        self.assertEqual(bursts(r), [4, 4, None, 4, None])
        self.assertFalse(r.early_tick)

    def test_tick_during_the_alignment_burst_is_early_without_a_skip(self):
        # With nothing skipped, tick_from itself lands inside the burst, so the row is suspect.
        r = burst_probe(util_seq([10, 11, 11, 12]), concurrent_runner(), ticks=1, skip=0)
        self.assertEqual(bursts(r), [4, 2, None])
        self.assertTrue(r.early_tick)

    def test_final_burst_past_the_last_tick_is_early_and_in_the_division(self):
        # before=10; burst4->10, 11 (tick1); the measured burst of 4 carries the meter from
        # 11 to 13 in one reading: 2% for the 4 prompts.
        r = burst_probe(util_seq([10, 10, 11, 13]), concurrent_runner(), ticks=1, skip=0)
        self.assertTrue(r.early_tick)
        self.assertEqual((r.tick_from, r.tick_to), (11, 13))
        self.assertEqual(r.tokens_per_pct, 4 * 20_000 / 2)


def reset_seq(values, resets, now=T0):
    """Readings whose resets_at is an ISO stamp `resets[i]` minutes after `now` (None for absent)."""
    from datetime import timedelta
    it = iter(zip(values, resets))
    def read():
        v, m = next(it)
        stamp = (now + timedelta(minutes=m)).isoformat() if m is not None else None
        return Utilization(now, v, 30.0, stamp)
    return read


class ResetWaitTests(unittest.TestCase):
    def test_reset_within_20_minutes_is_waited_for_and_measurement_starts_at_zero(self):
        # before=40 with 10 min to the reset; after the wait the meter reads 0.0 with no
        # resets_at yet (as the endpoint does right after a reset). No alignment: tick1 is 0,
        # the skip span is 0->1 and the measured span 1->2.
        slept = []
        read = reset_seq([40, 0, 0, 1, 1, 2], [10, None, 300, 300, 300, 300])
        r = burst_probe(read, concurrent_runner(), ticks=1, skip=1, sleep=slept.append)
        self.assertEqual(slept[0], 10 * 60 + 30)
        self.assertTrue(r.reset_start)
        self.assertEqual(bursts(r), [4, None, 4, None])
        self.assertEqual((r.tick_from, r.tick_to), (1, 2))
        self.assertEqual(r.tokens_per_pct, 100_000)
        self.assertEqual(r.prompts, 10)

    def test_reset_start_with_no_skip_measures_from_the_first_prompt(self):
        read = reset_seq([40, 0, 0, 1], [5, 300, 300, 300])
        r = burst_probe(read, concurrent_runner(), ticks=1, skip=0)
        self.assertTrue(r.reset_start)
        self.assertEqual((r.tick_from, r.tick_to), (0, 1))
        self.assertEqual(r.tokens_per_pct, 5 * 20_000)

    def test_reset_further_than_20_minutes_away_is_not_waited_for(self):
        slept = []
        read = reset_seq([40, 40, 41, 41, 42], [25, 25, 25, 25, 25])
        r = burst_probe(read, concurrent_runner(), ticks=1, skip=0, sleep=slept.append)
        self.assertFalse(r.reset_start)
        self.assertEqual(slept, [60] * 4)
        self.assertEqual((r.tick_from, r.tick_to), (41, 42))

    def test_meter_not_at_zero_after_the_wait_falls_back_to_alignment(self):
        read = reset_seq([40, 40, 40, 41, 41, 42], [10, 300, 300, 300, 300, 300])
        r = burst_probe(read, concurrent_runner(), ticks=1, skip=0)
        self.assertFalse(r.reset_start)
        self.assertEqual((r.tick_from, r.tick_to), (41, 42))

    def test_wait_is_charged_against_the_deadline(self):
        from datetime import timedelta
        clock = [T0]
        def now():
            return clock[0]
        def sleep(s):
            clock[0] += timedelta(seconds=s)
        read = reset_seq([40, 0, 0, 1], [10, 300, 300, 300])
        with self.assertRaises(ProbeAbort) as e:
            burst_probe(read, concurrent_runner(), ticks=1, skip=0, sleep=sleep, now=now,
                        deadline=T0 + timedelta(minutes=5))
        self.assertIn("deadline", str(e.exception))

    def test_unparseable_resets_at_is_ignored(self):
        r = burst_probe(util_seq([10, 11, 12]), concurrent_runner(), ticks=1, skip=0)
        self.assertFalse(r.reset_start)


class RowShapeTests(unittest.TestCase):
    def test_row_records_the_new_fields_and_drops_the_old_flags(self):
        r = burst_probe(util_seq([10, 11, 11, 12]), concurrent_runner(), ticks=1, skip=0, settle_s=30)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "probes.jsonl")
            append_result(p, r, account="dave")
            row = json.loads(p.read_text().splitlines()[0])
        self.assertEqual(row["payload_words"], WORDS)
        self.assertEqual(row["expect_tokens_per_pct"], EXPECT)
        self.assertIs(row["early_tick"], True)
        self.assertIs(row["reset_start"], False)
        self.assertEqual((row["skip"], row["ticks"], row["settle_s"]), (0, 1, 30))
        self.assertNotIn("burst", row)
        self.assertNotIn("overshoot", row)
        self.assertEqual(row["readings"][0]["burst"], 4)

    def test_defaults_are_three_measured_ticks_and_skip_one(self):
        slept = []
        # before=10; 11 (tick1); 12 (skipped); 13, 14, 15 (three measured).
        r = run_tick_probe("claude-sonnet-5", "low", "p", util_seq([10, 11, 12, 13, 14, 15]), runner(),
                           sleep=slept.append, now=lambda: T0)
        self.assertEqual((r.ticks, r.skip, r.settle_s), (3, 1, 60))
        self.assertEqual((r.tick_from, r.tick_to), (12, 15))
        self.assertEqual(slept, [60] * 5)
        self.assertEqual(r.tokens_per_pct, 20_000)


class CliTests(unittest.TestCase):
    PRICES = {"claude-sonnet-5": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1},
              "claude-fable-5-1": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}}

    def _capture(self, argv):
        import tracker.probe as probe_mod
        import tracker.cli_run as cli_run_mod
        import tracker.usage_api as usage_api_mod
        captured = {}

        def fake_run_tick_probe(model, effort, prompt, read, run, sleep, now, **kwargs):
            captured.update(kwargs)
            captured["prompt"] = None
            captured["prompt"] = run(0) and captured["prompt_text"]
            raise ProbeAbort("stop-test")

        def fake_run_prompt(prompt_text, model, effort, cfg):
            captured["prompt_text"] = prompt_text
            return RunUsage(model, 1, 1, 1, 0, 0.0, 1.0)

        orig = (probe_mod.choose_account, probe_mod.run_tick_probe, cli_run_mod.run_prompt, usage_api_mod.read_usage)
        probe_mod.choose_account = lambda accounts, *a, **k: accounts[0]
        probe_mod.run_tick_probe = fake_run_tick_probe
        cli_run_mod.run_prompt = fake_run_prompt
        usage_api_mod.read_usage = lambda cfg, fetch=None: Utilization(T0, 10.0, 30.0, "r1")
        try:
            with tempfile.TemporaryDirectory() as d:
                prices = Path(d, "prices.json")
                prices.write_text(json.dumps(self.PRICES))
                rc = probe_mod.main(argv + ["--account", f"dave={d}", "--prices", str(prices)])
        finally:
            (probe_mod.choose_account, probe_mod.run_tick_probe, cli_run_mod.run_prompt, usage_api_mod.read_usage) = orig
        return rc, captured

    def test_defaults_are_three_ticks_skip_one_and_the_expectation_sizes_the_prompt(self):
        # 117,897 is a low enough expectation that the fixed per-prompt overhead leaves
        # less than MIN_PAYLOAD_WORDS (500) of budget per prompt, so sizing clamps to it.
        rc, c = self._capture(["--model", "claude-fable-5-1", "--expect-tokens-per-pct", "117897"])
        self.assertEqual(rc, 4)
        self.assertEqual((c["ticks"], c["skip"], c["settle_s"]), (3, 1, 60))
        self.assertEqual(c["expect_tokens_per_pct"], 117897.0)
        self.assertEqual(c["payload_words"], 500)
        self.assertGreaterEqual(len(c["prompt_text"].split()), 500)
        self.assertLess(len(c["prompt_text"].split()), 500 + 40)

    def test_flags_reach_run_tick_probe(self):
        rc, c = self._capture(["--model", "claude-sonnet-5", "--expect-tokens-per-pct", "700000",
                               "--ticks", "2", "--skip", "0", "--settle", "30"])
        self.assertEqual((c["ticks"], c["skip"], c["settle_s"], c["payload_words"]), (2, 0, 30.0, 12_000))

    def test_expectation_is_required(self):
        with self.assertRaises(SystemExit):
            self._capture(["--model", "claude-sonnet-5"])

    def test_output_payload_keeps_its_fixed_reply_size(self):
        rc, c = self._capture(["--model", "claude-fable-5-1", "--payload", "output",
                               "--expect-tokens-per-pct", "31724"])
        self.assertEqual(c["payload"], "output")
        self.assertEqual(c["payload_words"], 4_000)
        self.assertIn("4,000 words", c["prompt_text"])


class InitialBurstTests(unittest.TestCase):
    def test_first_burst_counts_the_fixed_overhead_per_prompt(self):
        # Before any prompt has run, a prompt is estimated as payload plus the fixed
        # overhead; at the Fable expectation of 166,705 the burst is 80% of a 12-prompt
        # span, about 9, where the payload-only estimate would have fired 55.
        from tracker.probe import _burst_size, payload_words_for, TOKENS_PER_WORD, FIXED_PROMPT_TOKENS
        words = payload_words_for(166_705)
        k = _burst_size(166_705, 0.8, [], words, room=100)
        expected = int(0.8 * 166_705 // (words * TOKENS_PER_WORD + FIXED_PROMPT_TOKENS))
        self.assertEqual(k, expected)
        self.assertLessEqual(k, 10)
