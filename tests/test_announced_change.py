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
        self.assertEqual(cand["state"], "measured")
        self.assertEqual(cand["before_from"], C.CUT_AT.isoformat())

    def test_states_follow_after_counts(self):
        self.assertEqual(self._block(n_after_one=4)["candidates"][0]["state"], "measuring")
        self.assertEqual(self._block(n_after_one=5)["candidates"][0]["state"], "provisional")
        self.assertEqual(self._block(n_after_one=9)["candidates"][0]["state"], "provisional")
        self.assertEqual(self._block(n_after_one=10)["candidates"][0]["state"], "measured")

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


def _mixed(g=1.2, r=0.6, n=24, sd=0.08, seed=5, share=None):
    """Two accounts' stretches around CAND: known-rate Opus 5 work at 100 credits per 1%
    before; after it Opus 5 and Opus 5.5 mixed, the Opus 5.5 share varying stretch to
    stretch (or fixed at `share`), the new family charged at `r` times Opus 5 and the
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
            s_new = share if share is not None else rng.uniform(0.05, 0.95)
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

    def test_a_separable_fit_moves_the_regimes_at_once_whatever_its_intervals(self):
        # A small, noisy +8% step: separable, but both intervals include no change.
        fit = _joint(_mixed(g=1.08, r=0.6, sd=0.25, seed=11))
        self.assertTrue(fit["separable"])
        g_lo, g_hi = fit["five_hour_limit_change_interval_pct"]
        self.assertLess(g_lo, 0.0)
        self.assertGreater(g_hi, 0.0)
        meters = C.five_hour_on_meters(_announced(joint_fit=fit), _max20())
        cand = meters["candidates"][0]
        self.assertEqual(cand["scope"]["state"], "undetermined")
        self.assertEqual(cand["change_pct"], fit["five_hour_limit_change_pct"])
        (change,) = C.known_date_changes(meters)
        self.assertTrue(change["window_scaled"])
        self.assertAlmostEqual(change["ratio"], 1 + fit["five_hour_limit_change_pct"] / 100)
        (ev,) = P._announced_events(meters)
        g, w = cand["change_pct"], cand["scope"]["weekly_limit_change_pct"]
        self.assertEqual(ev["label"], f"Five-hour limit {g:+g}%, weekly limit {w:+g}% (measured)")
        self.assertFalse(ev["interval_excludes_no_change"])
        self.assertNotIn("announce", ev["label"].lower())

    def test_a_fit_that_cannot_separate_carries_the_window(self):
        meters = C.five_hour_on_meters(_announced(joint_fit=_joint(_mixed(share=0.5))), _max20())
        self.assertIsNone(meters["candidates"][0]["change_pct"])
        (change,) = C.known_date_changes(meters)
        self.assertFalse(change["window_scaled"])
        self.assertEqual(change["ratio"], 1.0)

    def test_states_follow_readings_after_and_every_state_applies(self):
        # A change applies as soon as it can be measured at all; `state` says how settled.
        for n, state in ((1, "measuring"), (4, "measuring"), (5, "provisional"),
                         (9, "provisional"), (10, "measured")):
            with self.subTest(n=n):
                cand = C.five_hour_on_meters(_announced(), _max20(n_after=n))["candidates"][0]
                self.assertEqual(cand["state"], state)
                self.assertTrue(cand["applies"])

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

    def test_a_measuring_change_enters_events_at_once_with_its_state_and_instant(self):
        block = C.five_hour_on_meters(_announced(), _max20(n_after=2))
        (ev,) = P._announced_events(block)
        self.assertEqual((ev["state"], ev["evidence_quality"]), ("measuring", "measuring"))
        self.assertEqual(ev["at"], CAND.isoformat())
        self.assertFalse(ev["provisional"])
        last = P._with_announced_last_change({"date": "2026-09-11"}, block)
        self.assertEqual((last["date"], last["state"]), ("2026-09-22", "measuring"))

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
        plain = C.log_ratio_side(before, after)
        weighted = C.log_ratio_side(before, after, rounding_before, [1 / 600] * 6)
        self.assertLess(abs(weighted["log_ratio"] - 0.2), abs(plain["log_ratio"] - 0.2) / 3)
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

    def test_an_account_with_no_unclaimed_work_is_not_listed(self):
        by = _fixture()
        self.assertEqual(C.unclaimed_shares(by, _output_value, CAND, C.CUT_AT, None, LABELS, sorted(by)), {})
        st = by["acct_one"][3]
        self.assertEqual(C.stretch_amount(st, _output_value, 1.0), _output_value(st["tokens"]))


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


if __name__ == "__main__":
    unittest.main()
