import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import ClassVar

from tracker.detect import MIN_STRETCH_PCT
from tracker.gs_passive import passive_credit_points, passive_dollar_readings, stretch_credits
from tracker.publish import (
    _regime_with_evidence,
    CREDITS_TABLE_AS_OF,
    REFERENCE_MIX,
    blended_api_price_per_token,
    blended_price_per_token,
    build_public_json,
    usd_per_pct,
)


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


def gs_weekly(**accounts):
    """A tracker.gs_passive report carrying only each account's own weekly_by_window points.

    The shape history/gs-passive.json has for jwork and dave: the same per-window
    points tracker/weekly.py writes, under accounts.<name>.weekly_by_window.
    """
    return {"accounts": {name: {"account": name, "stretches": [], "weekly_by_window": points}
                         for name, points in accounts.items()}}


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


#: Stretches a day in the event fixtures. Detection reads stretches, not days, and
#: weighs each by the meter movement it carries, so what a fixture has to supply is
#: meter percent: at 10% a stretch, four a day is 40 points a day, and the ten-then-five
#: day shapes below clear MIN_STRETCH_PCT on both sides with room for the intervals to
#: separate a 30% step (tracker/detect.py, "Credit stretches": 18 stretches a side).
#: One a day, the shape these fixtures had while a day was the unit of evidence, is 50
#: points on the short side and certifies nothing.
EVENT_STRETCHES_PER_DAY = 4


def daily_report(budgets, *, start_day=1, account="dave", reset_verified=True, per_day=1):
    """`per_day` accepted stretches a day on 2026-09-(start_day + i), each worth budgets[i]
    meter dollars per window. The day's pooled reading is budgets[i] whatever `per_day` is;
    only the meter movement behind it grows."""
    stretches = []
    for i, budget in enumerate(budgets):
        for k in range(per_day):
            # 10% of a window at $3.75/Mtok cache_write: budget/10 dollars is budget/10/3.75e-6 tokens.
            stretches.append({"status": "accepted",
                              "end": f"2026-09-{start_day + i:02d}T{8 + k:02d}:00:00+00:00",
                              "delta_pct": 10, "windows": 1, "reset_verified": reset_verified,
                              "tokens": {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0,
                                                             "cache_write": round(budget / 10 / 3.75e-6)}}})
    return {"accounts": {account: {"account": account, "stretches": stretches}}}


def agreeing_report(budgets, *, start_day=1, reset_verified=True, accounts=("dave", "jwork"),
                    per_day=EVENT_STRETCHES_PER_DAY):
    """The same daily series on two accounts, which is what a dollar-series change needs.

    A step one account alone saw is that account's own workload until a second account
    steps the same way within DOLLAR_AGREEMENT_DAYS (tracker/publish.py
    _agreeing_credit_events), so every fixture that expects an event carries two.
    """
    report = {"accounts": {}}
    for account in accounts:
        report["accounts"].update(daily_report(budgets, start_day=start_day, account=account,
                                               reset_verified=reset_verified, per_day=per_day)["accounts"])
    return report


