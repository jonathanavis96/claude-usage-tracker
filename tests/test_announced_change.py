"""The known-date change test: candidates from first-seen families, states, events, power."""
import copy
import math
import random
import statistics
import unittest
from datetime import datetime, timedelta, timezone

from tracker import credits as C
from tracker import publish as P
from tracker.gs_passive import stretch_credits
from tools import announced_change_sim as SIM

CREDITS = P.CREDITS
LABELS = {"acct_one": "a1", "acct_two": "a2", "acct_new": "a3"}
T0 = datetime(2026, 9, 15, tzinfo=timezone.utc)
CAND = datetime(2026, 9, 22, 17, tzinfo=timezone.utc)


def _st(start, hours, model, credits_value, *, verified=True, status="accepted"):
    return {"start": start.isoformat(), "end": (start + timedelta(hours=hours)).isoformat(),
            "delta_pct": 10.0, "status": status, "reset_verified": verified,
            "capture_status": "accepted", "tokens": {model: {"output": credits_value}}}


def _series(start, n, model, level, rng, sd=0.2, **kw):
    return [_st(start + timedelta(hours=3 * i), 2, model, level * math.exp(rng.gauss(0, sd)), **kw)
            for i in range(n)]


def _output_value(tokens):
    return sum(b.get("output", 0) for b in tokens.values())


def _fixture(n_after_one=10, step=1.2, seed=1):
    rng = random.Random(seed)
    # A family on record from the first stretch is no candidate.
    early = [_st(T0 - timedelta(days=5), 2, "claude-opus-5", 1000.0)]
    one = (early + _series(T0, 20, "claude-opus-5", 1000.0, rng)
           + _series(CAND, n_after_one, "claude-opus-5-5", 1000.0 * step, rng, verified=False))
    two = _series(T0, 15, "claude-opus-5", 500.0, rng) + _series(CAND, 3, "claude-opus-5-5", 600.0, rng)
    new = _series(CAND + timedelta(hours=1), 4, "claude-opus-5-5", 700.0, rng)  # nothing before
    return {"acct_one": one, "acct_two": two, "acct_new": new}


class CandidateTests(unittest.TestCase):
    def test_candidates_come_from_first_seen_family(self):
        cands = C.change_candidates(_fixture(), CREDITS)
        self.assertEqual([c["family"] for c in cands], ["opus-5-5"])
        self.assertEqual(cands[0]["at"], CAND)
        self.assertEqual(cands[0]["announcement"]["announced_change_pct"], 20)
        self.assertEqual(cands[0]["announcement"]["scope"], "five_hour")

    def test_candidate_needs_no_announcement(self):
        cands = C.change_candidates(_fixture(), CREDITS, announcements=[])
        self.assertEqual(len(cands), 1)
        self.assertIsNone(cands[0]["announcement"])

    def test_first_seen_is_by_instant_not_string(self):
        by = {"x": [_st(datetime(2026, 9, 1, tzinfo=timezone.utc), 1, "claude-opus-5", 1.0)],
              "y": [{**_st(CAND, 1, "claude-sonnet-5", 1.0),
                     "start": "2026-09-22T19:00:00+02:00"}],  # 17:00Z
              "z": [_st(CAND + timedelta(minutes=30), 1, "claude-sonnet-5", 1.0)]}
        seen = C.family_first_seen(by, CREDITS)["sonnet"]
        self.assertEqual(seen["account"], "y")

    def test_announcements_list_keeps_weekly_record(self):
        self.assertIs(C.ANNOUNCEMENTS[0], C.ANNOUNCEMENT)
        five = [a for a in C.ANNOUNCEMENTS if a["scope"] == "five_hour"]
        self.assertEqual(five[0]["date"], "2026-09-22")
        self.assertIn("Anthropic email to Max subscribers", five[0]["source"])
        self.assertNotIn("@", five[0]["source"])


class ChangeStateTests(unittest.TestCase):
    """measuring while the interval includes 0; provisional once it excludes it; measured once
    its half-width is 10 points or less. Nothing else sets it."""

    def test_each_state(self):
        self.assertEqual(C.change_state(None), "measuring")
        self.assertEqual(C.change_state([-10.9, 38.1]), "measuring")   # the live 22 Sep figure
        self.assertEqual(C.change_state([0.0, 12.0]), "measuring")     # touching 0 includes it
        self.assertEqual(C.change_state([0.6, 34.1]), "provisional")
        self.assertEqual(C.change_state([-40.0, -15.0]), "provisional")
        self.assertEqual(C.change_state([10.0, 30.0]), "measured")     # half-width exactly 10
        self.assertEqual(C.change_state([-18.5, -0.5]), "measured")

    def test_the_email_rules_do_not_set_it(self):
        # The notify step's 24 and 48 hour rules read an event's age and its
        # `interval_excludes_no_change`; the state has no clock in it. The same candidate
        # published as one hour old and as five days old carries the same state.
        fit = _joint(_mixed(g=1.08, r=0.6, sd=0.25, seed=11, n=60))
        cand = C.five_hour_on_meters(_announced(joint_fit=fit), _max20())["candidates"][0]
        young = P._announced_event_record(cand)
        old = P._announced_event_record({**cand, "at": (CAND - timedelta(days=5)).isoformat()})
        self.assertEqual(young["state"], "measuring")
        self.assertEqual(old["state"], young["state"])


class AnnouncedChangeTests(unittest.TestCase):
    def _block(self, **kw):
        return C.announced_change(_fixture(**kw), [], CREDITS, _output_value, LABELS)

    def test_per_account_rows_and_combination(self):
        cand = self._block()["candidates"][0]
        rows = cand["per_account"]
        self.assertEqual(rows["a1"]["n_before"], 20)
        self.assertEqual(rows["a1"]["n_after"], 10)
        self.assertEqual(rows["a1"]["n_after_reset_unverified"], 10)
        self.assertEqual(rows["a2"]["n_after"], 3)
        # nothing before: listed with counts, not combined
        self.assertEqual(rows["a3"]["n_before"], 0)
        self.assertEqual(rows["a3"]["n_after"], 4)
        self.assertFalse(rows["a3"]["combined"])
        self.assertEqual(cand["accounts_combined"], ["a1", "a2"])
        self.assertEqual(cand["state"], C.change_state(cand["interval_pct"]))
        self.assertEqual(cand["before_from"], C.CUT_AT.isoformat())

    def test_states_follow_the_interval_not_the_after_counts(self):
        # No change: the interval includes 0, so measuring however many stretches follow.
        flat = self._block(step=1.0)["candidates"][0]
        self.assertLessEqual(flat["interval_pct"][0], 0)
        self.assertEqual(flat["state"], "measuring")
        # +20% at the fixture's 20% scatter: excludes 0, but wider than 10 points a side.
        wide = self._block(n_after_one=40)["candidates"][0]
        self.assertGreater(wide["interval_pct"][0], 0)
        self.assertGreater(wide["interval_pct"][1] - wide["interval_pct"][0], 20)
        self.assertEqual(wide["state"], "provisional")
        # +20% at 2% scatter on two accounts: narrow enough to be measured.
        rng = random.Random(4)
        tight = {"acct_one": (_series(T0, 20, "claude-opus-5", 1000.0, rng, sd=0.02)
                              + _series(CAND, 6, "claude-opus-5", 1200.0, rng, sd=0.02)),
                 "acct_two": (_series(T0, 15, "claude-opus-5", 500.0, rng, sd=0.02)
                              + _series(CAND, 6, "claude-opus-5", 600.0, rng, sd=0.02))}
        # The new family's first token is on the first stretch after the step.
        tight["acct_one"][20]["tokens"] = {"claude-opus-5-5": {"output": 1.0},
                                           **tight["acct_one"][20]["tokens"]}
        cands = C.announced_change(tight, [], CREDITS, _output_value, LABELS)["candidates"]
        (cand,) = [c for c in cands if c["family"] == "opus-5-5"]
        lo, hi = cand["interval_pct"]
        self.assertTrue(0 < lo and (hi - lo) / 2 <= 10, cand["interval_pct"])
        self.assertEqual(cand["state"], "measured")

    def test_spanning_stretch_is_on_neither_side(self):
        by = _fixture()
        by["acct_two"].append(_st(CAND - timedelta(hours=1), 5, "claude-opus-5", 10.0))
        cand = C.announced_change(by, [], CREDITS, _output_value, LABELS)["candidates"][0]
        self.assertEqual(cand["per_account"]["a2"]["n_before"], 15)
        self.assertEqual(cand["per_account"]["a2"]["n_after"], 3)

    def test_unlabelled_account_gets_a_label_not_its_name(self):
        by = _fixture()
        by["someone"] = by.pop("acct_new")
        block = C.announced_change(by, [], CREDITS, _output_value, LABELS)
        self.assertIn("a4", block["candidates"][0]["per_account"])
        self.assertNotIn("someone", repr(block))

    def test_unpriced_stretches_are_counted_and_left_out(self):
        cand = C.announced_change(_fixture(), [], CREDITS, lambda t: None, LABELS)["candidates"][0]
        self.assertEqual(cand["per_account"]["a1"]["n_before_unpriced"], 20)
        self.assertEqual(cand["accounts_combined"], [])
        self.assertIsNone(cand["change_pct"])

    def test_real_stretch_credits_with_opus_5_5_priced(self):
        rates = copy.deepcopy(C.load_model_rates())
        rates.setdefault("per_family", {})["opus-5-5"] = {"input": 0.8 * 2 / 3,
                                                           "output_multiplier": 5}
        cand = C.announced_change(
            _fixture(), [], CREDITS, lambda t: stretch_credits(t, CREDITS, rates)[0],
            LABELS)["candidates"][0]
        self.assertEqual(cand["per_account"]["a1"]["n_after"], 10)
        self.assertEqual(cand["per_account"]["a1"]["n_after_unpriced"], 0)
        self.assertIsNotNone(cand["change_pct"])


