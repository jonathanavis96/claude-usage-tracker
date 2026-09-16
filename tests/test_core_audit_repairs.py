import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tracker import detect
from tracker.capture import judge
from tracker.detect import detect_smoothed_changes, detect_weighted_changes
from tracker.gs_passive import publishable
from tracker.join import Stretch
from tracker.publish import build_public_json, meter_usd, tokens_usd
from tracker.turns import Turn, iter_turns
from tracker.weekly import weekly_windows

T0 = datetime(2026, 8, 20, tzinfo=timezone.utc)
MODEL = "claude-sonnet-5"
PRICES = {
    MODEL: {
        "input": 2, "output": 10, "cache_read": 0.2,
        "cache_write": 2.5, "cache_write_1h": 4,
        "meter_weight": 1,
        "class_weight": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 1},
    }
}


class PairedMeterRepairTests(unittest.TestCase):
    def test_denominator_only_movement_is_retained(self):
        rows = []
        for i, (five, seven) in enumerate(((0, 0), (30, 4), (30, 6), (60, 10))):
            rows.append({
                "ts": (T0 + timedelta(minutes=i)).isoformat(),
                "five_hour": five, "seven_day": seven,
                "five_resets_at": (T0 + timedelta(hours=5)).isoformat(),
                "seven_resets_at": (T0 + timedelta(days=7)).isoformat(),
            })
        result = weekly_windows(rows, now=T0 + timedelta(days=30))
        self.assertEqual(result["history"][0]["windows"], 6.0)
        self.assertEqual(result["history"][0]["seven_day_pct"], 10.0)

    def test_independent_rounding_errors_do_not_invent_a_step(self):
        # Ten (11, 1) windows then two (47, 8): every reading is within a point of a
        # constant ratio of 6 if every d7 rounded the same way. Independent rounding
        # cannot exclude such a run, so the refusal rests on the floor: 10 points of
        # d7 before the split and 16 after, both under 20. With the floors lowered
        # to 10 the rounding intervals alone separate and the same points certify.
        points = [(T0 + timedelta(hours=6 * i), 11, 1) for i in range(10)]
        points += [(T0 + timedelta(hours=6 * i), 47, 8) for i in range(10, 12)]
        self.assertEqual((sum(p[2] for p in points[:10]), sum(p[2] for p in points[10:])), (10, 16))
        self.assertLess(16, min(detect.MIN_BASE_D7, detect.MIN_POOL_D7))
        self.assertEqual(detect_weighted_changes(points), [])
        with mock.patch.object(detect, "MIN_BASE_D7", 10.0), mock.patch.object(detect, "MIN_POOL_D7", 10.0):
            self.assertEqual([(e.direction, e.percent) for e in detect_weighted_changes(points)], [("decreased", 47)])

    def test_the_rounding_example_scaled_past_the_floor_certifies(self):
        # The same example with twenty (11, 1) windows and three (47, 8): 20 and 24
        # points of d7 clear the floor, and under independent rounding a same-sign
        # run that long is not a concession the method makes, so it is evidence of
        # a change.
        points = [(T0 + timedelta(hours=6 * i), 11, 1) for i in range(20)]
        points += [(T0 + timedelta(hours=6 * i), 47, 8) for i in range(20, 23)]
        events = detect_weighted_changes(points)
        self.assertEqual([(e.direction, e.percent, e.date) for e in events],
                         [("decreased", 47, points[20][0].date())])
        self.assertLess(events[0].after_interval[1], events[0].before_interval[0])

    def test_disconnected_pairs_keep_separate_rounding_error_budgets(self):
        reset5 = (T0 + timedelta(hours=5)).isoformat()
        reset7 = (T0 + timedelta(days=7)).isoformat()
        rows = [
            {"ts": "a", "five_hour": 0, "seven_day": 0, "five_resets_at": reset5, "seven_resets_at": reset7},
            {"ts": "b", "five_hour": 30, "seven_day": 5, "five_resets_at": reset5, "seven_resets_at": reset7},
            None,
            {"ts": "c", "five_hour": 30, "seven_day": 5, "five_resets_at": reset5, "seven_resets_at": reset7},
            {"ts": "d", "five_hour": 60, "seven_day": 10, "five_resets_at": reset5, "seven_resets_at": reset7},
        ]
        result = weekly_windows(rows, now=T0 + timedelta(days=30))
        self.assertEqual(result["history"][0]["pieces"], 2)
        # Both pieces lie in one five-hour window, so they publish as one window point
        # that carries both rounding budgets in `pieces` and its interval, rather than
        # as two points (the per-window series is one point per window everywhere).
        self.assertEqual([(p["pieces"], p["seven_day_pct"]) for p in result["by_window"]], [(2, 10.0)])
        connected = weekly_windows([rows[0], rows[1], rows[4]], now=T0 + timedelta(days=30))
        self.assertEqual(connected["by_window"][0]["pieces"], 1)
        (lo2, hi2), (lo1, hi1) = result["by_window"][0]["rounding_interval"], connected["by_window"][0]["rounding_interval"]
        self.assertTrue(lo2 < lo1 and hi2 > hi1)

    def test_persistent_segment_sets_the_onset_not_a_stray_low_day(self):
        values = [100] * 10 + [80, 100, 100] + [70] * 12
        events = detect_smoothed_changes([(T0 + timedelta(days=i), v) for i, v in enumerate(values)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].date, (T0 + timedelta(days=13)).date())


