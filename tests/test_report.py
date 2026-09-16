"""tracker/report.py: the plain-language summary at the top of a probe failure alert."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tracker.report import model_label, report, summary

ROOT = Path(__file__).resolve().parents[1]

RESET_LOG = """12:03:11Z jwork busy: pid 1942517, 2355887, 2877669
12:09:40Z prompt 9 (burst of 9): five_hour=7.0 resets_at=2026-09-13T12:30:00.111468+00:00 spent=input=18 output=45 cache_read=0 cache_write=347414
12:15:02Z prompt 20 (burst of 11): five_hour=8.0 resets_at=2026-09-13T12:30:00.187083+00:00 spent=input=22 output=55 cache_read=106359 cache_write=321562
12:28:50Z prompt 42: five_hour=9.0 resets_at=2026-09-13T12:30:00.941624+00:00 spent=input=2 output=5 cache_read=9669 cache_write=28770
12:57:40Z prompt 43: five_hour=0.0 resets_at=2026-09-13T17:30:00.335043+00:00 spent=input=2 output=5 cache_read=9669 cache_write=28844
12:57:41Z probe aborted on dave: window reset during probe
"""

# The pre-timestamp format, as written before 2026-09-13, must still parse.
OLD_JUMP_LOG = """jwork busy: pid 1942517
prompt 9 (burst of 9): five_hour=7.0 resets_at=2026-09-13T12:30:00.111468+00:00 spent=input=18 output=45 cache_read=0 cache_write=347414
prompt 10: five_hour=12.0 resets_at=2026-09-13T12:30:00.187083+00:00 spent=input=2 output=5 cache_read=9669 cache_write=29345
probe aborted on dave: utilization jumped 5% after 1 prompt; account not idle
"""

SKIP_LOG = """jwork busy: pid 1942517, 2355887
dave busy: pid 1317553, 491402
jwork busy: pid 1942517, 2355887
dave busy: meter moved 7% -> 8%
probe skipped: no idle account within max wait
"""

# Issue #38: dave passed over for its weekly meter, jwork for a session mid-turn.
WEEKLY_SKIP_LOG = """00:00:01Z jwork busy: pid 1942517
00:00:02Z dave weekly meter 99%
00:00:02Z dave weekly: 99% of the seven-day limit used, above 90%
00:15:03Z jwork busy: pid 1942517
00:15:04Z dave weekly meter 99%
00:15:04Z dave weekly: 99% of the seven-day limit used, above 90%
probe skipped: no usable account within max wait (jwork busy: pid 1942517; dave weekly: 99% of the seven-day limit used, above 90%)
"""

CRASH_LOG = """jwork busy: pid 1942517, 2355887, 2877669
prompt 9 (burst of 9): five_hour=6.0 resets_at=2026-09-13T02:30:00.654543+00:00 spent=input=18 output=45 cache_read=0 cache_write=349385
prompt 37: five_hour=8.0 resets_at=2026-09-13T02:30:00.243439+00:00 spent=input=2 output=5 cache_read=9669 cache_write=29291
Traceback (most recent call last):
  File "/home/jonathan/claude-usage-tracker/tracker/probe.py", line 608, in <module>
    raise SystemExit(main())
  File "/usr/lib/python3.14/urllib/request.py", line 611, in http_error_default
    raise HTTPError(req.full_url, code, msg, hdrs, fp)