FIVE_HOUR_NOTE = next(a for a in C.ANNOUNCEMENTS if a["scope"] == "five_hour")


def _window(label, ending, d5, d7, pieces=1):
    return {"account": label, "window_ending": ending.isoformat(), "five_hour_pct": d5,
            "seven_day_pct": d7, "pieces": pieces, "reset_verified": True}


def _max20(n_after=10, after_wpw=4.5, n_before=20):
    """a1 and a2 at 5 windows per week from the cut, then `after_wpw` from CAND; a3 after only.

    One window straddles CAND (ends 2 hours after it) and belongs to neither side.
    """
    rows = []
    for label in ("a1", "a2"):
        rows += [_window(label, C.CUT_AT + timedelta(hours=6 * (i + 1)), 50.0, 10.0)
                 for i in range(n_before)]
        rows.append(_window(label, CAND + timedelta(hours=2), 50.0, 1.0))
        rows += [_window(label, CAND + timedelta(hours=6 * (i + 1)), 50.0, 50.0 / after_wpw)
                 for i in range(n_after)]
    rows += [_window("a3", CAND + timedelta(hours=6 * (i + 1)), 40.0, 10.0) for i in range(4)]
    return {"by_window": rows, "by_account": {}}


def _announced(note=FIVE_HOUR_NOTE, at=CAND, joint_fit=None):
    return {"candidates": [{"family": "opus-5-5", "at": at.isoformat(), "at_source": "first_turn",
                            "first_seen_account": "a1", "first_seen_stretch_end": at.isoformat(),
                            "before_from": C.CUT_AT.isoformat(), "after_until": None,
                            "announcement": note, "joint_fit": joint_fit}]}


def _mixed(g=1.2, r=0.6, n=24, sd=0.08, seed=5, share=None, shares=None):
    """Two accounts' stretches around CAND: known-rate Opus 5 work at 100 credits per 1%
    before; after it Opus 5 and Opus 5.5 mixed, the Opus 5.5 share varying stretch to
    stretch (or fixed at `share`, or cycling through `shares`), the new family charged at
    `r` times Opus 5 and the
    five-hour limit multiplied by `g`. Valued by `_output_value`, so a stretch's credits at
    the base rate are its output tokens."""
    rng = random.Random(seed)
    by = {}
    for name, level in (("acct_one", 100.0), ("acct_two", 140.0)):
        rows = []
        for i in range(n):
            known = rng.uniform(500, 3000)
            st = _st(C.CUT_AT + timedelta(hours=3 * (i + 1)), 2, "claude-opus-5", known)
            st["delta_pct"] = known / level * math.exp(rng.gauss(0, sd))
            rows.append(st)
        for i in range(n):
            total = rng.uniform(500, 3000)
            s_new = (shares[i % len(shares)] if shares else
                     share if share is not None else rng.uniform(0.05, 0.95))
            known, new = total * (1 - s_new), total * s_new
            st = _st(CAND + timedelta(hours=3 * (i + 1)), 2, "claude-opus-5", known)
            st["tokens"]["claude-opus-5-5"] = {"output": new}
            st["delta_pct"] = (known + r * new) / (level * g) * math.exp(rng.gauss(0, sd))
            rows.append(st)
        by[name] = rows
    return by


def _joint(by):
    return C.joint_rate_fit(by, "opus-5-5", CAND, C.CUT_AT, None, _output_value, CREDITS,
                            LABELS, list(by), base_times_opus=1.0)


class FirstTurnTests(unittest.TestCase):
    def test_candidate_instant_is_the_first_turn_not_the_stretch_start(self):
        by = _fixture()
        for rows in by.values():
            for st in rows:
                if "claude-opus-5-5" in st["tokens"]:
                    first = datetime.fromisoformat(st["start"]) + timedelta(minutes=40)
                    st["first_turns"] = {"claude-opus-5-5": first.isoformat()}
        # A week-long stretch that starts first but whose first Opus 5.5 turn is days later.
        long = _st(CAND - timedelta(days=1), 150, "claude-opus-5-5", 1.0)
        long["first_turns"] = {"claude-opus-5-5": (CAND + timedelta(days=5)).isoformat()}
        by["acct_new"] = [long]
        cand = C.change_candidates(by, CREDITS)[0]
        self.assertEqual(cand["at"], CAND + timedelta(minutes=40))
        self.assertEqual(cand["at_source"], "first_turn")
        self.assertEqual(cand["first_seen_account"], "acct_one")
        self.assertEqual(cand["first_seen_stretch_end"], CAND + timedelta(hours=2))

    def test_without_stamps_the_stretch_start_stands_and_says_so(self):
        cand = C.change_candidates(_fixture(), CREDITS)[0]
        self.assertEqual((cand["at"], cand["at_source"]), (CAND, "stretch_start"))

    def test_stretch_records_each_models_first_turn(self):
        from tracker.join import Stretch
        from tracker.turns import Turn
        s = Stretch(CAND, CAND + timedelta(hours=1))
        for minutes, model in ((5, "claude-opus-5"), (9, "claude-opus-5-5"), (20, "claude-opus-5-5")):
            s.add(Turn(ts=CAND + timedelta(minutes=minutes), model=model, id=str(minutes),
                       input=1, output=1, cache_read=0, cache_write=0), {})
        self.assertEqual(s.first_turns["claude-opus-5-5"], CAND + timedelta(minutes=9))


class SeparabilityTests(unittest.TestCase):
    """A joint fit is separable only when the new family's rate is actually pinned: a narrow
    rate interval, and enough mixed stretches after the candidate to identify it."""

    def test_a_rate_interval_wider_than_one_and_a_half_times_is_not_separable(self):
        fit = _joint(_mixed(n=8, sd=0.25))
        lo, hi = fit["rate_relative_interval"]
        # Wide enough to fail the 1.5x test, narrow enough to have passed the old 3x one.
        self.assertGreater(hi / lo, C.JOINT_SEPARABLE_SPAN)
        self.assertLessEqual(hi / lo, 3.0)
        self.assertEqual(C.JOINT_SEPARABLE_SPAN, 1.5)
        self.assertFalse(fit["separable"])
        self.assertEqual(fit["state"], "measuring")
        self.assertIn("rate's 95% interval", fit["reason"])

    def test_fewer_than_three_mixed_after_stretches_is_not_separable(self):
        # Every after stretch is nearly all one family or the other: the shares vary a lot,
        # which pins a rate in the bootstrap, but no stretch mixes the two, so nothing does.
        fit = _joint(_mixed(shares=(0.05, 0.95), sd=0.02))
        lo, hi = fit["rate_relative_interval"]
        self.assertLessEqual(hi / lo, C.JOINT_SEPARABLE_SPAN)
        self.assertEqual(fit["n_mixed_after"], 0)
        self.assertFalse(fit["separable"])
        self.assertIn("mixed", fit["reason"])
        self.assertNotIn("rate's 95% interval", fit["reason"])

    def test_three_mixed_stretches_are_enough(self):
        by = _mixed(shares=(0.05, 0.95), sd=0.02)
        # Three after stretches on the first account at a 50% share.
        after = [st for st in by["acct_one"] if "claude-opus-5-5" in st["tokens"]][:3]
        for st in after:
            known = st["tokens"]["claude-opus-5"]["output"]
            new = st["tokens"]["claude-opus-5-5"]["output"]
            total = known + new
            st["tokens"]["claude-opus-5"]["output"] = total / 2
            st["tokens"]["claude-opus-5-5"]["output"] = total / 2
            st["delta_pct"] = (total / 2 + 0.6 * total / 2) / (100.0 * 1.2)
        fit = _joint(by)
        self.assertEqual(fit["n_mixed_after"], 3)
        self.assertTrue(fit["separable"], fit["reason"])


