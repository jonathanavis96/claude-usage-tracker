import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tracker.gs_passive import (JWORK_CEILING_SINCE, gs_accounts, main, passive_dollar_readings, probe_readings,
                                report)
from tracker.publish import usd_per_pct

T0 = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
DOLLAR = 400_000  # Sonnet cache-write tokens worth exactly $1 of meter value
PRICES = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5},
          "claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25}}


def turn_line(ts, mid, cw=DOLLAR, model="claude-sonnet-5"):
    return json.dumps({"type": "assistant", "timestamp": ts.isoformat().replace("+00:00", "Z"), "sessionId": "s",
                       "message": {"id": mid, "model": model,
                                   "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                             "cache_creation_input_tokens": cw}}})


def dave_home(root: Path, stretches=8, withheld_stretch=None) -> Path:
    """Dave's meter rising 2% every five minutes, $1 of transcripts per reading: $0.50 per 1%."""
    home = root / "home"
    dave = home / ".claude-dave"
    for d in ("projects/-proj-a", "projects/-proj-b"):
        (dave / d).mkdir(parents=True)
    (dave / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-dave"}}))
    ops = home / ".paperclip" / "ops"
    ops.mkdir(parents=True)
    meter = []
    for i in range(stretches * 5 + 1):
        meter.append(json.dumps({"ts": (T0 + timedelta(minutes=5 * i)).isoformat(), "account": "dave", "identity": "x",
                                 "five_hour": {"utilization": 2.0 * i, "resets_at": "2026-09-14T13:00:00+00:00"},
                                 "seven_day": {"utilization": float(i // 3), "resets_at": "2026-09-17T23:00:00+00:00"}}))
    (ops / "claude-usage-meter-dave.log").write_text("\n".join(meter) + "\n")
    kept, held = [], []
    for i in range(stretches * 5):
        (held if i // 5 == withheld_stretch else kept).append(turn_line(T0 + timedelta(minutes=5 * i + 1), f"m{i}"))
    (dave / "projects/-proj-a/s1.jsonl").write_text("\n".join(kept) + "\n")
    (dave / "projects/-proj-b/s2.jsonl").write_text("\n".join(held) + "\n")
    return home


def jwork_home(root: Path) -> Path:
    home = root / "home"
    shared = home / ".claude" / "projects" / "-proj"
    shared.mkdir(parents=True)
    for name in (".claude-javiswork", ".claude-jono"):
        (home / name).mkdir()
        (home / name / "projects").symlink_to(home / ".claude" / "projects")
    ops = home / ".paperclip" / "ops"
    ops.mkdir(parents=True)
    before = [f"{(JWORK_CEILING_SINCE - timedelta(minutes=5 * k)).isoformat()} ok five_hour=90% seven_day=31%"
              for k in range(12, 0, -1)]
    after = [f"{(JWORK_CEILING_SINCE + timedelta(minutes=1 + 5 * i)).isoformat()} ok five_hour={2 * i}% seven_day={i // 3}%"
             for i in range(41)]
    (ops / "gs-usage-ceiling.log").write_text("\n".join(before + ["x usage read FAILED — failing open"] + after) + "\n")
    turns = [turn_line(JWORK_CEILING_SINCE + timedelta(minutes=2 + 5 * i), f"j{i}") for i in range(40)]
    (shared / "s.jsonl").write_text("\n".join(turns) + "\n")
    return home


class AccountTests(unittest.TestCase):
    def test_the_account_mapping_lives_in_one_place(self):
        a = gs_accounts(Path("/h"))
        self.assertEqual(a["dave"].config_dir, Path("/h/.claude-dave"))
        self.assertEqual(a["dave"].meter_log, Path("/h/.paperclip/ops/claude-usage-meter-dave.log"))
        self.assertEqual(a["jwork"].config_dir, Path("/h/.claude-javiswork"))
        self.assertEqual(a["jwork"].meter_log, Path("/h/.paperclip/ops/gs-usage-ceiling.log"))
        self.assertEqual(a["jwork"].meter_since, JWORK_CEILING_SINCE)
        self.assertNotEqual(a["dave"].meter_log, a["jwork"].meter_log)


class ReportTests(unittest.TestCase):
    def test_each_account_is_valued_against_its_own_meter(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            r = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))["accounts"]["dave"]
        self.assertEqual([s["status"] for s in r["stretches"]], ["accepted"] * 8)
        self.assertAlmostEqual(r["stretches"][0]["usd_per_pct"], 0.5)
        self.assertEqual(r["daily"], [{"date": "2026-09-14", "usd_per_pct": 0.5, "delta_pct": 80.0, "stretches": 8,
                                       "rounding": 0.0125}])
        self.assertEqual(r["state"], {"state": "ok"})
        self.assertEqual(r["last_usable_at"], (T0 + timedelta(minutes=200)).isoformat())
        self.assertEqual(r["spread"]["stretch"]["n"], 8)
        self.assertTrue(r["weekly_by_window"])

    def test_withheld_transcripts_are_caught_and_never_reach_the_publisher(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d), withheld_stretch=6)
            full = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))
            cut = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1),
                         withhold={"dave": ["-proj-b/*"]})
        self.assertEqual(full["accounts"]["dave"]["stretches"][6]["status"], "accepted")
        held = cut["accounts"]["dave"]["stretches"][6]
        self.assertEqual((held["status"], held["capture"]), ("unaccounted", 0.0))
        self.assertEqual(cut["accounts"]["dave"]["transcripts"]["withheld_patterns"], ["-proj-b/*"])
        self.assertEqual([(r["kind"], r["stretches"]) for r in cut["accounts"]["dave"]["runs"]], [("collection_gap", 1)])
        readings = passive_dollar_readings(cut, PRICES, by="stretch")
        self.assertEqual(len(readings), 7)
        self.assertNotIn(datetime.fromisoformat(held["end"]), [t for t, _ in readings])
        self.assertEqual([round(v, 6) for _, v in passive_dollar_readings(cut, PRICES)], [50.0])

    def test_published_readings_are_meter_dollars_per_window_at_the_prices_they_are_published_with(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            r = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))
        dearer = {m: dict(p, cache_write=p["cache_write"] * 2) for m, p in PRICES.items()}
        self.assertEqual([(t, round(v, 6)) for t, v in passive_dollar_readings(r, PRICES)],
                         [(T0 + timedelta(minutes=200), 50.0)])
        self.assertEqual([round(v, 6) for _, v in passive_dollar_readings(r, dearer)], [100.0])

    def test_jwork_readings_from_before_the_ceiling_read_jwork_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            home = jwork_home(Path(d))
            r = report({"jwork": gs_accounts(home)["jwork"]}, PRICES, now=T0)["accounts"]["jwork"]
        self.assertEqual(r["meter"]["samples"], 41)
        self.assertGreaterEqual(datetime.fromisoformat(r["meter"]["first"]), JWORK_CEILING_SINCE)
        self.assertEqual(len(r["stretches"]), 8)

    def test_a_transcript_directory_shared_with_other_config_dirs_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            home = jwork_home(Path(d))
            r = report({"jwork": gs_accounts(home)["jwork"]}, PRICES, now=T0)["accounts"]["jwork"]
        self.assertEqual(r["transcripts"]["shared_with"], [".claude", ".claude-jono"])


class ProbeReadingTests(unittest.TestCase):
    def test_usable_probe_rows_become_dollar_readings_for_step_evidence(self):
        row = {"ts": "2026-09-14T12:00:00+00:00", "model": "claude-opus-5", "tick_from": 2, "tick_to": 5,
               "tokens": {"input": 60, "output": 150, "cache_read": 468_390, "cache_write": 814_306}}
        rows = [row, dict(row, ts="2026-09-14T13:00:00+00:00", outlier=True),
                dict(row, ts="2026-09-14T14:00:00+00:00", payload="output")]
        self.assertEqual(probe_readings(rows, PRICES),
                         [(datetime.fromisoformat(row["ts"]), usd_per_pct(row, PRICES["claude-opus-5"]))])


class CliTests(unittest.TestCase):
    def test_writes_the_report(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            prices, out = Path(d) / "prices.json", Path(d) / "out" / "passive-gs.json"
            prices.write_text(json.dumps(dict(PRICES, _source="test")))
            rc = main(["--home", str(home), "--account", "dave", "--prices", str(prices),
                       "--probes", str(Path(d) / "none.jsonl"), "--out", str(out)])
            self.assertEqual(rc, 0)
            self.assertEqual(list(json.loads(out.read_text())["accounts"]), ["dave"])
