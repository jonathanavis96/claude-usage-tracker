"""tools/credits_report.py: credits per 1%, per account, era and dominant model (issue #52).

Arithmetic only, so the tests are arithmetic: a planted token bundle whose credits can be
worked out by hand, and a planted Fable rate the solver has to give back exactly.
"""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tools.credits_report import (
    CLASSES,
    EFFORT_MATRIX_ACCOUNT,
    ERA_20X_AT,
    ERA_CUT_AT,
    PUBLISHED_RATES,
    HarnessRun,
    chargeable,
    era,
    family,
    harness_runs,
    load_rows,
    main,
    overlapping_run,
    pure_clusters,
    quartiles,
    rates,
    solve_fable_input,
)

SONNET_IN, SONNET_OUT = PUBLISHED_RATES["sonnet"]
OPUS_OUT = PUBLISHED_RATES["opus"][1]


def tok(input=0, output=0, cache_read=0, cache_write=0, **extra):
    return {"input": input, "output": output, "cache_read": cache_read, "cache_write": cache_write, **extra}


def stretch(end: str, tokens: dict, delta_pct: float = 10.0, **extra) -> dict:
    return {"start": end, "end": end, "delta_pct": delta_pct, "tokens": tokens,
            "status": "accepted", "capture": None, "reset_verified": True, **extra}


def report_file(root: Path, name: str, stretches: list[dict], account: str = "acct") -> Path:
    path = root / f"{name}.json"
    path.write_text(json.dumps({"accounts": {account: {"account": account, "stretches": stretches}}}))
    return path


def all_for(stretches: list[dict], account: str = "acct", **kw):
    """(rows, skipped, excluded) for a one-file report built from `stretches`."""
    with tempfile.TemporaryDirectory() as d:
        path = report_file(Path(d), "r", stretches, account)
        return load_rows({"one": path}, kw.pop("cache_read_weight", 0.0),
                         kw.pop("fable_input", 25 / 15), kw.pop("fable_output_ratio", 3), **kw)


def rows_for(stretches: list[dict], account: str = "acct", **kw):
    rows, skipped, _ = all_for(stretches, account, **kw)
    return rows, skipped


def _run_file(root: Path, rows: list[dict], name: str = "harness-runs.jsonl") -> Path:
    """A history/harness-runs.jsonl in the shape tools/harness_runs.py writes."""
    path = root / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def run(account: str, start: str, end: str, reason: str = "probe x/low") -> HarnessRun:
    return HarnessRun(account, datetime.fromisoformat(start), datetime.fromisoformat(end), reason)


class RateTests(unittest.TestCase):
    def test_every_version_of_a_published_model_takes_its_family_rate(self):
        for model, fam in (("claude-haiku-4-5", "haiku"), ("claude-sonnet-5", "sonnet"),
                           ("claude-sonnet-4-6", "sonnet"), ("claude-opus-5", "opus"),
                           ("claude-opus-4-7", "opus"), ("claude-opus-4-8", "opus"),
                           ("claude-fable-5-1", "fable")):
            with self.subTest(model=model):
                self.assertEqual(family(model), fam)
        self.assertIsNone(family("<synthetic>"))
        self.assertIsNone(family("gpt-4"))

    def test_the_published_table_is_the_articles_and_output_is_five_times_input(self):
        self.assertEqual(PUBLISHED_RATES, {"haiku": (2 / 15, 10 / 15), "sonnet": (6 / 15, 30 / 15),
                                           "opus": (10 / 15, 50 / 15)})
        for fam, (rate_in, rate_out) in PUBLISHED_RATES.items():
            with self.subTest(fam=fam):
                self.assertAlmostEqual(rate_out / rate_in, 5.0)

    def test_fable_takes_the_provisional_rate_and_its_own_output_ratio(self):
        self.assertEqual(rates("fable", 2.0, 3), (2.0, 6.0))
        self.assertEqual(rates("opus", 2.0, 3), PUBLISHED_RATES["opus"])


