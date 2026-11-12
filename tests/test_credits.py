"""tracker/credits.py and the publisher's credits block (issue #53).

Arithmetic, so the tests are arithmetic: a planted token bundle whose credits can be
worked out by hand, a window the fixture makes exactly, and a Fable rate whose three
per-account fits disagree, which has to come out the other end of every derivation as
a value (the median of the fits) with the union of their intervals, `status` null and
`agree` false.

The rule under test throughout is the one the reconciliation set: the measured
quantity is the five-hour window in credits, read off pure-Opus stretches that carry
no fitted parameter, and everything per model is a division of it. A figure that
cannot be computed is published as null with a status, never as a plausible number.
"""
import argparse
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from tools.credits_report import (
    BLOCK_KIND,
    compare_published,
    coverage,
    publish_check,
    recompute_credits_block,
)
from tracker import credits as C
from tracker import four_accounts as F
from tracker.credits import family, load_credits
from tracker.join import bundle_meter_usd
from tracker.publish import ACCOUNT_LABELS, build_public_json, rebuild_public_json
from tracker.turns import CANONICAL_MODELS, normalize_model

NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
LABELS = dict(ACCOUNT_LABELS)
#: The rates the tests price by hand, straight from data/prices.json's _credits block.
CREDITS = C.load_credits()
OPUS_IN, OPUS_OUT = C.rates("opus", CREDITS) or (0.0, 0.0)
PRICES = {"claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25},
          "claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5},
          "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 12.5}}


def tok(input=0, output=0, cache_read=0, cache_write=0):
    return {"input": input, "output": output, "cache_read": cache_read, "cache_write": cache_write}


def stretch(start: str, tokens: dict, delta_pct: float = 10.0, **extra) -> dict:
    """One passive stretch, accepted on both columns unless a test says otherwise."""
    end = extra.pop("end", None) or start
    return {"start": start, "end": end, "delta_pct": delta_pct, "tokens": tokens,
            "status": "accepted", "capture_status": "accepted", "capture": 1.0,
            "windows": 1, "unpriced_tokens": 0, "reset_verified": True, **extra}


def opus_stretch(start: str, per_pct: float, delta_pct: float = 10.0, **extra) -> dict:
    """A pure-Opus stretch built to read exactly `per_pct` credits per 1%.

    All of it on the input side, so the arithmetic is one multiplication: the
    stretch holds `per_pct * delta_pct / OPUS_IN` input tokens and nothing else.
    """
    return stretch(start, {"claude-opus-5": tok(input=round(per_pct * delta_pct / OPUS_IN))},
                   delta_pct, **extra)


def report(account: str, stretches: list[dict]) -> dict:
    return {"accounts": {account: {"account": account, "stretches": stretches}}}


class RateTableTests(unittest.TestCase):
    def test_every_opus_version_prices_as_opus(self):
        for model in ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7"):
            self.assertEqual(C.family(model, CREDITS), "opus")

    def test_opus_5_5_is_its_own_family_by_the_most_specific_match(self):
        """`opus` is a substring of claude-opus-5-5 too; the longer `opus-5-5` must win."""
        self.assertEqual(C.family("claude-opus-5-5", CREDITS), "opus-5-5")
        self.assertEqual(C.family("claude-opus-5-5-20260922", CREDITS), "opus-5-5")
        self.assertEqual(C.family("claude-opus-5", CREDITS), "opus")

    def test_the_most_specific_match_does_not_depend_on_the_table_order(self):
        rows = dict(CREDITS["per_family"])
        reordered = dict(CREDITS, per_family={"opus-5-5": rows.pop("opus-5-5"), **rows})
        backwards = dict(CREDITS, per_family=dict(reversed(list(reordered["per_family"].items()))))
        for table in (reordered, backwards):
            self.assertEqual(C.family("claude-opus-5-5", table), "opus-5-5")
            for model in ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7"):
                self.assertEqual(C.family(model, table), "opus")

    def test_opus_5_5_has_no_reference_rate_and_is_not_the_anchor(self):
        row = CREDITS["per_family"]["opus-5-5"]
        self.assertIsNone(C.rates("opus-5-5", CREDITS))
        self.assertEqual(row["role"], "reference")
        self.assertFalse(row.get("anchor"))
        self.assertIn("no reference rate", row["measured"])
        self.assertTrue(CREDITS["per_family"]["opus"]["anchor"])

    def test_rates_are_the_exact_fifteenths_the_table_stores(self):
        self.assertEqual(C.rates("haiku", CREDITS), (2 / 15, 10 / 15))
        self.assertEqual(C.rates("sonnet", CREDITS), (6 / 15, 30 / 15))
        self.assertEqual(C.rates("opus", CREDITS), (10 / 15, 50 / 15))

    def test_fable_has_no_stored_rate_and_no_stored_interval(self):
        """The endpoints are solved from the stretches at publish time, never typed here."""
        self.assertIsNone(C.rates("fable", CREDITS))
        row = CREDITS["per_family"]["fable"]
        self.assertIsNone(row["input"])
        self.assertIsNone(row["output"])
        self.assertNotIn("interval", row)
        self.assertEqual(row["output_ratio_candidates"], [3, 5])
        self.assertIn("computed at publish time", row["interval_rule"])

    def test_no_credit_figure_in_the_price_table_is_a_measurement(self):
        """Only reference rates and citations live in the file; measurements are computed."""
        text = json.dumps(CREDITS)
        for typed in ("2.0", "2.9", "1.2", "2.7", "19543887", "196989"):
            self.assertNotIn(f": {typed}", text)

    def test_cache_reads_are_free_with_the_fitted_range_kept_beside_the_zero(self):
        self.assertEqual(C.cache_read_weight(CREDITS), 0)
        self.assertEqual(CREDITS["cache_read_weight_range"], [0, 0.015])

    def test_cache_writes_ride_the_input_rate_and_the_one_hour_column_is_not_added_again(self):
        bundle = {"input": 10, "cache_write": 90, "cache_read": 1000, "cache_write_1h": 90}
        self.assertEqual(C.input_side(bundle, 0), 100)
        self.assertEqual(C.input_side(bundle, 0.015), 115)


class PricingTests(unittest.TestCase):
    def test_a_bundle_prices_at_the_published_rates(self):
        priced = C.price_tokens({"claude-opus-5": tok(input=300, output=150)}, CREDITS, 0)
        self.assertTrue(priced.priced)
        self.assertAlmostEqual(priced.known, 300 * OPUS_IN + 150 * OPUS_OUT)
        self.assertEqual((priced.fable_input, priced.fable_output), (0, 0))

    def test_fable_tokens_are_held_apart_from_what_a_rate_prices(self):
        priced = C.price_tokens({"claude-opus-5": tok(input=300),
                                 "claude-fable-5-1": tok(input=50, output=20)}, CREDITS, 0)
        self.assertAlmostEqual(priced.known, 300 * OPUS_IN)
        self.assertEqual((priced.fable_input, priced.fable_output), (50, 20))

    def test_a_model_no_family_covers_leaves_the_stretch_unpriceable(self):
        self.assertFalse(C.price_tokens({"<synthetic>": tok(input=10)}, CREDITS, 0).priced)

    def test_charged_tokens_exclude_the_cache_reads_that_raw_tokens_count(self):
        priced = C.price_tokens({"claude-opus-5": tok(input=10, output=5, cache_read=985)}, CREDITS, 0)
        self.assertEqual(priced.raw, 1000)
        self.assertEqual(priced.charged, 15)


class SelectionTests(unittest.TestCase):
    RUNS: ClassVar[list] = [C.HarnessRun("a", datetime(2026, 9, 9, 11, tzinfo=timezone.utc),
                         datetime(2026, 9, 9, 15, tzinfo=timezone.utc), "effort matrix")]

    def clean(self, stretches, account="a", **kw):
        return C.clean_stretches({account: stretches}, self.RUNS, **kw)[account]

    def test_a_stretch_overlapping_a_harness_run_on_its_own_account_is_left_out(self):
        inside = opus_stretch("2026-09-09T12:00:00+00:00", 200_000, end="2026-09-09T13:00:00+00:00")
        self.assertEqual(self.clean([inside]), [])

    def test_the_same_span_on_another_account_is_kept(self):
        inside = opus_stretch("2026-09-09T12:00:00+00:00", 200_000, end="2026-09-09T13:00:00+00:00")
        self.assertEqual(len(self.clean([inside], account="b")), 1)

    def test_the_rest_of_the_harness_runs_day_stays_in_the_series(self):
        later = opus_stretch("2026-09-09T20:00:00+00:00", 200_000, end="2026-09-09T21:00:00+00:00")
        self.assertEqual(len(self.clean([later])), 1)

    def test_a_stretch_holding_cloud_session_work_is_left_out(self):
        # Issue #133: a teleported cloud turn or a recorded cloud session in the span
        # (tracker/gs_passive.py `cloud_session`) takes the stretch out like a harness run.
        cloud = opus_stretch("2026-09-26T12:00:00+00:00", 200_000, end="2026-09-26T13:00:00+00:00",
                             cloud_session=True, remote_sourced_turns=40)
        local = opus_stretch("2026-09-26T14:00:00+00:00", 200_000, end="2026-09-26T15:00:00+00:00",
                             cloud_session=False, remote_sourced_turns=0)
        self.assertEqual([st["start"] for st in self.clean([cloud, local])], [local["start"]])
        self.assertEqual([st["start"] for st in self.clean([cloud, local], require="status")],
                         [local["start"]])

    def test_a_stretch_that_barely_moved_the_meter_is_mostly_rounding_and_is_left_out(self):
        self.assertEqual(self.clean([opus_stretch("2026-09-01T00:00:00+00:00", 200_000, delta_pct=2)]), [])

    def test_the_capture_column_is_the_publishers_acceptance_test(self):
        surplus = opus_stretch("2026-09-01T00:00:00+00:00", 9_000, capture_status="surplus", capture=None)
        self.assertEqual(self.clean([surplus]), [])
        self.assertEqual(len(self.clean([surplus], require="status")), 1)

    def test_an_exempt_account_skips_the_acceptance_test_entirely(self):
        unpriced = opus_stretch("2026-09-01T00:00:00+00:00", 9_000, status="unpriced")
        self.assertEqual(len(self.clean([unpriced], account="masterrig", require="status",
                                        exempt=("masterrig",))), 1)

    def test_an_accepted_status_with_an_unaccounted_capture_does_not_price_the_window(self):
        """The defect PR #64's gate caught: 42 stretches read exactly this way.

        Gating on `status` let them into the comparison and made the five-hour window
        look flat across 14 September. Gating on `capture_status` is what this module
        does everywhere, and `status` is never the default.
        """
        rows = [opus_stretch("2026-09-01T00:00:00+00:00", 200_000),
                opus_stretch("2026-09-02T00:00:00+00:00", 9_000, capture_status="unaccounted"),
                opus_stretch("2026-09-03T00:00:00+00:00", 9_000, status="unpriced",
                             capture_status="unpriced")]
        kept = self.clean(rows)
        self.assertEqual([r["start"] for r in kept], ["2026-09-01T00:00:00+00:00"])

    def test_a_stretch_the_host_saw_nothing_of_is_left_out(self):
        self.assertEqual(self.clean([stretch("2026-09-01T00:00:00+00:00", {})]), [])

    def test_masterrig_is_exempt_in_the_tools_selection_but_not_in_the_publishers(self):
        blind = opus_stretch("2026-09-01T00:00:00+00:00", 9_000, capture_status="unaccounted")
        self.assertEqual(len(self.clean([blind], account="masterrig", exempt=("masterrig",))), 1)
        self.assertEqual(self.clean([blind], account="masterrig"), [])


class WindowTests(unittest.TestCase):
    """The window computation on a fixture whose median and range are known by hand."""

    LEVELS = (180_000, 190_000, 200_000, 210_000, 220_000)

    def cluster(self, **kw):
        stretches = [opus_stretch(f"2026-09-{5 + i:02d}T00:00:00+00:00", level, **kw)
                     for i, level in enumerate(self.LEVELS)]
        clean = C.clean_stretches({"jwork": stretches}, [])
        return C.window_credits(clean, CREDITS, LABELS)

    def test_the_window_is_the_clusters_median_times_a_hundred(self):
        window = self.cluster()
        self.assertEqual(window["n"], 5)
        self.assertEqual(window["credits_per_pct"], 200_000)
        self.assertEqual(window["value"], 20_000_000)

    def test_the_interval_is_the_clusters_own_spread_not_an_error_model(self):
        self.assertEqual(self.cluster()["interval"], [18_000_000, 22_000_000])

    def test_every_watched_account_is_reported_by_label_including_the_empty_ones(self):
        window = self.cluster()
        self.assertEqual(sorted(window["accounts"]), ["a1", "a2", "a3", "a4"])
        self.assertEqual(window["accounts"]["a2"]["n"], 5)
        self.assertEqual(window["accounts"]["a1"], {"n": 0, "value": None, "interval": None})

    def test_a_stretch_with_any_other_model_in_it_is_not_pure_opus(self):
        mixed = stretch("2026-09-05T00:00:00+00:00",
                        {"claude-opus-5": tok(input=3_000_000), "claude-sonnet-5": tok(input=10)})
        window = C.window_credits(C.clean_stretches({"jwork": [mixed]}, []), CREDITS, LABELS)
        self.assertIsNone(window["value"])
        self.assertIn("no capture-accepted pure-opus stretch", window["status"])

    def test_the_method_names_what_was_done_rather_than_asserting_it(self):
        method = self.cluster()["method"]
        self.assertIn("capture_status 'accepted'", method)
        self.assertIn("no fitted parameter", method)

    def test_no_account_name_reaches_the_published_window(self):
        for name in ("jwork", "dave", "masterrig", "avis"):
            self.assertNotIn(name, json.dumps(self.cluster()))


def fable_stretch(start: str, input_tokens: int, output_tokens: int = 0,
                  delta_pct: float = 10.0, **extra) -> dict:
    """A pure-Fable stretch: the solve's divisor is exactly its own tokens."""
    return stretch(start, {"claude-fable-5-1": tok(input=input_tokens, output=output_tokens)},
                   delta_pct, **extra)


class FableIntervalTests(unittest.TestCase):
    """The interval is solved from the stretches; nothing about it is stored."""

    #: A window of 200,000 credits per 1%, so a stretch that moved 10% spent 2,000,000.
    W = 200_000

    def solve(self, **accounts):
        clean = C.clean_stretches(accounts, [], exempt=("masterrig",))
        return C.fable_interval(clean, CREDITS, self.W, LABELS)

    def test_a_pure_fable_stretch_solves_to_its_own_arithmetic(self):
        # 2,000,000 credits spent over 2,000,000 input tokens is 1.0 credits per token,
        # and with no output tokens the ratio cannot change it.
        out = self.solve(jwork=[fable_stretch("2026-09-10T00:00:00+00:00", 2_000_000)])
        self.assertEqual(out["per_account"]["a2"]["output_3x"]["p25"], 1.0)
        self.assertEqual(out["per_account"]["a2"]["output_5x"]["p25"], 1.0)

    def test_the_interval_runs_from_the_lowest_p25_at_5x_to_the_highest_at_3x(self):
        out = self.solve(jwork=[fable_stretch("2026-09-10T00:00:00+00:00", 2_000_000)],
                         masterrig=[fable_stretch("2026-09-10T00:00:00+00:00", 800_000)])
        self.assertEqual((out["input_low"], out["input_high"]), (1.0, 2.5))
        self.assertEqual(out["output_ratio"], [3, 5])
        self.assertEqual(out["status"], "interval, not yet separable")
        self.assertIsNone(out["unresolved"])

    def test_the_output_ratio_moves_the_solve_when_there_are_output_tokens(self):
        # divisor is fable_in + ratio x fable_out: 2,000,000 at 3x, 3,000,000 at 5x.
        out = self.solve(jwork=[fable_stretch("2026-09-10T00:00:00+00:00", 500_000, 500_000)])
        self.assertEqual(out["per_account"]["a2"]["output_3x"]["p25"], 1.0)
        self.assertEqual(out["per_account"]["a2"]["output_5x"]["p25"], 0.6667)

    def test_a_stretch_that_is_not_fable_heavy_is_not_solved_from(self):
        mostly_opus = stretch("2026-09-10T00:00:00+00:00",
                              {"claude-opus-5": tok(input=1_000_000),
                               "claude-fable-5-1": tok(input=10)})
        out = self.solve(jwork=[mostly_opus])
        self.assertEqual(out["per_account"]["a2"]["output_3x"]["n"], 0)
        self.assertEqual(out["unresolved"], "no Fable-heavy stretch to solve a rate from")

    def test_the_rule_names_no_account_and_publishes_none(self):
        out = self.solve(jwork=[fable_stretch("2026-09-10T00:00:00+00:00", 2_000_000)],
                         masterrig=[fable_stretch("2026-09-10T00:00:00+00:00", 800_000)])
        self.assertEqual(sorted(out["per_account"]), ["a1", "a2", "a3", "a4"])
        for name in ("jwork", "dave", "masterrig", "avis"):
            self.assertNotIn(name, json.dumps(out))

    def test_with_no_measured_window_there_is_nothing_to_solve_against(self):
        clean = C.clean_stretches({"jwork": [fable_stretch("2026-09-10T00:00:00+00:00", 2_000_000)]}, [])
        out = C.fable_interval(clean, CREDITS, None, LABELS)
        self.assertIsNone(out["input_low"])
        self.assertEqual(out["unresolved"], "no Fable-heavy stretch to solve a rate from")


class SolvedFableCarryThroughTests(unittest.TestCase):
    """A publish whose fixture has both a pure-Opus cluster and Fable-heavy stretches."""

    def setUp(self):
        gs = report("jwork", [opus_stretch("2026-09-05T00:00:00+00:00", 200_000),
                              fable_stretch("2026-09-10T00:00:00+00:00", 2_000_000)])
        masterrig = report("masterrig", [fable_stretch("2026-09-10T00:00:00+00:00", 800_000)])
        self.credits = _published(gs=gs, masterrig=masterrig)["credits"]

    def test_the_window_is_the_pure_opus_cluster_alone(self):
        self.assertEqual(self.credits["window_credits"]["value"], 20_000_000)
        self.assertEqual(self.credits["window_credits"]["n"], 1)

    def test_the_pooled_rate_is_the_stated_figure_and_the_solve_is_kept_as_working(self):
        fable = self.credits["fable_interval"]
        row = C.load_model_rates()["per_family"]["fable"]
        self.assertEqual((fable["input_low"], fable["input_high"]),
                         (round(row["interval"][0], 4), round(row["interval"][1], 4)))
        self.assertEqual(fable["input"], round(row["input"], 4))
        self.assertEqual(fable["status"], "measured by one fit pooled across every account")
        # The page prints "solved against a window of ..." only when this key holds a number.
        self.assertNotIn("window_credits_per_pct", fable)
        self.assertEqual((fable["solved"]["input_low"], fable["solved"]["input_high"]), (1.0, 2.5))
        self.assertEqual(self.credits["rates"]["per_family"]["fable"]["interval"], fable)

    def test_tokens_per_window_carries_the_measured_value_and_interval(self):
        """The row's rate is the measured one; the publish-time solve is published beside it.

        Fable's three per-account fits disagree, so the measured source is the median of them
        with the union of their intervals as its interval, `status` null and `agree` false --
        not a status sentence in place of a number. The solve of the same rate from the
        Fable-heavy stretches is still published, as `fable_interval` and in the rate table.
        """
        row = self.credits["per_model"]["fable"]
        rate = C.family_rate("fable", CREDITS, C.load_model_rates())
        self.assertEqual(row["credits_per_token"]["input"], rate.input)
        self.assertEqual(row["rate_source"], "measured")
        self.assertEqual(row["credits_per_token_interval"]["input"], list(rate.input_interval))
        figure = row["tokens_per_window"]["input"]
        self.assertEqual(figure["value"], round(20_000_000 / rate.input))
        self.assertIsNone(figure["status"])
        # Cheapest rate against the top of the window's range, dearest against the bottom.
        self.assertEqual(figure["interval"], [round(20_000_000 / rate.input_interval[1]),
                                              round(20_000_000 / rate.input_interval[0])])
        self.assertEqual((self.credits["fable_interval"]["solved"]["input_low"],
                          self.credits["fable_interval"]["solved"]["input_high"]), (1.0, 2.5))

    def test_the_api_value_and_the_session_count_are_values_too(self):
        rate = C.family_rate("fable", CREDITS, C.load_model_rates())
        tokens = round(20_000_000 / rate.input)
        api = self.credits["per_model"]["fable"]["api_value_per_window_usd"]["input"]
        self.assertIsNotNone(api["value"])
        self.assertEqual(api["value"], round(tokens * PRICES["claude-fable-5-1"]["input"] / 1e6, 2))
        sessions = self.credits["sessions"].get("claude-fable-5-1")
        if sessions is not None:
            self.assertIsNotNone(sessions["per_window"]["value"])


POOLED = {"anchor": {"family": "opus", "input": 10 / 15, "output": 50 / 15},
          "pooled_fit": {"times_opus": {"opus": 1.0, "sonnet": 0.5, "fable": 2.0},
                         "cache_read_weight": 0.01, "output_multiplier": 5},
          "per_family": {"fable": {"input": 2.0 * 10 / 15, "interval": [1.8 * 10 / 15, 2.2 * 10 / 15],
                                   "output_multiplier": 5}}}


class PooledValueTests(unittest.TestCase):
    """`across_cut_value`, the valuation the account windows compare accounts with: with a
    pooled fit on record, every family at the fit's rate."""

    def test_every_family_and_every_cache_read_is_priced_at_the_pooled_fit(self):
        # 1,000,000 Opus input + 100,000 Fable output + 1,000,000 Sonnet input + 10,000,000
        # cache reads: (1.0 x 1e6 + 2.0 x 5 x 1e5 + 0.5 x 1e6 + 0.01 x 1e7) x 10/15 credits.
        tokens = {"claude-opus-5": tok(input=1_000_000, cache_read=10_000_000),
                  "claude-fable-5-1": tok(output=100_000),
                  "claude-sonnet-5": tok(input=1_000_000)}
        credits = (1_000_000 + 2.0 * 5 * 100_000 + 0.5 * 1_000_000 + 0.01 * 10_000_000) * 10 / 15
        self.assertAlmostEqual(C.across_cut_value(CREDITS, model_rates=POOLED)(tokens), credits)

    def test_a_family_the_fit_has_no_rate_for_leaves_its_stretch_unpriced(self):
        tokens = {"claude-opus-5": tok(input=1_000_000), "claude-haiku-4-5": tok(input=10)}
        self.assertIsNone(C.across_cut_value(CREDITS, model_rates=POOLED)(tokens))

    def test_without_a_pooled_fit_the_held_fable_rate_still_prices(self):
        value = C.across_cut_value(CREDITS, model_rates={})
        self.assertAlmostEqual(value({"claude-opus-5": tok(input=3_000_000)}), 3_000_000 * 10 / 15)
        self.assertIsNotNone(value({"claude-fable-5-1": tok(input=1_000)}))


class PooledFableIntervalTests(unittest.TestCase):
    def test_the_pooled_rate_replaces_the_solve_as_the_stated_figure(self):
        clean = C.clean_stretches({"jwork": [fable_stretch("2026-09-10T00:00:00+00:00", 2_000_000)]}, [])
        out = C.fable_interval(clean, CREDITS, 200_000, LABELS, model_rates=POOLED)
        self.assertEqual((out["input_low"], out["input_high"]), (1.2, 1.4667))
        self.assertEqual(out["input"], 1.3333)
        self.assertEqual(out["times_opus"], [1.8, 2.2])
        self.assertEqual(out["solved"]["per_account"]["a2"]["output_5x"]["p25"], 1.0)
        self.assertNotIn("window_credits_per_pct", out)

    def test_a_fable_row_without_a_value_leaves_the_solve_as_the_figure(self):
        clean = C.clean_stretches({"jwork": [fable_stretch("2026-09-10T00:00:00+00:00", 2_000_000)]}, [])
        rates = {**POOLED, "per_family": {"fable": {"input": None, "interval": None}}}
        out = C.fable_interval(clean, CREDITS, 200_000, LABELS, model_rates=rates)
        self.assertEqual(out["status"], "interval, not yet separable")
        self.assertNotIn("solved", out)


class RateFitSelectionTests(unittest.TestCase):
    """The one selection the rate fits and the before-and-after comparison share."""

    def _s(self, start, end, **fields):
        return dict(stretch(start, {"claude-opus-5": tok(input=1_000_000)}), end=end, **fields)

    def test_a_stretch_that_spans_the_cut_is_in_neither_side_on_any_account(self):
        spans = self._s("2026-09-12T22:51:00+00:00", "2026-09-18T02:52:00+00:00")
        before = self._s("2026-09-12T00:00:00+00:00", "2026-09-12T05:00:00+00:00")
        after = self._s("2026-09-15T00:00:00+00:00", "2026-09-15T05:00:00+00:00")
        kept = C.rate_fit_stretches({"jwork": [spans, before, after],
                                     "masterrig": [spans, before, after]}, [])
        for account in ("jwork", "masterrig"):
            self.assertEqual([s["start"] for s in kept[account]], [before["start"], after["start"]])

    def test_masterrig_counts_from_six_september_and_takes_the_capture_test(self):
        early = self._s("2026-09-04T00:00:00+00:00", "2026-09-04T05:00:00+00:00")
        late = self._s("2026-09-07T00:00:00+00:00", "2026-09-07T05:00:00+00:00")
        unaccounted = dict(late, capture_status="unaccounted")
        kept = C.rate_fit_stretches({"masterrig": [early, late, unaccounted],
                                     "jwork": [early]}, [])
        self.assertEqual(kept["masterrig"], [late])
        self.assertEqual(kept["jwork"], [early])


class WindowSelectionTests(unittest.TestCase):
    """The window cluster's selection reads each account over its weeks' own history (rule 18)."""

    def _s(self, start, end, **fields):
        return dict(stretch(start, {"claude-opus-5": tok(input=1_000_000)}), end=end, **fields)

    def test_masterrig_counts_from_six_september_as_its_seven_day_steps_do(self):
        phantom = self._s("2026-09-02T01:41:00+00:00", "2026-09-02T14:26:00+00:00")
        august = self._s("2026-08-20T00:00:00+00:00", "2026-08-20T05:00:00+00:00")
        late = self._s("2026-09-07T00:00:00+00:00", "2026-09-07T05:00:00+00:00")
        kept = C.window_stretches({"masterrig": [august, phantom, late], "jwork": [phantom]}, [])
        self.assertEqual(kept["masterrig"], [late])
        self.assertEqual(kept["jwork"], [phantom])

    def test_the_gate_is_the_one_the_rows_week_reads_its_steps_through(self):
        from tracker import weekly_meter
        spans = [("2026-08-20T00:00:00+00:00", "2026-08-20T05:00:00+00:00"),
                 ("2026-09-02T01:41:00+00:00", "2026-09-02T14:26:00+00:00"),
                 ("2026-09-05T22:00:00+00:00", "2026-09-06T02:00:00+00:00"),
                 ("2026-09-07T00:00:00+00:00", "2026-09-07T05:00:00+00:00")]
        for start, end in spans:
            kept = C.window_stretches({"masterrig": [self._s(start, end)]}, [])["masterrig"]
            steps = weekly_meter.clean_steps({"masterrig": [{"start": start, "end": end}]},
                                             [])["masterrig"]
            self.assertEqual(bool(kept), bool(steps), start)

    def test_it_keeps_the_capture_test_and_every_other_account_as_before(self):
        early = self._s("2026-08-20T00:00:00+00:00", "2026-08-20T05:00:00+00:00")
        unaccounted = dict(early, capture_status="unaccounted")
        by_account = {"jwork": [early, unaccounted], "dave": [early]}
        self.assertEqual(C.window_stretches(by_account, []),
                         C.clean_stretches(by_account, [], require="capture_status"))


def _published(gs=None, masterrig=None, passive=None, effort_meta=None, prices=None,
               model_rates=None, four_accounts_run=None):
    """A public JSON built over planted stretches, with everything else minimal."""
    passive = passive or {"split": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
                          "session_tokens": {"claude-opus-5": 1_000_000}}
    return build_public_json([], passive, {}, prices or PRICES, NOW,
                             gs_passive=gs, masterrig_passive=masterrig,
                             effort_meta=effort_meta, credits=CREDITS, model_rates=model_rates,
                             four_accounts_run=four_accounts_run)


class HarnessRunFileTests(unittest.TestCase):
    """The runs come from history/harness-runs.jsonl and from nowhere else.

    A probe writes a history/probes.jsonl row only when it finishes, so the old source
    could not see a probe that aborted or crashed part way -- and one of those sent 80
    prompts. tools/harness_runs.py collects every run into one file; this module reads it.
    """

    ROWS: ClassVar[list] = [
        {"account": "jwork", "start": "2026-09-09T11:28:37+00:00", "end": "2026-09-09T14:53:22+00:00",
         "kind": "effort-matrix", "outcome": "completed"},
        {"account": "dave", "start": "2026-09-09T00:02:08+00:00", "end": "2026-09-09T00:12:05+00:00",
         "kind": "probe", "outcome": "completed", "model": "claude-fable-5-1", "effort": "low"},
        {"account": "jwork", "start": "2026-09-15T00:03:13+00:00", "end": "2026-09-15T02:13:32+00:00",
         "kind": "probe", "outcome": "aborted", "detail": "no second tick after 80 prompts"},
    ]

    def runs(self, rows=None):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harness-runs.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in
                                    (self.ROWS if rows is None else rows)), encoding="utf-8")
            return C.harness_runs(path)

    def test_every_row_becomes_a_run_on_its_own_account(self):
        self.assertEqual([(r.account, r.start.isoformat()) for r in self.runs()],
                         [("dave", "2026-09-09T00:02:08+00:00"),
                          ("jwork", "2026-09-09T11:28:37+00:00"),
                          ("jwork", "2026-09-15T00:03:13+00:00")])

    def test_an_aborted_probe_excludes_a_stretch_the_old_source_could_not_see(self):
        # The run is in no probes.jsonl row: it never finished one.
        overlapping = opus_stretch("2026-09-15T01:00:00+00:00", 200_000, end="2026-09-15T01:30:00+00:00")
        clean = C.clean_stretches({"jwork": [overlapping]}, self.runs())
        self.assertEqual(clean["jwork"], [])
        self.assertEqual(len(C.clean_stretches({"jwork": [overlapping]}, [])["jwork"]), 1)

    def test_the_reason_says_what_ran_and_how_it_ended(self):
        reasons = [r.reason for r in self.runs()]
        self.assertEqual(reasons[0], "probe claude-fable-5-1/low completed from 2026-09-09T00:02:08Z")
        self.assertIn("aborted: no second tick after 80 prompts", reasons[2])

    def test_a_row_without_a_span_or_an_account_is_not_a_run(self):
        self.assertEqual(self.runs([{"account": "jwork", "start": "2026-09-15T00:03:13+00:00"},
                                    {"start": "2026-09-15T00:03:13+00:00",
                                     "end": "2026-09-15T02:13:32+00:00"}]), [])

    def test_a_missing_file_excludes_nothing_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(C.harness_runs(Path(d) / "absent.jsonl"), [])

    def test_the_committed_file_holds_the_runs_the_probe_rows_hold_and_more(self):
        root = Path(__file__).resolve().parent.parent
        if not (root / "history" / "harness-runs.jsonl").exists():
            self.skipTest("no committed history/harness-runs.jsonl")
        runs = C.harness_runs()
        spans = {(r.account, r.start.isoformat()) for r in runs}
        probes = [json.loads(line) for line in
                  (root / "history" / "probes.jsonl").read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        for row in probes:
            self.assertIn((row["account"], datetime.fromisoformat(row["ts"]).isoformat()), spans)
        self.assertGreater(len(runs), len(probes))


#: Opus 5.5's row as #93 committed it to history/model-rates.json (c182e29): an 80% interval
#: 1.75x wide end to end, over the 1.5x published-rate rule and inside the provisional limit.
OPUS_5_5_PROVISIONAL_FIT = {
    "input": 0.5822334873013093, "interval": [0.4177156966837888, 0.7295185922968599],
    "output_multiplier": 5,
    "status": ("provisional: Opus 5.5 0.873x Opus, 80% interval [0.627x, 1.094x], 1.75x wide end"
               " to end; a rate is final when its interval is under 1.5x"),
    "rate_source": "measured", "anchor": False, "provisional": True, "reference_input": None,
    "times_opus": 0.873350230951964, "times_opus_interval": [0.6265735450256832, 1.09427788844529],
    "inferred": {"input": 0.5333333333333333, "times_opus": 0.8, "output_multiplier": 5,
                 "inferred_from": "list_price"},
}


class PublishedBlockTests(unittest.TestCase):
    """What build_public_json writes: the block, and what it refuses to write."""

    def setUp(self):
        self.gs = report("jwork", [opus_stretch(f"2026-09-{5 + i:02d}T00:00:00+00:00", level)
                                   for i, level in enumerate((190_000, 200_000, 210_000))])
        self.j = _published(gs=self.gs)
        self.credits = self.j["credits"]

    def test_the_window_is_published_with_its_interval_count_accounts_and_method(self):
        window = self.credits["window_credits"]
        self.assertEqual(window["value"], 20_000_000)
        self.assertEqual(window["interval"], [19_000_000, 21_000_000])
        self.assertEqual(window["n"], 3)
        self.assertEqual(window["accounts"]["a2"]["n"], 3)
        self.assertTrue(window["method"])

    def test_tokens_per_window_is_the_window_divided_by_the_models_own_rate(self):
        opus = self.credits["per_model"]["opus"]["tokens_per_window"]
        self.assertEqual(opus["input"]["value"], round(20_000_000 / OPUS_IN))
        self.assertEqual(opus["output"]["value"], round(20_000_000 / OPUS_OUT))
        self.assertEqual(opus["input"]["interval"],
                         [round(19_000_000 / OPUS_IN), round(21_000_000 / OPUS_IN)])

    def test_api_value_is_those_same_tokens_at_the_dollar_tables_list_price(self):
        api = self.credits["per_model"]["opus"]["api_value_per_window_usd"]["input"]
        self.assertEqual(api["value"], round(20_000_000 / OPUS_IN * 5 / 1e6, 2))

    def test_a_family_with_no_dollar_row_publishes_null_with_a_status(self):
        haiku = self.credits["per_model"]["haiku"]
        self.assertIsNone(haiku["api_value_per_window_usd"]["input"]["value"])
        self.assertIn("no row in the dollar table", haiku["api_value_per_window_usd"]["input"]["status"])

    def test_a_family_the_fit_cannot_measure_is_shown_at_an_inferred_rate(self):
        """Haiku is not measurable, and the page shows it anyway, marked as inferred.

        The row carries exactly the figures a measured row does, computed from the reference
        table's Haiku rate, with `rate_source` "inferred", no interval and no status. What the
        fit said -- the not-measurable sentence and its own point estimate -- stays under
        `measured_rate`, so a reader sees both.
        """
        haiku = self.credits["per_model"]["haiku"]
        mr = C.load_model_rates()["per_family"]["haiku"]
        self.assertTrue(mr["status"].startswith("not measurable"), mr["status"])
        self.assertEqual(haiku["rate_source"], "inferred")
        self.assertEqual(haiku["inferred_from"], "reference_table")
        self.assertEqual(haiku["measured_rate"]["inferred_from"], "reference_table")
        self.assertEqual(haiku["measured_rate"]["measured_status"], mr["status"])
        self.assertIsNone(haiku["status"])
        self.assertEqual(haiku["credits_per_token"]["input"], 2 / 15)
        self.assertAlmostEqual(haiku["credits_per_token"]["output"], 10 / 15)
        self.assertIsNone(haiku["credits_per_token_interval"])
        for side in ("input", "output"):
            self.assertIsNotNone(haiku["tokens_per_window"][side]["value"])
        if "claude-haiku-4-5" in PRICES:  # the fixture's dollar table may carry no Haiku row
            self.assertIsNotNone(haiku["api_value_per_window_usd"]["input"]["value"])
        self.assertEqual(haiku["reference_rate"]["input"], 2 / 15)

    def test_opus_5_5_publishes_its_fitted_rate_as_provisional(self):
        # A fit whose interval sits between the published-rate rule and the provisional limit
        # -- the fit #93 committed, OPUS_5_5_PROVISIONAL_FIT -- publishes the fitted value, its
        # interval, and a status sentence that says it is provisional; the list-price rate
        # stays in model-rates.json. The committed file stopped being that case when the
        # 2026-09-25 daily refit narrowed the interval under 1.5x, so the case is planted.
        model_rates = C.load_model_rates()
        model_rates = {**model_rates, "per_family": {**model_rates["per_family"],
                                                     "opus-5-5": OPUS_5_5_PROVISIONAL_FIT}}
        rates = model_rates["per_family"]["opus-5-5"]
        row = _published(gs=self.gs, model_rates=model_rates)["credits"]["per_model"]["opus-5-5"]
        self.assertTrue(rates["provisional"])
        self.assertEqual(row["rate_source"], "measured")
        self.assertTrue(row["measured_rate"]["provisional"])
        self.assertTrue(row["status"].startswith("provisional: Opus 5.5 "))
        self.assertAlmostEqual(row["credits_per_token"]["input"], rates["input"])
        self.assertAlmostEqual(rates["inferred"]["times_opus"], 0.8)

    def test_a_family_that_passes_the_rule_is_measured_by_itself(self):
        rates = C.load_model_rates()
        row = dict(rates["per_family"]["haiku"], input=0.1, interval=[0.09, 0.11], status=None)
        rate = C.family_rate("haiku", CREDITS, {**rates, "per_family": {"haiku": row}})
        self.assertEqual(rate.rate_source, "measured")
        self.assertEqual(rate.input, 0.1)

    def test_every_per_model_row_says_where_its_rate_came_from(self):
        for fam, row in self.credits["per_model"].items():
            self.assertIn(row["rate_source"], ("measured", "reference", "inferred"), fam)
            self.assertIn("reference_rate", row, fam)
        # Opus alone reads `reference`: it is the unit anchor, and nothing here tests it.
        sources = {fam: row["rate_source"] for fam, row in self.credits["per_model"].items()}
        self.assertEqual(sources["opus"], "reference")
        self.assertTrue(self.credits["per_model"]["opus"]["anchor"])
        self.assertEqual({fam for fam, v in sources.items() if v == "measured"}, {"sonnet", "fable", "opus-5-5"})
        self.assertEqual({fam for fam, v in sources.items() if v == "inferred"}, {"haiku"})
        # Only an inferred row names what it was inferred from.
        for fam, row in self.credits["per_model"].items():
            self.assertEqual(row["inferred_from"] is not None, sources[fam] == "inferred", fam)

    def test_the_sonnet_row_divides_by_the_measured_rate_not_the_tables(self):
        rate = C.family_rate("sonnet", CREDITS, C.load_model_rates())
        row = self.credits["per_model"]["sonnet"]
        self.assertEqual(row["credits_per_token"]["input"], rate.input)
        self.assertEqual(row["reference_rate"]["input"], 6 / 15)
        self.assertNotEqual(row["credits_per_token"]["input"], row["reference_rate"]["input"])
        self.assertEqual(row["tokens_per_window"]["input"]["value"],
                         round(20_000_000 / rate.input))
        self.assertEqual(row["tokens_per_window"]["input"]["interval"],
                         [round(19_000_000 / rate.input_interval[1]),
                          round(21_000_000 / rate.input_interval[0])])

    def test_the_sessions_block_uses_the_same_measured_rates(self):
        passive = {"split": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
                   "session_tokens": {"claude-sonnet-5": 1_000_000}}
        sessions = _published(gs=self.gs, passive=passive)["credits"]["sessions"]["claude-sonnet-5"]
        rate = C.family_rate("sonnet", CREDITS, C.load_model_rates())
        self.assertEqual(sessions["rate_source"], "measured")
        # All cache writes, so a token costs the input rate and the division is exact.
        self.assertEqual(sessions["per_window"]["value"], round(20_000_000 / rate.input / 1e6, 1))
        self.assertEqual(sessions["credits_per_token_at_split_interval"],
                         [round(rate.input_interval[0], 8), round(rate.input_interval[1], 8)])

    def test_a_publish_with_no_measured_rate_source_states_no_rate_but_the_anchor(self):
        """An archive or a fixture without history/model-rates.json publishes no rate it cannot
        measure, and the anchor -- which is what a credit means -- still stands."""
        credits = build_public_json([], {"split": {"cache_write": 1.0}, "session_tokens": {}}, {},
                                    PRICES, NOW, gs_passive=self.gs, credits=CREDITS,
                                    model_rates={})["credits"]
        opus = credits["per_model"]["opus"]
        self.assertEqual(opus["rate_source"], "reference")
        self.assertEqual(opus["tokens_per_window"]["input"]["value"], round(20_000_000 / OPUS_IN))
        sonnet = credits["per_model"]["sonnet"]
        self.assertIsNone(sonnet["tokens_per_window"]["input"]["value"])
        self.assertIn("no measured rate source", sonnet["status"])
        self.assertIn("no measured rate source", credits["measured_rates"]["status"])

    def test_with_no_fable_heavy_stretch_the_solve_is_null_and_says_why(self):
        """The fixture is pure Opus, so there is nothing to solve a Fable rate from.

        The solve and the fit are two instruments. The solve has nothing to work on here and
        says so; the row's rate comes from the committed fit, which is measured over other
        stretches and does not depend on this fixture. The committed pooled fit measures Fable
        from every group at once, so the row and `fable_interval` both carry its value and
        interval, and the empty solve is kept beside them as working.
        """
        fable = self.credits["fable_interval"]
        self.assertIsNone(fable["solved"]["input_low"])
        self.assertIsNone(fable["solved"]["input_high"])
        self.assertEqual(fable["solved"]["unresolved"], "no Fable-heavy stretch to solve a rate from")
        self.assertIsNone(fable["unresolved"])
        row = self.credits["per_model"]["fable"]
        self.assertIsNotNone(row["tokens_per_window"]["input"]["value"])
        self.assertIsNone(row["status"])
        rate = C.family_rate("fable", CREDITS, C.load_model_rates())
        self.assertGreaterEqual(rate.detail["n_fits"], 2)
        self.assertIn("one fit pooled over", rate.detail["why"])
        self.assertTrue(rate.detail["per_fit"])

    def test_the_fable_session_count_is_an_interval_too(self):
        sessions = self.credits["sessions"].get("claude-fable-5-1")
        if sessions is not None:  # only when the fixture's session_tokens name Fable
            self.assertIsNone(sessions["per_window"]["value"])

    def test_sessions_publish_the_cache_split_they_assume(self):
        opus = self.credits["sessions"]["claude-opus-5"]
        self.assertTrue(opus["cache_normalised"])
        self.assertEqual(opus["split"], {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0})
        self.assertIn("passive.json", opus["split_source"])
        # All cache writes, so a token costs the input rate and the division is exact.
        self.assertEqual(opus["per_window"]["value"], round(20_000_000 / OPUS_IN / 1_000_000, 1))

    def test_sessions_per_week_is_null_when_no_windows_per_week_are_measured(self):
        per_week = self.credits["sessions"]["claude-opus-5"]["per_week"]
        self.assertIsNone(per_week["value"])
        self.assertIn("windows per week", per_week["status"])

    def test_the_dollar_derived_figures_stay_and_say_they_are_the_legacy_route(self):
        rate = self.j["rates"]["claude-opus-5"]
        self.assertEqual(rate["derivation"], "list-price dollars, legacy")
        self.assertEqual(rate["credits_family"], "opus")
        self.assertIn("tokens_per_window", rate)
        self.assertIn("meter_budget_per_window", rate)

    def test_the_new_figures_say_they_came_from_credits(self):
        self.assertEqual(self.credits["derivation"], "credits")
        self.assertEqual(self.credits["window_credits"]["derivation"], "credits")
        self.assertEqual(self.credits["per_model"]["opus"]["derivation"], "credits")

    def test_with_no_history_at_all_every_figure_is_null_with_a_status(self):
        credits = _published()["credits"]
        window = credits["window_credits"]
        self.assertIsNone(window["value"])
        self.assertEqual(window["n"], 0)
        self.assertTrue(window["status"])
        self.assertIsNone(credits["per_model"]["opus"]["tokens_per_window"]["input"]["value"])
        self.assertTrue(credits["per_model"]["opus"]["tokens_per_window"]["input"]["status"])

    def test_the_cross_check_on_the_announced_caps_is_retired(self):
        # It paired the announced 14 September caps with the regimes either side of a
        # hand-dated cut; regimes open only at the detector's measured changes now.
        block = self.credits["window_credits_from_weekly"]
        self.assertEqual(block["kind"], "cross_check")
        self.assertEqual((block["before"], block["after"]), (None, None))
        self.assertIn("retired", block["status"])

    def test_the_third_account_joins_the_cluster_when_its_capture_is_accepted(self):
        masterrig = report("masterrig", [opus_stretch("2026-09-06T00:00:00+00:00", 150_000)])
        window = _published(gs=self.gs, masterrig=masterrig)["credits"]["window_credits"]
        self.assertEqual(window["n"], 4)
        self.assertEqual(window["accounts"]["a1"]["n"], 1)

    def test_a_third_account_with_no_usable_capture_column_stays_out_of_the_cluster(self):
        blind = report("masterrig", [opus_stretch("2026-09-06T00:00:00+00:00", 9_000,
                                                  capture_status="surplus", capture=None)])
        window = _published(gs=self.gs, masterrig=blind)["credits"]["window_credits"]
        self.assertEqual(window["n"], 3)
        self.assertEqual(window["accounts"]["a1"]["n"], 0)
        self.assertEqual(window["value"], 20_000_000)

    def test_the_window_reads_the_third_account_over_its_weeks_own_history(self):
        """ADR 0001 rule 18: a stretch from before MASTERRIG_FROM prices no window regime.

        On 2 September the takeoff pipeline was running on the account; one capture-accepted
        pure-Opus stretch from that day read 86k credits per 1% against 176k-208k on the other
        account, and as the before window's lowest reading it set the first windows-per-week
        row's upper end at 20.75 against a level of 5.11.
        """
        masterrig = report("masterrig", [opus_stretch("2026-09-02T02:00:00+00:00", 86_000),
                                         opus_stretch("2026-09-06T00:00:00+00:00", 150_000)])
        credits = _published(gs=self.gs, masterrig=masterrig)["credits"]
        window = credits["window_credits"]
        self.assertEqual(window["n"], 4)
        self.assertEqual(window["accounts"]["a1"]["n"], 1)
        self.assertEqual(window["accounts"]["a1"]["interval"], [15_000_000, 15_000_000])
        self.assertEqual(credits["window_tokens"]["accounts"]["a1"]["n"], 1)
        self.assertIn("from 6 September", window["method"])

    def test_no_account_name_reaches_the_public_json(self):
        masterrig = report("masterrig", [opus_stretch("2026-09-06T00:00:00+00:00", 150_000)])
        text = json.dumps(_published(gs=self.gs, masterrig=masterrig))
        for name in ("jwork", "dave", "masterrig", "avis"):
            self.assertNotIn(name, text)


class EffortCacheMixTests(unittest.TestCase):
    META: ClassVar[dict] = {"runs": {"claude-sonnet-5/low": [{"input": 2, "output": 100, "cache_read": 400,
                                              "cache_write": 498, "total": 1000},
                                             {"input": 2, "output": 100, "cache_read": 898,
                                              "cache_write": 0, "total": 1000}]}}

    def test_each_cell_carries_the_cache_read_share_of_its_own_runs(self):
        mix = _published(effort_meta=self.META)["credits"]["effort_cache_mix"]
        self.assertAlmostEqual(mix["claude-sonnet-5"]["low"]["cache_read_share"], 0.649)
        self.assertEqual(mix["claude-sonnet-5"]["low"]["runs"], 2)

    def test_the_cold_runs_are_counted_so_an_inverted_row_can_be_read(self):
        mix = _published(effort_meta=self.META)["credits"]["effort_cache_mix"]
        self.assertEqual(mix["claude-sonnet-5"]["low"]["cold_cache_runs"], 1)

    def test_the_real_matrix_shows_four_of_seven_cold_runs_on_the_sonnet_low_cell(self):
        """The inversion the block exists to make visible: low dearer than medium."""
        meta = json.loads(Path("data/effort_matrix.json").read_text())["_meta"]
        mix = _published(effort_meta=meta)["credits"]["effort_cache_mix"]["claude-sonnet-5"]
        self.assertEqual(mix["low"]["cold_cache_runs"], 4)
        self.assertEqual(mix["low"]["runs"], 7)
        self.assertLess(mix["medium"]["cold_cache_runs"], mix["low"]["cold_cache_runs"])

    def test_a_5_5_cell_publishes_exactly_as_the_older_cells_do(self):
        """Sonnet 5.5 and Opus 5.5 joined the matrix on 2026-10-08 beside the Sonnet 5 and Opus 5 cells.

        Each gets a cache-mix and a credits cell from its own runs, at its own family's rate
        (Sonnet 5.5 is a family of its own, tracker/credits.py auto_family), and the older
        cells stay in the block.
        """
        runs = self.META["runs"]["claude-sonnet-5/low"]
        meta = {"runs": {f"{m}/low": runs for m in ("claude-sonnet-5", "claude-sonnet-5-5",
                                                    "claude-opus-5", "claude-opus-5-5")}}
        credits = _published(effort_meta=meta)["credits"]
        for model in ("claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5", "claude-opus-5-5"):
            mix = credits["effort_cache_mix"][model]["low"]
            self.assertEqual((mix["runs"], mix["cold_cache_runs"]), (2, 1), model)
            self.assertAlmostEqual(mix["cache_read_share"], 0.649, msg=model)
            cell = credits["effort_credits"][model]["low"]
            self.assertEqual(cell["runs"], 2, model)
            self.assertIsNotNone(cell["median_credits"]["value"], model)
            self.assertIn("percent_of_window", cell)
        self.assertEqual(C.family("claude-sonnet-5-5", CREDITS), "sonnet-5-5")
        self.assertEqual(C.family("claude-opus-5-5", CREDITS), "opus-5-5")


class EventAndReferenceTests(unittest.TestCase):
    def setUp(self):
        self.j = _published(gs=report("jwork", [opus_stretch("2026-09-05T00:00:00+00:00", 200_000)]))

    def test_the_reference_is_dated_january_so_the_dashed_line_can_carry_its_date(self):
        ref = self.j["reference"]
        self.assertEqual(ref["as_of"], "2026-01-25")
        self.assertTrue(ref["dated"])
        self.assertEqual(ref["credits_per_window"]["max20"], 11_000_000)

    def test_every_change_since_the_reference_is_listed_with_what_it_multiplied(self):
        changes = {(c["scope"], c["multiplier"]): c for c in self.j["reference"]["changes_since"]}
        self.assertEqual(changes[("five_hour_window", 2.0)]["date"], "2026-05-06")
        self.assertFalse(changes[("weekly", 1.5)]["date_known"])
        self.assertEqual(changes[("weekly", 1.25)]["date"], "2026-09-14")

    def test_each_change_carries_the_sentence_it_was_read_from(self):
        for change in self.j["reference"]["changes_since"]:
            self.assertTrue(change["quote"])
            self.assertTrue(change["source"])

    def test_the_announcement_is_quoted_and_marked_as_announced_not_measured(self):
        announced = C.ANNOUNCEMENT
        self.assertEqual(announced["announced_change_pct"], -17)
        self.assertIn("17% reduction in weekly limits", announced["quote"])

    def test_the_weekly_event_row_states_only_the_measured_quantity(self):
        """PR #62's wording, computed from the event's own percent and dates."""
        weekly = [e for e in self.j["events"] if e["scope"] == "weekly"]
        for event in weekly:
            self.assertTrue(event["label"].startswith("Observed windows per week"))
            self.assertNotIn("weekly cap", event["label"])
            self.assertNotIn("17", event["label"])

    def test_a_weekly_event_carries_the_announcement_beside_it_never_inside_percent(self):
        for event in (e for e in self.j["events"] if e["scope"] == "weekly"):
            self.assertEqual(event["announced"]["announced_change_pct"], -17)
            self.assertNotEqual(event["percent"], 17)
            self.assertEqual(event["meter_attribution"], "unresolved")
            self.assertFalse(event["five_hour_window_credits"]["resolved"])


def _files(root: Path, gs: dict, masterrig: dict, passive: dict, matrix: dict,
           model_rates: dict | None = None) -> dict:
    (root / "history").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "history/gs-passive.json").write_text(json.dumps(gs))
    (root / "history/masterrig-passive.json").write_text(json.dumps(masterrig))
    (root / "history/passive.json").write_text(json.dumps(passive))
    (root / "history/probes.jsonl").write_text("")
    (root / "data/effort_matrix.json").write_text(json.dumps(matrix))
    (root / "data/prices.json").write_text(json.dumps({**PRICES, "_credits": CREDITS}))
    # The measured rates the publisher divides by, copied so the check reads a file of its
    # own rather than the checkout's -- which is what lets a test move a rate and see the
    # check notice.
    rates = json.loads(C.MODEL_RATES_PATH.read_text()) if model_rates is None else model_rates
    (root / "history/model-rates.json").write_text(json.dumps(rates))
    # The four-account detector's state is named too, and left absent: these fixtures state
    # no limit change. Unnamed, the publish and the check would both read the checkout's
    # committed history/four-accounts.json, whose changes the planted files know nothing of.
    return {"gs": root / "history/gs-passive.json",
            "masterrig": root / "history/masterrig-passive.json",
            "probes": root / "history/probes.jsonl",
            "effort_matrix": root / "data/effort_matrix.json",
            "passive": root / "history/passive.json",
            "prices": root / "data/prices.json",
            "model_rates": root / "history/model-rates.json",
            "four_accounts": root / "history/four-accounts.json"}