class MeterTests(unittest.TestCase):
    def test_windows_per_week_ratio_gives_the_change_and_both_readings(self):
        cand = C.five_hour_on_meters(_announced(), _max20())["candidates"][0]
        a1 = cand["per_account"]["a1"]
        self.assertEqual((a1["n_before"], a1["n_after"], a1["n_straddling"]), (20, 10, 1))
        self.assertEqual((a1["windows_per_week_before"], a1["windows_per_week_after"]), (5.0, 4.5))
        self.assertEqual(a1["windows_per_week_change_pct"], -10.0)
        self.assertEqual(a1["readings"]["five_hour_scope"]["five_hour_window_change_pct"],
                         round((5.0 / 4.5 - 1) * 100, 1))
        lo, hi = a1["windows_per_week_change_interval_pct"]
        self.assertLess(lo, -10.0)
        self.assertGreater(hi, -10.0)
        self.assertEqual(cand["accounts_combined"], ["a1", "a2"])
        self.assertFalse(cand["per_account"]["a3"]["combined"])
        self.assertEqual(cand["windows_per_week_change_pct"], -10.0)
        self.assertEqual(cand["readings"]["five_hour_scope"]["five_hour_window_change_pct"], 11.1)
        self.assertEqual(cand["readings"]["weekly_scope"]["weekly_cap_change_pct"], -10.0)
        self.assertEqual(cand["windows_per_week_ratio"], 0.9)
        self.assertEqual(cand["state"], "measured")
        self.assertTrue(cand["applies"])
        self.assertEqual(cand["method"], "windows_per_week_ratio")

    def test_without_a_separable_joint_fit_the_scope_is_undetermined(self):
        cand = C.five_hour_on_meters(_announced(), _max20())["candidates"][0]
        self.assertEqual(cand["scope"]["state"], "undetermined")
        self.assertIsNone(cand["change_pct"])
        self.assertIsNone(cand["interval_pct"])
        uniform = _joint(_mixed(share=0.5))
        self.assertFalse(uniform["separable"])
        self.assertIn("varies too little", uniform["reason"])
        cand = C.five_hour_on_meters(_announced(joint_fit=uniform), _max20())["candidates"][0]
        self.assertEqual(cand["scope"]["state"], "undetermined")
        self.assertIn("varies too little", cand["scope"]["reason"])

    def test_a_known_twenty_percent_five_hour_step_is_published_as_twenty_percent(self):
        # The new family at 0.6x its base, the five-hour limit up 20%, the weekly cap
        # unchanged: windows per week falls from 5 to 5 / 1.2.
        fit = _joint(_mixed(g=1.2, r=0.6))
        self.assertTrue(fit["separable"])
        lo, hi = fit["rate_relative_interval"]
        self.assertLess(lo, 0.6)
        self.assertGreater(hi, 0.6)
        g_lo, g_hi = fit["five_hour_limit_change_interval_pct"]
        self.assertLess(g_lo, 20.0)
        self.assertGreater(g_hi, 20.0)
        self.assertGreater(g_lo, 0.0)
        m = _max20(n_before=30, n_after=30, after_wpw=5.0 / 1.2)
        cand = C.five_hour_on_meters(_announced(joint_fit=fit), m)["candidates"][0]
        self.assertEqual(cand["scope"]["state"], "five_hour")
        # The published change is the fit's own g, within its interval of the true +20%.
        self.assertEqual(cand["change_pct"], fit["five_hour_limit_change_pct"])
        self.assertAlmostEqual(cand["change_pct"], 20.0, delta=5.0)
        self.assertAlmostEqual(cand["windows_per_week_change_pct"], (1 / 1.2 - 1) * 100, delta=0.1)
        undecided = C.five_hour_on_meters(_announced(), m)["candidates"][0]
        self.assertAlmostEqual(
            undecided["readings"]["five_hour_scope"]["five_hour_window_change_pct"], 20.0, delta=0.1)
        ev = P._announced_events(C.five_hour_on_meters(_announced(joint_fit=fit), m))[0]
        self.assertEqual((ev["scope"], ev["metric"], ev["direction"]),
                         ("five_hour", "five_hour_limit", "increased"))
        self.assertAlmostEqual(ev["percent"], 20, delta=5)
        # The weekly limit (g times the ratio 1 / 1.2) is about unchanged.
        self.assertAlmostEqual(ev["weekly_limit_change_pct"], 0.0, delta=5.0)

    def test_a_weekly_only_step_is_recovered_as_weekly(self):
        # The five-hour limit unchanged, the weekly cap down 20%: windows per week 5 to 4.
        fit = _joint(_mixed(g=1.0, r=0.6, seed=8))
        self.assertTrue(fit["separable"])
        g_lo, g_hi = fit["five_hour_limit_change_interval_pct"]
        self.assertLess(g_lo, 0.0)
        self.assertGreater(g_hi, 0.0)
        m = _max20(n_before=30, n_after=30, after_wpw=4.0)
        cand = C.five_hour_on_meters(_announced(joint_fit=fit), m)["candidates"][0]
        self.assertEqual(cand["scope"]["state"], "weekly")
        w_lo, w_hi = cand["scope"]["weekly_limit_change_interval_pct"]
        self.assertLess(w_lo, -20.0)
        self.assertGreater(w_hi, -20.0)
        self.assertLess(w_hi, 0.0)
        self.assertAlmostEqual(cand["change_pct"], 0.0, delta=5.0)
        ev = P._announced_events(C.five_hour_on_meters(_announced(joint_fit=fit), m))[0]
        self.assertEqual((ev["scope"], ev["metric"]), ("weekly", "five_hour_limit"))
        self.assertAlmostEqual(ev["weekly_limit_change_pct"], -20.0, delta=5.0)
        # The weekly change moved more, so its interval decides whether the email may go early.
        self.assertTrue(ev["interval_excludes_no_change"])
        # It is also the figure the plan-wide test refits, each account's g times the
        # windows-per-week ratio without it, and the one whose interval lets it apply.
        self.assertEqual(cand["headline"]["metric"], "weekly_limit")
        pw = cand["plan_wide"]
        self.assertEqual(pw["estimator"], "the weekly limit change, g times the windows-per-week ratio")
        self.assertEqual((pw["state"], sorted(pw["without"])), ("passed", ["a1", "a2"]), pw["reason"])
        self.assertTrue(cand["applies"])

    def test_a_separable_fit_moves_the_regimes_at_once_once_its_interval_excludes_no_change(self):
        fit = _joint(_mixed(g=1.3, r=0.6, sd=0.2, seed=11, n=60))
        self.assertTrue(fit["separable"], fit["reason"])
        self.assertGreater(fit["five_hour_limit_change_interval_pct"][0], 0.0)
        meters = C.five_hour_on_meters(_announced(joint_fit=fit), _max20())
        cand = meters["candidates"][0]
        self.assertEqual(cand["plan_wide"]["state"], "passed", cand["plan_wide"]["reason"])
        self.assertTrue(cand["applies"])
        self.assertIsNone(cand["withheld_reason"])
        self.assertEqual(cand["change_pct"], fit["five_hour_limit_change_pct"])
        (change,) = C.known_date_changes(meters)
        self.assertTrue(change["window_scaled"])
        self.assertAlmostEqual(change["ratio"], 1 + fit["five_hour_limit_change_pct"] / 100)
        (ev,) = P._announced_events(meters)
        g, w = cand["change_pct"], cand["scope"]["weekly_limit_change_pct"]
        self.assertEqual(ev["label"], f"Five-hour limit {g:+g}%, weekly limit {w:+g}% (provisional)")
        self.assertTrue(ev["interval_excludes_no_change"])
        self.assertNotIn("announce", ev["label"].lower())

    def test_a_separable_fit_whose_interval_includes_no_change_is_withheld(self):
        # A small, noisy +8% step: separable, but both intervals include no change. Rule 9:
        # it is not published, and its boundary carries the window instead of scaling it.
        fit = _joint(_mixed(g=1.08, r=0.6, sd=0.25, seed=11, n=60))
        self.assertTrue(fit["separable"])
        g_lo, g_hi = fit["five_hour_limit_change_interval_pct"]
        self.assertLess(g_lo, 0.0)
        self.assertGreater(g_hi, 0.0)
        meters = C.five_hour_on_meters(_announced(joint_fit=fit), _max20())
        cand = meters["candidates"][0]
        self.assertEqual((cand["state"], cand["change_pct"]),
                         ("measuring", fit["five_hour_limit_change_pct"]))
        self.assertTrue(cand["measurable"])
        self.assertFalse(cand["applies"])
        self.assertIn("interval test", cand["withheld_reason"])
        self.assertIn("includes no change", cand["withheld_reason"])
        (change,) = C.known_date_changes(meters)
        self.assertFalse(change["window_scaled"])
        self.assertEqual(change["ratio"], 1.0)
        self.assertEqual(P._announced_events(meters), [])

    def test_a_fit_that_cannot_separate_carries_the_window(self):
        meters = C.five_hour_on_meters(_announced(joint_fit=_joint(_mixed(share=0.5))), _max20())
        self.assertIsNone(meters["candidates"][0]["change_pct"])
        (change,) = C.known_date_changes(meters)
        self.assertFalse(change["window_scaled"])
        self.assertEqual(change["ratio"], 1.0)

    def test_states_follow_the_headline_interval_and_only_a_settled_one_applies(self):
        # `state` says how settled, read off the windows-per-week interval while the fit
        # cannot separate g; a change applies once that interval excludes no change.
        for n, wpw, state in ((1, 4.5, "measuring"), (5, 4.5, "measuring"),
                              (1, 3.8, "provisional"), (10, 4.5, "measured")):
            with self.subTest(n=n, wpw=wpw):
                cand = C.five_hour_on_meters(_announced(), _max20(n_after=n, after_wpw=wpw))["candidates"][0]
                self.assertEqual(cand["state"], state, cand["windows_per_week_change_interval_pct"])
                self.assertEqual(cand["state"], C.change_state(cand["windows_per_week_change_interval_pct"]))
                self.assertTrue(cand["measurable"])
                self.assertEqual(cand["applies"], state != "measuring", cand["withheld_reason"])

    def test_a_separable_fit_sets_the_state_from_the_five_hour_interval(self):
        for g, sd, state in ((1.08, 0.25, "measuring"), (1.3, 0.2, "provisional"),
                             (1.2, 0.02, "measured")):
            with self.subTest(g=g, sd=sd):
                fit = _joint(_mixed(g=g, r=0.6, sd=sd, seed=11, n=60))
                self.assertTrue(fit["separable"])
                self.assertEqual(fit["state"], state, fit["five_hour_limit_change_interval_pct"])
                cand = C.five_hour_on_meters(_announced(joint_fit=fit), _max20())["candidates"][0]
                self.assertEqual(cand["interval_pct"], fit["five_hour_limit_change_interval_pct"])
                self.assertEqual(cand["state"], state)
                events = P._announced_events({"candidates": [cand]})
                if state == "measuring":
                    self.assertEqual(events, [])
                    continue
                (ev,) = events
                self.assertEqual((ev["state"], ev["evidence_quality"]), (state, state))

    def test_no_reading_after_on_a_combined_account_does_not_apply(self):
        cand = C.five_hour_on_meters(_announced(), _max20(n_after=0))["candidates"][0]
        self.assertEqual(cand["accounts_combined"], [])
        self.assertFalse(cand["applies"])

    def test_a_candidate_without_an_announcement_is_measured_the_same(self):
        with_note = C.five_hour_on_meters(_announced(), _max20())["candidates"][0]
        without = C.five_hour_on_meters(_announced(note=None), _max20())["candidates"][0]
        self.assertIsNone(without["announcement"])
        self.assertEqual({k: v for k, v in with_note.items() if k != "announcement"},
                         {k: v for k, v in without.items() if k != "announcement"})

    def test_own_weekly_step_later_than_the_boundary_starts_the_before_side(self):
        m = _max20()
        step = C.CUT_AT + timedelta(hours=6 * 10 + 1)
        m["by_account"]["a1"] = {"step": {"percent": -20},
                                 "regimes": [{"start": "2026-09-01T00:00:00+00:00",
                                              "end": C.CUT_AT.isoformat()},
                                             {"start": step.isoformat(), "end": CAND.isoformat()}]}
        a1 = C.five_hour_on_meters(_announced(), m)["candidates"][0]["per_account"]["a1"]
        self.assertEqual(a1["n_before"], 10)
        self.assertEqual(a1["before_from"], step.isoformat())

    def test_own_weekly_step_after_the_candidate_ends_the_after_side(self):
        m = _max20()
        own_end = CAND + timedelta(hours=6 * 4 + 1)
        m["by_account"]["a1"] = {"step": {"percent": -20},
                                 "regimes": [{"start": "2026-09-01T00:00:00+00:00",
                                              "end": own_end.isoformat()},
                                             {"start": (own_end + timedelta(hours=5)).isoformat(),
                                              "end": "2026-10-01T00:00:00+00:00"}]}
        a1 = C.five_hour_on_meters(_announced(), m)["candidates"][0]["per_account"]["a1"]
        self.assertEqual(a1["n_after"], 4)
        self.assertEqual(a1["after_until"], own_end.isoformat())

    def test_measured_change_enters_events_and_last_change_at_its_scope_free_size(self):
        block = C.five_hour_on_meters(_announced(), _max20())
        events = P._announced_events(block)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual((ev["date"], ev["direction"], ev["percent"]), ("2026-09-22", "decreased", 10))
        self.assertEqual((ev["scope"], ev["metric"]), ("undetermined", "windows_per_week"))
        self.assertEqual(ev["change_pct"], -10.0)
        self.assertEqual(ev["readings"]["five_hour_scope"]["five_hour_window_change_pct"], 11.1)
        self.assertIn("not yet separable", ev["label"])
        self.assertTrue(ev["known_date_test"])
        older = {"date": "2026-09-11", "percent": 26}
        last = P._with_announced_last_change(older, block)
        self.assertEqual((last["date"], last["method"]), ("2026-09-22", "windows_per_week_ratio"))
        self.assertNotIn("label", last)

    def test_the_credits_test_no_longer_drives_events(self):
        credits_block = C.announced_change(_fixture(step=1.6), [], CREDITS, _output_value, LABELS)
        self.assertTrue(credits_block["candidates"][0]["interval_excludes_no_change"])
        self.assertEqual(P._announced_events(credits_block), [])

    def test_a_measuring_change_is_not_an_event_and_not_last_change(self):
        # Two accounts agree on -10% windows per week, but two readings after cannot yet
        # exclude no change: the candidate stays published under its candidates only, and
        # its boundary still splits the regimes without scaling anything.
        block = C.five_hour_on_meters(_announced(), _max20(n_after=2))
        cand = block["candidates"][0]
        self.assertEqual(cand["state"], "measuring")
        self.assertLessEqual(cand["windows_per_week_change_interval_pct"][0], 0.0)
        self.assertGreaterEqual(cand["windows_per_week_change_interval_pct"][1], 0.0)
        self.assertFalse(cand["applies"])
        self.assertIn("includes no change", cand["withheld_reason"])
        self.assertEqual(P._announced_events(block), [])
        (change,) = C.known_date_changes(block)
        self.assertFalse(change["window_scaled"])
        older = {"date": "2026-09-11"}
        self.assertIs(P._with_announced_last_change(older, block), older)
        self.assertIsNone(P._with_announced_last_change(None, block))
        # The same candidate once the readings settle it is an event and the last change.
        settled = C.five_hour_on_meters(_announced(), _max20(n_after=10))
        (ev,) = P._announced_events(settled)
        self.assertEqual((ev["at"], ev["state"]), (CAND.isoformat(), "measured"))
        self.assertEqual(P._with_announced_last_change(older, settled)["date"], "2026-09-22")

    def test_nothing_measurable_stays_out_of_events(self):
        block = C.five_hour_on_meters(_announced(), _max20(n_after=0))
        self.assertEqual(P._announced_events(block), [])
        older = {"date": "2026-09-11"}
        self.assertIs(P._with_announced_last_change(older, block), older)


