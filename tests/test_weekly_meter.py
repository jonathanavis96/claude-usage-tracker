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

    def test_whole_history_reads_masterrig_back_past_its_start_less_the_phantom(self):
        # The 14 September weekly before side reads masterrig's history before MASTERRIG_FROM,
        # all but the 2-5 September takeoff phantom; every other account is untouched.
        lo, hi = C.MASTERRIG_PHANTOM
        by = {"masterrig": [step(lo - timedelta(days=3), 1), step(lo - timedelta(minutes=30), 2),
                            step(lo + timedelta(days=1), 3), step(hi - timedelta(minutes=30), 4),
                            step(hi, 5)],
              "jwork": [step(lo, 6)]}
        whole = W.clean_steps(by, [], whole_history=True)
        self.assertEqual([value(s["tokens"]) for s in whole["masterrig"]], [1, 5])
        self.assertEqual([value(s["tokens"]) for s in whole["jwork"]], [6])
        self.assertEqual([value(s["tokens"]) for s in W.clean_steps(by, [])["masterrig"]], [5])

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


def regime(start, end):
    return {"start": start.isoformat(), "end": end.isoformat()}


def day_rows(first_day, n_days, level, rng, per_day=6):
    """Valued steps, `per_day` a day from `first_day` (UTC) for `n_days`, about `level`."""
    out = []
    for k, day in enumerate(noisy_days(level, n_days, rng, per_day, sd=0.05)):
        for i, c in enumerate(day):
            start = first_day + timedelta(days=k, hours=i)
            out.append({"start": start, "end": start + timedelta(minutes=30),
                        "day": start.date().isoformat(), "credits": c, "d7": 1, "d5": 5})
    return out


class CutBeforeSideTests(unittest.TestCase):
    """ADR 0001 rule 13: the 14 September weekly before side starts at the account's last
    certified boundary and reads every clean step from there."""

    AUG = datetime(2026, 8, 15, tzinfo=timezone.utc)
    STEP = datetime(2026, 9, 14, 11, 30, tzinfo=timezone.utc)

    def block(self, *starts):
        """An own windows-per-week block whose regimes start at `starts`, stepped at the last."""
        ends = [*(s - timedelta(minutes=10) for s in starts[1:]), datetime(2026, 10, 5, tzinfo=timezone.utc)]
        return {"regimes": [regime(s, e) for s, e in zip(starts, ends)],
                "step": {"onset": starts[-1].date().isoformat()}}

    def test_the_first_regimes_start_is_no_boundary(self):
        from tracker.passive import PLAN_CHANGE_AT
        self.assertEqual(C.cut_before_start(None), PLAN_CHANGE_AT)
        self.assertEqual(C.cut_before_start(self.block(self.AUG, self.STEP)), PLAN_CHANGE_AT)
        # A regime with no step after the cut bounds nothing either.
        late = datetime(2026, 9, 18, tzinfo=timezone.utc)
        self.assertEqual(C.cut_before_start({"regimes": [regime(late, late + timedelta(days=9))],
                                             "step": None}), PLAN_CHANGE_AT)

    def test_a_certified_boundary_before_the_step_starts_the_side(self):
        mid = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertEqual(C.cut_before_start(self.block(self.AUG, mid, self.STEP)), mid)
        # Without an own step, a boundary before the cut still starts it.
        no_step = dict(self.block(self.AUG, mid), step=None)
        self.assertEqual(C.cut_before_start(no_step), mid)

    def test_the_weekly_before_side_reads_back_to_the_boundary(self):
        from tracker.passive import PLAN_CHANGE_AT
        rng = random.Random(7)
        early = day_rows(PLAN_CHANGE_AT - timedelta(days=4), 3, 5000.0, rng)  # the Max 5x plan
        before = day_rows(datetime(2026, 8, 20, tzinfo=timezone.utc), 10, 1000.0, rng)
        after = day_rows(datetime(2026, 9, 15, tzinfo=timezone.utc), 5, 900.0, rng)
        max20 = {"by_account": {"a1": self.block(self.AUG, self.STEP)}}
        out = C.cut_direct_tests({}, [], value, {}, {"a1": early + before + after}, max20, None)
        row = out["weekly_change"]["per_account"]["a1"]
        self.assertEqual((row["n_before"], row["days_before"]), (len(before), 10))
        self.assertEqual(row["n_after"], len(after))
        self.assertAlmostEqual(row["change_pct"], -10.0, delta=3.0)
        # A certified boundary inside the history moves the start with it.
        mid = datetime(2026, 8, 25, tzinfo=timezone.utc)
        max20 = {"by_account": {"a1": self.block(self.AUG, mid, self.STEP)}}
        out = C.cut_direct_tests({}, [], value, {}, {"a1": early + before + after}, max20, None)
        self.assertEqual(out["weekly_change"]["per_account"]["a1"]["days_before"], 5)


