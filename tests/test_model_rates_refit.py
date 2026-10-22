"""The refit survives the data it crashed on, and does not publish a sharp move silently.

From 2026-10-13T19:30Z to 2026-10-22 tools/model_rates.py crashed on every gs run: first a
ZeroDivisionError in section 4's `restricted` comparison, from 2026-10-15T14:00Z an IndexError
in `interval` via section 4's mix comparison. Section 4 valued stretches at the January table,
which has no Opus 5.5 row, so Opus 5.5 stretches were worth zero and the medians went to zero;
and it named jwork and dave, so an account set without them raised KeyError. These tests build
the two crash shapes from a handful of stretches, an account set with no jwork, and the guard
that holds a refit whose rates move outside their own intervals.
"""
import contextlib
import io
import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tools.model_rates as M
from tools.model_rates import (HELD_EXIT, MIN_N, MIN_NONZERO, SHELLAC, account_pairs,
                               candidate_path, compare, prepare, priceable, rate_moves,
                               ratio_bootstrap, section4, section5, tables)

POST = "2026-09-16T00:00:00+00:00"
IDS = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "fable": "claude-fable-5-1",
       "opus_5_5": "claude-opus-5-5"}


def _tokens(reads: int = 0, **families) -> dict:
    return {IDS[f]: {"input": i, "output": o, "cache_read": reads, "cache_write": 0}
            for f, (i, o) in families.items()}


def _kept(delta: float, tokens: dict, start: str = POST, **fields) -> dict:
    s = {"start": start, "end": start, "delta_pct": delta, "tokens": tokens, "reset_verified": True,
         "capture_status": "accepted", "windows": 1}
    s.update(fields)
    return s


def _opus(n: int, account: str, per_pct: float = 100_000.0, **fields) -> list[dict]:
    """`n` all-Opus-5 stretches, each buying `per_pct` input tokens (a little spread) per 1%."""
    return prepare(account, [_kept(10.0, _tokens(opus=(int(per_pct * 10) + 1_000 * i, 0)), **fields)
                             for i in range(n)])


def _opus_5_5(n: int, account: str, **fields) -> list[dict]:
    """`n` all-Opus-5.5 stretches: the January table has no rate for any of their tokens."""
    return prepare(account, [_kept(10.0, _tokens(opus_5_5=(1_000_000 + 1_000 * i, 0)), **fields)
                             for i in range(n)])


class RatioBootstrapTests(unittest.TestCase):
    """The two ways the bootstrap crashed, now each a ratio that is not measurable."""

    def test_a_zero_median_denominator_is_not_measurable_rather_than_a_zero_division(self):
        # Three zero-priced stretches out of five put the median at zero: the
        # ZeroDivisionError of 2026-10-13T19:30Z.
        r = ratio_bootstrap([1.0, 2.0, 3.0], [0.0, 0.0, 0.0, 4.0, 5.0], random.Random(1), 50)
        self.assertFalse(r["measurable"])
        self.assertIsNone(r["ratio"])
        self.assertIsNone(r["interval"])
        self.assertIn("median is zero", r["why"])

    def test_a_bootstrap_with_no_surviving_draw_is_not_measurable_rather_than_an_index_error(self):
        # Every resample of an all-zero side has a zero median, so no draw survives: the
        # IndexError in `interval` of 2026-10-15T14:00Z onwards (0 of 600 draws on gs).
        r = ratio_bootstrap([1.0, 2.0], [0.0, 0.0, 0.0], random.Random(1), 50)
        self.assertFalse(r["measurable"])
        self.assertEqual(r["n_draws"], 0)

    def test_an_empty_side_is_not_measurable(self):
        self.assertFalse(ratio_bootstrap([], [1.0], random.Random(1), 10)["measurable"])
        self.assertFalse(ratio_bootstrap([1.0], [], random.Random(1), 10)["measurable"])

    def test_a_measurable_ratio_is_unchanged(self):
        r = ratio_bootstrap([2.0, 2.0, 2.0], [1.0, 1.0, 1.0], random.Random(1), 50)
        self.assertTrue(r["measurable"])
        self.assertEqual(r["ratio"], 2.0)
        self.assertEqual(r["interval"], [2.0, 2.0])

    def test_compare_needs_min_n_a_side_and_says_why(self):
        c = compare([1.0] * (MIN_N - 1), [1.0] * MIN_N, random.Random(1), 20)
        self.assertFalse(c["measurable"])
        self.assertIn(f"under the {MIN_N} a side", c["why"])
        c = compare([1.0] * MIN_N, [1.0] * MIN_N, random.Random(1), 20)
        self.assertEqual(c["verdict"], "explains")