class ChargeableTests(unittest.TestCase):
    def test_cache_writes_are_charged_at_the_plain_input_rate(self):
        # The article's correction to the API's 1.25x premium: a cache write counts as input.
        self.assertEqual(chargeable(tok(input=10, cache_write=90), 0.0), (100, 0))

    def test_cache_reads_are_free_at_weight_zero_and_weighted_otherwise(self):
        self.assertEqual(chargeable(tok(cache_read=1_000_000), 0.0), (0, 0))
        self.assertEqual(chargeable(tok(cache_read=1_000_000), 0.015), (15_000, 0))

    def test_the_one_hour_cache_write_subset_is_not_charged_twice(self):
        # tracker/turns.py already counts cache_write_1h inside cache_write, and the
        # credits model has no one-hour premium to add on top.
        self.assertEqual(chargeable(tok(cache_write=100, cache_write_1h=100), 0.0), (100, 0))
        self.assertNotIn("cache_write_1h", CLASSES)


class EraTests(unittest.TestCase):
    def test_the_boundaries_are_the_plans_and_the_instant_itself_belongs_to_the_later_era(self):
        self.assertEqual(era(ERA_20X_AT.replace(hour=11, minute=59)), "5x")
        self.assertEqual(era(ERA_20X_AT), "20x")
        self.assertEqual(era(ERA_CUT_AT.replace(hour=11, minute=59)), "20x")
        self.assertEqual(era(ERA_CUT_AT), "20x-cut")

    def test_a_stretch_is_placed_by_its_end_in_utc_whatever_offset_it_was_written_in(self):
        # masterrig writes +02:00; 13:30+02:00 is 11:30Z, before the 12:00Z seam.
        rows, _ = rows_for([stretch("2026-08-14T13:30:00+02:00", {"claude-sonnet-5": tok(output=1000)}),
                            stretch("2026-08-14T15:30:00+02:00", {"claude-sonnet-5": tok(output=1000)})])
        self.assertEqual([r["era"] for r in rows], ["5x", "20x"])


class CreditTests(unittest.TestCase):
    def test_credits_per_pct_is_the_bundle_priced_by_hand(self):
        rows, _ = rows_for([stretch("2026-09-01T00:00:00+00:00",
                                    {"claude-sonnet-5": tok(input=1000, output=2000, cache_write=3000,
                                                            cache_read=1_000_000)}, delta_pct=20.0)],
                           cache_read_weight=0.0)
        expected = (1000 + 3000) * SONNET_IN + 2000 * SONNET_OUT
        self.assertAlmostEqual(rows[0]["credits"], expected)
        self.assertAlmostEqual(rows[0]["credits_per_pct"], expected / 20.0)

    def test_the_cache_read_weight_adds_exactly_its_share_of_the_input_rate(self):
        bundle = [stretch("2026-09-01T00:00:00+00:00",
                          {"claude-sonnet-5": tok(output=1000, cache_read=1_000_000)})]
        free, _ = rows_for(bundle, cache_read_weight=0.0)
        weighted, _ = rows_for(bundle, cache_read_weight=0.015)
        self.assertAlmostEqual(free[0]["credits"], 1000 * SONNET_OUT)
        self.assertAlmostEqual(weighted[0]["credits"] - free[0]["credits"], 15_000 * SONNET_IN)

    def test_a_stretch_the_rates_value_at_nothing_is_counted_and_left_out(self):
        # Only at --cache-read-weight 0, and only for a stretch of nothing but cache
        # reads: a flat zero is not a level to take a median with. None in the real data.
        bundle = [stretch("2026-09-01T00:00:00+00:00", {"claude-sonnet-5": tok(cache_read=1_000_000)})]
        rows, skipped = rows_for(bundle, cache_read_weight=0.0)
        self.assertEqual((rows, skipped["no_credits"], skipped["no_tokens"]), ([], 1, 0))
        rows, skipped = rows_for(bundle, cache_read_weight=0.015)
        self.assertEqual((len(rows), skipped["no_credits"]), (1, 0))

    def test_an_older_opus_is_priced_as_opus_not_left_out(self):
        rows, skipped = rows_for([stretch("2026-09-01T00:00:00+00:00", {"claude-opus-4-7": tok(output=1000)})])
        self.assertAlmostEqual(rows[0]["credits"], 1000 * OPUS_OUT)
        self.assertEqual(skipped["unknown_model"], 0)


