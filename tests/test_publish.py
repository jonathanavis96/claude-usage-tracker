import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tracker.publish import (REFERENCE_MIX, blended_api_price_per_token, blended_price_per_token, build_public_json,
                             usd_per_pct)

def probe(day, model, tpp, account="dave"):
    return {"ts": f"2026-09-{day:02d}T08:00:00+00:00", "model": model, "effort": "low", "tokens_per_pct": tpp,
            "tokens": {"input": 100, "output": 400, "cache_read": tpp - 500, "cache_write": 0},
            "prompts": 8, "tick_from": 10, "tick_to": 11, "elapsed_s": 60, "account": account}

PASSIVE = {"generated_at": "2026-09-05T20:00:00+00:00", "plan_ratio_5x_to_20x": 0.25,
           "split": {"input": 0.062, "output": 0.021, "cache_read": 0.907, "cache_write": 0.010},
           "history": {"2026-08-01": {"tokens_per_pct": 100000, "interpolated": False}}}
EFFORT = {"claude-sonnet-5": {"low": 900000, "high": 2520000}}


def window(window_ending, d5, d7, pieces=1):
    """A passive.json per-window point as tracker/weekly.py writes it since the audit's finding-3 repair."""
    return {"window_ending": window_ending, "windows": round(d5 / d7, 2) if d7 else None,
            "five_hour_pct": d5, "seven_day_pct": d7, "pieces": pieces, "reset_verified": True}


def legacy_window(window_ending, d5, d7):
    """The same point as a passive.json from before that repair wrote it: no pieces, no reset flag."""
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
    legacy_window("2026-09-10T21:29:00+00:00", 72.0, 11.0),
    legacy_window("2026-09-12T16:30:00+00:00", 17.0, 3.0),
    legacy_window("2026-09-12T21:30:00+00:00", 27.0, 5.0),
    legacy_window("2026-09-14T16:30:00+00:00", 37.0, 8.0),
    legacy_window("2026-09-14T21:30:00+00:00", 17.0, 4.0),
    legacy_window("2026-09-15T16:30:00+00:00", 27.0, 6.0),
]
PRICES = {"claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
          "claude-opus-5": {"input": 7.5, "output": 37.5, "cache_read": 0.75, "cache_write": 9.375}}
# The same prices with data/prices.json's cache_read class weight of 0: where meter and API dollars part.
CACHE_READ_FREE = {m: {**p, "class_weight": {"cache_read": 0.0}} for m, p in PRICES.items()}


def gs_passive_report(*, account="dave", end, cache_write, delta_pct=10, model="claude-sonnet-5",
                      meter_last=None, reset_verified=True):
    """A minimal tracker.gs_passive.report() shape (issue #39): just enough for
    passive_dollar_readings to read one accepted stretch. `cache_write` tokens at
    PRICES' $3.75/Mtok give (cache_write * 3.75 / 1e6) meter dollars for the
    stretch; dividing by delta_pct and scaling to a full window is
    passive_dollar_readings' own job, not this fixture's. `reset_verified` is
    what a report from a reset-bearing meter log carries; False is the archived
    ceiling-log shape."""
    acct = {"account": account, "stretches": [
        {"status": "accepted", "end": end, "delta_pct": delta_pct, "windows": 1, "reset_verified": reset_verified,
         "tokens": {model: {"input": 0, "output": 0, "cache_read": 0, "cache_write": cache_write}}},
    ]}
    if meter_last is not None:
        acct["meter"] = {"last": meter_last}
    return {"accounts": {account: acct}}


def daily_report(budgets, *, start_day=1, account="dave", reset_verified=True):
    """One accepted stretch a day on 2026-09-(start_day + i), each worth budgets[i] meter dollars per window."""
    stretches = []
    for i, budget in enumerate(budgets):
        # 10% of a window at $3.75/Mtok cache_write: budget/10 dollars is budget/10/3.75e-6 tokens.
        stretches.append({"status": "accepted", "end": f"2026-09-{start_day + i:02d}T08:00:00+00:00",
                          "delta_pct": 10, "windows": 1, "reset_verified": reset_verified,
                          "tokens": {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0,
                                                         "cache_write": round(budget / 10 / 3.75e-6)}}})
    return {"accounts": {account: {"account": account, "stretches": stretches}}}