class PriceableTests(unittest.TestCase):
    def test_a_stretch_with_a_family_the_table_has_no_rate_for_is_not_valued_at_zero(self):
        opus, o55 = _opus(1, "a")[0], _opus_5_5(1, "a")[0]
        shellac = tables({})["shellac"]
        self.assertNotIn("opus-5-5", shellac)
        self.assertTrue(priceable(opus, shellac))
        self.assertFalse(priceable(o55, shellac))
        self.assertTrue(priceable(o55, {"opus-5-5": 0.5}))
        self.assertFalse(priceable(o55, {"opus-5-5": 0.0}))

    def test_the_tables_are_every_accounts_fit_not_a_named_two(self):
        fit = {"rates": {"opus": SHELLAC["opus"], "sonnet": 0.3, "haiku": 0.0}}
        out = tables({"masterrig/post/out5x": fit, "masterrig/post/out3x": fit, "avis/pre/out5x": fit})
        self.assertEqual(sorted(out), ["fitted avis/pre", "fitted masterrig/post", "shellac"])
        # A zero rate is no rate: the table cannot value a stretch carrying that family.
        self.assertNotIn("haiku", out["fitted masterrig/post"])


class CrashShapeTests(unittest.TestCase):
    """Section 4 on the inputs that crashed it, built small."""

    def test_the_zero_division_shape_restricted_to_reset_verified(self):
        # dave's reset_verified stretches mostly Opus 5.5, as on 2026-10-13T19:30Z (32 of 63).
        data = {"jwork": _opus(4, "jwork"),
                "dave": _opus(3, "dave") + _opus_5_5(4, "dave")}
        s4 = section4(data, {}, 1, 50)
        r = s4["restricted"]["dave:jwork"]["reset_verified only"]
        # The Opus 5.5 stretches are left out and counted, not valued at zero: dave's three
        # Opus 5 stretches are compared with jwork's four.
        self.assertTrue(r["measurable"])
        self.assertEqual(r["n"], {"dave": 3, "jwork": 4})
        self.assertEqual(s4["unpriced"]["shellac"], {"dave": 4, "jwork": 0})
        self.assertGreater(r["medians"]["dave"], 0)

    def test_the_index_error_shape_every_stretch_unpriceable(self):
        # Every jwork and dave stretch Opus 5.5, as from 2026-10-15T14:00Z: nothing to value.
        data = {"jwork": _opus_5_5(5, "jwork"), "dave": _opus_5_5(8, "dave")}
        s4 = section4(data, {}, 1, 50)
        m = s4["mix"]["dave:jwork"]["shellac"]
        self.assertFalse(m["measurable"])
        self.assertEqual(m["unpriced"], {"dave": 8, "jwork": 5})
        self.assertEqual(m["medians"], {"dave": None, "jwork": None})
        for label in ("reset_verified only", "delta_pct above 10"):
            self.assertFalse(s4["restricted"]["dave:jwork"][label]["measurable"])
        # The per-account reset_verified and length rows say the same.
        self.assertEqual(s4["reset_verified"]["dave/post"]["unpriced"], 8)
        self.assertIsNone(s4["length"]["dave/post"]["cut_delta_pct"])
        _quiet(M.show_section4, s4)

    def test_a_fitted_table_that_does_have_the_rate_values_those_stretches(self):
        data = {"jwork": _opus_5_5(5, "jwork"), "dave": _opus_5_5(8, "dave")}
        s3 = {"masterrig/post/out5x": {"rates": {"opus": SHELLAC["opus"], "opus-5-5": 0.5}}}
        m = section4(data, s3, 1, 50)["mix"]["dave:jwork"]["fitted masterrig/post"]
        self.assertTrue(m["measurable"])
        self.assertEqual(m["unpriced"], {"dave": 0, "jwork": 0})