class GroupingTests(unittest.TestCase):
    def test_over_ninety_percent_of_the_credits_names_the_stretch_else_mixed(self):
        # Opus output at 50/15 against Sonnet output at 30/15: 1000 Opus is 50/15 x 1000
        # credits, and it takes only a little Sonnet to drop it under 90%.
        rows, _ = rows_for([
            stretch("2026-09-01T00:00:00+00:00", {"claude-opus-5": tok(output=10_000),
                                                  "claude-sonnet-5": tok(output=100)}),
            stretch("2026-09-01T01:00:00+00:00", {"claude-opus-5": tok(output=10_000),
                                                  "claude-sonnet-5": tok(output=10_000)}),
        ])
        self.assertEqual([r["dominant"] for r in rows], ["opus-5", "mixed"])

    def test_a_stretch_dominant_by_tokens_can_still_be_mixed_by_credits(self):
        # 2M free Opus cache reads and a little Sonnet output: nearly all the tokens are
        # Opus's, none of the credits are. The grouping is on credits, as the task asks.
        rows, _ = rows_for([stretch("2026-09-01T00:00:00+00:00",
                                    {"claude-opus-5": tok(cache_read=2_000_000),
                                     "claude-sonnet-5": tok(output=1000)})], cache_read_weight=0.0)
        self.assertEqual(rows[0]["dominant"], "sonnet-5")

    def test_a_pure_cluster_is_one_family_and_nothing_else(self):
        rows, _ = rows_for([
            stretch("2026-09-01T00:00:00+00:00", {"claude-opus-5": tok(output=1000)}),
            # Two versions of one family are still pure.
            stretch("2026-09-01T01:00:00+00:00", {"claude-opus-5": tok(output=1000),
                                                  "claude-opus-4-8": tok(output=1000)}),
            stretch("2026-09-01T02:00:00+00:00", {"claude-sonnet-5": tok(output=1000)}),
            stretch("2026-09-01T03:00:00+00:00", {"claude-opus-5": tok(output=1000),
                                                  "claude-sonnet-5": tok(output=1000)}),
        ])
        self.assertEqual([r["pure"] for r in rows], ["opus", "opus", "sonnet", None])
        self.assertEqual(pure_clusters(rows, "opus")["acct"]["n"], 2)
        self.assertEqual(pure_clusters(rows, "sonnet")["acct"]["n"], 1)


class SkipTests(unittest.TestCase):
    def test_what_cannot_be_priced_is_counted_and_the_unknown_models_named(self):
        rows, skipped = rows_for([
            stretch("2026-09-01T00:00:00+00:00", {}),
            stretch("2026-09-01T01:00:00+00:00", {"claude-sonnet-5": tok()}),
            stretch("2026-09-01T02:00:00+00:00", {"claude-sonnet-5": tok(output=1)}, delta_pct=0.0),
            stretch("2026-09-01T03:00:00+00:00", {"mystery-9": tok(output=1000)}),
            stretch("2026-09-01T04:00:00+00:00", {"claude-sonnet-5": tok(output=1000)}),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped["no_tokens"], 2)
        self.assertEqual(skipped["no_movement"], 1)
        self.assertEqual((skipped["unknown_model"], skipped["unknown_models"]), (1, ["mystery-9"]))

    def test_a_zero_token_entry_beside_a_real_one_is_ignored_not_called_unknown(self):
        # `<synthetic>` appears in the real reports with every class at zero.
        rows, skipped = rows_for([stretch("2026-09-01T00:00:00+00:00",
                                          {"<synthetic>": tok(), "claude-sonnet-5": tok(output=1000)})])
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped["unknown_model"], 0)
        self.assertEqual(list(rows[0]["tokens"]), ["claude-sonnet-5"])

    def test_min_capture_keeps_only_stretches_that_have_one_and_meet_it(self):
        made = [stretch("2026-09-01T00:00:00+00:00", {"claude-sonnet-5": tok(output=1000)}, capture=0.9),
                stretch("2026-09-01T01:00:00+00:00", {"claude-sonnet-5": tok(output=1000)}, capture=0.2),
                stretch("2026-09-01T02:00:00+00:00", {"claude-sonnet-5": tok(output=1000)}, capture=None)]
        rows, skipped = rows_for(made, min_capture=0.5)
        self.assertEqual([r["capture"] for r in rows], [0.9])
        self.assertEqual(skipped["below_min_capture"], 2)


