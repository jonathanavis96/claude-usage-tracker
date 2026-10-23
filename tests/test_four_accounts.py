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
import io
import json
import math
import tempfile
import time
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from tracker import four_accounts as F

FIXTURE = Path(__file__).parent / "fixtures" / "four_accounts" / "readings-2026-10-23.json"
PRE_REPAIR = (Path(__file__).parent / "fixtures" / "four_accounts"
              / "readings-2026-10-23-without-masterrig-local-work.json")
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
            screen: float | None = F.Z_SCREEN, n_max: int | None = None) -> dict | None:
    tau = step_day * DAY
    return F.step_at(p, "seven_day", tau, tau - F.SIDE_DAYS * DAY, tau + F.SIDE_DAYS * DAY,
                     seed=11, n_perm=n_perm, screen=screen, n_max=n_max)


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
                         "finds": c.get("finds"), "decided": c.get("decided"),
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
    c = {"at": at, "_h": F.hours(F.parse(at)), "instant_range": [at, at], "found_on": found_on,
         "sign": 1 if found["change_pct"] > 0 else -1, "state": state, "carried": False,
         "found_today": True, "held_days": 7 if state == "measured" else 2,
         "first_found": "2026-09-19", "measured_on": "2026-09-25" if state == "measured" else None,
         "p": 0.00025, "z": -6.9, "change_pct": found["change_pct"],
         "interval_pct": found["interval_pct"], "accounts": found["accounts"], "sizes": sizes}
    c["reading"] = F.reading(c)
    return c


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

    def test_the_hero_is_the_newest_stated_change_as_its_larger_clear_limit(self):
        # 9 October, provisional: the five-hour size's interval excludes no change and the
        # seven-day size's does not, so the hero names the 5-hour limit (Jonathan, 2026-10-23).
        h = F.headline(block_with([SEP14, SEP23, OCT09]))
        self.assertEqual((h["date"], h["state"], h["provisional"], h["quantity"], h["limit"],
                          h["direction"], h["percent"], h["change_pct"], h["interval_pct"],
                          h["held_days"], h["holds_needed"], h["passed_over"]),
                         ("2026-10-09", "provisional", True, "five_hour_limit", "5-hour limit",
                          "increased", 10, 9.9, [1.2, 19.2], 2, 7, []))
        # Measured, it is the same record with the tag gone.
        h = F.headline(block_with([SEP14, SEP23, dict(OCT09, state="measured", held_days=7)]))
        self.assertEqual((h["at"], h["state"], h["provisional"], h["quantity"]),
                         (OCT09["at"], "measured", False, "five_hour_limit"))
        # Both limits clear: the larger one. At 23 September five-hour +31.4 beats weekly +18.3.
        h = F.headline(block_with([SEP14, SEP23]))
        self.assertEqual((h["at"], h["quantity"], h["percent"]), (SEP23["at"], "five_hour_limit", 31))
        bigger_week = dict(SEP23, sizes=dict(SEP23["sizes"], five_hour=size(19.7, 3.1, 39.1),
                                             seven_day=size(20.5, 13.5, 27.9)))
        h = F.headline(block_with([bigger_week]))
        self.assertEqual((h["quantity"], h["limit"], h["percent"]), ("weekly_limit", "weekly limit", 21))

    def test_a_withdrawn_or_candidate_change_falls_back_to_the_newest_measured_one(self):
        # A withdrawn find is gone from the run's changes; a candidate is never stated.
        self.assertEqual(F.headline(block_with([SEP14, SEP23]))["at"], SEP23["at"])
        self.assertEqual(F.headline(block_with([SEP14, SEP23, dict(OCT09, state="candidate")]))["at"],
                         SEP23["at"])

    def test_a_change_with_no_clear_limit_is_passed_over(self):
        # 14 September: neither the five-hour nor the seven-day interval excludes no change.
        self.assertIsNone(F.stated_quantity(SEP14))
        self.assertIsNone(F.headline(block_with([SEP14])))
        later = dict(SEP14, at="2026-10-12T10:00:00+00:00")
        h = F.headline(block_with([SEP23, later]))
        self.assertEqual(h["at"], SEP23["at"])
        self.assertEqual(h["passed_over"], [{"at": later["at"], "state": "measured", "found_on": "ratio",
                                             "reason": "no_limit_interval_excludes_no_change"}])
        self.assertIsNone(F.headline(None))

    def test_the_limit_the_hero_names_is_drawn(self):
        # 9 October's five-hour line: the voting accounts disagree, so the line has not moved by
        # `moved`, but the hero names it, so its charts show it.
        split = dict(OCT09, sizes=dict(OCT09["sizes"], five_hour=dict(size(15.3, 3.2, 28.8), agree=False)))
        self.assertFalse(F.line_moved(split, "five_hour"))
        self.assertEqual(F.stated_quantity(split), "five_hour_limit")
        self.assertTrue(F.drawn(split, "five_hour"))
        self.assertFalse(F.drawn(split, "seven_day"))

    def test_a_provisional_change_moves_no_level_and_is_a_dashed_marker(self):
        ratio = F.levels([SEP14, SEP23, OCT09], "ratio")
        self.assertEqual([s["from"] for s in ratio], [None, SEP14["at"], SEP23["at"]])
        self.assertEqual(F.chart_step(OCT09, "ratio"), 0.0)
        self.assertNotEqual(F.drawn_step(OCT09, "ratio"), 0.0)
        marks = F.change_markers(block_with([SEP14, SEP23, OCT09, dict(OCT09, at="2026-10-20T00:00:00+00:00",
                                                                        state="candidate")]))
        self.assertEqual({q: [(m["date"], m["style"], m["change_pct"], m["state"]) for m in rows]
                          for q, rows in marks.items()},
                         {"windows_per_week": [("2026-09-14", "step", -17.1, "measured"),
                                               ("2026-10-09", "dashed", -8.9, "provisional")],
                          "weekly_limit": [("2026-09-23", "step", 18.3, "measured")],
                          "five_hour_limit": [("2026-09-23", "step", 31.4, "measured"),
                                              ("2026-10-09", "dashed", 9.9, "provisional")]})
        measured = F.levels([SEP14, SEP23, dict(OCT09, state="measured")], "ratio")
        self.assertEqual([s["from"] for s in measured], [None, SEP14["at"], SEP23["at"], OCT09["at"]])
        self.assertEqual(round(measured[-2]["index"], 3), round(1 / 0.911, 3))

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

    def test_a_state_with_no_newest_run_is_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "four-accounts.json"
            path.write_text(json.dumps({"schema": F.SCHEMA, "runs": []}))
            self.assertIsNone(F.load_state(path))
            self.assertIsNone(F.load_state(Path(tmp)))  # a directory: unreadable, not a crash

    def test_the_readings_snapshot_round_trips(self):
        readings = {"a1": {"steps": [F.Reading(1.0, 2.0, 100.0, 5.0, 3.0, 1.0)], "stretches": []}}
        back = F.readings_from_json(json.loads(json.dumps(F.readings_to_json(readings))))
        self.assertEqual(back, readings)
        cut = F.known_at(readings, F.from_hours(1.5))
        self.assertEqual(cut, {})