class DerivedAccountTests(unittest.TestCase):
    """No account is named: the pairs are the data's, and each says which way up it is."""

    def test_no_jwork_in_the_data_is_not_a_key_error(self):
        data = {"masterrig": _opus(5, "masterrig"), "avis": _opus(4, "avis", per_pct=50_000.0)}
        s4 = section4(data, {}, 1, 50)
        self.assertEqual(s4["accounts"], ["avis", "masterrig"])
        self.assertEqual(s4["pairs"], ["avis:masterrig"])
        m = s4["mix"]["avis:masterrig"]["shellac"]
        self.assertEqual((m["numerator"], m["denominator"]), ("avis", "masterrig"))
        _quiet(M.show_section4, s4)

    def test_the_pair_order_is_named_and_the_ratio_is_numerator_over_denominator(self):
        # dave buys half the tokens per 1% jwork does, so dave's credits per 1% are half
        # jwork's and dave:jwork is 0.5 -- the old jwork/dave figure the other way up, and the
        # key and fields say so.
        data = {"jwork": _opus(5, "jwork", per_pct=200_000.0),
                "dave": _opus(5, "dave", per_pct=100_000.0)}
        m = section4(data, {}, 1, 50)["mix"]["dave:jwork"]["shellac"]
        self.assertEqual((m["numerator"], m["denominator"]), ("dave", "jwork"))
        self.assertAlmostEqual(m["ratio"], m["medians"]["dave"] / m["medians"]["jwork"])
        self.assertAlmostEqual(m["ratio"], 0.5, delta=0.02)
        self.assertEqual(m["verdict"], "does not explain")

    def test_every_pair_once_and_a_single_account_has_none(self):
        self.assertEqual(account_pairs(["c", "a", "b"]), [("a", "b"), ("a", "c"), ("b", "c")])
        s4 = section4({"masterrig": _opus(5, "masterrig")}, {}, 1, 20)
        self.assertEqual(s4["pairs"], [])
        self.assertEqual(s4["mix"], {})
        self.assertIn("masterrig/post", s4["quantisation"])
        _quiet(M.show_section4, s4)

    def test_section_five_reads_any_accounts_window_and_none_is_not_measurable(self):
        mr = {"per_family": {"opus": {"reference_input": SHELLAC["opus"], "input": SHELLAC["opus"],
                                      "interval": None, "status": None, "rate_source": "reference"}}}
        none = section5({"harness-runs": {}}, mr)
        self.assertFalse(none["measurable"])
        self.assertIn("no account has a clean pure-Opus stretch", none["why"])
        _quiet(M.show_section5, none)
        one = section5({"harness-runs": {"masterrig": {"median": 200_000.0}}}, mr)
        self.assertTrue(one["measurable"])
        self.assertEqual(one["by_account"]["masterrig"]["window_credits"], 20_000_000.0)
        _quiet(M.show_section5, one)

    def test_a_diagnostic_that_fails_is_recorded_not_raised(self):
        def broken():
            raise KeyError("jwork")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            out = M.diagnostic("4", broken)
        self.assertEqual(out, {"failed": "KeyError: 'jwork'"})
        self.assertIn("section 4 failed", err.getvalue())
        _quiet(M.show_section4, out)
        _quiet(M.show_section5, out)


def _quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        fn(*args)


def _block(**over) -> dict:
    """A `measured_rates` block in force: Sonnet measured, Opus 5.5 measured, Haiku carried
    unpublished, the cache-read weight and the headless factor with intervals."""
    per = {"opus": {"anchor": True, "input": SHELLAC["opus"], "times_opus": 1.0, "provisional": False},
           "sonnet": {"input": 0.5 * SHELLAC["opus"], "times_opus": 0.5,
                      "times_opus_interval": [0.44, 0.58], "provisional": False},
           "opus-5-5": {"input": 0.78 * SHELLAC["opus"], "times_opus": 0.78,
                        "times_opus_interval": [0.65, 0.94], "provisional": False},
           "haiku": {"input": None, "times_opus": None, "provisional": False}}
    block = {"per_family": per,
             "pooled_fit": {"times_opus": {"opus": 1.0, "sonnet": 0.5, "opus-5-5": 0.78, "haiku": 0.37},
                            "interval": {"sonnet": [0.44, 0.58], "opus-5-5": [0.65, 0.94],
                                         "haiku": [0.0, 1.04]},
                            "cache_read_weight": 0.0097, "cache_read_weight_interval": [0.0071, 0.0130]},
             "five_hour_unit": {"headless_factor": 1.49, "interval": [1.389, 1.597]},
             "anchor": {"family": "opus", "input": SHELLAC["opus"], "n_stretches_carrying": 72}}
    for path, value in over.items():
        node = block
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return block