def tokens_for(budget, price):
    """tokens_per_window for a meter budget on the frozen reference mix (audit finding 1's formula)."""
    return round(budget / (blended_price_per_token(REFERENCE_MIX["split"], price) * price.get("meter_weight", 1.0)))


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
    """build_public_json on a passive series. Until 2026-09-16 these tests built the
    series from probe rows; probe rows no longer form it at all (audit finding 13),
    so each one now feeds the same intent through daily passive readings."""

    def test_shape(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, CACHE_READ_FREE, now, gs_passive=daily_report([15.0] * 5))
        self.assertEqual(j["schema_version"], 2)
        self.assertEqual(j["generated_at"], "2026-09-05T20:15:00+00:00")
        self.assertEqual(j["last_sample_at"], "2026-09-05T08:00:00+00:00")
        self.assertEqual(j["plan_measured"], "max20")
        # One window's published 1:5:20 scaling, labelled as such; nothing about windows per week.
        self.assertEqual(j["plan_ratios"], {"pro": 0.05, "max5": 0.25, "max20": 1.0})
        self.assertEqual(j["plan_ratios_basis"]["kind"], "published_plan_scaling")
        # The frozen cross-plan weekly ratio is gone: unmeasured is empty (audit finding 6).
        self.assertEqual(j["weekly_window_ratios"], {})
        self.assertEqual(j["rate_basis"], "meter_budget")
        r = j["rates"]["claude-sonnet-5"]
        self.assertEqual(r["meter_budget_per_window"], 15.0)
        self.assertEqual(r["tokens_per_window"], tokens_for(15.0, CACHE_READ_FREE["claude-sonnet-5"]))
        # API value is the list price of exactly those tokens, cache reads included (finding 1):
        # with cache reads free to the meter it is several times the budget.
        api = round(r["tokens_per_window"] * blended_api_price_per_token(REFERENCE_MIX["split"], CACHE_READ_FREE["claude-sonnet-5"]), 2)
        self.assertEqual((r["api_value_per_window"], r["api_list_value_per_window"]), (api, api))
        self.assertGreater(r["api_value_per_window"], r["meter_budget_per_window"])
        self.assertEqual(r["source"], "derived_reference_mix")
        self.assertEqual(r["split"], REFERENCE_MIX["split"])
        self.assertFalse(r["assumptions"]["direct_model_cap_measurement"])
        self.assertEqual((r["quality"]["status"], r["evidence"]["reset_verified"], r["freshness"]["stale"]),
                         ("conditional", True, False))
        self.assertEqual(j["effort"], EFFORT)
        self.assertEqual(j["api_price_per_mtok"], CACHE_READ_FREE)
        self.assertIsNone(j["last_change"])
        self.assertEqual(j["model_plan_limits"]["claude-sonnet-5"]["pro"]["included"], True)
        hist = j["history"]["claude-sonnet-5"]
        # History starts at the first reading: nothing held or backfilled before it (finding 12).
        self.assertEqual(hist[0], {"date": "2026-09-01", "meter_budget_per_window": 15.0,
                                   "tokens_per_window": r["tokens_per_window"],
                                   "api_value_per_window": r["api_value_per_window"],
                                   "api_list_value_per_window": r["api_value_per_window"],
                                   "source": "passive", "quality": "measured", "readings": 1, "interpolated": False})
        self.assertEqual(len(hist), 5)

    def test_history_days_carry_the_days_meter_budget_for_every_model(self):
        # Every history day carries the meter budget, one figure per day and the same for
        # every model; the API list value is a different unit, the list price of the tokens
        # that budget converts to (finding 1), and with cache reads free to the meter it is
        # the larger figure. Days are the day's readings, not a held regime level
        # (finding 5): a step shows where it happened and nowhere else.
        budgets = [15.0] * 10 + [10.5] * 5
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, CACHE_READ_FREE, now, gs_passive=daily_report(budgets))
        event_date = j["last_change"]["date"]
        sonnet, opus = j["history"]["claude-sonnet-5"], j["history"]["claude-opus-5"]
        self.assertEqual([h["meter_budget_per_window"] for h in sonnet], [h["meter_budget_per_window"] for h in opus])
        self.assertEqual({h["meter_budget_per_window"] for h in sonnet if h["date"] < event_date}, {15.0})
        self.assertEqual({h["meter_budget_per_window"] for h in sonnet if h["date"] >= event_date}, {10.5})
        self.assertTrue(all(h["api_value_per_window"] > h["meter_budget_per_window"] for h in sonnet + opus))
        self.assertEqual(sonnet[-1]["meter_budget_per_window"], j["rates"]["claude-sonnet-5"]["meter_budget_per_window"])
        self.assertEqual(j["rates"]["claude-opus-5"]["meter_budget_per_window"], 10.5)

    def test_every_priced_model_gets_a_rate_derived_from_the_one_measured_budget(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=daily_report([15.0] * 5))
        self.assertEqual(set(j["rates"]), {"claude-sonnet-5", "claude-opus-5"})
        sonnet, opus = j["rates"]["claude-sonnet-5"], j["rates"]["claude-opus-5"]
        # the same meter budget, a different price -> different tokens_per_window
        self.assertEqual(sonnet["meter_budget_per_window"], opus["meter_budget_per_window"])
        self.assertEqual((sonnet["source"], opus["source"]), ("derived_reference_mix", "derived_reference_mix"))
        # probed_at still names each model's own latest probe row, and nothing more.
        self.assertIsNone(opus["probed_at"])
        self.assertEqual(sonnet["probed_at"], "2026-09-05T08:00:00+00:00")
        self.assertEqual(opus["tokens_per_window"], tokens_for(15.0, PRICES["claude-opus-5"]))
        # opus is priced ~2.5x sonnet across the board, so its derived rate is ~2.5x fewer tokens
        self.assertLess(opus["tokens_per_window"], sonnet["tokens_per_window"])

    def test_history_has_no_probe_days_whichever_model_was_probed(self):
        # There are no probe days any more: a probe row adds no history day and no day
        # reads "probe" or "derived", whichever model was probed (finding 13).
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=daily_report([15.0] * 2, start_day=4))
        for model in ("claude-sonnet-5", "claude-opus-5"):
            self.assertEqual([(h["date"], h["source"]) for h in j["history"][model]],
                             [("2026-09-04", "passive"), ("2026-09-05", "passive")])

    def test_each_probed_model_publishes_its_own_probed_at(self):
        # All three models were probed in rotation: each still publishes its own
        # latest probe row as probed_at, and none of them is the rate's source.
        prices = {**PRICES, "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25,
                                                  "cache_write": 12.5, "meter_weight": 2.0}}
        rows = [probe(1, "claude-sonnet-5", 420000), probe(2, "claude-opus-5", 420000),
                probe(3, "claude-fable-5-1", 420000), probe(4, "claude-sonnet-5", 420000)]
        now = datetime(2026, 9, 4, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, prices, now, gs_passive=daily_report([15.0] * 4))
        for model, expected_ts in [("claude-sonnet-5", "2026-09-04T08:00:00+00:00"),
                                    ("claude-opus-5", "2026-09-02T08:00:00+00:00"),
                                    ("claude-fable-5-1", "2026-09-03T08:00:00+00:00")]:
            self.assertEqual(j["rates"][model]["source"], "derived_reference_mix", model)
            self.assertEqual(j["rates"][model]["probed_at"], expected_ts, model)

    def test_probe_account_count_counts_distinct_accounts_without_naming_them(self):
        rows = [probe(1, "claude-sonnet-5", 420000, account="dave"),
                probe(2, "claude-sonnet-5", 420000, account="jwork"),
                dict(probe(3, "claude-sonnet-5", 420000, account="dave"), outlier=True)]
        now = datetime(2026, 9, 2, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=daily_report([15.0] * 2))
        self.assertEqual(j["probe_account_count"], 2)
        self.assertEqual(j["passive_account_count"], 1)
        self.assertNotIn("dave", json.dumps(j))
        self.assertNotIn("jwork", json.dumps(j))

    def test_model_with_only_an_outlier_row_publishes_derived_budget_with_null_probed_at(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        rows.append(dict(probe(5, "claude-opus-5", 420000), outlier=True))
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=daily_report([15.0] * 5))
        self.assertEqual(j["rates"]["claude-opus-5"]["source"], "derived_reference_mix")
        self.assertIsNone(j["rates"]["claude-opus-5"]["probed_at"])

    def test_outlier_rows_are_skipped_everywhere(self):
        # The rotation's drift check flags a lone outlier in place (tracker.rotate); it
        # stays in history/probes.jsonl but the publisher never sees it: not as a probed_at,
        # not as the probe effort, and it cannot touch the passive rates or history.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        rows.append(dict(probe(7, "claude-sonnet-5", 900000), outlier=True, effort="high"))
        now = datetime(2026, 9, 7, 20, 15, tzinfo=timezone.utc)
        report = daily_report([15.0] * 7)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=report)
        clean = build_public_json(rows[:-1], PASSIVE, EFFORT, PRICES, now, gs_passive=report)
        self.assertEqual(j["rates"], clean["rates"])
        self.assertEqual(j["history"], clean["history"])
        self.assertEqual(j["rates"]["claude-sonnet-5"]["probed_at"], "2026-09-05T08:00:00+00:00")
        self.assertEqual(j["rates"]["claude-sonnet-5"]["probe_effort"], "low")
        self.assertIsNone(j["last_change"])

    def test_only_outlier_rows_publishes_unavailable(self):
        # Probe rows alone (outliers or not) are no measurement: the publish no longer
        # refuses, it states that the rates are unavailable (finding 13).
        rows = [dict(probe(5, "claude-sonnet-5", 420000), outlier=True)]
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 5, 20, tzinfo=timezone.utc))
        self.assertEqual(j["instrument"], "unavailable")
        self.assertEqual(j["availability"], {"rates": "unavailable", "evidence": "unavailable",
                                             "reason": "no_eligible_passive_measurement"})
        for r in j["rates"].values():
            self.assertEqual((r["meter_budget_per_window"], r["tokens_per_window"], r["api_value_per_window"],
                              r["source"]), (None, None, None, "unavailable"))
        self.assertEqual(j["history"], {"claude-sonnet-5": [], "claude-opus-5": []})

    def test_shape_token_figures_stay_on_the_frozen_reference_mix(self):
        # Token figures are always on the frozen reference mix (finding 11): neither an
        # empty passive.json split nor a probe row's own class split changes them.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        report = daily_report([15.0] * 5)
        j = build_public_json(rows, {}, EFFORT, PRICES, now, gs_passive=report)
        with_split = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=report)
        self.assertEqual(j["rates"], with_split["rates"])
        self.assertEqual(j["rates"]["claude-opus-5"]["split"], REFERENCE_MIX["split"])
        self.assertEqual(j["rates"]["claude-opus-5"]["tokens_per_window"], tokens_for(15.0, PRICES["claude-opus-5"]))

    def test_fable_and_sonnet_days_at_same_meter_value_yield_same_meter_budget(self):
        # Two passive days of the same meter dollars, one spent on Sonnet and one on Fable
        # (whose meter_weight is 2): the meter budget is the same unit whichever model
        # spent it, so both days publish the same budget.
        prices = {**PRICES, "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25,
                                                  "cache_write": 12.5, "meter_weight": 2.0}}
        report = daily_report([15.0, 15.0])
        fable_day = report["accounts"]["dave"]["stretches"][1]
        # 15.0 per window is $1.30 of meter dollars over 10%: at $12.5/Mtok x 2 that is 52,000 tokens.
        fable_day["tokens"] = {"claude-fable-5-1": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 60_000}}
        now = datetime(2026, 9, 2, 20, 15, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, prices, now, gs_passive=report)
        self.assertEqual([h["meter_budget_per_window"] for h in j["history"]["claude-sonnet-5"]], [15.0, 15.0])

    def test_fable_derived_tokens_are_half_what_they_would_be_at_weight_one(self):
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        report = daily_report([15.0] * 5)
        fable_price = {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 12.5}
        prices_weight_one = {**PRICES, "claude-fable-5-1": fable_price}
        prices_weight_two = {**PRICES, "claude-fable-5-1": {**fable_price, "meter_weight": 2.0}}
        j1 = build_public_json([], PASSIVE, EFFORT, prices_weight_one, now, gs_passive=report)
        j2 = build_public_json([], PASSIVE, EFFORT, prices_weight_two, now, gs_passive=report)
        self.assertAlmostEqual(j2["rates"]["claude-fable-5-1"]["tokens_per_window"],
                                j1["rates"]["claude-fable-5-1"]["tokens_per_window"] / 2, delta=1)

    def test_change_event_surfaces(self):
        j = build_public_json([], PASSIVE, EFFORT, PRICES, datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
                              gs_passive=daily_report([15.0] * 10 + [10.5] * 5))
        c = j["last_change"]
        self.assertEqual((c["direction"], c["model"], c["scope"], c["metric"]),
                         ("decreased", "all", "window", "meter_budget_per_window"))
        self.assertIn(c["percent"], range(20, 40))
        # An observed change in this account's metric, provisional: the daily readings
        # carry no uncertainty model (findings 4 and 5).
        self.assertEqual((c["attribution"], c["observation_scope"], c["provisional"]),
                         ("observed_account_metric_change", "account", True))
        self.assertEqual(c["onset"], {"earliest": "2026-09-10", "latest": "2026-09-11"})

    def test_a_reset_less_series_publishes_no_change_event(self):
        # The same step from a log with no reset ids is a conditional reference only (finding 10).
        j = build_public_json([], PASSIVE, EFFORT, PRICES, datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
                              gs_passive=daily_report([15.0] * 10 + [10.5] * 5, reset_verified=False))
        self.assertIsNone(j["last_change"])
        self.assertEqual(j["events"], [])
        r = j["rates"]["claude-sonnet-5"]
        self.assertEqual((r["quality"]["status"], r["evidence"]["reset_verified"]), ("conditional", False))
        self.assertIn("legacy_reset_metadata_missing", r["quality"]["reasons"])
        self.assertEqual({h["quality"] for h in j["history"]["claude-sonnet-5"]}, {"legacy_reset_unverified"})

    def test_events_excludes_the_plan_change(self):
        # Jonathan's ruling: the public chart is a step function of the measured limit
        # only -- nothing about his own plan history belongs in it.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual([e for e in j["events"] if e["kind"] == "plan"], [])

    def test_change_produces_exactly_two_flat_levels_stepping_on_the_event_date(self):
        # With readings that are themselves flat either side of the step, the daily history
        # is two flat levels meeting on the event date. (History is the readings, not held
        # regime levels, since finding 5; flat inputs are what make it flat here.)
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now, gs_passive=daily_report([15.0] * 10 + [10.5] * 5))
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
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now, gs_passive=daily_report([15.0] * 10 + [10.5] * 5))
        change_events = [e for e in j["events"] if e["kind"] == "change"]
        self.assertTrue(change_events)
        self.assertEqual(change_events[-1]["date"], j["last_change"]["date"])
        self.assertEqual(change_events[-1]["label"],
                          f"Observed window budget changed -{j['last_change']['percent']}%")


