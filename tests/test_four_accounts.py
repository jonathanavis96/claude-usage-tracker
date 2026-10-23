"""tracker.four_accounts: the four-account limit-change detector (ADR 0001 rule 22).

Three kinds of test:

- the rule on synthetic accounts with fixed seeds: a step every account takes is a change, a
  step one account takes alone is not, the accounts must agree, the step must hold with any
  one account left out, and p must be under 0.001 by permutation;
- the hold: candidate, provisional, measured after 7 runs in a row, carried after that and
  never withdrawn;
- recovery on the frozen readings of 2026-10-23 (tests/fixtures/four_accounts, written by
  `python3 -m tracker.four_accounts --now 2026-10-23T00:00Z --snapshot`, valued at the rates
  in force that day): with no date given, every daily run replayed finds 14 September on the
  meters' ratio and 22 September on the seven-day line, and nothing at 17 October with the
  masterrig-local work in; and the null: with the found steps divided out and each account's
  readings shifted in time by its own offset, nothing is found.
"""
from __future__ import annotations

import functools
import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from tracker import four_accounts as F

FIXTURE = Path(__file__).parent / "fixtures" / "four_accounts" / "readings-2026-10-23.json"
UTC = timezone.utc
DAY = 24.0


# ------------------------------------------------------------------------------- synthetic
def synth(steps: dict[str, float], *, days: int = 40, step_day: int = 20, noise: float = 0.08,
          per_day: int = 6, seed: int = 7, base: dict[str, float] | None = None) -> F.Prepared:
    """Accounts a1.. with `per_day` readings a day of 1-4 meter points each, work per point
    lognormal around the account's own level (`base`), times 1 + `steps[a]` from `step_day`."""
    rng = np.random.default_rng(seed)
    t_step = step_day * DAY
    by = {}
    for i, a in enumerate(sorted(steps)):
        level = (base or {}).get(a, 1000.0 * (1 + 0.3 * i))
        tm = np.sort(rng.uniform(0, days * DAY, days * per_day))
        D = rng.integers(1, 5, tm.size).astype(float)
        f = np.where(tm >= t_step, 1 + steps[a], 1.0)
        W = level * f * D * np.exp(rng.normal(0, noise, tm.size) / np.sqrt(D))
        by[a] = (tm - 0.5, tm + 0.5, W, D)
    return F.prepare(by)


def at_step(p: F.Prepared, step_day: int = 20, n_perm: int = F.PERMUTATIONS,
            screen: float | None = F.Z_SCREEN) -> dict | None:
    tau = step_day * DAY
    return F.step_at(p, "seven_day", tau, tau - F.SIDE_DAYS * DAY, tau + F.SIDE_DAYS * DAY,
                     seed=11, n_perm=n_perm, screen=screen)


