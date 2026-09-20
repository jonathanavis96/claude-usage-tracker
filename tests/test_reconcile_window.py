import unittest
from datetime import datetime, timedelta, timezone

from tools.reconcile_window import clean


def _stretch(start: datetime, minutes: int = 60, **fields) -> dict:
    s = {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "delta_pct": 5.0,
        "tokens": {"claude-opus-5": {"input": 1000, "output": 100, "cache_read": 0, "cache_write": 0}},
        "status": "accepted",
        "capture_status": "accepted",
    }
    s.update(fields)
    return s


class CleanTests(unittest.TestCase):
    T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    def test_a_gs_stretch_needs_an_accepted_capture_not_just_an_accepted_status(self):
        # gs-passive.json carries both fields; 42 jwork stretches read status
        # "accepted" with capture_status "unaccounted", and those must not price
        # the window.
        kept = clean([
            _stretch(self.T0),
            _stretch(self.T0 + timedelta(hours=2), status="accepted", capture_status="unaccounted"),
            _stretch(self.T0 + timedelta(hours=4), status="unpriced", capture_status="unpriced"),
        ], "jwork", [])
        self.assertEqual([s["start"] for s, _, _ in kept], [self.T0.isoformat()])

    def test_masterrig_is_not_gated_on_capture(self):
        # Its meter counts other machines, so the capture check is informational
        # there and the stretch is still kept.
        kept = clean([_stretch(self.T0, capture_status="unaccounted")], "masterrig", [])
        self.assertEqual(len(kept), 1)

    def test_a_stretch_overlapping_a_harness_run_on_its_own_account_is_dropped(self):
        runs = [("jwork", self.T0 + timedelta(minutes=30), self.T0 + timedelta(minutes=90))]
        kept = clean([_stretch(self.T0), _stretch(self.T0 + timedelta(hours=3))], "jwork", runs)
        self.assertEqual(len(kept), 1)
        self.assertEqual(clean([_stretch(self.T0)], "dave", runs).__len__(), 1)

    def test_small_or_tokenless_stretches_are_dropped(self):
        kept = clean([_stretch(self.T0, delta_pct=2.9), _stretch(self.T0 + timedelta(hours=2), tokens={})], "jwork", [])
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