class JointFitTests(unittest.TestCase):
    def test_known_rate_and_limit_change_are_recovered_per_account_too(self):
        fit = _joint(_mixed(g=1.2, r=0.6))
        self.assertEqual(fit["state"], "measured")
        self.assertEqual(fit["base_family"], "opus")
        self.assertEqual(fit["accounts_combined"], ["a1", "a2"])
        self.assertAlmostEqual(fit["rate_relative_to_base"], 0.6, delta=0.1)
        self.assertAlmostEqual(fit["five_hour_limit_change_pct"], 20.0, delta=5.0)
        for label in ("a1", "a2"):
            self.assertAlmostEqual(fit["per_account"][label]["five_hour_limit_change_pct"], 20.0,
                                   delta=8.0)

    def test_the_fit_is_reproducible(self):
        self.assertEqual(_joint(_mixed()), _joint(_mixed()))

    def test_a_separable_fit_is_absorbed_into_the_rates(self):
        by = _mixed(g=1.2, r=0.6)
        rates, fits = C.absorb_new_family_rates(
            by, [], CREDITS, {"per_family": {}}, LABELS,
            lambda rates: _output_value)
        (key, fit), = fits.items()  # the candidate instant is the first Opus 5.5 stretch's start
        self.assertEqual(key, ("opus-5-5", (CAND + timedelta(hours=3)).isoformat()))
        self.assertTrue(fit["separable"])
        row = rates["per_family"]["opus-5-5"]
        self.assertEqual(row["rate_source"], "joint_fit_at_first_use")
        anchor = C.family_rate("opus", CREDITS, {}).input
        self.assertAlmostEqual(row["input"], anchor * fit["rate_relative_to_base"])
        rate = C.family_rate("opus-5-5", CREDITS, rates)
        self.assertAlmostEqual(rate.input, row["input"])

    def test_nothing_absorbed_returns_the_rates_given(self):
        given = {}
        rates, _ = C.absorb_new_family_rates(_mixed(share=0.5), [], CREDITS, given, LABELS,
                                             lambda rates: _output_value)
        self.assertIs(rates, given)


#: A rates block with a pooled fit, Sonnet published at its point estimate. `_unpublished`
#: is the same block after a few stretches widen Sonnet's interval past the publish limit.
_OPUS_IN = 10 / 15
_POOLED_RATES = {
    "anchor": {"family": "opus", "input": _OPUS_IN, "output": 5 * _OPUS_IN},
    "max_interval_ratio": 1.5,
    "pooled_fit": {"times_opus": {"opus": 1.0, "sonnet": 0.3625, "haiku": 0.70},
                   "interval": {"sonnet": [0.293, 0.434], "haiku": [0.28, 1.65]},
                   "cache_read_weight": 0.0047, "output_multiplier": 5},
    "per_family": {
        "opus": {"input": _OPUS_IN, "anchor": True, "rate_source": "reference", "times_opus": 1.0},
        "sonnet": {"input": 0.3625 * _OPUS_IN, "interval": [0.293 * _OPUS_IN, 0.434 * _OPUS_IN],
                   "status": None, "rate_source": "measured", "times_opus": 0.3625},
        "haiku": {"input": None, "interval": None, "rate_source": "measured",
                  "status": "not measurable, the pooled fit cannot pin Haiku down",
                  "inferred": {"input": 2 / 15, "times_opus": 0.2, "output_multiplier": 5,
                               "inferred_from": "reference_table"}},
    },
}


def _unpublished(rates):
    """`rates` with Sonnet's interval 1.53x wide end to end: the same point estimate, no
    published rate, and the reference table's 0.4 inferred for display."""
    out = copy.deepcopy(rates)
    out["pooled_fit"]["interval"]["sonnet"] = [0.286, 0.438]
    out["per_family"]["sonnet"] = {
        "input": None, "interval": None, "rate_source": "measured", "times_opus": None,
        "status": "not measurable, the pooled fit cannot pin Sonnet down: its 80% interval "
                  "is 1.53x wide end to end, and a published rate needs under 1.5x",
        "inferred": {"input": 0.4, "times_opus": 0.6, "output_multiplier": 5,
                     "inferred_from": "reference_table"}}
    return out


