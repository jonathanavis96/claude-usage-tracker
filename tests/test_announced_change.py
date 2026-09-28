"""The known-date change test: candidates from first-seen families, states, events, power."""
import copy
import math
import random
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


def _announced(note=FIVE_HOUR_NOTE, at=CAND, unconfounded=None):
    return {"candidates": [{"family": "opus-5-5", "at": at.isoformat(), "at_source": "first_turn",
                            "first_seen_account": "a1", "first_seen_stretch_end": at.isoformat(),
                            "before_from": C.CUT_AT.isoformat(), "after_until": None,
                            "announcement": note, "unconfounded": unconfounded}]}


def _unconfounded(ratio, interval, n_after=10, state="measured"):
    """An `announced_change` `unconfounded` block: the five-hour meter's cost per 1% moved by
    `ratio` on work the candidate did not reprice."""
    return {"without_family": "opus-5-5", "state": state, "ratio": ratio,
            "ratio_interval": list(interval), "accounts_combined": ["a1"],
            "per_account": {"a1": {"n_before": 20, "n_after": n_after}}}


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

    def test_without_unrepriced_work_the_scope_is_undetermined_and_no_window_change_is_claimed(self):
        cand = C.five_hour_on_meters(_announced(), _max20())["candidates"][0]
        self.assertEqual(cand["scope"]["state"], "undetermined")
        self.assertIn("too few stretches", cand["scope"]["reason"])
        self.assertIsNone(cand["change_pct"])
        self.assertIsNone(cand["interval_pct"])

    def test_scope_is_decided_by_which_meters_cost_moved(self):
        # The five-hour meter's cost per 1% up 11%, and the weekly's (that times 0.9) flat.
        five = C.five_hour_on_meters(_announced(unconfounded=_unconfounded(1.111, (1.05, 1.17))),
                                     _max20())["candidates"][0]
        self.assertEqual(five["scope"]["state"], "five_hour")
        self.assertEqual(five["change_pct"], 11.1)
        # The five-hour meter's cost flat, so the weekly's fell with the ratio.
        weekly = C.five_hour_on_meters(_announced(unconfounded=_unconfounded(1.0, (0.99, 1.01))),
                                       _max20(n_before=200, n_after=200))["candidates"][0]
        self.assertEqual(weekly["scope"]["state"], "weekly")
        self.assertEqual(weekly["change_pct"], 0.0)
        # Too few readings of the unrepriced work: undetermined whatever it says.
        thin = C.five_hour_on_meters(
            _announced(unconfounded=_unconfounded(1.111, (1.05, 1.17), n_after=3, state="measuring")),
            _max20())["candidates"][0]
        self.assertEqual(thin["scope"]["state"], "undetermined")

    def test_a_known_twenty_percent_five_hour_step_is_published_as_twenty_percent(self):
        # Windows per week falls from 5 to 5 / 1.2, the weekly cap unchanged: +20% window.
        m = _max20(n_before=30, n_after=30, after_wpw=5.0 / 1.2)
        cand = C.five_hour_on_meters(_announced(unconfounded=_unconfounded(1.2, (1.1, 1.3))),
                                     m)["candidates"][0]
        self.assertEqual(cand["scope"]["state"], "five_hour")
        self.assertAlmostEqual(cand["change_pct"], 20.0, delta=0.1)
        self.assertAlmostEqual(cand["windows_per_week_change_pct"], (1 / 1.2 - 1) * 100, delta=0.1)
        # Before the scope is known the five-hour reading carries the same +20%.
        undecided = C.five_hour_on_meters(_announced(), m)["candidates"][0]
        self.assertAlmostEqual(
            undecided["readings"]["five_hour_scope"]["five_hour_window_change_pct"], 20.0, delta=0.1)
        ev = P._announced_events(C.five_hour_on_meters(
            _announced(unconfounded=_unconfounded(1.2, (1.1, 1.3))), m))[0]
        self.assertEqual((ev["scope"], ev["percent"], ev["direction"]), ("five_hour", 20, "increased"))

    def test_states_follow_readings_after(self):
        for n, state in ((4, "measuring"), (5, "provisional"), (9, "provisional"), (10, "measured")):
            with self.subTest(n=n):
                cand = C.five_hour_on_meters(_announced(), _max20(n_after=n))["candidates"][0]
                self.assertEqual(cand["state"], state)
                self.assertEqual(cand["applies"], state == "measured")

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
        self.assertIn("scope undetermined", ev["label"])
        self.assertTrue(ev["known_date_test"])
        older = {"date": "2026-09-11", "percent": 26}
        last = P._with_announced_last_change(older, block)
        self.assertEqual((last["date"], last["method"]), ("2026-09-22", "windows_per_week_ratio"))
        self.assertNotIn("label", last)

    def test_the_credits_test_no_longer_drives_events(self):
        credits_block = C.announced_change(_fixture(step=1.6), [], CREDITS, _output_value, LABELS)
        self.assertTrue(credits_block["candidates"][0]["interval_excludes_no_change"])
        self.assertEqual(P._announced_events(credits_block), [])

    def test_not_measured_stays_out_of_events(self):
        block = C.five_hour_on_meters(_announced(), _max20(n_after=6))
        self.assertEqual(P._announced_events(block), [])
        older = {"date": "2026-09-11"}
        self.assertIs(P._with_announced_last_change(older, block), older)


class UnconfoundedTests(unittest.TestCase):
    def test_the_credits_test_on_work_without_the_candidates_family(self):
        rng = random.Random(3)
        by = _fixture()
        # Opus 5 work either side of the candidate, its cost per 1% up 20% after it.
        by["acct_one"] += _series(CAND + timedelta(days=2), 12, "claude-opus-5", 1200.0, rng, sd=0.05)
        cand = C.announced_change(by, [], CREDITS, _output_value, LABELS)["candidates"][0]
        test = cand["unconfounded"]
        self.assertEqual(test["without_family"], "opus-5-5")
        self.assertEqual(test["per_account"]["a1"], {"n_before": 20, "n_after": 12})
        self.assertEqual(test["state"], "measured")
        self.assertAlmostEqual(test["ratio"], 1.2, delta=0.1)


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
