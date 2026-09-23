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

    def test_the_solved_interval_reaches_the_published_block(self):
        fable = self.credits["fable_interval"]
        self.assertEqual((fable["input_low"], fable["input_high"]), (1.0, 2.5))
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
        self.assertEqual((self.credits["fable_interval"]["input_low"],
                          self.credits["fable_interval"]["input_high"]), (1.0, 2.5))

    def test_the_api_value_and_the_session_count_are_values_too(self):
        rate = C.family_rate("fable", CREDITS, C.load_model_rates())
        tokens = round(20_000_000 / rate.input)
        api = self.credits["per_model"]["fable"]["api_value_per_window_usd"]["input"]
        self.assertIsNotNone(api["value"])
        self.assertEqual(api["value"], round(tokens * PRICES["claude-fable-5-1"]["input"] / 1e6, 2))
        sessions = self.credits["sessions"].get("claude-fable-5-1")
        if sessions is not None:
            self.assertIsNotNone(sessions["per_window"]["value"])


class AcrossCutTests(unittest.TestCase):
    def block(self):
        before = [stretch("2026-09-10T00:00:00+00:00", {"claude-opus-5": tok(input=3_000_000)})]
        after = [stretch("2026-09-16T00:00:00+00:00", {"claude-opus-5": tok(input=3_000_000)})]
        return C.across_cut({"jwork": before + after}, CREDITS, LABELS)

    def test_each_side_of_the_announced_change_gets_its_own_median_and_count(self):
        a2 = self.block()["per_account"]["a2"]
        self.assertEqual((a2["n_before"], a2["n_after"]), (1, 1))
        self.assertEqual(a2["before"], a2["after"])
        self.assertEqual(a2["change_pct"], 0.0)

    def test_an_account_with_one_side_only_reports_no_change(self):
        only_after = C.across_cut({"dave": [stretch("2026-09-16T00:00:00+00:00",
                                                    {"claude-opus-5": tok(input=3_000_000)})]},
                                  CREDITS, LABELS)
        self.assertIsNone(only_after["per_account"]["a3"]["change_pct"])
        self.assertIsNone(only_after["per_account"]["a3"]["before"])

    def test_the_held_fable_rate_is_published_with_the_comparison_it_makes_possible(self):
        held = self.block()["fable_rate_held"]
        self.assertEqual(held["input"], 1.667)
        self.assertEqual(held["output"], 5.0)
        self.assertIn("not a claim about Fable's rate", held["why"])

    def test_the_attribution_is_unresolved_regardless_of_the_cross_account_spread(self):
        """Two accounts on the same plan 25% apart after a change one moved 10% across.

        An earlier version called this spread itself the reason the attribution was
        unresolved. The Codex review of commit 447b926 (2026-09-20) retracted that: a
        stable account-specific scale cancels out of a within-account ratio no matter
        how far apart two accounts sit, so the spread was never evidence. `resolved`
        stays false and `unresolved` states the algebraic reason instead, and neither
        field is published any more (`spread_after_pct`, `largest_move_pct`).
        """
        rows = {
            "jwork": [stretch("2026-09-10T00:00:00+00:00", {"claude-opus-5": tok(input=3_000_000)}),
                      stretch("2026-09-16T00:00:00+00:00", {"claude-opus-5": tok(input=3_300_000)})],
            "dave": [stretch("2026-09-16T00:00:00+00:00", {"claude-opus-5": tok(input=2_640_000)})],
        }
        out = C.across_cut(rows, CREDITS, LABELS)
        self.assertFalse(out["resolved"])
        self.assertNotIn("spread_after_pct", out)
        self.assertNotIn("largest_move_pct", out)
        self.assertEqual(out["unresolved"], C.ACROSS_CUT_UNRESOLVED)
        self.assertNotIn("differ from each other", out["unresolved"])

    def test_it_never_says_the_window_did_not_move(self):
        text = json.dumps(self.block())
        for claim in ("did not move", "flat", "unchanged"):
            self.assertNotIn(claim, text)

    def test_one_account_alone_is_unresolved_for_the_same_algebraic_reason(self):
        out = self.block()
        self.assertFalse(out["resolved"])
        self.assertEqual(out["unresolved"], C.ACROSS_CUT_UNRESOLVED)

    def test_an_account_with_no_usable_capture_column_says_so_in_its_own_row(self):
        blind = [dict(stretch("2026-09-10T00:00:00+00:00", {"claude-opus-5": tok(input=3_000_000)}),
                      capture=None)]
        out = C.across_cut({"masterrig": blind}, CREDITS, LABELS)
        self.assertEqual(out["per_account"]["a1"]["n_with_capture"], 0)


