import unittest

from tracker import credits as C
from tools.masterrig_fable import (capture_status_counts, drop_surplus, envelope, fable_rows,
                                   feasibility_reads_free, gated_rows, joint_fit, joint_fit_b5,
                                   since_cut)

POST = "2026-09-10T00:00:00+00:00"


def _tokens(**by_model: dict) -> dict:
    return by_model


def _opus(input_tokens: int, output_tokens: int = 0, cache_read: int = 0) -> dict:
    return {"input": input_tokens, "output": output_tokens, "cache_read": cache_read,
            "cache_write": 0}


def _fable(input_tokens: int, output_tokens: int = 0) -> dict:
    return {"input": input_tokens, "output": output_tokens, "cache_read": 0, "cache_write": 0}


def _stretch(day: str, delta: float, tokens: dict, windows: int = 1,
            capture_status: str = "accepted") -> dict:
    return {"start": day, "end": day, "delta_pct": delta, "tokens": tokens, "windows": windows,
            "capture_status": capture_status}


class SinceCutAndStatusTests(unittest.TestCase):
    def test_a_stretch_before_the_cut_is_dropped(self):
        rows = since_cut([_stretch("2026-09-05T00:00:00+00:00", 10.0, _tokens(**{"claude-opus-5": _opus(1)}))])
        self.assertEqual(rows, [])

    def test_capture_status_counts_every_status_seen(self):
        rows = [
            _stretch(POST, 10.0, _tokens(**{"claude-opus-5": _opus(1)}), capture_status="accepted"),
            _stretch(POST, 10.0, _tokens(**{"claude-opus-5": _opus(1)}), capture_status="unpriced"),
            _stretch(POST, 10.0, _tokens(**{"claude-opus-5": _opus(1)}), capture_status="unpriced"),
        ]
        self.assertEqual(capture_status_counts(rows), {"accepted": 1, "unpriced": 2})

    def test_gated_rows_drops_small_delta_and_keeps_every_status(self):
        rows = [
            _stretch(POST, 9.9, _tokens(**{"claude-opus-5": _opus(1)}), capture_status="unpriced"),
            _stretch(POST, 10.0, _tokens(**{"claude-opus-5": _opus(1)}), capture_status="surplus"),
        ]
        kept = gated_rows(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["capture_status"], "surplus")

    def test_drop_surplus_removes_only_that_status(self):
        rows = [
            _stretch(POST, 10.0, _tokens(**{"claude-opus-5": _opus(1)}), capture_status="unpriced"),
            _stretch(POST, 10.0, _tokens(**{"claude-opus-5": _opus(1)}), capture_status="surplus"),
        ]
        kept = drop_surplus(rows)
        self.assertEqual([r["capture_status"] for r in kept], ["unpriced"])