def window_event(j):
    """The newest dollar-series (window-scope) event of a published document, or None."""
    events = [e for e in j["events"] if e.get("scope") == "window"]
    return events[-1] if events else None


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
        # One window's 1 : 6 : 20 scaling from the credits table, labelled as such;
        # nothing about windows per week.
        self.assertEqual(j["plan_ratios"], {"pro": 0.05, "max5": 0.30, "max20": 1.0})
        self.assertEqual(j["plan_ratios_basis"]["kind"], "credits_table")
        # How many windows a week holds per plan, relative to Max 20x: the other
        # quantity, from the same table, with the tracker's own confirmation beside it.
        self.assertEqual(j["weekly_window_ratios"], {"pro": 1.20, "max5": 1.667, "max20": 1.0})
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
        # History is a step function again (restored 2026-09-17, reverses finding 12 by
        # Jonathan's decision): days before the first reading are held at the first (only)
        # regime's value, back to PASSIVE's own legacy `history` date.
        self.assertEqual(hist[0], {"date": "2026-08-01", "meter_budget_per_window": 15.0,
                                   "tokens_per_window": r["tokens_per_window"],
                                   "api_value_per_window": r["api_value_per_window"],
                                   "api_list_value_per_window": r["api_value_per_window"],
                                   "source": "held", "quality": "measured", "readings": 0, "interpolated": False})
        self.assertEqual(hist[-1], {"date": "2026-09-05", "meter_budget_per_window": 15.0,
                                    "tokens_per_window": r["tokens_per_window"],
                                    "api_value_per_window": r["api_value_per_window"],
                                    "api_list_value_per_window": r["api_value_per_window"],
                                    "source": "passive", "quality": "measured", "readings": 1, "interpolated": False})
        self.assertEqual(len(hist), 36)

    def test_fable_is_not_included_on_pro_and_has_half_the_weekly_limit_on_max(self):
        # Audit finding 2: the plan matrix offered Fable allowance Pro does not include, and
        # a full weekly allowance on Max where Fable has half. Each cell names its source.
        prices = {**PRICES, "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 12.5}}
        j = build_public_json([], PASSIVE, EFFORT, prices, datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc),
                              gs_passive=daily_report([15.0] * 5))
        limits = j["model_plan_limits"]
        self.assertEqual({plan: (cell["included"], cell["weekly_fraction"]) for plan, cell in limits["claude-fable-5-1"].items()},
                         {"pro": (False, 0.0), "max5": (True, 0.5), "max20": (True, 0.5)})
        self.assertEqual({plan: (cell["included"], cell["weekly_fraction"]) for plan, cell in limits["claude-opus-5"].items()},
                         {"pro": (True, 1.0), "max5": (True, 1.0), "max20": (True, 1.0)})
        self.assertTrue(all(cell["source_url"].startswith("https://") and cell["as_of"]
                            for model in limits.values() for cell in model.values()))

    def test_session_count_is_published_again(self):
        # Restored 2026-09-17 (reverses finding 11 by Jonathan's decision): the page's
        # "about N sessions" line needs this figure back, passed through from passive.py's
        # own transcript-derived median, unrelated to the frozen reference_mix below.
        passive = dict(PASSIVE, session_tokens={"claude-sonnet-5": 410000})
        j = build_public_json([], passive, EFFORT, PRICES, datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc),
                              gs_passive=daily_report([15.0] * 5))
        self.assertEqual(j["session_tokens"], {"claude-sonnet-5": 410000})
        self.assertEqual(j["rates"]["claude-sonnet-5"]["reference_mix"]["kind"], "derived_scenario")

    def test_session_tokens_defaults_to_empty_without_a_passive_field(self):
        j = build_public_json([], PASSIVE, EFFORT, PRICES, datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc),
                              gs_passive=daily_report([15.0] * 5))
        self.assertEqual(j["session_tokens"], {})

    def test_history_days_carry_the_regimes_meter_budget_for_every_model(self):
        # Every history day carries the meter budget, one figure per day and the same for
        # every model; the API list value is a different unit, the list price of the tokens
        # that budget converts to (finding 1), and with cache reads free to the meter it is
        # the larger figure. With flat readings either side of a step, the regime-held value
        # (restored 2026-09-17, reverses finding 12) and the day's own reading agree, so this
        # alone does not distinguish them -- see test_history_is_flat_within_a_regime_even_with_noisy_daily_readings.
        budgets = [15.0] * 10 + [10.5] * 5
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, CACHE_READ_FREE, now, gs_passive=agreeing_report(budgets))
        event_date = window_event(j)["date"]
        sonnet, opus = j["history"]["claude-sonnet-5"], j["history"]["claude-opus-5"]
        self.assertEqual([h["meter_budget_per_window"] for h in sonnet], [h["meter_budget_per_window"] for h in opus])
        self.assertEqual({h["meter_budget_per_window"] for h in sonnet if h["date"] < event_date}, {15.0})
        self.assertEqual({h["meter_budget_per_window"] for h in sonnet if h["date"] >= event_date}, {10.5})
        self.assertTrue(all(h["api_value_per_window"] > h["meter_budget_per_window"] for h in sonnet + opus))
        self.assertEqual(sonnet[-1]["meter_budget_per_window"], j["rates"]["claude-sonnet-5"]["meter_budget_per_window"])
        self.assertEqual(j["rates"]["claude-opus-5"]["meter_budget_per_window"], 10.5)

    def test_history_is_flat_within_a_regime_even_with_noisy_daily_readings(self):
        # Restored 2026-09-17 (reverses finding 12 by Jonathan's decision): Jonathan's own
        # passive account is noisy 2x-5x day to day, and the live page showed that noise
        # directly ("so much up and down instead of accurate") once finding 12 made history
        # the day's own reading. A model with real day-to-day scatter but no detected change
        # must publish one flat figure throughout, the regime's median, not its raw readings.
        budgets = [40.0, 38.0, 32.0, 47.0, 48.0, 42.0, 40.0, 36.0, 30.0]
        now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now, gs_passive=daily_report(budgets))
        self.assertIsNone(j["last_change"])
        sonnet = j["history"]["claude-sonnet-5"]
        passive_days = [h for h in sonnet if h["source"] == "passive"]
        self.assertEqual(len(passive_days), 9)
        self.assertEqual({h["meter_budget_per_window"] for h in sonnet}, {median(budgets)})
        self.assertEqual(j["rates"]["claude-sonnet-5"]["meter_budget_per_window"], median(budgets))

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
            hist = j["history"][model]
            self.assertEqual([h["date"] for h in hist if h["source"] not in ("held", "passive")], [])
            self.assertEqual([(h["date"], h["source"]) for h in hist[-2:]],
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
        hist = j["history"]["claude-sonnet-5"]
        self.assertEqual([h["meter_budget_per_window"] for h in hist if h["source"] == "passive"], [15.0, 15.0])

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
                              gs_passive=agreeing_report([15.0] * 10 + [10.5] * 5))
        c = window_event(j)
        self.assertEqual((c["direction"], c["model"], c["scope"], c["metric"]),
                         ("decreased", "all", "window", "meter_budget_per_window"))
        self.assertIn(c["percent"], range(20, 40))
        # An observed change in this account's metric, provisional: the daily readings
        # carry no uncertainty model (findings 4 and 5). A provisional change is published
        # in `events` and never as `last_change`.
        self.assertEqual((c["attribution"], c["observation_scope"], c["provisional"]),
                         ("observed_account_metric_change", "account", True))
        self.assertIsNone(j["last_change"])
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

    def test_a_day_mixing_verified_and_reset_less_stretches_keeps_its_verified_reading(self):
        # Review of PR 57: jwork's first day on its reset-bearing log also holds that
        # morning's ceiling-log stretches. The legacy-inclusive readings pool both into
        # one day value, so the history loop skipped it as "not verified" and never
        # added the verified one: the day vanished. It must publish its verified
        # reading alone, measured; a day with only reset-less stretches stays legacy.
        report = daily_report([15.0] * 5, start_day=1)
        legacy = daily_report([30.0, 12.0], start_day=5, reset_verified=False)["accounts"]["dave"]["stretches"]
        legacy[0]["end"] = "2026-09-05T06:00:00+00:00"   # before the verified stretch that day
        report["accounts"]["dave"]["stretches"] = sorted(report["accounts"]["dave"]["stretches"] + legacy,
                                                         key=lambda st: st["end"])
        j = build_public_json([], PASSIVE, EFFORT, PRICES, datetime(2026, 9, 6, 20, tzinfo=timezone.utc), gs_passive=report)
        hist = {h["date"]: (h["source"], h["quality"], h["readings"]) for h in j["history"]["claude-sonnet-5"]}
        self.assertEqual(hist["2026-09-05"], ("passive", "measured", 1))
        self.assertEqual(hist["2026-09-06"], ("passive", "legacy_reset_unverified", 1))
        # Too few readings (6) to split into a second regime: both days hold the same
        # regime-wide median, 15.0 -- the point being the verified day was not dropped
        # from the pool (PR 57's review bug), not that it shows its own value any more
        # (that changed with finding 12's reversal, restored 2026-09-17).
        self.assertEqual({h["meter_budget_per_window"] for h in j["history"]["claude-sonnet-5"]}, {15.0})
        self.assertEqual(j["availability"]["evidence"], "measured")

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
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now, gs_passive=agreeing_report([15.0] * 10 + [10.5] * 5))
        hist = j["history"]["claude-sonnet-5"]
        event_date = window_event(j)["date"]
        before = [h["tokens_per_window"] for h in hist if h["date"] < event_date]
        on_and_after = [h["tokens_per_window"] for h in hist if h["date"] >= event_date]
        self.assertTrue(before and on_and_after)
        self.assertEqual(len(set(before)), 1, before)
        self.assertEqual(len(set(on_and_after)), 1, on_and_after)
        self.assertNotEqual(before[0], on_and_after[0])
        self.assertEqual({h["tokens_per_window"] for h in hist}, {before[0], on_and_after[0]})

    def test_events_includes_every_detected_change(self):
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now, gs_passive=agreeing_report([15.0] * 10 + [10.5] * 5))
        change_events = [e for e in j["events"] if e["kind"] == "change"]
        self.assertTrue(change_events)
        self.assertEqual(change_events[-1]["date"], window_event(j)["date"])
        self.assertEqual(change_events[-1]["label"],
                          f"Observed window budget changed -{window_event(j)['percent']}%")


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
        # Probe days are not part of the series: days before the first passive reading
        # are held at its regime's value, back to PASSIVE's own legacy `history` date
        # (restored 2026-09-17, reverses finding 12 by Jonathan's decision), never at a
        # probe day's own value.
        hist = j["history"]["claude-sonnet-5"]
        self.assertEqual((hist[-1]["date"], hist[-1]["source"]), ("2026-09-06", "passive"))
        self.assertEqual([h["date"] for h in hist if h["source"] not in ("held", "passive")], [])

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

    def test_a_noisy_passive_series_fires_nothing_and_a_sustained_step_does(self):
        # Nine passive days scattering +-20% around one level fire nothing (detect_changes
        # would have fired on the 4th and 5th); a sustained week 40% higher does. The
        # series is per stretch now, so each day carries EVENT_STRETCHES_PER_DAY of them
        # and the evidence is meter percent, not a count of days.
        def rpt(values, start_day=1, accounts=("dave", "jwork")):
            return {"accounts": {account: {"stretches": [
                {"status": "accepted",
                 "end": f"2026-09-{start_day + i:02d}T{8 + k:02d}:00:00+00:00", "delta_pct": 10,
                 "windows": 1, "reset_verified": True,
                 "tokens": {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0, "cache_write": int(v)}}}
                for i, v in enumerate(values) for k in range(EVENT_STRETCHES_PER_DAY)]}
                for account in accounts}}
        noisy = [400_000, 380_000, 320_000, 470_000, 480_000, 420_000, 400_000, 360_000, 300_000]
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        j = build_public_json([], {"split": {"cache_write": 1.0}}, EFFORT, PRICES, now, gs_passive=rpt(noisy))
        self.assertEqual(j["events"], [])
        stepped = [400_000] * 8 + [560_000] * 8
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
        j = build_public_json([], {"split": {"cache_write": 1.0}}, EFFORT, PRICES, now, gs_passive=rpt(stepped))
        self.assertIsNotNone(window_event(j))
        self.assertEqual(window_event(j)["date"], "2026-09-09")

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
    RUNS: ClassVar[dict] = {"claude-sonnet-5/low": [
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
    # 2026-08-14 is the last full Max 5x week (ends on PLAN_CHANGE itself, at or
    # before it); 2026-08-21's (2026-08-14, 2026-08-21] span starts right at
    # PLAN_CHANGE and is a full Max 20x week, along with 2026-08-28 and 2026-09-04.
    # Weekly buckets are date-only (_plan_for_week), so none of these straddle the
    # precise within-day seam the per-window series uses (PLAN_CHANGE_AT).
    PASSIVE_WEEKLY: ClassVar[dict] = {"current": 6.46, "history": [
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
        # max5 and pro are scaled off max20's own figure, so with nothing to scale
        # from they say that rather than borrowing a weekly-row median.
        self.assertEqual({p: (ww[p]["current"], ww[p]["availability"]) for p in ("max20", "max5", "pro")}, {
            "max20": (None, {"status": "unavailable", "reason": "no_paired_meter_data"}),
            "max5": (None, {"status": "unavailable", "reason": "no_max20_current_to_scale_from"}),
            "pro": (None, {"status": "unavailable", "reason": "no_max20_current_to_scale_from"})})

    def test_passive_weeks_are_split_by_plan(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        passive = dict(PASSIVE, weekly_windows=self.PASSIVE_WEEKLY)
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        ww = j["weekly_windows"]
        self.assertEqual([h["week_ending"] for h in ww["max5"]["history"]], ["2026-08-14"])
        # The week ending 2026-08-21 starts ON the plan-change day, so it holds the last
        # Max 5x window and the straddling one and belongs to neither plan (_plan_for_week).
        self.assertEqual([h["week_ending"] for h in ww["max20"]["history"]],
                         ["2026-08-28", "2026-09-04"])
        self.assertEqual(ww["passive"]["history"], [dict(h, partial=False) for h in self.PASSIVE_WEEKLY["history"]])
        self.assertEqual(ww["probe"], {"current": None, "history": [], "by_window": []})
        # passive.json's own two-weeks median is not a current value (audit finding 6) and
        # is not published on the raw series either, and weeks paired before the finding-3
        # repair are published as legacy evidence.
        self.assertIsNone(ww["max20"]["current"])
        self.assertEqual((ww["passive"]["current"], ww["passive"]["reason"]),
                         (None, "median_of_two_complete_weeks_is_not_a_measurement"))
        self.assertEqual({(h["quality"], tuple(h["reasons"])) for h in ww["max20"]["history"]},
                         {("legacy_uncertain", ("paired_before_denominator_repair",))})
        self.assertFalse(ww["max20"]["assumed"])
        # max5's `current` is inferred from max20 now, so the block says `assumed`; its
        # weekly rows are still its own measured Max 5x era.
        self.assertTrue(ww["max5"]["assumed"])
        self.assertEqual({h["assumed"] for h in ww["max5"]["history"]}, {False})

    def test_max5_and_pro_currents_are_scaled_off_the_live_max20_figure(self):
        # Neither plan is measured on any watched account any more, so each one's
        # `current` is the live max20 figure times its weekly-window ratio from the
        # credits table, marked inferred. Pro's history and regimes stay empty; max5
        # keeps its own measured Jun-Aug weeks.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        by_window = [window(f"2026-08-{d:02d}T22:00:00+00:00", 60.0, 10.0) for d in range(15, 27)]
        passive = dict(PASSIVE, weekly_windows=dict(self.PASSIVE_WEEKLY, by_window=by_window))
        now = datetime(2026, 8, 27, 20, 15, tzinfo=timezone.utc)
        ww = build_public_json(rows, passive, EFFORT, PRICES, now)["weekly_windows"]
        self.assertEqual(ww["max20"]["current"], 6.0)
        self.assertEqual((ww["max5"]["current"], ww["pro"]["current"]),
                         (round(6.0 * 1.667, 2), round(6.0 * 1.20, 2)))
        for plan, ratio in (("max5", 1.667), ("pro", 1.20)):
            self.assertEqual((ww[plan]["assumed"], ww[plan]["inferred_from"],
                              ww[plan]["weekly_window_ratio"]), (True, "max20", ratio), plan)
            self.assertEqual(ww[plan]["availability"], {
                "status": "inferred", "reason": "scaled_from_max20_by_weekly_window_ratio"}, plan)
        self.assertEqual((ww["pro"]["history"], ww["pro"]["regimes"]), ([], []))
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
        self.assertEqual([h["week_ending"] for h in ww["max20"]["history"]],
                         ["2026-08-28", "2026-09-04"])
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
        by_window = ([window(f"2026-08-2{d}T10:00:00+00:00", 65.0, 10.0) for d in range(6)]   # 6.5, >14 days old
                     + [window(f"2026-09-{d:02d}T10:00:00+00:00", 60.0, 10.0) for d in range(4, 15)])  # 6.0
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
        # Max 5x windows (about 11) right up to PLAN_CHANGE_AT, then Max 20x at 6.5:
        # the plan change is Jonathan's, not Anthropic's, and must not fire.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        by_window = ([window(f"2026-08-{d:02d}T10:00:00+00:00", 110.0, 10.0) for d in range(6, 15)]
                     + [window("2026-08-14T22:00:00+00:00", 65.0, 10.0)]
                     + [window(f"2026-08-{d:02d}T22:00:00+00:00", 65.0, 10.0) for d in range(15, 27)])
        passive = dict(PASSIVE, weekly_windows={"current": 6.46, "history": self.PASSIVE_WEEKLY["history"],
                                                "by_window": by_window})
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 6.5)
        # Max 5x's own era is still published as measured regimes and weeks; only its
        # `current` is inferred, 6.5 scaled by the weekly-window ratio.
        self.assertEqual(j["weekly_windows"]["max5"]["current"], round(6.5 * 1.667, 2))
        self.assertEqual([r["windows"] for r in j["weekly_windows"]["max5"]["regimes"]], [11.0])
        self.assertFalse(j["weekly_windows"]["max5"]["plan_change"]["independently_verified"])

    def test_per_window_seam_splits_within_plan_change_day_not_by_whole_day(self):
        # The real seam: a window ending 16:20 UTC on PLAN_CHANGE day is the last Max 5x
        # point (ends at or before PLAN_CHANGE_AT, 17:00); a window whose own 5-hour
        # start lands at or after PLAN_CHANGE_AT that same day is the first Max 20x
        # point -- both land on 2026-08-14. Max 5x has exactly one regime and Max 20x's
        # first regime starts that day, not four days later.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        by_window = ([window(f"2026-08-{d:02d}T10:00:00+00:00", 110.0, 10.0) for d in range(1, 14)]
                     + [window("2026-08-14T16:20:00+00:00", 110.0, 10.0)]  # last Max 5x window
                     + [window("2026-08-14T22:00:00+00:00", 65.0, 10.0)]  # first Max 20x window
                     + [window(f"2026-08-{d:02d}T22:00:00+00:00", 65.0, 10.0) for d in range(15, 27)])
        passive = dict(PASSIVE, weekly_windows={"current": 6.46, "history": self.PASSIVE_WEEKLY["history"],
                                                "by_window": by_window})
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        max5, max20 = j["weekly_windows"]["max5"], j["weekly_windows"]["max20"]
        self.assertEqual([r["windows"] for r in max5["regimes"]], [11.0])
        self.assertEqual(max5["regimes"][0]["end"][:10], "2026-08-14")
        self.assertEqual(max20["regimes"][0]["windows"], 6.5)
        self.assertEqual(max20["regimes"][0]["start"][:10], "2026-08-14")
        self.assertEqual([e for e in j["events"] if e.get("scope") == "weekly"], [])

    def test_history_days_hold_at_the_nearest_regime_with_readings(self):
        # Buckets 1 and 3 have readings; a day indexing the empty bucket 2 holds at 1, a
        # held day before the first reading (bucket 0) holds at the oldest evidenced
        # regime, and a day past the newest reading holds at 3.
        values = {1: [60.0], 3: [40.0]}
        self.assertEqual(_regime_with_evidence(1, values), 1)
        self.assertEqual(_regime_with_evidence(2, values), 1)
        self.assertEqual(_regime_with_evidence(0, values), 1)
        self.assertEqual(_regime_with_evidence(7, values), 3)

    def test_plan_ratios_basis_names_the_credits_table_and_the_date_it_describes(self):
        # What one five-hour window is worth between plans, 1 : 6 : 20, with the
        # credits it came from and the date the table describes. A documented figure
        # is a reference to compare a measurement against, and an undated one reads
        # as a figure for now -- which is why `dated` is true and `as_of` is the same
        # January date every other block quoting this URL publishes.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(j["plan_ratios"], {"pro": 0.05, "max5": 0.30, "max20": 1.0})
        self.assertEqual(j["plan_ratios_basis"], {
            "kind": "credits_table", "scope": "one five-hour window",
            "credits_per_window": {"pro": 550_000, "max5": 3_300_000, "max20": 11_000_000},
            "source_url": "https://she-llac.com/claude-limits",
            "as_of": "2026-01-25", "dated": True})

    def test_weekly_window_ratios_are_the_credits_table_figures_not_a_measured_seam(self):
        # Max 5x ran a short 6.6 dip early on (unrelated to the plan move -- some other
        # stretch of heavier use), then about 11 windows a week for most of its run up
        # to PLAN_CHANGE_AT; Max 20x follows at 6.5 from the seam. Both eras still
        # publish as regimes, but the ratio itself is the credits table's (1.667 and
        # 1.20), not this account's own seam: one account's two eras are a
        # confirmation of that figure, not a replacement for it.
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        by_window = ([window(f"2026-06-{d:02d}T10:00:00+00:00", 66.0, 10.0) for d in range(20, 24)]
                     + [window(f"2026-07-{d:02d}T10:00:00+00:00", 110.0, 10.0) for d in range(1, 32)]
                     + [window(f"2026-08-{d:02d}T10:00:00+00:00", 110.0, 10.0) for d in range(1, 14)]
                     + [window("2026-08-14T16:20:00+00:00", 110.0, 10.0)]
                     + [window("2026-08-14T22:00:00+00:00", 65.0, 10.0)]
                     + [window(f"2026-08-{d:02d}T22:00:00+00:00", 65.0, 10.0) for d in range(15, 27)])
        passive = dict(PASSIVE, weekly_windows={"current": 6.46, "history": self.PASSIVE_WEEKLY["history"],
                                                "by_window": by_window})
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, passive, EFFORT, PRICES, now)
        regimes = j["weekly_windows"]["max5"]["regimes"]
        self.assertEqual([r["windows"] for r in regimes], [6.6, 11.0])
        self.assertGreater(regimes[1]["points"], regimes[0]["points"])
        self.assertEqual(j["weekly_windows"]["max20"]["regimes"][0]["windows"], 6.5)
        self.assertEqual(j["weekly_window_ratios"], {"pro": 1.20, "max5": 1.667, "max20": 1.0})
        # The seam this account actually shows, 11.0 over 6.5, is 1.69: close to the
        # table's 1.667 and published as `measured_confirmation`, not as the ratio.
        basis = j["weekly_window_ratios_basis"]
        self.assertEqual((basis["kind"], basis["dated"], basis["as_of"], basis["source_url"]),
                         ("credits_table", True, "2026-01-25", "https://she-llac.com/claude-limits"))
        self.assertEqual(basis["documented_windows_per_week"],
                         {"pro": 9.09, "max5": 12.63, "max20": 7.58})
        self.assertEqual(basis["credits_per_week"],
                         {"pro": 5_000_000, "max5": 41_666_700, "max20": 83_333_300})
        self.assertEqual(basis["measured_confirmation"]["max5_over_max20"], 1.66)

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


def flat(days, d5, d7=10.0, hour=10):
    """One per-window point a day, each moving the meters by d5 and d7."""
    return [window(f"2026-09-{d:02d}T{hour:02d}:00:00+00:00", d5, d7) for d in days]


class ThreeAccountWeeklyTests(unittest.TestCase):
    """weekly_windows.max20 over every watched Max 20x account, not masterrig alone.

    a1 is masterrig (history/passive.json's own by_window), a2 and a3 are the gs
    accounts' weekly_by_window. a1 and a2 both step, at their own seven-day
    resets three days apart; a3 has too little history to step at all.
    """

    A1 = flat(range(8, 14), 60.0) + flat(range(14, 18), 45.0)
    A2 = flat(range(8, 14), 60.0) + flat(range(16, 20), 45.0)
    A3 = flat(range(10, 14), 60.0)
    NOW = datetime(2026, 9, 20, 6, tzinfo=timezone.utc)

    def _publish(self):
        passive = dict(PASSIVE, weekly_windows={"current": 6.0, "history": [],
                                                "by_window": self.A1})
        return build_public_json([], passive, EFFORT, PRICES, self.NOW,
                                 gs_passive=gs_weekly(jwork=self.A2, dave=self.A3))

    def test_every_accounts_points_are_pooled_and_tagged_but_never_named(self):
        j = self._publish()
        max20 = j["weekly_windows"]["max20"]
        counts = {label: sum(1 for p in max20["by_window"] if p["account"] == label)
                  for label in ("a1", "a2", "a3")}
        self.assertEqual(counts, {"a1": 10, "a2": 10, "a3": 4})
        self.assertEqual({label: max20["by_account"][label]["n"] for label in counts}, counts)
        self.assertEqual([p["window_ending"] for p in max20["by_window"]],
                         sorted(p["window_ending"] for p in max20["by_window"]))
        # The current regime is the post-cut one: eight windows at 45/10 across a1 and a2.
        self.assertEqual(max20["current"], 4.5)
        estimate = max20["current_estimate"]
        self.assertEqual((estimate["points"], estimate["seven_day_pct"]), (8, 80.0))
        # Three accounts stand behind the published evidence, and none of them is named.
        self.assertEqual(j["passive_account_count"], 3)
        for name in ("masterrig", "jwork", "dave"):
            self.assertNotIn(name, json.dumps(j))

    def test_each_accounts_step_is_dated_on_its_own_points(self):
        # The cut reaches a1 on 09-14 and a2 on 09-16, each at its own seven-day
        # reset; a3 has one regime and no step. before/after are the pooled levels
        # the step separates and percent is signed.
        by_account = self._publish()["weekly_windows"]["max20"]["by_account"]
        self.assertEqual(by_account["a1"]["step"],
                         {"onset": "2026-09-14", "before": 6.0, "after": 4.5, "percent": -25})
        self.assertEqual(by_account["a2"]["step"],
                         {"onset": "2026-09-16", "before": 6.0, "after": 4.5, "percent": -25})
        self.assertIsNone(by_account["a3"]["step"])
        self.assertEqual({label: by_account[label]["current"] for label in ("a1", "a2", "a3")},
                         {"a1": 4.5, "a2": 4.5, "a3": 6.0})
        self.assertEqual([r["windows"] for r in by_account["a3"]["regimes"]], [6.0])

    def test_the_pooled_event_carries_the_accounts_own_onsets(self):
        # One published event, bounded by the earliest and the latest account onset
        # and dated at the earliest. The detector's own window bounds stay under
        # `onset.from_windows`.
        j = self._publish()
        events = [e for e in j["events"] if e["scope"] == "weekly"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["date"], "2026-09-14")
        self.assertEqual({k: events[0]["onset"][k] for k in ("earliest", "latest")},
                         {"earliest": "2026-09-14", "latest": "2026-09-16"})
        self.assertEqual(events[0]["onset"]["from_windows"],
                         {"earliest": "2026-09-13", "latest": "2026-09-14"})
        self.assertEqual(events[0]["attribution"], "observed_account_metric_change")

    def test_the_event_row_states_the_ratio_and_leaves_the_meter_unresolved(self):
        # The published row is the measured quantity -- how many five-hour windows a
        # week holds -- with its own percent and its own onset span. It never says
        # the weekly cap fell by that much: a bigger five-hour window moves the same
        # ratio, and the split is unresolved.
        j = self._publish()
        event = [e for e in j["events"] if e["scope"] == "weekly"][0]
        self.assertEqual(event["label"],
                         "Observed windows per week fell about 25% around 2026-09-14 to 2026-09-16")
        self.assertEqual(event["meter_attribution"], "unresolved")
        self.assertEqual(j["last_change"]["meter_attribution"], "unresolved")
        for word in ("cap", "limit", "anthropic"):
            self.assertNotIn(word, event["label"].lower())

    def test_a_pooled_date_later_than_an_accounts_own_onset_is_moved_back_and_says_so(self):
        # a1 steps 6.0 -> 5.0 on 09-12; a2 carries twice as many windows, holds 6.0
        # until its own reset on 09-16 and then falls to 3.6, so the pooled series
        # splits at 09-16 and a1's earlier step is lost in the pool. The accounts'
        # own onsets date the event, and `attribution` says the date did not come
        # from the pooled windows.
        a1 = flat(range(4, 12), 60.0) + flat(range(12, 18), 50.0)
        a2 = (flat(range(4, 16), 60.0, hour=8) + flat(range(4, 16), 60.0, hour=14)
              + flat(range(16, 20), 36.0, hour=8) + flat(range(16, 20), 36.0, hour=14))
        passive = dict(PASSIVE, weekly_windows={"current": 6.0, "history": [], "by_window": a1})
        j = build_public_json([], passive, EFFORT, PRICES, self.NOW, gs_passive=gs_weekly(jwork=a2))
        by_account = j["weekly_windows"]["max20"]["by_account"]
        self.assertEqual(by_account["a1"]["step"]["onset"], "2026-09-12")
        self.assertEqual(by_account["a2"]["step"]["onset"], "2026-09-16")
        self.assertEqual(by_account["a3"], {"n": 0, "current": None, "regimes": [], "step": None})
        change = j["last_change"]
        self.assertEqual((change["date"], change["onset"]["earliest"], change["onset"]["latest"]),
                         ("2026-09-12", "2026-09-12", "2026-09-16"))
        self.assertGreater(change["onset"]["from_windows"]["latest"], change["date"])
        self.assertEqual(change["attribution"],
                         "observed_account_metric_change_dated_from_per_account_onsets")

    def test_pooled_calendar_weeks_are_published_for_the_chart(self):
        # The same points bucketed into ISO weeks, pooled the way tracker/weekly.py
        # pools its own history rows, with the open week flagged partial.
        weekly = self._publish()["weekly_windows"]["max20"]["weekly"]
        self.assertEqual([(w["week_ending"], w["windows"], w["n"], w["five_hour_pct"],
                           w["seven_day_pct"], w["partial"]) for w in weekly],
                         [("2026-09-13", 6.0, 16, 960.0, 160.0, False),
                          ("2026-09-20", 4.5, 8, 360.0, 80.0, True)])
        lo, hi = weekly[0]["rounding_interval"]
        self.assertLess(lo, 6.0)
        self.assertGreater(hi, 6.0)

    def test_a_gs_point_with_no_reset_id_still_counts(self):
        # jwork's meter log names the reset on only its newest few windows, so
        # requiring one would drop that account altogether. The flag is published
        # per point instead.
        a2 = [dict(p, reset_verified=False) for p in self.A2]
        passive = dict(PASSIVE, weekly_windows={"current": 6.0, "history": [],
                                                "by_window": self.A1})
        j = build_public_json([], passive, EFFORT, PRICES, self.NOW, gs_passive=gs_weekly(jwork=a2))
        max20 = j["weekly_windows"]["max20"]
        self.assertEqual(max20["by_account"]["a2"]["n"], 10)
        self.assertEqual({p["reset_verified"] for p in max20["by_window"] if p["account"] == "a2"},
                         {False})

    def test_points_from_the_max5_era_stay_out_of_every_account(self):
        # The era rule applies to all three accounts, not just masterrig's own log:
        # a window that starts before PLAN_CHANGE_AT reads at the Max 5x ratio.
        early = [window("2026-08-10T10:00:00+00:00", 110.0, 10.0)]
        passive = dict(PASSIVE, weekly_windows={"current": 6.0, "history": [],
                                                "by_window": early + self.A1})
        j = build_public_json([], passive, EFFORT, PRICES, self.NOW,
                              gs_passive=gs_weekly(jwork=early + self.A2))
        by_account = j["weekly_windows"]["max20"]["by_account"]
        self.assertEqual((by_account["a1"]["n"], by_account["a2"]["n"]), (10, 10))
        self.assertTrue(all(p["window_ending"] > "2026-08-14"
                            for p in j["weekly_windows"]["max20"]["by_window"]))


# The weekly_windows block of the committed history/passive.json: masterrig's whole
# ~/.moonlighter/usage_log.jsonl (every five-hour window from 2026-06-13), paired as
# tracker/weekly.py pairs since the audit's finding-3 repair and regenerated on
# masterrig on 2026-09-16. Only the windows ending by that regeneration are read, so
# the pins below do not move as the file grows (tests/test_detect.py, HISTORY_CUTOFF).
HISTORY_CUTOFF = "2026-09-16T17:31"
REAL_WEEKLY = json.loads((Path(__file__).resolve().parents[1] / "history" / "passive.json")
                         .read_text(encoding="utf-8"))["weekly_windows"]


class RealLogTests(unittest.TestCase):
    def _publish(self, until: str, now: datetime) -> dict:
        """The public JSON from the committed history's windows ending before `until` (an ISO prefix)."""
        weekly = dict(REAL_WEEKLY, by_window=[w for w in REAL_WEEKLY["by_window"]
                                              if w["window_ending"] < min(until, HISTORY_CUTOFF)])
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(8, 15)]
        return build_public_json(rows, dict(PASSIVE, weekly_windows=weekly), EFFORT, PRICES, now)

    def test_the_committed_history_publishes_one_weekly_change_the_14_sep_cut(self):
        # Through the real publish path, the committed Max 20x series gives exactly one
        # event: the cut, -28%, dated by its first window, certified against both
        # levels' intervals, and published as an observed change in this account's
        # weekly/window ratio. Max 20x current is the new regime's own level, 145/31.
        j = self._publish(HISTORY_CUTOFF, datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc))
        # The row states the measured quantity and its own dates, and claims nothing
        # about which meter moved: a fall in windows per week can come from a smaller
        # weekly cap, a bigger five-hour window, or both.
        self.assertEqual([(e["date"], e["percent"], e["label"]) for e in j["events"]],
                         [("2026-09-14", 28,
                           "Observed windows per week fell about 28% around 2026-09-14")])
        self.assertEqual([e["meter_attribution"] for e in j["events"]], ["unresolved"])
        c = j["last_change"]
        self.assertEqual((c["scope"], c["direction"], c["metric"], c["attribution"], c["provisional"]),
                         ("weekly", "decreased", "weekly_to_five_hour_ratio", "observed_account_metric_change", False))
        # One account: the onset bounds are its own step's date, with the detector's
        # own window bounds kept under `from_windows`.
        self.assertEqual((c["onset"], c["confirmation"]),
                         ({"earliest": "2026-09-14", "latest": "2026-09-14",
                           "from_windows": {"earliest": "2026-09-13", "latest": "2026-09-14"}},
                          {"at": "2026-09-15", "evidence_points": 118, "seven_day_pct": 31.0}))
        self.assertEqual((c["rounding_interval_before"], c["rounding_interval_after"]),
                         ([6.1892, 6.8754], [3.9284, 5.695]))
        max20 = j["weekly_windows"]["max20"]
        self.assertEqual((max20["current"], max20["availability"]), (4.68, {"status": "measured", "reason": None}))
        est = max20["current_estimate"]
        self.assertEqual((est["five_hour_pct"], est["seven_day_pct"], est["points"], est["from"][:16], est["stale"]),
                         (145.0, 31.0, 9, "2026-09-14T11:30", False))
        # The Max 5x step on 2026-08-14 is the plan move: with the seam corrected onto
        # the meter's own boundary (16:20/PLAN_CHANGE_AT), the whole run is one regime,
        # not split by a four-day tail misdated to the plan-change day.
        self.assertEqual([(r["start"][:10], r["windows"]) for r in j["weekly_windows"]["max5"]["regimes"]],
                         [("2026-06-13", 10.86)])
        self.assertFalse(j["weekly_windows"]["max5"]["plan_change"]["independently_verified"])

    def test_nothing_publishes_before_the_cut_and_the_daily_replays_settle_on_it(self):
        # As masterrig's daily push would have delivered the history (windows ending by
        # 02:30Z): nothing through 2026-09-14 or 2026-09-15 (with the seam corrected onto
        # PLAN_CHANGE_AT, the max20 series no longer starts four days short, so the
        # 09-15 partial post-cut pool no longer misdates a change to 2026-08-28 the way
        # it used to under the whole-day exclusion); from 2026-09-16 it is the cut, with
        # current following it.
        for day in ("2026-08-25", "2026-09-01", "2026-09-08", "2026-09-14"):
            with self.subTest(day=day):
                j = self._publish(day + "T02:31", datetime.fromisoformat(day + "T03:30:00+00:00"))
                self.assertEqual((j["events"], j["last_change"]), ([], None))
        j = self._publish("2026-09-14T02:31", datetime(2026, 9, 14, 3, 30, tzinfo=timezone.utc))
        self.assertEqual(j["weekly_windows"]["max20"]["current"], 6.3)
        j = self._publish("2026-09-15T02:31", datetime(2026, 9, 15, 3, 30, tzinfo=timezone.utc))
        self.assertEqual([(e["date"], e["percent"]) for e in j["events"]], [])
        j = self._publish("2026-09-16T02:31", datetime(2026, 9, 16, 3, 30, tzinfo=timezone.utc))
        self.assertEqual([(e["date"], e["percent"]) for e in j["events"]], [("2026-09-14", 29)])
        current = j["weekly_windows"]["max20"]["current"]
        lo, hi = j["weekly_windows"]["max20"]["current_estimate"]["rounding_interval"]
        self.assertEqual(current, 4.61)
        self.assertTrue(lo <= current <= hi)