def _publish(paths: dict, now: datetime = NOW) -> dict:
    """A real publish over the files `_files` planted, through the publisher's own path.

    The published document and the check's rebuild then come off the same six files, so a
    test that plants a figure and expects the check to catch it is testing the check and
    not a difference between two ways of assembling a fixture.
    """
    return rebuild_public_json(now, probes=paths["probes"], passive=paths["passive"],
                               effort=paths["effort_matrix"], prices=paths["prices"],
                               gs_passive=paths["gs"], masterrig_passive=paths["masterrig"],
                               model_rates=paths["model_rates"],
                               four_accounts_state=paths["four_accounts"])


class PublishCheckTests(unittest.TestCase):
    """--publish-check: the published block against the same block rebuilt from the files."""

    GS: ClassVar[dict] = report("jwork", [opus_stretch("2026-09-05T00:00:00+00:00", 200_000),
                          opus_stretch("2026-09-06T00:00:00+00:00", 210_000)])
    PASSIVE: ClassVar[dict] = {"split": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
               "session_tokens": {"claude-opus-5": 1_000_000}}

    def published(self) -> dict:
        return _published(gs=self.GS, passive=self.PASSIVE)

    def run_check(self, paths: dict, root: Path, published: dict, tolerance: float = 0.005):
        """--publish-check over `published`, against the files `paths` names."""
        out = root / "claude-usage.json"
        out.write_text(json.dumps(published))
        args = argparse.Namespace(**paths, publish_check=out, tolerance=tolerance,
                                  contributed=root / "data/contributed.json")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = publish_check(args)
        return code, buf.getvalue()

    def check(self, published: dict, tolerance: float = 0.005, model_rates: dict | None = None):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = _files(root, self.GS, {}, self.PASSIVE, {}, model_rates)
            return self.run_check(paths, root, published, tolerance)

    def test_a_publish_reproduces_from_the_history_files(self):
        code, text = self.check(self.published())
        self.assertEqual(code, 0, text)
        self.assertIn("figures reproduce from the history files", text)

    def test_every_figure_is_printed_beside_its_recomputation(self):
        text = self.check(self.published())[1]
        self.assertIn("window_credits.value", text)
        self.assertIn("published", text)
        self.assertIn("recomputed", text)

    def test_a_deliberately_wrong_figure_fails_the_check(self):
        j = self.published()
        j["credits"]["window_credits"]["value"] = int(j["credits"]["window_credits"]["value"] * 1.02)
        code, text = self.check(j)
        self.assertEqual(code, 1)
        self.assertIn("window_credits.value", text)
        self.assertIn("do not reproduce", text)

    def test_a_figure_inside_the_tolerance_passes(self):
        j = self.published()
        j["credits"]["window_credits"]["value"] = int(j["credits"]["window_credits"]["value"] * 1.001)
        self.assertEqual(self.check(j)[0], 0)

    def test_a_changed_method_sentence_fails_as_well_as_a_changed_number(self):
        j = self.published()
        j["credits"]["window_credits"]["method"] = "trust me"
        self.assertEqual(self.check(j)[0], 1)

    def test_a_figure_the_publisher_never_wrote_fails(self):
        j = self.published()
        del j["credits"]["window_credits"]["n"]
        code, text = self.check(j)
        self.assertEqual(code, 1)
        self.assertIn("window_credits.n", text)

    def test_a_json_with_no_credits_block_is_a_failure_not_a_pass(self):
        self.assertEqual(self.check({"generated_at": NOW.isoformat()})[0], 1)

    def test_the_check_covers_the_measured_rate_fields_as_well_as_the_window(self):
        text = self.check(self.published())[1]
        for key in ("per_model.sonnet.rate_source", "per_model.sonnet.credits_per_token.input",
                    "per_model.sonnet.reference_rate.input", "per_model.haiku.status",
                    "measured_rates.per_family.sonnet.input",
                    "measured_rates.cache_read_weight.interval[0]",
                    "sessions.claude-opus-5.rate_source"):
            self.assertIn(key, text.replace("...", ""), key)

    def test_a_row_published_at_the_reference_rate_instead_of_the_measured_one_fails(self):
        """The defect this check exists for: a per-model figure divided by the January table."""
        j = self.published()
        row = j["credits"]["per_model"]["sonnet"]
        row["credits_per_token"]["input"] = 6 / 15
        row["tokens_per_window"]["input"]["value"] = round(20_000_000 / (6 / 15))
        code, text = self.check(j)
        self.assertEqual(code, 1)
        self.assertIn("per_model.sonnet.tokens_per_window.input.value", text)

    def test_a_measured_rate_the_committed_fit_does_not_hold_fails(self):
        """The rate is read from history/model-rates.json, never from the published JSON."""
        moved = json.loads(C.MODEL_RATES_PATH.read_text())
        row = moved["measured_rates"]["per_family"]["sonnet"]
        row["input"] = row["input"] * 1.5
        code, text = self.check(self.published(), model_rates=moved)
        self.assertEqual(code, 1)
        self.assertIn("per_model.sonnet.credits_per_token.input", text)

    def test_the_effort_matrix_in_credits_reproduces_too(self):
        matrix = {"_meta": {"runs": {"claude-opus-5/low": [
            {"input": 1000, "output": 100, "cache_read": 50_000, "cache_write": 2000, "total": 53_100}]}}}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = _files(root, self.GS, {}, self.PASSIVE, matrix)
            published = _publish(paths)
            cell = published["credits"]["effort_credits"]["claude-opus-5"]["low"]
            # (1000 + 2000) input-side tokens at 10/15 plus 100 output at 50/15.
            self.assertEqual(cell["median_credits"]["value"], round(3000 * OPUS_IN + 100 * OPUS_OUT))
            self.assertEqual(cell["rate_source"], "reference")
            code, text = self.run_check(paths, root, published)
        self.assertEqual(code, 0, text)
        self.assertIn("effort_credits.claude-opus-5.low.median_credits.value", text)