def _sonnet_mixed(g=1.25, r=0.8, n=24, sd=0.06, seed=11):
    """`_mixed` with Sonnet in the known work on both sides of the candidate, its share varying
    stretch to stretch, and cache reads beside it; the meter charges every family at the
    pooled fit's rates (`comparison_value` of `_POOLED_RATES`)."""
    rng = random.Random(seed)
    true = C.comparison_value(CREDITS, _POOLED_RATES)
    by = {}
    for name, level in (("acct_one", 900.0), ("acct_two", 1300.0), ("acct_new", 1100.0)):
        rows = []
        for i in range(2 * n):
            after = i >= n
            start = (CAND if after else C.CUT_AT) + timedelta(hours=3 * (i % n + 1))
            tokens = {"claude-opus-5": {"output": rng.uniform(100, 900),
                                        "cache_read": rng.uniform(0, 2e5)},
                      "claude-sonnet-5": {"output": rng.uniform(0, 3000),
                                          "input": rng.uniform(0, 2e4)}}
            new = 0.0
            if after:
                tokens["claude-opus-5-5"] = {"output": rng.uniform(50, 900)}
                new = true({"claude-opus-5": tokens["claude-opus-5-5"]})
            known = true({m: t for m, t in tokens.items() if m != "claude-opus-5-5"})
            rows.append({"start": start.isoformat(), "end": (start + timedelta(hours=2)).isoformat(),
                         "status": "accepted", "reset_verified": True, "capture_status": "accepted",
                         "tokens": tokens,
                         "delta_pct": (known + r * new) / (level * (g if after else 1.0))
                         * math.exp(rng.gauss(0, sd))})
        by[name] = rows
    return by


class PublishStateTests(unittest.TestCase):
    """A family's rate crossing the publish limit moves no change figure (issue #132)."""

    def setUp(self):
        # Low enough scatter that the fit pins the rate within JOINT_SEPARABLE_SPAN.
        self.by = _sonnet_mixed(sd=0.03)
        self.published = copy.deepcopy(_POOLED_RATES)
        self.unpublished = _unpublished(_POOLED_RATES)

    def _fit(self, rates, value_for=None):
        _, fits = C.absorb_new_family_rates(self.by, [], CREDITS, rates, LABELS, value_for)
        (fit,) = fits.values()
        return fit

    def test_the_fixture_differs_only_in_sonnet_being_published(self):
        self.assertIsNotNone(C.family_rate("sonnet", CREDITS, self.published).input)
        self.assertEqual(C.family_rate("sonnet", CREDITS, self.unpublished).rate_source, "inferred")
        self.assertEqual(C.pooled_fit_prices(self.published)["input"],
                         C.pooled_fit_prices(self.unpublished)["input"])

    def test_the_published_rate_valuation_does_move_the_fit(self):
        # The fixture reaches the fault: valued at the published rates, Sonnet falls back to
        # the reference table's 0.4 once unpublished, and the limit change moves with it.
        def published_rates(rates):
            return lambda t: stretch_credits(t, CREDITS, rates)[0]
        before = self._fit(self.published, published_rates)["five_hour_limit_change_pct"]
        after = self._fit(self.unpublished, published_rates)["five_hour_limit_change_pct"]
        self.assertGreater(abs(after - before), 1.0)

    def test_joint_fit_does_not_move_when_a_family_is_unpublished(self):
        for value_for in (None, lambda rates: C.comparison_value(CREDITS, rates)):
            one, two = self._fit(self.published, value_for), self._fit(self.unpublished, value_for)
            for key in ("five_hour_limit_change_pct", "five_hour_limit_change_interval_pct",
                        "rate_relative_to_base", "rate_relative_interval", "times_opus", "state"):
                self.assertEqual(one[key], two[key], key)
            for label in one["accounts_combined"]:
                self.assertEqual(one["per_account"][label]["five_hour_limit_change_pct"],
                                 two["per_account"][label]["five_hour_limit_change_pct"])
        self.assertEqual(one["accounts_combined"], ["a1", "a2", "a3"])
        self.assertAlmostEqual(one["rate_relative_to_base"], 0.8, delta=0.12)
        self.assertAlmostEqual(one["five_hour_limit_change_pct"], 25.0, delta=6.0)

    def test_per_account_credits_test_does_not_move_when_a_family_is_unpublished(self):
        blocks = []
        for rates in (self.published, self.unpublished):
            absorbed, fits = C.absorb_new_family_rates(self.by, [], CREDITS, rates, LABELS)
            blocks.append(C.announced_change(self.by, [], CREDITS,
                                             C.comparison_value(CREDITS, absorbed), LABELS,
                                             joint_fits=fits)["candidates"][0])
        one, two = blocks
        self.assertEqual(one["change_pct"], two["change_pct"])
        self.assertEqual(one["per_account"], two["per_account"])
        self.assertEqual(one["joint_fit"], two["joint_fit"])

    def test_the_absorbed_rate_is_reported_against_the_pooled_base(self):
        absorbed, fits = C.absorb_new_family_rates(self.by, [], CREDITS, self.unpublished, LABELS)
        (fit,) = fits.values()
        row = absorbed["per_family"]["opus-5-5"]
        self.assertEqual(row["rate_source"], "joint_fit_at_first_use")
        self.assertAlmostEqual(row["input"], _OPUS_IN * fit["rate_relative_to_base"])
        # A later candidate values the absorbed family at its fitted rate.
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", CREDITS, absorbed)[0], row["input"])


def _with_new_family_fitted(times_opus=0.55):
    """`_POOLED_RATES` with Opus 5.5 in the pooled fit at `times_opus`, as tools/model_rates.py
    fits it after 22 September: a final rate, absorbing whatever limit change came with it."""
    rates = copy.deepcopy(_POOLED_RATES)
    rates["pooled_fit"]["times_opus"]["opus-5-5"] = times_opus
    rates["pooled_fit"]["interval"]["opus-5-5"] = [times_opus * 0.85, times_opus * 1.15]
    rates["per_family"]["opus-5-5"] = {
        "input": times_opus * _OPUS_IN, "interval": [0.85 * times_opus * _OPUS_IN,
                                                     1.15 * times_opus * _OPUS_IN],
        "status": None, "rate_source": "measured", "provisional": False,
        "times_opus": times_opus, "times_opus_interval": [times_opus * 0.85, times_opus * 1.15]}
    return rates


class ListUntilSeparableTests(unittest.TestCase):
    """ADR 0001 rule 10: a new family is valued at its list price until the joint fit at its
    first use separates its rate from a limit change."""

    def setUp(self):
        self.rates = _with_new_family_fitted(0.55)
        self.list_ratio = C.list_price_ratio("opus-5-5", CREDITS)

    def _absorb(self, by, credits=CREDITS):
        return C.absorb_new_family_rates(by, [], credits, self.rates, LABELS,
                                         lambda rates: _output_value)

    def test_a_new_family_with_an_inseparable_fit_is_valued_at_list(self):
        absorbed, fits = self._absorb(_mixed(share=0.5))
        (fit,) = fits.values()
        self.assertFalse(fit["separable"])
        row = absorbed["per_family"]["opus-5-5"]
        self.assertEqual(row["rate_source"], C.LIST_UNTIL_SEPARABLE)
        self.assertEqual(row["rate_in_use"], "list")
        self.assertAlmostEqual(row["input"], self.list_ratio * _OPUS_IN)
        self.assertAlmostEqual(row["list_times_opus"], self.list_ratio)
        # The fitted rate stays beside it, with a sentence saying why it is not used.
        self.assertEqual(row["times_opus"], 0.55)
        self.assertEqual(row["times_opus_interval"], self.rates["per_family"]["opus-5-5"]["times_opus_interval"])
        self.assertIn("not used", row["status"])
        # Every valuation reads the list rate: the comparison, the pooled fit, the published rate.
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", CREDITS, absorbed)[0],
                               self.list_ratio * _OPUS_IN)
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", CREDITS, absorbed)[1],
                               5 * self.list_ratio * _OPUS_IN)
        self.assertAlmostEqual(C.pooled_fit_prices(absorbed)["times_opus"]["opus-5-5"], self.list_ratio)
        self.assertAlmostEqual(C.family_rate("opus-5-5", CREDITS, absorbed).input,
                               self.list_ratio * _OPUS_IN)
        tok = {"output": 10, "cache_read": 1000}
        self.assertAlmostEqual(C.across_cut_value(CREDITS, model_rates=absorbed)({"claude-opus-5-5": tok}),
                               50 * self.list_ratio * _OPUS_IN + 1000 * 0.0047 * _OPUS_IN)
        # The caller's block is not touched.
        self.assertEqual(self.rates, _with_new_family_fitted(0.55))

    def test_the_same_family_with_a_separable_fit_uses_its_fitted_rate(self):
        absorbed, fits = self._absorb(_mixed(g=1.2, r=0.6))
        (fit,) = fits.values()
        self.assertTrue(fit["separable"])
        row = absorbed["per_family"]["opus-5-5"]
        self.assertEqual(row["rate_source"], "joint_fit_at_first_use")
        self.assertEqual(row["rate_in_use"], "fitted")
        self.assertAlmostEqual(row["list_times_opus"], self.list_ratio)
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", CREDITS, absorbed)[0],
                               fit["rate_relative_to_base"] * _OPUS_IN)

    def test_a_family_with_no_list_price_keeps_the_fitted_rate(self):
        credits = copy.deepcopy(CREDITS)
        credits.setdefault("list_price_model", {})["opus-5-5"] = "claude-not-in-the-price-table"
        self.assertIsNone(C.list_price_ratio("opus-5-5", credits))
        absorbed, fits = self._absorb(_mixed(share=0.5), credits)
        (fit,) = fits.values()
        self.assertFalse(fit["separable"])
        self.assertIs(absorbed, self.rates)
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", credits, absorbed)[0], 0.55 * _OPUS_IN)

    def test_a_family_first_used_before_any_measured_before_side_keeps_its_fit(self):
        # Every account's first stretch already carries the new family's predecessor, but no
        # account has ANNOUNCED_MIN_BEFORE stretches before it: the record holds no limit
        # change at its first use to confound the rate with, as for Fable and Haiku.
        by = _mixed(share=0.5)
        for name, rows in by.items():
            by[name] = rows[:1] + [r for r in rows if "claude-opus-5-5" in r["tokens"]]
        absorbed, fits = self._absorb(by)
        (fit,) = fits.values()
        self.assertEqual(fit["accounts_combined"], [])
        self.assertIs(absorbed, self.rates)
        self.assertAlmostEqual(C.comparison_rate("opus-5-5", CREDITS, absorbed)[0], 0.55 * _OPUS_IN)

    def test_an_older_family_with_a_final_fitted_rate_is_unchanged(self):
        absorbed, _ = self._absorb(_mixed(share=0.5))
        self.assertEqual(absorbed["per_family"]["sonnet"], self.rates["per_family"]["sonnet"])
        self.assertEqual(absorbed["pooled_fit"]["times_opus"]["sonnet"], 0.3625)
        self.assertAlmostEqual(C.comparison_rate("sonnet", CREDITS, absorbed)[0], 0.3625 * _OPUS_IN)
        self.assertEqual(C.family_rate("sonnet", CREDITS, absorbed),
                         C.family_rate("sonnet", CREDITS, self.rates))

    def test_the_published_block_says_which_rate_is_in_use(self):
        absorbed, _ = self._absorb(_mixed(share=0.5))
        block = P._measured_rates_block(absorbed, LABELS)["per_family"]
        new = block["opus-5-5"]
        self.assertEqual((new["rate_in_use"], new["times_opus"]), ("list", 0.55))
        self.assertAlmostEqual(new["list_times_opus"], self.list_ratio)
        self.assertIn("not used", new["status"])
        old = block["sonnet"]
        self.assertEqual((old["rate_in_use"], old["times_opus"]), ("fitted", 0.3625))
        self.assertAlmostEqual(old["list_times_opus"], C.list_price_ratio("sonnet", CREDITS))
        self.assertEqual(block["opus"]["rate_in_use"], "anchor")


