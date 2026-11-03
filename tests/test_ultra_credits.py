"""Defects in tracker/credits.py that real history files trigger, one test class per defect.

Every fixture here is built in memory; nothing reads data/, history/ or any live file except
data/prices.json's `_credits` block, which the rest of the credits tests read the same way.
"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
