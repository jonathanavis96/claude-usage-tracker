import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tracker.cloud_sessions import _cloud_turn_times
from tracker.unclaimed import first_record


class TranscriptEdgeTests(unittest.TestCase):
    def _file(self, d, lines):
        p = Path(d, "t.jsonl")
        p.write_text("".join(json.dumps(x) + "\n" for x in lines))
        return p

    def test_cloud_turn_times_skip_a_bad_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d, [{"remoteSourced": True, "timestamp": "garbage"},
                               {"remoteSourced": True, "timestamp": 5},
                               {"remoteSourced": True, "timestamp": "2026-09-01T10:00:00Z"}])
            self.assertEqual(_cloud_turn_times(p), [datetime(2026, 9, 1, 10, tzinfo=timezone.utc)])

    def test_first_record_skips_a_bad_stamp_and_reads_naive_as_utc(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d, [{"timestamp": "garbage", "cwd": "/x"},
                               {"timestamp": "2026-09-01T10:00:00"}])
            self.assertEqual(first_record(p), (datetime(2026, 9, 1, 10, tzinfo=timezone.utc), "/x"))
