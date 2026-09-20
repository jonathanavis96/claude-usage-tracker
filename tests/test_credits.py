"""tracker/credits.py and the publisher's credits block (issue #53).

Arithmetic, so the tests are arithmetic: a planted token bundle whose credits can be
worked out by hand, a window the fixture makes exactly, and a Fable interval that has
to come out the other end of every derivation as an interval with no value in it.

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
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from tools.credits_report import (
    compare_published,
    publish_check,
    recompute_credits_block,
)
from tracker import credits as C
from tracker.publish import ACCOUNT_LABELS, build_public_json

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
        self.assertEqual(sorted(window["accounts"]), ["a1", "a2", "a3"])
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
        for name in ("jwork", "dave", "masterrig"):
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
        self.assertEqual(sorted(out["per_account"]), ["a1", "a2", "a3"])
        for name in ("jwork", "dave", "masterrig"):
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

    def test_tokens_per_window_carries_the_solved_interval_with_no_value(self):
        row = self.credits["per_model"]["fable"]
        self.assertIsNone(row["credits_per_token"]["input"])
        self.assertEqual(row["credits_per_token_interval"]["input"], [1.0, 2.5])
        self.assertEqual(row["credits_per_token_interval"]["output"], [3.0, 12.5])
        figure = row["tokens_per_window"]["input"]
        self.assertIsNone(figure["value"])
        self.assertEqual(figure["status"], "rate not yet identified")
        # Cheapest rate against the top of the window's range, dearest against the bottom.
        self.assertEqual(figure["interval"], [round(20_000_000 / 2.5), round(20_000_000 / 1.0)])

    def test_the_api_value_and_the_session_count_are_intervals_too(self):
        self.assertIsNone(self.credits["per_model"]["fable"]["api_value_per_window_usd"]["input"]["value"])
        sessions = self.credits["sessions"].get("claude-fable-5-1")
        if sessions is not None:
            self.assertIsNone(sessions["per_window"]["value"])


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

    def test_the_attribution_is_unresolved_when_the_accounts_spread_wider_than_they_moved(self):
        """Two accounts on the same plan 25% apart after a change one moved 10% across."""
        rows = {
            "jwork": [stretch("2026-09-10T00:00:00+00:00", {"claude-opus-5": tok(input=3_000_000)}),
                      stretch("2026-09-16T00:00:00+00:00", {"claude-opus-5": tok(input=3_300_000)})],
            "dave": [stretch("2026-09-16T00:00:00+00:00", {"claude-opus-5": tok(input=2_640_000)})],
        }
        out = C.across_cut(rows, CREDITS, LABELS)
        self.assertFalse(out["resolved"])
        self.assertEqual(out["largest_move_pct"], 10.0)
        self.assertEqual(out["spread_after_pct"], 25.0)
        self.assertIn("cannot be separated from these stretches", out["unresolved"])

    def test_it_never_says_the_window_did_not_move(self):
        text = json.dumps(self.block())
        for claim in ("did not move", "flat", "unchanged"):
            self.assertNotIn(claim, text)

    def test_one_account_alone_cannot_resolve_it_either(self):
        out = self.block()
        self.assertFalse(out["resolved"])
        self.assertIn("not enough accounts", out["unresolved"])

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

    def test_a_family_with_credits_but_no_dollar_row_publishes_null_with_a_status(self):
        haiku = self.credits["per_model"]["haiku"]
        self.assertIsNotNone(haiku["tokens_per_window"]["input"]["value"])
        self.assertIsNone(haiku["api_value_per_window_usd"]["input"]["value"])
        self.assertIn("no row in the dollar table", haiku["api_value_per_window_usd"]["input"]["status"])

    def test_with_no_fable_heavy_stretch_the_interval_is_null_and_says_why(self):
        """The fixture is pure Opus, so there is nothing to solve a Fable rate from."""
        fable = self.credits["fable_interval"]
        self.assertIsNone(fable["input_low"])
        self.assertIsNone(fable["input_high"])
        self.assertEqual(fable["unresolved"], "no Fable-heavy stretch to solve a rate from")
        row = self.credits["per_model"]["fable"]
        self.assertIsNone(row["tokens_per_window"]["input"]["value"])
        self.assertIsNone(row["tokens_per_window"]["input"]["interval"])
        self.assertEqual(row["status"], "no Fable-heavy stretch to solve a rate from")

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
        for name in ("jwork", "dave", "masterrig"):
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


def _files(root: Path, gs: dict, masterrig: dict, passive: dict, matrix: dict) -> dict:
    (root / "history").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "history/gs-passive.json").write_text(json.dumps(gs))
    (root / "history/masterrig-passive.json").write_text(json.dumps(masterrig))
    (root / "history/passive.json").write_text(json.dumps(passive))
    (root / "history/probes.jsonl").write_text("")
    (root / "data/effort_matrix.json").write_text(json.dumps(matrix))
    (root / "data/prices.json").write_text(json.dumps({**PRICES, "_credits": CREDITS}))
    return {"gs": root / "history/gs-passive.json",
            "masterrig": root / "history/masterrig-passive.json",
            "probes": root / "history/probes.jsonl",
            "effort_matrix": root / "data/effort_matrix.json",
            "passive": root / "history/passive.json",
            "prices": root / "data/prices.json"}


class PublishCheckTests(unittest.TestCase):
    """--publish-check: the published block against the same block rebuilt from the files."""

    GS: ClassVar[dict] = report("jwork", [opus_stretch("2026-09-05T00:00:00+00:00", 200_000),
                          opus_stretch("2026-09-06T00:00:00+00:00", 210_000)])
    PASSIVE: ClassVar[dict] = {"split": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0},
               "session_tokens": {"claude-opus-5": 1_000_000}}

    def published(self) -> dict:
        return _published(gs=self.GS, passive=self.PASSIVE)

    def check(self, published: dict, tolerance: float = 0.005):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            paths = _files(root, self.GS, {}, self.PASSIVE, {})
            out = root / "claude-usage.json"
            out.write_text(json.dumps(published))
            args = argparse.Namespace(**paths, publish_check=out, tolerance=tolerance)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = publish_check(args)
            return code, buf.getvalue()

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
        self.assertEqual(sorted(priced), ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"])


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


if __name__ == "__main__":
    unittest.main()