class LastChangeScopeTests(unittest.TestCase):
    # A certifiable weekly step dated 2026-09-14: eight windows at 6.0, then four at 4.5.
    WEEKLY_MAX20: ClassVar[dict] = {"current": None, "history": [], "by_window": (
        [window(f"2026-09-{d:02d}T10:00:00+00:00", 60.0, 10.0) for d in range(6, 14)]
        + [window(f"2026-09-14T{h:02d}:00:00+00:00", 45.0, 10.0) for h in (10, 15, 20)]
        + [window("2026-09-15T10:00:00+00:00", 45.0, 10.0)])}

    def test_newer_weekly_event_beats_an_older_window_event(self):
        # The window event is dated 2026-09-06 (passive readings step down there); the
        # weekly event is dated 2026-09-14, later, so it must win last_change.
        passive = dict(PASSIVE, weekly_windows=self.WEEKLY_MAX20)
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        j = build_public_json([], passive, EFFORT, PRICES, now,
                              gs_passive=agreeing_report([15.0] * 5 + [10.5] * 5))
        self.assertIsNotNone(window_event(j))
        self.assertLess(window_event(j)["date"], "2026-09-14")
        self.assertEqual(j["last_change"]["scope"], "weekly")
        self.assertEqual(j["last_change"]["date"], "2026-09-14")

    def test_a_newer_window_event_never_takes_last_change_from_a_certified_one(self):
        # Same weekly step (dated 2026-09-14) with the window event pushed later than it.
        # The window event is provisional -- the daily readings carry no uncertainty model
        # at all -- and the page states last_change as "Anthropic last decreased Claude's
        # limits by N%", so a provisional event never takes it however new it is.
        passive = dict(PASSIVE, weekly_windows=self.WEEKLY_MAX20)
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        j = build_public_json([], passive, EFFORT, PRICES, now,
                              gs_passive=agreeing_report([15.0] * 20 + [10.5] * 5))
        weekly_events = [e for e in j["events"] if e.get("scope") == "weekly"]
        self.assertTrue(weekly_events)
        self.assertGreater(window_event(j)["date"], weekly_events[-1]["date"])
        self.assertTrue(window_event(j)["provisional"])
        self.assertEqual(j["last_change"]["scope"], "weekly")
        self.assertEqual(j["last_change"]["date"], weekly_events[-1]["date"])

    def test_with_only_a_provisional_change_there_is_no_last_change(self):
        # The provisional event stays in `events` with its own evidence_quality; the
        # headline has nothing certified to state, so last_change is null.
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        j = build_public_json([], PASSIVE, EFFORT, PRICES, now,
                              gs_passive=agreeing_report([15.0] * 10 + [10.5] * 5))
        self.assertIsNone(j["last_change"])
        self.assertEqual([e["evidence_quality"] for e in j["events"]], ["provisional"])


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