class QuartileTests(unittest.TestCase):
    def test_linear_interpolation_on_the_sorted_sample(self):
        self.assertEqual(quartiles([1, 2, 3, 4, 5]), (2.0, 3, 4.0))
        self.assertEqual(quartiles([1, 2, 3, 4]), (1.75, 2.5, 3.25))
        self.assertEqual(quartiles([7]), (7, 7, 7))
        self.assertEqual(quartiles([2, 1]), (1.25, 1.5, 1.75))


class FableSolveTests(unittest.TestCase):
    """The one derived number here: the Fable input rate one stretch implies.

    Built backwards. Pick W (credits a percent buys) and a true Fable rate, price a bundle
    at both, set delta_pct so the stretch's credits are exactly delta_pct x W, and the
    solver must hand the planted rate back.
    """

    def _rows(self, fable_rate: float, ratio: float = 3.0, known_sonnet_out: int = 0):
        w = 200_000.0
        fable_in, fable_out = 3_000_000, 200_000
        known = known_sonnet_out * SONNET_OUT
        total = known + fable_rate * (fable_in + ratio * fable_out)
        delta = total / w
        pure_opus_out = w * 10 / OPUS_OUT  # a 10% stretch of nothing but Opus output at W
        made = [
            stretch("2026-09-01T00:00:00+00:00", {"claude-opus-5": tok(output=round(pure_opus_out))}),
            stretch("2026-09-01T01:00:00+00:00",
                    {"claude-fable-5-1": tok(input=fable_in, output=fable_out),
                     **({"claude-sonnet-5": tok(output=known_sonnet_out)} if known_sonnet_out else {})},
                    delta_pct=delta),
        ]
        rows, _ = rows_for(made, cache_read_weight=0.0, fable_output_ratio=ratio)
        return rows, w

    def test_the_planted_rate_comes_back(self):
        rows, _ = self._rows(25 / 15)
        solved = solve_fable_input(rows, pure_clusters(rows, "opus"), 3.0, 0.0)
        self.assertEqual(len(solved), 1)
        self.assertAlmostEqual(solved[0]["solved_fable_input"], 25 / 15, places=6)

    def test_credits_of_the_other_models_are_subtracted_before_solving(self):
        rows, _ = self._rows(2.0, known_sonnet_out=500_000)
        solved = solve_fable_input(rows, pure_clusters(rows, "opus"), 3.0, 0.0)
        self.assertAlmostEqual(solved[0]["solved_fable_input"], 2.0, places=6)

    def test_the_output_ratio_used_to_solve_is_the_one_given(self):
        rows, _ = self._rows(2.0, ratio=5.0)
        self.assertAlmostEqual(solve_fable_input(rows, pure_clusters(rows, "opus"), 5.0, 0.0)[0]
                               ["solved_fable_input"], 2.0, places=6)
        # Solved with the wrong ratio, the rate is wrong: the assumption is not hidden.
        self.assertNotAlmostEqual(solve_fable_input(rows, pure_clusters(rows, "opus"), 3.0, 0.0)[0]
                                  ["solved_fable_input"], 2.0, places=3)

    def test_a_stretch_that_is_not_fable_heavy_is_not_solved(self):
        rows, _ = rows_for([
            stretch("2026-09-01T00:00:00+00:00", {"claude-opus-5": tok(output=60_000)}),
            stretch("2026-09-01T01:00:00+00:00", {"claude-fable-5-1": tok(output=1000),
                                                  "claude-sonnet-5": tok(output=1_000_000)}),
        ])
        self.assertLess(rows[1]["fable_token_share"], 0.5)
        self.assertEqual(solve_fable_input(rows, pure_clusters(rows, "opus"), 3.0, 0.0), [])

    def test_without_a_pure_opus_cluster_the_account_is_not_solved(self):
        rows, _ = rows_for([stretch("2026-09-01T00:00:00+00:00", {"claude-fable-5-1": tok(output=1000)})])
        self.assertEqual(solve_fable_input(rows, {}, 3.0, 0.0), [])