class RuleTest(unittest.TestCase):
    """The flag: p < ALPHA by permutation, every voting account agreeing, plan-wide."""

    def test_a_step_every_account_takes_is_a_change(self):
        r = at_step(synth({"a1": 0.2, "a2": 0.2, "a3": 0.2, "a4": 0.2}))
        self.assertEqual(r["k"], 4)
        self.assertLess(r["p"], F.ALPHA)
        self.assertTrue(F.agreement(r)["agree"])
        self.assertTrue(F.plan_wide(r))
        self.assertTrue(F.flagged(r))
        self.assertAlmostEqual(r["d"], math.log(1.2), delta=0.04)

    def test_no_step_is_no_change(self):
        r = at_step(synth({"a1": 0.0, "a2": 0.0, "a3": 0.0, "a4": 0.0}), screen=None)
        self.assertGreater(r["p"], F.ALPHA)
        self.assertFalse(F.flagged(r))

    def test_a_step_one_account_takes_alone_is_a_counting_problem_on_it(self):
        r = at_step(synth({"a1": 0.6, "a2": 0.0, "a3": 0.0, "a4": 0.0}), screen=None)
        ag = F.agreement(r)
        self.assertFalse(ag["agree"])
        self.assertIn("a1", ag["accounts_apart"])
        # The account that moved is the one furthest from the rest.
        apart = max(r["acc"], key=lambda a: abs(r["acc"][a]["z_vs_others"]))
        self.assertEqual(apart, "a1")
        self.assertFalse(F.flagged(r))

    def test_accounts_stepping_opposite_ways_do_not_agree(self):
        r = at_step(synth({"a1": 0.4, "a2": 0.4, "a3": -0.3, "a4": 0.4}), screen=None)
        self.assertIn("a3", F.agreement(r)["accounts_apart"])
        self.assertFalse(F.flagged(r))

    def test_a_step_resting_on_one_account_fails_with_it_left_out(self):
        # Two accounts: a1 steps 50%, a2 steps 5%. Each agrees within AGREE_Z of the other at
        # this noise, but a2 alone does not hold the step: not plan-wide.
        r = at_step(synth({"a1": 0.5, "a2": 0.05}, noise=0.6), screen=None)
        self.assertEqual(r["k"], 2)
        self.assertFalse(F.plan_wide(r))
        self.assertFalse(F.flagged(r))

    def test_leave_one_out_is_each_account_against_the_others(self):
        r = at_step(synth({"a1": 0.2, "a2": 0.2, "a3": 0.2}))
        for a, x in r["acc"].items():
            others = [b for b in r["acc"] if b != a]
            ws = sum(1 / r["acc"][b]["var"] for b in others)
            d_o = sum(r["acc"][b]["d"] / r["acc"][b]["var"] for b in others) / ws
            self.assertAlmostEqual(x["d_others"], d_o)
            self.assertAlmostEqual(x["z_others"], d_o * math.sqrt(ws))

    def test_one_voting_account_is_never_a_change(self):
        r = at_step(synth({"a1": 0.5}))
        self.assertEqual(r["k"], 1)
        self.assertFalse(F.agreement(r)["agree"])
        self.assertFalse(F.flagged(r))

    def test_p_is_a_permutation_p_and_can_only_pass_with_enough_shuffles(self):
        p = synth({"a1": 0.3, "a2": 0.3, "a3": 0.3, "a4": 0.3})
        r = at_step(p)
        self.assertAlmostEqual(r["p"], 1 / (F.PERMUTATIONS + 1))
        # With 999 shuffles the smallest p is 1/1000, which is not under 0.001.
        few = at_step(p, n_perm=999)
        self.assertEqual(few["p"], 1 / 1000)
        self.assertFalse(F.flagged(few))

    def test_below_the_screen_no_permutation_runs(self):
        r = at_step(synth({"a1": 0.0, "a2": 0.0, "a3": 0.0, "a4": 0.0}))
        self.assertLess(abs(r["z"]), F.Z_SCREEN)
        self.assertIsNone(r["p"])
        self.assertFalse(F.flagged(r))

    def test_the_same_day_gives_the_same_p(self):
        p = synth({"a1": 0.08, "a2": 0.08, "a3": 0.08, "a4": 0.08}, noise=0.3)
        self.assertEqual(at_step(p, screen=None)["p"], at_step(p, screen=None)["p"])

    def test_sides_are_cut_at_the_neighbouring_changes(self):
        lo, hi = F.sides(500.0, [100.0, 480.0, 500.0, 640.0, 1000.0])
        self.assertEqual((lo, hi), (480.0, 640.0))
        lo, hi = F.sides(500.0, [])
        self.assertEqual((lo, hi), (500.0 - F.SIDE_DAYS * DAY, 500.0 + F.SIDE_DAYS * DAY))


