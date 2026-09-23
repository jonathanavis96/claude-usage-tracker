import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.cache_read_profile import (
    GRID,
    PRICES,
    best_weight,
    consistency,
    matrices,
    pooled,
    profile,
    sensitivity,
)
from tools.model_rates import prepare

PRE = "2026-09-10T00:00:00+00:00"
#: Percent of meter per million input-equivalent tokens, per family. Only the ratio matters to
#: the weight, which is what the tests check.
OPUS, SONNET = 1.0, 0.6


def _stretches(weight: float, n: int = 24, seed: int = 1, noise: float = 0.0) -> list[dict]:
    """Stretches whose meter movement is priced at `weight` on cache reads, Opus input rate.

    The mix varies from stretch to stretch so the weight is identified: cache reads are not a
    fixed multiple of either family's input-equivalent tokens.
    """
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        o_in, o_out = rng.randint(200_000, 2_000_000), rng.randint(20_000, 200_000)
        s_in, s_out = rng.randint(0, 1_500_000), rng.randint(0, 150_000)
        o_reads, s_reads = rng.randint(10_000_000, 80_000_000), rng.randint(0, 60_000_000)
        ie_opus = o_in + 5 * o_out
        ie_sonnet = s_in + 5 * s_out
        delta = (OPUS * (ie_opus + weight * (o_reads + s_reads)) + SONNET * ie_sonnet) / 1e6
        delta *= 1 + noise * rng.uniform(-1, 1)
        out.append({"start": PRE, "end": PRE, "delta_pct": delta, "reset_verified": True,
                    "capture_status": "accepted", "windows": 1,
                    "tokens": {"claude-opus-5": {"input": o_in, "output": o_out,
                                                 "cache_read": o_reads, "cache_write": 0},
                               "claude-sonnet-5": {"input": s_in, "output": s_out,
                                                   "cache_read": s_reads, "cache_write": 0}}})
    return out


def _group(weight: float, **kw) -> list[dict]:
    return [r for r in prepare("jwork", _stretches(weight, **kw)) if r["ok"]]


class ProfileTests(unittest.TestCase):
    def test_the_profile_minimum_lands_on_the_true_weight(self):
        prof = profile({"synthetic": _group(0.015, noise=0.02)})["synthetic"]
        at = {g["weight"]: g["rss"] for g in prof["grid"]}
        self.assertEqual(min(at, key=lambda w: at[w]), 0.015)
        self.assertAlmostEqual(prof["best"]["weight"], 0.015, delta=0.002)
        self.assertEqual(sorted(at), list(GRID))

    def test_without_noise_the_best_weight_and_rates_are_exact(self):
        prof = profile({"synthetic": _group(0.015)})["synthetic"]
        self.assertAlmostEqual(prof["best"]["weight"], 0.015, places=5)
        self.assertLess(prof["best"]["rss"], 1e-9)
        # Opus is fixed at 10/15 to set the scale; Sonnet comes back at its true share of it.
        rates = prof["best"]["rates"]
        self.assertAlmostEqual(rates["opus"], 10 / 15)
        self.assertAlmostEqual(rates["sonnet"] / rates["opus"], SONNET / OPUS, places=4)

    def test_a_weight_of_zero_is_found_on_the_bound(self):
        X, y, reads, _ = matrices(_group(0.0))
        self.assertEqual(best_weight([(X, y, reads)]), 0.0)


class ConsistencyTests(unittest.TestCase):
    def test_a_fit_at_zero_contradicts_one_at_0_015(self):
        G = {"zero": _group(0.0, seed=2, noise=0.02), "w": _group(0.015, seed=3, noise=0.02)}
        prof = profile(G)
        out = consistency(G, prof, seed=7, resamples=60)
        self.assertEqual(out["zero"]["against"]["w"]["verdict"], "contradicts")
        self.assertEqual(out["w"]["against"]["zero"]["verdict"], "contradicts")
        self.assertGreater(out["zero"]["against"]["w"]["rss_increase"], 0)

    def test_two_fits_at_the_same_weight_cannot_be_distinguished(self):
        G = {"a": _group(0.015, seed=4, noise=0.05), "b": _group(0.015, seed=5, noise=0.05)}
        out = consistency(G, profile(G), seed=7, resamples=60)
        self.assertEqual(out["a"]["against"]["b"]["verdict"], "cannot distinguish")
        self.assertEqual(out["b"]["against"]["a"]["verdict"], "cannot distinguish")


class PooledTests(unittest.TestCase):
    def test_one_shared_weight_is_recovered_with_its_interval_around_it(self):
        G = {f"g{i}": _group(0.015, seed=10 + i, noise=0.02) for i in range(3)}
        out = pooled(G, profile(G), seed=7, resamples=40)
        self.assertAlmostEqual(out["weight"], 0.015, delta=0.001)
        # An 80% interval on noisy data need not hold the truth exactly; it must hold the
        # estimate and land within the noise of the truth.
        lo, hi = out["interval"]
        self.assertLessEqual(lo, out["weight"])
        self.assertGreaterEqual(hi, out["weight"])
        self.assertLess(lo, 0.016)
        self.assertGreater(hi, 0.014)
        self.assertEqual(set(out["per_fit"]), set(G))

    def test_the_pooled_weight_sits_between_fits_that_disagree(self):
        G = {"zero": _group(0.0, seed=2, noise=0.02), "w": _group(0.02, seed=3, noise=0.02)}
        w = pooled(G, profile(G), seed=7, resamples=10)["weight"]
        self.assertGreater(w, 0.0)
        self.assertLess(w, 0.02)

    def test_variance_weighting_leans_to_the_fit_that_scatters_less(self):
        G = {"noisy zero": _group(0.0, seed=2, noise=0.2), "clean": _group(0.02, seed=3, noise=0.01)}
        out = pooled(G, profile(G), seed=7, resamples=10)
        self.assertGreater(out["variance_weighted"]["weight"], out["weight"])
        self.assertAlmostEqual(out["variance_weighted"]["weight"], 0.02, delta=0.002)


class SensitivityTests(unittest.TestCase):
    def test_the_committed_price_table_is_never_written(self):
        before = PRICES.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            runs = sensitivity({"w=0": 0.0, "w=0.02": 0.02},
                               datetime(2026, 9, 23, 12, tzinfo=timezone.utc), Path(tmp))
            self.assertTrue(all(Path(r["file"]).exists() for r in runs.values()))
        self.assertEqual(PRICES.read_bytes(), before)
        # A dearer cache read makes a percent of the window worth more credits, never fewer.
        self.assertGreater(runs["w=0.02"]["figures"]["window_credits"],
                           runs["w=0"]["figures"]["window_credits"])
        self.assertTrue(all(v in (0.0, None) for v in runs["w=0"]["change_from_w=0"].values()))


if __name__ == "__main__":
    unittest.main()
