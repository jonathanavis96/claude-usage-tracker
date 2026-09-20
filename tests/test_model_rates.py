import random
import unittest

from tools.model_rates import (DOMINANCE, MIN_N, SHELLAC, fit, prepare, ratio_bootstrap,
                               section1, section2, single_model)

PRE, POST = "2026-09-10T00:00:00+00:00", "2026-09-16T00:00:00+00:00"


def _tokens(**families) -> dict:
    """A token block per model id, from {model_family: (input, output)}."""
    ids = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5",
           "fable": "claude-fable-5-1"}
    return {ids[f]: {"input": i, "output": o, "cache_read": 10_000, "cache_write": 0}
            for f, (i, o) in families.items()}


def _kept(day: str, delta: float, tokens: dict, **fields) -> tuple[dict, float, dict]:
    s = {"start": day, "end": day, "delta_pct": delta, "tokens": tokens, "reset_verified": True}
    s.update(fields)
    return s, delta, tokens


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


if __name__ == "__main__":
    unittest.main()