class FirstRunTest(unittest.TestCase):
    """The daily command with no usable state rebuilds the hold (--replay), and says so.

    On 2026-10-23 the first live publish after PR #142 went out with no four_accounts block:
    nothing had written history/four-accounts.json, so the page could state no limit change.
    """

    PRICES = Path(__file__).resolve().parent.parent / "data" / "prices.json"

    def daily(self, tmp: Path, state: Path) -> tuple[int, str, mock.MagicMock]:
        rebuilt = F.advance(None, dict(block_with([]), day="2026-10-23", replayed=True))
        argv = ["--state", str(state), "--now", "2026-10-23T13:30:00+00:00",
                "--prices", str(self.PRICES), "--gs-passive", str(tmp / "none.json"),
                "--masterrig-passive", str(tmp / "none.json"),
                "--model-rates", str(tmp / "none.json")]
        err = io.StringIO()
        with mock.patch.object(F, "first_day", return_value=date(2026, 9, 1)), \
                mock.patch.object(F, "replay", return_value=rebuilt) as replay, \
                mock.patch.object(F, "run") as run, redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = F.main(argv)
        run.assert_not_called()
        return code, err.getvalue(), replay

    def test_a_missing_state_is_rebuilt_from_scratch_and_the_log_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            state = tmp / "four-accounts.json"
            code, err, replay = self.daily(tmp, state)
            self.assertEqual(code, 0)
            replay.assert_called_once()
            self.assertEqual(replay.call_args.args[1:3], (date(2026, 9, 1), date(2026, 10, 23)))
            self.assertIn(f"state {state} missing, rebuilding", err)
            self.assertIn("--replay", err)
            self.assertEqual(F.load_state(state)["latest"]["day"], "2026-10-23")

    def test_an_unreadable_state_is_rebuilt_and_replaced(self):
        for body in ("not json", json.dumps({"schema": "another"}), json.dumps({"schema": F.SCHEMA})):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                state = tmp / "four-accounts.json"
                state.write_text(body)
                code, err, replay = self.daily(tmp, state)
                self.assertEqual(code, 0)
                replay.assert_called_once()
                self.assertIn(f"state {state} unreadable or another schema, rebuilding", err)
                self.assertEqual(F.load_state(state)["latest"]["day"], "2026-10-23")