class CutRatioRouteTests(unittest.TestCase):
    """ADR 0001 rule 15: the 14 September ratio route (windows per week times the window) is
    read on the direct weekly test's own sides, selection, unit and estimator, so on one set
    of readings it equals credits per 1% of the seven-day meter by identity."""

    AUG = datetime(2026, 8, 15, tzinfo=timezone.utc)
    STEP = datetime(2026, 9, 14, 11, 30, tzinfo=timezone.utc)
    NEXT = datetime(2026, 9, 22, 19, 41, tzinfo=timezone.utc)

    def block(self):
        return {"regimes": [regime(self.AUG, self.STEP - timedelta(minutes=10)),
                            regime(self.STEP, datetime(2026, 10, 5, tzinfo=timezone.utc))],
                "step": {"onset": "2026-09-14"}}

    @staticmethod
    def window(t, d5, d7=1.0):
        return {"window_ending": t.isoformat(), "five_hour_pct": d5, "seven_day_pct": d7,
                "pieces": 1, "account": "a2"}

    @staticmethod
    def stretch(t, credits_per_pct, delta=6.0, **extra):
        return {"start": (t - timedelta(hours=4)).isoformat(), "end": (t - timedelta(hours=1)).isoformat(),
                "delta_pct": delta, "tokens": {"claude-opus-5": {"output": credits_per_pct * delta}},
                "status": "accepted", **extra}

    def history(self, wpw_before=6.0, wpw_after=4.5, win_before=1000.0, win_after=1100.0):
        windows, stretches = [], []
        for day in range(20):  # 20 August to 8 September
            for h in (6, 12, 18):
                t = datetime(2026, 8, 20, h, tzinfo=timezone.utc) + timedelta(days=day)
                windows.append(self.window(t, wpw_before))
                stretches.append(self.stretch(t, win_before))
        for day in range(6):  # 15 to 20 September
            for h in (6, 12, 18):
                t = datetime(2026, 9, 15, h, tzinfo=timezone.utc) + timedelta(days=day)
                windows.append(self.window(t, wpw_after))
                stretches.append(self.stretch(t, win_after))
        return windows, stretches

    def route(self, windows, stretches, raw=None, clean=None, runs=()):
        max20 = {"by_window": windows, "by_account": {"a2": self.block()}}
        return C.cut_ratio_route({"jwork": stretches}, list(runs), value, {"jwork": "a2"},
                                 {"jwork": raw or []}, {"jwork": clean or []}, max20, self.NEXT)

    def test_the_product_of_its_factors_on_the_direct_tests_sides(self):
        out = self.route(*self.history())
        row = out["per_account"]["a2"]
        self.assertAlmostEqual(row["windows_per_week_ratio"], 0.75, places=4)
        self.assertAlmostEqual(row["window_ratio"], 1.1, places=4)
        self.assertAlmostEqual(row["ratio_after_over_before"], 0.825, places=4)
        self.assertEqual(row["change_pct"], -17.5)
        lo, hi = row["ratio_interval"]
        self.assertLess(lo, 0.825)
        self.assertGreater(hi, 0.825)
        # The sides are the direct weekly test's own instants.
        sides = C.cut_weekly_sides(self.block(), self.NEXT)
        self.assertEqual((row["before_from"], row["before_until"], row["after_from"], row["after_until"]),
                         tuple(C._utc(x) for x in sides))
        self.assertEqual(out["accounts"], ["a2"])
        self.assertAlmostEqual(out["ratio_after_over_before"], 0.825, places=4)

    def test_the_window_factor_is_in_metered_tokens(self):
        # Rule 12 weights a stretch's headless tokens on the five-hour figures it publishes; the
        # meter ratio beside it counts raw five-hour points, so the compound reads metered tokens.
        windows, stretches = self.history()
        weighted = []
        for st in stretches:
            if st["start"] >= "2026-09-15":
                st = dict(st, metered_tokens=st["tokens"],
                          tokens={"claude-opus-5": {"output": st["tokens"]["claude-opus-5"]["output"] * 1.5}},
                          headless_tokens={"claude-opus-5": {"output": 1}})
            weighted.append(st)
        row = self.route(windows, weighted)["per_account"]["a2"]
        self.assertAlmostEqual(row["window_ratio"], 1.1, places=4)

    def test_nothing_outside_the_sides_enters(self):
        windows, stretches = self.history()
        late = self.NEXT + timedelta(days=2)
        early = datetime(2026, 8, 13, tzinfo=timezone.utc)  # before PLAN_CHANGE_AT
        windows += [self.window(late, 40.0), self.window(early, 40.0)]
        stretches += [self.stretch(late, 9000.0), self.stretch(early, 9000.0)]
        row = self.route(windows, stretches)["per_account"]["a2"]
        self.assertAlmostEqual(row["ratio_after_over_before"], 0.825, places=4)

    def test_what_the_weekly_selection_leaves_out_the_ratio_leaves_out(self):
        windows, stretches = self.history()
        t = datetime(2026, 9, 10, 9, tzinfo=timezone.utc)
        # A five-hour window whose hours a rejected seven-day step covers, a stretch the
        # status gate fails, a cloud stretch and one under a harness run.
        windows.append(self.window(t, 40.0))
        raw = [{"start": (t - timedelta(hours=2)).isoformat(), "end": (t - timedelta(hours=1)).isoformat()}]
        stretches += [self.stretch(t, 9000.0, status="unaccounted"),
                      self.stretch(t + timedelta(hours=1), 9000.0, cloud_session=True),
                      self.stretch(t + timedelta(hours=2), 9000.0)]
        run = C.HarnessRun("jwork", t - timedelta(hours=3), t + timedelta(hours=1, minutes=30), "probe")
        row = self.route(windows, stretches, raw=raw, clean=[], runs=[run])["per_account"]["a2"]
        self.assertAlmostEqual(row["ratio_after_over_before"], 0.825, places=4)
        self.assertEqual(row["before"]["n_windows_left_out"], 1)

    def test_an_own_step_before_the_cut_opens_the_after_side_there(self):
        # The account's own step (11:30) is its split, as in the direct test: a window ending
        # at 13:00 is after it, not a window straddling CUT_AT.
        windows, stretches = self.history()
        windows.append(self.window(datetime(2026, 9, 14, 13, tzinfo=timezone.utc), 4.5))
        row = self.route(windows, stretches)["per_account"]["a2"]
        self.assertEqual(row["after"]["n_windows"], 19)

    def test_an_account_with_too_few_stretches_a_side_is_not_measured(self):
        windows, stretches = self.history()
        stretches = [st for st in stretches if st["start"] < "2026-09-15"] + \
            [st for st in stretches if st["start"] >= "2026-09-15"][:C.FIVE_HOUR_MIN_SIDE - 1]
        out = self.route(windows, stretches)
        self.assertEqual(out["per_account"], {})
        self.assertIsNone(out["ratio_after_over_before"])


if __name__ == "__main__":
    unittest.main()
