"""tracker/list_prices.py: list-price rows for new models, from the pricing page (issue #130).

No network: the page is tests/fixtures/pricing_page.html, whose Claude Sonnet 5.5 row is
invented for these tests ($3 / $15 / $3.75 / $6 / $0.30, not Anthropic's price).
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from tracker import credits as credit_model
from tracker.gs_passive import passive_credit_points, unpriced_credit_models
from tracker.join import Stretch, bundle_meter_usd
from tracker.list_prices import (
    METER_WEIGHT_SOURCE,
    PRICING_URL,
    PriceTableError,
    add_rows,
    model_id,
    parse_price_table,
    run,
)
from tracker.turns import Turn

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "pricing_page.html"
PRICES = ROOT / "data" / "prices.json"


def prices_without_auto_rows() -> dict:
    """data/prices.json as it stood before tracker/list_prices.py filed any row.

    The daily publisher runs list_prices against the live page and commits what it
    adds (claude-sonnet-5-5 on 2026-09-29), so the shipped table is not a fixture:
    a test that copies it unfiltered stops seeing the "no row yet" case it tests.
    """
    raw = json.loads(PRICES.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not (isinstance(v, dict) and "list_price_source" in v)}
NOW = datetime(2026, 9, 30, 0, 5, tzinfo=timezone.utc)
SONNET_55 = "claude-sonnet-5-5"
TOKENS = {"input": 1_000, "output": 20_000, "cache_read": 3_000_000, "cache_write": 400_000}


def page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def stretch(models: dict[str, dict], status: str = "accepted", unpriced_tokens: int = 0) -> dict:
    return {"start": "2026-09-29T10:00:00+00:00", "end": "2026-09-29T11:00:00+00:00",
            "delta_pct": 6.0, "windows": 1, "status": status, "reset_verified": True,
            "unpriced_tokens": unpriced_tokens, "tokens": models}


def report(*stretches: dict) -> dict:
    return {"accounts": {"Max account 1": {"stretches": list(stretches)}}}


class ParseTests(unittest.TestCase):
    def test_reads_the_main_table_not_batch_or_fast_mode(self):
        rows = parse_price_table(page())
        # The invented main-table row, not the batch ($1.50) or fast-mode ($18) one.
        self.assertEqual(rows[SONNET_55], {"input": 3.0, "output": 15.0, "cache_write": 3.75,
                                           "cache_write_1h": 6.0, "cache_read": 0.3})
        self.assertEqual(rows["claude-opus-5-5"], {"input": 4.0, "output": 20.0, "cache_write": 5.0,
                                                   "cache_write_1h": 8.0, "cache_read": 0.2})
        self.assertEqual(rows["claude-haiku-3-5"]["cache_read"], 0.08)
        self.assertIn("claude-opus-4-1", rows)

    def test_column_order_is_read_from_the_header(self):
        html = ("<table><tr><th>Model</th><th>Output Tokens</th><th>Cache Hits &amp; Refreshes</th>"
                "<th>1h Cache Writes</th><th>Base Input Tokens</th><th>5m Cache Writes</th></tr>"
                "<tr><td>Claude Sonnet 5.5</td><td>$15 / MTok</td><td>$0.30 / MTok</td><td>$6 / MTok</td>"
                "<td>$3 / MTok</td><td>$3.75 / MTok</td></tr>"
                + "".join(f"<tr><td>Claude Opus 4.{v}</td>" + "<td>$5 / MTok</td>" * 5 + "</tr>" for v in range(1, 6))
                + "</table>")
        self.assertEqual(parse_price_table(html)[SONNET_55],
                         {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_write_1h": 6.0,
                          "cache_read": 0.3})

    def test_display_names_map_to_price_table_keys(self):
        self.assertEqual(model_id("Claude Sonnet 5.5"), "claude-sonnet-5-5")
        self.assertEqual(model_id("Claude Haiku 4.5"), "claude-haiku-4-5")
        self.assertEqual(model_id("Claude Opus 5"), "claude-opus-5")
        self.assertEqual(model_id("Claude Fable 5.1"), "claude-fable-5-1")
        self.assertIsNone(model_id("Claude Opus 5 / Claude Opus 4.8"))
        self.assertIsNone(model_id("Additional models"))

    def test_malformed_tables_are_refused(self):
        main = page().split("<h2>Model pricing</h2>")[1]
        for label, html in {
            "no main table": page().split("<h2>Model pricing</h2>")[0],
            "zero price": main.replace("$3.75 <span>", "$0 <span>"),
            "non-numeric price": main.replace("$3.75 <span>", "TBC <span>"),
            "missing price": main.replace("<td>$6 <span>/ MTok</span></td><td>$0.30", "<td>$0.30"),
        }.items():
            with self.subTest(label), self.assertRaises(PriceTableError):
                parse_price_table(html)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.prices = self.dir / "prices.json"
        self.prices.write_text(json.dumps(prices_without_auto_rows(), indent=2) + "\n", encoding="utf-8")
        self.before = self.prices.read_bytes()
        self.history = self.dir / "gs-passive.json"
        # As tracker.gs_passive writes it while the model has no row: kept by its raw id,
        # the stretch withheld as unpriced.
        self.history.write_text(json.dumps(report(
            stretch({"claude-sonnet-5": TOKENS}),
            stretch({"claude-opus-5-5": TOKENS, SONNET_55: TOKENS}, status="unpriced", unpriced_tokens=1)))
                                + "\n")

    def _run(self, fetcher=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            added = run(self.prices, [self.history, self.dir / "absent.json"],
                        fetcher=fetcher or (lambda _url: page()), now=NOW)
        return added, err.getvalue()

    def test_adds_a_row_only_for_a_model_in_use_without_one(self):
        added, err = self._run()
        self.assertEqual(added, [SONNET_55])
        self.assertEqual(err, "")
        after = json.loads(self.prices.read_text(encoding="utf-8"))
        before = json.loads(self.before)
        # Opus 4.1 and Mythos 5.1 are on the page but in no stretch: no row.
        self.assertEqual(set(after) - set(before), {SONNET_55})
        row = after[SONNET_55]
        self.assertEqual(list(row), ["input", "output", "cache_read", "cache_write", "cache_write_1h",
                                     "meter_weight", "meter_weight_source", "class_weight", "list_price_source"])
        self.assertEqual((row["input"], row["output"], row["cache_read"], row["cache_write"],
                          row["cache_write_1h"]), (3, 15, 0.3, 3.75, 6.0))
        self.assertEqual(row["meter_weight"], 1.0)
        self.assertEqual(row["meter_weight_source"], METER_WEIGHT_SOURCE)
        self.assertIn("assumed, not measured", row["meter_weight_source"])
        self.assertEqual(row["class_weight"], before["claude-opus-5-5"]["class_weight"])
        self.assertEqual(row["list_price_source"], {"url": PRICING_URL, "fetched": "2026-09-30"})
        # The new row sits with the model rows, ahead of the trailing publisher block.
        self.assertEqual(list(after)[-1], "_output_weight")

    def test_existing_rows_are_byte_identical(self):
        self._run()
        after = json.loads(self.prices.read_text(encoding="utf-8"))
        del after[SONNET_55]
        self.assertEqual(json.dumps(after, indent=2) + "\n", self.before.decode("utf-8"))

    def test_second_run_changes_nothing(self):
        self._run()
        once = self.prices.read_bytes()
        added, _ = self._run()
        self.assertEqual(added, [])
        self.assertEqual(self.prices.read_bytes(), once)

    def test_model_the_page_does_not_list_is_left_unpriced(self):
        html = page().replace("Claude Sonnet 5.5", "Claude Sonnet 9")
        added, err = self._run(lambda _url: html)
        self.assertEqual(added, [])
        self.assertEqual(err, "")
        self.assertEqual(self.prices.read_bytes(), self.before)

    def test_fetch_failure_is_a_no_op_with_one_warning(self):
        def fail(_url):
            raise urllib.error.URLError("timed out")
        added, err = self._run(fail)
        self.assertEqual(added, [])
        self.assertEqual(self.prices.read_bytes(), self.before)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("warning", err)

    def test_malformed_table_is_a_no_op_with_one_warning(self):
        for html in (page().replace("$3.75 <span>", "$0 <span>"),
                     page().replace("$6 <span>", "n/a <span>"),
                     "<html><body>maintenance</body></html>"):
            with self.subTest(html=html[-40:]):
                added, err = self._run(lambda _url, html=html: html)
                self.assertEqual(added, [])
                self.assertEqual(self.prices.read_bytes(), self.before)
                self.assertEqual(len(err.splitlines()), 1)

    def test_nothing_unpriced_means_no_fetch(self):
        self.history.write_text(json.dumps(report(stretch({"claude-sonnet-5": TOKENS}))))

        def never(_url):
            raise AssertionError("fetched with nothing to price")
        added, err = self._run(never)
        self.assertEqual((added, err), ([], ""))

    def test_add_rows_never_touches_an_existing_row(self):
        prices = json.loads(self.before)
        listed = parse_price_table(page())
        listed["claude-opus-5-5"] = {k: 99.0 for k in listed["claude-opus-5-5"]}
        out, added = add_rows(prices, listed, ["claude-opus-5-5"], PRICING_URL, "2026-09-30")
        self.assertEqual(added, [])
        self.assertEqual(out["claude-opus-5-5"], json.loads(self.before)["claude-opus-5-5"])


class NewRowPricesTheStretchTests(unittest.TestCase):
    """After the row is added, a Sonnet 5.5 stretch is valued and kept."""

    def setUp(self):
        self.prices = prices_without_auto_rows()
        self.credits = credit_model.load_credits(self.prices)
        self.listed = parse_price_table(page())

    def test_auto_family_resolves_to_the_new_row(self):
        fam = credit_model.family(SONNET_55, self.credits)
        assert fam is not None
        self.assertEqual(fam, "sonnet-5-5")
        self.assertEqual(credit_model.list_price_model(fam, self.credits), SONNET_55)
        self.assertIsNone(credit_model.list_price_ratio(fam, self.credits, self.prices))
        with_row, _ = add_rows(self.prices, self.listed, [SONNET_55], PRICING_URL, "2026-09-30")
        opus = self.prices[credit_model.list_price_model("opus", self.credits)]["input"]
        self.assertAlmostEqual(credit_model.list_price_ratio(fam, self.credits, with_row), 3.0 / opus)

    def test_stretch_is_dropped_without_the_row_and_kept_with_it(self):
        rpt = report(stretch({"claude-sonnet-5": TOKENS, SONNET_55: TOKENS}))
        self.assertEqual(passive_credit_points(rpt, self.prices, self.credits), [])
        self.assertEqual(unpriced_credit_models(rpt, self.prices, self.credits), [SONNET_55])
        with_row, _ = add_rows(self.prices, self.listed, [SONNET_55], PRICING_URL, "2026-09-30")
        self.assertIsNotNone(bundle_meter_usd(SONNET_55, TOKENS, with_row))
        self.assertIsNotNone(bundle_meter_usd(SONNET_55 + "-20261001", TOKENS, with_row))
        points = passive_credit_points(rpt, with_row, self.credits)
        self.assertEqual(len(points), 1)
        self.assertGreater(points[0][1], 0)
        self.assertEqual(unpriced_credit_models(rpt, with_row, self.credits), [])

    def test_stretch_builder_files_the_turns_as_priced(self):
        # tracker/gs_passive builds its stretches with tracker/join.py, which keyed only
        # the ids tracker/turns.py knew; a new row must be enough on its own.
        with_row, _ = add_rows(self.prices, self.listed, [SONNET_55], PRICING_URL, "2026-09-30")
        s = Stretch(start=NOW, end=NOW)
        s.add(Turn(NOW, SONNET_55 + "[1m]", 10, 20, 30, 40), with_row)
        self.assertEqual(s.unpriced_tokens, 0)
        self.assertEqual(list(s.tokens), [SONNET_55])
        self.assertGreater(s.usd, 0)
        unpriced = Stretch(start=NOW, end=NOW)
        unpriced.add(Turn(NOW, SONNET_55, 10, 20, 30, 40), self.prices)
        self.assertEqual(unpriced.tokens, {})
        self.assertEqual(list(unpriced.unpriced), [SONNET_55])


if __name__ == "__main__":
    unittest.main()
