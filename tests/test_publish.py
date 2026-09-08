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

    def test_usd_per_pct_applies_meter_weight(self):
        row = {"tokens": {"input": 0, "output": 0, "cache_read": 100_000, "cache_write": 400_000},
               "tick_from": 10, "tick_to": 11}
        price = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5, "meter_weight": 2.0}
        # same list-dollar bundle as test_usd_per_pct_brief_example (1.02), but this
        # model's meter weight doubles the meter-dollar reading.
        self.assertAlmostEqual(usd_per_pct(row, price), 2.04, delta=1e-9)

    def test_usd_per_pct_missing_meter_weight_defaults_to_one(self):
        row = {"tokens": {"input": 0, "output": 0, "cache_read": 100_000, "cache_write": 400_000},
               "tick_from": 10, "tick_to": 11}
        price = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}
        self.assertAlmostEqual(usd_per_pct(row, price), 1.02, delta=1e-9)

    def test_usd_per_pct_applies_class_weight(self):
        # 100k output tokens at $10/M is $1.00 list; an output class_weight of 1.8
        # makes the meter see $1.80, while the cache classes stay at list.
        row = {"tokens": {"input": 0, "output": 100_000, "cache_read": 100_000, "cache_write": 0},
               "tick_from": 10, "tick_to": 11}
        price = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5,
                 "class_weight": {"output": 1.8}}
        self.assertAlmostEqual(usd_per_pct(row, price), 1.82, delta=1e-9)

    def test_blended_price_per_token_applies_class_weight(self):
        price = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5,
                 "class_weight": {"output": 1.8}}
        split = {"output": 0.5, "cache_write": 0.5}
        self.assertAlmostEqual(blended_price_per_token(split, price), (0.5 * 18 + 0.5 * 2.5) / 1e6, delta=1e-15)

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
        # days before the first probe are held flat at the first (only) regime's value --
        # no plan noise, no passive daily series, just the step function's opening level.
        self.assertEqual(hist[0], {"date": "2026-08-01", "tokens_per_window": r["tokens_per_window"],
                                   "api_value_per_window": r["api_value_per_window"], "source": "held", "interpolated": False})
        self.assertEqual(hist[-1]["source"], "probe")

    def test_history_days_carry_the_held_regime_dollar_value_for_every_model(self):
        # The page shows dollars per window beside tokens per window with the same
        # step treatment, so every history day carries the regime's held dollar
        # value: one figure per day, the same for every model, stepping only on a
        # detected change and matching rates[model].api_value_per_window today.
        rows = ([probe(d, "claude-sonnet-5", 420000) for d in range(1, 11)]
                + [probe(d, "claude-sonnet-5", 300000) for d in range(11, 16)])
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        event_date = j["last_change"]["date"]
        sonnet, opus = j["history"]["claude-sonnet-5"], j["history"]["claude-opus-5"]
        self.assertEqual([h["api_value_per_window"] for h in sonnet], [h["api_value_per_window"] for h in opus])
        before = {h["api_value_per_window"] for h in sonnet if h["date"] < event_date}
        after = {h["api_value_per_window"] for h in sonnet if h["date"] >= event_date}
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)
        self.assertGreater(before.pop(), after.pop())
        self.assertEqual(sonnet[-1]["api_value_per_window"], j["rates"]["claude-sonnet-5"]["api_value_per_window"])
        self.assertEqual(sonnet[-1]["api_value_per_window"], j["rates"]["claude-opus-5"]["api_value_per_window"])

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

    def test_outlier_rows_are_skipped_everywhere(self):
        # The rotation's drift check flags a lone outlier in place (tracker.rotate);
        # it stays in history/probes.jsonl but the publisher must never see it: not
        # in the regime median, not as a probe day, not as the latest sample.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        rows.append(dict(probe(7, "claude-sonnet-5", 900000), outlier=True))
        now = datetime(2026, 9, 7, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        clean = build_public_json(rows[:-1], PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(j["rates"], clean["rates"])
        self.assertEqual(j["history"], clean["history"])
        self.assertEqual(j["last_sample_at"], "2026-09-05T08:00:00+00:00")
        self.assertIsNone(j["last_change"])
        self.assertEqual(next(h for h in j["history"]["claude-sonnet-5"] if h["date"] == "2026-09-07")["source"], "derived")

    def test_only_outlier_rows_refuses(self):
        rows = [dict(probe(5, "claude-sonnet-5", 420000), outlier=True)]
        with self.assertRaises(ValueError):
            build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 5, 20, tzinfo=timezone.utc))

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

    def test_fable_and_sonnet_probe_rows_at_same_meter_value_yield_same_api_value_per_window(self):
        prices = {**PRICES, "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25,
                                                  "cache_write": 12.5, "meter_weight": 2.0}}
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        sonnet_rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        j_sonnet = build_public_json(sonnet_rows, PASSIVE, EFFORT, prices, now)
        # tokens_per_pct chosen so tokens_usd(tokens, fable_price) * meter_weight(2.0)
        # equals tokens_usd(sonnet 420000 tokens, sonnet_price) -- same meter dollars,
        # different list dollars and a different model.
        fable_rows = [probe(d, "claude-fable-5-1", 180800) for d in range(1, 6)]
        j_fable = build_public_json(fable_rows, PASSIVE, EFFORT, prices, now)
        self.assertAlmostEqual(j_sonnet["rates"]["claude-sonnet-5"]["api_value_per_window"],
                                j_fable["rates"]["claude-fable-5-1"]["api_value_per_window"], delta=1e-6)

    def test_fable_derived_tokens_are_half_what_they_would_be_at_weight_one(self):
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        fable_price = {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 12.5}
        prices_weight_one = {**PRICES, "claude-fable-5-1": fable_price}
        prices_weight_two = {**PRICES, "claude-fable-5-1": {**fable_price, "meter_weight": 2.0}}
        j1 = build_public_json(rows, PASSIVE, EFFORT, prices_weight_one, now)
        j2 = build_public_json(rows, PASSIVE, EFFORT, prices_weight_two, now)
        self.assertAlmostEqual(j2["rates"]["claude-fable-5-1"]["tokens_per_window"],
                                j1["rates"]["claude-fable-5-1"]["tokens_per_window"] / 2, delta=1)

    def test_change_event_surfaces(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 11)] + [probe(d, "claude-sonnet-5", 300000) for d in range(11, 16)]
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 15, tzinfo=timezone.utc))
        self.assertEqual(j["last_change"]["direction"], "decreased")
        self.assertEqual(j["last_change"]["model"], "all")
        self.assertIn(j["last_change"]["percent"], range(20, 40))

    def test_events_excludes_the_plan_change(self):
        # Jonathan's ruling: the public chart is a step function of the measured limit
        # only -- nothing about his own plan history belongs in it.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual([e for e in j["events"] if e["kind"] == "plan"], [])

    def test_change_produces_exactly_two_flat_levels_stepping_on_the_event_date(self):
        rows = ([probe(d, "claude-sonnet-5", 420000) for d in range(1, 11)]
                + [probe(d, "claude-sonnet-5", 300000) for d in range(11, 16)])
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        hist = j["history"]["claude-sonnet-5"]
        event_date = j["last_change"]["date"]
        before = [h["tokens_per_window"] for h in hist if h["date"] < event_date]
        on_and_after = [h["tokens_per_window"] for h in hist if h["date"] >= event_date]
        self.assertTrue(before and on_and_after)
        self.assertEqual(len(set(before)), 1, before)
        self.assertEqual(len(set(on_and_after)), 1, on_and_after)
        self.assertNotEqual(before[0], on_and_after[0])
        self.assertEqual({h["tokens_per_window"] for h in hist}, {before[0], on_and_after[0]})

    def test_events_includes_every_detected_change(self):
        rows = ([probe(d, "claude-sonnet-5", 420000) for d in range(1, 11)]
                + [probe(d, "claude-sonnet-5", 300000) for d in range(11, 16)])
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        change_events = [e for e in j["events"] if e["kind"] == "change"]
        self.assertTrue(change_events)
        self.assertEqual(change_events[-1]["date"], j["last_change"]["date"])
        self.assertEqual(change_events[-1]["label"],
                          f"Window changed -{j['last_change']['percent']}%")


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