# ------------------------------------------------------------------------------- recovery
@functools.lru_cache(maxsize=1)
def fixture_readings() -> dict:
    return F.readings_from_json(json.loads(FIXTURE.read_text(encoding="utf-8")))


#: Each replayed daily run's wall time, seconds.
SECONDS: list[float] = []


@functools.lru_cache(maxsize=1)
def replayed() -> tuple[dict, ...]:
    """Every daily run from the first possible day to 23 October on the frozen readings:
    the hold rebuilt from scratch with no date given (about 30 s)."""
    readings = fixture_readings()
    blocks = []
    state = None
    d = F.first_day(readings)
    valued = json.loads(FIXTURE.read_text(encoding="utf-8"))["valuation"]
    while d <= date(2026, 10, 23):
        t = time.time()
        block = F.run(readings, datetime(d.year, d.month, d.day, tzinfo=UTC),
                      (state or {}).get("latest"), valued, replayed=True)
        SECONDS.append(time.time() - t)
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
        self.assertLess(c["interval_pct"][1], 0)
        self.assertEqual(c["sizes"]["ratio"]["accounts"], ["a1", "a2"])
        # R1: the ratio's step inside its measured interval on these readings (spec 8), every
        # voting account agreeing and each leave-one-out step holding; the seven-day line's
        # interval holds the -9.9% the frozen figure once stated.
        ratio = c["sizes"]["ratio"]
        self.assertTrue(-21.6 <= ratio["change_pct"] <= -13.0, ratio["change_pct"])
        self.assertTrue(ratio["agree"] and ratio["plan_wide"])
        for x in ratio["per_account"].values():
            self.assertGreaterEqual(abs(x["without_this_account"]["z"]), 1.96)
        lo, hi = c["sizes"]["seven_day"]["interval_pct"]
        self.assertTrue(lo <= -9.9 <= hi, (lo, hi))
        # R6 on the ratio: it reads no rate but a step's headless share, so no family's rate
        # interval moves it by more than a point.
        lo, hi = ratio["rate_dependence"]["change_pct_range"]
        self.assertLess(hi - lo, 1.0)
        self.assertFalse(ratio["rate_dependence"]["rate_limited"])

    def test_it_finds_22_september_on_the_seven_day_meter(self):
        sep22 = [c for c in self.measured if c["found_on"] == "seven_day"
                 and overlaps(c, "2026-09-22T00:00:00+00:00", "2026-09-23T23:59:00+00:00")]
        self.assertEqual(len(sep22), 1, self.measured)
        c = sep22[0]
        # Max account 4's readings start on 23 September at 10:53, two hours before the
        # instant: too little on the before side to vote.
        self.assertEqual(c["accounts"], ["a1", "a2", "a3"])
        # R2: the seven-day step inside [+11.5, +25.7]%, the five-hour interval holding the
        # announced +20%, the ratio not significant.
        seven, five, ratio = (c["sizes"][x] for x in ("seven_day", "five_hour", "ratio"))
        self.assertTrue(11.5 <= seven["change_pct"] <= 25.7, seven["change_pct"])
        self.assertGreater(seven["interval_pct"][0], 0)
        self.assertTrue(five["interval_pct"][0] <= 20 <= five["interval_pct"][1])
        self.assertFalse(ratio["excludes_no_change"])
        # The after side runs on Opus 5.5, which the before side never used: across that
        # family's rate interval the step runs from about nothing to about +35%, and its
        # record says so beside the figure at the rates in force.
        rd = seven["rate_dependence"]
        self.assertTrue(rd["rate_limited"])
        self.assertEqual(rd["families"][0]["family"], "opus-5-5")
        self.assertLess(rd["interval_pct"][0], 0)
        self.assertFalse(rd["excludes_no_change"])
        record = F.change_record(c, {})
        self.assertTrue(record["rate_limited"])
        self.assertEqual(record["interval_with_rates_pct"], rd["interval_pct"])
        self.assertTrue(c["decided"]["moved"]["seven_day"])
        self.assertFalse(c["decided"]["moved"]["ratio"])
        # The instant: the median of the hold's seven finds, the range spanning all of them.
        self.assertEqual(c["at"], sorted(f[0] for f in c["finds"])[(len(c["finds"]) - 1) // 2])
        self.assertLessEqual(F.parse(c["instant_range"][0]), min(F.parse(f[1]) for f in c["finds"]))
        self.assertGreaterEqual(F.parse(c["instant_range"][1]), max(F.parse(f[2]) for f in c["finds"]))
        # Its capture passed on both sides for every voting account.
        self.assertEqual(c["capture_audit"]["failing"], [])

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

    def test_what_a_measured_change_says_moved_is_never_re_decided(self):
        for c in self.measured:
            later = [x for b in self.blocks if b["day"] >= c["measured_on"]
                     for x in b["changes"] if x["at"] == c["at"] and x["found_on"] == c["found_on"]]
            self.assertTrue(later)
            self.assertEqual({json.dumps(x["decided"], sort_keys=True) for x in later},
                             {json.dumps(c["decided"], sort_keys=True)})
            self.assertEqual(c["decided"]["on"], c["measured_on"])

    def test_every_run_and_hold_is_marked_replayed(self):
        self.assertTrue(all(b["replayed"] for b in self.blocks))
        self.assertTrue(all(c["hold_replayed"] for c in self.last["changes"]))

    def test_the_same_readings_give_the_same_changes_on_later_days(self):
        # The review's seed flip: on the readings of 23 October, the runs of 24 and 25
        # October find the 9 October ratio step again, one more day held each time, at the
        # same p (the shuffles are seeded by line, instant and account, never by the day).
        readings = F.known_at(fixture_readings(), datetime(2026, 10, 23, tzinfo=UTC))
        valued = json.loads(FIXTURE.read_text(encoding="utf-8"))["valuation"]
        prev, held = self.last, []
        for day in (24, 25):
            block = F.run(readings, datetime(2026, 10, day, tzinfo=UTC), prev, valued)
            oct9 = [c for c in block["changes"] if c["found_on"] == "ratio"
                    and overlaps(c, "2026-10-08T00:00:00+00:00", "2026-10-10T23:59:00+00:00")]
            self.assertEqual(len(oct9), 1, (day, F.summary(block)))
            held.append((oct9[0]["held_days"], oct9[0]["p"]))
            self.assertFalse(block["replayed"])
            prev = block
        last9 = [c for c in self.last["changes"] if c["found_on"] == "ratio"
                 and overlaps(c, "2026-10-08T00:00:00+00:00", "2026-10-10T23:59:00+00:00")][0]
        self.assertEqual([h for h, _ in held], [last9["held_days"] + 1, last9["held_days"] + 2])
        self.assertEqual(held[0][1], held[1][1])

    def test_a_daily_run_fits_the_cron(self):
        self.assertLess(max(SECONDS), 60)


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
            self.assertEqual(F.search(F.Scanner(preps, F.SEED + k), taus), [], offs)


# ------------------------------------------------------------------------------- more rule tests
class PermutationTest(unittest.TestCase):
    """The shuffles: seeded by line, instant and account alone; more of them near ALPHA."""

    def test_the_seed_is_never_the_day(self):
        self.assertFalse(hasattr(F, "run_seed"))
        a = F._rng(F.SEED, "ratio", 100.0, "a1").random(5)
        self.assertTrue(np.array_equal(a, F._rng(F.SEED, "ratio", 100.0, "a1").random(5)))
        self.assertFalse(np.array_equal(a, F._rng(F.SEED, "ratio", 100.0, "a2").random(5)))
        self.assertFalse(np.array_equal(a, F._rng(F.SEED, "seven_day", 100.0, "a1").random(5)))

    def test_one_account_more_or_less_never_changes_another_accounts_shuffles(self):
        four = synth({"a1": 0.15, "a2": 0.15, "a3": 0.15, "a4": 0.15}, noise=0.25)
        three = F.Prepared(["a1", "a2", "a3"], four.tm, four.W, four.D, four.s2, four.first, four.last)
        tau = 20 * DAY
        for p in (four, three):
            r = F.step_at(p, "seven_day", tau, tau - 14 * DAY, tau + 14 * DAY, screen=None, n_max=4000)
            self.assertIsNotNone(r["p"])
        # Same account set, same draws: a second call is identical.
        r1 = F.step_at(three, "seven_day", tau, tau - 14 * DAY, tau + 14 * DAY, screen=None, n_max=4000)
        r2 = F.step_at(three, "seven_day", tau, tau - 14 * DAY, tau + 14 * DAY, screen=None, n_max=4000)
        self.assertEqual(r1["p"], r2["p"])

    def test_ambiguous_is_within_mc_z_monte_carlo_errors_of_alpha(self):
        self.assertFalse(F._ambiguous(0, 4000))       # no shuffle reached it
        self.assertTrue(F._ambiguous(3, 4000))        # p = 4/4001, right on ALPHA
        self.assertTrue(F._ambiguous(8, 4000))        # p = 0.00225, inside 3 errors
        self.assertFalse(F._ambiguous(12, 4000))      # p = 0.00325, outside
        self.assertFalse(F._ambiguous(3, 100_000))    # p = 0.00004 with 100,000

    def test_a_step_near_alpha_gets_more_shuffles(self):
        p = synth({"a1": 0.07, "a2": 0.07, "a3": 0.07, "a4": 0.07}, noise=0.35, seed=5)  # p ~ 0.006
        first = at_step(p, screen=None, n_max=F.PERMUTATIONS)
        self.assertEqual(first["permutations"], F.PERMUTATIONS)
        k = round(first["p"] * (F.PERMUTATIONS + 1)) - 1
        self.assertGreater(k, 0, "the test needs a step some shuffles reach")
        with mock.patch.object(F, "ALPHA", first["p"]):
            more = at_step(p, screen=None)
        self.assertGreater(more["permutations"], F.PERMUTATIONS)
        self.assertLessEqual(more["permutations"], F.PERMUTATIONS_MAX)

    def test_a_published_p_keeps_four_significant_figures(self):
        self.assertEqual(F.p_value(4 / 4001), 0.0009998)
        self.assertLess(F.p_value(4 / 4001), F.ALPHA)
        self.assertIsNone(F.p_value(None))


class CalibrationTest(unittest.TestCase):
    """Spec 8, tests 4 and 13: on null panels the permutation p is calibrated, and the
    24-hour unit is what keeps readings that share a day's shock from passing as evidence."""

    @staticmethod
    def panel(seed: int, shared: bool) -> F.Prepared:
        rng = np.random.default_rng(seed)
        by = {}
        for i, a in enumerate(("a1", "a2", "a3", "a4")):
            tm = np.sort(rng.uniform(0, 28 * DAY, 28 * 4))
            D = rng.integers(1, 4, tm.size).astype(float)
            day_shock = rng.normal(0, 0.2, 28)[np.floor(tm / DAY).astype(int)] if shared else 0.0
            own = rng.normal(0, 0.2 if not shared else 0.05, tm.size)
            W = 1000 * (1 + 0.2 * i) * D * np.exp(day_shock + own)
            by[a] = (tm - 0.5, tm + 0.5, W, D)
        return F.prepare(by)

    def share(self, shared: bool, n: int = 300) -> tuple[float, float]:
        ps = []
        for k in range(n):
            r = at_step(self.panel(1000 + k, shared), step_day=14, screen=None, n_max=F.PERMUTATIONS)
            ps.append(r["p"])
        ps = np.array(ps)
        return float(np.mean(ps < 0.01)), float(np.mean(ps < 0.001))

    def test_on_null_panels_the_p_is_calibrated(self):
        p01, p001 = self.share(shared=False)
        self.assertTrue(0.003 <= p01 <= 0.02, p01)
        self.assertLessEqual(p001, 0.005)

    def test_day_units_hold_where_hour_units_would_not(self):
        _, day = self.share(shared=True)
        self.assertLessEqual(day, 0.005)
        with mock.patch.object(F, "UNIT_HOURS", 1.0):
            _, hour = self.share(shared=True)
        self.assertGreater(hour, 0.005)


class CoverageTest(unittest.TestCase):
    @staticmethod
    def drifting(drift: float, seed: int = 4) -> F.Prepared:
        """Four flat accounts over 80 days whose levels wander day by day (a random walk of
        `drift` a day): what independent day units understate."""
        rng = np.random.default_rng(seed)
        by = {}
        for i, a in enumerate(("a1", "a2", "a3", "a4")):
            walk = np.cumsum(rng.normal(0, drift, 80))
            tm = np.sort(rng.uniform(0, 80 * DAY, 80 * 6))
            D = rng.integers(1, 5, tm.size).astype(float)
            W = 1000 * (1 + 0.2 * i) * D * np.exp(walk[np.floor(tm / DAY).astype(int)]
                                                  + rng.normal(0, 0.1, tm.size) / np.sqrt(D))
            by[a] = (tm - 0.5, tm + 0.5, W, D)
        return F.prepare(by)

    def test_the_factor_is_the_spread_of_z_where_days_are_not_independent(self):
        # Independent days: z spreads as a standard normal and the factor stays near one.
        # Levels that wander from day to day: the day units understate the step's error, z
        # spreads wider, and the factor widens the interval by that spread.
        for drift, lo, hi in ((0.0, 1.0, 1.4), (0.04, 1.5, 10.0)):
            p = self.drifting(drift)
            taus = F.grid({"x": p}, None)
            cover = F.coverage({line: p for line in F.LINES}, [], taus)["seven_day"]
            self.assertTrue(lo <= cover["factor"] <= hi, (drift, cover))

    def test_an_interval_is_its_factor_times_wider(self):
        p = synth({"a1": 0.2, "a2": 0.2, "a3": 0.2, "a4": 0.2})
        one = F.size_on(p, "seven_day", 20 * DAY, [], 1.0)
        two = F.size_on(p, "seven_day", 20 * DAY, [], 2.0)
        w1 = math.log1p(one["interval_pct"][1] / 100) - math.log1p(one["interval_pct"][0] / 100)
        w2 = math.log1p(two["interval_pct"][1] / 100) - math.log1p(two["interval_pct"][0] / 100)
        self.assertAlmostEqual(w2 / w1, 2.0, delta=0.1)  # percents to one decimal

    def test_on_the_frozen_readings_the_ratio_and_five_hour_intervals_are_widened(self):
        cover = replayed()[-1]["interval_coverage"]
        self.assertGreater(cover["ratio"]["factor"], 1.5)
        self.assertGreater(cover["five_hour"]["factor"], 1.5)


class HoldInstantTest(unittest.TestCase):
    H = F.hours(datetime(2026, 9, 23, 13, tzinfo=UTC))

    def days(self, finds):
        return HoldTest.days(self, finds)

    def test_the_measured_instant_is_the_median_of_the_finds_and_the_range_spans_them(self):
        moves = [-21, 120, 118, 0, 0, 0, 0]   # 22 Sep 16:00, 28 Sep twice, then 23 Sep
        runs = self.days([[found_at(self.H + m, line="seven_day", sign=1)] for m in moves])
        last = runs[-1][0][0]
        self.assertEqual(last["state"], "measured")
        self.assertEqual(last["_h"], self.H)
        self.assertEqual(last["instant_range"], [self.H - 21, self.H + 120])
        self.assertEqual(len(last["finds"]), 7)

    def test_a_find_is_matched_against_the_hold_not_yesterday(self):
        # 0, +150, +300: each within MATCH_HOURS of the day before, but +300 is not within
        # it of the hold's median (0): a walking find restarts the hold.
        runs = self.days([[found_at(self.H + m)] for m in (0, 150, 300)])
        self.assertEqual([c[0][-1]["held_days"] for c in runs], [1, 2, 1])

    def test_a_second_change_beside_a_measured_one_is_its_own(self):
        carried = {"found_on": "seven_day", "sign": 1, "_h": self.H,
                   "instant_range": [self.H - 10, self.H + 30]}
        r = {"d": 0.2}
        self.assertTrue(F._carried_again([carried], "seven_day", self.H + 30 + F.MIN_GAP_HOURS, r))
        self.assertFalse(F._carried_again([carried], "seven_day", self.H + 5 * DAY, r))
        self.assertFalse(F._carried_again([carried], "seven_day", self.H, {"d": -0.2}))


class DecidedTest(unittest.TestCase):
    def test_a_decided_line_draws_as_decided_whatever_todays_interval_says(self):
        c = change("2026-09-23T13:00:00+00:00", "seven_day", "measured",
                   {"seven_day": size(18.3, 11.5, 25.6), "five_hour": size(17.8, -0.4, 39.2),
                    "ratio": size(-2.6, -7.9, 3.0)})
        self.assertFalse(F.drawn(c, "five_hour"))
        c["decided"] = {"on": "2026-10-11", "moved": {"seven_day": True, "five_hour": True, "ratio": False}}
        self.assertTrue(F.drawn(c, "five_hour"))
        self.assertEqual(F.change_record(c, {})["scope"], "both")
        self.assertEqual(F.reading(c), "both_limits")


# ------------------------------------------------------------------------------- whole runs
def synth_readings(names: list[str], *, days: int = 44, step_day: int = 22, step: float = 0.3,
                   gap_after: dict | None = None, seed: int = 9, new_family: str | None = None) -> dict:
    """Readings as `readings_for` gives them, for synthetic accounts: a five-hour stretch every
    two hours (2 points) and a seven-day step every six (1 point), work per point lognormal
    around each account's level, times 1 + `step` from `step_day` on both credit lines.
    `gap_after` gives an account the share of its after-side stretches with nothing captured.
    With `new_family`, every reading after the step is that family's work."""
    rng = np.random.default_rng(seed)
    out = {}
    for i, a in enumerate(names):
        level = 1000.0 * (1 + 0.25 * i)
        rs = {"stretches": [], "steps": []}
        for kind, every, d5, d7 in (("stretches", 2.0, 2.0, 0.0), ("steps", 6.0, 3.0, 1.0)):
            t = rng.uniform(0, every)
            while t < days * DAY:
                after = t >= step_day * DAY
                pts = d5 if kind == "stretches" else d7
                w = level * pts * (1 + step if after else 1.0) * math.exp(rng.normal(0, 0.1))
                if kind == "stretches" and after and rng.random() < (gap_after or {}).get(a, 0.0):
                    w = 0.0
                fam = {new_family: w} if new_family and after and w else None
                rs[kind].append(F.Reading(t, t + 1.0, w, 0.0, d5, d7, fam, None))
                t += every
        out[a] = rs
    return out


def synth_valued(new_family: str | None = None) -> dict:
    v = dict(F.VALUATION)
    fams = {new_family: {"times_opus": 1.0, "interval": [0.7, 1.3], "provisional": False}} if new_family else {}
    v["rate_intervals"] = {"percentiles": [10, 90], "families": fams}
    return v


def held_six_days(at: str, line: str = "seven_day", sign: int = 1) -> dict:
    """A previous run holding one unconfirmed change six days."""
    return {"changes": [{"at": at, "found_on": line, "sign": sign, "state": "provisional",
                         "held_days": 6, "first_found": "2026-02-10", "measured_on": None,
                         "instant_range": [at, at], "finds": [[at, at, at]] * 6}],
            "withdrawn": []}


class WholeRunTest(unittest.TestCase):
    """`run` on synthetic accounts: no date given, any number of accounts."""

    UNTIL = F.from_hours(44 * DAY)
    AT = F.stamp(22 * DAY)

    def test_a_fifth_account_votes_with_no_code_change(self):
        fixed = (("masterrig", "a1"), ("jwork", "a2"), ("dave", "a3"), ("avis", "a4"))
        labels = F.labels_for(["masterrig", "jwork", "dave", "avis", "newcomer"], fixed)
        self.assertEqual(labels["newcomer"], "a5")
        readings = synth_readings(sorted(labels.values()))
        block = F.run(readings, self.UNTIL, None, synth_valued())
        found = [c for c in block["changes"] if c["found_on"] == "seven_day"]
        self.assertEqual(len(found), 1, F.summary(block))
        self.assertIn("a5", found[0]["accounts"])
        self.assertIn("Max account 5", F.public_block(block)["accounts"])

    def test_the_same_inputs_give_byte_identical_json_and_no_date_is_read(self):
        readings = synth_readings(["a1", "a2", "a3", "a4"])
        one = json.dumps(F.run(readings, self.UNTIL, None, synth_valued()), sort_keys=True)
        two = json.dumps(F.run(readings, self.UNTIL, None, synth_valued()), sort_keys=True)
        self.assertEqual(one, two)
        from tracker import credits as credit_model
        with mock.patch.object(credit_model, "ANNOUNCEMENTS", []), \
                mock.patch.object(credit_model, "CUT_AT", datetime(2030, 1, 1, tzinfo=UTC)):
            three = json.dumps(F.run(readings, self.UNTIL, None, synth_valued()), sort_keys=True)
        self.assertEqual(one, three)
        source = Path(F.__file__).read_text(encoding="utf-8")
        for name in ("ANNOUNCEMENTS", "CUT_AT", "PLAN_CHANGE", "ACCOUNTS ="):
            self.assertNotIn(name, source)

    def test_a_change_whose_capture_fails_stays_provisional_and_names_the_account(self):
        readings = synth_readings(["a1", "a2", "a3", "a4"], gap_after={"a2": 0.3})
        block = F.run(readings, self.UNTIL, held_six_days(self.AT), synth_valued())
        c = [c for c in block["changes"] if c["found_on"] == "seven_day"][0]
        self.assertEqual(c["held_days"], 7)
        self.assertEqual(c["state"], "provisional")
        self.assertEqual(c["held_by_capture"], ["a2"])
        self.assertLess(c["capture_audit"]["per_account"]["a2"]["after"], F.CAPTURE_MIN)
        clean = F.run(synth_readings(["a1", "a2", "a3", "a4"]), self.UNTIL, held_six_days(self.AT),
                      synth_valued())
        c = [c for c in clean["changes"] if c["found_on"] == "seven_day"][0]
        self.assertEqual((c["state"], c["held_by_capture"]), ("measured", None))
        self.assertEqual(c["capture_audit"]["failing"], [])
        self.assertEqual(c["decided"]["on"], clean["day"])

    def test_a_step_that_comes_with_a_new_model_is_limited_by_its_rate(self):
        readings = synth_readings(["a1", "a2", "a3", "a4"], new_family="newmodel")
        block = F.run(readings, self.UNTIL, held_six_days(self.AT), synth_valued("newmodel"))
        c = [c for c in block["changes"] if c["found_on"] == "seven_day"][0]
        seven = c["sizes"]["seven_day"]
        rd = seven["rate_dependence"]
        self.assertEqual(rd["families"][0]["family"], "newmodel")
        # Every after-side reading is the new model's: its rate at 0.7 and 1.3 of the rate in
        # force moves the +30% step to about 1.3 x 0.7 - 1 = -9% and 1.3 x 1.3 - 1 = +69%.
        self.assertAlmostEqual(rd["change_pct_range"][0], -9.0, delta=3)
        self.assertAlmostEqual(rd["change_pct_range"][1], 69.0, delta=4)
        self.assertTrue(rd["rate_limited"])
        self.assertLess(rd["interval_pct"][0], 0)
        self.assertFalse(rd["excludes_no_change"])
        # The figure, its interval and what the charts draw stay at the rate in force.
        self.assertGreater(seven["interval_pct"][0], 0)
        self.assertTrue(seven["excludes_no_change"])
        self.assertTrue(c["decided"]["moved"]["seven_day"])
        record = F.change_record(c, {})
        self.assertEqual(record["rate_dependence"]["families"][0]["family"], "newmodel")
        self.assertTrue(record["rate_limited"])
        self.assertTrue(record["interval_excludes_no_change"])
        self.assertEqual(record["interval_with_rates_pct"], rd["interval_pct"])


class CaptureDependenceTest(unittest.TestCase):
    """Spec 8 R3: the protection against the 17 October artifact is the capture, and this
    test has teeth. On the records as they were before the capture repair (#137, the
    masterrig-local work missing; tests/fixtures/four_accounts/
    readings-2026-10-23-without-masterrig-local-work.json), a shared seven-day step of -30% or
    more around 17 October is significant by permutation; only the accounts' disagreement or
    the leave-one-out test keeps it from being a change. With the work in, nothing there is
    even significant (`RecoveryTest.test_nothing_at_17_october_with_the_masterrig_local_work_in`)."""

    def test_without_the_masterrig_local_work_17_october_is_a_significant_shared_step(self):
        obj = json.loads(PRE_REPAIR.read_text(encoding="utf-8"))
        block = F.run(F.readings_from_json(obj), datetime(2026, 10, 23, tzinfo=UTC), None,
                      obj["valuation"])
        steps = [e for e in block["not_limit_changes"] + [
                     {"line": c["found_on"], "at": c["at"], "change_pct": c["change_pct"]}
                     for c in block["changes"]]
                 if e["line"] == "seven_day" and "2026-10-16" <= e["at"][:10] <= "2026-10-18"]
        self.assertTrue(steps, F.summary(block))
        self.assertLessEqual(min(e["change_pct"] for e in steps), -30.0)
        for e in block["not_limit_changes"]:
            if e in steps:
                self.assertLess(e["p"], F.ALPHA)
                self.assertGreaterEqual(len(e["per_account"]), 2)

    def test_with_the_work_in_nothing_is_significant_there(self):
        last = replayed()[-1]
        for e in last["not_limit_changes"]:
            self.assertFalse(e["line"] == "seven_day" and "2026-10-15" <= e["at"][:10] <= "2026-10-19", e)


if __name__ == "__main__":
    unittest.main()