def _published(gs=None, masterrig=None, passive=None, effort_meta=None, prices=None):
    """A public JSON built over planted stretches, with everything else minimal."""
    passive = passive or {"split": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
                          "session_tokens": {"claude-opus-5": 1_000_000}}
    return build_public_json([], passive, {}, prices or PRICES, NOW,
                             gs_passive=gs, masterrig_passive=masterrig,
                             effort_meta=effort_meta, credits=CREDITS)


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

    def test_a_family_with_no_measurable_rate_publishes_a_status_sentence_and_no_number(self):
        """Haiku: no clean stretch anywhere carries enough Haiku to fit a rate.

        The reference table has a Haiku row and the page draws it, but nothing divides by it,
        so the row publishes the sentence saying so and no token figure at all.

        The sentence itself is whatever history/model-rates.json holds -- the reason has
        already changed once, when 2026-09-23 priced Haiku 4.5 and its tokens started
        reaching the fits without ever carrying a stretch -- so this reads it from there
        rather than pinning the wording. What must not change is the shape: a status
        sentence, no value, no interval, and the reference figure still carried.
        """
        haiku = self.credits["per_model"]["haiku"]
        expected = C.load_model_rates()["per_family"]["haiku"]["status"]
        self.assertTrue(expected.startswith("not measurable"), expected)
        self.assertEqual(haiku["status"], expected)
        self.assertEqual(haiku["rate_source"], "measured")
        self.assertIsNone(haiku["credits_per_token"]["input"])
        self.assertIsNone(haiku["credits_per_token_interval"])
        for side in ("input", "output"):
            figure = haiku["tokens_per_window"][side]
            self.assertIsNone(figure["value"])
            self.assertIsNone(figure["interval"])
            self.assertEqual(figure["status"], expected)
        # The reference figure is still carried, for the page to draw beside the sentence.
        self.assertEqual(haiku["reference_rate"]["input"], 2 / 15)

    def test_every_per_model_row_says_where_its_rate_came_from(self):
        for fam, row in self.credits["per_model"].items():
            self.assertIn(row["rate_source"], ("measured", "reference"), fam)
            self.assertIn("reference_rate", row, fam)
        # Opus alone reads `reference`: it is the unit anchor, and nothing here tests it.
        sources = {fam: row["rate_source"] for fam, row in self.credits["per_model"].items()}
        self.assertEqual(sources["opus"], "reference")
        self.assertTrue(self.credits["per_model"]["opus"]["anchor"])
        self.assertEqual({fam for fam, v in sources.items() if v == "measured"},
                         {"sonnet", "haiku", "fable"})

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
        stretches and does not depend on this fixture. The committed fit's three per-account
        fits disagree, so the row still carries a value (their median) and the union of their
        intervals, with `status` null and `agree` false -- not a status sentence in place of a
        number.
        """
        fable = self.credits["fable_interval"]
        self.assertIsNone(fable["input_low"])
        self.assertIsNone(fable["input_high"])
        self.assertEqual(fable["unresolved"], "no Fable-heavy stretch to solve a rate from")
        row = self.credits["per_model"]["fable"]
        self.assertIsNotNone(row["tokens_per_window"]["input"]["value"])
        self.assertIsNone(row["status"])
        rate = C.family_rate("fable", CREDITS, C.load_model_rates())
        self.assertFalse(rate.detail["agree"])
        self.assertIn("disagree", rate.detail["why"])

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

    def test_the_cross_check_needs_a_certified_weekly_change_to_anchor_on(self):
        block = self.credits["window_credits_from_weekly"]
        self.assertEqual(block["kind"], "cross_check")
        self.assertIsNone(block["before"])
        self.assertIn("no certified weekly change", block["status"])

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
    return {"gs": root / "history/gs-passive.json",
            "masterrig": root / "history/masterrig-passive.json",
            "probes": root / "history/probes.jsonl",
            "effort_matrix": root / "data/effort_matrix.json",
            "passive": root / "history/passive.json",
            "prices": root / "data/prices.json",
            "model_rates": root / "history/model-rates.json"}


def _publish(paths: dict, now: datetime = NOW) -> dict:
    """A real publish over the files `_files` planted, through the publisher's own path.

    The published document and the check's rebuild then come off the same six files, so a
    test that plants a figure and expects the check to catch it is testing the check and
    not a difference between two ways of assembling a fixture.
    """
    return rebuild_public_json(now, probes=paths["probes"], passive=paths["passive"],
                               effort=paths["effort_matrix"], prices=paths["prices"],
                               gs_passive=paths["gs"], masterrig_passive=paths["masterrig"],
                               model_rates=paths["model_rates"])


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
        priced = {k: v for k, v in raw.items() if not k.startswith("_")}
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
            for d in range(15, 27)]},
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

    def test_the_week_is_published_because_this_fixture_measures_windows_per_week(self):
        """The fixture carries a weekly series, so per_week is a product and not a status."""
        published = self.check()[2]
        per_week = published["credits"]["window_tokens"]["per_week"]
        windows = published["weekly_windows"]["max20"]["current"]
        self.assertEqual(per_week["windows_per_week"]["value"], windows)
        self.assertEqual(per_week["all"]["value"],
                         round(published["credits"]["window_tokens"]["all"]["value"] * windows))
        self.assertIsNone(per_week["status"])

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
        haiku = self.credits["per_model"]["haiku"]
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
                                            paths["effort_matrix"], paths["passive"], paths["prices"])
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
                                            paths["passive"], paths["prices"])
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


class WindowsPerWeekRatioNoteTests(unittest.TestCase):
    """C.windows_per_week_ratio_note: the retracted 47%-spread argument's replacement.

    The measured fact is the pooled ratio of five-hour to seven-day meter movement over
    the rows behind the last two certified regimes; everything else here is a worked
    example of the one-equation-two-unknowns algebra, not a claim about which meter moved.
    """

    def _row(self, ending: str, d5: float, d7: float, account: str = "a1") -> dict:
        return {"window_ending": ending, "five_hour_pct": d5, "seven_day_pct": d7, "account": account}

    def _weekly(self, regimes: list[dict], by_window: list[dict]) -> dict:
        return {"max20": {"regimes": regimes, "by_window": by_window}}

    def _regime(self, start: str, end: str) -> dict:
        return {"start": start, "end": end}

    def test_fewer_than_two_regimes_is_not_measurable(self):
        self.assertIsNone(C.windows_per_week_ratio_note(
            self._weekly([self._regime("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00")], [])))
        self.assertIsNone(C.windows_per_week_ratio_note(self._weekly([], [])))

    def test_a_regime_whose_rows_sum_to_zero_seven_day_movement_is_not_measurable(self):
        regimes = [self._regime("2026-09-01T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
                  self._regime("2026-09-06T00:00:00+00:00", "2026-09-10T00:00:00+00:00")]
        by_window = [self._row("2026-09-03T00:00:00+00:00", 5.0, 0.0),
                    self._row("2026-09-08T00:00:00+00:00", 3.0, 10.0)]
        self.assertIsNone(C.windows_per_week_ratio_note(self._weekly(regimes, by_window)))

    def test_the_rows_pooled_are_exactly_those_inside_each_regimes_own_span(self):
        regimes = [self._regime("2026-09-01T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
                  self._regime("2026-09-06T00:00:00+00:00", "2026-09-10T00:00:00+00:00")]
        by_window = [
            self._row("2026-08-31T00:00:00+00:00", 99.0, 99.0),  # before the first regime: excluded
            self._row("2026-09-02T00:00:00+00:00", 10.0, 20.0),
            self._row("2026-09-04T00:00:00+00:00", 5.0, 10.0),
            self._row("2026-09-07T00:00:00+00:00", 4.0, 10.0),
            self._row("2026-09-09T00:00:00+00:00", 6.0, 10.0),
            self._row("2026-09-11T00:00:00+00:00", 1.0, 1.0),  # after the last regime: excluded
        ]
        out = C.windows_per_week_ratio_note(self._weekly(regimes, by_window))
        self.assertEqual(out["before"]["n_windows"], 2)
        self.assertEqual(out["before"]["sum_five_hour_pct"], 15.0)
        self.assertEqual(out["before"]["sum_seven_day_pct"], 30.0)
        self.assertEqual(out["after"]["n_windows"], 2)
        self.assertEqual(out["after"]["sum_five_hour_pct"], 10.0)
        self.assertEqual(out["after"]["sum_seven_day_pct"], 20.0)

    def test_a_window_at_a_shared_boundary_instant_lands_in_the_earlier_regime_only(self):
        # Two accounts' windows can share a window_ending instant that sits exactly on a
        # regime boundary. The earlier regime's own `end` and the later regime's own
        # `start` are both that instant here; the boundary row must be counted once.
        boundary = "2026-09-05T12:00:00+00:00"
        regimes = [self._regime("2026-09-01T00:00:00+00:00", boundary),
                  self._regime(boundary, "2026-09-10T00:00:00+00:00")]
        by_window = [
            self._row("2026-09-02T00:00:00+00:00", 10.0, 20.0, account="a1"),
            self._row(boundary, 5.0, 10.0, account="a1"),
            self._row("2026-09-08T00:00:00+00:00", 4.0, 10.0, account="a2"),
        ]
        out = C.windows_per_week_ratio_note(self._weekly(regimes, by_window))
        self.assertEqual(out["before"]["n_windows"], 2)
        self.assertEqual(out["before"]["sum_seven_day_pct"], 30.0)
        self.assertEqual(out["after"]["n_windows"], 1)
        self.assertEqual(out["after"]["sum_seven_day_pct"], 10.0)
        # Total seven-day movement pooled is unchanged; the boundary row was not dropped
        # or duplicated, only assigned once.
        self.assertEqual(out["before"]["sum_seven_day_pct"] + out["after"]["sum_seven_day_pct"], 40.0)

    def test_a_row_at_the_after_regimes_own_start_is_not_the_boundary_tie_and_is_kept(self):
        # The after regime's own first window sits at its own `start`, which is not the
        # shared instant unless it also equals the before regime's `end`. Excluding every
        # row at the after regime's `start` -- rather than only the one shared instant --
        # would drop this window from both sums, counting it nowhere.
        regimes = [self._regime("2026-09-01T00:00:00+00:00", "2026-09-04T00:00:00+00:00"),
                  self._regime("2026-09-06T00:00:00+00:00", "2026-09-10T00:00:00+00:00")]
        by_window = [
            self._row("2026-09-02T00:00:00+00:00", 10.0, 20.0, account="a1"),
            self._row("2026-09-06T00:00:00+00:00", 4.0, 10.0, account="a2"),  # after's own start
            self._row("2026-09-08T00:00:00+00:00", 6.0, 10.0, account="a2"),
        ]
        out = C.windows_per_week_ratio_note(self._weekly(regimes, by_window))
        self.assertEqual(out["before"]["n_windows"], 1)
        self.assertEqual(out["before"]["sum_seven_day_pct"], 20.0)
        self.assertEqual(out["after"]["n_windows"], 2)
        self.assertEqual(out["after"]["sum_seven_day_pct"], 20.0)
        self.assertEqual(out["after"]["sum_five_hour_pct"], 10.0)
        # Every row is accounted for exactly once.
        self.assertEqual(out["before"]["sum_seven_day_pct"] + out["after"]["sum_seven_day_pct"], 40.0)

    def test_rho_fall_pct_and_five_hour_only_pct(self):
        regimes = [self._regime("2026-09-01T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
                  self._regime("2026-09-06T00:00:00+00:00", "2026-09-10T00:00:00+00:00")]
        # before: ratio d5/d7 = 20/10 = 2.0; after: ratio = 10/10 = 1.0; rho = 1.0/2.0 = 0.5
        by_window = [self._row("2026-09-02T00:00:00+00:00", 20.0, 10.0),
                    self._row("2026-09-07T00:00:00+00:00", 10.0, 10.0)]
        out = C.windows_per_week_ratio_note(self._weekly(regimes, by_window))
        self.assertAlmostEqual(out["before"]["ratio"], 2.0)
        self.assertAlmostEqual(out["after"]["ratio"], 1.0)
        self.assertAlmostEqual(out["ratio_fell_pct"], 50.0)
        # five_hour_only_pct = (1/rho - 1) * 100 = (2.0 - 1) * 100 = 100.0
        five_hour_only = next(s for s in out["consistent_with"]
                              if s["weekly_cap_change_pct"] == 0.0)
        self.assertAlmostEqual(five_hour_only["five_hour_window_change_pct"], 100.0)
        weekly_only = next(s for s in out["consistent_with"]
                           if s["five_hour_window_change_pct"] == 0.0)
        self.assertAlmostEqual(weekly_only["weekly_cap_change_pct"], -50.0)

    def test_the_announced_17_percent_split_is_the_third_worked_example(self):
        regimes = [self._regime("2026-09-01T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
                  self._regime("2026-09-06T00:00:00+00:00", "2026-09-10T00:00:00+00:00")]
        by_window = [self._row("2026-09-02T00:00:00+00:00", 20.0, 10.0),
                    self._row("2026-09-07T00:00:00+00:00", 10.0, 10.0)]
        out = C.windows_per_week_ratio_note(self._weekly(regimes, by_window))
        announced = next(s for s in out["consistent_with"]
                         if s["weekly_cap_change_pct"] == -17.0)
        rho = 0.5
        expected_five_hour = round(((1 - 0.17) / rho - 1) * 100, 2)
        self.assertAlmostEqual(announced["five_hour_window_change_pct"], expected_five_hour)


class EventRecordWeeklyRatioWiringTests(unittest.TestCase):
    """_event_record passes weekly_windows through to windows_per_week_ratio_note, or None."""

    def _event(self):
        from datetime import date
        from tracker.detect import ChangeEvent
        return ChangeEvent(date=date(2026, 9, 15), direction="down", percent=10, model="all",
                          confirmed_at=date(2026, 9, 16), evidence_points=5, denominator_pct=50.0,
                          before_interval=(1.0, 2.0), after_interval=(3.0, 4.0))

    def test_a_falsy_weekly_windows_argument_publishes_none_without_calling_the_note(self):
        from tracker.publish import _event_record
        for empty in (None, {}):
            out = _event_record(self._event(), "weekly", across_cut=None, weekly_windows=empty)
            self.assertIsNone(out["windows_per_week_ratio"])

    def test_a_populated_weekly_windows_argument_publishes_the_note(self):
        from tracker.publish import _event_record
        regimes = [{"start": "2026-09-01T00:00:00+00:00", "end": "2026-09-05T00:00:00+00:00"},
                  {"start": "2026-09-06T00:00:00+00:00", "end": "2026-09-10T00:00:00+00:00"}]
        by_window = [{"window_ending": "2026-09-02T00:00:00+00:00", "five_hour_pct": 20.0,
                     "seven_day_pct": 10.0},
                    {"window_ending": "2026-09-07T00:00:00+00:00", "five_hour_pct": 10.0,
                     "seven_day_pct": 10.0}]
        weekly_windows = {"max20": {"regimes": regimes, "by_window": by_window}}
        out = _event_record(self._event(), "weekly", across_cut=None, weekly_windows=weekly_windows)
        self.assertIsNotNone(out["windows_per_week_ratio"])
        self.assertAlmostEqual(out["windows_per_week_ratio"]["ratio_fell_pct"], 50.0)

    def test_window_scope_events_carry_no_weekly_ratio_key_at_all(self):
        from tracker.publish import _event_record
        out = _event_record(self._event(), "window", across_cut=None, weekly_windows=None)
        self.assertNotIn("windows_per_week_ratio", out)


class FiveHourWindowChangeTests(unittest.TestCase):
    """Which accounts' across-the-cut readings are allowed to state a five-hour change."""

    @staticmethod
    def across(**per_account):
        return {"per_account": {label: {"change_pct": pct, "n_with_capture": capture,
                                        "n_before": before, "n_after": after}
                                for label, (pct, capture, before, after) in per_account.items()}}

    def test_the_account_with_a_usable_capture_column_and_both_sides_carries_it(self):
        out = C.five_hour_window_change(self.across(a2=(8.6, 56, 40, 15)))
        self.assertEqual(out, {"pct": 8.6, "accounts": ["a2"]})

    def test_an_account_with_no_capture_column_is_not_read(self):
        # a1's meter counts machines its transcripts never saw, so its two medians are
        # not a reading of its own window however far apart they are.
        self.assertIsNone(C.five_hour_window_change(self.across(a1=(17.2, 0, 166, 32))))

    def test_a_thin_side_is_not_read(self):
        for sides in ((9, 15), (40, 9)):
            with self.subTest(sides=sides):
                self.assertIsNone(C.five_hour_window_change(
                    self.across(a2=(8.6, 56, sides[0], sides[1]))))

    def test_two_qualifying_accounts_give_the_median_of_their_own_changes(self):
        out = C.five_hour_window_change(self.across(a2=(8.6, 56, 40, 15), a3=(12.6, 19, 20, 20)))
        self.assertEqual(out, {"pct": 10.6, "accounts": ["a2", "a3"]})

    def test_nothing_at_all_is_None_rather_than_zero(self):
        for across in (None, {}, {"per_account": {}},
                       self.across(a3=(None, 19, 0, 18))):
            with self.subTest(across=across):
                self.assertIsNone(C.five_hour_window_change(across))