class SearchTest(unittest.TestCase):
    """The whole-history search on synthetic accounts: no date is given anywhere."""

    def test_two_shared_steps_are_found_and_dated(self):
        rng = np.random.default_rng(3)
        by = {}
        for i, a in enumerate(("a1", "a2", "a3", "a4")):
            tm = np.sort(rng.uniform(0, 60 * DAY, 60 * 6))
            D = rng.integers(1, 5, tm.size).astype(float)
            f = np.where(tm >= 20 * DAY + 3, 0.8, 1.0) * np.where(tm >= 41 * DAY + 9, 1.3, 1.0)
            W = 1000 * (1 + 0.2 * i) * f * D * np.exp(rng.normal(0, 0.08, tm.size) / np.sqrt(D))
            by[a] = (tm - 0.5, tm + 0.5, W, D)
        prep = F.prepare(by)
        preps = {"seven_day": prep, "ratio": F.prepare({}), "five_hour": prep}
        taus = F.grid({"seven_day": prep}, None)
        found = F.search(F.Scanner(preps, 5), taus)
        self.assertEqual(sorted(f["line"] for f in found), ["seven_day", "seven_day"])
        F.refine_all(preps, found)
        hours = sorted(f["_h"] for f in found)
        self.assertLessEqual(abs(hours[0] - (20 * DAY + 3)), 24)
        self.assertLessEqual(abs(hours[1] - (41 * DAY + 9)), 24)
        signs = [f["sign"] for f in sorted(found, key=lambda f: f["_h"])]
        self.assertEqual(signs, [-1, 1])

    def test_a_step_on_one_account_is_listed_as_not_a_limit_change(self):
        prep = synth({"a1": 0.6, "a2": 0.0, "a3": 0.0, "a4": 0.0}, noise=0.05)
        preps = {"seven_day": prep, "ratio": F.prepare({}), "five_hour": prep}
        taus = F.grid({"seven_day": prep}, None)
        scanner = F.Scanner(preps, 5)
        self.assertEqual(F.search(scanner, taus), [])
        episodes = F.not_limit_changes(scanner, taus, [])
        self.assertEqual(len(episodes), 1)
        ep = episodes[0]
        self.assertEqual((ep["line"], ep["why"]), ("seven_day", "accounts disagree"))
        self.assertIn("a1", ep["accounts_apart"])
        self.assertEqual(max(ep["per_account"], key=ep["per_account"].get), "a1")
        self.assertAlmostEqual(ep["per_account"]["a1"], 60.0, delta=5.0)


# ------------------------------------------------------------------------------- the hold
def found_at(h: float, line: str = "ratio", sign: int = -1) -> dict:
    return {"line": line, "grid": h, "_h": h, "instant_range": [h, h], "sign": sign,
            "p": 0.00025, "z": -6.0 * sign}


def as_previous(changes: list[dict], withdrawn: list[dict] | None = None) -> dict:
    """Today's changes as tomorrow's run reads them (`_public_change`'s fields)."""
    return {"changes": [{"at": F.stamp(c["_h"]), "found_on": c["found_on"], "sign": c["sign"],
                         "state": c["state"], "held_days": c["held_days"],
                         "first_found": c["first_found"], "measured_on": c.get("measured_on"),
                         "instant_range": [F.stamp(x) for x in c["instant_range"]],
                         "change_pct": -10.0} for c in changes],
            "withdrawn": withdrawn or []}


