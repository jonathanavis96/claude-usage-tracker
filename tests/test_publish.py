import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tracker.publish import build_public_json, usd_per_pct, blended_price_per_token

def probe(day, model, tpp, account="dave"):
    return {"ts": f"2026-09-{day:02d}T08:00:00+00:00", "model": model, "effort": "low", "tokens_per_pct": tpp,
            "tokens": {"input": 100, "output": 400, "cache_read": tpp - 500, "cache_write": 0},
            "prompts": 8, "tick_from": 10, "tick_to": 11, "elapsed_s": 60, "account": account}

PASSIVE = {"generated_at": "2026-09-05T20:00:00+00:00", "plan_ratio_5x_to_20x": 0.25,
           "split": {"input": 0.062, "output": 0.021, "cache_read": 0.907, "cache_write": 0.010},
           "history": {"2026-08-01": {"tokens_per_pct": 100000, "interpolated": False}}}
EFFORT = {"claude-sonnet-5": {"low": 900000, "high": 2520000}}


def window(window_ending, d5, d7):
    return {"window_ending": window_ending, "windows": round(d5 / d7, 2) if d7 else None,
            "five_hour_pct": d5, "seven_day_pct": d7}


# history/passive.json's max20 weeks as published on 2026-09-15 (issue #25): the
# open 09-18 week blends pre- and post-cut days to 5.43.
ISSUE_25_WEEKS = [
    {"week_ending": "2026-08-28", "windows": 6.58, "five_hour_pct": 250.0, "seven_day_pct": 38.0},
    {"week_ending": "2026-09-04", "windows": 6.35, "five_hour_pct": 324.0, "seven_day_pct": 51.0},
    {"week_ending": "2026-09-11", "windows": 6.02, "five_hour_pct": 373.0, "seven_day_pct": 62.0},
    {"week_ending": "2026-09-18", "windows": 5.43, "five_hour_pct": 114.0, "seven_day_pct": 21.0},
]
# The per-window points the issue quotes from masterrig's raw log for the same span.
ISSUE_25_BY_WINDOW = [
    window("2026-09-10T21:29:00+00:00", 72.0, 11.0),
    window("2026-09-12T16:30:00+00:00", 17.0, 3.0),
    window("2026-09-12T21:30:00+00:00", 27.0, 5.0),
    window("2026-09-14T16:30:00+00:00", 37.0, 8.0),
    window("2026-09-14T21:30:00+00:00", 17.0, 4.0),
    window("2026-09-15T16:30:00+00:00", 27.0, 6.0),
]
PRICES = {"claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
          "claude-opus-5": {"input": 7.5, "output": 37.5, "cache_read": 0.75, "cache_write": 9.375}}


def gs_passive_report(*, account="dave", end, cache_write, delta_pct=10, model="claude-sonnet-5"):
    """A minimal tracker.gs_passive.report() shape (issue #39): just enough for
    passive_dollar_readings to read one accepted stretch. `cache_write` tokens at
    PRICES' $3.75/Mtok give (cache_write * 3.75 / 1e6) meter dollars for the
    stretch; dividing by delta_pct and scaling to a full window is
    passive_dollar_readings' own job, not this fixture's."""
    return {"accounts": {account: {"stretches": [
        {"status": "accepted", "end": end, "delta_pct": delta_pct,
         "tokens": {model: {"input": 0, "output": 0, "cache_read": 0, "cache_write": cache_write}}},
    ]}}}