class WindowClusterCutTests(unittest.TestCase):
    """The pure-Opus cluster split on 14 September, and which side states the window NOW.

    The live cluster is ten stretches from one account before the cut and two from another
    after it, so a median over the whole of it is a pre-cut reading published as the
    current window. The split is on each stretch's own start against C.CUT_AT.
    """

    BEFORE: ClassVar[list] = [opus_stretch(f"2026-09-{d:02d}T00:00:00+00:00", level)
                              for d, level in ((10, 100_000), (11, 110_000),
                                               (12, 120_000), (13, 130_000))]
    AFTER: ClassVar[list] = [opus_stretch(f"2026-09-{d:02d}T00:00:00+00:00", level)
                             for d, level in ((16, 200_000), (17, 220_000))]

    def window(self, before=None, after=None, five_hour_pct=None):
        clean = C.clean_stretches({"jwork": (self.BEFORE if before is None else before),
                                   "dave": (self.AFTER if after is None else after)}, [])
        return C.window_credits(clean, CREDITS, LABELS, five_hour_pct=five_hour_pct)

    def test_each_side_publishes_its_own_median_spread_and_count(self):
        window = self.window()
        self.assertEqual(window["cut_at"], C.CUT_AT.isoformat())
        self.assertEqual(window["before"], {"value": 11_500_000,
                                            "interval": [10_000_000, 13_000_000], "n": 4})
        self.assertEqual(window["after"], {"value": 21_000_000,
                                           "interval": [20_000_000, 22_000_000], "n": 2})
        self.assertEqual(window["n"], 6)

    def test_a_thin_after_cluster_leaves_the_before_cluster_scaled_to_now(self):
        window = self.window(five_hour_pct=8.6)
        self.assertEqual(window["current_source"], "before_cluster_scaled_by_five_hour_change")
        self.assertEqual(window["value"], round(11_500_000 * 1.086))
        self.assertEqual(window["credits_per_pct"], round(115_000 * 1.086))
        self.assertEqual(window["interval"], [round(10_000_000 * 1.086), round(13_000_000 * 1.086)])
        self.assertEqual(window["five_hour_window_pct"], 8.6)

    def test_a_thick_after_cluster_states_the_window_itself(self):
        after = [opus_stretch(f"2026-09-{d:02d}T00:00:00+00:00", level)
                 for d, level in ((15, 180_000), (16, 190_000), (17, 200_000),
                                  (18, 210_000), (19, 220_000))]
        window = self.window(after=after, five_hour_pct=8.6)
        self.assertEqual(window["current_source"], "after_cluster")
        self.assertEqual((window["value"], window["interval"]),
                         (20_000_000, [18_000_000, 22_000_000]))

    def test_with_no_measured_five_hour_change_the_before_cluster_goes_out_unscaled(self):
        window = self.window()
        self.assertEqual(window["current_source"], "before_cluster_unscaled")
        self.assertEqual(window["value"], 11_500_000)
        self.assertIsNone(window["five_hour_window_pct"])

    def test_the_value_always_sits_inside_its_own_interval(self):
        for pct in (None, 8.6, -20.0):
            with self.subTest(pct=pct):
                window = self.window(five_hour_pct=pct)
                self.assertLessEqual(window["interval"][0], window["value"])
                self.assertLessEqual(window["value"], window["interval"][1])

    def test_a_cluster_with_nothing_placeable_publishes_the_whole_of_it(self):
        # A fixture whose stretches carry no start stamp: neither side can claim them,
        # and the figure is the whole cluster with `current_source` saying so.
        undated = [dict(st, start=None) for st in self.BEFORE]
        window = self.window(before=undated, after=[], five_hour_pct=8.6)
        self.assertEqual((window["current_source"], window["value"]),
                         ("whole_cluster_unsplit", 11_500_000))
        self.assertEqual((window["before"]["n"], window["after"]["n"]), (0, 0))

    def test_an_empty_cluster_states_no_current_source_at_all(self):
        window = self.window(before=[], after=[])
        self.assertIsNone(window["value"])
        self.assertIsNone(window["current_source"])