class HoldTest(unittest.TestCase):
    H = F.hours(datetime(2026, 9, 14, 16, tzinfo=UTC))

    def days(self, finds: list[list[dict]], start: date = date(2026, 9, 19)):
        """Run advance_changes over successive days' finds; each day's (changes, withdrawn)."""
        prev, out = None, []
        for i, found in enumerate(finds):
            day = start + timedelta(days=i)
            held = F.carried(prev)
            changes, unmatched = F.advance_changes(found, held, prev, day)
            withdrawn = F._withdrawn(unmatched, changes, prev, day)
            out.append((changes, withdrawn))
            prev = as_previous(changes, withdrawn)
        return out

    def test_candidate_then_provisional_then_measured_on_the_seventh_run(self):
        runs = self.days([[found_at(self.H)] for _ in range(7)])
        states = [changes[0]["state"] for changes, _ in runs]
        self.assertEqual(states, ["candidate"] + ["provisional"] * 5 + ["measured"])
        last = runs[-1][0][0]
        self.assertEqual((last["held_days"], last["first_found"], last["measured_on"]),
                         (7, "2026-09-19", "2026-09-25"))

    def test_a_find_that_moves_within_match_hours_is_the_same_change(self):
        moves = [0, 6, -12, 20, 24, 18, 12]  # each within MATCH_HOURS of the day before
        runs = self.days([[found_at(self.H + m)] for m in moves])
        self.assertEqual(runs[-1][0][0]["state"], "measured")

    def test_a_find_further_than_match_hours_is_a_new_change(self):
        runs = self.days([[found_at(self.H)], [found_at(self.H + F.MATCH_HOURS + 6)]])
        changes, withdrawn = runs[1]
        self.assertEqual((changes[0]["state"], changes[0]["held_days"]), ("candidate", 1))
        self.assertEqual(len(withdrawn), 1)
        self.assertEqual(withdrawn[0]["replaced_by"], F.stamp(self.H + F.MATCH_HOURS + 6))

    def test_another_line_or_direction_is_not_the_same_change(self):
        runs = self.days([[found_at(self.H)], [found_at(self.H, line="seven_day")],
                          [found_at(self.H, line="seven_day", sign=1)]])
        self.assertEqual([c[0][0]["held_days"] for c in runs], [1, 1, 1])

    def test_a_day_not_found_starts_the_hold_again(self):
        runs = self.days([[found_at(self.H)]] * 3 + [[]] + [[found_at(self.H)]])
        self.assertEqual(runs[3][0], [])
        self.assertEqual(runs[3][1][0]["withdrawn_on"], "2026-09-22")
        self.assertEqual(runs[4][0][0]["held_days"], 1)

    def test_a_measured_change_is_carried_and_never_withdrawn(self):
        runs = self.days([[found_at(self.H)] for _ in range(7)] + [[], [], [found_at(self.H + 40)]])
        for changes, withdrawn in runs[7:]:
            measured = [c for c in changes if c["state"] == "measured"]
            self.assertEqual(len(measured), 1)
            self.assertTrue(measured[0]["carried"])
            self.assertEqual(measured[0]["_h"], self.H)
            self.assertEqual(withdrawn, [])

    def test_carried_reads_only_measured_changes(self):
        prev = as_previous([dict(found_at(self.H), found_on="ratio", state="measured", held_days=9,
                                 first_found="2026-09-19", measured_on="2026-09-25"),
                            dict(found_at(self.H + 300), found_on="ratio", state="provisional",
                                 held_days=3, first_found="2026-09-30")])
        held = F.carried(prev)
        self.assertEqual([(c["_h"], c["carried"], c["held_days"]) for c in held], [(self.H, True, 9)])

    def test_the_state_names(self):
        self.assertEqual([F._state_of(n) for n in (1, 2, 6, 7, 30)],
                         ["candidate", "provisional", "provisional", "measured", "measured"])


# ------------------------------------------------------------------------------- publishing
def block_with(changes: list[dict]) -> dict:
    return {"schema": F.SCHEMA, "method": F.METHOD, "day": "2026-10-23",
            "data_until": "2026-10-23T00:00:00+00:00", "rule": F.RULE, "valuation": F.VALUATION,
            "headless_factor": {"value": 1.42}, "interval_coverage": {},
            "accounts": {a: {"label": F.public_label(a), "lines": {}} for a in ("a1", "a2", "a3", "a4")},
            "changes": changes, "withdrawn": [], "not_limit_changes": [], "lines": {}, "levels": {}}


def size(pct: float, lo: float, hi: float, accounts=("a1", "a2")) -> dict:
    return {"change_pct": pct, "interval_pct": [lo, hi], "excludes_no_change": lo > 0 or hi < 0,
            "_d": math.log1p(pct / 100), "accounts": list(accounts),
            "per_account": {a: {"change_pct": pct} for a in accounts}}


def change(at: str, found_on: str, state: str, sizes: dict) -> dict:
    found = sizes[found_on]
    return {"at": at, "_h": F.hours(F.parse(at)), "instant_range": [at, at], "found_on": found_on,
            "sign": 1 if found["change_pct"] > 0 else -1, "state": state, "carried": False,
            "found_today": True, "held_days": 7 if state == "measured" else 2,
            "first_found": "2026-09-19", "measured_on": "2026-09-25" if state == "measured" else None,
            "p": 0.00025, "z": -6.9, "change_pct": found["change_pct"],
            "interval_pct": found["interval_pct"], "accounts": found["accounts"],
            "reading": F.reading(sizes), "sizes": sizes}


SEP14 = change("2026-09-14T16:00:00+00:00", "ratio", "measured",
               {"ratio": size(-17.1, -24.5, -9.0), "seven_day": size(-2.2, -13.9, 10.9),
                "five_hour": size(10.3, -9.2, 34.1)})
SEP23 = change("2026-09-23T13:00:00+00:00", "seven_day", "measured",
               {"ratio": size(-2.6, -12.5, 8.4), "seven_day": size(18.3, 11.5, 25.6, ("a1", "a2", "a3", "a4")),
                "five_hour": size(31.4, 14.0, 51.4)})
