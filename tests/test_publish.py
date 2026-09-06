import unittest
from datetime import datetime, timezone
from tracker.publish import build_public_json, usd_per_pct, blended_price_per_token

def probe(day, model, tpp, account="dave"):
    return {"ts": f"2026-09-{day:02d}T08:00:00+00:00", "model": model, "effort": "low", "tokens_per_pct": tpp,
            "tokens": {"input": 100, "output": 400, "cache_read": tpp - 500, "cache_write": 0},
            "prompts": 8, "tick_from": 10, "tick_to": 11, "elapsed_s": 60, "account": account}

PASSIVE = {"generated_at": "2026-09-05T20:00:00+00:00", "plan_ratio_5x_to_20x": 0.25,
           "split": {"input": 0.062, "output": 0.021, "cache_read": 0.907, "cache_write": 0.010},
           "history": {"2026-08-01": {"tokens_per_pct": 100000, "interpolated": False}}}
EFFORT = {"claude-sonnet-5": {"low": 900000, "high": 2520000}}
PRICES = {"claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
          "claude-opus-5": {"input": 7.5, "output": 37.5, "cache_read": 0.75, "cache_write": 9.375}}


class UsdPerPctTests(unittest.TestCase):
    def test_usd_per_pct_brief_example(self):
        row = {"tokens": {"input": 0, "output": 0, "cache_read": 100_000, "cache_write": 400_000},
               "tick_from": 10, "tick_to": 11}
        price = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}
        self.assertAlmostEqual(usd_per_pct(row, price), 1.02, delta=1e-9)

    def test_blended_price_per_token_brief_example(self):
        # blended_price_per_token returns USD per TOKEN (note the /1e6 in its
        # body converts from prices.json's USD-per-million-tokens), not per
        # million tokens -- pin that here since a missed conversion would be
        # a silent 1e6x error in every derived model's tokens_per_window.
        price = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}
        split = {"cache_read": 0.9, "cache_write": 0.1}
        self.assertAlmostEqual(blended_price_per_token(split, price), 4.3e-7, delta=1e-15)


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

    def test_every_priced_model_gets_a_rate_derived_from_the_one_probed_model(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(set(j["rates"]), {"claude-sonnet-5", "claude-opus-5"})
        sonnet, opus = j["rates"]["claude-sonnet-5"], j["rates"]["claude-opus-5"]
        # same dollar invariant, different price -> different tokens_per_window
        self.assertEqual(sonnet["api_value_per_window"], opus["api_value_per_window"])
        self.assertEqual(opus["source"], "derived")
        self.assertEqual(sonnet["source"], "probe")
        latest = probe(5, "claude-sonnet-5", 420000)
        api_value = usd_per_pct(latest, PRICES["claude-sonnet-5"]) * 100
        expected_opus_tpw = round(api_value / blended_price_per_token(PASSIVE["split"], PRICES["claude-opus-5"]))
        self.assertEqual(opus["tokens_per_window"], expected_opus_tpw)
        # opus is priced ~2.5x sonnet across the board, so its derived rate is ~2.5x fewer tokens
        self.assertLess(opus["tokens_per_window"], sonnet["tokens_per_window"])

    def test_history_marks_probe_days_by_which_model_was_actually_probed(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        sonnet_hist = [h for h in j["history"]["claude-sonnet-5"] if h["source"] == "probe" or h["date"] >= "2026-09-01"]
        opus_hist = [h for h in j["history"]["claude-opus-5"] if h["date"] >= "2026-09-01"]
        self.assertTrue(all(h["source"] == "probe" for h in sonnet_hist))
        self.assertTrue(all(h["source"] == "derived" for h in opus_hist))

    def test_shape_empty_passive_split_falls_back_to_the_latest_row_split(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, {}, EFFORT, PRICES, now)
        r = j["rates"]["claude-sonnet-5"]
        # with no passive split, the row's own class fractions are used, which reduces to
        # the raw tokens_per_pct * 100 for the model that was actually probed.
        self.assertAlmostEqual(r["tokens_per_window"], 42_000_000, delta=1)
        self.assertIn("api_value_per_window", r)
        # the same fallback split is used to derive the unprobed model's rate too
        opus = j["rates"]["claude-opus-5"]
        latest = probe(5, "claude-sonnet-5", 420000)
        api_value = usd_per_pct(latest, PRICES["claude-sonnet-5"]) * 100
        row_split = {"input": 100 / 420000, "output": 400 / 420000, "cache_read": 419500 / 420000, "cache_write": 0.0}
        expected_opus_tpw = round(api_value / blended_price_per_token(row_split, PRICES["claude-opus-5"]))
        self.assertEqual(opus["tokens_per_window"], expected_opus_tpw)

    def test_change_event_surfaces(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 11)] + [probe(d, "claude-sonnet-5", 300000) for d in range(11, 16)]
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 15, tzinfo=timezone.utc))
        self.assertEqual(j["last_change"]["direction"], "decreased")
        self.assertEqual(j["last_change"]["model"], "all")
        self.assertIn(j["last_change"]["percent"], range(20, 40))


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
            build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 20, tzinfo=timezone.utc))

    def test_passive_generated_at_is_null_when_missing(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        j = build_public_json(rows, {}, EFFORT, PRICES, datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc))
        self.assertIsNone(j["passive_generated_at"])