class WindowTokensCutTests(unittest.TestCase):
    """The same split on the token figures: `all` is now, `per_class` carries both sides."""

    #: 10% stretches, so a window is ten times the stretch's own counts. Before: 116,000
    #: and 232,000 tokens a window; after: 500,000 and 600,000.
    BEFORE: ClassVar[list] = [classed_stretch("2026-09-10T00:00:00+00:00", 100, 1_000, 10_000, 500),
                              classed_stretch("2026-09-11T00:00:00+00:00", 200, 2_000, 20_000, 1_000)]
    AFTER: ClassVar[list] = [classed_stretch("2026-09-16T00:00:00+00:00", 500, 5_000, 43_000, 1_500),
                             classed_stretch("2026-09-17T00:00:00+00:00", 600, 6_000, 51_500, 1_900)]

    def block(self, before=None, after=None, five_hour_pct=None, **kw):
        clean = C.clean_stretches({"jwork": (self.BEFORE if before is None else before),
                                   "dave": (self.AFTER if after is None else after)}, [])
        return C.window_tokens(clean, CREDITS, LABELS, None, five_hour_pct=five_hour_pct, **kw)

    def test_the_all_classes_figure_carries_both_sides_at_the_top_level(self):
        block = self.block()
        self.assertEqual(block["cut_at"], C.CUT_AT.isoformat())
        self.assertEqual(block["before"], {"value": 174_000, "interval": [116_000, 232_000], "n": 2})
        self.assertEqual(block["after"], {"value": 550_000, "interval": [500_000, 600_000], "n": 2})

    def test_each_class_carries_its_own_two_sides(self):
        per_class = self.block()["per_class"]
        self.assertEqual(per_class["cache_read"]["before"],
                         {"value": 150_000, "interval": [100_000, 200_000], "n": 2})
        self.assertEqual(per_class["cache_read"]["after"],
                         {"value": 472_500, "interval": [430_000, 515_000], "n": 2})
        # The class median itself stays the whole cluster's: it is the mix the per-family
        # conversions hold their shape from, and each side is published beside it.
        self.assertEqual(per_class["cache_read"]["value"], 315_000)

    def test_all_is_the_before_cluster_scaled_while_the_after_cluster_is_thin(self):
        block = self.block(five_hour_pct=8.6)
        self.assertEqual(block["current_source"], "before_cluster_scaled_by_five_hour_change")
        self.assertEqual(block["all"]["value"], round(174_000 * 1.086))
        self.assertEqual(block["all"]["interval"],
                         [round(116_000 * 1.086), round(232_000 * 1.086)])

    def test_a_thick_after_cluster_states_the_tokens_itself(self):
        after = [classed_stretch(f"2026-09-{d:02d}T00:00:00+00:00", 500, 5_000, 43_000, 1_500)
                 for d in range(15, 20)]
        block = self.block(after=after, five_hour_pct=8.6)
        self.assertEqual((block["current_source"], block["all"]["value"]), ("after_cluster", 500_000))

    def test_the_week_and_every_family_follow_the_current_figure(self):
        block = self.block(five_hour_pct=8.6, windows_per_week=5.07)
        current = block["all"]["value"]
        self.assertEqual(block["per_week"]["all"]["value"], round(current * 5.07))
        self.assertEqual(block["per_family"]["opus"]["all"]["value"], current)