class CollectionRepairTests(unittest.TestCase):
    def test_any_positive_unpriced_work_withholds_a_monetary_stretch(self):
        stretch = Stretch(T0, T0 + timedelta(minutes=10), delta_pct=10)
        stretch.add(Turn(T0, MODEL, 100_000, 0, 1_000_000, 0), PRICES)
        stretch.add(Turn(T0, "claude-unknown", 0, 40_000, 0, 0), PRICES)
        verdict = judge([stretch])[0]
        self.assertIn("claude-unknown", stretch.unpriced)
        self.assertFalse(publishable(verdict))

    def test_cache_write_one_hour_is_a_subset_but_gets_its_price_premium(self):
        tokens = {"input": 0, "output": 0, "cache_read": 0,
                  "cache_write": 1_000_000, "cache_write_1h": 1_000_000}
        self.assertEqual(tokens_usd(tokens, PRICES[MODEL]), 4.0)
        self.assertEqual(meter_usd(tokens, PRICES[MODEL]), 4.0)

    def test_turn_parser_retains_cache_duration_without_double_counting_total(self):
        usage = {
            "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 60},
        }
        row = {"type": "assistant", "timestamp": T0.isoformat(),
               "message": {"id": "m", "model": MODEL, "usage": usage}}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "turn.jsonl"
            p.write_text(json.dumps(row) + "\n")
            turn = next(iter_turns([p]))
        self.assertEqual((turn.cache_write, turn.cache_write_1h, turn.total), (100, 60, 100))


class PublicationRepairTests(unittest.TestCase):
    def test_no_eligible_passive_measurement_emits_nullable_rates(self):
        result = build_public_json([], {}, {}, PRICES, T0, gs_passive=None)
        rate = result["rates"][MODEL]
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["rate_basis"], "meter_budget")
        self.assertIsNone(rate["meter_budget_per_window"])
        self.assertIsNone(rate["api_value_per_window"])
        self.assertEqual(rate["quality"]["status"], "unavailable")
        self.assertEqual(result["events"], [])

    def test_archived_resetless_evidence_is_conditional_and_never_fires_events(self):
        stretches = []
        for i, usd in enumerate((1.0,) * 8 + (0.5,) * 8):
            stretches.append({
                "start": (T0 + timedelta(days=i)).isoformat(),
                "end": (T0 + timedelta(days=i, minutes=10)).isoformat(),
                "delta_pct": 10, "status": "accepted", "unpriced_tokens": 0,
                "tokens": {MODEL: {"input": int(usd / 2 * 1_000_000), "output": 0,
                                     "cache_read": 0, "cache_write": 0}},
            })
        report = {"accounts": {"legacy": {"stretches": stretches, "meter": {"last": stretches[-1]["end"]}}}}
        result = build_public_json([], {}, {}, PRICES, T0 + timedelta(days=16), gs_passive=report)
        rate = result["rates"][MODEL]
        self.assertEqual(rate["quality"]["status"], "conditional")
        self.assertFalse(rate["evidence"]["reset_verified"])
        self.assertEqual(result["events"], [])
        self.assertIsNotNone(rate["meter_budget_per_window"])

    def test_meter_budget_and_actual_api_list_value_use_distinct_units(self):
        # Cache reads contribute API list value but have zero meter weight.
        split = {"input": 0.1, "output": 0, "cache_read": 0.9, "cache_write": 0}
        tokens = 10_000_000
        meter = meter_usd({k: int(tokens * v) for k, v in split.items()}, PRICES[MODEL])
        api = tokens_usd({k: int(tokens * v) for k, v in split.items()}, PRICES[MODEL])
        self.assertEqual(meter, 2.0)
        self.assertEqual(api, 3.8)

    def test_below_threshold_history_does_not_blend_the_current_estimate(self):
        stretches = []
        for i, level in enumerate([100] * 20 + [90] * 20):
            # delta=10: input-token list/meter dollars of level/10 produces
            # `level` dollars per full 100% window.
            input_tokens = int((level / 10) / 2 * 1_000_000)
            stretches.append({
                "start": (T0 + timedelta(days=i)).isoformat(),
                "end": (T0 + timedelta(days=i, minutes=10)).isoformat(),
                "delta_pct": 10, "windows": 1, "status": "accepted",
                "capture_status": "accepted", "reset_verified": True,
                "unpriced_tokens": 0,
                "tokens": {MODEL: {"input": input_tokens, "output": 0,
                                     "cache_read": 0, "cache_write": 0}},
            })
        report = {"accounts": {"a": {"stretches": stretches, "meter": {"last": stretches[-1]["end"]}}}}
        result = build_public_json([], {}, {}, PRICES, T0 + timedelta(days=40), gs_passive=report)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["rates"][MODEL]["meter_budget_per_window"], 90.0)
        self.assertEqual({h["meter_budget_per_window"] for h in result["history"][MODEL]}, {90.0, 100.0})


if __name__ == "__main__":
    unittest.main()
