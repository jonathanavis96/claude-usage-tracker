import random
import unittest

from tools.model_rates import (DOMINANCE, MIN_N, SHELLAC, adopt, agree, fit, fit_bootstrap,
                               measured_rates, prepare, ratio_bootstrap, section1, section2,
                               section3, section4, single_model)

PRE, POST = "2026-09-10T00:00:00+00:00", "2026-09-16T00:00:00+00:00"


def _tokens(reads: int = 10_000, **families) -> dict:
    """A token block per model id, from {model_family: (input, output)}, `reads` cache reads each."""
    ids = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5",
           "fable": "claude-fable-5-1"}
    return {ids[f]: {"input": i, "output": o, "cache_read": reads, "cache_write": 0}
            for f, (i, o) in families.items()}


def _kept(day: str, delta: float, tokens: dict, **fields) -> dict:
    """One stretch in the shape tracker.credits.clean_stretches hands back."""
    s = {"start": day, "end": day, "delta_pct": delta, "tokens": tokens, "reset_verified": True,
         "capture_status": "accepted", "windows": 1}
    s.update(fields)
    return s


def _recs(rows, account="jwork"):
    return prepare(account, rows)


class SingleModelTests(unittest.TestCase):
    def test_a_stretch_one_model_carries_prices_that_model_alone(self):
        # 950 of 1000 raw tokens are Opus, which is the 95% threshold exactly.
        recs = _recs([_kept(PRE, 10.0, _tokens(opus=(950, 0), sonnet=(50, 0)))])
        self.assertEqual(recs[0]["share"]["opus"], 0.95)
        self.assertEqual(list(single_model(recs, 5)), ["opus"])

    def test_a_mixed_stretch_prices_no_model_alone(self):
        recs = _recs([_kept(PRE, 10.0, _tokens(opus=(900, 0), sonnet=(100, 0)))])
        self.assertLess(recs[0]["share"]["opus"], DOMINANCE)
        self.assertEqual(single_model(recs, 5), {})

    def test_cache_reads_count_nothing_and_output_counts_five_times_input(self):
        recs = _recs([_kept(PRE, 10.0, _tokens(opus=(100, 10)))])
        self.assertEqual(recs[0]["ie"][5]["opus"], 150)
        self.assertEqual(recs[0]["ie"][3]["opus"], 130)
        self.assertEqual(single_model(recs, 5)["opus"], [15.0])


class RatioTests(unittest.TestCase):
    """Two clusters whose tokens per 1% differ by a known factor recover that factor."""

    def _clusters(self, opus_per_pct, sonnet_per_pct, n_sonnet=4):
        rows = [_kept(PRE, 10.0, _tokens(opus=(opus_per_pct * 10 + i, 0))) for i in range(4)]
        rows += [_kept(PRE, 10.0, _tokens(sonnet=(sonnet_per_pct * 10 + i, 0))) for i in range(n_sonnet)]
        return _recs(rows)

    def test_a_known_token_ratio_is_the_rate_ratio_the_other_way_up(self):
        # Sonnet buys 500k tokens per 1% where Opus buys 300k, so the meter charges Opus
        # 500/300 = 1.667 times what it charges Sonnet: Shellac's ratio exactly.
        recs = self._clusters(300_000, 500_000)
        out = section2({"jwork": recs}, 1, 200)["jwork/pre/opus:sonnet"]["out5x"]
        self.assertTrue(out["measurable"])
        self.assertAlmostEqual(out["ratio"], 5 / 3, places=3)
        self.assertAlmostEqual(out["prior"], SHELLAC["opus"] / SHELLAC["sonnet"], places=6)
        self.assertEqual(out["verdict"], "agrees")
        self.assertAlmostEqual(out["row_move"], 0.0, places=3)

    def test_a_ratio_away_from_shellac_reads_as_a_disagreement_and_moves_the_row(self):
        # Sonnet buying only 360k per 1% means a ratio of 1.2, 28% below Shellac's 1.667,
        # and the Sonnet tokens-per-window row shrinks by the same 28%.
        recs = self._clusters(300_000, 360_000)
        out = section2({"jwork": recs}, 1, 200)["jwork/pre/opus:sonnet"]["out5x"]
        self.assertAlmostEqual(out["ratio"], 1.2, places=3)
        self.assertEqual(out["verdict"], "disagrees")
        self.assertAlmostEqual(out["row_move"], 1.2 / (5 / 3) - 1, places=3)

    def test_the_bootstrap_interval_brackets_the_ratio(self):
        recs = self._clusters(300_000, 500_000)
        opus = single_model([r for r in recs if r["dominant"] == "opus"], 5)["opus"]
        sonnet = single_model([r for r in recs if r["dominant"] == "sonnet"], 5)["sonnet"]
        r = ratio_bootstrap(sonnet, opus, random.Random(7), 200)
        self.assertLessEqual(r["interval"][0], r["ratio"])
        self.assertLessEqual(r["ratio"], r["interval"][1])


