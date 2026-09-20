import unittest

import numpy as np

from tools.model_rates import prepare
from tools.rounding_feasibility import (FIXED_READ_WEIGHT, READS_FREE_WEIGHT, design_fixed_weight,
                                        group_rows, rounding_lp, run)

PRE, POST = "2026-09-10T00:00:00+00:00", "2026-09-16T00:00:00+00:00"


def _tokens(input_tokens: int, output_tokens: int = 0, cache_read: int = 0) -> dict:
    return {"claude-opus-5": {"input": input_tokens, "output": output_tokens,
                              "cache_read": cache_read, "cache_write": 0}}


def _stretch(day: str, delta: float, tokens: dict, windows: int = 1) -> dict:
    return {"start": day, "end": day, "delta_pct": delta, "tokens": tokens, "windows": windows,
            "reset_verified": True, "capture_status": "accepted"}


class RoundingLPTests(unittest.TestCase):
    """The linear programme itself, on hand-picked matrices with a known answer."""

    def test_an_exactly_fitting_design_needs_no_extra_slack(self):
        x = np.array([[0.1], [0.2]])
        y = np.array([10.0, 20.0])
        pieces = np.array([1.0, 1.0])
        result = rounding_lp(x, y, pieces)
        self.assertAlmostEqual(result["extra_pp"], 0.0, places=6)

    def test_two_targets_on_one_feature_need_the_gap_between_them_as_slack(self):
        # Same feature value, two different targets: one coefficient cannot separate them, so
        # the best it can do is split the difference. Midpoint 15 is 5 away from each target and
        # rounding already covers 1 of that, leaving t=4.
        x = np.array([[0.1], [0.1]])
        y = np.array([10.0, 20.0])
        pieces = np.array([1.0, 1.0])
        result = rounding_lp(x, y, pieces)
        self.assertAlmostEqual(result["extra_pp"], 4.0, places=6)


class DesignFixedWeightTests(unittest.TestCase):
    """The tool's own column-building, from prepared records."""

    def test_reads_free_drops_the_read_charge_entirely(self):
        recs = prepare("jwork", [_stretch(PRE, 10.0, _tokens(100_000, cache_read=999_999_999))])
        x, y, pieces = design_fixed_weight(recs, READS_FREE_WEIGHT)
        self.assertAlmostEqual(x[0, 0], 0.1)  # 100_000 / 1e6, unaffected by the huge read count
        self.assertEqual(list(y), [10.0])
        self.assertEqual(list(pieces), [1.0])

    def test_a_fixed_weight_adds_the_read_charge_to_the_opus_column(self):
        recs = prepare("jwork", [_stretch(PRE, 10.0, _tokens(100_000, cache_read=10_000_000))])
        x, _, _ = design_fixed_weight(recs, FIXED_READ_WEIGHT)
        expected = 100_000 / 1e6 + FIXED_READ_WEIGHT * 10_000_000 / 1e6
        self.assertAlmostEqual(x[0, 0], expected)


class RunTests(unittest.TestCase):
    """End-to-end wiring: group membership, era split and both weight profiles."""

    def test_a_tiny_synthetic_fixture_reproduces_by_hand(self):
        kept = {
            "jwork": [
                # pre: two rows an opus-only rate fits exactly -> t=0 both profiles.
                _stretch(PRE, 10.0, _tokens(100_000)),
                _stretch(PRE, 20.0, _tokens(200_000)),
                # post: same feature, two different targets -> t=4 (see RoundingLPTests above).
                _stretch(POST, 10.0, _tokens(100_000)),
                _stretch(POST, 20.0, _tokens(100_000)),
            ],
            "dave": [
                _stretch(POST, 10.0, _tokens(100_000)),
            ],
        }
        result = run(kept)
        self.assertEqual(set(result), {"jwork/pre", "jwork/post", "dave/post"})
        self.assertEqual(result["jwork/pre"]["n"], 2)
        self.assertAlmostEqual(result["jwork/pre"]["reads_free"]["extra_pp"], 0.0, places=6)
        self.assertAlmostEqual(result["jwork/pre"]["reads_fixed_0.0118"]["extra_pp"], 0.0, places=6)
        self.assertEqual(result["jwork/post"]["n"], 2)
        self.assertAlmostEqual(result["jwork/post"]["reads_free"]["extra_pp"], 4.0, places=6)
        self.assertEqual(result["dave/post"]["n"], 1)
        self.assertAlmostEqual(result["dave/post"]["reads_free"]["extra_pp"], 0.0, places=6)

    def test_group_rows_only_keeps_the_matching_era(self):
        kept = {"jwork": [_stretch(PRE, 10.0, _tokens(100_000)), _stretch(POST, 10.0, _tokens(100_000))]}
        self.assertEqual(len(group_rows("jwork", "pre", kept)), 1)
        self.assertEqual(len(group_rows("jwork", "post", kept)), 1)


if __name__ == "__main__":
    unittest.main()
