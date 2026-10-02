"""Sanity of the price table the publisher reads and tracker/list_prices.py writes to.

The publish gate (tracker/publish_gate.py) runs this on gs before every publish, so a
broken row filed automatically is caught the same day. CUT_PRICES_FILE points the
checks at another file (the gate's own test uses it to prove a bad row fails).
"""
import json
import os
import unittest
from pathlib import Path

PRICES = Path(os.environ.get("CUT_PRICES_FILE") or Path(__file__).resolve().parent.parent / "data" / "prices.json")
CLASSES = ("input", "output", "cache_read", "cache_write")


def model_rows() -> dict:
    table = json.loads(PRICES.read_text(encoding="utf-8"))
    return {k: v for k, v in table.items() if k.startswith("claude-")}


class PriceTableTests(unittest.TestCase):
    def test_table_has_models(self):
        self.assertGreaterEqual(len(model_rows()), 1)

    def test_every_row_is_priced_sanely(self):
        for model, row in model_rows().items():
            with self.subTest(model=model):
                self.assertIsInstance(row, dict)
                for c in CLASSES:
                    v = row.get(c)
                    self.assertTrue(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 1000,
                                    f"{model}.{c} = {v!r}")
                self.assertLess(row["cache_read"], row["input"], f"{model}: cache reads cost less than input")
                self.assertLess(row["input"], row["output"], f"{model}: output costs more than input")
                w = row.get("meter_weight", 1.0)
                self.assertTrue(isinstance(w, (int, float)) and w > 0, f"{model}.meter_weight = {w!r}")
                for c, cw in (row.get("class_weight") or {}).items():
                    self.assertTrue(isinstance(cw, (int, float)) and 0 <= cw <= 1, f"{model}.class_weight.{c}")


if __name__ == "__main__":
    unittest.main()