OCT09 = change("2026-10-09T16:00:00+00:00", "ratio", "provisional",
               {"ratio": size(-8.9, -15.2, -2.2), "seven_day": size(4.1, -2.7, 11.3),
                "five_hour": size(9.9, 1.2, 19.2)})


class PublishTest(unittest.TestCase):
    def test_last_change_is_the_newest_measured_change_never_a_provisional_one(self):
        last = F.last_change(block_with([SEP14, SEP23, OCT09]))
        self.assertEqual((last["at"], last["metric"], last["change_pct"], last["percent"],
                          last["direction"], last["state"]),
                         ("2026-09-23T13:00:00+00:00", "weekly_limit", 18.3, 18, "increased", "measured"))
        self.assertEqual(last["scope"], "both")
        self.assertEqual(last["windows_per_week_change_pct"], -2.6)
        self.assertEqual(last["per_account"], {"Max account 1": 18.3, "Max account 2": 18.3,
                                               "Max account 3": 18.3, "Max account 4": 18.3})
        self.assertIsNone(F.last_change(block_with([OCT09])))
        self.assertIsNone(F.last_change(None))

    def test_events_are_the_measured_changes_each_stated_on_the_line_that_found_it(self):
        events = F.events(block_with([SEP14, SEP23, OCT09]))
        self.assertEqual([(e["date"], e["metric"], e["percent"], e["direction"]) for e in events],
                         [("2026-09-14", "windows_per_week", 17, "decreased"),
                          ("2026-09-23", "weekly_limit", 18, "increased")])
        self.assertEqual(events[0]["label"], "Windows per week -17.1% (measured)")

    def test_a_percent_rounds_the_way_the_page_does(self):
        half = change("2026-09-23T13:00:00+00:00", "seven_day", "measured",
                      {"seven_day": size(-12.5, -20.0, -5.0)})
        self.assertEqual(F.events(block_with([half]))[0]["percent"], 12)
        self.assertEqual(F.page_round(-12.5), -12)
        self.assertEqual(F.page_round(12.5), 13)

    def test_a_line_steps_where_it_found_the_change_or_its_interval_excludes_none(self):
        self.assertTrue(F.drawn(SEP14, "ratio"))
        self.assertFalse(F.drawn(SEP14, "seven_day"))
        self.assertFalse(F.drawn(SEP14, "five_hour"))
        self.assertTrue(F.drawn(SEP23, "five_hour"))
        self.assertEqual(F.drawn_step(SEP14, "seven_day"), 0.0)

    def test_the_step_line_ends_at_one_and_steps_by_the_drawn_sizes(self):
        levels = F.levels([SEP14, SEP23], "seven_day")
        self.assertEqual([s["from"] for s in levels], [None, SEP14["at"], SEP23["at"]])
        self.assertEqual(levels[-1]["index"], 1.0)
        self.assertEqual(levels[1]["index"], round(1 / 1.183, 4))
        self.assertEqual(levels[0]["index"], levels[1]["index"])
        ratio = F.levels([SEP14, SEP23], "ratio")
        self.assertEqual([s["index"] for s in ratio], [round(1 / 0.829, 4), 1.0, 1.0])

    def test_regime_changes_carry_what_each_chart_draws(self):
        rc = F.regime_changes(block_with([SEP14, SEP23, OCT09]))
        self.assertEqual([c["at"].isoformat() for c in rc], [SEP14["at"], SEP23["at"]])
        first, second = rc
        self.assertFalse(first["window_scaled"])
        self.assertEqual(first["ratio"], 1.0)
        self.assertTrue(first["windows_per_week"]["drawn"])
        self.assertFalse(first["weekly"]["drawn"])
        self.assertTrue(second["window_scaled"])
        self.assertAlmostEqual(second["ratio"], 1.314)
        self.assertTrue(second["weekly"]["drawn"])

    def test_the_public_block_names_accounts_only_by_label(self):
        out = F.public_block(block_with([SEP14, SEP23, OCT09]))
        self.assertEqual(out["accounts"], ["Max account 1", "Max account 2", "Max account 3",
                                           "Max account 4"])
        text = json.dumps(out)
        for bare in ('"a1"', '"a2"', '"a3"', '"a4"'):
            self.assertNotIn(bare, text)
        self.assertEqual([c["state"] for c in out["changes"]], ["measured", "measured", "provisional"])
        self.assertEqual(out["sentence"], F.SENTENCE)
        self.assertIsNone(F.public_block(None))

    def test_labels_follow_the_fixed_table_then_the_next_free_number(self):
        fixed = (("masterrig", "a1"), ("jwork", "a2"))
        self.assertEqual(F.labels_for(["new", "jwork", "masterrig", "other"], fixed),
                         {"masterrig": "a1", "jwork": "a2", "new": "a3", "other": "a4"})