class WeeklyWindowsPassthroughTests(unittest.TestCase):
    # 2026-08-14 is the last full Max 5x week (ends before PLAN_CHANGE 2026-08-18);
    # 2026-08-21's (2026-08-14, 2026-08-21] span straddles PLAN_CHANGE and belongs
    # to neither plan; 2026-08-28 and 2026-09-04 are full Max 20x weeks.
    PASSIVE_WEEKLY = {"current": 6.46, "history": [
        {"week_ending": "2026-08-14", "windows": 10.91, "five_hour_pct": 400.0, "seven_day_pct": 36.7},
        {"week_ending": "2026-08-21", "windows": 6.8, "five_hour_pct": 300.0, "seven_day_pct": 44.1},
        {"week_ending": "2026-08-28", "windows": 6.58, "five_hour_pct": 250.0, "seven_day_pct": 38.0},
        {"week_ending": "2026-09-04", "windows": 6.35, "five_hour_pct": 324.0, "seven_day_pct": 51.0},
    ]}

    def test_absent_from_passive_omits_top_level_key(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertNotIn("weekly_windows", j)

    def test_passive_weeks_are_split_by_plan_and_the_straddling_week_is_dropped(self):
        # None of the existing rows carry five_hour_before/after, so the probe
        # series is empty and max20 is passive-only.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        passive = dict(PASSIVE, weekly_windows=self.PASSIVE_WEEKLY)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        ww = j["weekly_windows"]
        self.assertEqual([h["week_ending"] for h in ww["max5"]["history"]], ["2026-08-14"])
        self.assertEqual([h["week_ending"] for h in ww["max20"]["history"]], ["2026-08-28", "2026-09-04"])
        self.assertEqual(ww["passive"], self.PASSIVE_WEEKLY)
        self.assertEqual(ww["probe"], {"current": None, "history": []})
        self.assertEqual(ww["max20"]["current"], self.PASSIVE_WEEKLY["current"])
        self.assertFalse(ww["max20"]["assumed"])
        self.assertFalse(ww["max5"]["assumed"])

    def test_pro_publishes_as_an_assumed_copy_of_max5(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        passive = dict(PASSIVE, weekly_windows=self.PASSIVE_WEEKLY)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        ww = j["weekly_windows"]
        self.assertEqual(ww["pro"]["current"], ww["max5"]["current"])
        self.assertEqual(ww["pro"]["history"], ww["max5"]["history"])
        self.assertTrue(ww["pro"]["assumed"])

    def test_probe_weeks_replace_passive_max20_weeks_from_the_first_probe_week_on(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        for r, (fhb, fha, sdb, sda, wk) in zip(rows, [
            (10.0, 40.0, 10.0, 15.0, "2026-08-28T03:59:59+00:00"),
            (10.0, 45.0, 10.0, 16.0, "2026-08-28T03:59:59+00:00"),
            (10.0, 50.0, 10.0, 15.0, "2026-09-04T03:59:59+00:00"),
            (10.0, 55.0, 10.0, 16.0, "2026-09-04T03:59:59+00:00"),
            (10.0, 30.0, 10.0, 20.0, "2026-09-11T03:59:59+00:00"),  # newest week, incomplete
        ]):
            r["five_hour_before"], r["five_hour_after"] = fhb, fha
            r["seven_day_before"], r["seven_day_after"] = sdb, sda
            r["seven_day_resets_at"] = wk
        passive = dict(PASSIVE, weekly_windows=self.PASSIVE_WEEKLY)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        ww = j["weekly_windows"]
        self.assertEqual(len(ww["probe"]["history"]), 3)
        # 08-28 and 09-04 are covered by the probe now, so passive's max20 weeks
        # are entirely superseded.
        self.assertEqual(ww["max20"]["history"], ww["probe"]["history"])
        self.assertEqual(ww["max20"]["current"], ww["probe"]["current"])
        self.assertNotEqual(ww["max20"]["current"], self.PASSIVE_WEEKLY["current"])
        # max5 is untouched by any of this -- it is frozen passive-era history.
        self.assertEqual([h["week_ending"] for h in ww["max5"]["history"]], ["2026-08-14"])

    def test_no_weekly_event_on_real_max20_history(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        passive = dict(PASSIVE, weekly_windows=self.PASSIVE_WEEKLY)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])
        self.assertIsNone(j["last_change"])

    def test_weekly_event_fires_on_a_synthetic_max20_drop(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        weekly = {"current": 6.46, "history": [
            {"week_ending": "2026-08-28", "windows": 6.58, "five_hour_pct": 250.0, "seven_day_pct": 38.0},
            {"week_ending": "2026-09-04", "windows": 6.35, "five_hour_pct": 324.0, "seven_day_pct": 51.0},
            {"week_ending": "2026-09-11", "windows": 6.5, "five_hour_pct": 300.0, "seven_day_pct": 46.0},
            {"week_ending": "2026-09-18", "windows": 4.0, "five_hour_pct": 260.0, "seven_day_pct": 65.0},
            # confirming week: detect_changes now needs the next reading to also
            # cross the threshold, in the same direction, before the 2026-09-18
            # step fires (see tracker/detect.py)
            {"week_ending": "2026-09-25", "windows": 4.0, "five_hour_pct": 260.0, "seven_day_pct": 65.0},
        ]}
        passive = dict(PASSIVE, weekly_windows=weekly)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        weekly_change_events = [e for e in j["events"] if e.get("scope") == "weekly"]
        self.assertEqual(len(weekly_change_events), 1)
        self.assertEqual(weekly_change_events[0]["date"], "2026-09-18")
        self.assertEqual(j["last_change"]["scope"], "weekly")
        self.assertEqual(j["last_change"]["date"], "2026-09-18")
        self.assertEqual(j["last_change"]["direction"], "decreased")


class LastChangeScopeTests(unittest.TestCase):
    WEEKLY_MAX20 = [
        {"week_ending": "2026-08-28", "windows": 6.58, "five_hour_pct": 250.0, "seven_day_pct": 38.0},
        {"week_ending": "2026-09-04", "windows": 6.35, "five_hour_pct": 324.0, "seven_day_pct": 51.0},
        {"week_ending": "2026-09-11", "windows": 6.5, "five_hour_pct": 300.0, "seven_day_pct": 46.0},
        {"week_ending": "2026-09-18", "windows": 4.0, "five_hour_pct": 260.0, "seven_day_pct": 65.0},
        # confirming week: see tracker/detect.py -- a step needs a next reading
        # past the same base, in the same direction, before it fires.
        {"week_ending": "2026-09-25", "windows": 4.0, "five_hour_pct": 260.0, "seven_day_pct": 65.0},
    ]

    def test_newer_weekly_event_beats_an_older_window_event(self):
        # Window event fires around 2026-09-05 (early Sept rows); the weekly
        # event is dated 2026-09-18, later, so it must win last_change.
        rows = ([probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
                + [probe(d, "claude-sonnet-5", 300000) for d in range(6, 10)])
        passive = dict(PASSIVE, weekly_windows={"current": 6.46, "history": self.WEEKLY_MAX20})
        now = datetime(2026, 9, 10, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        window_events = [e for e in j["events"] if e.get("scope") == "window"]
        self.assertTrue(window_events)
        self.assertLess(window_events[-1]["date"], "2026-09-18")
        self.assertEqual(j["last_change"]["scope"], "weekly")
        self.assertEqual(j["last_change"]["date"], "2026-09-18")

    def test_older_weekly_event_loses_to_a_newer_window_event(self):
        # Same weekly step (dated 2026-09-18), but now the window event is
        # pushed later than it, past 2026-09-18, so the window event must win.
        rows = ([probe(d, "claude-sonnet-5", 420000) for d in range(1, 21)]
                + [probe(d, "claude-sonnet-5", 300000) for d in range(21, 26)])
        passive = dict(PASSIVE, weekly_windows={"current": 6.46, "history": self.WEEKLY_MAX20})
        now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        window_events = [e for e in j["events"] if e.get("scope") == "window"]
        weekly_events = [e for e in j["events"] if e.get("scope") == "weekly"]
        self.assertTrue(window_events)
        self.assertTrue(weekly_events)
        self.assertGreater(window_events[-1]["date"], weekly_events[-1]["date"])
        self.assertEqual(j["last_change"]["scope"], "window")
        self.assertEqual(j["last_change"]["date"], window_events[-1]["date"])
