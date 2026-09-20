"""tools/credits_report.py: credits per 1%, per account, era and dominant model (issue #52).

Arithmetic only, so the tests are arithmetic: a planted token bundle whose credits can be
worked out by hand, and a planted Fable rate the solver has to give back exactly.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.credits_report import (
    CLASSES,
    ERA_20X_AT,
    ERA_CUT_AT,
    PUBLISHED_RATES,
    chargeable,
    era,
    family,
    load_rows,
    main,
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


def rows_for(stretches: list[dict], account: str = "acct", **kw):
    with tempfile.TemporaryDirectory() as d:
        path = report_file(Path(d), "r", stretches, account)
        return load_rows({"one": path}, kw.pop("cache_read_weight", 0.0),
                         kw.pop("fable_input", 25 / 15), kw.pop("fable_output_ratio", 3), **kw)


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
        rows, skipped = load_rows({"gs": gs, "masterrig": mr}, 0.015, 25 / 15, 3)
        self.assertEqual(skipped["unknown_models"], [])
        self.assertEqual({r["account"] for r in rows}, {"jwork", "dave", "masterrig"})
        self.assertTrue(all(r["credits_per_pct"] > 0 for r in rows))
        self.assertEqual(sum(1 for r in rows if r["era"] == "5x"), 0)  # transcripts that old are gone


if __name__ == "__main__":
    unittest.main()
