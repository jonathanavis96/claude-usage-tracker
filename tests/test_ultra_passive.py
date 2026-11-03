"""Defects found in tracker/gs_passive.py, tracker/passive.py and tracker/meter_log.py (2026-11-03)."""
import unittest
from datetime import datetime, timedelta, timezone

from tracker.gs_passive import passive_dollar_readings

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
PRICES = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}}
EMPTY = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def _stretch(end: datetime, tokens: dict, delta: float = 10.0) -> dict:
    return {"start": (end - timedelta(hours=1)).isoformat(), "end": end.isoformat(), "delta_pct": delta,
            "windows": 1, "tokens": tokens, "unpriced_tokens": 0, "status": "accepted",
            "reset_verified": True}


class EmptySyntheticBundleTest(unittest.TestCase):
    """Claude Code's `<synthetic>` placeholder rows carry no tokens and no price. A stretch that
    holds one beside real work is still a reading: on the real 2026-11-02 record 59 of dave's
    223 accepted, reset-verified stretches carried such a bundle and every one of them was left
    out of the published dollar series, while the credit series (`passive_credit_points`)
    already kept them."""

    def test_an_empty_synthetic_bundle_does_not_drop_the_stretch(self):
        work = {"claude-sonnet-5": dict(EMPTY, cache_write=4_000_000)}  # $10 of meter value
        report = {"accounts": {"dave": {"stretches": [
            _stretch(T0, work),
            _stretch(T0 + timedelta(hours=2), {**work, "<synthetic>": dict(EMPTY)}),
        ]}}}
        readings = passive_dollar_readings(report, PRICES, by="stretch")
        self.assertEqual([(t, round(v, 6)) for t, v in readings],
                         [(T0, 100.0), (T0 + timedelta(hours=2), 100.0)])

    def test_a_synthetic_bundle_with_tokens_still_drops_the_stretch(self):
        work = {"claude-sonnet-5": dict(EMPTY, cache_write=4_000_000)}
        report = {"accounts": {"dave": {"stretches": [
            _stretch(T0, {**work, "<synthetic>": dict(EMPTY, output=50)})]}}}
        self.assertEqual(passive_dollar_readings(report, PRICES, by="stretch"), [])

    def test_a_stretch_of_only_an_empty_synthetic_bundle_is_no_reading(self):
        # Nothing priced explains the meter's movement: no $0 reading.
        report = {"accounts": {"dave": {"stretches": [_stretch(T0, {"<synthetic>": dict(EMPTY)})]}}}
        self.assertEqual(passive_dollar_readings(report, PRICES, by="stretch"), [])
        self.assertEqual(passive_dollar_readings(report, PRICES, by="day"), [])


if __name__ == "__main__":
    unittest.main()
