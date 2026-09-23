import gzip
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests.test_join import PRICES
from tests.test_speed import T0, session
from tools import fast_mode_correction as F
from tracker.gs_passive import summarise
from tracker.join import build_stretches
from tracker.samples import Sample
from tracker.turns import turns_in

ACCOUNT = F.ACCOUNT


def S(mins, fh):
    return Sample(T0 + timedelta(minutes=mins), fh, None, "r1", "ceiling")


def committed(lines_by_file: dict[str, list[dict]], samples: list) -> dict:
    """A stretch file as the join wrote it before fast mode was taken out."""
    seen: set[str] = set()
    turns = [t for lines in lines_by_file.values() for t in turns_in(lines, seen)]
    stretches = build_stretches(samples, turns, PRICES, stretch_pct=10)
    meta = {"meter": {"log": "x"}, "transcripts": {"files": len(lines_by_file)}}
    body = summarise({ACCOUNT: stretches}, {ACCOUNT: {s.start: "logged" for s in stretches}},
                     {ACCOUNT: meta}, {ACCOUNT: []}, [], PRICES, T0 + timedelta(hours=1))
    body = json.loads(json.dumps(body))
    for rec in body["accounts"][ACCOUNT]["stretches"]:
        del rec["fast_mode_tokens"], rec["fast_mode_turns"]
    return body


class FastModeCorrectionTests(unittest.TestCase):
    def setUp(self):
        # A fast Opus session inside the first stretch, a standard one inside the second.
        self.lines = {"fast.jsonl": session(20, 170, prefix="f"),
                      "std.jsonl": session(40, 70, start=330, prefix="s")}
        self.samples = [S(0, 0), S(5, 10), S(10, 20), S(15, 30)]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.extract = Path(tmp.name) / "extract.jsonl.gz"
        with gzip.open(self.extract, "wt", encoding="utf-8") as fh:
            for name, lines in self.lines.items():
                for d in lines:
                    fh.write(json.dumps({"file": name, **d}) + "\n")
        self.body = committed(self.lines, self.samples)

    def correct(self, body):
        turns, counts = F.fast_turns(self.extract)
        out = F.correct(body, turns, self.samples, PRICES, [], {"applied_at": "t", **counts})
        assert out is not None
        return out

    def test_a_stretch_loses_exactly_its_fast_mode_tokens_and_records_them(self):
        before = self.body["accounts"][ACCOUNT]["stretches"]
        out = self.correct(self.body)
        after = out["accounts"][ACCOUNT]["stretches"]
        self.assertEqual(len(after), len(before))
        fast = after[0]
        self.assertEqual(fast["fast_mode_turns"], 20)
        self.assertEqual(fast["fast_mode_tokens"],
                         {"claude-opus-5": {"input": 40, "output": 12_000, "cache_read": 1_000_000,
                                            "cache_write": 20_000}})
        self.assertNotIn("claude-opus-5", fast["tokens"])
        self.assertEqual(fast["turns"], before[0]["turns"] - 20)
        self.assertEqual(out["fast_mode_correction"]["stretches_changed"], 1)

    def test_a_stretch_with_no_fast_mode_request_is_unchanged(self):
        before = self.body["accounts"][ACCOUNT]["stretches"]
        after = self.correct(self.body)["accounts"][ACCOUNT]["stretches"]
        self.assertGreater(len(before), 1)
        for b, a in zip(before[1:], after[1:]):
            self.assertEqual((a["fast_mode_tokens"], a["fast_mode_turns"]), ({}, 0))
            for k in ("start", "end", "delta_pct", "tokens", "usd", "usd_per_pct", "bounds", "turns"):
                self.assertEqual(a[k], b[k], k)

    def test_the_correction_is_idempotent(self):
        out = self.correct(self.body)
        turns, _ = F.fast_turns(self.extract)
        self.assertIsNone(F.correct(json.loads(json.dumps(out)), turns, self.samples, PRICES, [], {}))
        path = self.extract.parent / "stretches.json"
        path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        code = F.main(["--extract", str(self.extract), "--meter-home", str(self.extract.parent),
                       "--stretches", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_a_file_that_does_not_rebuild_from_its_own_records_is_refused(self):
        self.body["accounts"][ACCOUNT]["stretches"][0]["usd"] += 1.0
        with self.assertRaises(ValueError):
            self.correct(self.body)


if __name__ == "__main__":
    unittest.main()
