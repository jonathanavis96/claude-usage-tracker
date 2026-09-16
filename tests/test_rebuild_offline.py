"""The review command must not mutate inputs or enter live publishing paths."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker import rebuild_offline


class OfflineRebuildTests(unittest.TestCase):
    def test_rejects_input_overwrite_before_reading_or_building(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "data/prices.json"
            target.parent.mkdir()
            target.write_text('{"preserved": true}')
            with patch.object(rebuild_offline, "rebuild") as build:
                with self.assertRaises(SystemExit) as raised:
                    rebuild_offline.main(["--root", tmp, "--now", "2026-09-16T12:00:00Z", "--out", str(target)])
                self.assertEqual(raised.exception.code, 1)
                build.assert_not_called()
            self.assertEqual(json.loads(target.read_text()), {"preserved": True})

    def test_requires_timezone(self):
        with patch.object(rebuild_offline, "rebuild") as build:
            with self.assertRaises(SystemExit):
                rebuild_offline.main(["--now", "2026-09-16T12:00:00", "--out", "unused.json"])
            build.assert_not_called()

    def test_writes_only_explicit_review_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "review.json"
            with patch.object(rebuild_offline, "rebuild", return_value={"schema_version": 2}) as build:
                self.assertEqual(rebuild_offline.main(["--root", tmp, "--now", "2026-09-16T14:00:00+02:00", "--out", str(target)]), 0)
                self.assertEqual(build.call_args.args[1].hour, 12)
            self.assertEqual(json.loads(target.read_text()), {"schema_version": 2})
