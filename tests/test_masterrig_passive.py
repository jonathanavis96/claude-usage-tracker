"""masterrig joined as an account, with its per-stretch, per-model detail retained (issue #52).

The fixture is masterrig's own meter shape: moonlighter's reset-bearing JSONL and the
ceiling's systemd log, two logs of one meter, against two transcripts under
`~/.claude/projects`. What is asserted is the record -- the same shape gs accounts get --
and the two things masterrig data does that gs data never has: readings with no reset id
partway through, and a stretch the transcripts leave empty, which used to crash the
capture check's run classifier.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from tracker.capture import UNKNOWN
from tracker.gs_passive import (
    NO_TOKENS,
    Account,
    MeterLog,
    gs_accounts,
    load_samples,
    main,
    masterrig_account,
    report,
)

T0 = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
RESETS = "2026-09-14T13:00:00+00:00"
SEVEN_RESETS = "2026-09-17T23:00:00+00:00"
PRICES = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5},
          "claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25}}
#: Cache-write tokens worth exactly $1 of meter value at PRICES, per model.
DOLLAR = {"claude-sonnet-5": 400_000, "claude-opus-5": 160_000}


def turn_line(ts: datetime, mid: str, model: str = "claude-sonnet-5") -> str:
    """One assistant turn worth $1 of meter value, all of it cache_write."""
    return json.dumps({"type": "assistant", "timestamp": ts.isoformat().replace("+00:00", "Z"), "sessionId": "s",
                       "message": {"id": mid, "model": model,
                                   "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                             "cache_creation_input_tokens": DOLLAR[model]}}})


def _moonlighter(i: int) -> str:
    return json.dumps({"ts": (T0 + timedelta(minutes=5 * i)).isoformat(),
                       "five_hour": {"utilization": 2.0 * i, "resets_at": RESETS},
                       "seven_day": {"utilization": float(i // 3), "resets_at": SEVEN_RESETS}})


def _ceiling(i: int) -> str:
    return f"{(T0 + timedelta(minutes=5 * i)).isoformat()} 5-hour {2 * i}% / 7-day {i // 3}%"


def masterrig_home(root: Path, readings: int = 41, moonlighter_until: int = 30,
                   opus_stretch: int | None = 3, empty_stretches: int = 0) -> Path:
    """The meter rising 2% every five minutes, $1 of transcripts per reading: $0.50 per 1%.

    `moonlighter_until` is the last reading moonlighter wrote; the ceiling log carries the
    rest, without reset ids. Turns alternate between two transcript files, so the join has
    to read both. `opus_stretch`, if given, is spent on Opus instead of Sonnet, so the
    record's per-model detail has something to keep. `empty_stretches` leading stretches
    get no turns at all -- masterrig's off-host use, which is what leaves the capture
    check with no reference.
    """
    home = root / "home"
    for d in ("projects/-proj-a", "projects/-proj-b"):
        (home / ".claude" / d).mkdir(parents=True)
    (home / ".moonlighter").mkdir(parents=True)
    ops = home / ".paperclip" / "ops"
    ops.mkdir(parents=True)
    (home / ".moonlighter" / "usage_log.jsonl").write_text(
        "\n".join(_moonlighter(i) for i in range(min(readings, moonlighter_until + 1))) + "\n")
    (ops / "mis-usage-ceiling-systemd.log").write_text(
        "\n".join(["Started claude usage ceiling check."]
                  + [_ceiling(i) for i in range(moonlighter_until + 1, readings)]) + "\n")
    files: dict[str, list[str]] = {"-proj-a": [], "-proj-b": []}
    for i in range(readings - 1):
        if i // 5 < empty_stretches:
            continue
        model = "claude-opus-5" if i // 5 == opus_stretch else "claude-sonnet-5"
        files["-proj-a" if i % 2 == 0 else "-proj-b"].append(
            turn_line(T0 + timedelta(minutes=5 * i + 1), f"m{i}", model))
    for name, lines in files.items():
        (home / ".claude" / "projects" / name / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
    return home


class AccountTests(unittest.TestCase):
    def test_the_masterrig_mapping_names_two_logs_of_one_meter(self):
        a = masterrig_account(Path("/h"))
        self.assertEqual((a.name, a.config_dir), ("masterrig", Path("/h/.claude")))
        self.assertEqual((a.meter_log, a.meter_format), (Path("/h/.moonlighter/usage_log.jsonl"), "moonlighter"))
        self.assertEqual(a.extra_meter_logs,
                         (MeterLog(Path("/h/.paperclip/ops/mis-usage-ceiling-systemd.log"), "ceiling"),))
        self.assertIsNone(a.legacy_meter_log)
        # It stays out of the gs mapping: it is a different kind of instrument.
        self.assertNotIn("masterrig", gs_accounts(Path("/h")))

    def test_the_meter_note_says_the_meter_counts_more_than_this_host(self):
        note = masterrig_account(Path("/h")).meter_note
        self.assertIsNotNone(note)
        for phrase in ("web", "phone", "capture"):
            self.assertIn(phrase, note)

    def test_both_logs_are_read_and_merged_a_minute_at_a_time(self):
        with tempfile.TemporaryDirectory() as d:
            home = masterrig_home(Path(d))
            samples = load_samples(masterrig_account(home))
        self.assertEqual(len(samples), 41)
        self.assertEqual([s.source for s in samples].count("moonlighter"), 31)
        self.assertEqual([s.source for s in samples].count("ceiling"), 10)
        self.assertTrue(all(a.ts < b.ts for a, b in pairwise(samples)))
        # Only moonlighter records reset times; the ceiling log has none.
        self.assertEqual(sum(1 for s in samples if s.resets_at is not None), 31)

    def test_a_reading_both_logs_hold_is_taken_once_from_the_reset_bearing_one(self):
        # merge_samples keys on the minute and prefers a reset-bearing source, so an
        # overlap does not pair a reading with itself and add a zero-movement pair.
        with tempfile.TemporaryDirectory() as d:
            home = masterrig_home(Path(d), readings=41, moonlighter_until=40)
            ops = home / ".paperclip" / "ops" / "mis-usage-ceiling-systemd.log"
            ops.write_text("\n".join(_ceiling(i) for i in range(41)) + "\n")
            samples = load_samples(masterrig_account(home))
        self.assertEqual(len(samples), 41)
        self.assertEqual({s.source for s in samples}, {"ceiling"})  # ceiling wins the minute

    def test_an_unknown_meter_format_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "m.log"
            log.write_text("whatever\n")
            with self.assertRaises(ValueError) as cm:
                load_samples(Account("x", Path(d), log, "not-a-format"))
        self.assertIn("not-a-format", str(cm.exception))


class RecordTests(unittest.TestCase):
    def _report(self, home: Path) -> dict:
        return report({"masterrig": masterrig_account(home)}, PRICES, now=T0 + timedelta(days=1))

    def test_every_stretch_is_kept_with_its_own_per_model_token_counts(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._report(masterrig_home(Path(d)))["accounts"]["masterrig"]
        self.assertEqual(r["account"], "masterrig")
        self.assertEqual(len(r["stretches"]), 8)
        self.assertEqual([s["status"] for s in r["stretches"]], ["accepted"] * 8)
        for s in r["stretches"]:
            self.assertEqual(s["delta_pct"], 10.0)
            self.assertAlmostEqual(s["usd_per_pct"], 0.5)
            self.assertEqual(s["turns"], 5)
        # Stretch 3 was spent on Opus: the record keeps the model, not just the total.
        self.assertEqual(list(r["stretches"][3]["tokens"]), ["claude-opus-5"])
        self.assertEqual(r["stretches"][3]["tokens"]["claude-opus-5"]["cache_write"], 5 * DOLLAR["claude-opus-5"])
        self.assertEqual(list(r["stretches"][0]["tokens"]), ["claude-sonnet-5"])
        self.assertEqual(r["stretches"][0]["tokens"]["claude-sonnet-5"]["cache_write"], 5 * DOLLAR["claude-sonnet-5"])

    def test_stretches_built_on_the_reset_less_ceiling_log_are_not_reset_verified(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._report(masterrig_home(Path(d)))["accounts"]["masterrig"]
        # Readings 0..30 are moonlighter's, 31..40 the ceiling's: the last two stretches
        # straddle or sit inside the reset-less part.
        self.assertEqual([s["reset_verified"] for s in r["stretches"]], [True] * 6 + [False] * 2)
        self.assertEqual(r["meter"]["reset_verified_samples"], 31)

    def test_the_record_has_the_same_shape_as_a_gs_accounts(self):
        from tests.test_gs_passive import dave_home
        with tempfile.TemporaryDirectory() as d:
            mine = self._report(masterrig_home(Path(d)))["accounts"]["masterrig"]
        with tempfile.TemporaryDirectory() as d:
            theirs = report({"dave": gs_accounts(dave_home(Path(d)))["dave"]}, PRICES,
                            now=T0 + timedelta(days=1))["accounts"]["dave"]
        self.assertEqual(sorted(mine), sorted(theirs))
        for key in ("stretches", "runs", "daily", "split", "state", "spread", "weekly_by_window",
                    "meter", "transcripts"):
            self.assertIn(key, mine)
        self.assertEqual(sorted(mine["stretches"][0]), sorted(theirs["stretches"][0]))

    def test_the_meter_meta_names_both_logs_and_carries_the_note(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._report(masterrig_home(Path(d)))["accounts"]["masterrig"]
        self.assertEqual(r["meter"]["format"], "moonlighter")
        self.assertEqual(r["meter"]["samples"], 41)
        self.assertEqual([e["format"] for e in r["meter"]["extra_logs"]], ["ceiling"])
        self.assertIn("mis-usage-ceiling-systemd.log", r["meter"]["extra_logs"][0]["log"])
        self.assertIn("web", r["meter"]["note"])
        # A gs account has the same fields, empty.
        self.assertEqual(r["transcripts"]["files"], 2)

    def test_the_day_is_pooled_and_the_spread_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._report(masterrig_home(Path(d)))["accounts"]["masterrig"]
        self.assertEqual([(dd["date"], dd["usd_per_pct"], dd["stretches"]) for dd in r["daily"]],
                         [("2026-09-14", 0.5, 8)])
        self.assertEqual(r["spread"]["stretch"]["n"], 8)
        self.assertEqual(r["state"], {"state": "ok"})
        self.assertTrue(r["weekly_by_window"])
        self.assertEqual(r["split"], {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0})


class EmptyCaptureTests(unittest.TestCase):
    """masterrig's own failure mode: the meter moves on work this host never saw.

    Five leading stretches with no turns make the capture check's bootstrap median zero.
    Every later stretch is then judged against a reference of zero -- no capture to
    divide out -- and the run of them had nothing to take a median of:
    `statistics.StatisticsError: no median for empty data` from Run.capture. The run is
    now `unknown` and the stretches are still written.
    """

    def _report(self, d: Path) -> dict:
        home = masterrig_home(d, readings=51, moonlighter_until=50, opus_stretch=None, empty_stretches=5)
        return report({"masterrig": masterrig_account(home)}, PRICES, now=T0 + timedelta(days=1))

    def test_a_run_with_no_capture_verdicts_is_unknown_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._report(Path(d))["accounts"]["masterrig"]
        self.assertEqual(len(r["stretches"]), 10)
        self.assertEqual([v["reference"] for v in r["stretches"][:5]], [0.0] * 5)
        self.assertEqual([(run["kind"], run["capture"], run["step"]) for run in r["runs"]],
                         [(UNKNOWN, None, None)])

    def test_a_stretch_the_transcripts_leave_empty_is_not_called_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._report(Path(d))["accounts"]["masterrig"]
        self.assertEqual([s["status"] for s in r["stretches"][:5]], [NO_TOKENS] * 5)
        self.assertEqual([s["tokens"] for s in r["stretches"][:5]], [{}] * 5)
        # The five real ones are published, and the empty ones do not drag the day down.
        self.assertEqual([s["status"] for s in r["stretches"][5:]], ["accepted"] * 5)
        self.assertEqual([(dd["usd_per_pct"], dd["stretches"]) for dd in r["daily"]], [(0.5, 5)])

    def test_the_spread_of_readings_that_spent_nothing_has_a_count_and_nothing_else(self):
        from tracker.gs_passive import spread
        self.assertEqual(spread([0.0, 0.0, 0.0]),
                         {"n": 3, "median": 0.0, "half_range": None, "mad": None, "cv": None})


class CliTests(unittest.TestCase):
    def test_the_masterrig_flag_writes_the_record(self):
        with tempfile.TemporaryDirectory() as d:
            home = masterrig_home(Path(d))
            prices, out = Path(d) / "prices.json", Path(d) / "out" / "masterrig-passive.json"
            prices.write_text(json.dumps(dict(PRICES, _source="test")))
            rc = main(["--home", str(home), "--masterrig", "--prices", str(prices),
                       "--probes", str(Path(d) / "none.jsonl"), "--out", str(out)])
            self.assertEqual(rc, 0)
            written = json.loads(out.read_text())
        self.assertEqual(list(written["accounts"]), ["masterrig"])
        self.assertEqual(written["accounts"]["masterrig"]["account"], "masterrig")
        self.assertEqual(len(written["accounts"]["masterrig"]["stretches"]), 8)

    def test_without_the_flag_the_gs_accounts_are_joined_as_before(self):
        with tempfile.TemporaryDirectory() as d:
            home = masterrig_home(Path(d))
            prices = Path(d) / "prices.json"
            prices.write_text(json.dumps(PRICES))
            rc = main(["--home", str(home), "--prices", str(prices), "--probes", str(Path(d) / "none.jsonl")])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