class ComparisonValueTests(unittest.TestCase):
    def test_every_family_at_the_pooled_point_estimate_published_or_not(self):
        value = C.comparison_value(CREDITS, _POOLED_RATES)
        tok = {"input": 100, "cache_write": 50, "output": 10, "cache_read": 1000}
        cache = 1000 * 0.0047 * _OPUS_IN
        self.assertAlmostEqual(value({"claude-haiku-4-5": tok}),
                               (150 + 50) * 0.70 * _OPUS_IN + cache)
        self.assertAlmostEqual(value({"claude-sonnet-5": tok}),
                               (150 + 50) * 0.3625 * _OPUS_IN + cache)
        self.assertEqual(value({"claude-sonnet-5": tok}),
                         C.comparison_value(CREDITS, _unpublished(_POOLED_RATES))({"claude-sonnet-5": tok}))

    def test_without_a_pooled_fit_it_is_the_across_cut_fallback(self):
        tok = {"input": 100, "cache_write": 50, "output": 10}
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5"):
            self.assertAlmostEqual(C.comparison_value(CREDITS, {})({model: tok}),
                                   C.across_cut_value(CREDITS)({model: tok}))
        # A family on no table at all is priced at its list-price ratio, not dropped.
        ratio = C.list_price_ratio("opus-5-5", CREDITS)
        self.assertAlmostEqual(C.comparison_value(CREDITS, {})({"claude-opus-5-5": tok}),
                               (150 + 50) * ratio * _OPUS_IN)

    def test_empty_bundles_are_skipped_and_unknown_models_unpriced(self):
        value = C.comparison_value(CREDITS, _POOLED_RATES)
        tok = {"output": 10}
        self.assertEqual(value({"claude-opus-5": tok, "<synthetic>": {"output": 0}}),
                         value({"claude-opus-5": tok}))
        self.assertIsNone(value({"some-other-vendor-model": tok}))


class RoundingWeightTests(unittest.TestCase):
    """Each stretch weighs 1 / (scatter + its own whole-percent rounding variance)."""

    def test_rounding_variance_is_windows_over_six_delta_squared(self):
        self.assertAlmostEqual(C.rounding_variance({"delta_pct": 10.0, "windows": 1}), 1 / 600)
        self.assertAlmostEqual(C.rounding_variance({"delta_pct": 5.0, "windows": 3}), 3 / 150)
        self.assertEqual(C.rounding_variance({"delta_pct": 0}), 0.0)

    def test_equal_rounding_is_the_plain_two_sample_interval(self):
        rng = random.Random(3)
        before = [rng.gauss(0, 0.2) for _ in range(12)]
        after = [rng.gauss(0.2, 0.2) for _ in range(6)]
        plain = C.log_ratio_side(before, after)
        same = C.log_ratio_side(before, after, [0.002] * 12, [0.002] * 6)
        self.assertAlmostEqual(same["log_ratio"], plain["log_ratio"])
        for a, b in zip(same["interval"], plain["interval"]):
            self.assertAlmostEqual(a, b)

    def test_a_stretch_that_is_mostly_rounding_counts_for_less(self):
        # Nine before stretches at level 0, one whose 3% over four window pieces read half a
        # log point high: an error its rounding alone (4 / 54 = 0.074) can make. After: 0.2.
        before = [0.01 * (-1) ** i for i in range(9)] + [0.5]
        rounding_before = [1 / 600] * 9 + [4 / (6 * 3 ** 2)]
        after = [0.2 + 0.01 * (-1) ** i for i in range(6)]
        # Both the rounding weight and the Huber weight (`huber_location`) now count it for
        # less, so the unweighted call is no longer a foil; either way the step stays 0.2.
        weighted = C.log_ratio_side(before, after, rounding_before, [1 / 600] * 6)
        self.assertLess(abs(weighted["log_ratio"] - 0.2), 0.02)
        self.assertLess(weighted["scatter_sd"], weighted["sd"])

    def test_the_joint_fit_weights_a_rounding_heavy_after_stretch_down(self):
        # _mixed at a known 20% limit change, then three after stretches spread over six window
        # pieces each with their meter reading 40% high -- inside what six pieces of rounding
        # allow on a small movement, and far outside the fixture's own scatter.
        by = _mixed(g=1.2, r=0.6, sd=0.02)
        after = [st for st in by["acct_one"] if datetime.fromisoformat(st["start"]) >= CAND]
        for st in sorted(after, key=lambda s: s["delta_pct"])[:3]:
            st["delta_pct"] *= 1.4
            st["windows"] = 6
        groups = [C._joint_rows(by[n], "opus-5-5", CAND, C.CUT_AT, None, _output_value, CREDITS)
                  for n in sorted(by)]
        plain = C._joint_solve(groups)
        fit = _joint(by)
        self.assertLess(abs(fit["five_hour_limit_change_pct"] - 20.0),
                        abs((math.exp(plain[1]) - 1) * 100 - 20.0) / 2)
        self.assertLess(abs(fit["rate_relative_to_base"] - 0.6), abs(plain[0] - 0.6) / 2)