class GsPassiveTests(unittest.TestCase):
    """Passive readings from tracker.gs_passive (issue #39) are the published series
    on their own since 2026-09-16; probe rows never join them (the scales differ), and
    since the 2026-09-16 audit (finding 13) never stand in for them either."""

    def test_passive_readings_are_the_series_and_probe_rows_only_date_their_own_runs(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=GS_PASSIVE_MATCHING_PROBE)
        self.assertEqual(j["instrument"], "passive")
        self.assertNotIn("passive_calibration", j)
        self.assertEqual(j["last_sample_at"], "2026-09-06T08:00:00+00:00")
        self.assertEqual(j["passive_account_count"], 1)
        r = j["rates"]["claude-sonnet-5"]
        self.assertEqual(r["source"], "derived_reference_mix")
        self.assertEqual(r["measured_at"], "2026-09-06T08:00:00+00:00")
        # probed_at is unchanged by a passive reading: still this model's own latest probe row.
        self.assertEqual(r["probed_at"], "2026-09-05T08:00:00+00:00")
        self.assertEqual(j["rates"]["claude-opus-5"]["source"], "derived_reference_mix")
        # The budget is the passive stretch's own: $1.3215 over 10%, x100 = 13.215 per window.
        self.assertAlmostEqual(r["meter_budget_per_window"], 13.22, delta=0.01)
        # The class split is the frozen reference mix (finding 11), not any account's live one.
        self.assertEqual(r["split"], REFERENCE_MIX["split"])
        # Probe days are not part of the series and nothing is held before the first
        # passive day (finding 12): the history is that one day.
        self.assertEqual([(h["date"], h["source"]) for h in j["history"]["claude-sonnet-5"]],
                         [("2026-09-06", "passive")])

    def test_a_passive_series_far_from_the_probe_level_fires_no_change_event(self):
        # The whole point of not joining: a probe steady at 97 next to passive days
        # at 150 used to read as a +55% step. Now the probe rows are not in the series.
        prices = {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 1.0}}

        def steady_probe(day):
            return {"ts": f"2026-09-{day:02d}T08:00:00+00:00", "model": "claude-sonnet-5", "effort": "low",
                    "tokens_per_pct": 970_000, "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 970_000},
                    "prompts": 8, "tick_from": 10, "tick_to": 11, "elapsed_s": 60, "account": "dave"}

        rows = [steady_probe(d) for d in range(1, 6)]
        raw_passive = gs_passive_report(end="2026-09-06T08:00:00+00:00", cache_write=150_000_000, delta_pct=100)
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, {"split": {"cache_write": 1.0}}, EFFORT, prices, now, gs_passive=raw_passive)
        self.assertIsNone(j["last_change"])
        self.assertEqual(j["instrument"], "passive")
        self.assertAlmostEqual(j["rates"]["claude-sonnet-5"]["meter_budget_per_window"], 150.0, delta=1e-6)
        self.assertAlmostEqual(j["rates"]["claude-sonnet-5"]["api_value_per_window"], 150.0, delta=1e-6)

    def test_no_gs_passive_publishes_unavailable_rates_not_the_probe_series(self):
        # Audit finding 13: without the passive report the probe series used to be
        # published in its place, on another scale (-22% on the 2026-09-16 data with no
        # limit change). Now the rates say they are unavailable, and why.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(j["instrument"], "unavailable")
        r = j["rates"]["claude-sonnet-5"]
        self.assertEqual((r["source"], r["meter_budget_per_window"], r["tokens_per_window"]), ("unavailable", None, None))
        self.assertEqual(r["quality"]["reasons"], ["no_eligible_passive_measurement"])
        self.assertEqual(r["probed_at"], "2026-09-05T08:00:00+00:00")
        self.assertEqual(j["passive_account_count"], 0)

    def test_a_gs_passive_report_with_no_priced_reading_is_the_same_as_no_report(self):
        # ...and a report whose only stretch is unpriced is the same as no report at all.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        empty = {"accounts": {"jwork": {"stretches": [{"status": "unpriced", "end": "2026-09-06T08:00:00+00:00",
                                                        "delta_pct": 10, "tokens": {}}]}}}
        without = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=empty)
        self.assertEqual(j["instrument"], "unavailable")
        self.assertEqual(j["passive_account_count"], 0)
        self.assertEqual(j, without)

    def test_freshness_follows_a_fresh_passive_reading_when_probes_are_stale(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]  # 2026-09-01..05
        now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)  # 21 days past the last probe
        fresh_passive = gs_passive_report(end="2026-09-25T08:00:00+00:00", cache_write=352400)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=fresh_passive)
        self.assertEqual(j["instrument"], "passive")
        self.assertEqual(j["last_sample_at"], "2026-09-25T08:00:00+00:00")
        self.assertFalse(j["rates"]["claude-sonnet-5"]["freshness"]["stale"])

    def test_a_stale_passive_series_publishes_as_stale_even_with_a_fresh_probe_row(self):
        # Freshness reads the series that is published. A probe row run by hand yesterday
        # does not freshen a passive series that stopped three weeks ago. The publish no
        # longer refuses: the rate carries its own staleness, apart from generated_at,
        # which only says the build ran (audit finding 16).
        rows = [probe(25, "claude-sonnet-5", 420000)]
        now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        stale_passive = gs_passive_report(end="2026-09-05T08:00:00+00:00", cache_write=352400)
        r = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=stale_passive)["rates"]["claude-sonnet-5"]
        self.assertEqual(r["freshness"], {"as_of": "2026-09-05T08:00:00+00:00",
                                          "stale_after": "2026-09-15T08:00:00+00:00", "stale": True})
        self.assertIn("evidence_stale", r["quality"]["reasons"])

    def test_publishing_works_with_zero_probe_rows(self):
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now, gs_passive=GS_PASSIVE_MATCHING_PROBE)
        self.assertEqual(j["instrument"], "passive")
        self.assertEqual(j["probe_account_count"], 0)
        self.assertEqual(j["passive_account_count"], 1)
        r = j["rates"]["claude-sonnet-5"]
        self.assertEqual(r["source"], "derived_reference_mix")
        self.assertIsNone(r["probed_at"])
        self.assertIsNone(r["probe_effort"])

    def test_zero_probe_rows_and_no_split_anywhere_is_cleanly_unavailable(self):
        # No probe rows, a passive.json with no "split", and a report whose one stretch
        # carries no tokens: nothing to divide by zero any more (the mix is frozen), and a
        # stretch with no captured work is a collection gap, not a free window, so there is
        # no reading and the rates are cleanly unavailable rather than $0.
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        passive_without_split = {k: v for k, v in PASSIVE.items() if k != "split"}
        tokenless = {"accounts": {"dave": {"stretches": [
            {"status": "accepted", "end": "2026-09-06T08:00:00+00:00", "delta_pct": 10, "tokens": {},
             "reset_verified": True}]}}}
        j = build_public_json([], passive_without_split, EFFORT, PRICES, now, gs_passive=tokenless)
        self.assertEqual(j["instrument"], "unavailable")
        self.assertIsNone(j["rates"]["claude-sonnet-5"]["meter_budget_per_window"])

    def test_no_readings_at_all_publishes_unavailable_and_no_prices_refuses(self):
        # No readings is a published "unavailable", not a refusal (finding 13); an empty
        # price table is still refused, since there is nothing to publish at all.
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now)
        self.assertEqual((j["instrument"], j["last_sample_at"], j["history"]["claude-sonnet-5"]),
                         ("unavailable", None, []))
        with self.assertRaises(ValueError):
            build_public_json([], PASSIVE, EFFORT, {}, now)

    def test_a_passive_series_uses_the_smoothed_detector(self):
        # Nine passive days scattering +-20% around one level fire nothing (detect_changes
        # would have fired on the 4th and 5th); a sustained week 40% higher does.
        def rpt(values, start_day=1):
            return {"accounts": {"dave": {"stretches": [
                {"status": "accepted", "end": f"2026-09-{start_day + i:02d}T08:00:00+00:00", "delta_pct": 10,
                 "reset_verified": True,
                 "tokens": {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0, "cache_write": int(v)}}}
                for i, v in enumerate(values)]}}}
        noisy = [400_000, 380_000, 320_000, 470_000, 480_000, 420_000, 400_000, 360_000, 300_000]
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        j = build_public_json([], {"split": {"cache_write": 1.0}}, EFFORT, PRICES, now, gs_passive=rpt(noisy))
        self.assertIsNone(j["last_change"])
        stepped = [400_000] * 8 + [560_000] * 8
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
        j = build_public_json([], {"split": {"cache_write": 1.0}}, EFFORT, PRICES, now, gs_passive=rpt(stepped))
        self.assertIsNotNone(j["last_change"])
        self.assertEqual(j["last_change"]["date"], "2026-09-09")

    def test_passive_account_count_only_counts_accounts_with_an_accepted_stretch(self):
        report = {"accounts": {
            "dave": GS_PASSIVE_MATCHING_PROBE["accounts"]["dave"],
            "jwork": {"stretches": [{"status": "unpriced", "end": "2026-09-06T08:00:00+00:00",
                                     "delta_pct": 5, "tokens": {}}]},
        }}
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=report)
        self.assertEqual(j["passive_account_count"], 1)


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
            self.assertEqual(with_missing["instrument"], "unavailable")

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
        # An empty price table has no rates to publish at all: refused.
        with self.assertRaises(ValueError):
            build_public_json([], PASSIVE, EFFORT, {}, datetime(2026, 9, 5, tzinfo=timezone.utc))

    def test_meter_read_at_is_the_newest_meter_sample_not_the_newest_measurement(self):
        # The meter is read every few minutes; a stretch closes at an idle bound or a
        # window end, so the figure derived from it is routinely hours older. The page
        # pill says "Last sample", which means the reading.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        report = gs_passive_report(end="2026-09-06T08:00:00+00:00", cache_write=352400,
                                   meter_last="2026-09-06T19:55:00+00:00")
        report["accounts"]["jwork"] = {"stretches": [], "meter": {"last": "2026-09-06T19:50:00+00:00"}}
        now = datetime(2026, 9, 6, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now, gs_passive=report)
        self.assertEqual(j["meter_read_at"], "2026-09-06T19:55:00+00:00")
        self.assertEqual(j["last_sample_at"], "2026-09-06T08:00:00+00:00")

    def test_meter_read_at_is_null_without_a_gs_passive_report(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc))
        self.assertIsNone(j["meter_read_at"])

    def test_stale_last_sample_publishes_as_stale(self):
        # A newest reading older than MAX_SAMPLE_AGE_DAYS publishes as stale rather than
        # refusing (finding 16): the reader sees the figure's own date and status.
        j = build_public_json([], PASSIVE, EFFORT, PRICES, datetime(2026, 9, 20, tzinfo=timezone.utc),
                              gs_passive=daily_report([15.0] * 5))
        self.assertTrue(j["rates"]["claude-sonnet-5"]["freshness"]["stale"])
        self.assertEqual(j["last_sample_at"], "2026-09-05T08:00:00+00:00")

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

    def test_absent_from_passive_publishes_every_plan_unavailable(self):
        # The contract publishes weekly_windows always, each plan saying why it has no
        # figure, so a consumer never infers "unmeasured" from a missing key.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        ww = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)["weekly_windows"]
        self.assertEqual({p: (ww[p]["current"], ww[p]["availability"]) for p in ("max20", "max5", "pro")}, {
            "max20": (None, {"status": "unavailable", "reason": "no_paired_meter_data"}),
            "max5": (None, {"status": "historical_only", "reason": "no_current_max5_measurement"}),
            "pro": (None, {"status": "unavailable", "reason": "no_pro_measurement"})})

    def test_passive_weeks_are_split_by_plan_and_the_straddling_week_is_dropped(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        passive = dict(PASSIVE, weekly_windows=self.PASSIVE_WEEKLY)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        ww = j["weekly_windows"]
        self.assertEqual([h["week_ending"] for h in ww["max5"]["history"]], ["2026-08-14"])
        self.assertEqual([h["week_ending"] for h in ww["max20"]["history"]], ["2026-08-28", "2026-09-04"])
        self.assertEqual(ww["passive"]["history"], [dict(h, partial=False) for h in self.PASSIVE_WEEKLY["history"]])
        self.assertEqual(ww["probe"], {"current": None, "history": [], "by_window": []})
        # passive.json's own two-weeks median is not a current value (audit finding 6), and
        # weeks paired before the finding-3 repair are published as legacy evidence.
        self.assertIsNone(ww["max20"]["current"])
        self.assertEqual({(h["quality"], tuple(h["reasons"])) for h in ww["max20"]["history"]},
                         {("legacy_uncertain", ("paired_before_denominator_repair",))})
        self.assertFalse(ww["max20"]["assumed"])
        self.assertFalse(ww["max5"]["assumed"])

    def test_pro_publishes_nothing_borrowed_from_max5(self):
        # Not any more (finding 6): Pro has no measurement of its own, so it publishes
        # nothing borrowed from Max 5x, and Max 5x has no current figure either.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        passive = dict(PASSIVE, weekly_windows=self.PASSIVE_WEEKLY)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        ww = build_public_json(rows, passive, EFFORT, PRICES, now)["weekly_windows"]
        self.assertEqual((ww["pro"]["current"], ww["pro"]["history"], ww["pro"]["regimes"], ww["pro"]["assumed"]),
                         (None, [], [], False))
        self.assertIsNone(ww["max5"]["current"])
        self.assertTrue(ww["max5"]["history"])

    def test_probe_weeks_publish_beside_passive_max20_weeks_not_in_place_of_them(self):
        # Not any more (finding 13): the probe runs are another instrument, published as
        # their own weekly series, and the passive max20 weeks stay what they are.
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
        self.assertEqual({h["source"] for h in ww["probe"]["history"]}, {"probe_paired_deltas"})
        self.assertEqual([h["week_ending"] for h in ww["max20"]["history"]], ["2026-08-28", "2026-09-04"])
        self.assertEqual({h["source"] for h in ww["max20"]["history"]}, {"passive_paired_deltas"})
        self.assertIsNone(ww["max20"]["current"])
        # max5 is untouched by any of this -- it is frozen passive-era history.
        self.assertEqual([h["week_ending"] for h in ww["max5"]["history"]], ["2026-08-14"])

    def test_probe_history_carries_the_partial_flag_from_probe_weekly_windows(self):
        # The probe series carries probe_weekly_windows' own partial flags, now under
        # weekly_windows.probe rather than spliced into max20.
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
        by_week = {h["week_ending"]: h["partial"] for h in j["weekly_windows"]["probe"]["history"]}
        self.assertEqual(by_week, {"2026-08-28": False, "2026-09-04": False, "2026-09-11": True})
        self.assertEqual({h["week_ending"]: h["partial"] for h in j["weekly_windows"]["max20"]["history"]},
                         {"2026-08-28": False, "2026-09-04": False})

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
        # A passive.json with weekly rows but no per-window series: the rows still
        # publish, but detection only runs on window points -- the calendar-week series
        # blends a mid-week step away (issue #25) -- and a median of two calendar weeks
        # is no longer published as current either (finding 6).
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
        self.assertIsNone(j["weekly_windows"]["max20"]["current"])
        self.assertEqual(len(j["weekly_windows"]["max20"]["history"]), 5)

    def test_issue_25_windows_as_they_stood_on_2026_09_15_publish_no_uncertified_cut(self):
        # The six per-window points quoted in issue #25, in two shapes. As the archived
        # passive.json carries them (paired before the finding-3 repair) nothing is
        # computed from them: max20 has no current and the reason says why. As repaired
        # points they are too few to certify the cut (finding 4; the whole real series
        # does, RealLogTests), and current pools all six, since no change split them.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        now = datetime(2026, 9, 15, 17, 30, tzinfo=timezone.utc)
        legacy = dict(PASSIVE, weekly_windows={"current": 6.18, "history": ISSUE_25_WEEKS, "by_window": ISSUE_25_BY_WINDOW})
        j = build_public_json(rows, legacy, EFFORT, PRICES, now)
        self.assertEqual(j["events"], [])
        self.assertIsNone(j["weekly_windows"]["max20"]["current"])
        self.assertEqual(j["weekly_windows"]["max20"]["availability"],
                         {"status": "unavailable", "reason": "passive_weekly_windows_predate_paired_delta_repair"})
        # The calendar-week rows are untouched: the chart still gets them.
        self.assertEqual([h["windows"] for h in j["weekly_windows"]["max20"]["history"]], [6.58, 6.35, 6.02, 5.43])
        self.assertEqual(j["weekly_windows"]["passive"]["by_window"], ISSUE_25_BY_WINDOW)
        repaired = [dict(w, pieces=1, reset_verified=True) for w in ISSUE_25_BY_WINDOW]
        j = build_public_json(rows, dict(PASSIVE, weekly_windows={"current": 6.18, "history": ISSUE_25_WEEKS,
                                                                  "by_window": repaired}), EFFORT, PRICES, now)
        self.assertIsNone(j["last_change"])
        estimate = j["weekly_windows"]["max20"]["current_estimate"]
        self.assertEqual((estimate["value"], estimate["five_hour_pct"], estimate["seven_day_pct"], estimate["pieces"]),
                         (5.32, 197.0, 37.0, 6))
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 5.32)

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
        estimate = j["weekly_windows"]["max20"]["current_estimate"]
        self.assertEqual((estimate["points"], estimate["seven_day_pct"], estimate["stale"]), (11, 110.0, False))
        lo, hi = estimate["rounding_interval"]
        self.assertLess(lo, 6.0)
        self.assertGreater(hi, 6.0)
        # A weekly log that stops arriving is stale against the publish time, not its own newest point.
        later = build_public_json(rows, passive, EFFORT, PRICES, datetime(2026, 10, 15, tzinfo=timezone.utc))
        self.assertTrue(later["weekly_windows"]["max20"]["current_estimate"]["stale"])
        self.assertEqual(later["weekly_windows"]["max20"]["availability"], {"status": "measured", "reason": "evidence_stale"})

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
        # Max 5x keeps its measured level as history (a regime), never as current (finding 6).
        self.assertIsNone(j["weekly_windows"]["max5"]["current"])
        self.assertEqual([r["windows"] for r in j["weekly_windows"]["max5"]["regimes"]], [11.0])
        self.assertFalse(j["weekly_windows"]["max5"]["plan_change"]["independently_verified"])

    def test_probe_runs_per_window_points_are_published_but_stay_out_of_the_max20_series(self):
        # The real 2026-09-14/15 probe rows: each run moves the seven-day meter
        # by a point or none, so pooling them in only adds rounding (see
        # _max20_window_points). They publish under probe.by_window and change
        # nothing in max20.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        repaired = [dict(w, pieces=1, reset_verified=True) for w in ISSUE_25_BY_WINDOW]
        passive = dict(PASSIVE, weekly_windows={"current": 6.18, "history": ISSUE_25_WEEKS, "by_window": repaired})
        now = datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc)
        without = build_public_json(rows, passive, EFFORT, PRICES, now)
        for r, (fhb, fha, sdb, sda) in zip(rows[-3:], [(0.0, 5.0, 94.0, 95.0), (5.0, 9.0, 95.0, 95.0), (0.0, 5.0, 0.0, 1.0)]):
            r["five_hour_before"], r["five_hour_after"] = fhb, fha
            r["seven_day_before"], r["seven_day_after"] = sdb, sda
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual([p["windows"] for p in j["weekly_windows"]["probe"]["by_window"]], [5.0, None, 5.0])
        self.assertEqual(j["weekly_windows"]["max20"], without["weekly_windows"]["max20"])
        self.assertEqual(j["events"], without["events"])

    def test_probe_runs_alone_cannot_set_current(self):
        # A passive.json without by_window plus the probe runs above: six points
        # of seven-day movement is rounding, not a level. There is no max20 current
        # at all -- the weekly rows' median is not one either (finding 6).
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        for r, (fhb, fha, sdb, sda) in zip(rows[-3:], [(0.0, 5.0, 94.0, 95.0), (5.0, 9.0, 95.0, 95.0), (0.0, 5.0, 0.0, 1.0)]):
            r["five_hour_before"], r["five_hour_after"] = fhb, fha
            r["seven_day_before"], r["seven_day_after"] = sdb, sda
        passive = dict(PASSIVE, weekly_windows={"current": 6.18, "history": ISSUE_25_WEEKS})
        now = datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertIsNone(j["weekly_windows"]["max20"]["current"])
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])