class RefusalTests(unittest.TestCase):
    def test_two_stretches_a_side_are_not_measurable_and_carry_no_number(self):
        rows = [_kept(PRE, 10.0, _tokens(opus=(3_000_000 + i, 0))) for i in range(4)]
        rows += [_kept(PRE, 10.0, _tokens(sonnet=(5_000_000 + i, 0))) for i in range(MIN_N - 1)]
        out = section2({"jwork": _recs(rows)}, 1, 200)["jwork/pre/opus:sonnet"]["out5x"]
        self.assertFalse(out["measurable"])
        self.assertNotIn("ratio", out)
        self.assertEqual((out["n_num"], out["n_den"]), (4, MIN_N - 1))

    def test_section_one_marks_a_thin_group_rather_than_dropping_it(self):
        rows = [_kept(PRE, 10.0, _tokens(opus=(1_000_000, 0))) for _ in range(MIN_N - 1)]
        row = section1({"jwork": _recs(rows)})["jwork/pre"]["opus"]["out5x"]
        self.assertEqual(row["n"], MIN_N - 1)
        self.assertFalse(row["measurable"])


class MixedFitTests(unittest.TestCase):
    """A fit over stretches built from known rates recovers those rates."""

    WINDOW = 200_000.0   # credits per 1%
    RATES = {"opus": 10 / 15, "sonnet": 6 / 15, "fable": 25 / 15}

    def _rows(self):
        mixes = [(9, 1, 0), (1, 9, 0), (0, 1, 9), (5, 4, 1), (2, 2, 6), (7, 2, 1), (3, 6, 1), (4, 1, 5),
                 (6, 3, 1), (1, 1, 8)]
        rows = []
        for o, s, f in mixes:
            tokens = _tokens(opus=(o * 1_000_000, 0), sonnet=(s * 1_000_000, 0), fable=(f * 1_000_000, 0))
            credits = sum(n * 1_000_000 * self.RATES[k] for n, k in ((o, "opus"), (s, "sonnet"), (f, "fable")))
            rows.append(_kept(PRE, credits / self.WINDOW, tokens))
        return _recs(rows)

    def test_the_fit_recovers_the_rates_and_the_window_that_built_the_stretches(self):
        f = fit(self._rows(), 5)
        self.assertEqual(f["n"], 10)
        for k, v in self.RATES.items():
            self.assertAlmostEqual(f["rates"][k], v, places=6, msg=k)
        self.assertAlmostEqual(f["window_credits_per_pct"], self.WINDOW, places=3)
        self.assertLess(f["residual_median_abs_rel"], 1e-9)

    def test_a_model_present_in_too_few_stretches_is_left_out_of_the_fit(self):
        rows = self._rows()
        rows.append(_recs([_kept(PRE, 10.0, _tokens(opus=(1_000_000, 0), haiku=(1_000, 0)))])[0])
        f = fit(rows, 5)
        self.assertNotIn("haiku", f["families"])

    def test_too_few_stretches_refuse_the_fit(self):
        self.assertIsNone(fit(self._rows()[:4], 5))


class QuantisationFloorTests(unittest.TestCase):
    """section4's whole-percent floor: `windows` pieces per stretch, not a flat 0.5."""

    def test_a_stretch_of_five_windows_and_delta_20_gives_a_quarter_point_floor(self):
        rows = [_kept(POST, 20.0, _tokens(opus=(1_000_000, 0)), windows=5) for _ in range(MIN_N)]
        dave_rows = [_kept(POST, 10.0, _tokens(opus=(1_000_000, 0)), windows=1)]
        s4 = section4({"jwork": _recs(rows), "dave": _recs(dave_rows, account="dave")}, {}, 1, 10)
        self.assertAlmostEqual(s4["quantisation"]["jwork/post"]["per_stretch"], 0.25, places=9)

    def test_a_stretch_missing_the_windows_field_is_not_silently_treated_as_one(self):
        # prepare() requires the field rather than defaulting it: a stretch file that omits
        # `windows` is missing data the floor depends on, and defaulting to 1 would understate
        # the floor for any stretch actually pooled from more than one window.
        s = _kept(PRE, 20.0, _tokens(opus=(1_000_000, 0)))
        del s["windows"]
        with self.assertRaises(KeyError):
            prepare("jwork", [s])