class ReferenceDateTests(unittest.TestCase):
    """One source, one date (wf-58 item 2).

    The page reads https://she-llac.com/claude-limits in four places. Two of them --
    `plan_ratios_basis` and `weekly_window_ratios_basis` -- published `dated: false`
    while `reference` and the credits block's weekly-cap baseline both published
    2026-01-25 for the same URL, so the page could print one source with two dates. The
    reconciliation established the date; these four now read one constant.
    """

    def setUp(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        self.j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)

    def _blocks(self):
        return {
            "plan_ratios_basis": self.j["plan_ratios_basis"],
            "weekly_window_ratios_basis": self.j["weekly_window_ratios_basis"],
            "reference": self.j["reference"],
            "credits.window_credits_from_weekly.weekly_cap_baseline_source":
                self.j["credits"]["window_credits_from_weekly"].get("weekly_cap_baseline_source"),
        }

    def test_the_four_blocks_quoting_the_table_agree_on_its_date(self):
        dates = {name: block["as_of"] for name, block in self._blocks().items() if block}
        self.assertEqual(set(dates.values()), {CREDITS_TABLE_AS_OF},
                         f"the four blocks disagree on the table's date: {dates}")

    def test_the_four_blocks_quoting_the_table_agree_on_its_url(self):
        urls = {block.get("url") or block.get("source_url") for block in self._blocks().values() if block}
        self.assertEqual(urls, {"https://she-llac.com/claude-limits"})

    def test_neither_basis_block_still_says_it_is_undated(self):
        for name in ("plan_ratios_basis", "weekly_window_ratios_basis"):
            self.assertTrue(self.j[name]["dated"], name)

    def test_the_date_is_one_constant_and_not_four_literals(self):
        """Move the constant and all four move: the point of the change."""
        import tracker.publish as publisher
        original = publisher.CREDITS_TABLE_AS_OF
        try:
            publisher.CREDITS_TABLE_AS_OF = "2026-02-02"
            rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
            now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
            j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        finally:
            publisher.CREDITS_TABLE_AS_OF = original
        self.assertEqual(j["reference"]["as_of"], "2026-02-02")
        # The two basis dicts are module-level literals built once at import, so they
        # keep the date they were built with; what must never happen is a *third* date
        # appearing, which is what a per-block literal would give.
        self.assertEqual({j["plan_ratios_basis"]["as_of"], j["weekly_window_ratios_basis"]["as_of"]},
                         {original})


