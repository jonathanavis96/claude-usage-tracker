"""Defects in tracker/credits.py that real history files trigger, one test class per defect.

Every fixture here is built in memory; nothing reads data/, history/ or any live file except
data/prices.json's `_credits` block, which the rest of the credits tests read the same way.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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


class PriceTokensSkipsEmptyBundleTests(unittest.TestCase):
    """`price_tokens(...).priced` must not go False for a bundle of no tokens.

    `fits_as_of` says its selection is exactly the fit's `ok` column, which counts a stretch
    whose only model outside the families is a zero-token `<synthetic>` row; `price_tokens`
    marked it unpriced, so the fits' as-of date, the publisher's per-account newest priced
    stretch and the Fable solve all skipped 39 such stretches of the fits' selection.
    """

    def test_a_zero_token_synthetic_row_leaves_the_stretch_priced(self):
        priced = C.price_tokens({"claude-opus-5": tok(input=300, output=150), **SYNTHETIC},
                                CREDITS, 0)
        self.assertTrue(priced.priced)
        self.assertAlmostEqual(priced.known, 300 * OPUS_IN + 150 * OPUS_OUT)
        self.assertEqual((priced.raw, priced.charged), (450, 450))

    def test_a_synthetic_row_that_carries_tokens_still_unprices(self):
        self.assertFalse(C.price_tokens({"claude-opus-5": tok(input=300),
                                         "<synthetic>": tok(input=10)}, CREDITS, 0).priced)

    def test_fits_as_of_reads_the_newest_fitted_stretch_even_with_a_synthetic_row(self):
        older = {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-01T02:00:00+00:00",
                 "delta_pct": 10.0, "tokens": {"claude-opus-5": tok(input=1_000)}}
        newer = {"start": "2026-10-02T00:00:00+00:00", "end": "2026-10-02T02:00:00+00:00",
                 "delta_pct": 10.0, "tokens": {"claude-opus-5": tok(input=1_000), **SYNTHETIC}}
        got = C.fits_as_of({"jwork": [older, newer]}, CREDITS, {"fits_pooled": ["jwork/post"]})
        self.assertEqual(got, newer["end"])


class StamplessStretchTests(unittest.TestCase):
    """A stretch with no `start`/`end` must not crash a selection that `clean_stretches` let
    it through: that function keeps it ("a missing stamp must not be read as 'overlaps
    nothing' in one place and crash in another"), and `window_stretches` and `spans_cut` keep
    it too, but the masterrig filters raised KeyError and `_side_of` raised on it."""

    @staticmethod
    def _stretch(**extra) -> dict:
        return {"delta_pct": 10.0, "tokens": {"claude-opus-5": tok(input=1_000)},
                "status": "accepted", "capture_status": "accepted", **extra}

    # Code review round 3: a masterrig stretch with no start cannot be shown to be from
    # MASTERRIG_FROM on, so every selection leaves it out (01267da8 had kept it), without
    # crashing; another account's stamp-less stretch is kept as before.
    def test_the_rate_fit_selection_leaves_out_a_stampless_masterrig_stretch(self):
        dated = self._stretch(start="2026-09-10T00:00:00+00:00", end="2026-09-10T02:00:00+00:00")
        kept = C.rate_fit_stretches({"masterrig": [self._stretch(), dated], "jwork": [self._stretch()]}, [])
        self.assertEqual(kept["masterrig"], [dated])
        self.assertEqual(len(kept["jwork"]), 1)
        self.assertEqual(C.window_stretches({"masterrig": [self._stretch(), dated]}, [])["masterrig"], [dated])

    def test_the_known_date_selection_leaves_out_a_stampless_masterrig_stretch(self):
        kept = C.announced_change_stretches({"masterrig": [self._stretch()]}, [])
        self.assertEqual(kept["masterrig"], [])

    def test_the_masterrig_cut_reads_only_the_start(self):
        # Code review round 6: _masterrig_in_use parsed `end` too, which masterrig_excluded
        # never reads without whole_history. (clean_stretches parses both stamps before it.)
        self.assertTrue(C._masterrig_in_use({"start": "2026-09-10T00:00:00+00:00", "end": "not a time"}))
        self.assertFalse(C._masterrig_in_use({"start": "2026-09-03T00:00:00+00:00"}))
        # Round 8: a naive start is UTC, as everywhere else, not a TypeError against the cut.
        self.assertTrue(C._masterrig_in_use({"start": "2026-10-01T00:00:00"}))
        self.assertFalse(C._masterrig_in_use({"start": "2026-09-03T00:00:00"}))

    def test_a_naive_stretch_is_placed_beside_a_candidate_not_a_crash(self):
        # Code review round 10: the filters read a naive stamp as UTC, so it reached _side_of,
        # which compared it naive with the aware candidate instant.
        at = datetime(2026, 9, 20, tzinfo=timezone.utc)
        naive = self._stretch(start="2026-09-21T00:00:00", end="2026-09-21T02:00:00")
        split = C.split_at_candidate([naive], at, None, None, lambda t: 1_000.0)
        self.assertEqual(len(split["sides"]["after"]), 1)

    def test_masterrig_before_the_cut_is_still_left_out(self):
        early = self._stretch(start="2026-09-03T00:00:00+00:00", end="2026-09-03T02:00:00+00:00")
        for select in (C.rate_fit_stretches, C.window_stretches, C.announced_change_stretches):
            self.assertEqual(select({"masterrig": [early]}, [])["masterrig"], [], select.__name__)

    def test_a_stampless_stretch_is_on_neither_side_of_a_candidate(self):
        at = datetime(2026, 9, 20, tzinfo=timezone.utc)
        split = C.split_at_candidate([self._stretch()], at, None, None, lambda t: 1_000.0)
        self.assertEqual(split["sides"], {"before": [], "after": []})


class HarnessRunsUnreadableLineTests(unittest.TestCase):
    """A run row that cannot be parsed still stops the read (a run that cannot be read cannot
    be excluded), but the error names the file and line, so a cron publish says which."""

    def test_a_truncated_last_line_raises_naming_the_file_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness-runs.jsonl"
            path.write_text('{"account": "jwork", "start": "2026-09-09T11:00:00+00:00", '
                            '"end": "2026-09-09T12:00:00+00:00", "kind": "probe"}\n'
                            '{"account": "jwork", "start": "2026-09-10T1', encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError) as caught:
                C.harness_runs(path)
        self.assertIn(f"{path}:2", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class OnlyEmptyBundlesStayAsOnMainTests(unittest.TestCase):
    """Code review round 1: skipping a zero-token bundle is for a stretch with work beside it.
    A stretch of nothing but `<synthetic>` rows priced as before this branch (unpriced: the
    model has no family) instead of turning into a priced zero that the stretch-reading
    paths (passive_dollar_readings, fit_output_weight) drop as a collection gap."""

    def test_price_tokens_leaves_an_all_synthetic_stretch_unpriced(self):
        self.assertFalse(C.price_tokens(dict(SYNTHETIC), CREDITS).priced)
        self.assertTrue(C.price_tokens({"claude-opus-5": tok(input=1_000), **SYNTHETIC}, CREDITS).priced)

    def test_the_pooled_value_leaves_an_all_synthetic_stretch_unpriced(self):
        value = C.across_cut_value(CREDITS, model_rates=POOLED)
        self.assertIsNone(value(dict(SYNTHETIC)))


class EmptyBundleRuleTests(unittest.TestCase):
    """Code review round 3: one rule for every reader of a stretch's work, and the pricing
    rule built on it, so the copies cannot drift apart again."""

    WORK = {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0}
    EMPTY = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    def test_readers_drop_empty_bundles_and_see_nothing_left_as_no_work(self):
        self.assertEqual(C.bundles_with_work({"m": self.WORK, "<synthetic>": self.EMPTY}), {"m": self.WORK})
        self.assertEqual(C.bundles_with_work({"<synthetic>": self.EMPTY}), {})
        self.assertEqual(C.bundles_with_work(None), {})
        # Code review rounds 8 and 9: a value that is no bundle (a null, a number) is kept, so
        # its stretch is refused whole, neither crashing bundle_meter_usd's .get nor read
        # without that model's work.
        self.assertEqual(C.bundles_with_work({"m": self.WORK, "x": None, "y": 7}), {"m": self.WORK, "x": None, "y": 7})

    def test_a_stretch_with_a_malformed_bundle_is_no_passive_reading(self):
        from tracker.gs_passive import passive_dollar_readings
        from tracker.join import bundle_meter_usd
        prices = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}}
        self.assertIsNone(bundle_meter_usd("claude-sonnet-5", None, prices))
        st = {"status": "accepted", "capture_status": "accepted", "reset_verified": True,
              "start": "2026-11-01T10:00:00+00:00", "end": "2026-11-01T11:00:00+00:00", "delta_pct": 10,
              "windows": 1, "tokens": {"claude-sonnet-5": {**self.WORK, "cache_write": 400000},
                                       "claude-sonnet-5-20251001": "garbage"}}
        self.assertEqual(passive_dollar_readings({"accounts": {"jwork": {"stretches": [st]}}}, prices), [])

    def test_pricing_keeps_an_all_empty_stretch_as_it_was(self):
        self.assertEqual(C.work_bundles({"m": self.WORK, "<synthetic>": self.EMPTY}), {"m": self.WORK})
        self.assertEqual(C.work_bundles({"<synthetic>": self.EMPTY}), {"<synthetic>": self.EMPTY})


class NaiveStampEverySelectionTests(unittest.TestCase):
    """PR #155 review, final round 1: `_masterrig_in_use` and `_side_of` read a naive stamp as
    UTC, but `spans_cut` and `clean_stretches` (via `overlapping_run`) still compared it naive
    with an aware instant, so the stretch crashed one filter further on."""

    @staticmethod
    def _stretch(**extra) -> dict:
        return {"delta_pct": 10.0, "tokens": {"claude-opus-5": tok(input=1_000)},
                "status": "accepted", "capture_status": "accepted", **extra}

    def test_a_naive_masterrig_stretch_passes_the_cut_test_without_a_crash(self):
        naive = self._stretch(start="2026-09-20T10:00:00", end="2026-09-20T12:00:00")
        self.assertEqual(C.rate_fit_stretches({"masterrig": [naive]}, [])["masterrig"], [naive])
        spanning = self._stretch(start="2026-09-14T11:00:00", end="2026-09-14T13:00:00")
        self.assertTrue(C.spans_cut(spanning))

    def test_a_naive_stretch_is_tested_against_the_harness_runs_as_utc(self):
        run = C.HarnessRun("jwork", datetime(2026, 9, 20, 11, tzinfo=timezone.utc),
                           datetime(2026, 9, 20, 11, 30, tzinfo=timezone.utc), "probe")
        inside = self._stretch(start="2026-09-20T10:00:00", end="2026-09-20T12:00:00")
        clear = self._stretch(start="2026-09-20T13:00:00", end="2026-09-20T14:00:00")
        kept = C.clean_stretches({"jwork": [inside, clear]}, [run])
        self.assertEqual(kept["jwork"], [clear])


class HarnessRunsNonObjectLineTests(unittest.TestCase):
    def test_a_line_that_is_no_object_raises_naming_the_file_and_line(self):
        # Round 1: `[]` parsed, then crashed on row.get with no file or line named.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness-runs.jsonl"
            path.write_text('{"account": "jwork", "start": "2026-09-09T11:00:00+00:00", '
                            '"end": "2026-09-09T12:00:00+00:00", "kind": "probe"}\n[]\n', encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                C.harness_runs(path)
        self.assertIn(f"{path}:2", str(caught.exception))


class NaiveHarnessRunTests(unittest.TestCase):
    def test_a_naive_run_is_read_as_utc_against_a_stretch(self):
        # PR #155 review, final round 2: stretch_span made every span aware, but a run row
        # with no offset stayed naive, and overlapping_run raised TypeError between them.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness-runs.jsonl"
            path.write_text('{"account": "jwork", "start": "2026-09-01T00:00:00", '
                            '"end": "2026-09-02T00:00:00", "kind": "probe"}\n', encoding="utf-8")
            runs = C.harness_runs(path)
        self.assertEqual(runs[0].start, datetime(2026, 9, 1, tzinfo=timezone.utc))
        st = {"delta_pct": 10.0, "tokens": {"claude-opus-5": tok(input=1_000)},
              "capture_status": "accepted", "start": "2026-09-01T10:00:00", "end": "2026-09-01T11:00:00"}
        self.assertEqual(C.clean_stretches({"jwork": [st]}, runs)["jwork"], [])


class EmptyBundleBesideNoBundleTests(unittest.TestCase):
    def test_an_empty_row_beside_a_value_that_is_no_bundle_still_unprices(self):
        # PR #155 review, final round 2: work_bundles dropped the empty <synthetic> row because
        # a malformed value counted as "another bundle", and the readers then skipped that
        # value, so a stretch of no work priced at 0 instead of unpriced as on main.
        tokens = {**SYNTHETIC, "claude-sonnet-5": None}
        self.assertFalse(C.price_tokens(tokens, CREDITS, 0).priced)
        self.assertIsNone(C.across_cut_value(CREDITS, model_rates=POOLED)(tokens))
        self.assertEqual(C.work_bundles(tokens), tokens)