urllib.error.HTTPError: HTTP Error 401: Unauthorized
"""


class TestModelLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(model_label("claude-sonnet-5"), "Sonnet 5")
        self.assertEqual(model_label("claude-opus-5"), "Opus 5")
        self.assertEqual(model_label("claude-fable-5-1"), "Fable 5.1")
        self.assertEqual(model_label("weird"), "Weird")


class TestSummary(unittest.TestCase):
    def test_window_reset_says_failed_where_how_far_and_why_it_was_not_avoided(self):
        s = summary("claude-sonnet-5", 4, "Rotation run", RESET_LOG)
        self.assertTrue(s.startswith("Outcome: FAILED. Rotation run for Sonnet 5 stopped"))
        self.assertIn("No measurement was recorded", s)
        self.assertIn('ran on the "dave" account', s)
        self.assertIn("sent 43 prompts and moved the 5-hour meter from 7% to 9%", s)
        self.assertIn("was due at 12:30 UTC", s)
        self.assertIn("less than 20 minutes away", s)
        self.assertIn("nothing needs fixing by hand", s)
        self.assertNotIn("Traceback", s)

    def test_window_reset_reports_the_meter_value_the_new_window_started_on(self):
        log = RESET_LOG.replace("prompt 43: five_hour=0.0", "prompt 43: five_hour=1.0")
        s = summary("claude-sonnet-5", 4, "Rotation run", log)
        self.assertIn("meter dropped to 1%", s)
        self.assertIn("from 7% to 9%", s)
        s0 = summary("claude-sonnet-5", 4, "Rotation run", RESET_LOG)
        self.assertIn("meter dropped to 0%", s0)

    def test_old_log_format_without_timestamps_still_parses_and_a_jump_names_the_intruder(self):
        s = summary("claude-sonnet-5", 4, "Drift rerun", OLD_JUMP_LOG)
        self.assertIn("Drift rerun for Sonnet 5", s)
        self.assertIn("moved the 5-hour meter from 7% to 12%", s)
        self.assertIn("Someone else was using the account", s)
        self.assertIn("utilization jumped 5% after 1 prompt", s)

    def test_skipped_names_the_busy_accounts_and_their_last_reason(self):
        s = summary("claude-opus-5", 3, "Rotation run", SKIP_LOG)
        self.assertTrue(s.startswith("Outcome: SKIPPED. Rotation run for Opus 5 never started"))
        self.assertIn("dave was busy (meter moved 7% -> 8%)", s)
        self.assertIn("jwork was busy (pid 1942517, 2355887)", s)
        self.assertIn("nothing is broken", s)

    def test_skipped_tells_a_weekly_meter_apart_from_a_busy_account(self):
        s = summary("claude-opus-5", 3, "Rotation run", WEEKLY_SKIP_LOG)
        self.assertTrue(s.startswith("Outcome: SKIPPED. Rotation run for Opus 5 never started, because no "
                                     "account was both idle and under its weekly limit within the 4-hour wait."))
        self.assertIn("dave was too close to its weekly limit (99% of the seven-day limit used, above 90%)", s)
        self.assertIn("jwork was busy (pid 1942517)", s)
        self.assertIn("seven-day meter resets", s)
        self.assertNotIn("Who was busy", s)

    def test_crash_quotes_the_exception_and_explains_a_401(self):
        s = summary("claude-sonnet-5", 1, "Rotation run", CRASH_LOG)
        self.assertTrue(s.startswith("Outcome: CRASHED. Rotation run for Sonnet 5 exited with code 1"))
        self.assertIn("sent 37 prompts and moved the 5-hour meter from 6% to 8%", s)
        self.assertIn("Last error: urllib.error.HTTPError: HTTP Error 401: Unauthorized", s)
        self.assertIn(".credentials.json has probably expired", s)
        self.assertIn("this needs a look", s)

    def test_crash_reports_an_exception_class_without_an_error_suffix(self):
        log = CRASH_LOG.replace("urllib.error.HTTPError: HTTP Error 401: Unauthorized",
                                "tracker.probe.ProbeAbort: deadline")
        s = summary("claude-sonnet-5", 1, "Rotation run", log)
        self.assertIn("Last error: tracker.probe.ProbeAbort: deadline", s)
        self.assertNotIn("no Python error line", s)
        log = CRASH_LOG.replace("urllib.error.HTTPError: HTTP Error 401: Unauthorized",
                                "subprocess.TimeoutExpired: Command '['claude']' timed out after 600 seconds")
        s = summary("claude-sonnet-5", 1, "Rotation run", log)
        self.assertIn("Last error: subprocess.TimeoutExpired: Command", s)

    def test_refusal_before_any_account_is_explained(self):
        log = "expectation 40000 tokens per 1% fits fewer than 8 prompts per tick at the minimum payload; quantisation would exceed the published tolerance\n"
        s = summary("claude-opus-5", 4, "Rotation run", log)
        self.assertIn("refused to start", s)
        self.assertIn("fits fewer than 8 prompts", s)

    def test_unrecognised_log_falls_back_without_guessing(self):
        s = summary("claude-opus-5", 4, "Rotation run", "fake probe 1 rc 4\n")
        self.assertIn("does not show why the probe stopped", s)
        s = summary("claude-opus-5", 7, "Rotation run", "fake probe 1 rc 7\n")
        self.assertIn("CRASHED", s)
        self.assertIn("no Python error line", s)

    def test_output_payload_says_what_the_weight_does(self):
        s = summary("claude-fable-5-1", 3, "Weekly output probe", SKIP_LOG, payload="output")
        self.assertIn("Weekly output probe for Fable 5.1 (output payload)", s)
        self.assertIn("output class weight keeps its current value", s)


class TestReport(unittest.TestCase):
    def test_summary_first_then_the_log_tail_under_a_rule(self):
        body = report("claude-sonnet-5", 4, "Rotation run", RESET_LOG, tail=3)
        head, _, debug = body.partition("\n----\n")
        self.assertTrue(head.startswith("Outcome: FAILED."))
        self.assertTrue(debug.startswith("Debug log (last 3 of 6 lines of out/probe-last.log; tracker.probe exit code 4):\n"))
        self.assertIn("12:57:41Z probe aborted on dave: window reset during probe\n", debug)
        self.assertNotIn("prompt 9 (burst of 9)", debug)  # outside the tail

    def test_cli_reads_the_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "probe-last.log"
            log.write_text(SKIP_LOG, encoding="utf-8")
            proc = subprocess.run([sys.executable, "-m", "tracker.report", "--model", "claude-opus-5",
                                   "--rc", "3", "--what", "Rotation run", "--log", str(log)],
                                  cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("Outcome: SKIPPED."))
        self.assertIn(f"lines of {log}; tracker.probe exit code 3", proc.stdout)

    def test_cli_with_a_missing_log_still_produces_a_body(self):
        proc = subprocess.run([sys.executable, "-m", "tracker.report", "--model", "claude-opus-5",
                               "--rc", "4", "--what", "Rotation run", "--log", "/nonexistent/x.log"],
                              cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("could not read /nonexistent/x.log", proc.stdout)


if __name__ == "__main__":
    unittest.main()