class ShortfallTests(unittest.TestCase):
    """`reference.shortfall`: goal 7, published rather than left in a findings doc (wf-58 item 3).

    Both measured plans sat at about 0.86 of the reference table's windows per week
    before the 14 September cut. The page drew the dashed line and said nothing about
    the gap; the reconciliation's answer is that the table is January's and two announced
    changes stand between it and the measurement.
    """

    def _published(self, now=datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        by_window = ([window(f"2026-07-{d:02d}T10:00:00+00:00", 110.0, 10.0) for d in range(1, 32)]
                     + [window(f"2026-08-{d:02d}T10:00:00+00:00", 110.0, 10.0) for d in range(1, 14)]
                     + [window("2026-08-14T16:20:00+00:00", 110.0, 10.0)]
                     + [window("2026-08-14T22:00:00+00:00", 65.0, 10.0)]
                     + [window(f"2026-08-{d:02d}T22:00:00+00:00", 65.0, 10.0) for d in range(15, 27)])
        passive = dict(PASSIVE, weekly_windows={"current": 6.46, "history": [], "by_window": by_window})
        return build_public_json(rows, passive, EFFORT, PRICES, now)

    def test_each_plan_publishes_its_measured_and_documented_windows_per_week(self):
        shortfall = self._published()["reference"]["shortfall"]
        max20, max5 = shortfall["per_plan"]["max20"], shortfall["per_plan"]["max5"]
        self.assertEqual(max5["measured_windows_per_week"], 11.0)
        self.assertEqual(max20["measured_windows_per_week"], 6.5)
        self.assertEqual(max5["documented_windows_per_week"], 12.63)
        self.assertEqual(max20["documented_windows_per_week"], 7.58)

    def test_the_ratio_is_the_measurement_over_the_documented_figure(self):
        per_plan = self._published()["reference"]["shortfall"]["per_plan"]
        self.assertEqual(per_plan["max5"]["ratio"], round(11.0 / 12.63, 4))
        self.assertEqual(per_plan["max20"]["ratio"], round(6.5 / 7.58, 4))

    def test_the_expected_figure_applies_the_two_announced_multipliers_to_the_table(self):
        """The reconciliation's own arithmetic: 83.33M x 1.5 over 11.0M x 2."""
        per_plan = self._published()["reference"]["shortfall"]["per_plan"]
        self.assertEqual(per_plan["max20"]["expected_windows_per_week"],
                         round(83_333_300 * 1.5 / (11_000_000 * 2.0), 4))
        self.assertEqual(per_plan["max20"]["ratio_to_expected"],
                         round(6.5 / (83_333_300 * 1.5 / (11_000_000 * 2.0)), 4))

    def test_a_plan_with_no_measured_regime_publishes_a_status_and_not_a_scaled_figure(self):
        pro = self._published()["reference"]["shortfall"]["per_plan"]["pro"]
        self.assertIsNone(pro["measured_windows_per_week"])
        self.assertIsNone(pro["ratio"])
        self.assertIn("inferred from max20", pro["status"])

    def test_the_block_says_the_reconciliation_explains_it_and_quotes_what_it_found(self):
        shortfall = self._published()["reference"]["shortfall"]
        self.assertEqual(shortfall["status"], "explained")
        self.assertIn("predates the 6 May five-hour doubling", shortfall["explanation"])
        self.assertIn("not comparable to the measured levels", shortfall["explanation"])
        self.assertEqual(shortfall["source"], "docs/findings-2026-09-20-reconciliation.md")
        self.assertEqual(shortfall["plans_measured"], ["max5", "max20"])

    def test_a_plan_the_explanation_does_not_reach_leaves_the_status_open(self):
        import tracker.publish as publisher
        original = publisher.SHORTFALL_EXPLAINED_PLANS
        try:
            publisher.SHORTFALL_EXPLAINED_PLANS = ("pro",)
            shortfall = self._published()["reference"]["shortfall"]
        finally:
            publisher.SHORTFALL_EXPLAINED_PLANS = original
        self.assertEqual(shortfall["status"], "open")
        self.assertIn("does not reach every plan", shortfall["explanation"])

    def test_a_regime_still_running_across_the_cut_is_not_a_pre_cut_reading(self):
        """The test is the regime's own end stamp, not its place in the list."""
        from tracker.publish import _pre_cut_regime
        across = {"regimes": [{"end": "2026-09-20T00:00:00+00:00", "windows": 5.0}]}
        self.assertIsNone(_pre_cut_regime(across))
        before = {"regimes": [{"end": "2026-09-01T00:00:00+00:00", "windows": 6.5},
                              {"end": "2026-09-20T00:00:00+00:00", "windows": 5.0}]}
        self.assertEqual(_pre_cut_regime(before)["windows"], 6.5)

    def test_the_multipliers_are_the_ones_the_change_list_records(self):
        """PRE_CUT_MULTIPLIERS is named separately; it must not drift from the tuple."""
        from tracker.publish import PLAN_CHANGES_SINCE_REFERENCE, PRE_CUT_MULTIPLIERS
        five_hour = [c for c in PLAN_CHANGES_SINCE_REFERENCE if c["scope"] == "five_hour_window"]
        promotion = [c for c in PLAN_CHANGES_SINCE_REFERENCE
                     if c["scope"] == "weekly" and c.get("until") == "2026-09-13"]
        self.assertEqual([c["multiplier"] for c in five_hour], [PRE_CUT_MULTIPLIERS["five_hour_window"]])
        self.assertEqual([c["multiplier"] for c in promotion], [PRE_CUT_MULTIPLIERS["weekly"]])


class DollarSeriesAgreementTests(unittest.TestCase):
    """A dollar-series change is what two accounts saw, never what one pooled series did.

    The published 2026-09-20 "-23%" was an artefact of pooling: the detector ran on one
    series holding every account's verified daily readings, and the accounts sit at
    different levels, so which account happened to be busy moved the pooled median on its
    own. Across that date one account's own readings went UP and the other's went DOWN.
    Detection now runs per account and publishes only what two of them agree on.
    """

    NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)

    def published(self, **accounts):
        report = {"accounts": {}}
        for account, budgets in accounts.items():
            start_day = 1
            if isinstance(budgets, tuple):
                budgets, start_day = budgets
            report["accounts"].update(
                daily_report(budgets, start_day=start_day, account=account,
                             per_day=EVENT_STRETCHES_PER_DAY)["accounts"])
        return build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=report)

    def test_one_account_stepping_alone_publishes_no_change(self):
        j = self.published(dave=[15.0] * 10 + [10.5] * 5)
        self.assertEqual(j["events"], [])
        self.assertIsNone(j["last_change"])

    def test_two_accounts_stepping_in_opposite_directions_publish_no_change(self):
        j = self.published(dave=[15.0] * 10 + [10.5] * 5, jwork=[15.0] * 10 + [21.0] * 5)
        self.assertEqual(j["events"], [])

    def test_a_step_only_the_pooled_series_has_publishes_no_change(self):
        # The 20 September shape: each account is flat on its own, the two sit at
        # different levels, and which of them has readings on a day moves the pooled
        # median. The pooled series steps; neither account does.
        j = self.published(dave=[15.0] * 10, jwork=([30.0] * 10, 11))
        self.assertEqual(j["events"], [])

    def test_two_accounts_stepping_together_publish_one_change(self):
        j = self.published(dave=[15.0] * 10 + [10.5] * 5, jwork=[20.0] * 10 + [12.0] * 5)
        events = [e for e in j["events"] if e["scope"] == "window"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["date"], events[0]["direction"]), ("2026-09-11", "decreased"))
        # Dated at the earliest onset, bounded by the last reading at the old level.
        self.assertEqual(events[0]["onset"], {"earliest": "2026-09-10", "latest": "2026-09-11"})

    def test_the_percent_is_the_step_the_published_history_draws(self):
        # The two accounts' own detectors see -30% and -40%; the levels the page holds
        # pool both accounts, 17.5 then 11.25, which is -36%. The event must state the
        # step the chart draws, not a percent from a different pool.
        j = self.published(dave=[15.0] * 10 + [10.5] * 5, jwork=[20.0] * 10 + [12.0] * 5)
        event = [e for e in j["events"] if e["scope"] == "window"][0]
        hist = j["history"]["claude-sonnet-5"]
        before = [h["meter_budget_per_window"] for h in hist if h["date"] < event["date"]][-1]
        after = [h["meter_budget_per_window"] for h in hist if h["date"] >= event["date"]][0]
        self.assertEqual((before, after), (17.5, 11.25))
        self.assertEqual(event["percent"], round(abs(after / before - 1) * 100))
        self.assertEqual(event["percent"], 36)

    def test_onsets_more_than_three_days_apart_are_not_one_change(self):
        j = self.published(dave=[15.0] * 10 + [10.5] * 10, jwork=[15.0] * 15 + [10.5] * 5)
        self.assertEqual(j["events"], [])

    def test_an_account_with_too_little_meter_movement_is_not_a_second_account(self):
        # The minimum evidence is five-hour meter percent, not a count of readings: an
        # account needs 2 x MIN_STRETCH_PCT points of movement before the detector can
        # split it at all, so an account below that cannot agree with anything. At
        # EVENT_STRETCHES_PER_DAY x 10% a day, four days is 160 points and six is 240.
        stepped = [15.0] * 8 + [10.5] * 8
        short = self.published(dave=stepped, jwork=([15.0] * 2 + [10.5] * 2, 5))
        self.assertEqual(short["events"], [])
        testable = self.published(dave=stepped, jwork=([15.0] * 5 + [10.5] * 5, 5))
        self.assertEqual([e["date"] for e in testable["events"]], ["2026-09-09"])

    def test_the_agreement_rule_is_the_one_the_constant_states(self):
        from tracker.publish import DOLLAR_AGREEMENT_DAYS
        self.assertEqual(DOLLAR_AGREEMENT_DAYS.days, 3)