class MoveGuardTests(unittest.TestCase):
    def test_the_same_rates_and_a_first_fit_are_not_held(self):
        self.assertEqual(rate_moves(_block(), _block()), [])
        self.assertEqual(rate_moves({}, _block()), [])

    def test_a_move_inside_the_old_interval_is_not_held(self):
        new = _block(per_family__sonnet__times_opus=0.56, pooled_fit__cache_read_weight=0.012)
        self.assertEqual(rate_moves(_block(), new), [])

    def test_the_moves_of_the_first_refit_after_the_crash_are_held(self):
        # The skeptic's fresh refit on the history of 2026-10-22: Opus 5.5 0.780x measured to
        # 0.367x provisional, the cache-read weight 0.0097 to 0.0023, the headless factor 1.49
        # to 1.369.
        new = _block()
        new["per_family"]["opus-5-5"] = {"input": 0.367 * SHELLAC["opus"], "times_opus": 0.367,
                                         "times_opus_interval": [0.211, 0.516], "provisional": True}
        new["pooled_fit"]["cache_read_weight"] = 0.0023
        new["five_hour_unit"]["headless_factor"] = 1.369
        moves = rate_moves(_block(), new)
        self.assertEqual(len(moves), 4, moves)
        self.assertIn("Opus 5.5 drops from measured to provisional", moves[0])
        self.assertIn("Opus 5.5 moves from 0.780x Opus [0.650, 0.940] to 0.367x", moves[1])
        self.assertIn("cache-read weight moves from 0.0097", moves[2])
        self.assertIn("headless factor moves from 1.490", moves[3])

    def test_a_published_rate_that_loses_its_value_is_held(self):
        new = _block()
        new["per_family"]["sonnet"] = {"input": None, "times_opus": None, "provisional": False}
        self.assertEqual(rate_moves(_block(), new),
                         ["Sonnet no longer has a published rate (was 0.500x Opus)"])
        del new["per_family"]["sonnet"]
        self.assertEqual(len(rate_moves(_block(), new)), 1)

    def test_an_unpublished_family_the_comparison_prices_is_guarded_too(self):
        new = _block()
        new["pooled_fit"]["times_opus"]["haiku"] = 1.6
        moves = rate_moves(_block(), new)
        self.assertEqual(len(moves), 1)
        self.assertIn("pooled fit's Haiku (not published", moves[0])

    def test_floating_point_noise_at_an_end_of_the_interval_is_not_a_move(self):
        # A degenerate interval reproduced to the last bits, and Haiku's interval on gs, which
        # "reaches zero" at 1.4e-13 while the refit puts the point at 6.6e-14.
        self.assertFalse(M._outside(2.0000000000000004, [1.9999999999999998, 2.0]))
        self.assertFalse(M._outside(6.6e-14, [1.4e-13, 1.037]))
        self.assertTrue(M._outside(0.0, [0.05, 1.037]))
        self.assertFalse(M._outside(None, [0.1, 0.2]))
        self.assertFalse(M._outside(0.5, None))

    def test_a_new_family_arrives_freely(self):
        new = _block()
        new["per_family"]["sonnet-5-5"] = {"input": 0.25 * SHELLAC["opus"], "times_opus": 0.25,
                                           "times_opus_interval": [0.2, 0.3], "provisional": True}
        new["pooled_fit"]["times_opus"]["sonnet-5-5"] = 0.25
        self.assertEqual(rate_moves(_block(), new), [])

    def test_a_fit_the_anchor_no_longer_ties_to_the_unit_is_held(self):
        new = _block(anchor__n_stretches_carrying=MIN_NONZERO - 1)
        moves = rate_moves(_block(), new)
        self.assertEqual(len(moves), 1)
        self.assertIn("carry Opus, the unit anchor", moves[0])
        self.assertEqual(rate_moves(_block(), _block(anchor__n_stretches_carrying=MIN_NONZERO)), [])

    def test_a_headless_factor_that_stops_being_measured_is_held(self):
        moves = rate_moves(_block(), _block(five_hour_unit__headless_factor=None))
        self.assertEqual(moves, ["the headless factor is no longer measured (was 1.490)"])

    def test_the_candidate_sits_beside_the_file_in_force(self):
        self.assertEqual(candidate_path(Path("history/model-rates.json")),
                         Path("history/model-rates.candidate.json"))


