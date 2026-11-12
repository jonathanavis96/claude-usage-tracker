"""Opus 5.5 held at Anthropic's list ratio in every comparison (ADR 0001 rule 23).

`credits.PINNED_AT_LIST` names the families every change figure values at their input
list-price ratio to the Opus anchor (data/prices.json) instead of a fitted rate, and the
four-account detector moves no pinned family across a rate interval (`rate_intervals`).
Every other family keeps its fitted rate, and the per-model rate the page publishes is
untouched.
"""
from __future__ import annotations

import copy
import unittest

from tracker import credits as C
from tracker import four_accounts as F

CREDITS = C.load_credits()
OPUS_IN, OPUS_OUT = C.rates("opus", CREDITS)

#: A rates file in the shape tools/model_rates.py writes, with Opus 5.5 fitted well away
#: from its list ratio so the pin is visible.
RATES_RAW = {
    "_meta": {"generated_at": "2026-10-13T00:00:00+00:00"},
    "interval_percentiles": [10, 90],
    "measured_rates": {
        "anchor": {"family": "opus", "input": OPUS_IN, "output": OPUS_OUT},
        "pooled_fit": {"times_opus": {"opus": 1.0, "opus-5-5": 0.55, "sonnet-5-5": 0.5},
                       "cache_read_weight": 0.01, "output_multiplier": 5},
        "per_family": {
            "opus-5-5": {"input": 0.55 * OPUS_IN, "rate_source": "measured", "times_opus": 0.55,
                         "times_opus_interval": [0.45, 0.65], "provisional": False},
            "sonnet-5-5": {"input": 0.5 * OPUS_IN, "rate_source": "measured", "times_opus": 0.5,
                           "times_opus_interval": [0.4, 0.6], "provisional": False},
        },
    },
}
RATES = C.load_model_rates(raw=RATES_RAW)


class PinnedAtListTest(unittest.TestCase):
    def test_only_opus_5_5_is_pinned_and_its_list_ratio_is_0_8(self):
        self.assertEqual(C.PINNED_AT_LIST, ("opus-5-5",))
        self.assertAlmostEqual(C.list_price_ratio("opus-5-5", CREDITS), 0.8)

    def test_opus_5_5_is_valued_at_its_list_ratio_whatever_the_fit_says(self):
        self.assertAlmostEqual(C.pooled_fit_prices(RATES)["times_opus"]["opus-5-5"], 0.55)
        inp, out = C.comparison_rate("opus-5-5", CREDITS, RATES)
        self.assertAlmostEqual(inp, 0.8 * OPUS_IN)
        self.assertAlmostEqual(out, 0.8 * OPUS_OUT)
        self.assertAlmostEqual(out / inp, 5.0)
        # With no rates file at all, the same rate.
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", CREDITS, {})[0], 0.8 * OPUS_IN)

    def test_a_rate_absorbed_at_first_use_does_not_override_the_pin(self):
        absorbed = copy.deepcopy(RATES)
        absorbed["per_family"]["opus-5-5"] = {"input": 0.6 * OPUS_IN, "output_multiplier": 5,
                                              "rate_source": "joint_fit_at_first_use"}
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", CREDITS, absorbed)[0], 0.8 * OPUS_IN)

    def test_every_other_family_keeps_its_fitted_rate(self):
        self.assertAlmostEqual(C.comparison_rate("sonnet-5-5", CREDITS, RATES)[0], 0.5 * OPUS_IN)
        self.assertAlmostEqual(C.comparison_rate("opus", CREDITS, RATES)[0], OPUS_IN)
        self.assertIsNone(C.pinned_rate("sonnet-5-5", CREDITS))

    def test_comparison_value_prices_opus_5_5_tokens_at_list_and_cache_reads_at_the_fit(self):
        value = C.comparison_value(CREDITS, RATES)
        tok = {"input": 100, "cache_write": 50, "output": 10, "cache_read": 1000}
        self.assertAlmostEqual(value({"claude-opus-5-5": tok}),
                               (150 + 50) * 0.8 * OPUS_IN + 1000 * 0.01 * OPUS_IN)

    def test_the_published_per_model_rate_is_still_the_fit(self):
        self.assertAlmostEqual(C.family_rate("opus-5-5", CREDITS, RATES).input, 0.55 * OPUS_IN)

    def test_a_pinned_family_without_a_list_price_falls_back_to_its_fit(self):
        credits = copy.deepcopy(CREDITS)
        credits.setdefault("list_price_model", {})["opus-5-5"] = "claude-not-in-the-price-table"
        self.assertIsNone(C.pinned_rate("opus-5-5", credits))
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", credits, RATES)[0], 0.55 * OPUS_IN)


class DetectorSkipsPinnedTest(unittest.TestCase):
    def test_rate_intervals_leaves_the_pinned_family_out(self):
        fams = F.rate_intervals(RATES_RAW)["families"]
        self.assertNotIn("opus-5-5", fams)
        self.assertEqual(fams["sonnet-5-5"], {"times_opus": 0.5, "interval": [0.4, 0.6],
                                              "provisional": False})

    def test_the_valuation_block_states_the_pin(self):
        block = F.valuation_block(RATES_RAW, CREDITS)
        self.assertEqual(block["pinned_at_list"], {"opus-5-5": 0.8})
        self.assertEqual(block["times_opus"]["opus-5-5"], 0.8)
        self.assertEqual(block["times_opus"]["sonnet-5-5"], 0.5)
        self.assertNotIn("opus-5-5", block["rate_intervals"]["families"])
        self.assertIn("Opus 5.5", block["rule"])

    def test_the_detector_valuation_reads_the_pin(self):
        value = F.valuation(CREDITS, RATES)
        self.assertAlmostEqual(value({"claude-opus-5-5": {"input": 100}}), 100 * 0.8 * OPUS_IN)
        parts = F.family_parts(CREDITS, RATES)({"claude-opus-5-5": {"input": 100}})
        self.assertAlmostEqual(parts["opus-5-5"], 100 * 0.8 * OPUS_IN)


if __name__ == "__main__":
    unittest.main()