class PlanSeamTests(unittest.TestCase):
    """_plan_for_week: a week that opens on plan-change day belongs to neither plan."""

    def test_a_week_starting_on_plan_change_day_is_not_max20(self):
        from tracker.publish import PLAN_CHANGE, _plan_for_week
        seam = (PLAN_CHANGE + timedelta(days=7)).isoformat()
        self.assertIsNone(_plan_for_week(seam))
        self.assertEqual(_plan_for_week((PLAN_CHANGE + timedelta(days=8)).isoformat()), "max20")
        self.assertEqual(_plan_for_week(PLAN_CHANGE.isoformat()), "max5")

    def test_the_seam_week_is_published_by_neither_plan(self):
        from tracker.publish import PLAN_CHANGE
        weeks = [{"week_ending": (PLAN_CHANGE + timedelta(days=d)).isoformat(),
                  "windows": 7.24, "five_hour_pct": 100.0, "seven_day_pct": 14.0, "pieces": 3}
                 for d in (0, 7, 14)]
        passive = dict(PASSIVE, weekly_windows={"current": None, "history": weeks, "by_window": []})
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        ww = build_public_json([], passive, EFFORT, PRICES, now)["weekly_windows"]
        published = {h["week_ending"] for plan in ("max5", "max20") for h in ww[plan]["history"]}
        self.assertNotIn((PLAN_CHANGE + timedelta(days=7)).isoformat(), published)
        self.assertEqual(published, {PLAN_CHANGE.isoformat(),
                                     (PLAN_CHANGE + timedelta(days=14)).isoformat()})