class PriceTableRoundTripTests(unittest.TestCase):
    """The credits block has to survive the publisher rewriting data/prices.json.

    tracker/weight.py rewrites the whole file whenever the output class weight moves.
    It loads the raw table and writes it back, so an underscore-prefixed block rides
    through -- but nothing said so, and a block silently dropped on a Sunday would take
    every credit figure on the page with it.
    """

    def test_the_credits_block_survives_a_weight_rewrite(self):
        from tracker.weight import _write
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "prices.json"
            raw = json.loads(Path("data/prices.json").read_text())
            _write(path, raw)
            self.assertEqual(json.loads(path.read_text())["_credits"], raw["_credits"])

    def test_the_credits_block_is_not_mistaken_for_a_model(self):
        """build_public_json is handed the table with underscore keys already filtered."""
        raw = json.loads(Path("data/prices.json").read_text())
        # Rows tracker/list_prices.py files from the pricing page (marked by
        # `list_price_source`) arrive with the daily publisher, not with code.
        priced = {k: v for k, v in raw.items() if not k.startswith("_") and "list_price_source" not in v}
        self.assertNotIn("_credits", priced)
        self.assertEqual(sorted(priced),
                         ["claude-fable-5-1", "claude-haiku-4-5", "claude-opus-4-7",
                          "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5",
                          "claude-sonnet-4-6", "claude-sonnet-5"])


