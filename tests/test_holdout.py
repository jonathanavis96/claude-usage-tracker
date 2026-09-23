import random
import unittest
from datetime import datetime, timedelta, timezone

from tools import holdout as H
from tools.model_rates import prepare

IDS = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "fable": "claude-fable-5-1"}
# Known meter points per million input-equivalent tokens (input + 5x output), and per 1e8 cache reads.
RATES = {"opus": 0.9, "sonnet": 0.5, "fable": 1.8}
READ_RATE = 0.2
T0 = datetime(2026, 9, 15, tzinfo=timezone.utc)


def stretches(n: int, account: str, rng: random.Random, noise: float = 0.0,
              scale: float = 1.0, start: datetime = T0) -> list[dict]:
    """`n` synthetic stretches whose meter movement is the known rates applied to their tokens."""
    out = []
    for i in range(n):
        tokens, delta = {}, 0.0
        for f, rate in RATES.items():
            inp, outp = rng.randint(200_000, 3_000_000), rng.randint(20_000, 300_000)
            tokens[IDS[f]] = {"input": inp, "output": outp, "cache_read": 0, "cache_write": 0}
            delta += (inp + 5 * outp) / 1e6 * rate
        reads = rng.randint(10_000_000, 90_000_000)
        tokens[IDS["opus"]]["cache_read"] = reads
        delta += reads / 1e8 * READ_RATE
        delta *= scale * (1 + rng.uniform(-noise, noise))
        at = start + timedelta(hours=6 * i)
        out.append({"start": at.isoformat(), "end": (at + timedelta(hours=5)).isoformat(),
                    "delta_pct": delta, "tokens": tokens, "reset_verified": True,
                    "capture_status": "accepted", "windows": 1})
    return out


class HoldoutTest(unittest.TestCase):
    def test_exact_data_recovers_the_known_rates(self):
        recs = prepare("jwork", stretches(20, "jwork", random.Random(1)))
        families, b = H._raw_fit(recs, 5, True)
        got = dict(zip(families, b))
        for f, rate in RATES.items():
            self.assertAlmostEqual(got[f], rate, places=6)
        self.assertAlmostEqual(b[len(families)], READ_RATE, places=6)

    def test_exact_data_predicts_held_out_stretches_exactly(self):
        train = prepare("jwork", stretches(20, "jwork", random.Random(2)))
        test = prepare("dave", stretches(10, "dave", random.Random(3)))
        r = H.predict(train, test, random.Random(4), resamples=50)
        self.assertTrue(r["fitted"])
        self.assertEqual((r["n_train"], r["n"]), (20, 10))
        self.assertLess(r["median_abs_error_pts"], 1e-6)
        self.assertLess(r["median_abs_rel_error"], 1e-8)
        self.assertAlmostEqual(r["median_predicted_over_actual"], 1.0, places=6)

    def test_noisy_data_has_small_error_and_honest_coverage(self):
        train = prepare("jwork", stretches(40, "jwork", random.Random(5), noise=0.1))
        test = prepare("dave", stretches(60, "dave", random.Random(6), noise=0.1))
        r = H.predict(train, test, random.Random(7), resamples=300)
        self.assertLess(r["median_abs_rel_error"], 0.08)
        self.assertGreater(r["coverage"], 0.6)
        self.assertEqual(r["nominal_coverage"], 0.8)

    def test_an_account_offset_shows_as_bias_and_lost_coverage(self):
        train = prepare("jwork", stretches(40, "jwork", random.Random(8), noise=0.05))
        test = prepare("dave", stretches(30, "dave", random.Random(9), noise=0.05, scale=1.3))
        r = H.predict(train, test, random.Random(10), resamples=200)
        self.assertAlmostEqual(r["median_predicted_over_actual"], 1 / 1.3, delta=0.03)
        self.assertLess(r["coverage"], 0.2)
        self.assertEqual(r["below_interval"], 0)

    def test_too_small_a_training_set_is_not_fitted(self):
        train = prepare("jwork", stretches(5, "jwork", random.Random(11)))
        test = prepare("dave", stretches(5, "dave", random.Random(12)))
        r = H.predict(train, test, random.Random(13), resamples=10)
        self.assertFalse(r["fitted"])
        self.assertIn("training set", r["why"])

    def test_time_split_separates_at_the_split_point_and_counts_straddlers(self):
        rng = random.Random(14)
        early = stretches(3, "jwork", rng, start=datetime(2026, 9, 16, tzinfo=timezone.utc))
        late = stretches(4, "jwork", rng, start=datetime(2026, 9, 18, 1, tzinfo=timezone.utc))
        straddle = stretches(1, "jwork", rng, start=datetime(2026, 9, 17, 22, tzinfo=timezone.utc))
        pre_cut = stretches(2, "jwork", rng, start=datetime(2026, 9, 10, tzinfo=timezone.utc))
        recs = prepare("jwork", early + late + straddle + pre_cut)
        before, after, n_straddle = H.time_split(recs, H.SPLIT_AT)
        self.assertEqual((len(before), len(after), n_straddle), (3, 4, 1))

    def test_directions_cover_both_account_swaps_and_the_time_split(self):
        data = {"jwork": [], "dave": []}
        labels = [d["label"] for d in H.directions(data)]
        self.assertIn("jwork -> dave (post-cut)", labels)
        self.assertIn("dave -> jwork (post-cut)", labels)
        self.assertIn("jwork: ended by 2026-09-18 -> started after", labels)


if __name__ == "__main__":
    unittest.main()