class TokensPerWeekChangeTests(unittest.TestCase):
    """What a week's tokens did across the cut: windows per week compounded with the
    five-hour window, because the window grew while the week held fewer of them."""

    RATIO_NOTE: ClassVar[dict] = {"ratio_fell_pct": 21.8}
    ACROSS: ClassVar[dict] = {"per_account": {
        "a1": {"change_pct": 17.2, "n_with_capture": 0, "n_before": 166, "n_after": 32},
        "a2": {"change_pct": 8.6, "n_with_capture": 56, "n_before": 40, "n_after": 15},
        "a3": {"change_pct": None, "n_with_capture": 19, "n_before": 0, "n_after": 18}}}

    UNSET: ClassVar[object] = object()

    def change(self, note=UNSET, across=UNSET):
        from tracker.publish import _tokens_per_week_change
        return _tokens_per_week_change(self.RATIO_NOTE if note is self.UNSET else note,
                                       self.ACROSS if across is self.UNSET else across)

    def test_the_two_measured_changes_compound(self):
        self.assertEqual(self.change(), {
            "percent": 15, "direction": "decreased", "signed_pct": -15.1,
            "windows_per_week_pct": -21.8, "five_hour_window_pct": 8.6,
            "five_hour_accounts": ["a2"], "method": self.change()["method"]})
        self.assertIn("windows_per_week_pct", self.change()["method"])

    def test_the_arithmetic_is_the_published_numbers_own(self):
        out = self.change()
        expected = ((1 + out["windows_per_week_pct"] / 100)
                    * (1 + out["five_hour_window_pct"] / 100) - 1) * 100
        self.assertEqual(out["signed_pct"], round(expected, 1))
        self.assertEqual(out["percent"], round(abs(out["signed_pct"])))

    def test_a_rise_reads_as_a_rise(self):
        out = self.change(note={"ratio_fell_pct": -10.0},
                          across={"per_account": {"a2": {"change_pct": 5.0, "n_with_capture": 4,
                                                         "n_before": 10, "n_after": 10}}})
        self.assertEqual((out["direction"], out["signed_pct"], out["percent"]),
                         ("increased", 15.5, 16))

    def test_with_no_account_able_to_state_a_five_hour_change_there_is_no_figure(self):
        blind = {"per_account": {"a1": {"change_pct": 17.2, "n_with_capture": 0,
                                        "n_before": 166, "n_after": 32}}}
        self.assertIsNone(self.change(across=blind))
        for empty in (None, {}, {"per_account": {}}):
            with self.subTest(across=empty):
                self.assertIsNone(self.change(across=empty))

    def test_with_no_windows_per_week_change_there_is_no_figure(self):
        # Two regimes are what a before-and-after change is taken from; with one there
        # is no note, and nothing is published in its place.
        self.assertIsNone(self.change(note=None))

    def test_last_change_carries_the_same_object_as_the_event(self):
        from tracker.detect import ChangeEvent
        from tracker.publish import _build_events, _latest_change_with_scope
        weekly = {"max20": {
            "regimes": [{"start": "2026-09-01T00:00:00+00:00", "end": "2026-09-05T00:00:00+00:00"},
                        {"start": "2026-09-06T00:00:00+00:00", "end": "2026-09-10T00:00:00+00:00"}],
            "by_window": [{"window_ending": "2026-09-02T00:00:00+00:00", "five_hour_pct": 20.0,
                           "seven_day_pct": 10.0},
                          {"window_ending": "2026-09-07T00:00:00+00:00", "five_hour_pct": 10.0,
                           "seven_day_pct": 10.0}]}}
        event = ChangeEvent(date(2026, 9, 6), "decreased", 50)
        published = _build_events([], [event], self.ACROSS, weekly)
        last = _latest_change_with_scope([], [event], self.ACROSS, weekly)
        self.assertIsNotNone(published[0]["tokens_per_week_change"])
        self.assertEqual(last["tokens_per_week_change"], published[0]["tokens_per_week_change"])

    def test_a_provisional_event_carries_no_tokens_per_week_change_at_all(self):
        from tracker.detect import ChangeEvent
        from tracker.publish import _event_record
        out = _event_record(ChangeEvent(date(2026, 9, 6), "decreased", 50), "window",
                            self.ACROSS, None)
        self.assertNotIn("tokens_per_week_change", out)