class PublishCheckScopeTests(unittest.TestCase):
    """--publish-check covers the whole published file, not the credits block alone (wf-58 item 1).

    Until now the check rebuilt `credits` and printed "All N figures reproduce", while the
    headline percent, the onset dates, the windows per week, the rates, the history, the
    effort cells and both basis blocks -- all of them on the page -- were never compared to
    anything. Every one of them is a publisher-derived figure, and every one is now rebuilt
    through `tracker.publish.rebuild_public_json`, the same function the publisher runs.
    """

    GS: ClassVar[dict] = report("jwork", [opus_stretch("2026-09-05T00:00:00+00:00", 200_000),
                                          opus_stretch("2026-09-06T00:00:00+00:00", 210_000)])
    PASSIVE: ClassVar[dict] = {
        "generated_at": "2026-09-19T00:00:00+00:00",
        "split": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
        "session_tokens": {"claude-opus-5": 1_000_000},
        "weekly_windows": {"current": 6.46, "history": [], "by_window": [
            {"window_ending": f"2026-08-{d:02d}T22:00:00+00:00", "windows": 6.5,
             "five_hour_pct": 65.0, "seven_day_pct": 10.0, "pieces": 1, "reset_verified": True}
            for d in range(15, 27)] + [
            # The same level after the weekly change, so the newest regime has windows per
            # week of its own and the hero week (the newest per-week regime) is a product.
            {"window_ending": f"2026-09-{d:02d}T22:00:00+00:00", "windows": 6.5,
             "five_hour_pct": 65.0, "seven_day_pct": 10.0, "pieces": 1, "reset_verified": True}
            for d in range(15, 19)]},
    }

    def _root(self, stack, matrix=None):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        paths = _files(root, self.GS, {}, self.PASSIVE, matrix or {})
        return root, paths, _publish(paths)

    def check(self, mutate=None, matrix=None, write=None):
        with ExitStack() as stack:
            root, paths, published = self._root(stack, matrix)
            if write:
                write(root)
            if mutate:
                mutate(published)
            out = root / "claude-usage.json"
            out.write_text(json.dumps(published))
            args = argparse.Namespace(**paths, publish_check=out, tolerance=0.005,
                                      contributed=root / "data/contributed.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = publish_check(args)
            return code, buf.getvalue(), published

    def test_a_whole_publish_reproduces_and_the_count_is_the_whole_files(self):
        code, text, published = self.check()
        self.assertEqual(code, 0, text)
        from tools.credits_report import _leaves
        self.assertIn(f"All {len(_leaves(published)):,}".replace(",", ""), text.replace(",", ""))

    def test_every_top_level_block_of_the_publish_is_checked_and_classified(self):
        code, text, published = self.check()
        self.assertEqual(code, 0, text)
        from tools.credits_report import _leaves
        rows = [{"key": k, "ok": True} for k in _leaves(published)]
        checked = {block for block, _kind, _n, _bad in coverage(rows)}
        self.assertEqual(checked, set(published))
        unclassified = [b for b in checked if b not in BLOCK_KIND]
        self.assertEqual(unclassified, [], f"unclassified blocks: {unclassified}")

    def test_a_figure_in_any_block_outside_credits_fails_the_check(self):
        """One case per block the hostile review named as unchecked."""
        cases = {
            "weekly_windows.max20.current": lambda j: j["weekly_windows"]["max20"].update(current=99.0),
            "last_change": lambda j: j.update(last_change={"date": "2026-01-01", "percent": 99}),
            "events": lambda j: j.update(events=[{"date": "2026-01-01", "percent": 99}]),
            "rates.claude-opus-5.tokens_per_window":
                lambda j: j["rates"]["claude-opus-5"].update(tokens_per_window=1),
            "history.claude-opus-5[0].meter_budget_per_window":
                lambda j: j["history"]["claude-opus-5"][0].update(meter_budget_per_window=1.0),
            "plan_ratios_basis.as_of": lambda j: j["plan_ratios_basis"].update(as_of="2026-07-07"),
            "weekly_window_ratios.max5": lambda j: j["weekly_window_ratios"].update(max5=9.9),
            "reference.shortfall.per_plan.max20.ratio":
                lambda j: j["reference"]["shortfall"]["per_plan"]["max20"].update(ratio=0.5),
            "effort_usd": lambda j: j.update(effort_usd={"claude-opus-5": {"low": 1.0}}),
            "rates.claude-opus-5.deprecated":
                lambda j: j["rates"]["claude-opus-5"].update(deprecated="never mind"),
        }
        for key, mutate in cases.items():
            with self.subTest(key=key):
                code, text, _ = self.check(mutate)
                self.assertEqual(code, 1, f"{key} was not caught")
                self.assertIn("do not reproduce", text)

    def test_every_window_tokens_figure_is_checked_and_reproduces(self):
        """The new block is not exempt: every leaf of it is compared, and a wrong one fails."""
        from tools.credits_report import _leaves
        code, text, published = self.check()
        self.assertEqual(code, 0, text)
        leaves = _leaves(published["credits"]["window_tokens"])
        self.assertGreater(len(leaves), 50)
        for key in ("all.value", "per_class.cache_read.value", "accounts.a2.n",
                    "per_family.sonnet.all.value", "per_week.all.value",
                    "cache_read_share.value", "method", "selection", "as_of"):
            self.assertIn(key, leaves, key)

    def test_a_wrong_figure_anywhere_in_window_tokens_fails_the_check(self):
        cases = {
            "credits.window_tokens.all.value":
                lambda j: j["credits"]["window_tokens"]["all"].update(value=1),
            "credits.window_tokens.per_class.cache_read.value":
                lambda j: j["credits"]["window_tokens"]["per_class"]["cache_read"].update(value=1),
            "credits.window_tokens.accounts.a1.all.status":
                lambda j: j["credits"]["window_tokens"]["accounts"]["a1"]["all"].update(
                    status="contributed nothing"),
            "credits.window_tokens.per_family.haiku.all.value":
                lambda j: j["credits"]["window_tokens"]["per_family"]["haiku"]["all"].update(
                    value=123_456),
            "credits.window_tokens.per_week.all.value":
                lambda j: j["credits"]["window_tokens"]["per_week"]["all"].update(value=1),
            "credits.window_tokens.method":
                lambda j: j["credits"]["window_tokens"].update(method="trust me"),
        }
        for key, mutate in cases.items():
            with self.subTest(key=key):
                code, text, _ = self.check(mutate)
                self.assertEqual(code, 1, f"{key} was not caught")
                self.assertIn("do not reproduce", text)

    def test_without_seven_day_steps_there_is_no_week(self):
        """The fixture carries a weekly series but no seven-day steps."""
        # The week is measured on the seven-day meter (ADR 0001 rule 11). This fixture's
        # history records no seven-day steps, so the week is a status and nulls, never the
        # window times the meter ratio it does measure.
        published = self.check()[2]
        per_week = published["credits"]["window_tokens"]["per_week"]
        self.assertIsNotNone(published["weekly_windows"]["max20"]["current"])
        self.assertIsNone(per_week["windows_per_week"]["value"])
        self.assertIsNone(per_week["all"]["value"])
        self.assertIsNotNone(per_week["status"])

    def test_a_block_that_gained_its_first_entry_is_not_invisible(self):
        """An empty list is a leaf of its own, so filling one is a difference, not a silence."""
        code, text, _ = self.check(
            lambda j: j["weekly_windows"]["pro"].update(regimes=[{"windows": 4.0}]))
        self.assertEqual(code, 1)
        self.assertIn("weekly_windows.pro.regimes", text)

    def test_pass_through_data_is_compared_against_the_file_it_came_from(self):
        for key, mutate in (("session_tokens", lambda j: j.update(session_tokens={"claude-opus-5": 7})),
                            ("api_price_per_mtok",
                             lambda j: j["api_price_per_mtok"]["claude-opus-5"].update(input=99))):
            with self.subTest(key=key):
                code, text, _ = self.check(mutate)
                self.assertEqual(code, 1)
                self.assertIn(key, text)

    def test_the_contributed_block_is_read_only_when_the_publish_carries_one(self):
        block = {"figures": {"tokens_per_window": 1234}}

        def plant(root):
            (root / "data/contributed.json").write_text(json.dumps(block))

        # Planted on disk but absent from the publish: the check must not invent it.
        self.assertEqual(self.check(write=plant)[0], 0)
        # Carried by the publish and matching the file: it reproduces.
        code, text, _ = self.check(lambda j: j.update(contributed=block), write=plant)
        self.assertEqual(code, 0, text)
        # Carried by the publish and not matching the file: it does not.
        code, text, _ = self.check(lambda j: j.update(contributed={"figures": {"tokens_per_window": 9}}),
                                   write=plant)
        self.assertEqual(code, 1)
        self.assertIn("contributed", text)

    def test_the_summary_names_each_block_its_kind_and_its_figure_count(self):
        text = self.check()[1]
        for line in ("credits                       derived",
                     "api_price_per_mtok            pass_through",
                     "generated_at                  from_publish",
                     "plan_ratios_basis             constant"):
            self.assertIn(line, text)

    def test_a_publish_with_no_generated_at_is_a_failure_not_a_pass(self):
        code, text, _ = self.check(lambda j: j.pop("generated_at"))
        self.assertEqual(code, 1)
        self.assertIn("no `generated_at`", text)


class CreditsAsOfTests(unittest.TestCase):
    """`credits.as_of` and the per-model rows' own dates (wf-58 item 4).

    The credit figures carried no date at all, so the page printed a neighbouring block's
    under them. Two readings stand behind them and they are not the same date: the
    pure-Opus cluster the window is the median of, and the stretches the per-model fits
    ran over. Each row publishes the one its own figures rest on.
    """

    #: A pure-Opus stretch, and a later mixed one that is priceable but not pure. The
    #: window can only see the first; the fits see both, so the two dates differ.
    GS: ClassVar[dict] = report("jwork", [
        opus_stretch("2026-09-05T00:00:00+00:00", 200_000),
        stretch("2026-09-08T00:00:00+00:00",
                {"claude-opus-5": tok(input=1_000_000), "claude-sonnet-5": tok(input=2_000_000)}),
    ])

    def setUp(self):
        self.credits = _published(gs=self.GS)["credits"]

    def test_the_block_dates_itself_from_the_newer_of_the_two_readings(self):
        self.assertEqual(self.credits["as_of"], "2026-09-08T00:00:00+00:00")
        self.assertEqual(self.credits["as_of_source"]["window_cluster"], "2026-09-05T00:00:00+00:00")
        self.assertEqual(self.credits["as_of_source"]["measured_rate_fits"], "2026-09-08T00:00:00+00:00")

    def test_the_anchor_row_dates_from_the_window_and_a_measured_row_from_the_fits(self):
        per_model = self.credits["per_model"]
        self.assertEqual(per_model["opus"]["rate_source"], "reference")
        self.assertEqual(per_model["opus"]["as_of"], "2026-09-05T00:00:00+00:00")
        for fam in ("sonnet", "fable"):
            self.assertEqual(per_model[fam]["as_of"], "2026-09-08T00:00:00+00:00", fam)

    def test_a_row_with_no_figure_publishes_no_date_either(self):
        rates = C.load_model_rates()
        bare = dict(rates["per_family"]["haiku"], inferred=None)
        credits = _published(gs=self.GS, model_rates={**rates, "per_family": {
            **rates["per_family"], "haiku": bare}})["credits"]
        haiku = credits["per_model"]["haiku"]
        self.assertIsNone(haiku["credits_per_token"]["input"])
        self.assertIsNone(haiku["as_of"])
        self.assertIn("not measurable", haiku["status"])

    def test_no_stretch_at_all_leaves_every_date_null_rather_than_guessing(self):
        credits = _published(gs=report("jwork", []))["credits"]
        self.assertIsNone(credits["as_of"])
        self.assertIsNone(credits["as_of_source"]["window_cluster"])
        self.assertIsNone(credits["as_of_source"]["measured_rate_fits"])
        self.assertIsNone(credits["per_model"]["opus"]["as_of"])

    def test_the_newest_stretch_is_the_latest_instant_not_the_largest_string(self):
        """One watched account writes +02:00, so a lexical maximum reads the wrong row."""
        self.assertEqual(C.newest_end([{"end": "2026-09-20T02:16:35+02:00"},
                                       {"end": "2026-09-20T01:00:00+00:00"}]),
                         "2026-09-20T01:00:00+00:00")
        self.assertIsNone(C.newest_end([{"delta_pct": 4.0}]))

    def test_the_fits_date_names_no_account_anywhere_in_the_document(self):
        text = json.dumps(_published(gs=self.GS))
        for name, _label in ACCOUNT_LABELS:
            self.assertNotIn(name, text)


class CompareTests(unittest.TestCase):
    def test_a_number_within_tolerance_is_ok_and_one_outside_it_is_not(self):
        rows = {r["key"]: r for r in compare_published({"a": 1000.0, "b": 1000.0},
                                                       {"a": 1004.0, "b": 1010.0})}
        self.assertTrue(rows["a"]["ok"])
        self.assertFalse(rows["b"]["ok"])

    def test_a_zero_is_compared_absolutely_rather_than_by_dividing_by_it(self):
        rows = {r["key"]: r for r in compare_published({"a": 0}, {"a": 0})}
        self.assertTrue(rows["a"]["ok"])

    def test_a_missing_path_is_reported_on_the_side_it_is_missing_from(self):
        rows = {r["key"]: r for r in compare_published({"a": 1}, {"b": 1})}
        self.assertEqual(rows["a"]["recomputed"], "<absent>")
        self.assertEqual(rows["b"]["published"], "<absent>")

    def test_true_is_not_treated_as_the_number_one(self):
        rows = {r["key"]: r for r in compare_published({"a": True}, {"a": 1})}
        self.assertFalse(rows["a"]["ok"])


class RecomputeTests(unittest.TestCase):
    def test_the_recompute_reads_the_files_and_not_the_published_numbers(self):
        """A published JSON whose figures are nonsense still recomputes the real ones."""
        gs = report("jwork", [opus_stretch("2026-09-05T00:00:00+00:00", 200_000)])
        passive = {"split": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
                   "session_tokens": {}}
        with tempfile.TemporaryDirectory() as d:
            paths = _files(Path(d), gs, {}, passive, {})
            block = recompute_credits_block({"generated_at": NOW.isoformat(),
                                             "credits": {"window_credits": {"value": 1}}},
                                            paths["gs"], paths["masterrig"], paths["probes"],
                                            paths["effort_matrix"], paths["passive"], paths["prices"],
                                            four_accounts=paths["four_accounts"])
        self.assertEqual(block["window_credits"]["value"], 20_000_000)

    def test_the_recompute_uses_the_publishs_own_instant(self):
        """Not `now`: the weekly block's current regime is bounded by the publish time."""
        gs = report("jwork", [opus_stretch("2026-09-05T00:00:00+00:00", 200_000)])
        passive = {"split": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
                   "session_tokens": {}}
        later = (NOW + timedelta(days=400)).isoformat()
        with tempfile.TemporaryDirectory() as d:
            paths = _files(Path(d), gs, {}, passive, {})
            block = recompute_credits_block({"generated_at": later}, paths["gs"], paths["masterrig"],
                                            paths["probes"], paths["effort_matrix"],
                                            paths["passive"], paths["prices"],
                                            four_accounts=paths["four_accounts"])
        self.assertEqual(block["window_credits"]["value"], 20_000_000)


def classed_stretch(start: str, input: int, cache_write: int, cache_read: int, output: int,
                    delta_pct: float = 10.0, model: str = "claude-opus-5", **extra) -> dict:
    """A pure-Opus stretch with all four token classes set, so a per-class median has a spread."""
    return stretch(start, {model: tok(input=input, output=output, cache_read=cache_read,
                                      cache_write=cache_write)}, delta_pct, **extra)


class WindowTokensTests(unittest.TestCase):
    """What a window buys in tokens, on a cluster whose medians are known by hand.

    Every stretch below moved the meter 10%, so a full window is its own counts times ten.
    jwork carries three stretches at 1x, 2x and 3x a base bundle; Dave carries one, richer
    in cache reads, so the pooled median falls between jwork's middle and Dave's and the
    per-account intervals do not nest.
    """

    #: jwork's three, then Dave's one. Per window (x10, since each moved 10%):
    #: jwork all-four 116,000 / 232,000 / 348,000; Dave 564,000.
    JWORK: ClassVar[list] = [
        classed_stretch("2026-09-05T00:00:00+00:00", 100, 1_000, 10_000, 500),
        classed_stretch("2026-09-06T00:00:00+00:00", 200, 2_000, 20_000, 1_000),
        classed_stretch("2026-09-07T00:00:00+00:00", 300, 3_000, 30_000, 1_500),
    ]
    DAVE: ClassVar[list] = [classed_stretch("2026-09-08T00:00:00+00:00", 400, 4_000, 50_000, 2_000)]
    #: A planted measured-rate block: Sonnet at exactly half the Opus input rate, so the
    #: conversion is a doubling whatever the class mix; Fable an envelope whose dear edge is
    #: the anchor itself; Haiku nothing at all.
    RATES: ClassVar[dict] = {"per_family": {
        "opus": {"input": OPUS_IN, "interval": None, "anchor": True, "rate_source": "reference",
                 "output_multiplier": 5, "status": None},
        "sonnet": {"input": OPUS_IN / 2, "interval": [OPUS_IN / 4, OPUS_IN],
                   "output_multiplier": 5, "rate_source": "measured", "status": None},
        "fable": {"input": None, "interval": [OPUS_IN / 2, OPUS_IN], "output_multiplier": 5,
                  "rate_source": "measured", "status": "rate not yet identified"},
        "haiku": {"input": None, "interval": None, "output_multiplier": 5,
                  "rate_source": "measured",
                  "status": "not measurable, no clean stretch is Haiku-heavy"},
    }}

    def clean(self, jwork=None, dave=None, runs=()):
        return C.clean_stretches({"jwork": self.JWORK if jwork is None else jwork,
                                  "dave": self.DAVE if dave is None else dave}, list(runs))

    def block(self, *, rates=None, windows=None, interval=None, **kw):
        return C.window_tokens(self.clean(**kw), CREDITS, LABELS, rates,
                               windows_per_week=windows, windows_per_week_interval=interval)

    # --- the measurement itself -------------------------------------------------------

    def test_a_full_window_is_the_stretchs_own_counts_over_its_own_percent(self):
        block = self.block()
        self.assertEqual(block["n"], 4)
        self.assertEqual(block["all"]["value"], 290_000)
        self.assertIsNone(block["all"]["status"])

    def test_the_interval_is_the_union_of_the_account_intervals(self):
        """Not the pooled spread by another name: each account's own lowest and highest."""
        block = self.block()
        self.assertEqual(block["accounts"]["a2"]["all"]["interval"], [116_000, 348_000])
        self.assertEqual(block["accounts"]["a3"]["all"]["interval"], [564_000, 564_000])
        self.assertEqual(block["all"]["interval"], [116_000, 564_000])

    def test_each_token_class_has_its_own_median_and_spread(self):
        # Each class also carries its own two sides of the cut; the median and the spread
        # here are the whole cluster's, which is the mix the conversions hold their shape
        # from (see the cut tests below).
        per_class = self.block()["per_class"]
        for cls, value, interval in (("input", 2_500, [1_000, 4_000]),
                                     ("cache_write", 25_000, [10_000, 40_000]),
                                     ("cache_read", 250_000, [100_000, 500_000]),
                                     ("output", 12_500, [5_000, 20_000])):
            with self.subTest(cls=cls):
                self.assertEqual(per_class[cls]["value"], value)
                self.assertEqual(per_class[cls]["interval"], interval)

    def test_an_account_gets_its_own_per_class_medians_too(self):
        a2 = self.block()["accounts"]["a2"]
        self.assertEqual(a2["n"], 3)
        self.assertEqual(a2["per_class"]["cache_read"], {"value": 200_000,
                                                         "interval": [100_000, 300_000]})

    def test_the_cache_read_share_is_the_same_fact_as_one_fraction(self):
        share = self.block()["cache_read_share"]
        self.assertEqual(share["value"], round(10_000 / 11_600, 4))
        self.assertEqual(share["interval"], [round(10_000 / 11_600, 4), round(50_000 / 56_400, 4)])

    def test_the_block_dates_itself_from_the_newest_stretch_in_the_cluster(self):
        self.assertEqual(self.block()["as_of"], "2026-09-08T00:00:00+00:00")

    def test_no_rate_and_no_class_weight_enters_the_measured_figure(self):
        """The Opus row is the same number with the cache-read weight moved off zero."""
        heavy = C.window_tokens(self.clean(), CREDITS, LABELS, self.RATES, weight=0.015)
        self.assertEqual(heavy["all"]["value"], self.block(rates=self.RATES)["all"]["value"])
        self.assertEqual(heavy["per_family"]["opus"]["all"]["value"], 290_000)

    def test_the_method_says_what_was_done_and_what_the_interval_is(self):
        method = self.block()["method"]
        self.assertIn("per token class", method)
        self.assertIn("union of the account intervals", method)
        self.assertIn("No credit rate and no class weight", method)

    # --- the selection ----------------------------------------------------------------

    def test_it_reads_the_same_cluster_as_the_window_in_credits(self):
        clean = self.clean()
        self.assertEqual(C.window_tokens(clean, CREDITS, LABELS)["n"],
                         C.window_credits(clean, CREDITS, LABELS)["n"])

    def test_the_selection_sentence_is_the_one_the_window_in_credits_states(self):
        self.assertIn(self.block()["selection"], C.window_credits(self.clean(), CREDITS,
                                                                  LABELS)["method"])

    def test_a_stretch_with_a_second_model_in_it_is_not_pure_opus(self):
        mixed = stretch("2026-09-09T00:00:00+00:00",
                        {"claude-opus-5": tok(input=1_000), "claude-sonnet-5": tok(input=10)})
        self.assertEqual(self.block(jwork=[*self.JWORK, mixed])["n"], 4)

    def test_a_stretch_that_barely_moved_the_meter_is_left_out(self):
        rounding = classed_stretch("2026-09-09T00:00:00+00:00", 100, 1_000, 10_000, 500,
                                   delta_pct=1.0)
        self.assertEqual(self.block(jwork=[*self.JWORK, rounding])["n"], 4)

    def test_an_unaccounted_capture_column_keeps_a_stretch_out(self):
        unaccounted = classed_stretch("2026-09-09T00:00:00+00:00", 100, 1_000, 10_000, 500,
                                      capture_status="unaccounted")
        self.assertEqual(self.block(jwork=[*self.JWORK, unaccounted])["n"], 4)

    def test_a_stretch_cut_by_one_of_the_trackers_own_runs_is_left_out(self):
        run = C.HarnessRun("jwork", datetime(2026, 9, 4, 22, tzinfo=timezone.utc),
                           datetime(2026, 9, 5, 1, tzinfo=timezone.utc), "probe")
        self.assertEqual(self.block(runs=[run])["n"], 3)

    # --- the null shapes --------------------------------------------------------------

    def test_an_account_that_contributed_nothing_says_so_and_publishes_no_number(self):
        a1 = self.block()["accounts"]["a1"]
        self.assertEqual(a1, {"n": 0, "per_class": None,
                              "all": {"value": None, "interval": None,
                                      "status": "contributed no clean pure-opus stretch"}})

    def test_no_pure_opus_stretch_anywhere_leaves_every_figure_null(self):
        block = C.window_tokens(C.clean_stretches({}, []), CREDITS, LABELS, self.RATES)
        self.assertEqual(block["n"], 0)
        self.assertIsNone(block["all"]["value"])
        self.assertIn("no capture-accepted pure-opus stretch", block["all"]["status"])
        self.assertIsNone(block["as_of"])
        for fam in ("opus", "sonnet", "fable", "haiku"):
            self.assertIsNone(block["per_family"][fam]["all"]["value"], fam)

    def test_a_family_whose_fits_did_not_separate_publishes_the_envelope_and_no_value(self):
        fable = self.block(rates=self.RATES)["per_family"]["fable"]
        self.assertEqual(fable["rate_source"], "envelope")
        self.assertIsNone(fable["all"]["value"])
        self.assertEqual(fable["all"]["status"], "rate not yet identified")
        # The dear edge of the envelope is the anchor itself, so the low edge is the
        # window's own; the cheap edge buys strictly more than the anchor does.
        self.assertEqual(fable["all"]["interval"][0], 116_000)
        self.assertGreater(fable["all"]["interval"][1], 564_000)
        self.assertIn("envelope", fable["conversion"])

    def test_a_family_with_nothing_to_measure_a_rate_from_publishes_a_sentence_only(self):
        haiku = self.block(rates=self.RATES)["per_family"]["haiku"]
        self.assertEqual(haiku["rate_source"], "none")
        self.assertEqual(haiku["all"], {"value": None, "interval": None,
                                        "status": "not measurable, no clean stretch is Haiku-heavy"})
        self.assertIsNone(haiku["conversion"])

    def test_with_no_measured_rate_source_only_the_anchor_survives(self):
        block = self.block()
        self.assertEqual(block["per_family"]["opus"]["all"]["value"], 290_000)
        for fam in ("sonnet", "fable", "haiku"):
            self.assertIsNone(block["per_family"][fam]["all"]["value"], fam)
            self.assertEqual(block["per_family"][fam]["all"]["status"], C.NO_MEASURED_SOURCE)

    # --- the conversions --------------------------------------------------------------

    def test_the_anchor_family_is_the_measurement_itself_and_converts_nothing(self):
        opus = self.block(rates=self.RATES)["per_family"]["opus"]
        self.assertEqual(opus["rate_source"], "anchor")
        self.assertIsNone(opus["conversion"])
        self.assertEqual(opus["all"]["value"], 290_000)
        self.assertEqual(opus["all"]["interval"], [116_000, 564_000])

    def test_a_family_at_half_the_anchors_rate_buys_twice_the_tokens(self):
        """Half the input rate and the same output multiple: the mix cancels exactly."""
        sonnet = self.block(rates=self.RATES)["per_family"]["sonnet"]
        self.assertEqual(sonnet["rate_source"], "measured")
        self.assertEqual(sonnet["all"]["value"], 580_000)
        # The cheapest rate buys the most tokens: the quarter-rate edge against the top of
        # the window's range, the anchor-rate edge against the bottom.
        self.assertEqual(sonnet["all"]["interval"], [116_000, 2_256_000])
        self.assertIn("measured Sonnet input rate", sonnet["conversion"])

    # --- the week ---------------------------------------------------------------------

    def test_a_week_is_the_window_times_the_measured_windows_per_week(self):
        per_week = self.block(rates=self.RATES, windows=4.0, interval=[3.5, 4.5])["per_week"]
        self.assertEqual(per_week["windows_per_week"]["value"], 4.0)
        self.assertEqual(per_week["all"]["value"], 1_160_000)
        self.assertEqual(per_week["all"]["interval"], [406_000, 2_538_000])
        self.assertEqual(per_week["per_family"]["sonnet"]["all"]["value"], 2_320_000)
        self.assertEqual(per_week["per_family"]["sonnet"]["all"]["interval"], [406_000, 10_152_000])
        self.assertIsNone(per_week["status"])

    def test_a_family_with_an_envelope_keeps_that_shape_through_the_week(self):
        per_week = self.block(rates=self.RATES, windows=4.0, interval=[3.5, 4.5])["per_week"]
        fable = per_week["per_family"]["fable"]["all"]
        self.assertIsNone(fable["value"])
        self.assertEqual(fable["interval"][0], 116_000 * 3.5)
        self.assertEqual(fable["status"], "rate not yet identified")
        self.assertIsNone(per_week["per_family"]["haiku"]["all"]["interval"])

    def test_with_no_measured_windows_per_week_the_week_is_a_status_and_nulls(self):
        per_week = self.block(rates=self.RATES)["per_week"]
        self.assertEqual(per_week["status"], "no measured windows per week to multiply by")
        self.assertIsNone(per_week["all"]["value"])
        self.assertIsNone(per_week["all"]["interval"])
        self.assertIsNone(per_week["windows_per_week"]["value"])
        for fam in ("opus", "sonnet", "fable", "haiku"):
            self.assertIsNone(per_week["per_family"][fam]["all"]["value"], fam)


class WindowTokensPublishedTests(unittest.TestCase):
    """The block as the publisher writes it, and what it refuses to write."""

    def setUp(self):
        self.gs = report("jwork", WindowTokensTests.JWORK)
        self.credits = _published(gs=self.gs)["credits"]
        self.block = self.credits["window_tokens"]

    def test_the_block_is_published_beside_the_window_it_is_measured_on(self):
        self.assertEqual(self.block["derivation"], "credits")
        self.assertEqual(self.block["n"], self.credits["window_credits"]["n"])
        self.assertEqual(self.block["all"]["value"], 232_000)
        self.assertEqual(self.block["all"]["interval"], [116_000, 348_000])

    def test_the_watched_accounts_are_published_by_label_and_never_by_name(self):
        self.assertEqual(sorted(self.block["accounts"]), ["a1", "a2", "a3", "a4"])
        for name in ("jwork", "dave", "masterrig", "avis"):
            self.assertNotIn(name, json.dumps(self.block))

    def test_the_haiku_row_states_the_same_sentence_the_per_model_row_states(self):
        self.assertEqual(self.block["per_family"]["haiku"]["all"]["status"],
                         self.credits["per_model"]["haiku"]["status"])

    def test_a_publish_with_no_weekly_measurement_publishes_no_week(self):
        self.assertEqual(self.block["per_week"]["status"],
                         "no measured windows per week to multiply by")

    def test_the_legacy_dollar_route_is_marked_deprecated_and_still_published(self):
        rates = _published(gs=self.gs)["rates"]["claude-opus-5"]
        self.assertIn("tokens_per_window", rates)
        self.assertEqual(rates["deprecated"],
                         "read credits.window_tokens; removed after the page moves")


#: The instant the regime tests open their first change at. Any instant would do: the regimes
#: open at the four-account detector's measured changes and nowhere else (ADR 0001 rule 22).
CUT = C.CUT_AT.isoformat()


def measured(at: str, found_on: str = "seven_day", **sizes) -> dict:
    """One measured change of the four-account detector as the regimes read it
    (`four_accounts.regime_changes`). `sizes` maps a line to (change %, (low, high)); a size
    whose interval holds no change is carried, not drawn."""
    return F.regime_changes({"changes": [{
        "at": at, "state": "measured", "found_on": found_on,
        "sizes": {line: {"change_pct": pct, "interval_pct": list(iv), "agree": True,
                         "excludes_no_change": iv[0] > 0 or iv[1] < 0}
                  for line, (pct, iv) in sizes.items()}}]})[0]


def drawn_five_hour(at: str, pct: float = 8.6, iv=(4.0, 13.0)) -> dict:
    """A measured change whose five-hour size the window chart draws."""
    return measured(at, five_hour=(pct, iv), seven_day=(pct, iv))


def carried_five_hour(at: str) -> dict:
    """A measured change found on the seven-day line whose five-hour size holds no change."""
    return measured(at, seven_day=(20.0, (10.0, 30.0)), five_hour=(2.0, (-5.0, 9.0)))


class WindowRegimeTests(unittest.TestCase):
    """The pure-Opus window cluster split at the detector's measured changes, and which regime
    states the window NOW. Before 14 September 115,000 credits per 1% (four stretches); after
    it two stretches at 200,000 and 220,000."""

    BEFORE: ClassVar[list] = [opus_stretch(f"2026-09-{d:02d}T00:00:00+00:00", level)
                              for d, level in ((10, 100_000), (11, 110_000),
                                               (12, 120_000), (13, 130_000))]
    AFTER: ClassVar[list] = [opus_stretch(f"2026-09-{d:02d}T00:00:00+00:00", level)
                             for d, level in ((16, 200_000), (17, 220_000))]
    THICK: ClassVar[list] = [opus_stretch(f"2026-09-{d:02d}T00:00:00+00:00", level)
                             for d, level in ((15, 180_000), (16, 190_000), (17, 200_000),
                                              (18, 210_000), (19, 220_000))]

    def window(self, changes=(), before=None, after=None):
        clean = C.clean_stretches({"jwork": (self.BEFORE if before is None else before),
                                   "dave": (self.AFTER if after is None else after)}, [])
        return C.window_credits(clean, CREDITS, LABELS, changes=list(changes))

    def test_with_no_measured_change_the_window_is_the_whole_cluster(self):
        window = self.window()
        self.assertEqual((window["current_source"], window["value"]), ("whole_cluster", 12_500_000))
        self.assertEqual([(r["from"], r["until"]) for r in window["regimes"]], [(None, None)])
        self.assertIsNone(window["split_at"])
        self.assertEqual((window["before"]["n"], window["after"]["n"], window["n"]), (0, 0, 6))

    def test_each_side_of_the_newest_change_publishes_its_own_median_spread_and_count(self):
        window = self.window([drawn_five_hour(CUT)])
        self.assertEqual(window["split_at"], CUT)
        self.assertEqual(window["before"], {"value": 11_500_000,
                                            "interval": [10_000_000, 13_000_000], "n": 4})
        self.assertEqual(window["after"], {"value": 21_000_000,
                                           "interval": [20_000_000, 22_000_000], "n": 2})
        self.assertEqual(window["n"], 6)

    def test_a_thin_regime_is_the_previous_one_times_the_detectors_five_hour_size(self):
        window = self.window([drawn_five_hour(CUT)])
        self.assertEqual(window["current_source"], C.KNOWN_DATE_SCALED_SOURCE)
        before, now = window["regimes"]
        self.assertEqual((before["source"], before["value"]), ("before_first_change", 11_500_000))
        self.assertEqual((before["until"], now["from"], now["until"]), (CUT, CUT, None))
        self.assertEqual(now["value"], round(11_500_000 * 1.086))
        self.assertEqual(now["interval"], [round(10_000_000 * 1.04), round(13_000_000 * 1.13)])
        self.assertEqual((window["value"], window["interval"]), (now["value"], now["interval"]))
        self.assertEqual(window["credits_per_pct"], round(115_000 * 1.086))
        self.assertIn("Changes applied: from 2026-09-14T12:00:00+00:00, five-hour +8.6%",
                      window["current_method"])

    def test_a_thick_regime_states_itself_and_the_earlier_one_steps_by_the_detectors_size(self):
        window = self.window([drawn_five_hour(CUT)], after=self.THICK)
        self.assertEqual(window["current_source"], C.KNOWN_DATE_CLUSTER_SOURCE)
        before, now = window["regimes"]
        self.assertEqual((now["value"], now["interval"]), (20_000_000, [18_000_000, 22_000_000]))
        # The chart draws the detector's +8.6% at the change, not the clusters' own +74%: the
        # earlier regime is restated from the newest, its own reading kept beside it.
        self.assertAlmostEqual(now["value"] / before["value"], 1.086, places=4)
        self.assertEqual((before["own_value"], before["chained"]),
                         (11_500_000, {"at": CUT, "step_pct": 8.6}))

    def test_a_change_whose_five_hour_size_holds_no_change_carries_the_window(self):
        for after in (self.AFTER, self.AFTER[:1]):
            with self.subTest(after=len(after)):
                window = self.window([carried_five_hour(CUT)], after=after)
                self.assertEqual(len(window["regimes"]), 2)
                self.assertEqual(window["current_source"], C.KNOWN_DATE_UNSCALED_SOURCE)
                before, now = window["regimes"]
                self.assertEqual((now["value"], now["interval"]), (before["value"], before["interval"]))
                self.assertEqual(before["chained"], {"at": CUT, "step_pct": 0.0})
                self.assertIn("five-hour carried", window["current_method"])

    def test_a_candidate_opens_no_regime_and_a_provisional_change_does(self):
        # Jonathan's decision, 2026-10-23: the charts update provisionally too.
        block = {"changes": [{"at": CUT, "state": "candidate", "found_on": "ratio", "sizes": {}}]}
        self.assertEqual(F.regime_changes(block), [])
        block = {"changes": [{"at": CUT, "state": "provisional", "found_on": "ratio", "sizes": {}}]}
        (change,) = F.regime_changes(block)
        self.assertEqual((change["state"], change["provisional"], change["window_scaled"]),
                         ("provisional", True, False))
        self.assertIn("(provisional)", C.current_method([change]))

    def test_the_value_always_sits_inside_its_own_interval(self):
        for changes in ([], [drawn_five_hour(CUT)], [drawn_five_hour(CUT, -20.0, (-30.0, -10.0))],
                        [carried_five_hour(CUT)]):
            with self.subTest(changes=len(changes)):
                window = self.window(changes)
                self.assertLessEqual(window["interval"][0], window["value"])
                self.assertLessEqual(window["value"], window["interval"][1])

    def test_a_cluster_with_nothing_placeable_publishes_the_whole_of_it(self):
        # A fixture whose stretches carry no start stamp: no regime can claim them.
        undated = [dict(st, start=None) for st in self.BEFORE]
        window = self.window([drawn_five_hour(CUT)], before=undated, after=[])
        self.assertEqual((window["before"]["n"], window["after"]["n"], window["n"]), (0, 0, 4))
        self.assertIsNone(window["regimes"][0]["value"])

    def test_an_empty_cluster_states_no_current_source_at_all(self):
        window = self.window(before=[], after=[])
        self.assertIsNone(window["value"])
        self.assertIsNone(window["current_source"])


class WindowTokensRegimeTests(unittest.TestCase):
    """The same regimes on the token figures: `all` is now, `per_class` carries both sides."""

    #: 10% stretches, so a window is ten times the stretch's own counts. Before: 116,000
    #: and 232,000 tokens a window; after: 500,000 and 600,000.
    BEFORE: ClassVar[list] = [classed_stretch("2026-09-10T00:00:00+00:00", 100, 1_000, 10_000, 500),
                              classed_stretch("2026-09-11T00:00:00+00:00", 200, 2_000, 20_000, 1_000)]
    AFTER: ClassVar[list] = [classed_stretch("2026-09-16T00:00:00+00:00", 500, 5_000, 43_000, 1_500),
                             classed_stretch("2026-09-17T00:00:00+00:00", 600, 6_000, 51_500, 1_900)]
    AT = "2026-09-22T17:03:48+00:00"

    def block(self, changes=(), before=None, after=None, **kw):
        clean = C.clean_stretches({"jwork": (self.BEFORE if before is None else before),
                                   "dave": (self.AFTER if after is None else after)}, [])
        return C.window_tokens(clean, CREDITS, LABELS, None, changes=list(changes), **kw)

    def test_the_all_classes_figure_carries_both_sides_at_the_top_level(self):
        block = self.block([drawn_five_hour(CUT)])
        self.assertEqual(block["split_at"], CUT)
        self.assertEqual(block["before"], {"value": 174_000, "interval": [116_000, 232_000], "n": 2})
        self.assertEqual(block["after"], {"value": 550_000, "interval": [500_000, 600_000], "n": 2})

    def test_each_class_carries_its_own_two_sides(self):
        per_class = self.block([drawn_five_hour(CUT)])["per_class"]
        self.assertEqual(per_class["cache_read"]["before"],
                         {"value": 150_000, "interval": [100_000, 200_000], "n": 2})
        self.assertEqual(per_class["cache_read"]["after"],
                         {"value": 472_500, "interval": [430_000, 515_000], "n": 2})
        # The class median itself stays the whole cluster's: it is the mix the per-family
        # conversions hold their shape from, and each side is published beside it.
        self.assertEqual(per_class["cache_read"]["value"], 315_000)

    def test_all_is_the_previous_regime_scaled_while_the_newest_is_thin(self):
        block = self.block([drawn_five_hour(CUT)])
        self.assertEqual(block["current_source"], C.KNOWN_DATE_SCALED_SOURCE)
        self.assertEqual(block["all"]["value"], round(174_000 * 1.086))
        self.assertEqual(block["all"]["interval"], [round(116_000 * 1.04), round(232_000 * 1.13)])

    def test_a_thick_regime_states_the_tokens_itself(self):
        after = [classed_stretch(f"2026-09-{d:02d}T00:00:00+00:00", 500, 5_000, 43_000, 1_500)
                 for d in range(15, 20)]
        block = self.block([drawn_five_hour(CUT)], after=after)
        self.assertEqual((block["current_source"], block["all"]["value"]),
                         (C.KNOWN_DATE_CLUSTER_SOURCE, 500_000))

    def test_every_measured_change_opens_a_regime_and_the_week_and_families_follow(self):
        block = self.block([drawn_five_hour(CUT), drawn_five_hour(self.AT, 38.0, (14.0, 67.0))],
                           windows_per_week=5.0)
        self.assertEqual(block["current_source"], C.KNOWN_DATE_SCALED_SOURCE)
        self.assertEqual([(r["from"], r["until"]) for r in block["regimes"]],
                         [(None, CUT), (CUT, self.AT), (self.AT, None)])
        self.assertEqual([r["value"] for r in block["regimes"]],
                         [174_000, round(174_000 * 1.086), round(174_000 * 1.086 * 1.38)])
        self.assertEqual(block["regimes"][-1]["interval"],
                         [round(116_000 * 1.04 * 1.14), round(232_000 * 1.13 * 1.67)])
        self.assertEqual(block["all"]["value"], block["regimes"][-1]["value"])
        self.assertEqual(block["per_family"]["opus"]["all"]["value"], block["all"]["value"])
        self.assertEqual(block["per_week"]["all"]["value"], round(block["all"]["value"] * 5.0))


def detector_changes(first: str, second: str, separable: tuple[bool, bool] = (False, False),
                     cut_five_hour: float | None = 8.6) -> list[dict]:
    """The detector's measured changes for the family tests: one at CUT (the five-hour size
    `cut_five_hour` drawn, or carried when None), and two more at `first` and `second`, each
    +10% [+5%, +15%] on the five-hour line where `separable`, else carried."""
    cut = drawn_five_hour(CUT, cut_five_hour) if cut_five_hour is not None else carried_five_hour(CUT)
    return [cut, *(drawn_five_hour(at, 10.0, (5.0, 15.0)) if sep else carried_five_hour(at)
                   for at, sep in zip((first, second), separable))]


def opus55_window(start: str, window: int, delta_pct: float = 10.0) -> dict:
    """A pure Opus 5.5 stretch that reads exactly `window` tokens per full window."""
    total = round(window * delta_pct / 100)
    return classed_stretch(start, 100, 1_000, total - 1_600, 500, delta_pct=delta_pct,
                           model="claude-opus-5-5")


class SteppedRoundingTests(unittest.TestCase):
    """ADR 0001 rule 19: a regime set by a measured change keeps it through rounding."""

    @staticmethod
    def drawn(regimes):
        from tracker.invariants import _page_round
        return [_page_round((b["value"] - a["value"]) / a["value"] * 100)
                for a, b in zip(regimes, regimes[1:])]

    def regimes(self, before: float, step: float, **extra):
        row = {"interval": None, "measured_value": None, "measured_interval": None}
        return [dict(row, value=before), dict(row, value=before * step, step=step, **extra),
                dict(row, value=before * step)]

    def test_a_step_on_a_half_is_drawn_as_the_change_rounds(self):
        # The 22 September case: rounded on its own, 686,128,836 x 1.195 drew +19.49999999%.
        self.assertEqual(round(686_128_836.4 * 1.195) / round(686_128_836.4), 819_923_959 / 686_128_836)
        published = C._rounded_regimes(self.regimes(686_128_836.4, 1.195))
        self.assertGreaterEqual((published[1]["value"] - published[0]["value"])
                                / published[0]["value"] * 100, 19.5)
        self.assertEqual(self.drawn(published), [20, 0])

    def test_a_fall_keeps_its_side_too(self):
        from tracker.invariants import _page_round
        for before in (668_922_266.0, 686_128_836.4, 173_516_520.0, 29_449_873.0):
            for step in (0.995, 0.985, 0.993):
                with self.subTest(before=before, step=step):
                    published = C._rounded_regimes(self.regimes(before, step))
                    self.assertEqual(self.drawn(published)[0],
                                     _page_round(round((step - 1) * 100, 9)))

    def test_it_moves_the_value_by_no_more_than_rounding(self):
        published = C._rounded_regimes(self.regimes(686_128_836.4, 1.195))
        self.assertLessEqual(abs(published[1]["value"] - 686_128_836 * 1.195), 1)

    def test_a_carried_regime_is_published_at_the_same_value_and_step_is_not_published(self):
        published = C._rounded_regimes(self.regimes(686_128_836.4, 1.195))
        self.assertEqual(published[2]["value"], published[1]["value"])
        self.assertTrue(all("step" not in r for r in published))

    def test_a_regime_with_no_step_rounds_as_before(self):
        row = {"interval": None, "measured_value": None, "measured_interval": None}
        published = C._rounded_regimes([dict(row, value=100.4), dict(row, value=119.4)])
        self.assertEqual([r["value"] for r in published], [100, 119])


class RegimeFamilyTests(unittest.TestCase):
    """A regime is measured in the family actually in use: the family with the most pure
    clean stretches starting in it. Once Opus 5.5 replaced Opus 5 there is no pure Opus 5
    cluster left to chain from, and the Opus 5.5 headline is its own direct reading."""

    FIRST, SECOND = "2026-09-22T19:41:49+00:00", "2026-09-29T18:25:18+00:00"
    #: Five pure Opus 5.5 stretches between the two changes, one pure Opus 5 stretch too.
    MIDDLE: ClassVar[list] = [opus55_window(f"2026-09-{d:02d}T00:00:00+00:00", w)
                              for d, w in ((23, 600_000), (24, 640_000), (25, 612_000),
                                           (26, 590_000), (27, 700_000))]
    #: Four after the second change: too thin to state a regime.
    LATEST: ClassVar[list] = [opus55_window(f"2026-10-{d:02d}T00:00:00+00:00", 750_000)
                              for d in (1, 2, 3, 4)]
    RATES: ClassVar[dict] = {"per_family": {"opus-5-5": {
        "input": OPUS_IN * 0.8, "interval": [OPUS_IN * 0.7, OPUS_IN * 0.9],
        "output_multiplier": 5, "rate_source": "measured", "status": None}}}

    def block(self, model_rates=None, latest=None, separable=(True, False)):
        # The first change is measured, so the 14 September regime is a run of its own and
        # the regimes from 22 September are one run.
        after = [*self.MIDDLE, opus_stretch("2026-09-28T00:00:00+00:00", 300_000),
                 *(self.LATEST if latest is None else latest)]
        clean = C.clean_stretches({"jwork": WindowTokensRegimeTests.BEFORE, "masterrig": after}, [])
        return C.window_tokens(clean, CREDITS, LABELS, model_rates, windows_per_week=5.0,
                               changes=detector_changes(self.FIRST, self.SECOND, separable))

    def test_a_regime_in_a_new_family_states_that_familys_direct_median(self):
        block = self.block(self.RATES, latest=[])
        regime = block["regimes"][2]
        self.assertEqual((regime["source"], regime["measured_family"]),
                         (C.RUN_CLUSTER_SOURCE, "opus-5-5"))
        self.assertEqual(regime["measured_value"], 612_000)
        self.assertEqual(regime["measured_interval"], [590_000, 700_000])
        # The earlier regimes stay in the anchor family.
        self.assertEqual([r["measured_family"] for r in block["regimes"][:2]], ["opus", "opus"])

    def test_the_headline_family_equals_the_direct_reading_exactly(self):
        for rates in (self.RATES, None):
            with self.subTest(rates=bool(rates)):
                block = self.block(rates)
                # The run from 22 September holds nine Opus 5.5 readings and one Opus 5.
                newest = block["regimes"][-1]
                self.assertEqual((newest["source"], newest["measured_family"]),
                                 (C.RUN_CLUSTER_SOURCE, "opus-5-5"))
                self.assertEqual(newest["measured_value"], 700_000)
                self.assertEqual(block["per_family"]["opus-5-5"]["all"]["value"], 700_000)
                self.assertEqual(block["per_family"]["opus-5-5"]["all"]["interval"],
                                 [590_000, 750_000])
                self.assertEqual(block["measured_family"], "opus-5-5")

    def test_the_anchor_unit_value_goes_through_the_rate(self):
        block = self.block(self.RATES)
        # Opus 5.5 at 0.8x Opus 5 for every class: a window of Opus 5.5 tokens is 0.8 of one
        # in Opus 5 tokens.
        # The run opens at a separable +10% change, so the anchor-unit value is bridged:
        # the previous regime's window times 1.1.
        prev = block["regimes"][1]["value"]
        self.assertAlmostEqual(block["regimes"][-1]["value"], prev * 1.1, delta=1)
        self.assertEqual(block["all"]["value"], block["regimes"][-1]["value"])
        self.assertEqual(block["per_family"]["opus"]["all"]["value"], block["all"]["value"])

    def test_a_regime_between_measured_boundaries_states_itself_with_five_readings(self):
        block = self.block(self.RATES, latest=[*self.LATEST, opus55_window(
            "2026-10-04T06:00:00+00:00", 760_000)], separable=(True, True))
        newest = block["regimes"][-1]
        self.assertEqual((newest["source"], newest["measured_value"]),
                         (C.KNOWN_DATE_CLUSTER_SOURCE, 750_000))
        self.assertEqual(block["per_family"]["opus-5-5"]["all"]["value"], 750_000)

    def test_each_regime_a_certified_change_opens_steps_by_that_change(self):
        # Both changes measured (+10% each), and each regime is its own Opus 5.5 cluster:
        # the anchor-unit steps are the certified changes, not a rate conversion, through a
        # bridge rate on each regime; the headline stays the newest direct reading.
        block = self.block(self.RATES, latest=[*self.LATEST, opus55_window(
            "2026-10-04T06:00:00+00:00", 760_000)], separable=(True, True))
        _, cut, first, second = block["regimes"]
        for prev, reg in ((cut, first), (first, second)):
            self.assertEqual(reg["source"], C.KNOWN_DATE_CLUSTER_SOURCE)
            self.assertAlmostEqual(reg["value"] / prev["value"], 1.1, places=3)
            self.assertIn("equal the five-hour change the detector measured there",
                          reg["bridge_rate_source"])
        self.assertEqual(second["measured_value"], 750_000)
        self.assertEqual(block["per_family"]["opus-5-5"]["all"]["value"], 750_000)
        self.assertEqual(block["per_family"]["opus-5-5"]["history_rate"]["times_anchor"],
                         second["bridge_rate"])


class UnmeasuredBoundaryRunTests(unittest.TestCase):
    """A boundary the tracker could not measure (a joint fit that cannot separate) does not
    split the window cluster: the regimes it joins form one run, measured together in the
    run's own family. A measured boundary (the 14 September cut, a separable fit) still splits."""

    FIRST, SECOND = RegimeFamilyTests.FIRST, RegimeFamilyTests.SECOND
    #: Three pure Opus 5.5 stretches between the changes and three after: each regime alone
    #: is too thin, the run from 14 September holds six.
    MIDDLE: ClassVar[list] = [opus55_window(f"2026-09-{d:02d}T00:00:00+00:00", w)
                              for d, w in ((23, 600_000), (24, 640_000), (25, 450_000))]
    LATEST: ClassVar[list] = [opus55_window(f"2026-10-{d:02d}T00:00:00+00:00", w)
                              for d, w in ((1, 700_000), (2, 760_000), (3, 620_000))]
    RATES = RegimeFamilyTests.RATES

    def block(self, changes=None, middle=None, latest=None, five_hour_pct=8.6):
        after = [opus_stretch("2026-09-16T00:00:00+00:00", 300_000),
                 *(self.MIDDLE if middle is None else middle),
                 *(self.LATEST if latest is None else latest)]
        clean = C.clean_stretches({"jwork": WindowTokensRegimeTests.BEFORE, "masterrig": after}, [])
        return C.window_tokens(clean, CREDITS, LABELS, self.RATES, windows_per_week=5.0,
                               changes=changes or detector_changes(
                                   self.FIRST, self.SECOND, cut_five_hour=five_hour_pct))

    def test_a_run_of_unmeasured_boundaries_is_measured_as_one_cluster(self):
        block = self.block()
        before, *run = block["regimes"]
        self.assertEqual(before["source"], "before_first_change")
        for regime in run:
            self.assertEqual((regime["source"], regime["measured_family"], regime["regime_family"]),
                             (C.RUN_CLUSTER_SOURCE, "opus-5-5", "opus-5-5"))
            self.assertEqual(regime["n_regime_family"], 6)
            self.assertEqual(regime["run_from"], C.CUT_AT.isoformat())
            # Median of 450k, 600k, 620k, 640k, 700k, 760k.
            self.assertEqual(regime["measured_value"], 630_000)
            self.assertEqual(regime["measured_interval"], [450_000, 760_000])
        self.assertIsNone(before["run_from"])

    #: The bridge rate: the pre-cut window (174,000 Opus 5 tokens) times the meters' +8.6%
    #: across the cut, over the run's direct 630,000 Opus 5.5 tokens.
    BRIDGE = 174_000 * 1.086 / 630_000

    def test_the_run_converts_at_the_rate_that_matches_the_meters_at_its_opening(self):
        block = self.block()
        first = block["regimes"][1]
        self.assertAlmostEqual(first["bridge_rate"], round(self.BRIDGE, 4))
        self.assertEqual(first["bridge_rate_interval"],
                         [round(174_000 * 1.086 / 760_000, 4), round(174_000 * 1.086 / 450_000, 4)])
        self.assertIn("derived: the rate that makes the step at 2026-09-14", first["bridge_rate_source"])
        for regime in block["regimes"][1:]:
            self.assertEqual(regime["value"], round(630_000 * self.BRIDGE))
        # Other families still convert from the anchor at their own rates, and the fitted
        # Opus 5.5 rate is not touched.
        self.assertEqual(block["per_family"]["opus"]["all"]["value"], block["all"]["value"])
        self.assertEqual(block["per_family"]["opus-5-5"]["history_rate"]["times_anchor"],
                         round(self.BRIDGE, 4))

    def test_the_step_at_the_opening_boundary_equals_the_meters_change(self):
        block = self.block()
        before, first = block["regimes"][:2]
        self.assertEqual(first["family_value"], 630_000)
        self.assertAlmostEqual(first["family_value"] / before["family_value"], 1.086, places=3)

    def test_the_bridge_does_not_move_the_headline(self):
        for pct in (8.6, None, -20.0):
            with self.subTest(five_hour_pct=pct):
                self.assertEqual(self.block(five_hour_pct=pct)["per_family"]["opus-5-5"]["all"],
                                 {"value": 630_000, "interval": [450_000, 760_000], "status": None})

    def test_without_a_measured_five_hour_change_at_the_opening_the_fitted_rate_stands(self):
        block = self.block(five_hour_pct=None)
        first = block["regimes"][1]
        self.assertNotIn("bridge_rate", first)
        self.assertEqual(first["value"], round(630_000 * 0.8))
        # The change's five-hour size holds no change, so the window is carried across it:
        # the earlier regime is restated to the run's level, its own reading kept beside it.
        before = block["regimes"][0]
        self.assertEqual((before["family_value"], before["value"]), (630_000, first["value"]))
        self.assertEqual((before["own_value"], before["chained"]["step_pct"]), (first["value"], 0.0))

    def test_the_headline_family_is_the_runs_direct_median_exactly(self):
        block = self.block()
        self.assertEqual(block["per_family"]["opus-5-5"]["all"],
                         {"value": 630_000, "interval": [450_000, 760_000], "status": None})
        self.assertEqual(block["current_source"], C.RUN_CLUSTER_SOURCE)

    def test_a_thin_run_behaves_as_before(self):
        block = self.block(middle=self.MIDDLE[:1], latest=self.LATEST[:1])
        self.assertEqual([r["source"] for r in block["regimes"][2:]],
                         [C.KNOWN_DATE_UNSCALED_SOURCE, C.KNOWN_DATE_UNSCALED_SOURCE])
        self.assertEqual(block["regimes"][-1]["measured_family"], "opus")

    def test_a_separable_boundary_still_splits_the_run(self):
        changes = detector_changes(self.FIRST, self.SECOND, separable=(False, True))
        block = self.block(changes=changes, middle=[*self.MIDDLE, *(
            opus55_window(f"2026-09-{d:02d}T00:00:00+00:00", w)
            for d, w in ((26, 700_000), (27, 760_000)))], latest=self.LATEST[2:])
        middle_run, newest = block["regimes"][1:3], block["regimes"][3]
        # 14 September to 29 September is one run of five Opus 5.5 readings.
        for regime in middle_run:
            self.assertEqual((regime["source"], regime["measured_value"]),
                             (C.RUN_CLUSTER_SOURCE, 640_000))
        # The measured boundary splits: the newest regime is that run scaled by g.
        self.assertIsNone(newest["run_from"])
        self.assertEqual((newest["source"], newest["measured_value"]),
                         (C.KNOWN_DATE_SCALED_SOURCE, round(640_000 * 1.1)))


class CurrentWindowDownstreamTests(unittest.TestCase):
    """Everything the page divides by the window follows the current figure, with no
    second rule: the per-model rows, the session counts and the week."""

    #: Ten pure-Opus stretches before 14 September at exactly 200,000 credits per 1%.
    BEFORE: ClassVar[list] = [opus_stretch(f"2026-09-{d:02d}T00:00:00+00:00", 200_000)
                              for d in range(4, 14)]
    #: Ten after it, each one mixed (a token of Sonnet in it), so they never enter the
    #: pure-Opus cluster: the regime after the change is too thin to state itself.
    AFTER: ClassVar[list] = [
        stretch(f"2026-09-{d:02d}T{h:02d}:00:00+00:00",
                {"claude-opus-5": tok(input=round(220_000 * 10 / OPUS_IN)),
                 "claude-sonnet-5": tok(input=1)})
        for d in range(15, 20) for h in (5, 20)]
    #: The detector's newest run: one measured change at 14 September, +10% on the five-hour
    #: line (a size the window chart draws).
    RUN: ClassVar[dict] = {
        "schema": F.SCHEMA, "method": F.METHOD, "day": "2026-09-20",
        "data_until": "2026-09-20T00:00:00+00:00", "rule": F.RULE, "valuation": F.VALUATION,
        "headless_factor": {"value": None}, "interval_coverage": {}, "accounts": {},
        "withdrawn": [], "not_limit_changes": [], "lines": {}, "levels": {},
        "changes": [{
            "at": CUT, "instant_range": [CUT, CUT], "state": "measured", "found_on": "seven_day",
            "sign": 1, "held_days": 7, "first_found": "2026-09-14", "measured_on": "2026-09-20",
            "p": 0.00025, "change_pct": 12.0, "interval_pct": [6.0, 18.0], "accounts": [],
            "reading": "both_limits",
            "sizes": {"five_hour": {"change_pct": 10.0, "interval_pct": [4.0, 16.0],
                                    "excludes_no_change": True, "agree": True},
                      "seven_day": {"change_pct": 12.0, "interval_pct": [6.0, 18.0],
                                    "excludes_no_change": True, "agree": True}}}]}

    def setUp(self):
        self.j = _published(gs=report("jwork", [*self.BEFORE, *self.AFTER]),
                            four_accounts_run=self.RUN)
        self.credits = self.j["credits"]
        self.window = self.credits["window_credits"]

    def test_the_published_window_is_the_earlier_cluster_scaled_by_the_measured_change(self):
        self.assertEqual((self.window["before"]["n"], self.window["after"]["n"]), (10, 0))
        self.assertEqual(self.window["current_source"], C.KNOWN_DATE_SCALED_SOURCE)
        self.assertEqual(self.window["value"], 22_000_000)

    def test_without_a_measured_change_it_is_the_whole_cluster(self):
        window = _published(gs=report("jwork", [*self.BEFORE, *self.AFTER]))["credits"]["window_credits"]
        self.assertEqual((window["current_source"], window["value"]), ("whole_cluster", 20_000_000))
        self.assertEqual([(r["from"], r["until"]) for r in window["regimes"]], [(None, None)])

    def test_the_publish_check_reads_the_detector_state_it_is_given(self):
        """--publish-check takes the limit changes from the --four-accounts file, as the
        publish did: the same state reproduces the publish, a state that differs does not,
        and neither reads the checkout's history/four-accounts.json."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = _files(root, report("jwork", [*self.BEFORE, *self.AFTER]), {},
                           PublishCheckTests.PASSIVE, {})
            F.write_state(paths["four_accounts"], F.advance(None, self.RUN))
            published = _publish(paths)
            self.assertEqual(published["credits"]["window_credits"]["value"], 22_000_000)
            out = root / "claude-usage.json"
            out.write_text(json.dumps(published))

            def check(four_accounts: Path) -> tuple[int, str]:
                args = argparse.Namespace(**{**paths, "four_accounts": four_accounts},
                                          publish_check=out, tolerance=0.005,
                                          contributed=root / "data/contributed.json")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    return publish_check(args), buf.getvalue()

            code, text = check(paths["four_accounts"])
            self.assertEqual(code, 0, text)
            code, text = check(root / "history/no-four-accounts.json")
            self.assertEqual(code, 1)
            self.assertIn("window_credits.value", text)

    def test_every_per_model_row_divides_the_current_window(self):
        opus = self.credits["per_model"]["opus"]["tokens_per_window"]
        self.assertEqual(opus["input"]["value"], round(22_000_000 / OPUS_IN))
        self.assertEqual(opus["output"]["value"], round(22_000_000 / OPUS_OUT))

    def test_the_session_counts_divide_the_current_window(self):
        sessions = self.credits["sessions"]["claude-opus-5"]
        # The fixture's split is pure cache_write, which rides the input rate.
        self.assertEqual(sessions["per_window"]["value"],
                         round(22_000_000 / OPUS_IN / 1_000_000, 1))

    def test_the_token_figure_moves_with_it_too(self):
        tokens = self.credits["window_tokens"]
        self.assertEqual(tokens["current_source"], C.KNOWN_DATE_SCALED_SOURCE)
        self.assertEqual(tokens["all"]["value"], round(tokens["before"]["value"] * 1.10))

    def test_both_blocks_publish_their_regimes_ending_at_the_current_figure(self):
        # One measured change, so two regimes, split at its instant; the newest one is the
        # published figure.
        tokens = self.credits["window_tokens"]
        for regimes, current in ((self.window["regimes"], self.window["value"]),
                                 (tokens["regimes"], tokens["all"]["value"])):
            self.assertEqual([(r["from"], r["until"]) for r in regimes], [(None, CUT), (CUT, None)])
            self.assertEqual(regimes[-1]["value"], current)
        self.assertEqual(self.j["last_change"]["at"], CUT)


class PricedModelsTests(unittest.TestCase):
    """Every id normalize_model will hand on must be priceable, and land in a family.

    The five older models joined the table on 2026-09-23 (issue #63).  A row that is
    priced but has no credit family, or a family whose `list_price_model` names a row
    that is not there, would put a hole straight into the published per-model figures.
    """
    prices: ClassVar[dict]
    credits: ClassVar[dict]

    @classmethod
    def setUpClass(cls):
        cls.prices = json.loads(
            (Path(__file__).resolve().parent.parent / "data/prices.json").read_text(encoding="utf-8"))
        cls.credits = load_credits(cls.prices)

    def test_every_canonical_model_has_a_complete_row(self):
        for model in CANONICAL_MODELS:
            with self.subTest(model=model):
                row = self.prices.get(model)
                self.assertIsNotNone(row, f"{model} normalizes but has no price row")
                for field in ("input", "output", "cache_read", "cache_write",
                              "cache_write_1h", "meter_weight", "class_weight"):
                    self.assertIn(field, row)
                self.assertEqual(set(row["class_weight"]),
                                 {"input", "output", "cache_read", "cache_write"})

    def test_every_priced_row_normalizes_to_itself(self):
        for model, row in self.prices.items():
            # An auto-filed row is priced by its own id (join.priced_model), not via CANONICAL_MODELS.
            if not model.startswith("_") and "list_price_source" not in row:
                with self.subTest(model=model):
                    self.assertEqual(normalize_model(model), model)

    def test_the_added_models_land_in_the_right_family(self):
        for model, fam in (("claude-opus-5-5", "opus-5-5"), ("claude-opus-4-8", "opus"),
                           ("claude-opus-4-7", "opus"), ("claude-sonnet-4-6", "sonnet"),
                           ("claude-haiku-4-5", "haiku")):
            with self.subTest(model=model):
                self.assertEqual(family(model, self.credits), fam)

    def test_opus_4_7_and_4_8_list_at_opus_5_prices(self):
        # Taken from the pricing page, not assumed: $5 / $6.25 / $10 / $0.50 / $25.
        opus5 = self.prices["claude-opus-5"]
        for model in ("claude-opus-4-8", "claude-opus-4-7"):
            with self.subTest(model=model):
                self.assertEqual({k: self.prices[model][k] for k in
                                  ("input", "output", "cache_read", "cache_write", "cache_write_1h")},
                                 {k: opus5[k] for k in
                                  ("input", "output", "cache_read", "cache_write", "cache_write_1h")})

    def test_every_family_list_price_model_is_in_the_table(self):
        for fam, model in (self.credits.get("list_price_model") or {}).items():
            with self.subTest(family=fam):
                self.assertIn(model, self.prices)
                self.assertEqual(family(model, self.credits), fam)

    def test_the_new_rows_say_their_meter_weight_is_assumed(self):
        for model in ("claude-opus-5-5", "claude-opus-4-8", "claude-opus-4-7",
                      "claude-sonnet-4-6", "claude-haiku-4-5"):
            with self.subTest(model=model):
                self.assertIn("assumed", self.prices[model]["meter_weight_source"])

    def test_a_bundle_on_a_new_model_is_valued_rather_than_dropped(self):
        priced = {k: v for k, v in self.prices.items() if not k.startswith("_")}
        # 1M cache_write on Haiku 4.5 at $1.25/MTok, class weight 1.0, meter weight 1.0.
        self.assertAlmostEqual(
            bundle_meter_usd("claude-haiku-4-5", {"cache_write": 1_000_000}, priced), 1.25)
        # cache_read is weighted 0.0 on every row, new ones included.
        self.assertEqual(
            bundle_meter_usd("claude-sonnet-4-6", {"cache_read": 1_000_000}, priced), 0.0)
        self.assertIsNone(bundle_meter_usd("<synthetic>", {"input": 10}, priced))


if __name__ == "__main__":
    unittest.main()


class RegimeFiguresTests(unittest.TestCase):
    """`per_week_regimes` and `account_regimes`: each regime's own window times its own
    windows per week, pooled and per account, never one regime's figure held across another."""

    AT = datetime(2026, 9, 22, 17, tzinfo=timezone.utc)

    def regimes(self, values, intervals=None):
        bounds = [(None, C.CUT_AT.isoformat()), (C.CUT_AT.isoformat(), self.AT.isoformat()),
                  (self.AT.isoformat(), None)]
        return [{"from": f, "until": u, "value": v,
                 "interval": (intervals or [None] * 3)[k], "source": "x"}
                for k, ((f, u), v) in enumerate(zip(bounds, values))]

    def row(self, label, ending, d5, d7):
        return {"account": label, "window_ending": ending.isoformat(), "five_hour_pct": d5,
                "seven_day_pct": d7, "pieces": 1}

    def st(self, start, hours, v):
        return {"start": start.isoformat(), "end": (start + timedelta(hours=hours)).isoformat(),
                "delta_pct": 10.0, "tokens": {"v": v}}

    #: The detector's sizes at the two regime boundaries by default: each matches the fixture's
    #: own direct step, so restating every row from the newest moves nothing. 14 September:
    #: the week +10% (11 over 10 credits per 1%), windows per week carried. AT: the week
    #: +36.36% (15 over 11), the window +9.1%, windows per week +25% (2.5 over 2.0).
    CUT_SIZES: ClassVar[dict] = {"seven_day": (10.0, (5.0, 15.0)), "ratio": (0.0, (-3.0, 3.0))}
    AT_SIZES: ClassVar[dict] = {"seven_day": (36.3636, (30.0, 43.0)), "five_hour": (9.1, (4.0, 14.0)),
                                "ratio": (25.0, (20.0, 30.0))}

    def changes(self, cut=None, at=None, at_found_on="seven_day"):
        """The detector's measured changes at the two regime boundaries, sizes by line as
        (change %, (low, high)); a line left out, or whose interval holds no change, carries
        (the line that found the change always steps)."""
        return [measured(C.CUT_AT.isoformat(), **(self.CUT_SIZES if cut is None else cut)),
                measured(self.AT.isoformat(), at_found_on, **(self.AT_SIZES if at is None else at))]

    def stp(self, start, credits):
        """One seven-day step: 1% of the weekly meter, 4 five-hour points, `credits` of work."""
        return {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(),
                "d7": 1, "d5": 4, "tokens": {"v": credits}}

    def day_steps(self, day, credits, n=2):
        return [self.stp(day + timedelta(hours=h), credits) for h in range(n)]

    def default_steps(self):
        # a1 at 10, 11 and 12 credits per 1% of the seven-day meter; a2 at 18 after AT.
        cut, day = C.CUT_AT, timedelta(days=1)
        return {"one": (self.day_steps(cut - 5 * day, 10.0) + self.day_steps(cut - 4 * day, 10.0)
                        + self.day_steps(cut + day, 11.0) + self.day_steps(cut + 2 * day, 11.0)
                        + self.day_steps(self.AT + day, 12.0)
                        + self.day_steps(self.AT + 2 * day, 12.0)),
                "two": self.day_steps(self.AT + day, 18.0) + self.day_steps(self.AT + 2 * day, 18.0)}

    def figures(self, changes=None, stretches=None, families=None, sources=None, runs=None,
                steps=None):
        cut, day = C.CUT_AT, timedelta(days=1)
        max20 = {"by_window": [
            self.row("a1", cut - 5 * day, 60.0, 10.0),
            # a1 stepped three days before the cut: this reading is already after the change.
            self.row("a1", cut - 2 * day, 50.0, 10.0),
            self.row("a1", cut + day, 50.0, 10.0),
            self.row("a1", self.AT + day, 40.0, 10.0),
            self.row("a2", self.AT + 2 * day, 30.0, 10.0),
        ], "by_account": {"a1": {"step": {"percent": -17}, "regimes": [
            {"start": (cut - 9 * day).isoformat(), "end": (cut - 3 * day).isoformat()},
            {"start": (cut - 3 * day + timedelta(hours=1)).isoformat(), "end": self.AT.isoformat()}]}}}
        tokens = {"regimes": self.regimes([1000, 1100, 1200], [[800, 1200], [900, 1300], [1000, 1400]]),
                  "all": {"value": 1200, "interval": [1000, 1400], "status": None},
                  "per_family": {"opus": {"all": {"value": 1200, "interval": [1000, 1400]}},
                                 "sonnet": {"all": {"value": 2400, "interval": [2000, 2800]}}}}
        for k, reg in enumerate(tokens["regimes"]):
            if families:
                reg["measured_family"] = families[k]
            if sources and sources[k]:
                reg["source"] = sources[k]
            if runs:
                reg["run_from"] = runs[k]
        # Half a credit a token: a week of c credits per 1% is 200 c tokens.
        tokens["anchor_credits_per_token"] = 0.5
        credits = {"regimes": self.regimes([10_000, 11_000, 12_000])}

        def value(t):
            # Credits: the test's own `v`, else one credit a token.
            return t["v"] if "v" in t else sum(C.class_totals(t).values())
        if stretches is None:
            stretches = {"one": [self.st(cut - 5 * day, 2, 1000.0),
                                 self.st(self.AT - timedelta(hours=1), 3, 5.0),  # spans AT
                                 self.st(self.AT + day, 2, 1320.0)],
                         "two": []}
        return C.regime_figures(credits, tokens, max20, stretches, {"one": "a1", "two": "a2"},
                                value, changes=self.changes() if changes is None else changes,
                                credits=CREDITS,
                                steps=self.default_steps() if steps is None else steps)

    def test_every_regime_week_is_measured_on_the_seven_day_meter(self):
        per_week = self.figures()["per_week_regimes"]
        self.assertEqual([(r["from"], r["until"]) for r in per_week],
                         [(None, C.CUT_AT.isoformat()), (C.CUT_AT.isoformat(), self.AT.isoformat()),
                          (self.AT.isoformat(), None)])
        # Credits per 1% of the seven-day meter, pooled meter-weighted: a2's 18 counts as much
        # as a1's 12 after AT, four steps each.
        self.assertEqual([r["weekly_credits_per_pct"] for r in per_week], [10, 11, 15])
        self.assertEqual([r["seven_day_points"] for r in per_week], [4, 4, 8])
        self.assertEqual([r["weekly_accounts"] for r in per_week], [["a1"], ["a1"], ["a1", "a2"]])
        self.assertEqual([r["value"] for r in per_week], [2000, 2200, 3000])
        self.assertEqual([r["interval"] for r in per_week], [[2000, 2000], [2200, 2200], [3000, 3000]])
        self.assertEqual([r["per_week_source"] for r in per_week], ["seven_day_meter_direct"] * 3)
        self.assertEqual([r["window"] for r in per_week], [1000, 1100, 1200])

    def test_windows_per_week_is_the_week_over_the_window(self):
        per_week = self.figures()["per_week_regimes"]
        self.assertEqual([r["windows_per_week"] for r in per_week], [2.0, 2.0, 2.5])
        self.assertEqual([r["windows_per_week_source"] for r in per_week],
                         ["weekly_over_window"] * 3)
        # The week's ends over the window's opposite ends.
        self.assertEqual(per_week[0]["windows_per_week_interval"],
                         [round(2000 / 1200, 4), round(2000 / 800, 4)])
        # The meter ratios stay beside it for reference only: five-hour over seven-day points
        # in the same steps, and the window readings pooled.
        self.assertEqual([r["windows_per_week_meters"] for r in per_week], [4.0, 4.0, 4.0])
        self.assertEqual([r["windows_per_week_pooled"] for r in per_week], [5.5, 5.0, 3.5])
        for r in per_week:
            self.assertIsNone(r["per_week_factor"])

    def test_the_week_steps_by_the_seven_day_size_and_never_by_the_ratios(self):
        # The ratio at AT read -50%: windows per week steps by it, the week does not.
        rows = self.figures(self.changes(at={"seven_day": (36.3636, (30.0, 43.0)),
                                             "ratio": (-50.0, (-60.0, -40.0))}))["per_week_regimes"]
        self.assertEqual([r["value"] for r in rows], [2000, 2200, 3000])
        self.assertEqual([r["windows_per_week"] for r in rows], [5.0, 5.0, 2.5])
        self.assertEqual(rows[1]["windows_per_week_bridge"]["direct_value"], 2.0)

    def test_without_seven_day_steps_the_week_is_null(self):
        figures = self.figures(steps={})
        for r in figures["per_week_regimes"]:
            self.assertEqual((r["value"], r["interval"], r["windows_per_week"]), (None, None, None))
            self.assertIsNone(r["windows_per_week_source"])
            self.assertIsNotNone(r["windows_per_week_pooled"])
        week = figures["per_week"]
        self.assertIsNone(week["all"]["value"])
        self.assertIsNotNone(week["status"])

    @staticmethod
    def pure(start, model, total):
        return {"start": start.isoformat(), "end": (start + timedelta(hours=2)).isoformat(),
                "delta_pct": 10.0, "tokens": {model: tok(cache_read=total)}}

    def test_each_account_window_is_its_own_direct_median_unscaled(self):
        cut, day = C.CUT_AT, timedelta(days=1)
        pure = self.pure
        stretches = {"one": [*(pure(cut - (5 - k) * day, "claude-opus-5", total)
                               for k, total in enumerate((40_000, 60_000, 50_000, 45_000, 55_000))),
                             pure(self.AT + day, "claude-opus-5-5", 61_000),     # 610k
                             pure(self.AT + 2 * day, "claude-opus-5-5", 65_000),  # 650k
                             pure(self.AT + 3 * day, "claude-opus-5-5", 70_000),  # 700k
                             # Not the regime's family: no part of its window.
                             pure(self.AT + 4 * day, "claude-opus-5", 10_000)],
                     "two": []}
        figures = self.figures(stretches=stretches,
                               families=["opus", "opus", "opus-5-5"])["account_regimes"]
        a1, a2 = figures["a1"], figures["a2"]
        # The direct pure-family median is `measured_window`, never scaled, once there are as
        # many readings as a regime's own direct window needs; below that it is null and the
        # count says why (one stretch reads its own token mix, not the account's level).
        self.assertEqual((a1[0]["measured_window"], a1[0]["n_measured_window"]), (500_000, 5))
        self.assertEqual((a1[2]["measured_window"], a1[2]["n_measured_window"]), (None, 3))
        self.assertEqual(a1[2]["measured_family"], "opus-5-5")
        # a1 is the only account with readings, so it is the pool and sits at the regime
        # value; every stretch counts toward the relative figure, whatever its family.
        self.assertEqual((a1[0]["window"], a1[0]["n_window"]), (1000, 5))
        self.assertEqual((a1[1]["window"], a1[1]["n_window"]), (None, 0))
        self.assertEqual((a1[2]["window"], a1[2]["n_window"]), (1200, 4))
        # Each account's week is its own steps; its windows per week its week over its window.
        self.assertEqual([r["per_week"] for r in a1], [2000, 2200, 2400])
        self.assertEqual([r["weekly_credits_per_pct"] for r in a1], [10, 11, 12])
        self.assertEqual([r["windows_per_week"] for r in a1], [2.0, None, 2.0])
        # a1's readings are filed by the detector's change instants only: both of its windows
        # before 14 September are in the first row, whatever its own reset did.
        self.assertEqual((a1[0]["windows_per_week_meters"], a1[1]["windows_per_week_meters"]),
                         (5.5, 5.0))
        self.assertEqual([r["window"] for r in a2], [None, None, None])
        self.assertEqual([r["measured_window"] for r in a2], [None, None, None])
        self.assertEqual([r["per_week"] for r in a2], [None, None, 3600])
        self.assertIsNone(a2[2]["windows_per_week"])
        self.assertEqual(a2[2]["windows_per_week_meters"], 3.0)

    def test_an_account_window_is_its_level_relative_to_the_pool(self):
        # Mixed-family stretches only: no direct reading, but a relative window. The
        # account at the pooled median sits at the regime value; the other at its own
        # median over the pool's.
        day = timedelta(days=1)

        def mixed(start, credits):
            return {"start": start.isoformat(), "end": (start + timedelta(hours=2)).isoformat(),
                    "delta_pct": 10.0, "tokens": {"v": credits}}

        stretches = {"one": [mixed(self.AT + day, 100.0), mixed(self.AT + 2 * day, 200.0),
                             mixed(self.AT + 3 * day, 300.0)],
                     "two": [mixed(self.AT + day, 200.0), mixed(self.AT + 2 * day, 500.0)]}
        figures = self.figures(stretches=stretches, families=["opus", "opus", "opus-5-5"])
        a1, a2 = figures["account_regimes"]["a1"][-1], figures["account_regimes"]["a2"][-1]
        self.assertEqual((a1["window"], a1["n_window"]), (1200, 3))
        self.assertEqual((a2["window"], a2["n_window"]), (round(1200 * 35 / 20), 2))
        self.assertIsNone(a1["measured_window"])
        self.assertIsNone(a2["measured_window"])

    def test_account_windows_pool_the_accounts_readings_over_a_run(self):
        cut, day = C.CUT_AT, timedelta(days=1)

        def pure(start, total):
            return self.pure(start, "claude-opus-5-5", total)

        stretches = {"one": [pure(cut + day, 50_000),               # 500k, regime 1
                             pure(cut + 2 * day, 58_000),           # 580k, regime 1
                             # Spans the boundary but lies wholly in the run.
                             pure(self.AT - timedelta(hours=1), 70_000),
                             pure(self.AT + day, 61_000),           # 610k, regime 2
                             pure(self.AT + 2 * day, 64_000)],      # 640k, regime 2
                     "two": [pure(self.AT + 2 * day, 66_000)]}
        figures = self.figures(stretches=stretches, families=["opus", "opus-5-5", "opus-5-5"],
                               runs=[None, cut.isoformat(), cut.isoformat()])["account_regimes"]
        a1, a2 = figures["a1"], figures["a2"]
        for row in a1[1:]:
            self.assertEqual((row["measured_window"], row["n_window"], row["n_measured_window"]),
                             (610_000, 5, 5))
        for row in a2[1:]:
            self.assertEqual((row["measured_window"], row["n_window"], row["n_measured_window"]),
                             (None, 1, 1))
        self.assertEqual(a1[0]["window"], None)

    def test_a_change_whose_seven_day_size_holds_no_change_states_one_week_either_side(self):
        # Found on the ratio, with a seven-day size whose interval holds no change: the week
        # does not step at AT. Both rows state one week, over every step from 14 September
        # (a1 at 11 four times and 12 four times, a2 at 18 four times).
        rows = self.figures(self.changes(at={"seven_day": (6.7, (-2.0, 15.0)),
                                             "five_hour": (9.1, (4.0, 14.0)),
                                             "ratio": (-10.0, (-16.0, -4.0))},
                                         at_found_on="ratio"))["per_week_regimes"]
        self.assertEqual(rows[1]["value"], rows[2]["value"])
        self.assertEqual((rows[1]["seven_day_points"], rows[2]["seven_day_points"]), (12, 12))
        self.assertEqual(rows[1]["weekly_credits_per_pct"], round((11 * 4 + 12 * 4 + 18 * 4) / 12))
        # Windows per week still steps by the ratio's size there.
        self.assertAlmostEqual(rows[2]["windows_per_week"] / rows[1]["windows_per_week"], 0.9, places=3)
        # The 14 September row is its own week.
        self.assertEqual(rows[0]["seven_day_points"], 4)

    def test_the_hero_week_is_the_newest_regimes_figure(self):
        figures = self.figures()
        newest, week = figures["per_week_regimes"][-1], figures["per_week"]
        self.assertEqual(week["all"]["value"], newest["value"])
        self.assertEqual(week["all"]["interval"], newest["interval"])
        self.assertEqual(week["per_family"]["opus"]["all"], {"value": newest["value"],
                                                              "interval": newest["interval"]})
        # Every family's week moves from its window as the anchor's does, end by end.
        self.assertEqual(week["per_family"]["sonnet"]["all"]["value"], 2 * newest["value"])
        self.assertEqual(week["per_family"]["sonnet"]["all"]["interval"],
                         [round(2000 / 1000 * newest["interval"][0]),
                          round(2800 / 1400 * newest["interval"][1])])
        self.assertEqual(week["windows_per_week"]["value"], newest["windows_per_week"])
        self.assertIn("newest regime", week["windows_per_week"]["source"])
        self.assertIn("weekly_over_window", week["windows_per_week"]["source"])
        self.assertIsNone(week["status"])

    def test_each_week_step_is_the_detectors_seven_day_size(self):
        # The pooled levels step 2200 -> 3000 (+36%, a2 joins after AT); the detector measured
        # +25% on every account against itself. The newest row stays its own direct level and
        # every row before it is restated by the detector's steps: +25% at AT, +10% at 14 Sep.
        figures = self.figures(self.changes(at={"seven_day": (25.0, (10.0, 40.0)),
                                                "ratio": (25.0, (20.0, 30.0))}))
        rows = figures["per_week_regimes"]
        self.assertEqual([r["value"] for r in rows], [round(2400 / 1.1), 2400, 3000])
        self.assertAlmostEqual(rows[2]["value"] / rows[1]["value"], 1.25, places=3)
        self.assertEqual([r["week_bridge"]["direct_value"] for r in rows[:2]], [2000, 2200])
        self.assertEqual(rows[0]["week_bridge"]["factor"], round(3000 / 1.25 / 2200, 6))
        self.assertIsNone(rows[2].get("week_bridge"))
        # The hero week is the newest direct level, not moved by the restatement.
        self.assertEqual(figures["per_week"]["all"]["value"], 3000)

    def test_windows_per_week_is_carried_where_the_ratio_holds_no_change(self):
        rows = self.figures(self.changes(at={"seven_day": (25.0, (10.0, 40.0)),
                                             "ratio": (1.0, (-5.0, 7.0))}))["per_week_regimes"]
        self.assertEqual([r["windows_per_week"] for r in rows], [2.5, 2.5, 2.5])
        self.assertEqual([r["windows_per_week_bridge"]["direct_value"] for r in rows[:2]], [2.0, 2.0])


class CertifiedWeekChainTests(unittest.TestCase):
    """`chain_certified_weeks` on rows by hand: every step the detector's own size."""

    AT = "2026-09-22T19:41:49+00:00"

    def rows(self):
        return [{"from": None, "value": 2600, "interval": [2300, 2800], "windows_per_week": 5.7,
                 "windows_per_week_interval": [4.2, 16.0], "windows_per_week_source": "weekly_over_window"},
                {"from": C.CUT_AT.isoformat(), "value": 2300, "interval": [2100, 2550],
                 "windows_per_week": 4.6, "windows_per_week_interval": [2.9, 7.0],
                 "windows_per_week_source": "weekly_over_window"},
                {"from": self.AT, "value": 3000, "interval": [2900, 3100], "windows_per_week": 6.0,
                 "windows_per_week_interval": [4.0, 8.4], "windows_per_week_source": "weekly_over_window"}]

    def changes(self, cut=None, at=None):
        return [measured(C.CUT_AT.isoformat(), found_on="ratio",
                         **(cut or {"ratio": (-17.0, (-22.0, -12.0)), "seven_day": (-9.9, (-20.0, 1.0))})),
                measured(self.AT, **(at or {"seven_day": (29.6, (20.0, 40.0)),
                                            "ratio": (2.0, (-4.0, 8.0))}))]

    def test_live_shape(self):
        rows = self.rows()
        C.chain_certified_weeks(rows, self.changes())
        # The week: +29.6% at AT, carried at 14 September (its seven-day size holds no change).
        self.assertAlmostEqual(rows[2]["value"] / rows[1]["value"] - 1, 0.296, places=3)
        self.assertEqual(rows[1]["value"], rows[0]["value"])
        # Windows per week: carried at AT (the ratio holds no change), -17% at 14 September.
        self.assertEqual(rows[2]["windows_per_week"], rows[1]["windows_per_week"])
        self.assertAlmostEqual(rows[1]["windows_per_week"] / rows[0]["windows_per_week"], 0.83, places=3)
        self.assertEqual([r["week_bridge"]["at"] for r in rows[:2]], [[C.CUT_AT.isoformat()], [self.AT]])
        self.assertEqual([r["week_bridge"]["direct_value"] for r in rows[:2]], [2600, 2300])

    def test_a_drawn_seven_day_size_steps_the_week_by_it(self):
        rows = self.rows()
        C.chain_certified_weeks(rows, self.changes(cut={"ratio": (-17.0, (-22.0, -12.0)),
                                                        "seven_day": (-9.9, (-15.0, -4.0))}))
        self.assertAlmostEqual(rows[1]["value"] / rows[0]["value"] - 1, -0.099, places=3)
        self.assertAlmostEqual(rows[2]["value"] / rows[1]["value"] - 1, 0.296, places=3)

    def test_the_newest_row_is_never_restated(self):
        rows = self.rows()
        C.chain_certified_weeks(rows, self.changes())
        self.assertEqual((rows[2]["value"], rows[2]["windows_per_week"]), (3000, 6.0))
        self.assertNotIn("week_bridge", rows[2])

    def test_a_row_with_no_figure_stays_null(self):
        rows = self.rows()
        rows[1]["value"] = None
        C.chain_certified_weeks(rows, self.changes())
        self.assertIsNone(rows[1]["value"])
        self.assertEqual(rows[0]["value"], 2600)
