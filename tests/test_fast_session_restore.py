import copy
import gzip
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests.test_join import PRICES
from tests.test_speed import T0, session
from tools import fast_session_restore as F
from tracker.gs_passive import summarise
from tracker.join import build_stretches
from tracker.samples import Sample
from tracker.turns import turns_in

ACCOUNT = F.ACCOUNT


def S(mins, fh):
    return Sample(T0 + timedelta(minutes=mins), fh, None, "r1", "ceiling")


def body_of(stretches) -> dict:
    meta = {"meter": {"log": "x"}, "transcripts": {"files": 2}}
    body = summarise({ACCOUNT: stretches}, {ACCOUNT: {s.start: "logged" for s in stretches}},
                     {ACCOUNT: meta}, {ACCOUNT: []}, [], PRICES, T0 + timedelta(hours=1))
    return json.loads(json.dumps(body))


class FastSessionRestoreTests(unittest.TestCase):
    def setUp(self):
        # A fast Opus session inside the first stretch, a normal one inside the second.
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
        seen: set[str] = set()
        turns = [t for lines in self.lines.values() for t in turns_in(lines, seen)]
        # The file before #88: every turn counted.
        self.original = body_of(build_stretches(self.samples, turns, PRICES, stretch_pct=10))
        # The file #88's correction left: the fast turns taken out, and recorded as taken.
        fast = [t for t in turns if t.id.startswith("f")]
        taken = {s.start: s for s in build_stretches(self.samples, fast, PRICES, stretch_pct=10,
                                                     fast={t.id for t in fast})}
        rest = build_stretches(self.samples, [t for t in turns if not t.id.startswith("f")], PRICES,
                               stretch_pct=10)
        self.corrected = body_of(rest)
        for s, rec in zip(rest, self.corrected["accounts"][ACCOUNT]["stretches"]):
            f = taken.get(s.start)
            del rec["fast_session_tokens"], rec["fast_session_turns"]
            rec["fast_mode_tokens"] = f.fast_session_tokens if f else {}
            rec["fast_mode_turns"] = f.fast_session_turns if f else 0
        self.corrected["fast_mode_correction"] = {"applied_at": "t", "stretches_changed": 1, "fast_mode_turns": 20}

    def restore(self, body):
        turns, counts = F.fast_turns(self.extract)
        return F.restore(body, turns, self.samples, PRICES, [], {"applied_at": "t2", **counts})

    def restored(self) -> dict:
        out = self.restore(copy.deepcopy(self.corrected))
        assert out is not None
        return out

    def test_the_restored_file_is_the_file_before_the_correction(self):
        out = self.restored()
        self.assertNotEqual(F.without_fast_fields(self.corrected), F.without_fast_fields(self.original))
        self.assertTrue(F.same(F.without_fast_fields(out), F.without_fast_fields(self.original)))

    def test_a_stretch_gets_its_fast_session_tokens_back_and_keeps_them_recorded(self):
        before = self.corrected["accounts"][ACCOUNT]["stretches"]
        after = self.restored()["accounts"][ACCOUNT]["stretches"]
        fast = after[0]
        self.assertEqual(fast["fast_session_turns"], 20)
        self.assertEqual(fast["fast_session_tokens"], before[0]["fast_mode_tokens"])
        self.assertEqual(fast["tokens"]["claude-opus-5"]["output"], 20 * 600)
        self.assertEqual(fast["turns"], before[0]["turns"] + 20)
        self.assertNotIn("fast_mode_tokens", fast)
        for a in after[1:]:
            self.assertEqual((a["fast_session_tokens"], a["fast_session_turns"]), ({}, 0))

    def test_the_restore_is_idempotent(self):
        out = self.restored()
        self.assertEqual(out["fast_session_restore"]["undid"],
                         {"pr": 88, "applied_at": "t", "stretches_changed": 1, "turns": 20})
        self.assertNotIn("fast_mode_correction", out)
        self.assertIsNone(self.restore(json.loads(json.dumps(out))))
        path = self.extract.parent / "stretches.json"
        path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        code = F.main(["--extract", str(self.extract), "--meter-home", str(self.extract.parent),
                       "--stretches", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_tokens_that_do_not_match_what_was_taken_out_are_refused(self):
        rec = self.corrected["accounts"][ACCOUNT]["stretches"][0]
        rec["fast_mode_turns"] -= 1
        with self.assertRaises(ValueError):
            self.restore(self.corrected)

    def test_a_file_that_was_never_corrected_is_refused(self):
        with self.assertRaises(ValueError):
            self.restore(self.original)


if __name__ == "__main__":
    unittest.main()
