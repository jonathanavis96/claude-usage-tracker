import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tracker.turns import iter_turns, transcript_paths

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