class CreditValuedDetectionTests(unittest.TestCase):
    """The detector reads meter credits, so the model mix no longer moves the reading.

    These exercise tracker/gs_passive.py's `stretch_credits` and `passive_credit_points`
    from the publisher's side rather than from tests/test_gs_passive.py, because they are
    about what the publisher detects on: the eligibility rule and the day pooling are
    already covered there, and what is new here is the unit.
    """

    NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)

    @classmethod
    def setUpClass(cls):
        from tracker import credits as credit_model
        from tracker.publish import CREDITS
        cls.credits = CREDITS
        cls.model_rates = credit_model.load_model_rates()
        cls.rates = {fam: credit_model.family_rate(fam, cls.credits, cls.model_rates)
                     for fam in ("sonnet", "opus")}

    def mixed_report(self, *, accounts=("dave", "jwork"), per_day=4):
        """Ten days pure Sonnet then ten days pure Opus, sized so the CREDITS are flat.

        Both models are priced in PRICES, where Opus's cache_write is 2.5x Sonnet's, and
        the meter charges the two much closer than that. So the list-dollar daily series
        steps hard at day 11 and the credit series does not move at all: a pure model-mix
        artefact, the mechanism behind the withdrawn 20 September event.
        """
        opus_tokens = 1_000_000
        sonnet_tokens = round(opus_tokens * self.rates["opus"].input / self.rates["sonnet"].input)
        report = {"accounts": {}}
        for account in accounts:
            stretches = []
            for day in range(1, 21):
                model, tok = (("claude-sonnet-5", sonnet_tokens) if day <= 10
                              else ("claude-opus-5", opus_tokens))
                for k in range(per_day):
                    stretches.append({
                        "status": "accepted", "end": f"2026-09-{day:02d}T{8 + k:02d}:00:00+00:00",
                        "delta_pct": 10, "windows": 1, "reset_verified": True,
                        "tokens": {model: {"input": 0, "output": 0, "cache_read": 0, "cache_write": tok}}})
            report["accounts"][account] = {"account": account, "stretches": stretches}
        return report

    def test_a_pure_model_mix_step_publishes_no_change(self):
        report = self.mixed_report()
        # The list-dollar series really does step here: this is the artefact, not a
        # series that happens to be flat in both units.
        dollars = passive_dollar_readings({"accounts": {"dave": report["accounts"]["dave"]}},
                                          PRICES, by="day")
        before = median([v for ts, v in dollars if ts.day <= 10])
        after = median([v for ts, v in dollars if ts.day > 10])
        # How hard it steps depends on the measured Sonnet rate, because `mixed_report`
        # sizes the Sonnet days by it: the cheaper Sonnet is measured to be, the more
        # Sonnet tokens buy the same credits and the smaller the dollar step. The refit of
        # 2026-09-23 moved it from 1.55 to 1.49. The claim under test is that the dollar
        # series steps hard while the credit series below does not move at all, so the
        # bound is well clear of flat rather than tight against whatever the rate is today.
        self.assertGreater(after / before, 1.4)
        # The credit series does not move, so nothing is published.
        j = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=report)
        self.assertIsNone(window_event(j))

    def test_the_same_shape_with_a_real_credit_step_does_publish(self):
        # The control: halve the second half's tokens and the credit series steps too.
        report = self.mixed_report()
        for account in report["accounts"].values():
            for st in account["stretches"]:
                if st["end"][:10] > "2026-09-10":
                    st["tokens"]["claude-opus-5"]["cache_write"] //= 2
        j = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=report)
        event = window_event(j)
        self.assertIsNotNone(event)
        self.assertEqual((event["date"], event["direction"]), ("2026-09-11", "decreased"))

    def test_credit_points_are_one_per_stretch_and_weighted_by_delta_pct(self):
        report = self.mixed_report(accounts=("dave",), per_day=4)
        points = passive_credit_points(report, PRICES, self.credits, self.model_rates)
        self.assertEqual(len(points), 80)
        self.assertEqual(sum(p[2] for p in points), 800.0)
        # Flat in credits: every stretch carries the same credits per percent.
        self.assertEqual(len({round(p[1] / p[2]) for p in points}), 1)

    def test_a_stretch_records_which_rate_source_priced_it(self):
        opus = {"claude-opus-5": {"input": 1000, "output": 0, "cache_read": 0, "cache_write": 0}}
        sonnet = {"claude-sonnet-5": {"input": 1000, "output": 0, "cache_read": 0, "cache_write": 0}}
        value, source = stretch_credits(opus, self.credits, self.model_rates)
        self.assertEqual(source, "anchor")
        self.assertAlmostEqual(value, 1000 * self.rates["opus"].input)
        self.assertEqual(stretch_credits({**opus, **sonnet}, self.credits, self.model_rates)[1], "measured")
        # A family with no measured row at all falls back to the reference table; the
        # anchor is definitional and survives having no measurement.
        self.assertEqual(stretch_credits(opus, self.credits, {"per_family": {}})[1], "anchor")
        self.assertEqual(stretch_credits(sonnet, self.credits, {"per_family": {}})[1], "reference_table")
        # A model in no family at all cannot be priced, and the stretch is dropped.
        self.assertEqual(stretch_credits({"some-other-vendor-model": opus["claude-opus-5"]},
                                         self.credits, self.model_rates), (None, "unpriced"))

    def test_the_evidence_names_the_series_detection_ran_on(self):
        j = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=self.mixed_report())
        block = j["rates"]["claude-sonnet-5"]["evidence"]["credit_detection"]
        self.assertEqual(block["series"], "credits_per_window")
        self.assertEqual((block["points"], block["meter_pct"]), (160, 1600.0))
        self.assertEqual(block["min_meter_pct_per_side"], MIN_STRETCH_PCT)
        self.assertEqual(block["testable_accounts"], 2)
        self.assertEqual(set(block["rate_sources"]), {"anchor", "measured"})
