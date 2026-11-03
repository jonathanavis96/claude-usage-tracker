"""Defects in tracker/credits.py that real history files trigger, one test class per defect.

Every fixture here is built in memory; nothing reads data/, history/ or any live file except
data/prices.json's `_credits` block, which the rest of the credits tests read the same way.
"""
import unittest
from datetime import datetime, timezone

from tracker import credits as C

CREDITS = C.load_credits()
OPUS_IN, OPUS_OUT = C.rates("opus", CREDITS) or (0.0, 0.0)
POOLED = {"anchor": {"family": "opus", "input": 10 / 15, "output": 50 / 15},
          "pooled_fit": {"times_opus": {"opus": 1.0, "sonnet": 0.5, "fable": 2.0},
                         "cache_read_weight": 0.01, "output_multiplier": 5}}


def tok(input=0, output=0, cache_read=0, cache_write=0):
    return {"input": input, "output": output, "cache_read": cache_read, "cache_write": cache_write}


#: Claude Code's placeholder row: model id `<synthetic>`, every class zero. 129 stretches in
#: history/gs-passive.json and history/masterrig-passive.json carry one (2026-11-03).
SYNTHETIC = {"<synthetic>": tok()}


class PooledValueSkipsEmptyBundleTests(unittest.TestCase):
    """`across_cut_value` with a pooled fit must not drop a stretch for a bundle of no tokens.

    tools/model_rates.py fits a stretch whose only model outside the families is a
    zero-token `<synthetic>` row (its `ok` column is `raw["other"] == 0`), and
    `comparison_value` and `gs_passive.stretch_credits` both skip such a bundle. The pooled
    valuation returned None for it, so `regime_figures` dropped 81 of the 505 stretches the
    fit was measured on, and `weekly_meter.valued` dropped seven-day steps the same way.
    """

    def test_a_zero_token_synthetic_row_prices_at_nothing_rather_than_unpricing(self):
        value = C.across_cut_value(CREDITS, model_rates=POOLED)
        opus_only = {"claude-opus-5": tok(input=1_000_000, cache_read=1_000_000)}
        self.assertAlmostEqual(value({**opus_only, **SYNTHETIC}), value(opus_only))

    def test_a_synthetic_row_that_carries_tokens_still_unprices(self):
        value = C.across_cut_value(CREDITS, model_rates=POOLED)
        self.assertIsNone(value({"claude-opus-5": tok(input=1_000),
                                 "<synthetic>": tok(input=10)}))


class PriceTokensSkipsEmptyBundleTests(unittest.TestCase):
    """`price_tokens(...).priced` must not go False for a bundle of no tokens.

    `fits_as_of` says its selection is exactly the fit's `ok` column, which counts a stretch
    whose only model outside the families is a zero-token `<synthetic>` row; `price_tokens`
    marked it unpriced, so the fits' as-of date, the publisher's per-account newest priced
    stretch and the Fable solve all skipped 39 such stretches of the fits' selection.
    """

    def test_a_zero_token_synthetic_row_leaves_the_stretch_priced(self):
        priced = C.price_tokens({"claude-opus-5": tok(input=300, output=150), **SYNTHETIC},
                                CREDITS, 0)
        self.assertTrue(priced.priced)
        self.assertAlmostEqual(priced.known, 300 * OPUS_IN + 150 * OPUS_OUT)
        self.assertEqual((priced.raw, priced.charged), (450, 450))

    def test_a_synthetic_row_that_carries_tokens_still_unprices(self):
        self.assertFalse(C.price_tokens({"claude-opus-5": tok(input=300),
                                         "<synthetic>": tok(input=10)}, CREDITS, 0).priced)

    def test_fits_as_of_reads_the_newest_fitted_stretch_even_with_a_synthetic_row(self):
        older = {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T02:00:00+00:00",
                 "delta_pct": 10.0, "tokens": {"claude-opus-5": tok(input=1_000)}}
        newer = {"start": "2026-10-02T00:00:00+00:00", "end": "2026-10-02T02:00:00+00:00",
                 "delta_pct": 10.0, "tokens": {"claude-opus-5": tok(input=1_000), **SYNTHETIC}}
        got = C.fits_as_of({"jwork": [older, newer]}, CREDITS, {"fits_pooled": ["jwork/post"]})
        self.assertEqual(got, newer["end"])


class StamplessStretchTests(unittest.TestCase):
    """A stretch with no `start`/`end` must not crash a selection that `clean_stretches` let
    it through: that function keeps it ("a missing stamp must not be read as 'overlaps
    nothing' in one place and crash in another"), and `window_stretches` and `spans_cut` keep
    it too, but the masterrig filters raised KeyError and `_side_of` raised on it."""

    @staticmethod
    def _stretch(**extra) -> dict:
        return {"delta_pct": 10.0, "tokens": {"claude-opus-5": tok(input=1_000)},
                "status": "accepted", "capture_status": "accepted", **extra}

    def test_the_rate_fit_selection_keeps_a_stampless_masterrig_stretch(self):
        dated = self._stretch(start="2026-09-10T00:00:00+00:00", end="2026-09-10T02:00:00+00:00")
        kept = C.rate_fit_stretches({"masterrig": [self._stretch(), dated]}, [])
        self.assertEqual(len(kept["masterrig"]), 2)
        self.assertEqual(len(C.window_stretches({"masterrig": [self._stretch(), dated]}, [])
                             ["masterrig"]), 2)

    def test_the_known_date_selection_keeps_a_stampless_masterrig_stretch(self):
        kept = C.announced_change_stretches({"masterrig": [self._stretch()]}, [])
        self.assertEqual(len(kept["masterrig"]), 1)

    def test_a_stampless_stretch_is_on_neither_side_of_a_candidate(self):
        at = datetime(2026, 9, 20, tzinfo=timezone.utc)
        split = C.split_at_candidate([self._stretch()], at, None, None, lambda t: 1_000.0)
        self.assertEqual(split["sides"], {"before": [], "after": []})


if __name__ == "__main__":
    unittest.main()