# 352400 cache-write tokens at PRICES' cache_write price is $1.3215, and dividing
# by this stretch's own 10% and scaling to 100% reproduces probe(5, ...)'s own
# 13.215 dollars-per-window exactly, so a test can mix the two without a change
# event firing on the value alone.
GS_PASSIVE_MATCHING_PROBE = gs_passive_report(end="2026-09-06T08:00:00+00:00", cache_write=352400)


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
        self.assertIsNone(opus["probed_at"])
        self.assertEqual(sonnet["probed_at"], "2026-09-05T08:00:00+00:00")
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

    def test_each_probed_model_publishes_its_own_probe_source(self):
        # All three models are probed in rotation, one per 12 hours -- each has
        # its own probe rows even on days it wasn't the one probed most recently.
        prices = {**PRICES, "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25,
                                                  "cache_write": 12.5, "meter_weight": 2.0}}
        rows = [probe(1, "claude-sonnet-5", 420000), probe(2, "claude-opus-5", 420000),
                probe(3, "claude-fable-5-1", 420000), probe(4, "claude-sonnet-5", 420000)]
        now = datetime(2026, 9, 4, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, prices, now)
        for model, expected_ts in [("claude-sonnet-5", "2026-09-04T08:00:00+00:00"),
                                    ("claude-opus-5", "2026-09-02T08:00:00+00:00"),
                                    ("claude-fable-5-1", "2026-09-03T08:00:00+00:00")]:
            self.assertEqual(j["rates"][model]["source"], "probe", model)
            self.assertEqual(j["rates"][model]["probed_at"], expected_ts, model)

    def test_probe_accounts_lists_distinct_accounts_on_usable_rows(self):
        rows = [probe(1, "claude-sonnet-5", 420000, account="dave"),
                probe(2, "claude-sonnet-5", 420000, account="jwork"),
                dict(probe(3, "claude-sonnet-5", 420000, account="dave"), outlier=True)]
        now = datetime(2026, 9, 2, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(j["probe_accounts"], ["dave", "jwork"])

    def test_model_with_only_an_outlier_row_publishes_derived_with_null_probed_at(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        rows.append(dict(probe(5, "claude-opus-5", 420000), outlier=True))
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(j["rates"]["claude-opus-5"]["source"], "derived")
        self.assertIsNone(j["rates"]["claude-opus-5"]["probed_at"])

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


class GsPassiveTests(unittest.TestCase):
    """Passive readings from tracker.gs_passive (issue #39) extending the probe series."""

    def test_passive_reading_extends_the_series_and_becomes_the_newest(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=GS_PASSIVE_MATCHING_PROBE)
        self.assertEqual(j["instrument"], "passive")
        self.assertEqual(j["last_sample_at"], "2026-09-06T08:00:00+00:00")
        self.assertEqual(j["passive_accounts"], ["dave"])
        r = j["rates"]["claude-sonnet-5"]
        self.assertEqual(r["source"], "passive")
        self.assertEqual(r["measured_at"], "2026-09-06T08:00:00+00:00")
        # probed_at is unchanged by a passive reading: still this model's own latest probe row.
        self.assertEqual(r["probed_at"], "2026-09-05T08:00:00+00:00")
        # An unprobed model reads "passive" too -- a passive reading is model-agnostic,
        # so it can't tell the page this is specifically an opus day either.
        self.assertEqual(j["rates"]["claude-opus-5"]["source"], "passive")

    def test_no_gs_passive_keeps_source_probe_as_before(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(j["instrument"], "probe")
        self.assertEqual(j["rates"]["claude-sonnet-5"]["source"], "probe")
        self.assertEqual(j["passive_accounts"], [])

    def test_freshness_guard_is_satisfied_by_a_fresh_passive_reading_when_probes_are_stale(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]  # 2026-09-01..05
        now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)  # 21 days past the last probe
        # Without a passive reading this exact series is already covered by
        # FailurePathTests.test_stale_last_sample_refuses; here a passive reading
        # 1 day old keeps the newest-reading-of-either-kind guard satisfied.
        fresh_passive = gs_passive_report(end="2026-09-25T08:00:00+00:00", cache_write=352400)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=fresh_passive)
        self.assertEqual(j["instrument"], "passive")
        self.assertEqual(j["last_sample_at"], "2026-09-25T08:00:00+00:00")

    def test_publishing_works_with_zero_probe_rows(self):
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now, gs_passive=GS_PASSIVE_MATCHING_PROBE)
        self.assertEqual(j["instrument"], "passive")
        self.assertEqual(j["probe_accounts"], [])
        self.assertEqual(j["passive_accounts"], ["dave"])
        r = j["rates"]["claude-sonnet-5"]
        self.assertEqual(r["source"], "passive")
        self.assertIsNone(r["probed_at"])
        self.assertIsNone(r["probe_effort"])

    def test_zero_probe_rows_and_no_split_anywhere_refuses_cleanly(self):
        # Review finding: with no probe rows there is no row split to fall back to, and
        # a passive.json with no "split" key leaves passive_split == {} --
        # blended_price_per_token({}, price) is 0.0, which would ZeroDivisionError in the
        # rates loop below. That must surface as a ValueError (which main() already
        # catches), never the bare ZeroDivisionError.
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        passive_without_split = {k: v for k, v in PASSIVE.items() if k != "split"}
        with self.assertRaises(ValueError):
            build_public_json([], passive_without_split, EFFORT, PRICES, now, gs_passive=GS_PASSIVE_MATCHING_PROBE)

    def test_no_readings_at_all_refuses(self):
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            build_public_json([], PASSIVE, EFFORT, PRICES, now)

    def test_passive_accounts_only_lists_accounts_with_an_accepted_stretch(self):
        report = {"accounts": {
            "dave": GS_PASSIVE_MATCHING_PROBE["accounts"]["dave"],
            "jwork": {"stretches": [{"status": "unaccounted", "end": "2026-09-06T08:00:00+00:00",
                                     "delta_pct": 5, "tokens": {}}]},
        }}
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=report)
        self.assertEqual(j["passive_accounts"], ["dave"])


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

    def _run(self, d, *, gs_passive=None):
        from tracker.publish import main
        argv = ["--probes", str(d / "probes.jsonl"), "--passive", str(d / "passive.json"),
                "--effort", str(d / "effort.json"), "--prices", str(d / "prices.json"),
                "--out", str(d / "out.json")]
        if gs_passive is not None:
            argv += ["--gs-passive", str(gs_passive)]
        return main(argv)

    def test_missing_gs_passive_file_is_a_warning_and_output_is_unchanged(self):
        import json
        import tempfile
        now = datetime.now(timezone.utc)
        row = probe(5, "claude-sonnet-5", 420000)
        row["ts"] = now.isoformat()
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t, rows=[row])
            self.assertEqual(self._run(d), 0)
            without = json.loads((d / "out.json").read_text())
            self.assertEqual(self._run(d, gs_passive=d / "does-not-exist.json"), 0)
            with_missing = json.loads((d / "out.json").read_text())
            # generated_at is `datetime.now()` at call time (this test doesn't pin `now`);
            # everything else must be identical whether or not --gs-passive was given.
            del without["generated_at"], with_missing["generated_at"]
            self.assertEqual(without, with_missing)
            self.assertEqual(with_missing["instrument"], "probe")

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

    def test_effort_usd_passes_through_beside_effort(self):
        import json
        import tempfile
        now = datetime.now(timezone.utc)
        row = probe(5, "claude-sonnet-5", 420000)
        row["ts"] = now.isoformat()
        usd = {"claude-sonnet-5": {"low": 0.0355, "high": 0.12}, "_note": "dropped"}
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t, rows=[row], effort={**EFFORT, "usd": usd, "_meta": {"cells": {}}})
            self.assertEqual(self._run(d), 0)
            j = json.loads((d / "out.json").read_text())
            self.assertEqual(j["effort"], EFFORT)  # usd and _meta never leak in as models
            self.assertEqual(j["effort_usd"], {"claude-sonnet-5": {"low": 0.0355, "high": 0.12}})

    # Two of the seven live Sonnet low runs (gs, 2026-09-09): one turn and two turns.
    RUNS = {"claude-sonnet-5/low": [
        {"input": 2, "output": 1610, "cache_read": 19795, "cache_write": 0, "total": 21407},
        {"input": 4, "output": 1410, "cache_read": 39590, "cache_write": 877, "total": 41881},
        {"input": 2, "output": 1713, "cache_read": 19795, "cache_write": 0, "total": 21510},
    ]}

    def test_effort_usd_is_derived_from_stored_runs_when_the_file_has_no_usd(self):
        import json
        import tempfile
        from tracker.calibrate import recompute
        now = datetime.now(timezone.utc)
        row = probe(5, "claude-sonnet-5", 420000)
        row["ts"] = now.isoformat()
        # As committed: cells from the old code, runs recorded, no usd block.
        matrix = {"claude-sonnet-5": {"low": 999}, "_meta": {"runs": self.RUNS}}
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t, rows=[row], effort=matrix)
            self.assertEqual(self._run(d), 0)
            j = json.loads((d / "out.json").read_text())
            expect = recompute(json.loads(json.dumps(matrix)), PRICES)
            self.assertEqual(j["effort_usd"], expect["usd"])
            self.assertAlmostEqual(j["effort_usd"]["claude-sonnet-5"]["low"], 0.03164, places=6)  # (6 + 1713*15 + 19795*0.3) / 1e6, the middle run
            self.assertEqual(j["effort"], {"claude-sonnet-5": {"low": 21510}})  # re-derived, not the stale 999

    def test_price_change_between_publishes_changes_effort_usd_without_touching_the_matrix(self):
        import json
        import tempfile
        now = datetime.now(timezone.utc)
        row = probe(5, "claude-sonnet-5", 420000)
        row["ts"] = now.isoformat()
        matrix = {"claude-sonnet-5": {"low": 21510}, "usd": {"claude-sonnet-5": {"low": 0.1}},
                  "_meta": {"runs": self.RUNS}}
        heavier = {**PRICES, "claude-sonnet-5": {**PRICES["claude-sonnet-5"], "class_weight": {"output": 2.0}}}
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t, rows=[row], effort=matrix)
            before = (d / "effort.json").read_bytes()
            self.assertEqual(self._run(d), 0)
            first = json.loads((d / "out.json").read_text())["effort_usd"]["claude-sonnet-5"]["low"]
            (d / "prices.json").write_text(json.dumps(heavier))
            self.assertEqual(self._run(d), 0)
            second = json.loads((d / "out.json").read_text())["effort_usd"]["claude-sonnet-5"]["low"]
            self.assertNotEqual(first, 0.1)  # the stored usd block is never trusted
            self.assertGreater(second, first)
            self.assertEqual((d / "effort.json").read_bytes(), before)

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
        self.assertEqual(ww["probe"], {"current": None, "history": [], "by_window": []})
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
            (10.0, 50.0, 10.0, 16.0, "2026-09-04T03:59:59+00:00"),
            (10.0, 55.0, 10.0, 17.0, "2026-09-04T03:59:59+00:00"),
            (10.0, 70.0, 10.0, 20.0, "2026-09-11T03:59:59+00:00"),  # newest week, incomplete
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

    def test_max20_history_carries_the_partial_flag_from_probe_weekly_windows(self):
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
        by_week = {h["week_ending"]: h["partial"] for h in j["weekly_windows"]["max20"]["history"]}
        self.assertEqual(by_week, {"2026-08-28": False, "2026-09-04": False, "2026-09-11": True})

    def test_passive_partial_flags_are_recomputed_from_the_publish_time(self):
        # A lagging passive.json from before the flag existed: the open week is
        # flagged partial against the publisher's own `now`, the rest complete.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        # 09-04 carries a stale partial=True from a passive.json written while that week
        # was open; it must come out False against this publish's now.
        weekly = {"current": 6.46, "history": [dict(h) for h in self.PASSIVE_WEEKLY["history"][:-1]]
                  + [dict(self.PASSIVE_WEEKLY["history"][-1], partial=True)]
                  + [{"week_ending": "2026-09-11", "windows": 5.7, "five_hour_pct": 245.0, "seven_day_pct": 43.0}]}
        passive = dict(PASSIVE, weekly_windows=weekly)
        now = datetime(2026, 9, 9, 5, 30, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        for series in ("passive", "max5", "max20", "pro"):
            for h in j["weekly_windows"][series]["history"]:
                self.assertIn("partial", h, series)
                self.assertEqual(h["partial"], h["week_ending"] >= "2026-09-09", (series, h["week_ending"]))

    def test_no_weekly_event_on_real_max20_history(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        passive = dict(PASSIVE, weekly_windows=self.PASSIVE_WEEKLY)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])
        self.assertIsNone(j["last_change"])

    def test_calendar_week_rows_alone_never_produce_a_weekly_event(self):
        # A passive.json from before the per-window series existed: the weekly
        # rows still publish, but detection only runs on `by_window` -- the
        # calendar-week series blends a mid-week step away (issue #25), so it is
        # not detected on at all rather than detected on badly.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        weekly = {"current": 6.46, "history": [
            {"week_ending": "2026-08-28", "windows": 6.58, "five_hour_pct": 250.0, "seven_day_pct": 38.0},
            {"week_ending": "2026-09-04", "windows": 6.35, "five_hour_pct": 324.0, "seven_day_pct": 51.0},
            {"week_ending": "2026-09-11", "windows": 6.5, "five_hour_pct": 300.0, "seven_day_pct": 46.0},
            {"week_ending": "2026-09-18", "windows": 4.0, "five_hour_pct": 260.0, "seven_day_pct": 65.0},
            {"week_ending": "2026-09-25", "windows": 4.0, "five_hour_pct": 260.0, "seven_day_pct": 65.0},
        ]}
        passive = dict(PASSIVE, weekly_windows=weekly)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])
        self.assertIsNone(j["last_change"])
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 6.46)  # median of the last two complete weeks

    def test_issue_25_weekly_cut_is_published_from_the_data_as_it_stood_on_2026_09_15(self):
        # The real series: calendar weeks as published (the open 09-18 week
        # blending to 5.43) and the per-window points quoted in issue #25.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        passive = dict(PASSIVE, weekly_windows={"current": 6.18, "history": ISSUE_25_WEEKS, "by_window": ISSUE_25_BY_WINDOW})
        now = datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        weekly_change_events = [e for e in j["events"] if e.get("scope") == "weekly"]
        self.assertEqual(weekly_change_events, [
            {"date": "2026-09-14", "kind": "change", "scope": "weekly", "label": "Weekly limit changed -26%"}])
        self.assertEqual(j["last_change"], {"date": "2026-09-14", "direction": "decreased", "percent": 26,
                                            "model": "all", "scope": "weekly"})
        # `current` follows the post-cut level (81/18), not the two pre-cut weeks.
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 4.5)
        # The calendar-week rows are untouched: the chart still gets them.
        self.assertEqual([h["windows"] for h in j["weekly_windows"]["max20"]["history"]], [6.58, 6.35, 6.02, 5.43])
        self.assertEqual(j["weekly_windows"]["passive"]["by_window"], ISSUE_25_BY_WINDOW)

    def test_max20_current_is_the_pooled_trailing_fortnight_of_the_regime(self):
        # No change detected: current pools the last 14 days of per-window
        # points (anchored on the newest point), not the median of two weeks.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        by_window = ([window("2026-08-2%dT10:00:00+00:00" % d, 65.0, 10.0) for d in range(0, 6)]   # 6.5, >14 days old
                     + [window("2026-09-%02dT10:00:00+00:00" % d, 60.0, 10.0) for d in range(4, 15)])  # 6.0
        passive = dict(PASSIVE, weekly_windows={"current": 6.18, "history": ISSUE_25_WEEKS, "by_window": by_window})
        now = datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 6.0)

    def test_per_window_points_from_before_the_plan_change_stay_out_of_max20(self):
        # Max 5x windows (about 11) right up to PLAN_CHANGE, then Max 20x at 6.5:
        # the plan change is Jonathan's, not Anthropic's, and must not fire.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        by_window = ([window("2026-08-%02dT10:00:00+00:00" % d, 110.0, 10.0) for d in range(10, 19)]
                     + [window("2026-08-%02dT10:00:00+00:00" % d, 65.0, 10.0) for d in range(19, 31)])
        passive = dict(PASSIVE, weekly_windows={"current": 6.46, "history": self.PASSIVE_WEEKLY["history"],
                                                "by_window": by_window})
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 6.5)
        self.assertEqual(j["weekly_windows"]["max5"]["current"], 10.91)

    def test_probe_runs_per_window_points_are_published_but_stay_out_of_the_max20_series(self):
        # The real 2026-09-14/15 probe rows: each run moves the seven-day meter
        # by a point or none, so pooling them in only adds rounding (see
        # _max20_window_points). They publish under probe.by_window and change
        # neither the event nor current.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        for r, (fhb, fha, sdb, sda) in zip(rows[-3:], [(0.0, 5.0, 94.0, 95.0), (5.0, 9.0, 95.0, 95.0), (0.0, 5.0, 0.0, 1.0)]):
            r["five_hour_before"], r["five_hour_after"] = fhb, fha
            r["seven_day_before"], r["seven_day_after"] = sdb, sda
        passive = dict(PASSIVE, weekly_windows={"current": 6.18, "history": ISSUE_25_WEEKS, "by_window": ISSUE_25_BY_WINDOW})
        now = datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual([p["windows"] for p in j["weekly_windows"]["probe"]["by_window"]], [5.0, None, 5.0])
        self.assertEqual(j["last_change"]["percent"], 26)
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 4.5)

    def test_probe_runs_alone_cannot_set_current(self):
        # A passive.json without by_window plus the probe runs above: six points
        # of seven-day movement is rounding, not a level. current stays the
        # weekly rows' median rather than the runs' pooled 46/6.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        for r, (fhb, fha, sdb, sda) in zip(rows[-3:], [(0.0, 5.0, 94.0, 95.0), (5.0, 9.0, 95.0, 95.0), (0.0, 5.0, 0.0, 1.0)]):
            r["five_hour_before"], r["five_hour_after"] = fhb, fha
            r["seven_day_before"], r["seven_day_after"] = sdb, sda
        passive = dict(PASSIVE, weekly_windows={"current": 6.18, "history": ISSUE_25_WEEKS})
        now = datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 6.18)  # median of 6.35 and 6.02
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])