class JointCacheReadFitTests(unittest.TestCase):
    """Stretches built at a known cache-read weight, and what each fit makes of them.

    Every stretch's meter movement is exactly
    `(sum of input-equivalent tokens x rate + cache reads x WEIGHT x opus rate) / window`,
    so the weight is in the data and the joint fit has to find it. The point of the second
    test is the reason the joint fit exists at all: the cache-read charge does not vanish when
    a fit holds cache reads at zero, it lands on whichever rate carries the cache-read-heavy
    stretches.
    """

    WINDOW = 200_000.0                  # credits per 1%
    RATES = {"opus": 10 / 15, "sonnet": 6 / 15, "fable": 25 / 15}
    WEIGHT = 0.01                       # of the Opus input rate
    #: Opus, Sonnet and Fable tokens in millions, and cache reads in millions. The
    #: cache-read column is heaviest where Sonnet is, which is what these accounts look like
    #: (sub-agent traffic) and what lets a zero-weight fit hide the charge in Sonnet's rate.
    MIXES = ((9, 1, 0, 20), (1, 9, 0, 180), (0, 1, 9, 30), (5, 4, 1, 90), (2, 2, 6, 40),
             (7, 2, 1, 40), (3, 6, 1, 120), (4, 1, 5, 25), (6, 3, 1, 60), (1, 1, 8, 20))

    def _rows(self):
        rows = []
        for o, s, f, reads in self.MIXES:
            per = {k: n * 1_000_000 for k, n in (("opus", o), ("sonnet", s), ("fable", f))}
            tokens = _tokens(reads=0, opus=(per["opus"], 0), sonnet=(per["sonnet"], 0),
                             fable=(per["fable"], 0))
            # Every cache read on the Sonnet bundle, which is what a sub-agent-heavy stretch
            # looks like; the fit sees one cache-read column either way.
            tokens["claude-sonnet-5"]["cache_read"] = reads * 1_000_000
            credits = sum(per[k] * self.RATES[k] for k in per)
            credits += reads * 1_000_000 * self.WEIGHT * self.RATES["opus"]
            rows.append(_kept(PRE, credits / self.WINDOW, tokens))
        return _recs(rows)

    def test_the_joint_fit_recovers_the_rates_and_the_cache_read_weight(self):
        f = fit(self._rows(), 5, joint=True)
        self.assertAlmostEqual(f["cache_read_weight"], self.WEIGHT, places=6)
        self.assertAlmostEqual(f["cache_read_rate"], self.WEIGHT * self.RATES["opus"], places=6)
        for k, v in self.RATES.items():
            self.assertAlmostEqual(f["rates"][k], v, places=6, msg=k)
        self.assertAlmostEqual(f["window_credits_per_pct"], self.WINDOW, places=3)
        self.assertLess(f["residual_median_abs_rel"], 1e-9)

    def test_holding_cache_reads_at_zero_loads_the_charge_onto_the_other_rates(self):
        rows = self._rows()
        held = fit(rows, 5)
        self.assertEqual(held["cache_read_weight"], 0.0)
        # The charge has to go somewhere: the fit that cannot see cache reads misses the
        # Sonnet rate it was built with, and the joint fit on the same stretches does not.
        self.assertGreater(abs(held["rates"]["sonnet"] / self.RATES["sonnet"] - 1), 0.1)
        self.assertGreater(held["residual_median_abs_rel"], 1e-6)

    def test_the_bootstrap_brackets_the_weight_it_was_built_with(self):
        f = fit_bootstrap(self._rows(), 5, random.Random(11), 120, joint=True)
        lo, hi = f["cache_read_weight_interval"]
        self.assertLessEqual(lo, self.WEIGHT)
        self.assertLessEqual(self.WEIGHT, hi)

    def test_a_group_with_no_cache_reads_has_no_joint_fit_rather_than_a_weight_of_zero(self):
        rows = [_kept(PRE, 10.0 + i, _tokens(reads=0, opus=((i + 1) * 1_000_000, 0),
                                             sonnet=((10 - i) * 1_000_000, 0)))
                for i in range(10)]
        self.assertIsNone(fit(_recs(rows), 5, joint=True))
        self.assertIsNotNone(fit(_recs(rows), 5))