class UnclaimedShareTests(unittest.TestCase):
    """The pooled root's unclaimed work: each account's share of it is measured, not assumed."""

    def _pooled(self, seed=5):
        # One account whose meter carried every unclaimed bundle: its own tokens are the meter's
        # credits less a varying unclaimed part, so leaving the part out scatters the stretches.
        rng = random.Random(seed)
        out = []
        for i, st in enumerate(_series(T0, 20, "claude-opus-5", 1000.0, rng, sd=0.02)
                               + _series(CAND, 8, "claude-opus-5", 1200.0, rng, sd=0.02)):
            part = st["tokens"]["claude-opus-5"]["output"] * (0.1 + 0.5 * (i % 3) / 2)
            st["tokens"]["claude-opus-5"]["output"] -= part
            st["unclaimed_tokens"] = {"claude-opus-5": {"output": part}}
            out.append(st)
        return {"acct_one": out}

    def test_the_share_is_measured_and_takes_the_scatter_out(self):
        by = self._pooled()
        shares = C.unclaimed_shares(by, _output_value, CAND, C.CUT_AT, None, LABELS, sorted(by))
        self.assertGreaterEqual(shares["a1"]["share"], 0.9)
        self.assertEqual(shares["a1"]["n_with_unclaimed"], 28)
        lo, hi = shares["a1"]["interval"]
        self.assertLessEqual(lo, shares["a1"]["share"])
        self.assertLessEqual(shares["a1"]["share"], hi)
        without = C.split_at_candidate(by["acct_one"], CAND, C.CUT_AT, None, _output_value)
        withit = C.split_at_candidate(by["acct_one"], CAND, C.CUT_AT, None, _output_value,
                                      shares["a1"]["share"])
        self.assertLess(statistics.stdev(withit["sides"]["before"]),
                        statistics.stdev(without["sides"]["before"]) / 3)
        ratio = math.exp(statistics.mean(withit["sides"]["after"])
                         - statistics.mean(withit["sides"]["before"]))
        self.assertAlmostEqual(ratio, 1.2, delta=0.03)

    def test_work_another_meter_carried_gets_no_share(self):
        # The unclaimed bundles are noise here: the account's own tokens already match its meter.
        rng = random.Random(9)
        by = {"acct_one": _series(T0, 20, "claude-opus-5", 1000.0, rng, sd=0.02)}
        for i, st in enumerate(by["acct_one"]):
            st["unclaimed_tokens"] = {"claude-opus-5": {"output": 800.0 * (i % 4)}}
        shares = C.unclaimed_shares(by, _output_value, CAND, C.CUT_AT, None, LABELS, sorted(by))
        self.assertLessEqual(shares["a1"]["share"], 0.04)

    def test_two_accounts_reading_one_pool_share_at_most_all_of_it(self):
        # Issue #133: Max accounts 2 and 4 read the same pooled root, so the same unclaimed
        # turns sit in both accounts' overlapping stretches. Fitted apart, each took nearly
        # all of them (1.0 and 0.82 live); fitted together they sum to at most 1.
        one = self._pooled()["acct_one"]
        two = copy.deepcopy(one)
        by = {"acct_one": one, "acct_two": two}
        alone = C.unclaimed_shares({"acct_two": two}, _output_value, CAND, C.CUT_AT, None, LABELS, ["acct_two"])
        self.assertGreaterEqual(alone["a2"]["share"], 0.9)
        shares = C.unclaimed_shares(by, _output_value, CAND, C.CUT_AT, None, LABELS, sorted(by))
        self.assertLessEqual(shares["a1"]["share"] + shares["a2"]["share"], 1.0 + 1e-9)
        self.assertEqual((shares["a1"]["capped_with"], shares["a2"]["capped_with"]), (["a2"], ["a1"]))

    def test_accounts_whose_unclaimed_work_never_coincides_are_fitted_alone(self):
        one = self._pooled()["acct_one"]
        two = copy.deepcopy(one)
        for st in two:  # the same pattern, each in the hour between two of acct_one's stretches
            start = datetime.fromisoformat(st["start"]) + timedelta(minutes=135)
            st["start"], st["end"] = start.isoformat(), (start + timedelta(minutes=30)).isoformat()
        by = {"acct_one": one, "acct_two": two}
        shares = C.unclaimed_shares(by, _output_value, CAND, C.CUT_AT, None, LABELS, sorted(by))
        self.assertGreaterEqual(shares["a1"]["share"], 0.9)
        self.assertGreaterEqual(shares["a2"]["share"], 0.9)
        self.assertEqual(shares["a1"]["capped_with"], [])

    def test_a_zero_length_stretch_with_unclaimed_work_overlaps_nothing(self):
        # Review round 1 on #108: its end sorted before its start and raised KeyError.
        at = datetime(2026, 9, 25, tzinfo=timezone.utc)
        rows = {"acct_one": [(1.0, 1.0, 10.0, at, at)],
                "acct_two": [(1.0, 1.0, 10.0, at - timedelta(hours=1), at + timedelta(hours=1))]}
        self.assertEqual(C._unclaimed_caps(rows), [])
        rows["acct_one"].append((1.0, 1.0, 10.0, at, at + timedelta(minutes=5)))
        self.assertEqual(C._unclaimed_caps(rows), [frozenset({"acct_one", "acct_two"})])

    def test_four_overlapping_accounts_are_solved_by_one_budget(self):
        # Review round 1 on #108: the exact search took ~26 s per call for a group of 4. Above
        # CAPPED_EXACT_MAX the group shares one budget; where the whole group is one cap that is
        # the exact answer, found by both methods.
        import itertools
        grid = [i / 10 for i in range(11)]
        best = {"a": 0.9, "b": 0.5, "c": 0.3, "d": 0.1}
        ss = {n: [(g - b) ** 2 + 0.001 * k * g for g in grid] for k, (n, b) in enumerate(best.items())}
        solved = C._capped_best(ss, [frozenset(best)], grid)
        self.assertLessEqual(sum(solved.values()), 1.0 + 1e-9)
        brute = min(sum(ss[m][i] for m, i in zip("abcd", idx))
                    for idx in itertools.product(range(11), repeat=4) if sum(idx) <= 10)
        self.assertAlmostEqual(sum(ss[m][round(v * 10)] for m, v in solved.items()), brute)
        exact = C._capped_best({k: ss[k] for k in "abc"}, [frozenset("abc")], grid)
        self.assertEqual({m: grid[i] for m, i in C._one_budget(ss, list("abc"), 10).items()}, exact)

    def test_an_account_with_no_unclaimed_work_is_not_listed(self):
        by = _fixture()
        self.assertEqual(C.unclaimed_shares(by, _output_value, CAND, C.CUT_AT, None, LABELS, sorted(by)), {})
        st = by["acct_one"][3]
        self.assertEqual(C.stretch_amount(st, _output_value, 1.0), _output_value(st["tokens"]))


class MissingWorkTests(unittest.TestCase):
    """Stretches whose transcripts missed work the meter counted read far low of their level;
    the Huber weights keep a few of them from moving the change."""

    def test_the_known_date_test_is_not_pulled_by_missing_work(self):
        rng = random.Random(8)
        before = [rng.gauss(0, 0.1) for _ in range(30)]
        after = [0.2 + rng.gauss(0, 0.1) for _ in range(12)]
        # A burst of four after-side stretches that lost 60% of their work.
        hit = after[:8] + [x + math.log(0.4) for x in after[8:]]
        clean, got = C.log_ratio_side(before, after), C.log_ratio_side(before, hit)
        naive = sum(hit) / len(hit) - sum(before) / len(before)
        self.assertLess(abs(got["log_ratio"] - clean["log_ratio"]), abs(naive - clean["log_ratio"]) / 2)
        self.assertGreaterEqual(got["n_downweighted"], 4)
        self.assertLess(got["scatter_sd"], got["sd"])

    def test_the_joint_fit_is_not_pulled_by_missing_work(self):
        by = _mixed(g=1.2, r=0.6, sd=0.05, seed=5)
        after = [st for st in by["acct_one"] if datetime.fromisoformat(st["start"]) >= CAND]
        for st in after[:4]:
            st["delta_pct"] /= 0.5     # the meter moved twice what the tokens explain
        groups = [C._joint_rows(by[n], "opus-5-5", CAND, C.CUT_AT, None, _output_value, CREDITS)
                  for n in sorted(by)]
        plain = (math.exp(C._joint_solve(groups)[1]) - 1) * 100
        fit = _joint(by)
        self.assertLess(abs(fit["five_hour_limit_change_pct"] - 20.0), abs(plain - 20.0) / 2)


class AnnouncementIndependenceTests(unittest.TestCase):
    """No announcement changes any computed figure: the committed history rebuilt with
    ANNOUNCEMENTS emptied publishes identical numbers, only the reference metadata gone."""

    @staticmethod
    def _strip(node):
        if isinstance(node, dict):
            return {k: AnnouncementIndependenceTests._strip(v) for k, v in node.items()
                    if k not in ("announcement", "announced")}
        if isinstance(node, list):
            return [AnnouncementIndependenceTests._strip(v) for v in node]
        return node

    def test_rebuild_with_announcements_emptied_is_identical(self):
        from pathlib import Path
        from unittest import mock

        from tracker.rebuild_offline import rebuild
        root = Path(__file__).resolve().parent.parent
        now = datetime(2026, 9, 28, 17, tzinfo=timezone.utc)
        with_notes = rebuild(root, now)
        with mock.patch.object(C, "ANNOUNCEMENTS", []):
            without = rebuild(root, now)
        cands = with_notes["credits"]["five_hour_on_meters"]["candidates"]
        self.assertTrue(any(c["announcement"] for c in cands), "the fixture must carry a note")
        self.assertFalse(any(c["announcement"]
                             for c in without["credits"]["five_hour_on_meters"]["candidates"]))
        self.assertEqual(self._strip(with_notes), self._strip(without))


class PowerTests(unittest.TestCase):
    """Acceptance: +20% after 10 stretches of 10 points on each account with a before side,
    residuals resampled from a frozen fixture, fixed seeds.

    The fixture holds each account's before-side log residuals as main produced them at
    52ad06b, the data this test was accepted on. The live history changes daily, so the
    test reads the fixture rather than it (docs/findings-2026-09-24-opus-5-5-rate.md)."""

    @classmethod
    def setUpClass(cls):
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parent / "fixtures" / "announced_change_regimes.json"
        cls.before = json.loads(path.read_text(encoding="utf-8"))["accounts"]

    def test_the_fixture_is_the_accepted_regime(self):
        def sd(xs):
            m = sum(xs) / len(xs)
            return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
        self.assertEqual({a: (len(xs), round(sd(xs), 3)) for a, xs in self.before.items()},
                         {"a1": (32, 0.22), "a2": (41, 0.507), "a3": (51, 0.437)})

    def test_accounts_with_a_before_side(self):
        self.assertGreaterEqual(len(self.before), 2)

    def test_twenty_percent_step_is_found(self):
        self.assertGreaterEqual(SIM.exclusion_rate(self.before, 0.20, 10, trials=200, seed=0), 0.70)

    def test_no_step_is_rarely_called(self):
        self.assertLessEqual(SIM.exclusion_rate(self.before, 0.0, 10, trials=200, seed=0), 0.10)