# weekly_windows() over the real ~/.moonlighter/usage_log.jsonl on masterrig as it
# stood on 2026-09-15: the weekly_windows block passive.json carries once masterrig
# runs the per-window code, every five-hour window from 2026-06-13.
REAL_WEEKLY = json.loads((Path(__file__).parent / "fixtures" / "passive_weekly_windows_2026-09-15.json")
                         .read_text(encoding="utf-8"))


class RealLogTests(unittest.TestCase):
    def _publish(self, until: str) -> dict:
        """The public JSON from the real log's windows ending before `until` (an ISO prefix)."""
        weekly = dict(REAL_WEEKLY, by_window=[w for w in REAL_WEEKLY["by_window"] if w["window_ending"] < until])
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        return build_public_json(rows, dict(PASSIVE, weekly_windows=weekly), EFFORT, PRICES,
                                 datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc))

    def test_the_real_log_publishes_one_weekly_change_the_14_sep_cut(self):
        # The acceptance test for the vote floor: through the real publish path,
        # the 84 max20 windows from 2026-08-19 to 09-15 give exactly one event.
        # At a floor of 3 they gave four: +38% 08-23 and +27% 09-03, each voted
        # by a d7=6 window, -23% 08-28, which only read as a candidate against
        # the base the false 08-23 regime left behind, and then this one.
        j = self._publish("2026-09-16")
        self.assertEqual([e for e in j["events"] if e["scope"] == "weekly"], [
            {"date": "2026-09-14", "kind": "change", "scope": "weekly", "label": "Weekly limit changed -29%"}])
        self.assertEqual(j["last_change"], {"date": "2026-09-14", "direction": "decreased", "percent": 29,
                                            "model": "all", "scope": "weekly"})

    def test_max20_current_follows_the_cut_from_the_publish_that_detects_it(self):
        # Before the second post-cut window lands nothing has fired, and current
        # is the trailing fortnight (6.23, mostly pre-cut). From the publish that
        # detects the cut it is the new regime's own level: 54/12 = 4.5 on 09-14,
        # 82/18 = 4.56 on 09-15, where the two-complete-weeks median (6.18) would
        # overstate the week's capacity by 36%.
        self.assertEqual(REAL_WEEKLY["current"], 6.18)
        for until, fired, current in [("2026-09-14T17", False, 6.23), ("2026-09-14T22", True, 4.5),
                                      ("2026-09-16", True, 4.56)]:
            with self.subTest(until=until):
                j = self._publish(until)
                self.assertEqual(j["last_change"] is not None, fired)
                self.assertEqual(j["weekly_windows"]["max20"]["current"], current)