# weekly_windows() over the real ~/.moonlighter/usage_log.jsonl on masterrig as it
# stood on 2026-09-15, paired as tracker/weekly.py pairs since the audit's finding-3
# repair: every five-hour window from 2026-06-13.
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
        # Through the real publish path, the whole max20 series up to the evening of
        # 2026-09-14 gives exactly one event: the cut, -31%, dated by its first window,
        # certified against both levels' rounding intervals, and published as an observed
        # change in this account's weekly/window ratio. A day later the same cut sits
        # just inside the method's resolution and publishes no event (tracker/detect.py,
        # why ROUNDING_Z is 2); and nothing before the cut ever fires.
        j = self._publish("2026-09-14T22")
        self.assertEqual([(e["date"], e["percent"], e["label"]) for e in j["events"]],
                         [("2026-09-14", 31, "Observed weekly/window ratio changed -31%")])
        c = j["last_change"]
        self.assertEqual((c["scope"], c["direction"], c["metric"], c["attribution"], c["provisional"]),
                         ("weekly", "decreased", "weekly_to_five_hour_ratio", "observed_account_metric_change", False))
        self.assertEqual((c["onset"], c["confirmation"]),
                         ({"earliest": "2026-09-13", "latest": "2026-09-14"},
                          {"at": "2026-09-14", "evidence_points": 97, "seven_day_pct": 14.0}))
        self.assertLess(c["rounding_interval_after"][1], c["rounding_interval_before"][0])
        self.assertEqual(self._publish("2026-09-16")["events"], [])
        self.assertEqual(self._publish("2026-09-13")["events"], [])

    def test_max20_current_follows_the_cut_from_the_publish_that_detects_it(self):
        # Before the cut current is the trailing fortnight, 6.27. From the publish that
        # certifies the cut it is the new regime's own level (46/10 = 4.6 on 09-14T17,
        # 63/14 = 4.5 on 09-14T22), with the interval that level really has; once the cut
        # no longer certifies (09-16) it is the trailing fortnight again, mostly pre-cut.
        self.assertEqual(REAL_WEEKLY["current"], 6.24)
        for until, fired, current in [("2026-09-13", False, 6.27), ("2026-09-14T17", True, 4.6),
                                      ("2026-09-14T22", True, 4.5), ("2026-09-16", False, 6.15)]:
            with self.subTest(until=until):
                j = self._publish(until)
                self.assertEqual(j["last_change"] is not None, fired)
                self.assertEqual(j["weekly_windows"]["max20"]["current"], current)
                lo, hi = j["weekly_windows"]["max20"]["current_estimate"]["rounding_interval"]
                self.assertTrue(lo <= current <= hi)


