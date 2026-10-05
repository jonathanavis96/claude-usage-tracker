"""tracker/five_hour_weight.py: the headless factor fitted from the seven-day steps, and the
interactive-equivalent stretches every five-hour figure reads."""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from tracker import five_hour_weight as W

T0 = datetime(2026, 9, 15, tzinfo=timezone.utc)
CUT = datetime(2026, 9, 22, tzinfo=timezone.utc)
LABELS = {"one": "a1", "two": "a2"}


def value(tokens: dict) -> float:
    return float(sum(t.get("v", 0) for t in tokens.values()))


def step(i: int, share: float, d5: float, *, split: bool = True, regime_shift=timedelta(0)) -> dict:
    start = T0 + regime_shift + timedelta(hours=i)
    st = {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(),
          "d7": 1, "d5": d5, "tokens": {"m": {"v": 100.0}}}
    if split:
        st["headless_tokens"] = {"m": {"v": 100.0 * share}} if share else {}
    return st


def exact_steps(factor: float, account_level: float = 1.0, n: int = 60) -> list[dict]:
    """Steps whose five-hour points follow the model exactly: 4 x account x factor**share,
    half before CUT and half after it at a level 1.2x higher."""
    out = []
    for i in range(n):
        share = (i % 6) / 5
        after = i >= n // 2
        d5 = 4 * account_level * (1.2 if after else 1.0) * factor ** share
        out.append(step(i, share, d5, regime_shift=timedelta(days=10) if after else timedelta(0)))
    return out


class QuasiPoissonTest(unittest.TestCase):
    def test_recovers_an_exact_log_linear_model(self):
        xs = [[1.0, s / 10] for s in range(11)]
        y = [3 * math.exp(0.4 * x[1]) for x in xs]
        fit = W.quasi_poisson(y, xs)
        self.assertAlmostEqual(fit["coef"][0], math.log(3), places=6)
        self.assertAlmostEqual(fit["coef"][1], 0.4, places=6)
        self.assertAlmostEqual(fit["deviance"], 0.0, places=6)


class HeadlessFactorTest(unittest.TestCase):
    def test_fits_the_factor_with_regime_and_account_terms(self):
        steps = {"one": exact_steps(1.5), "two": exact_steps(1.5, account_level=1.3)}
        block = W.headless_factor(steps, value, LABELS, [CUT])
        self.assertAlmostEqual(block["headless_factor"]["value"], 1.5, places=3)
        lo, hi = block["headless_factor"]["interval"]
        self.assertLessEqual(lo, 1.5)
        self.assertGreaterEqual(hi, 1.5)
        self.assertEqual(block["n_steps"], 120)
        self.assertEqual(block["accounts"], ["a1", "a2"])
        self.assertEqual(block["basis"], "interactive_equivalent")
        for label in ("a1", "a2"):
            self.assertAlmostEqual(block["per_account"][label]["value"], 1.5, places=3)
            self.assertEqual(block["per_account"][label]["mean_headless_share"], 0.5)

    def test_measured_not_fixed(self):
        steps = {"one": exact_steps(1.2), "two": exact_steps(1.2, account_level=0.8)}
        self.assertAlmostEqual(W.factor_of(W.headless_factor(steps, value, LABELS, [CUT])), 1.2, places=3)

    def test_an_account_without_a_split_is_named_and_left_out(self):
        steps = {"one": exact_steps(1.5),
                 "two": [step(i, 0.0, 4.0, split=False) for i in range(30)]}
        block = W.headless_factor(steps, value, LABELS, [CUT])
        self.assertEqual(block["accounts"], ["a1"])
        self.assertEqual(block["accounts_without_split"], ["a2"])
        self.assertEqual(block["n_steps"], 60)

    def test_too_few_steps_publish_no_factor_and_say_why(self):
        block = W.headless_factor({"one": exact_steps(1.5, n=20)}, value, LABELS, [CUT])
        self.assertIsNone(W.factor_of(block))
        self.assertIn("not measured", block["headless_factor"]["status"])

    def test_an_account_whose_share_hardly_varies_has_no_own_factor(self):
        flat = [step(i, 0.0, 4.0) for i in range(30)]
        block = W.headless_factor({"one": exact_steps(1.5), "two": flat}, value, LABELS, [CUT])
        self.assertIsNone(block["per_account"]["a2"]["value"])
        self.assertIn("not fitted alone", block["per_account"]["a2"]["status"])

    def test_a_step_across_a_regime_boundary_is_left_out(self):
        across = step(0, 0.5, 4.0)
        across["start"] = (CUT - timedelta(minutes=10)).isoformat()
        across["end"] = (CUT + timedelta(minutes=10)).isoformat()
        block = W.headless_factor({"one": exact_steps(1.5) + [across]}, value, LABELS, [CUT])
        self.assertEqual(block["n_steps"], 60)


class InteractiveEquivalentTest(unittest.TestCase):
    def report(self):
        return {"accounts": {"two": {
            "stretches": [{"start": "s", "tokens": {"m": {"input": 10, "output": 4}},
                           "headless_tokens": {"m": {"input": 6, "output": 2}}},
                          {"start": "t", "tokens": {"m": {"input": 10}}, "headless_tokens": {}}],
            "weekly_steps": [{"start": "s", "tokens": {"m": {"input": 10}},
                              "headless_tokens": {"m": {"input": 6}}}]}}}

    def test_headless_tokens_count_at_the_factor_and_steps_stay(self):
        out = W.interactive_equivalent(self.report(), 1.5)
        first, second = out["accounts"]["two"]["stretches"]
        self.assertEqual(first["tokens"], {"m": {"input": 13, "output": 5}})
        self.assertEqual(first["metered_tokens"], {"m": {"input": 10, "output": 4}})
        self.assertEqual(first["headless_weight"], 1.5)
        self.assertEqual(second["tokens"], {"m": {"input": 10}})
        self.assertEqual(out["accounts"]["two"]["weekly_steps"], self.report()["accounts"]["two"]["weekly_steps"])

    def test_the_input_report_is_not_changed(self):
        rpt = self.report()
        W.interactive_equivalent(rpt, 1.5)
        self.assertEqual(rpt, self.report())

    def test_no_factor_is_no_weight(self):
        rpt = self.report()
        self.assertIs(W.interactive_equivalent(rpt, None), rpt)


if __name__ == "__main__":
    unittest.main()