class LastChangeScopeTests(unittest.TestCase):
    # The real 2026-09-13 cut (issue #25), which detects as a weekly event dated 2026-09-14.
    WEEKLY_MAX20 = {"current": 6.18, "history": ISSUE_25_WEEKS, "by_window": ISSUE_25_BY_WINDOW}

    def test_newer_weekly_event_beats_an_older_window_event(self):
        # Window event fires around 2026-09-05 (early Sept rows); the weekly
        # event is dated 2026-09-14, later, so it must win last_change.
        rows = ([probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
                + [probe(d, "claude-sonnet-5", 300000) for d in range(6, 10)])
        passive = dict(PASSIVE, weekly_windows=self.WEEKLY_MAX20)
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        window_events = [e for e in j["events"] if e.get("scope") == "window"]
        self.assertTrue(window_events)
        self.assertLess(window_events[-1]["date"], "2026-09-14")
        self.assertEqual(j["last_change"]["scope"], "weekly")
        self.assertEqual(j["last_change"]["date"], "2026-09-14")

    def test_older_weekly_event_loses_to_a_newer_window_event(self):
        # Same weekly step (dated 2026-09-14), but now the window event is
        # pushed later than it, past 2026-09-14, so the window event must win.
        rows = ([probe(d, "claude-sonnet-5", 420000) for d in range(1, 21)]
                + [probe(d, "claude-sonnet-5", 300000) for d in range(21, 26)])
        passive = dict(PASSIVE, weekly_windows=self.WEEKLY_MAX20)
        now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        window_events = [e for e in j["events"] if e.get("scope") == "window"]
        weekly_events = [e for e in j["events"] if e.get("scope") == "weekly"]
        self.assertTrue(window_events)
        self.assertTrue(weekly_events)
        self.assertGreater(window_events[-1]["date"], weekly_events[-1]["date"])
        self.assertEqual(j["last_change"]["scope"], "window")
        self.assertEqual(j["last_change"]["date"], window_events[-1]["date"])


class ContributedBlockTests(unittest.TestCase):
    """tracker.publish --contributed carries data/contributed.json through untouched."""

    def _files(self, d):
        import json
        from pathlib import Path
        d = Path(d)
        (d / "probes.jsonl").write_text(json.dumps(probe(5, "claude-sonnet-5", 420000)) + "\n")
        (d / "passive.json").write_text(json.dumps(PASSIVE))
        (d / "effort.json").write_text(json.dumps(EFFORT))
        (d / "prices.json").write_text(json.dumps(PRICES))
        return d

    def _run(self, d, *extra):
        from tracker.publish import main
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        return main(["--probes", str(d / "probes.jsonl"), "--passive", str(d / "passive.json"),
                     "--effort", str(d / "effort.json"), "--prices", str(d / "prices.json"),
                     "--out", str(d / "out.json"), *extra], now=now)

    def test_block_is_carried_through_unchanged(self):
        import json
        import tempfile
        block = {"updated_at": "2026-09-05T05:30:00+00:00",
                 "max20": {"contributors": 2, "samples": 9,
                           "tokens_per_pct": {"claude-sonnet-5": {"median": 410000, "spread": 0.2, "contributors": 2, "samples": 7}},
                           "usd_per_pct": {"claude-sonnet-5": {"median": 0.97, "spread": 0.1, "contributors": 2, "samples": 7}},
                           "weekly_windows": {"measured": 27.5, "reason": None, "contributors": 2, "dropped": 0, "weeks": 3}},
                 "max5": {"contributors": 0, "samples": 0, "tokens_per_pct": {}, "usd_per_pct": {},
                          "weekly_windows": {"measured": None, "reason": "no samples", "contributors": 0, "dropped": 0, "weeks": 0}},
                 "pro": {"contributors": 1, "samples": 3, "tokens_per_pct": {}, "usd_per_pct": {},
                         "weekly_windows": {"measured": None, "reason": "1 contributor with a complete week; 2 needed",
                                            "contributors": 1, "dropped": 0, "weeks": 1}}}
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t)
            (d / "contributed.json").write_text(json.dumps(block))
            self.assertEqual(self._run(d, "--contributed", str(d / "contributed.json")), 0)
            j = json.loads((d / "out.json").read_text())
            self.assertEqual(j["contributed"], block)
            # Nothing else moves: the same publish without the flag differs only by that key.
            self.assertEqual(self._run(d), 0)
            without = json.loads((d / "out.json").read_text())
            self.assertNotIn("contributed", without)
            j.pop("contributed")
            self.assertEqual(j, without)

    def test_block_is_omitted_when_the_file_is_absent(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t)
            self.assertEqual(self._run(d, "--contributed", str(d / "missing.json")), 0)
            self.assertNotIn("contributed", json.loads((d / "out.json").read_text()))

    def test_unreadable_block_is_a_warning_not_a_failed_publish(self):
        import io
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t)
            (d / "contributed.json").write_text("{not json")
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                rc = self._run(d, "--contributed", str(d / "contributed.json"))
            self.assertEqual(rc, 0)
            self.assertNotIn("contributed", json.loads((d / "out.json").read_text()))
            self.assertIn("contributed block not published", err.getvalue())