class JointFitTests(unittest.TestCase):
    """A tiny synthetic fixture an Opus-only rate fits exactly, plus one Fable-heavy row."""

    def setUp(self):
        self.credits = C.load_credits()
        # Two Opus-only rows an exact rate of 1e-4 pp per raw token fits: 100_000 -> 10.0,
        # 200_000 -> 20.0. Reads and Sonnet are zero everywhere, so their coefficients pin to 0.
        self.rows = [
            _stretch(POST, 10.0, _tokens(**{"claude-opus-5": _opus(100_000)})),
            _stretch(POST, 20.0, _tokens(**{"claude-opus-5": _opus(200_000)})),
        ]

    def test_an_exact_opus_only_design_recovers_the_known_rate(self):
        fit = joint_fit(self.rows, self.credits, resamples=20)
        self.assertEqual(fit["n"], 2)
        self.assertAlmostEqual(fit["coef"]["opus47"], 0.0, places=8)
        self.assertAlmostEqual(fit["coef"]["opus"], 1e-4, places=8)
        self.assertAlmostEqual(fit["coef"]["sonnet"], 0.0, places=8)
        self.assertAlmostEqual(fit["coef"]["fable"], 0.0, places=8)
        self.assertAlmostEqual(fit["coef_reads"], 0.0, places=8)
        self.assertEqual(fit["fable_opus_ratio"], 0.0)

    def test_claude_opus_4_7_gets_its_own_column_not_the_priced_opus_one(self):
        rows = [
            _stretch(POST, 10.0, _tokens(**{"claude-opus-4-7": _opus(100_000)})),
            _stretch(POST, 20.0, _tokens(**{"claude-opus-4-7": _opus(200_000)})),
        ]
        fit = joint_fit(rows, self.credits, resamples=20)
        self.assertAlmostEqual(fit["coef"]["opus47"], 1e-4, places=8)
        self.assertAlmostEqual(fit["coef"]["opus"], 0.0, places=8)

    def test_b5_from_the_fit_is_the_reciprocal_of_the_opus_coefficient(self):
        fit = joint_fit(self.rows, self.credits, resamples=20)
        b5 = joint_fit_b5(fit, self.rows, self.credits)
        self.assertAlmostEqual(b5["median"], 10_000.0, places=2)  # 1 / 1e-4

    def test_feasibility_slack_is_zero_for_a_design_that_fits_exactly(self):
        feas = feasibility_reads_free(self.rows, self.credits)
        self.assertAlmostEqual(feas["extra_pp"], 0.0, places=6)

    def test_no_rows_returns_none_everywhere_without_raising(self):
        self.assertIsNone(joint_fit([], self.credits))
        self.assertIsNone(feasibility_reads_free([], self.credits))


class EnvelopeWiringTests(unittest.TestCase):
    def test_a_fable_heavy_row_solves_against_a_known_b5(self):
        credits = C.load_credits()
        fable_row = _stretch(POST, 10.0, _tokens(**{"claude-fable-5": _fable(100_000, 10_000)}))
        rows = fable_rows([fable_row], credits)
        self.assertEqual(len(rows), 1)
        b5 = {"median": 10_000.0, "range": [10_000.0, 10_000.0]}
        env = envelope(b5, rows)
        self.assertEqual(env["n"], 1)
        # f = (10.0 * 10_000 - 0) / (100_000 + 5*10_000) = 100_000 / 150_000
        self.assertAlmostEqual(env["median"], 100_000 / 150_000, places=6)

    def test_a_row_below_delta_min_is_dropped_even_if_fable_heavy(self):
        credits = C.load_credits()
        fable_row = _stretch(POST, 5.0, _tokens(**{"claude-fable-5": _fable(100_000, 10_000)}))
        self.assertEqual(fable_rows([fable_row], credits), [])

    def test_claude_opus_4_7_is_charged_at_its_own_coefficient_not_the_opus_5_weight(self):
        credits = C.load_credits()
        # Fable at 90% of raw tokens, claude-opus-4-7 the rest, so the row still clears
        # FABLE_SHARE with the opus47 tokens present.
        row = _stretch(POST, 10.0, _tokens(
            **{"claude-fable-5": _fable(90_000, 0), "claude-opus-4-7": _opus(10_000)}))
        rows_no_charge = fable_rows([row], credits, coef_opus47=0.0)
        rows_charged = fable_rows([row], credits, coef_opus47=2e-4)
        self.assertEqual(rows_no_charge[0]["opus47_pct_charge"], 0.0)
        self.assertAlmostEqual(rows_charged[0]["opus47_pct_charge"], 2e-4 * 10_000, places=8)
        self.assertEqual(rows_no_charge[0]["other_charge_fixed"], 0.0)
        b5 = {"median": 10_000.0, "range": [10_000.0, 10_000.0]}
        f_no_charge = envelope(b5, rows_no_charge)["median"]
        f_charged = envelope(b5, rows_charged)["median"]
        # A positive opus47 coefficient subtracts its own percentage-point charge from
        # delta_pct before the B5 multiplication, so the solved f falls.
        self.assertLess(f_charged, f_no_charge)


if __name__ == "__main__":
    unittest.main()
