"""Regression guards for the Codex audit of 2026-09-16 findings that had no direct test.

Each test replays the audit's own counterexample (audit_checks.py, preserved in the
parent workspace at .agents/codex/claude-usage-tracker-audit-2026-09-16/) against the
current code and asserts the repaired behaviour. Findings already covered by
tests/test_core_audit_repairs.py are not repeated here. See
docs/reliability-2026-10-02/audit-closure.md for the per-finding status.
"""
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import contributed, detect
from tracker.publish import _model_plan_limits, _regime_current

T0 = datetime(2026, 8, 20, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 16, 14, tzinfo=timezone.utc)
MODEL = "claude-sonnet-5"
PRICES = {
    MODEL: {
        "input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5,
        "meter_weight": 1,
        "class_weight": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 1},
    }
}
ROOT = Path(__file__).resolve().parent.parent


def counts(**kw):
    return {c: kw.get(c, 0) for c in ("input", "output", "cache_read", "cache_write")}


def contributor_row(**over):
    row = {
        "contributor_id": "11111111-1111-4111-8111-111111111111", "plan": "max20",
        "ts": NOW.isoformat(),
        "five_hour": {"utilization": 10, "resets_at": (NOW + timedelta(hours=1)).isoformat()},
        "seven_day": {"utilization": 10, "resets_at": (NOW + timedelta(days=1)).isoformat()},
        "tokens_since_five_hour_reset": {MODEL: counts(output=1_000_000)},
        "tokens_since_seven_day_reset": {MODEL: counts(output=6_000_000, cache_read=94_000_000)},
    }
    row.update(over)
    return row


class Finding2ModelEligibility(unittest.TestCase):
    def test_fable_is_not_included_on_pro_and_half_the_week_on_max(self):
        limits = _model_plan_limits({"claude-fable-5-1": {}, MODEL: {}})
        fable = limits["claude-fable-5-1"]
        self.assertFalse(fable["pro"]["included"])
        self.assertEqual(fable["pro"]["weekly_fraction"], 0.0)
        self.assertEqual((fable["max5"]["weekly_fraction"], fable["max20"]["weekly_fraction"]), (0.5, 0.5))
        self.assertTrue(all(limits[MODEL][p]["included"] and limits[MODEL][p]["weekly_fraction"] == 1.0
                            for p in ("pro", "max5", "max20")))


class Finding4CumulativeStep(unittest.TestCase):
    def test_a_cut_made_of_light_windows_is_detected_and_levels_split(self):
        # Audit: 4 windows at 60/10 then 21 at 18/4 produced no event and one flat 4.98.
        points = [(T0 + timedelta(days=i), 60, 10) for i in range(4)]
        points += [(T0 + timedelta(days=i), 18, 4) for i in range(4, 25)]
        events = detect.detect_weighted_changes(points)
        self.assertEqual([(e.direction, e.percent) for e in events], [("decreased", 25)])
        self.assertEqual([r["windows"] for r in detect.weighted_regimes(points)], [6.0, 4.5])
        self.assertIsNotNone(events[0].onset_earliest)
        self.assertIsNotNone(events[0].before_interval)


class Finding7ContributorWeeklyRatio(unittest.TestCase):
    def test_points_never_carry_a_token_quotient_windows_figure(self):
        # Audit: the same capacities with a cache-heavy week published 100 windows/week.
        point = contributed._points({"c": [contributor_row()]}, PRICES, NOW)[0]
        self.assertNotIn("windows", point)


class Finding8MissingModelIsMissing(unittest.TestCase):
    def test_an_unpriced_model_drops_the_by_model_map_rather_than_relabelling_the_total(self):
        row = contributor_row(tokens_since_seven_day_reset={
            MODEL: counts(output=1000), "claude-mystery-9": counts(output=10_000_000)})
        point = contributed._points({"c": [row]}, PRICES, NOW)[0]
        self.assertNotIn("tokens_per_pct_week_by_model", point)
        self.assertIn(MODEL, point["tokens_per_pct_by_model"])


class Finding9SamplerKeepsUnknownModels(unittest.TestCase):
    def test_sampler_keeps_every_model_id_it_sees(self):
        spec = importlib.util.spec_from_file_location("contrib_sample", ROOT / "contrib" / "sample.py")
        sample = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sample)
        turns = [(NOW, MODEL, counts(output=1000)), (NOW, "claude-fable-5", counts(output=1000)),
                 (NOW, "claude-haiku-4-5-20251001", counts(output=1000))]
        got = sample.tokens_since(turns, NOW - timedelta(hours=1), NOW)
        self.assertEqual(len(got), 3)
        self.assertEqual(sum(v["output"] for v in got.values()), 3000)

    def test_contributor_value_is_withheld_when_any_work_is_unpriced(self):
        row = contributor_row(tokens_since_five_hour_reset={
            MODEL: counts(output=1_000_000), "claude-mystery-9": counts(output=1)})
        self.assertIsNone(contributed._sample_values(row, PRICES))


class Finding16WeeklyFreshness(unittest.TestCase):
    def test_a_stopped_weekly_log_is_stale_against_the_publish_clock(self):
        # Audit: the weekly current anchored its lookback to its own newest point,
        # so a stopped weekly collector stayed "current" indefinitely.
        points = [(T0 + timedelta(hours=6 * i), 30, 5, 1) for i in range(12)]
        fresh = _regime_current(points, now=points[-1][0] + timedelta(hours=1))
        stopped = _regime_current(points, now=points[-1][0] + timedelta(days=30))
        self.assertFalse(fresh["stale"])
        self.assertTrue(stopped["stale"])
        self.assertIn("evidence_stale", stopped["reasons"])
        self.assertEqual(fresh["value"], stopped["value"])


if __name__ == "__main__":
    unittest.main()
