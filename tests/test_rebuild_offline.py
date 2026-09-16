"""The review command must not mutate inputs or enter live publishing paths."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
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

    def _archive(self, tmp: str, *, mix: dict | None, probes: bool = True) -> Path:
        """An archive root holding this checkout's price table and gs readings, an empty probes
        file unless `probes` is false, and `mix` as its reference mix unless it is None."""
        repo = Path(__file__).resolve().parents[1]
        root = Path(tmp) / "archive"
        (root / "data").mkdir(parents=True)
        (root / "history").mkdir()
        (root / "data/prices.json").write_text((repo / "data/prices.json").read_text())
        (root / "history/gs-passive.json").write_text((repo / "history/gs-passive.json").read_text())
        if probes:
            (root / "history/probes.jsonl").write_text("")
        if mix is not None:
            (root / "data/reference_mix.json").write_text(json.dumps(mix))
        return root

    def test_rates_are_derived_on_the_archives_reference_mix(self):
        # Review of PR 57, round 2, finding 3: rates used the running checkout's mix
        # whatever --root held, so an archive with another mix rebuilt with this
        # checkout's token figures under a snapshot labelled offline_archive.
        from tracker.publish import REFERENCE_MIX
        other = dict(REFERENCE_MIX, id="archived-mix-v0", split={"input": 0.5, "output": 0.5, "cache_read": 0.0, "cache_write": 0.0})
        now = datetime(2026, 9, 16, 18, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            archived = rebuild_offline.rebuild(self._archive(tmp, mix=other), now)
        with tempfile.TemporaryDirectory() as tmp:
            current = rebuild_offline.rebuild(self._archive(tmp, mix=REFERENCE_MIX), now)
        with tempfile.TemporaryDirectory() as tmp:
            no_mix = rebuild_offline.rebuild(self._archive(tmp, mix=None), now)
        sonnet = "claude-sonnet-5"
        self.assertEqual((archived["rates"][sonnet]["reference_mix"]["id"], archived["rates"][sonnet]["split"]),
                         ("archived-mix-v0", other["split"]))
        self.assertEqual(archived["rebuild"]["reference_mix_source"], "archive")
        # Same meter budget, different token figures: the archive's mix, not a label on this one's.
        self.assertIsNotNone(current["rates"][sonnet]["tokens_per_window"])
        self.assertEqual(archived["rates"][sonnet]["meter_budget_per_window"], current["rates"][sonnet]["meter_budget_per_window"])
        self.assertNotEqual(archived["rates"][sonnet]["tokens_per_window"], current["rates"][sonnet]["tokens_per_window"])
        # An archive with no mix file says the running checkout's was used.
        self.assertEqual((no_mix["rates"][sonnet]["reference_mix"]["id"], no_mix["rebuild"]["reference_mix_source"]),
                         (REFERENCE_MIX["id"], "running_checkout"))

    def test_an_archive_without_a_probes_file_rebuilds(self):
        # Review of PR 57, round 2, finding 4: load_probes read history/probes.jsonl
        # unguarded, so an archive without it (the probes are retired) failed with exit 1.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._archive(tmp, mix=None, probes=False)
            target = Path(tmp) / "review.json"
            self.assertFalse((root / "history/probes.jsonl").exists())
            self.assertEqual(rebuild_offline.main(["--root", str(root), "--now", "2026-09-16T18:30:00Z", "--out", str(target)]), 0)
            snapshot = json.loads(target.read_text())
            self.assertIsNotNone(snapshot["rates"]["claude-sonnet-5"]["meter_budget_per_window"])
            self.assertIsNone(snapshot["rates"]["claude-sonnet-5"]["probed_at"])
