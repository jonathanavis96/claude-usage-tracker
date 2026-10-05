"""The weekly limit measured directly on the seven-day meter (tracker/weekly_meter.py)."""
import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from tracker import credits as C
from tracker import weekly_meter as W
from tracker.cloud_sessions import CloudSpan
from tracker.samples import Sample
from tracker.turns import Turn

T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)
PRICES = {"claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}}


def dt(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def sample(m, five, seven, resets="R1", seven_resets="W1"):
    return Sample(dt(m), five, seven, resets, "test", seven_resets)


def turn(m, output, remote=False, model="claude-opus-5"):
    return Turn(dt(m), model, 0, output, 0, 0, remote=remote)


def outputs(step):
    return sum(b.get("output", 0) for b in step["tokens"].values())


class SevenDayStepTests(unittest.TestCase):
    def test_steps_tile_the_crossings_with_the_turns_and_five_hour_points_in_each(self):
        samples = [sample(0, 10, 20), sample(5, 12, 21), sample(10, 13, 21), sample(15, 15, 22),
                   sample(20, 16, 23)]
        turns = [turn(1, 100), turn(6, 10), turn(12, 1), turn(16, 1000), turn(21, 5)]
        steps = W.seven_day_steps(samples, turns, PRICES)
        # Seven-day crossings 21 (at 5), 22 (at 15), 23 (at 20): two one-point steps.
        self.assertEqual([(s["start"], s["end"]) for s in steps],
                         [(dt(5).isoformat(), dt(15).isoformat()),
                          (dt(15).isoformat(), dt(20).isoformat())])
        self.assertEqual([s["seven_day"] for s in steps], [22, 23])
        # A turn belongs to the step holding it, [start, end): 10 and 1, then 1000.
        self.assertEqual([outputs(s) for s in steps], [11, 1000])
        # Five-hour crossings by their upper reading: 11 and 12 (at 5) and 13 (at 10) in the
        # first, 14 and 15 (at 15) in the second; 16 (at 20) opens the next, unclosed one.
        self.assertEqual([s["d5"] for s in steps], [3, 2])
        self.assertEqual([s["d7"] for s in steps], [1, 1])
        self.assertEqual([s["turns"] for s in steps], [2, 1])

    def test_a_two_point_jump_gives_an_empty_step_and_the_chain_stays_exact(self):
        samples = [sample(0, 0, 10), sample(5, 1, 11), sample(10, 2, 13), sample(15, 3, 14)]
        steps = W.seven_day_steps(samples, [turn(7, 50), turn(12, 7)], PRICES)
        self.assertEqual([s["seven_day"] for s in steps], [12, 13, 14])
        self.assertEqual([outputs(s) for s in steps], [50, 0, 7])
        self.assertEqual(steps[1]["start"], steps[1]["end"])

    def test_no_step_spans_a_weekly_reset_or_a_gap(self):
        samples = [sample(0, 0, 50), sample(5, 0, 51), sample(10, 0, 2, seven_resets="W2"),
                   sample(15, 0, 3, seven_resets="W2"), sample(60, 0, 4, seven_resets="W2"),
                   sample(65, 0, 5, seven_resets="W2")]
        steps = W.seven_day_steps(samples, [], PRICES)
        # 51 alone before the reset, 3 alone before the gap, 5 alone after it: no pairs.
        self.assertEqual(steps, [])

    def test_cloud_work_marks_the_step(self):
        samples = [sample(0, 0, 10), sample(5, 0, 11), sample(10, 0, 12), sample(15, 0, 13)]
        span = CloudSpan(session="s", start=dt(11), end=dt(12), source="launch")
        steps = W.seven_day_steps(samples, [turn(6, 5, remote=True)], PRICES, cloud=[span])
        self.assertEqual([s["cloud_session"] for s in steps], [True, True])
        self.assertEqual(steps[0]["remote_sourced_turns"], 1)
        self.assertFalse(W.seven_day_steps(samples, [turn(6, 5)], PRICES)[0]["cloud_session"])


def step(start, credits, hours=1.0, d5=5, cloud=False):
    return {"start": start.isoformat(), "end": (start + timedelta(hours=hours)).isoformat(),
            "d7": 1, "d5": d5, "tokens": {"v": {"output": credits}}, "cloud_session": cloud}


def value(tokens):
    return float(sum(b.get("output", 0) for b in tokens.values()))


class SelectionTests(unittest.TestCase):
    def test_harness_runs_cloud_steps_and_early_masterrig_are_left_out(self):
        day = datetime(2026, 9, 10, tzinfo=timezone.utc)
        run = C.HarnessRun("jwork", day + timedelta(hours=1, minutes=30), day + timedelta(hours=2),
                           "probe")
        by = {"jwork": [step(day, 1), step(day + timedelta(hours=1), 2),
                        step(day + timedelta(hours=3), 3, cloud=True)],
              "masterrig": [step(C.MASTERRIG_FROM - timedelta(hours=2), 4),
                            step(C.MASTERRIG_FROM, 5)]}
        kept = W.clean_steps(by, [run])
        self.assertEqual([value(s["tokens"]) for s in kept["jwork"]], [1])
        self.assertEqual([value(s["tokens"]) for s in kept["masterrig"]], [5])

    def test_a_step_over_work_the_transcripts_did_not_see_is_left_out(self):
        # The capture check withheld the stretch (status not accepted): the meter moved with
        # work no transcript on this host holds, so its steps read too few credits per point.
        # Every stretch figure leaves such a stretch out; so does the weekly limit.
        day = datetime(2026, 9, 13, tzinfo=timezone.utc)
        by = {"jwork": [step(day, 900), step(day + timedelta(hours=1), 50),
                        step(day + timedelta(hours=2), 60), step(day + timedelta(hours=4), 950)]}
        stretches = {"jwork": [
            {"start": (day + timedelta(minutes=50)).isoformat(),
             "end": (day + timedelta(hours=3)).isoformat(), "status": "unaccounted"},
            {"start": (day + timedelta(hours=3)).isoformat(),
             "end": (day + timedelta(hours=6)).isoformat(), "status": "accepted"}]}
        kept = W.clean_steps(by, [], stretches)
        self.assertEqual([value(s["tokens"]) for s in kept["jwork"]], [950])
        # Without the stretch record nothing is gated.
        self.assertEqual(len(W.clean_steps(by, [])["jwork"]), 4)

    def test_steps_come_from_every_report_and_an_old_report_has_none(self):
        gs = {"accounts": {"jwork": {"weekly_steps": [step(T0, 1)]}, "dave": {}}}
        mr = {"accounts": {"masterrig": {"weekly_steps": [step(T0, 2)]}}}
        self.assertEqual(sorted(W.steps_by_account(gs, mr, None)), ["jwork", "masterrig"])

    def test_an_unpriced_step_is_dropped_and_an_empty_one_kept_at_zero(self):
        rows = W.valued([step(T0, 0), {**step(T0, 1), "tokens": {"x": {}}}],
                        lambda t: None if "x" in t else value(t))
        self.assertEqual([r["credits"] for r in rows], [0.0])
        self.assertEqual(rows[0]["day"], "2026-09-20")

    def test_the_day_is_the_utc_day_of_the_steps_end(self):
        late = {"start": "2026-09-21T01:00:00+02:00", "end": "2026-09-21T01:30:00+02:00",
                "d7": 1, "d5": 0, "tokens": {}}
        self.assertEqual(W.valued([late], value)[0]["day"], "2026-09-20")


def rows_for(levels_by_day, d5=5):
    """Valued steps: one list of per-step credits per UTC day, from T0."""
    out = []
    for k, day in enumerate(levels_by_day):
        for i, c in enumerate(day):
            start = T0 + timedelta(days=k, hours=i)
            out.append({"start": start, "end": start + timedelta(minutes=30),
                        "day": start.date().isoformat(), "credits": float(c), "d7": 1, "d5": d5})
    return out


class LevelTests(unittest.TestCase):
    def test_the_pooled_level_is_meter_weighted_with_a_day_bootstrap_interval(self):
        a = rows_for([[100, 120], [80, 100], [110, 90]])            # 600 / 6
        b = rows_for([[300, 300, 300, 300], [300, 300, 300, 300]])  # 2400 / 8
        lvl = W.level({"a1": a, "a2": b}, "t")
        self.assertAlmostEqual(lvl["credits_per_pct"], 3000 / 14)
        self.assertEqual((lvl["seven_day_points"], lvl["days"]), (14, 5))
        self.assertEqual(lvl["accounts"], ["a1", "a2"])
        lo, hi = lvl["interval"]
        self.assertLessEqual(lo, lvl["credits_per_pct"])
        self.assertGreaterEqual(hi, lvl["credits_per_pct"])
        self.assertAlmostEqual(lvl["per_account"]["a1"]["credits_per_pct"], 100.0)
        self.assertEqual(lvl["per_account"]["a2"]["interval"], [300.0, 300.0])
        self.assertAlmostEqual(lvl["windows_per_week_meters"], 5.0)
        self.assertEqual(W.level({"a1": a, "a2": b}, "t"), lvl)  # reproducible

    def test_no_steps_is_no_level(self):
        lvl = W.level({"a1": []}, "t")
        self.assertIsNone(lvl["credits_per_pct"])
        self.assertIsNone(lvl["interval"])
        self.assertEqual(lvl["per_account"], {})


def noisy_days(level, n_days, rng, per_day=6, sd=0.15):
    return [[level * math.exp(rng.gauss(0, sd)) for _ in range(per_day)] for _ in range(n_days)]


def sides(before_level, after_level, rng, n_before=6, n_after=4):
    before = rows_for(noisy_days(before_level, n_before, rng))
    after = rows_for(noisy_days(after_level, n_after, rng))
    shift = timedelta(days=n_before)
    for r in after:
        r["start"] += shift
        r["end"] += shift
        r["day"] = r["start"].date().isoformat()
    return before, after


class WeeklyChangeTests(unittest.TestCase):
    def test_a_shared_step_is_certified_on_every_account(self):
        rng = random.Random(3)
        out = W.weekly_change({"a1": sides(1000, 1300, rng), "a2": sides(1500, 1950, rng),
                               "a3": sides(800, 1040, rng)}, "shared")
        self.assertAlmostEqual(out["change_pct"], 30.0, delta=6.0)
        self.assertEqual(out["accounts_combined"], ["a1", "a2", "a3"])
        self.assertEqual(out["plan_wide"]["state"], "passed", out["plan_wide"]["reason"])
        self.assertTrue(out["certified"])
        self.assertEqual(out["unit"], "credits per 1% of the seven-day meter")
        self.assertAlmostEqual(sum(r["weight"] for r in out["per_account"].values()), 1.0, places=3)

    def test_a_step_on_one_account_is_not_plan_wide(self):
        rng = random.Random(4)
        out = W.weekly_change({"a1": sides(1000, 1600, rng), "a2": sides(1500, 1500, rng)}, "one")
        self.assertEqual(out["plan_wide"]["state"], "failed")
        self.assertFalse(out["plan_wide"]["without"]["a1"]["holds"])
        self.assertFalse(out["certified"])

    def test_an_account_needs_a_before_side_and_two_days_each_side(self):
        rng = random.Random(5)
        before, after = sides(1000, 1300, rng, n_before=6, n_after=1)
        out = W.weekly_change({"a1": (before, after), "a2": ([], after)}, "thin")
        self.assertEqual(out["accounts_combined"], [])
        self.assertFalse(out["per_account"]["a1"]["combined"])
        self.assertEqual(out["per_account"]["a2"]["n_before"], 0)
        self.assertIsNone(out["change_pct"])
        self.assertEqual(out["plan_wide"]["state"], "untested")
        self.assertFalse(out["certified"])

    def test_no_change_is_not_certified(self):
        rng = random.Random(6)
        out = W.weekly_change({"a1": sides(1000, 1000, rng), "a2": sides(1500, 1500, rng)}, "flat")
        lo, hi = out["interval_pct"]
        self.assertLess(lo, 0.0)
        self.assertGreater(hi, 0.0)
        self.assertFalse(out["certified"])


if __name__ == "__main__":
    unittest.main()