class HeldRefitEndToEndTests(unittest.TestCase):
    """main() on a small built history: a first fit is written, a refit that moves a rate
    outside the interval in force is held at the candidate path with exit HELD_EXIT and the
    file in force untouched, and --accept-moves writes it and clears the candidate."""

    TIMES_OPUS = {"opus": 1.0, "sonnet": 0.6, "fable": 2.0}
    MIXES = ((9, 1, 0), (1, 9, 0), (0, 1, 9), (5, 4, 1), (2, 2, 6), (7, 2, 1), (3, 6, 1),
             (4, 1, 5), (6, 3, 1), (1, 1, 8))

    def _history(self) -> dict:
        accounts = {}
        t0 = datetime(2026, 9, 16, tzinfo=timezone.utc)
        for k, (account, scale) in enumerate((("acct-a", 200_000.0), ("acct-b", 150_000.0))):
            rows = []
            for i, (o, s_, f) in enumerate(self.MIXES):
                start = t0 + timedelta(hours=6 * i + k)
                tokens = _tokens(0, opus=(o * 1_000_000, 0), sonnet=(s_ * 1_000_000, 0),
                                 fable=(f * 1_000_000, 0))
                credits = SHELLAC["opus"] * sum(n * 1_000_000 * self.TIMES_OPUS[x]
                                                for n, x in ((o, "opus"), (s_, "sonnet"), (f, "fable")))
                rows.append(_kept(credits / scale, tokens, start=start.isoformat(),
                                  end=(start + timedelta(hours=5)).isoformat()))
            accounts[account] = {"stretches": rows}
        return {"accounts": accounts}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "history").mkdir()
        (self.root / "history" / "gs-passive.json").write_text(json.dumps(self._history()))
        (self.root / "masterrig.json").write_text(json.dumps({"accounts": {}}))
        self.out = self.root / "history" / "model-rates.json"
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.saved = (M.probes_only_runs, M.five_hour_unit, M.FAMILIES, M.CANDIDATE_BOUNDS)
        M.probes_only_runs = lambda: []
        M.five_hour_unit = lambda *a, **k: {"headless_factor": None, "interval": None,
                                            "status": "not measured", "what": "test"}

    def tearDown(self):
        os.chdir(self.cwd)
        M.probes_only_runs, M.five_hour_unit, M.FAMILIES, M.CANDIDATE_BOUNDS = self.saved
        self.tmp.cleanup()

    def _main(self, *extra) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = M.main(["masterrig.json", "--json", str(self.out), "--resamples", "5", *extra])
        return rc, err.getvalue()

    def test_held_then_accepted(self):
        rc, _ = self._main()
        self.assertEqual(rc, 0)
        first = json.loads(self.out.read_text())
        sonnet = first["measured_rates"]["per_family"]["sonnet"]
        self.assertAlmostEqual(sonnet["times_opus"], 0.6, places=3)
        self.assertEqual(first["section4"]["pairs"], ["acct-a:acct-b"])
        # Put the rates in force somewhere this history cannot reproduce.
        sonnet["times_opus_interval"] = [0.30, 0.40]
        sonnet["times_opus"] = 0.35
        in_force = json.dumps(first)
        self.out.write_text(in_force)

        rc, err = self._main()
        self.assertEqual(rc, HELD_EXIT)
        self.assertEqual(self.out.read_text(), in_force)
        cand = candidate_path(self.out)
        held = json.loads(cand.read_text())
        self.assertIn("Sonnet moves from 0.350x Opus [0.300, 0.400]", held["_meta"]["held"]["moves"][0])
        self.assertIn("--accept-moves", held["_meta"]["held"]["accept"])
        self.assertIn("refit held (exit 3)", err)

        rc, _ = self._main("--accept-moves")
        self.assertEqual(rc, 0)
        adopted = json.loads(self.out.read_text())
        self.assertAlmostEqual(adopted["measured_rates"]["per_family"]["sonnet"]["times_opus"], 0.6,
                               places=3)
        self.assertEqual(len(adopted["_meta"]["accepted_moves"]), 1)
        self.assertFalse(cand.exists())

        # A refit that matches the rates in force is written as before, with nothing held.
        rc, _ = self._main()
        self.assertEqual(rc, 0)
        self.assertNotIn("held", json.loads(self.out.read_text())["_meta"])


if __name__ == "__main__":
    unittest.main()