class PoolingTests(unittest.TestCase):
    """What the publisher adopts: pooled where the fits agree, a sentence where they do not."""

    def _s3(self, pre_sonnet, post_sonnet, spread=0.02):
        """Two fitted groups, each with a Sonnet rate and an interval of +-`spread` around it."""
        def group(rate):
            return {"n": 20, "rates": {"opus": 10 / 15, "sonnet": rate},
                    "interval": {"opus": [10 / 15, 10 / 15],
                                 "sonnet": [rate - spread, rate + spread]},
                    "cache_read_weight": 0.01, "cache_read_weight_interval": [0.005, 0.015],
                    "window_credits_per_pct": 200_000.0, "window_interval": [190_000.0, 210_000.0],
                    "residual_median_abs_rel": 0.05, "residual_p90_abs_rel": 0.1, "ratios": {}}
        return {"jwork/pre/out5x": {**group(pre_sonnet), "joint": group(pre_sonnet)},
                "jwork/post/out5x": {**group(post_sonnet), "joint": group(post_sonnet)}}

    def test_overlapping_intervals_agree_and_disjoint_ones_do_not(self):
        self.assertTrue(agree([[0.4, 0.6], [0.5, 0.8]]))
        self.assertFalse(agree([[0.4, 0.6], [0.7, 0.9]]))
        self.assertFalse(agree([]))

    def test_fits_that_agree_pool_to_the_median_with_the_union_of_their_intervals(self):
        row = adopt(self._s3(0.50, 0.54))["sonnet"]
        self.assertTrue(row["agree"])
        self.assertAlmostEqual(row["measured"], 0.52)
        self.assertEqual(row["interval"], [0.48, 0.56])
        self.assertEqual(sorted(row["per_fit"]), ["jwork/post", "jwork/pre"])

    def test_fits_that_do_not_agree_publish_the_interval_and_no_value(self):
        row = adopt(self._s3(0.50, 0.90))["sonnet"]
        self.assertFalse(row["agree"])
        self.assertIsNone(row["measured"])
        self.assertEqual(row["interval"], [0.48, 0.92])

    def test_a_family_no_fit_carries_is_not_measurable_and_a_disagreeing_one_is_not_identified(self):
        s1 = {"jwork/pre": {"max_share": {"opus": 1.0, "sonnet": 0.7, "haiku": 0.0, "fable": 0.8}}}
        mr = measured_rates(s1, self._s3(0.50, 0.90))
        self.assertEqual(mr["per_family"]["haiku"]["status"],
                         "not measurable, no clean stretch is Haiku-heavy")
        self.assertIsNone(mr["per_family"]["haiku"]["interval"])
        self.assertEqual(mr["per_family"]["sonnet"]["status"], "rate not yet identified")
        self.assertIsNone(mr["per_family"]["sonnet"]["input"])
        self.assertEqual(mr["per_family"]["sonnet"]["interval"], [0.48, 0.92])

    def test_the_opus_row_is_the_anchor_and_says_it_came_from_the_reference(self):
        mr = measured_rates({}, self._s3(0.50, 0.54))
        opus = mr["per_family"]["opus"]
        self.assertEqual(opus["input"], SHELLAC["opus"])
        self.assertEqual(opus["rate_source"], "reference")
        self.assertTrue(opus["anchor"])
        self.assertIsNone(opus["interval"])
        self.assertEqual(mr["per_family"]["sonnet"]["rate_source"], "measured")

    def test_the_cache_read_weight_is_pooled_the_same_way(self):
        w = measured_rates({}, self._s3(0.50, 0.54))["cache_read_weight"]
        self.assertTrue(w["agree"])
        self.assertAlmostEqual(w["value"], 0.01)
        self.assertEqual(w["interval"], [0.005, 0.015])
        self.assertEqual(w["reference"], 0.0)

    def test_the_section_three_shape_carries_both_fits(self):
        rows = JointCacheReadFitTests()._rows()
        out = section3({"jwork": rows}, 1, 30)["jwork/pre/out5x"]
        self.assertEqual(out["cache_read_weight"], 0.0)
        self.assertAlmostEqual(out["joint"]["cache_read_weight"],
                               JointCacheReadFitTests.WEIGHT, places=6)


if __name__ == "__main__":
    unittest.main()