class RateCheckTests(unittest.TestCase):
    """ADR 0001 rule 8: a joint fit is separable only if its rate interval reaches half to
    twice the list-price ratio: Opus 5.5 lists at 0.8x Opus 5 on input, so the band is 0.4x to
    1.6x. This is on top of the span and mixing tests."""

    def test_the_band_is_read_off_the_input_list_prices(self):
        check = C.joint_rate_check("opus-5-5", "opus", CREDITS, [0.9, 1.2])
        self.assertEqual((check["state"], check["price_class"]), ("passed", "input"))
        self.assertEqual(check["list_price_ratio"], 0.8)
        self.assertEqual(check["plausible_band"], [0.4, 1.6])
        self.assertEqual(C.joint_rate_check("sonnet-5-5", "sonnet", CREDITS, [2.51, 7.2])["state"],
                         "failed")
        # A family with no list price skips the test and says so.
        skipped = C.joint_rate_check("nope-9", "opus", CREDITS, [1.0, 1.1])
        self.assertEqual(skipped["state"], "not_applicable")
        self.assertIn("no input list price", skipped["reason"])
        self.assertIn("skipped", skipped["reason"])

    def test_the_band_is_against_the_base_family_not_the_anchor(self):
        # Sonnet 5.5 and Sonnet 5 list alike, so the band is 0.5x to 2x Sonnet 5.
        check = C.joint_rate_check("sonnet-5-5", "sonnet", CREDITS, [1.23, 4.43])
        self.assertEqual((check["list_price_ratio"], check["plausible_band"]), (1.0, [0.5, 2.0]))
        self.assertEqual(check["state"], "passed")  # it overlaps; the span test is what fails it
        self.assertIsNone(check["reason"])

    def test_an_implausible_new_family_rate_leaves_the_fit_unseparated(self):
        # The meter charges Opus 5.5 at 3x Opus 5, which no list price explains.
        fit = _joint(_mixed(g=1.0, r=3.0))
        lo, hi = fit["rate_relative_interval"]
        self.assertGreater(lo, 1.6)
        self.assertLess(hi / lo, C.JOINT_SEPARABLE_SPAN)  # pinned, so only the check refuses it
        self.assertEqual(fit["rate_check"]["state"], "failed")
        self.assertFalse(fit["separable"])
        self.assertEqual(fit["state"], "measuring")
        self.assertIn("rate check (ADR 0001 rule 8)", fit["reason"])
        self.assertIn("has not separated the rate", fit["reason"])
        self.assertIsNone(fit["plan_wide"])  # an unseparated g is not refitted
        meters = C.five_hour_on_meters(_announced(joint_fit=fit), _max20())
        cand = meters["candidates"][0]
        self.assertIsNone(cand["change_pct"])
        self.assertEqual(cand["scope"]["state"], "undetermined")
        self.assertIn("has not separated the rate", cand["scope"]["reason"])
        # The g it refused is not the headline: the windows-per-week change is.
        self.assertEqual(cand["headline"]["metric"], "windows_per_week")
        # Withheld on its own interval, the reason also names the rate check.
        thin = C.five_hour_on_meters(_announced(joint_fit=fit), _max20(n_after=2))["candidates"][0]
        self.assertFalse(thin["applies"])
        self.assertIn("rate check (ADR 0001 rule 8)", thin["withheld_reason"])
        rates, _ = C.absorb_new_family_rates(_mixed(g=1.0, r=3.0), [], CREDITS, {}, LABELS,
                                             lambda rates: _output_value)
        self.assertEqual(rates, {})  # nothing absorbed

    def test_a_plausible_rate_passes(self):
        fit = _joint(_mixed(g=1.2, r=0.6))
        self.assertEqual(fit["rate_check"]["state"], "passed")
        self.assertTrue(fit["separable"])


def _mixed_accounts(steps, r=0.6, n=24, sd=0.08, seed=5):
    """`_mixed` on any accounts, each with its own five-hour step: `steps` maps an account
    name to the g its meter moves by at CAND. A plan change moves every account by one g."""
    rng = random.Random(seed)
    by = {}
    for k, (name, g) in enumerate(sorted(steps.items())):
        level = 100.0 + 40.0 * k
        rows = []
        for i in range(n):
            known = rng.uniform(500, 3000)
            st = _st(C.CUT_AT + timedelta(hours=3 * (i + 1)), 2, "claude-opus-5", known)
            st["delta_pct"] = known / level * math.exp(rng.gauss(0, sd))
            rows.append(st)
        for i in range(n):
            total, s_new = rng.uniform(500, 3000), rng.uniform(0.05, 0.95)
            known, new = total * (1 - s_new), total * s_new
            st = _st(CAND + timedelta(hours=3 * (i + 1)), 2, "claude-opus-5", known)
            st["tokens"]["claude-opus-5-5"] = {"output": new}
            st["delta_pct"] = (known + r * new) / (level * g) * math.exp(rng.gauss(0, sd))
            rows.append(st)
        by[name] = rows
    return by


def _max20_accounts(after_wpw):
    """`_max20` with each account's own windows per week after CAND (5.0 before)."""
    rows = []
    for label, wpw in sorted(after_wpw.items()):
        rows += [_window(label, C.CUT_AT + timedelta(hours=6 * (i + 1)), 50.0, 10.0)
                 for i in range(20)]
        rows += [_window(label, CAND + timedelta(hours=6 * (i + 1)), 50.0, 50.0 / wpw)
                 for i in range(10)]
    return {"by_window": rows, "by_account": {}}


class PlanWideTests(unittest.TestCase):
    """A plan limit change moves every account at once, so it must hold with each one left out."""

    def test_an_artifact_on_one_account_is_rejected(self):
        # a1's meter steps 40%; a2 and a3 do not move. Combined, the fit still reads a rise.
        fit = _joint(_mixed_accounts({"acct_one": 1.4, "acct_two": 1.0, "acct_new": 1.0}))
        self.assertTrue(fit["separable"])
        self.assertEqual(fit["accounts_combined"], ["a1", "a2", "a3"])
        self.assertGreater(fit["five_hour_limit_change_interval_pct"][0], 0.0)
        pw = fit["plan_wide"]
        self.assertEqual(pw["state"], "failed")
        self.assertEqual(sorted(pw["without"]), ["a1", "a2", "a3"])
        self.assertFalse(pw["without"]["a1"]["holds"])
        self.assertEqual(pw["without"]["a1"]["accounts_combined"], ["a2", "a3"])
        self.assertIn("without a1", pw["reason"])
        cand = C.five_hour_on_meters(_announced(joint_fit=fit), _max20())["candidates"][0]
        self.assertEqual(cand["plan_wide"], pw)
        self.assertFalse(cand["applies"])
        self.assertIn("plan-wide test (ADR 0001 rule 9)", cand["withheld_reason"])

    def test_a_change_shared_by_every_account_passes(self):
        fit = _joint(_mixed_accounts({"acct_one": 1.2, "acct_two": 1.2, "acct_new": 1.2}))
        pw = fit["plan_wide"]
        self.assertEqual(pw["state"], "passed", pw["reason"])
        self.assertIsNone(pw["reason"])
        for label in ("a1", "a2", "a3"):
            row = pw["without"][label]
            self.assertTrue(row["holds"])
            self.assertGreater(row["interval_pct"][0], 0.0)
            # The refit is the same estimator on the other accounts: its own rate and g.
            self.assertTrue(row["separable"])

    def test_with_two_accounts_each_one_alone_must_show_it(self):
        # 48 stretches a side, so the combined fit passes the 1.5x span test and is refitted.
        fit = _joint(_mixed_accounts({"acct_one": 1.4, "acct_two": 1.0}, n=48, sd=0.04))
        self.assertTrue(fit["separable"], fit["reason"])
        pw = fit["plan_wide"]
        self.assertEqual(pw["state"], "failed")
        self.assertEqual(pw["without"]["a1"]["accounts_combined"], ["a2"])
        self.assertEqual(pw["without"]["a2"]["accounts_combined"], ["a1"])
        self.assertTrue(pw["without"]["a2"]["holds"])   # a1 alone shows it
        self.assertFalse(pw["without"]["a1"]["holds"])  # a2 alone does not

    def test_an_inseparable_fit_is_tested_on_windows_per_week(self):
        # The published figure is then the windows-per-week change, so that is what is refitted.
        shared = C.five_hour_on_meters(_announced(), _max20_accounts({"a1": 4.0, "a2": 4.0, "a3": 4.0}))
        pw = shared["candidates"][0]["plan_wide"]
        self.assertEqual((pw["state"], pw["estimator"]), ("passed", "the combined windows-per-week ratio"))
        self.assertEqual(pw["without"]["a1"]["change_pct"], -20.0)
        one = C.five_hour_on_meters(_announced(), _max20_accounts({"a1": 3.0, "a2": 5.0, "a3": 5.0}))
        cand = one["candidates"][0]
        self.assertLess(cand["windows_per_week_change_interval_pct"][1], 0.0)
        self.assertEqual(cand["plan_wide"]["state"], "failed")
        self.assertFalse(cand["plan_wide"]["without"]["a1"]["holds"])

    def test_a_change_resting_on_one_account_is_not_plan_wide(self):
        cand = C.five_hour_on_meters(_announced(), _max20_accounts({"a1": 3.0}))["candidates"][0]
        self.assertEqual(cand["accounts_combined"], ["a1"])
        self.assertEqual(cand["plan_wide"]["state"], "failed")
        self.assertIn("rests on one account", cand["plan_wide"]["reason"])


if __name__ == "__main__":
    unittest.main()
