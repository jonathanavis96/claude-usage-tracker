import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tracker.turns import iter_turns, normalize_model, session_tokens_by_model, transcript_paths

def rec(mid, ts="2026-09-05T20:20:48.817Z", model="claude-fable-5-1", **usage):
    u = {"input_tokens": 2, "cache_creation_input_tokens": 41398, "cache_read_input_tokens": 24483, "output_tokens": 96}
    u.update(usage)
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": model, "usage": u}})

class TurnTests(unittest.TestCase):
    def test_dedups_same_message_id_across_blocks_and_files(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d, "a.jsonl")
            b = Path(d, "b.jsonl")
            a.write_text("\n".join([rec("m1"), rec("m1"), json.dumps({"type": "user", "timestamp": "x"}), "garbage", rec("m2", model="claude-sonnet-5")]) + "\n")
            b.write_text(rec("m1") + "\n")
            turns = list(iter_turns([a, b]))
        self.assertEqual([t.model for t in turns], ["claude-fable-5-1", "claude-sonnet-5"])
        t = turns[0]
        self.assertEqual((t.input, t.output, t.cache_read, t.cache_write), (2, 96, 24483, 41398))
        self.assertEqual(t.total, 2 + 96 + 24483 + 41398)
        self.assertEqual(t.ts, datetime(2026, 9, 5, 20, 20, 48, 817000, tzinfo=timezone.utc))

    def test_paths_filtered_by_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d, "sub", "old.jsonl")
            old.parent.mkdir()
            old.write_text("")
            new = Path(d, "new.jsonl")
            new.write_text("")
            os.utime(old, (0, 0))
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.assertEqual(transcript_paths(Path(d), since), [new])
            self.assertEqual(set(transcript_paths(Path(d), None)), {old, new})


class NormalizeModelTests(unittest.TestCase):
    def test_canonical_ids_pass_through(self):
        for m in ("claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"):
            self.assertEqual(normalize_model(m), m)

    def test_date_suffix_stripped(self):
        self.assertEqual(normalize_model("claude-sonnet-5-20250929"), "claude-sonnet-5")

    def test_1m_marker_stripped(self):
        self.assertEqual(normalize_model("claude-sonnet-5[1m]"), "claude-sonnet-5")

    def test_other_versions_dropped(self):
        for m in ("claude-opus-4-7", "claude-fable-5", "claude-haiku-4-5-20251001",
                  "claude-sonnet-4-6", "<synthetic>"):
            self.assertIsNone(normalize_model(m))


class SessionTokensByModelTests(unittest.TestCase):
    def test_groups_by_file_sums_and_assigns_top_model(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
            a = Path(d, "a.jsonl")
            # Two turns, both sonnet: session total 2000, assigned to sonnet.
            a.write_text("\n".join([
                rec("m1", ts=recent, model="claude-sonnet-5", output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=500),
                rec("m2", ts=recent, model="claude-sonnet-5", output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=500),
            ]) + "\n")
            b = Path(d, "b.jsonl")
            # One turn only -- dropped (needs at least 2 turns).
            b.write_text(rec("m3", ts=recent, model="claude-sonnet-5") + "\n")
            result = session_tokens_by_model([a, b], now=now)
        self.assertEqual(result, {"claude-sonnet-5": 2000})

    def test_drops_sessions_outside_window(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            stale = (now - timedelta(days=40)).isoformat().replace("+00:00", "Z")
            a = Path(d, "a.jsonl")
            a.write_text("\n".join([rec("m1", ts=stale), rec("m2", ts=stale)]) + "\n")
            result = session_tokens_by_model([a], now=now)
        self.assertEqual(result, {})

    def test_drops_sessions_whose_top_model_does_not_normalize(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
            a = Path(d, "a.jsonl")
            a.write_text("\n".join([
                rec("m1", ts=recent, model="claude-haiku-4-5-20251001", output_tokens=900, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=900),
                rec("m2", ts=recent, model="claude-sonnet-5", output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=1),
            ]) + "\n")
            result = session_tokens_by_model([a], now=now)
        self.assertEqual(result, {})

    def test_median_across_multiple_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
            paths = []
            for i, out in enumerate((100, 300, 500)):
                p = Path(d, f"s{i}.jsonl")
                p.write_text("\n".join([
                    rec(f"a{i}", ts=recent, model="claude-opus-5", output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=0),
                    rec(f"b{i}", ts=recent, model="claude-opus-5", output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=0),
                ]) + "\n")
                paths.append(p)
            result = session_tokens_by_model(paths, now=now)
        self.assertEqual(result, {"claude-opus-5": 300})
