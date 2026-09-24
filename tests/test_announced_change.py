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


class EventTests(unittest.TestCase):
    def test_measured_excluding_one_enters_events_and_last_change(self):
        block = C.announced_change(_fixture(step=1.6), [], CREDITS, _output_value, LABELS)
        cand = block["candidates"][0]
        self.assertTrue(cand["interval_excludes_no_change"])
        events = P._announced_events(block)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["date"], "2026-09-22")
        self.assertEqual(ev["direction"], "increased")
        self.assertTrue(ev["known_date_test"])
        self.assertEqual(ev["kind"], "change")
        older = {"date": "2026-09-11", "percent": 26}
        self.assertEqual(P._with_announced_last_change(older, block)["date"], "2026-09-22")
        self.assertNotIn("label", P._with_announced_last_change(older, block))

    def test_not_measured_stays_out_of_events(self):
        block = C.announced_change(_fixture(n_after_one=6, step=1.6), [], CREDITS,
                                   _output_value, LABELS)
        self.assertEqual(P._announced_events(block), [])
        older = {"date": "2026-09-11"}
        self.assertIs(P._with_announced_last_change(older, block), older)


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