class CurrentWindowDownstreamTests(unittest.TestCase):
    """Everything the page divides by the window follows the current figure, with no
    second rule: the per-model rows, the session counts and the week."""

    #: Ten pure-Opus stretches before the cut at exactly 200,000 credits per 1%.
    BEFORE: ClassVar[list] = [opus_stretch(f"2026-09-{d:02d}T00:00:00+00:00", 200_000)
                              for d in range(4, 14)]
    #: Ten after it, each one mixed (a token of Sonnet in it), so they price into the
    #: across-the-cut comparison but never into the pure-Opus cluster. 220,000 credits
    #: per 1% is a +10% five-hour window across the cut.
    AFTER: ClassVar[list] = [
        stretch(f"2026-09-{d:02d}T{h:02d}:00:00+00:00",
                {"claude-opus-5": tok(input=round(220_000 * 10 / OPUS_IN)),
                 "claude-sonnet-5": tok(input=1)})
        for d in range(15, 20) for h in (5, 20)]

    def setUp(self):
        self.j = _published(gs=report("jwork", [*self.BEFORE, *self.AFTER]))
        self.credits = self.j["credits"]
        self.window = self.credits["window_credits"]

    def test_the_five_hour_change_comes_from_the_account_that_can_state_one(self):
        cut = self.credits["five_hour_window_across_cut"]["per_account"]["a2"]
        self.assertEqual((cut["n_before"], cut["n_after"], cut["change_pct"]), (10, 10, 10.0))
        self.assertEqual(self.window["five_hour_window_pct"], 10.0)

    def test_the_published_window_is_the_pre_cut_cluster_scaled_by_that_change(self):
        self.assertEqual((self.window["before"]["n"], self.window["after"]["n"]), (10, 0))
        self.assertEqual(self.window["current_source"], "before_cluster_scaled_by_five_hour_change")
        self.assertEqual(self.window["value"], 22_000_000)

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
        self.assertEqual(tokens["current_source"], "before_cluster_scaled_by_five_hour_change")
        self.assertEqual(tokens["all"]["value"], round(tokens["before"]["value"] * 1.10))


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
        for model in self.prices:
            if not model.startswith("_"):
                with self.subTest(model=model):
                    self.assertEqual(normalize_model(model), model)

    def test_the_older_models_land_in_the_right_family(self):
        for model, fam in (("claude-opus-5-5", "opus"), ("claude-opus-4-8", "opus"),
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
