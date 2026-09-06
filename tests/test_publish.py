import unittest
from datetime import date, datetime, timezone
from tracker.publish import probe_daily_series, build_public_json, usd_per_pct, blended_price_per_token

def probe(day, model, tpp, account="dave"):
    return {"ts": f"2026-09-{day:02d}T08:00:00+00:00", "model": model, "effort": "low", "tokens_per_pct": tpp,
            "tokens": {"input": 100, "output": 400, "cache_read": tpp - 500, "cache_write": 0},
            "prompts": 8, "tick_from": 10, "tick_to": 11, "elapsed_s": 60, "account": account}

PASSIVE = {"generated_at": "2026-09-05T20:00:00+00:00", "plan_ratio_5x_to_20x": 0.25,
           "split": {"input": 0.062, "output": 0.021, "cache_read": 0.907, "cache_write": 0.010},
           "history": {"2026-08-01": {"tokens_per_pct": 100000, "interpolated": False}}}
EFFORT = {"claude-sonnet-5": {"low": 900000, "high": 2520000}}
PRICES = {"claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}}


class UsdPerPctTests(unittest.TestCase):
    def test_usd_per_pct_brief_example(self):
        row = {"tokens": {"input": 0, "output": 0, "cache_read": 100_000, "cache_write": 400_000},
               "tick_from": 10, "tick_to": 11}
        price = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}
        self.assertAlmostEqual(usd_per_pct(row, price), 1.02, delta=1e-9)

    def test_blended_price_per_token_brief_example(self):
        price = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}
        split = {"cache_read": 0.9, "cache_write": 0.1}
        self.assertAlmostEqual(blended_price_per_token(split, price), 4.3e-7, delta=1e-15)


class SeriesTests(unittest.TestCase):
    def test_trailing_median_per_model(self):
        rows = [probe(1, "claude-sonnet-5", 400000), probe(2, "claude-sonnet-5", 420000), probe(3, "claude-sonnet-5", 410000)]
        s = probe_daily_series(rows, PRICES, {})
        self.assertAlmostEqual(s["claude-sonnet-5"][date(2026, 9, 3)], 41_000_000, delta=1e-3)
        self.assertAlmostEqual(s["claude-sonnet-5"][date(2026, 9, 1)], 40_000_000, delta=1e-3)

class BuildTests(unittest.TestCase):
    def test_shape(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(j["generated_at"], "2026-09-05T20:15:00+00:00")
        self.assertEqual(j["last_sample_at"], "2026-09-05T08:00:00+00:00")
        self.assertEqual(j["plan_measured"], "max20")
        self.assertEqual(j["plan_ratios"], {"pro": 0.05, "max5": 0.25, "max20": 1.0})
        self.assertEqual(j["rate_basis"], "api_value")
        r = j["rates"]["claude-sonnet-5"]
        latest = probe(5, "claude-sonnet-5", 420000)
        expected_tpw = usd_per_pct(latest, PRICES["claude-sonnet-5"]) * 100 / blended_price_per_token(PASSIVE["split"], PRICES["claude-sonnet-5"])
        self.assertAlmostEqual(r["tokens_per_window"], round(expected_tpw), delta=1)
        self.assertEqual(r["source"], "probe")
        self.assertEqual(r["split"], PASSIVE["split"])
        self.assertEqual(r["api_value_per_window"], round(usd_per_pct(latest, PRICES["claude-sonnet-5"]) * 100, 2))
        self.assertEqual(j["effort"], EFFORT)
        self.assertEqual(j["api_price_per_mtok"], PRICES)
        self.assertIsNone(j["last_change"])
        hist = j["history"]["claude-sonnet-5"]
        self.assertEqual(hist[0], {"date": "2026-08-01", "tokens_per_window": 10_000_000, "source": "passive", "interpolated": False})
        self.assertEqual(hist[-1]["source"], "probe")

    def test_shape_empty_passive_split_falls_back_to_row_split(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, {}, EFFORT, PRICES, now)
        r = j["rates"]["claude-sonnet-5"]
        # with no passive split, the row's own class fractions are used, which reduces to
        # the raw tokens_per_pct * 100 (see probe_daily_series docstring in the brief).
        self.assertAlmostEqual(r["tokens_per_window"], 42_000_000, delta=1)
        self.assertIn("api_value_per_window", r)

    def test_change_event_surfaces(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 11)] + [probe(d, "claude-sonnet-5", 360000) for d in range(11, 16)]
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 15, tzinfo=timezone.utc))
        self.assertEqual(j["last_change"]["direction"], "decreased")
        self.assertEqual(j["last_change"]["percent"], 14)
        self.assertEqual(j["last_change"]["model"], "claude-sonnet-5")


class FailurePathTests(unittest.TestCase):
    def test_corrupt_input_exits_nonzero_and_keeps_previous_output(self):
        import tempfile
        from pathlib import Path
        from tracker.publish import main
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "claude-usage.json"
            out.write_text('{"previous": true}')
            (d / "probes.jsonl").write_text("not json\n")
            (d / "passive.json").write_text("{}")
            rc = main(["--probes", str(d / "probes.jsonl"), "--passive", str(d / "passive.json"), "--out", str(out)])
            self.assertNotEqual(rc, 0)
            self.assertEqual(out.read_text(), '{"previous": true}')


class GuardTests(unittest.TestCase):
    def _files(self, d, *, rows=None, effort=None, prices=None, passive=None):
        import json
        from pathlib import Path
        d = Path(d)
        rows = [probe(5, "claude-sonnet-5", 420000)] if rows is None else rows
        (d / "probes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (d / "passive.json").write_text(json.dumps(PASSIVE if passive is None else passive))
        (d / "effort.json").write_text(json.dumps(EFFORT if effort is None else effort))
        (d / "prices.json").write_text(json.dumps(PRICES if prices is None else prices))
        return d

    def _run(self, d):
        from tracker.publish import main
        return main(["--probes", str(d / "probes.jsonl"), "--passive", str(d / "passive.json"),
                     "--effort", str(d / "effort.json"), "--prices", str(d / "prices.json"),
                     "--out", str(d / "out.json")])

    def test_placeholder_effort_matrix_refuses_and_does_not_write(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t, effort={"_status": "placeholder", **EFFORT})
            self.assertEqual(self._run(d), 1)
            self.assertFalse((d / "out.json").exists())

    def test_underscore_keys_are_filtered_out_of_prices(self):
        import json
        import tempfile
        now = datetime.now(timezone.utc)
        row = probe(5, "claude-sonnet-5", 420000)
        row["ts"] = now.isoformat()
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t, rows=[row], effort={k: v for k, v in EFFORT.items()},
                            prices={"_source": "docs", **PRICES})
            self.assertEqual(self._run(d), 0)
            j = json.loads((d / "out.json").read_text())
            self.assertEqual(j["api_price_per_mtok"], PRICES)
            self.assertEqual(j["passive_generated_at"], PASSIVE["generated_at"])

    def test_no_rates_refuses(self):
        with self.assertRaises(ValueError):
            build_public_json([], PASSIVE, EFFORT, PRICES, datetime(2026, 9, 5, tzinfo=timezone.utc))

    def test_stale_last_sample_refuses(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        with self.assertRaises(ValueError):
            build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 12, tzinfo=timezone.utc))

    def test_passive_generated_at_is_null_when_missing(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        j = build_public_json(rows, {}, EFFORT, PRICES, datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc))
        self.assertIsNone(j["passive_generated_at"])