class LastChangeScopeTests(unittest.TestCase):
    # A certifiable weekly step dated 2026-09-14: eight windows at 6.0, then four at 4.5.
    WEEKLY_MAX20 = {"current": None, "history": [], "by_window": (
        [window("2026-09-%02dT10:00:00+00:00" % d, 60.0, 10.0) for d in range(6, 14)]
        + [window("2026-09-14T%02d:00:00+00:00" % h, 45.0, 10.0) for h in (10, 15, 20)]
        + [window("2026-09-15T10:00:00+00:00", 45.0, 10.0)])}

    def test_newer_weekly_event_beats_an_older_window_event(self):
        # The window event is dated 2026-09-06 (passive readings step down there); the
        # weekly event is dated 2026-09-14, later, so it must win last_change.
        passive = dict(PASSIVE, weekly_windows=self.WEEKLY_MAX20)
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        j = build_public_json([], passive, EFFORT, PRICES, now, gs_passive=daily_report([15.0] * 5 + [10.5] * 5))
        window_events = [e for e in j["events"] if e.get("scope") == "window"]
        self.assertTrue(window_events)
        self.assertLess(window_events[-1]["date"], "2026-09-14")
        self.assertEqual(j["last_change"]["scope"], "weekly")
        self.assertEqual(j["last_change"]["date"], "2026-09-14")

    def test_older_weekly_event_loses_to_a_newer_window_event(self):
        # Same weekly step (dated 2026-09-14), but now the window event is
        # pushed later than it, past 2026-09-14, so the window event must win.
        passive = dict(PASSIVE, weekly_windows=self.WEEKLY_MAX20)
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        j = build_public_json([], passive, EFFORT, PRICES, now, gs_passive=daily_report([15.0] * 20 + [10.5] * 5))
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