class StateTest(unittest.TestCase):
    def test_advance_keeps_one_row_a_day_and_the_newest_run_in_full(self):
        b1 = dict(block_with([OCT09]), day="2026-10-22")
        b2 = dict(block_with([SEP14, OCT09]), day="2026-10-23")
        state = F.advance(F.advance(None, b1), b2)
        state = F.advance(state, b2)
        self.assertEqual([r["day"] for r in state["runs"]], ["2026-10-22", "2026-10-23"])
        self.assertIs(state["latest"], b2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "four-accounts.json"
            F.write_state(path, json.loads(json.dumps(state, default=str)))
            self.assertEqual(F.load_state(path)["runs"][1]["changes"][0]["at"], SEP14["at"])
            path.write_text("not json")
            self.assertIsNone(F.load_state(path))

    def test_the_readings_snapshot_round_trips(self):
        readings = {"a1": {"steps": [F.Reading(1.0, 2.0, 100.0, 5.0, 3.0, 1.0)], "stretches": []}}
        back = F.readings_from_json(json.loads(json.dumps(F.readings_to_json(readings))))
        self.assertEqual(back, readings)
        cut = F.known_at(readings, F.from_hours(1.5))
        self.assertEqual(cut, {})


# ------------------------------------------------------------------------------- recovery
@functools.lru_cache(maxsize=1)
def fixture_readings() -> dict:
    return F.readings_from_json(json.loads(FIXTURE.read_text(encoding="utf-8")))


@functools.lru_cache(maxsize=1)
def replayed() -> tuple[dict, ...]:
    """Every daily run from the first possible day to 23 October on the frozen readings:
    the hold rebuilt from scratch with no date given (about 30 s)."""
    readings = fixture_readings()
    blocks = []
    state = None
    d = F.first_day(readings)
    while d <= date(2026, 10, 23):
        block = F.run(readings, datetime(d.year, d.month, d.day, tzinfo=UTC),
                      (state or {}).get("latest"))
        state = F.advance(state, block)
        blocks.append(block)
        d += timedelta(days=1)
    return tuple(blocks)


def overlaps(change: dict, first: str, last: str) -> bool:
    lo, hi = (F.parse(x) for x in change["instant_range"])
    return lo <= F.parse(last) and hi >= F.parse(first)


class RecoveryTest(unittest.TestCase):
    """The frozen readings of 2026-10-23, every daily run replayed, no date given."""

    @classmethod
    def setUpClass(cls):
        cls.blocks = replayed()
        cls.last = cls.blocks[-1]
        cls.measured = [c for c in cls.last["changes"] if c["state"] == "measured"]

    def test_it_finds_14_september_on_the_meter_ratio(self):
        sep14 = [c for c in self.measured if c["found_on"] == "ratio"
                 and overlaps(c, "2026-09-14T00:00:00+00:00", "2026-09-15T23:59:00+00:00")]
        self.assertEqual(len(sep14), 1, self.measured)
        c = sep14[0]
        self.assertLess(c["change_pct"], 0)
        self.assertLess(c["interval_pct"][1], 0)
        self.assertEqual(c["sizes"]["ratio"]["accounts"], ["a1", "a2"])

    def test_it_finds_22_september_on_the_seven_day_meter(self):
        sep22 = [c for c in self.measured if c["found_on"] == "seven_day"
                 and overlaps(c, "2026-09-22T00:00:00+00:00", "2026-09-23T23:59:00+00:00")]
        self.assertEqual(len(sep22), 1, self.measured)
        c = sep22[0]
        self.assertGreater(c["interval_pct"][0], 0)
        # Max account 4's readings start on 23 September at 10:53, two hours before the
        # instant: too little on the before side to vote.
        self.assertEqual(c["accounts"], ["a1", "a2", "a3"])
        self.assertTrue(c["sizes"]["five_hour"]["excludes_no_change"])

    def test_nothing_at_17_october_with_the_masterrig_local_work_in(self):
        for block in self.blocks:
            for c in block["changes"]:
                self.assertFalse(overlaps(c, "2026-10-15T00:00:00+00:00", "2026-10-19T23:59:00+00:00"),
                                 (block["day"], c))

    def test_each_measured_change_was_held_seven_runs_and_never_moved_after(self):
        for c in self.measured:
            first, measured_on = (date.fromisoformat(c[k]) for k in ("first_found", "measured_on"))
            self.assertEqual((measured_on - first).days, F.HOLD_DAYS - 1)
            later = [b for b in self.blocks if b["day"] >= c["measured_on"]]
            for b in later:
                same = [x for x in b["changes"] if x["at"] == c["at"] and x["found_on"] == c["found_on"]]
                self.assertEqual(len(same), 1, b["day"])
                self.assertEqual(same[0]["state"], "measured")

    def test_no_measured_change_was_ever_withdrawn_or_lost(self):
        for b in self.blocks:
            for w in b["withdrawn"]:
                self.assertLess(w["held_days"], F.HOLD_DAYS)
            for c in b["changes"]:
                if c["state"] == "measured":
                    self.assertIsNot(c["found_today"], False, (b["day"], c["at"]))

    def test_the_9_october_ratio_step_is_held_but_not_yet_measured(self):
        oct9 = [c for c in self.last["changes"] if overlaps(c, "2026-10-08T00:00:00+00:00",
                                                           "2026-10-10T23:59:00+00:00")]
        self.assertEqual(len(oct9), 1)
        self.assertEqual((oct9[0]["found_on"], oct9[0]["state"]), ("ratio", "provisional"))
        self.assertEqual(oct9[0]["accounts"], ["a1", "a2", "a3", "a4"])

    def test_the_lines_index_every_account_to_its_current_level(self):
        for line in F.LINES:
            self.assertEqual(sorted(self.last["lines"][line]), ["a1", "a2", "a3", "a4"])
            self.assertEqual(self.last["levels"][line][-1]["index"], 1.0)

    def test_a_daily_run_fits_the_cron(self):
        self.assertLess(max(b["seconds"] for b in self.blocks), 60)


class NullTest(unittest.TestCase):
    """With the found steps divided out, account by account, and each account's readings
    shifted in time by its own offset (so no step is shared by construction), the search
    finds nothing: 0 of 20 shifted histories on these readings when this was written."""

    def shifted(self, preps: dict, offs: dict | None) -> dict:
        if offs is None:
            return preps
        out = {}
        for line, p in preps.items():
            tm, W, D = {}, {}, {}
            for a in p.accounts:
                lo = min(q.first[a] for q in preps.values() if a in q.first)
                hi = max(q.last[a] for q in preps.values() if a in q.last)
                t = lo + np.mod(p.tm[a] - lo + offs[a] * (hi - lo), hi - lo)
                o = np.argsort(t, kind="stable")
                tm[a], W[a], D[a] = t[o], p.W[a][o], p.D[a][o]
            out[line] = F.Prepared(p.accounts, tm, W, D, p.s2, p.first, p.last)
        return out

    def test_shifted_accounts_share_no_change(self):
        readings = fixture_readings()
        found = replayed()[-1]["changes"]
        breaks = [F.hours(F.parse(c["at"])) for c in found]
        until = datetime(2026, 10, 23, tzinfo=UTC)
        hf = F.headless_factor({k: v["steps"] for k, v in readings.items()})
        bed = {line: F.destepped(F.prepare(F.series(readings, line, hf["value"])), breaks)
               for line in F.LINES}
        rng = np.random.default_rng(424242)
        for k in range(6):
            offs = None if k == 0 else {a: float(rng.uniform(0.1, 0.9))
                                        for a in sorted(bed["seven_day"].accounts)}
            preps = self.shifted(bed, offs)
            taus = F.grid({line: preps[line] for line in F.SEARCHED}, F.hours(until))
            self.assertEqual(F.search(F.Scanner(preps, F.run_seed(until.date()) + k), taus), [], offs)


if __name__ == "__main__":
    unittest.main()
