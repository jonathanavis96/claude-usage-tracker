"""The 2026-07-30..2026-08-19 passive days restored from old commits survive a rebuild."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from tests.test_passive import dr
from tools import restore_passive_days as restore
from tracker import passive

ROOT = Path(__file__).resolve().parent.parent


class RestoredDaysTests(unittest.TestCase):
    def test_merge_adds_missing_days_and_never_overwrites(self):
        current = {"history": {"2026-08-19": {"tokens_per_pct": 7, "interpolated": False}}}
        merged, added = restore.merge(current, {"2026-08-18": {"tokens_per_pct": 1, "interpolated": True},
                                                "2026-08-19": {"tokens_per_pct": 2, "interpolated": False}})
        self.assertEqual(added, ["2026-08-18"])
        self.assertEqual(merged["history"]["2026-08-19"]["tokens_per_pct"], 7)
        self.assertEqual(list(merged["history"]), ["2026-08-18", "2026-08-19"])

    def test_committed_file_holds_the_restored_days(self):
        hist = json.loads((ROOT / "history" / "passive.json").read_text())["history"]
        for ds in ("2026-07-30", "2026-08-11", "2026-08-19"):
            self.assertIn(ds, hist)

    def test_a_restored_day_survives_a_rebuild(self):
        src = json.loads((ROOT / "history" / "passive.json").read_text())
        kept = src["history"]["2026-08-01"]
        # The transcripts now reach back only to 2026-08-20, as on masterrig.
        rates = {date(2026, 8, 20) + timedelta(days=i): dr(900000) for i in range(30)}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "passive.json"
            out.write_text(json.dumps(src))
            with mock.patch.object(passive, "_rebuild", return_value=(rates, {}, None)):
                self.assertEqual(passive.main(["--out", str(out)]), 0)
            written = json.loads(out.read_text())
        self.assertEqual(written["history"]["2026-08-01"], kept)
        self.assertIn("2026-07-30", written["history"])
        self.assertIsNotNone(written["plan_ratio_5x_to_20x"], "the restored pre-change days feed the ratio again")


if __name__ == "__main__":
    unittest.main()