class CliTests(unittest.TestCase):
    def _files(self, d: Path) -> list[str]:
        gs = report_file(d, "gs", [
            stretch("2026-09-16T00:00:00+00:00", {"claude-opus-5": tok(output=100_000)}),
            stretch("2026-09-16T01:00:00+00:00", {"claude-fable-5-1": tok(input=1_000_000, output=50_000)}),
        ], account="jwork")
        mr = report_file(d, "mr", [
            stretch("2026-09-01T00:00:00+00:00", {"claude-sonnet-5": tok(output=100_000)}),
        ], account="masterrig")
        return ["--gs", str(gs), "--masterrig", str(mr)]

    def test_it_prints_a_table_citing_the_reference_and_marking_fable_provisional(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(self._files(Path(d)) + ["--cache-read-weight", "0"])
        text = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("docs/reference-2026-09-20-shellac-credits-model.md", text)
        self.assertIn("a reference, not a source of truth", text)
        self.assertIn("PROVISIONAL", text)
        self.assertIn("jwork", text)
        self.assertIn("masterrig", text)
        # masterrig's own caveat is printed whenever a masterrig row is in the table.
        self.assertIn("web, phone and other machines", text)

    def test_json_dumps_the_per_stretch_rows_with_the_rates_they_were_priced_at(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "sub" / "rows.json"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main(self._files(Path(d)) + ["--json", str(out), "--cache-read-weight", "0.02",
                                                  "--fable-input", "2.0", "--fable-output-ratio", "4"])
            written = json.loads(out.read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(len(written["rows"]), 3)
        self.assertEqual(written["rates"]["cache_read_weight"], 0.02)
        self.assertEqual((written["rates"]["fable_input"], written["rates"]["fable_output_ratio"]), (2.0, 4))
        self.assertEqual(sorted(written["rates"]["provisional"]),
                         ["cache_read_weight", "fable_input", "fable_output_ratio"])
        self.assertEqual(sorted(written["eras"]), ["20x", "20x-cut", "5x"])
        self.assertIn("credits_per_pct", written["rows"][0])

    def test_missing_input_files_are_reported_not_crashed_on(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--gs", str(Path(d) / "none.json"), "--masterrig", str(Path(d) / "also-none.json")])
        self.assertEqual(rc, 1)
        self.assertIn("no priceable stretches", buf.getvalue())

    def test_the_real_reports_price_every_stretch_that_has_tokens(self):
        """Against the committed history/: no unknown model, and all three accounts present."""
        root = Path(__file__).resolve().parent.parent
        gs, mr = root / "history" / "gs-passive.json", root / "history" / "masterrig-passive.json"
        if not (gs.exists() and mr.exists()):
            self.skipTest("no committed history/*-passive.json")
        rows, skipped, _ = load_rows({"gs": gs, "masterrig": mr}, 0.015, 25 / 15, 3)
        self.assertEqual(skipped["unknown_models"], [])
        self.assertEqual({r["account"] for r in rows}, {"jwork", "dave", "masterrig"})
        self.assertTrue(all(r["credits_per_pct"] > 0 for r in rows))
        self.assertEqual(sum(1 for r in rows if r["era"] == "5x"), 0)  # transcripts that old are gone


if __name__ == "__main__":
    unittest.main()


class HarnessRunTests(unittest.TestCase):
    """A stretch cut by the tracker's own instrument is not a reading of ordinary use.

    The 2026-09-20 review corrected the cause of the 9 September jwork contamination: it was
    not a probe (history/probes.jsonl holds no jwork row that day) but the effort-matrix run
    in data/effort_matrix.json. So the exclusion is keyed to the runs themselves, never to a
    date.
    """

    def test_each_row_of_the_run_file_spans_its_own_account(self):
        with tempfile.TemporaryDirectory() as d:
            runs = harness_runs(_run_file(Path(d), [
                {"account": "jwork", "start": "2026-09-08T11:36:02+00:00",
                 "end": "2026-09-08T11:46:02+00:00", "kind": "probe", "outcome": "completed",
                 "model": "claude-sonnet-5", "effort": "low"},
                {"account": "dave", "start": "2026-09-09T00:02:08+00:00",
                 "end": "2026-09-09T00:03:08+00:00", "kind": "probe", "outcome": "completed",
                 "model": "claude-fable-5-1", "effort": "low"},
                {"start": "2026-09-09T05:00:00+00:00", "end": "2026-09-09T05:01:00+00:00"},
            ]))
        self.assertEqual([(r.account, r.start.isoformat(), r.end.isoformat()) for r in runs],
                         [("dave", "2026-09-09T00:02:08+00:00", "2026-09-09T00:03:08+00:00"),
                          ("jwork", "2026-09-08T11:36:02+00:00", "2026-09-08T11:46:02+00:00")])
        self.assertIn("claude-sonnet-5/low", runs[1].reason)

    def test_a_probe_that_aborted_is_a_run_like_any_other(self):
        # It sent its prompts and moved the meter; what it never did is write a
        # history/probes.jsonl row, which is why that file is no longer the source.
        with tempfile.TemporaryDirectory() as d:
            runs = harness_runs(_run_file(Path(d), [
                {"account": "jwork", "start": "2026-09-15T13:16:32+00:00",
                 "end": "2026-09-15T15:27:25+00:00", "kind": "probe", "outcome": "aborted",
                 "detail": "tick too early"},
            ]))
        self.assertEqual(len(runs), 1)
        self.assertIn("aborted", runs[0].reason)
        self.assertIn("tick too early", runs[0].reason)

    def test_the_effort_matrix_row_carries_the_account_it_ran_on(self):
        with tempfile.TemporaryDirectory() as d:
            runs = harness_runs(_run_file(Path(d), [
                {"account": EFFORT_MATRIX_ACCOUNT, "start": "2026-09-09T11:28:37.136014+00:00",
                 "end": "2026-09-09T14:53:22.399540+00:00", "kind": "effort-matrix",
                 "outcome": "completed"},
            ]))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].account, EFFORT_MATRIX_ACCOUNT)
        self.assertEqual((runs[0].start.isoformat(), runs[0].end.isoformat()),
                         ("2026-09-09T11:28:37.136014+00:00", "2026-09-09T14:53:22.399540+00:00"))
        self.assertIn("effort-matrix", runs[0].reason)

    def test_a_missing_or_incomplete_run_file_gives_no_runs_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(harness_runs(Path(d) / "none.jsonl"), [])
            self.assertEqual(harness_runs(_run_file(Path(d), [
                {"account": "jwork", "start": "2026-09-09T11:28:37+00:00"},  # no end
            ])), [])

    def test_overlap_is_half_open_so_touching_a_boundary_is_not_overlapping(self):
        r = [run("jwork", "2026-09-09T11:28:37+00:00", "2026-09-09T14:53:22+00:00")]
        inside = ("2026-09-09T12:20:15+00:00", "2026-09-09T12:52:14+00:00")
        self.assertIsNotNone(overlapping_run(r, "jwork", *map(datetime.fromisoformat, inside)))
        # Straddling either end still overlaps.
        for span in (("2026-09-09T10:47:14+00:00", "2026-09-09T11:58:14+00:00"),
                     ("2026-09-09T14:36:44+00:00", "2026-09-09T14:58:44+00:00"),
                     ("2026-09-09T09:00:00+00:00", "2026-09-09T20:00:00+00:00")):
            with self.subTest(span=span):
                self.assertIsNotNone(overlapping_run(r, "jwork", *map(datetime.fromisoformat, span)))
        # Meeting at an instant, and lying wholly outside, do not.
        for span in (("2026-09-09T10:00:00+00:00", "2026-09-09T11:28:37+00:00"),
                     ("2026-09-09T14:53:22+00:00", "2026-09-09T16:00:00+00:00"),
                     ("2026-09-08T10:00:00+00:00", "2026-09-08T11:00:00+00:00")):
            with self.subTest(span=span):
                self.assertIsNone(overlapping_run(r, "jwork", *map(datetime.fromisoformat, span)))

    def test_a_run_only_excludes_its_own_account(self):
        r = [run("jwork", "2026-09-09T11:28:37+00:00", "2026-09-09T14:53:22+00:00")]
        span = tuple(map(datetime.fromisoformat, ("2026-09-09T12:00:00+00:00", "2026-09-09T13:00:00+00:00")))
        self.assertIsNotNone(overlapping_run(r, "jwork", *span))
        self.assertIsNone(overlapping_run(r, "dave", *span))
        self.assertIsNone(overlapping_run(r, "masterrig", *span))

    def _three(self):
        return [
            {"start": "2026-09-09T09:00:00+00:00", "end": "2026-09-09T10:00:00+00:00", "delta_pct": 10.0,
             "tokens": {"claude-opus-5": tok(output=50_000)}, "status": "accepted", "capture": 1.0},
            {"start": "2026-09-09T12:20:15+00:00", "end": "2026-09-09T12:52:14+00:00", "delta_pct": 11.0,
             "tokens": {"claude-opus-5": tok(output=5_000)}, "status": "accepted", "capture": 0.53},
            {"start": "2026-09-09T16:00:00+00:00", "end": "2026-09-09T17:00:00+00:00", "delta_pct": 10.0,
             "tokens": {"claude-opus-5": tok(output=50_000)}, "status": "accepted", "capture": 1.0},
        ]

    def test_the_contaminated_stretch_is_excluded_and_its_neighbours_kept(self):
        runs = [run("acct", "2026-09-09T11:28:37+00:00", "2026-09-09T14:53:22+00:00", "effort matrix")]
        rows, skipped, excluded = all_for(self._three(), runs=runs)
        self.assertEqual([r["start"][11:19] for r in rows], ["09:00:00", "16:00:00"])
        self.assertEqual(skipped["harness_run"], 1)
        self.assertEqual([(e["start"][11:19], e["capture"], e["reason"]) for e in excluded],
                         [("12:20:15", 0.53, "effort matrix")])

    def test_the_rest_of_the_day_survives_because_the_rule_is_not_a_date(self):
        # The exclusion must not reach 09:00 or 16:00 on the same date.
        runs = [run("acct", "2026-09-09T11:28:37+00:00", "2026-09-09T14:53:22+00:00")]
        rows, _, _ = all_for(self._three(), runs=runs)
        self.assertTrue(all(r["end"][:10] == "2026-09-09" for r in rows))
        self.assertEqual(len(rows), 2)

    def test_with_no_runs_nothing_is_excluded(self):
        rows, skipped, excluded = all_for(self._three())
        self.assertEqual((len(rows), skipped["harness_run"], excluded), (3, 0, []))

    def test_a_stretch_in_a_run_is_excluded_even_when_it_could_not_be_priced(self):
        # Provenance first: it is reported as a harness run, not as an unknown model.
        runs = [run("acct", "2026-09-09T11:00:00+00:00", "2026-09-09T15:00:00+00:00")]
        made = [{"start": "2026-09-09T12:00:00+00:00", "end": "2026-09-09T13:00:00+00:00",
                 "delta_pct": 10.0, "tokens": {"mystery-9": tok(output=1000)}, "capture": None,
                 "status": "accepted"}]
        rows, skipped, excluded = all_for(made, runs=runs)
        self.assertEqual((rows, skipped["harness_run"], skipped["unknown_model"]), ([], 1, 0))
        self.assertEqual(len(excluded), 1)

    def test_the_cli_prints_every_exclusion_with_its_reason_and_keep_turns_it_off(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            report_file(root, "gs", self._three(), account="jwork")
            runs = _run_file(root, [
                {"account": "dave", "kind": "probe", "outcome": "completed",
                 "model": "claude-fable-5-1", "effort": "low",
                 "start": "2026-09-09T00:02:08+00:00", "end": "2026-09-09T00:03:08+00:00"},
                {"account": EFFORT_MATRIX_ACCOUNT, "kind": "effort-matrix", "outcome": "completed",
                 "start": "2026-09-09T11:28:37+00:00", "end": "2026-09-09T14:53:22+00:00"}])
            argv = ["--gs", str(root / "gs.json"), "--masterrig", str(root / "none.json"),
                    "--harness-runs", str(runs), "--cache-read-weight", "0"]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main(argv), 0)
            excluded_run = buf.getvalue()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main([*argv, "--keep-harness-runs"]), 0)
            kept_run = buf.getvalue()
        self.assertIn("harness_run 1", excluded_run)
        self.assertIn("2026-09-09T12:20:15", excluded_run)
        self.assertIn("effort-matrix completed from 2026-09-09T11:28:37Z", excluded_run)
        # The Dave probe is not jwork's, so it excludes nothing here.
        self.assertNotIn("claude-fable-5-1/low", excluded_run)
        self.assertIn("(none)", kept_run)
        self.assertNotIn("harness_run", kept_run.split("Excluded:")[0])

    def test_the_json_dump_carries_the_runs_and_the_exclusions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            report_file(root, "gs", self._three(), account="jwork")
            matrix = root / "effort_matrix.json"
            matrix.write_text(json.dumps({"_meta": {"started": "2026-09-09T11:28:37+00:00",
                                                    "finished": "2026-09-09T14:53:22+00:00"}}))
            out = root / "rows.json"
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--gs", str(root / "gs.json"), "--masterrig", str(root / "none.json"),
                      "--harness-runs", str(_run_file(root, [
                          {"account": EFFORT_MATRIX_ACCOUNT, "kind": "effort-matrix",
                           "outcome": "completed", "start": "2026-09-09T11:28:37+00:00",
                           "end": "2026-09-09T14:53:22+00:00"}])),
                      "--json", str(out)])
            written = json.loads(out.read_text())
        self.assertEqual([r["account"] for r in written["harness_runs"]], [EFFORT_MATRIX_ACCOUNT])
        self.assertEqual(len(written["excluded"]), 1)
        self.assertEqual(written["skipped"]["harness_run"], 1)

    def test_the_real_data_excludes_the_nine_september_jwork_window_and_nothing_of_daves(self):
        """Against the committed history/ and data/: the review's finding, reproduced."""
        root = Path(__file__).resolve().parent.parent
        gs = root / "history" / "gs-passive.json"
        if not gs.exists():
            self.skipTest("no committed history/gs-passive.json")
        runs = harness_runs(root / "history" / "harness-runs.jsonl")
        # No jwork probe ran on 9 September: every probe run that day is Dave's, the two
        # that finished and the one that aborted.
        nine_september = [r for r in runs if r.start.date().isoformat() == "2026-09-09"
                          and "probe" in r.reason]
        self.assertEqual({r.account for r in nine_september}, {"dave"})
        self.assertGreaterEqual(len(nine_september), 2)
        _, skipped, excluded = load_rows({"gs": gs}, 0.0, 25 / 15, 3, runs=runs)
        self.assertEqual(skipped["harness_run"], len(excluded))
        matrix = [e for e in excluded if "effort-matrix" in e["reason"]]
        self.assertEqual([e["account"] for e in matrix], ["jwork"] * len(matrix))
        # The six stretches the review named, plus the two that straddle the window's ends.
        self.assertEqual([e["start"][11:19] for e in matrix],
                         ["10:47:14", "11:58:14", "12:20:15", "12:52:14",
                          "13:30:44", "13:47:14", "14:09:14", "14:36:44"])
        # Every one of them reads well under the ~1.0 capture either side.
        self.assertTrue(all(e["capture"] < 0.7 for e in matrix))
        self.assertEqual([e["account"] for e in excluded if e["account"] == "dave"], [])
