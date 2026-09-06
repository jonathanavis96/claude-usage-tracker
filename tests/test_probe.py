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
                           ticks=1)
        self.assertEqual(r.prompts, 7)
        self.assertEqual((r.tick_from, r.tick_to), (11, 12))
        self.assertEqual(r.tokens_per_pct, 80_000)  # 4 prompts between ticks
        self.assertEqual(r.tokens["cache_read"], 4 * 19_500)

    def test_three_tick_span_sums_and_divides_by_ticks(self):
        # tick1 at 11, then three separate 1% jumps (12, 13, 14): span = 3.
        read = util_seq([10, 11, 12, 13, 14])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=3)
        self.assertEqual(r.prompts, 4)
        self.assertEqual((r.tick_from, r.tick_to), (11, 14))
        self.assertEqual(r.tokens_per_pct, 20_000)
        self.assertEqual(r.tokens["cache_read"], 3 * 19_500)

    def test_readings_recorded_once_per_prompt(self):
        read = util_seq([10, 10, 10, 11, 11, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0,
                           ticks=1)
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
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0)

    def test_too_many_prompts_aborts(self):
        read = util_seq([10] * 100)
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0, max_prompts=5)

    def test_tick_faster_than_prompts_explain_aborts(self):
        # a jump of 3% after one 20k prompt cannot be ours
        read = util_seq([10, 10, 11, 14])
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0)

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
                           ticks=1)
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
                           sleep=lambda s: None, now=now, deadline=T0 + timedelta(hours=1))
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
                           now=lambda: T0, usd_per_token=self.PRICE, ticks=1)
        self.assertIn("too early", str(e.exception))

    def test_tick_we_paid_for_is_accepted_and_records_seven_day(self):
        read = util_seq([10, 10, 10, 11, 11, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(tokens=1_000_000),
                           sleep=lambda s: None, now=lambda: T0, usd_per_token=self.PRICE, ticks=1)
        self.assertEqual((r.tick_from, r.tick_to), (11, 12))
        self.assertEqual((r.seven_day_before, r.seven_day_after), (30.0, 30.0))


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
                                    "--account", f"dave={d}", "--prices", str(prices)])
        finally:
            probe_mod.choose_account = orig_choose
            probe_mod.run_tick_probe = orig_run_tick
            cli_run_mod.run_prompt = orig_run_prompt
            usage_api_mod.read_usage = orig_read_usage

        self.assertEqual(rc, 4)  # ProbeAbort path
        self.assertEqual(captured["payload_kwarg"], "output")
        self.assertIn("4,000 words", captured["prompt"])
