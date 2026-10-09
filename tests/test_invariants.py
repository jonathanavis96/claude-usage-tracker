"""tracker.invariants: publish-time checks over the built public JSON, and their alerting.

Each check has a passing fixture and a failing one. The fixture is the smallest public JSON
the checks read: `credits.window_tokens` (headline, per-family rows, regimes, account
regimes) and `credits.five_hour_on_meters.candidates`.
"""
from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tracker import invariants as I
from tracker import supervise

CUT = "2026-09-14T12:00:00+00:00"
SEP22 = "2026-09-22T19:41:49.479000+00:00"


def _candidate(at=SEP22, *, applies=False, change_pct=None, interval_pct=None,
               separable=False, rate_check="passed", plan_wide="passed",
               ratio=0.97, ratio_interval=(0.68, 1.43)):
    return {"family": "opus-5-5", "at": at, "applies": applies, "measurable": True,
            "change_pct": change_pct, "interval_pct": interval_pct,
            "windows_per_week_ratio": ratio, "windows_per_week_ratio_interval": list(ratio_interval),
            "joint_fit": {"separable": separable, "rate_check": {"state": rate_check}},
            "plan_wide": {"state": plan_wide},
            "withheld_reason": None if applies else "interval test (ADR 0001 rule 9) failed"}


def _good() -> dict:
    """Today's shape: a run read directly in Opus 5.5 from the cut, one withheld boundary."""
    regimes = [
        {"from": None, "until": CUT, "value": 450, "source": "before_cluster",
         "measured_family": "opus", "measured_value": 450},
        {"from": CUT, "until": SEP22, "value": 500, "source": "unmeasured_boundary_run_cluster",
         "measured_family": "opus-5-5", "measured_value": 665},
        {"from": SEP22, "until": None, "value": 500, "source": "unmeasured_boundary_run_cluster",
         "measured_family": "opus-5-5", "measured_value": 665},
    ]

    # The withheld boundary opens no per-week or account regime: those rows run from the cut.
    spans = [(None, CUT), (CUT, None)]

    def acct(windows, measured):
        return [{"from": f, "until": u, "window": w, "measured_window": m}
                for (f, u), w, m in zip(spans, windows, measured)]
    return {"credits": {
        "window_tokens": {
            "all": {"value": 500}, "measured_family": "opus-5-5",
            "per_family": {"opus": {"all": {"value": 500}, "measured_directly": False,
                                    "rate_source": "anchor"},
                           "opus-5-5": {"all": {"value": 665}, "measured_directly": True,
                                        "rate_source": "measured"}},
            "regimes": regimes,
            "per_week_regimes": [{"from": f, "until": u} for f, u in spans],
            "account_regimes": {"a1": acct([410, 470], [None, None]),
                                "a2": acct([490, 610], [460, 800]),
                                "a3": acct([None, 395], [None, 620])}},
        "five_hour_on_meters": {"candidates": [_candidate()]}}}


def _wt(doc):
    return doc["credits"]["window_tokens"]


def _headline(doc, anchor_value):
    """Move the headline the way the page does: the anchor figure and the family row together."""
    wt = _wt(doc)
    scale = wt["per_family"]["opus-5-5"]["all"]["value"] / wt["all"]["value"]
    wt["all"]["value"] = wt["per_family"]["opus"]["all"]["value"] = anchor_value
    wt["per_family"]["opus-5-5"]["all"]["value"] = round(anchor_value * scale)
    return doc


class HeadlineInsideItsAccountsTest(unittest.TestCase):
    def test_passes_inside_the_scaled_account_range(self):
        # 665 / 500 scales the accounts to about 525 .. 811; the headline 665 is inside.
        self.assertEqual(I.headline_inside_accounts(_good()), [])

    def test_fails_above_every_account(self):
        # The pre-fix shape: a headline chained through two factors, above every account.
        doc = _headline(_good(), 990)
        (msg,) = I.headline_inside_accounts(doc)
        self.assertIn("Opus 5.5", msg)
        self.assertIn("1,317", msg)
        self.assertIn("a2", msg)  # names the account at the edge it left

    def test_allows_five_percent_for_rounding(self):
        # The page scales accounts and headline alike, so the anchor figure is what moves.
        self.assertEqual(I.headline_inside_accounts(_headline(_good(), round(610 * 1.04))), [])
        self.assertEqual(len(I.headline_inside_accounts(_headline(_good(), round(610 * 1.06)))), 1)
        self.assertEqual(I.headline_inside_accounts(_headline(_good(), round(395 * 0.96))), [])
        self.assertEqual(len(I.headline_inside_accounts(_headline(_good(), round(395 * 0.94)))), 1)

    def test_needs_two_accounts_with_a_window(self):
        doc = _good()
        for label in ("a2", "a3"):
            _wt(doc)["account_regimes"][label][-1]["window"] = None
        _wt(doc)["per_family"]["opus-5-5"]["all"]["value"] = 5000
        self.assertEqual(I.headline_inside_accounts(doc), [])

    def test_without_measured_family_the_anchor_is_the_headline(self):
        doc = _good()
        del _wt(doc)["measured_family"]
        self.assertEqual(I.headline_family(doc), "opus")
        self.assertEqual(I.headline_inside_accounts(doc), [])
        _wt(doc)["all"]["value"] = _wt(doc)["per_family"]["opus"]["all"]["value"] = 900
        self.assertEqual(len(I.headline_inside_accounts(doc)), 1)


class NoUnprovenStepTest(unittest.TestCase):
    def _scaled(self, **cand):
        doc = _good()
        doc["credits"]["five_hour_on_meters"]["candidates"] = [_candidate(**cand)]
        _wt(doc)["regimes"][2]["source"] = "previous_regime_scaled_by_known_date_change"
        return doc

    def test_carried_and_measured_regimes_pass(self):
        self.assertEqual(I.no_unproven_step(_good()), [])

    def test_a_step_from_a_published_change_passes(self):
        doc = self._scaled(applies=True, change_pct=20.0, interval_pct=[8.0, 33.0], separable=True)
        self.assertEqual(I.no_unproven_step(doc), [])

    def test_a_step_from_a_withheld_change_fails(self):
        doc = self._scaled(applies=True, change_pct=48.3, interval_pct=[3.3, 89.2], separable=True,
                           plan_wide="failed")
        (msg,) = I.no_unproven_step(doc)
        self.assertIn(SEP22, msg)
        self.assertIn("plan-wide", msg)

    def test_a_step_failing_the_rate_check_fails(self):
        doc = self._scaled(applies=True, change_pct=33.3, interval_pct=[17.3, 53.5], separable=True,
                           rate_check="failed")
        self.assertIn("rate check", I.no_unproven_step(doc)[0])

    def test_a_step_with_no_record_of_rules_8_and_9_fails(self):
        # The pre-fix JSON carried neither record: a step it cannot show passed them fails.
        doc = self._scaled(applies=True, change_pct=48.3, interval_pct=[3.3, 89.2], separable=True)
        cand = doc["credits"]["five_hour_on_meters"]["candidates"][0]
        del cand["plan_wide"], cand["joint_fit"]["rate_check"]
        (msg,) = I.no_unproven_step(doc)
        self.assertIn("no record", msg)

    def test_a_step_with_no_change_behind_it_fails(self):
        doc = self._scaled()
        doc["credits"]["five_hour_on_meters"]["candidates"] = []
        self.assertIn("no five-hour change candidate", I.no_unproven_step(doc)[0])

    def test_the_published_rate_interval_is_read_again(self):
        # Records from older code said separable; today's span test and rule 8 refuse it.
        doc = self._scaled(applies=True, change_pct=33.3, interval_pct=[17.3, 53.5], separable=True)
        cand = doc["credits"]["five_hour_on_meters"]["candidates"][0]
        cand["family"] = "sonnet-5-5"
        cand["joint_fit"].update(base_family="sonnet", rate_relative_interval=[2.4298, 6.5116])
        (msg,) = I.no_unproven_step(doc)
        self.assertIn("spans 2.68x", msg)
        self.assertIn("read against data/prices.json", msg)
        cand["joint_fit"]["rate_relative_interval"] = [0.9, 1.1]
        self.assertEqual(I.no_unproven_step(doc), [])

    def test_the_cut_regime_is_the_certified_weekly_change(self):
        doc = _good()
        _wt(doc)["regimes"][1]["source"] = "before_cluster_scaled_by_five_hour_change"
        self.assertEqual(I.no_unproven_step(doc), [])


class DirectWindowChangeTest(unittest.TestCase):
    """A record carrying its direct window change (ADR 0001 rule 11) is proved by it."""

    def _window(self, certified=True, plan_wide="passed", interval=(8.0, 33.0)):
        return {"certified": certified, "plan_wide": {"state": plan_wide},
                "interval_pct": list(interval) if interval else None}

    def _scaled(self, window, applies=True):
        doc = _good()
        cand = _candidate(applies=applies, change_pct=20.0, interval_pct=[8.0, 33.0])
        cand["window_change"] = window
        doc["credits"]["five_hour_on_meters"]["candidates"] = [cand]
        _wt(doc)["regimes"][2]["source"] = "previous_regime_scaled_by_known_date_change"
        return doc

    def test_a_certified_window_change_proves_its_step_without_a_separable_fit(self):
        self.assertEqual(I.no_unproven_step(self._scaled(self._window())), [])

    def test_an_uncertified_window_change_does_not(self):
        (msg,) = I.no_unproven_step(self._scaled(
            self._window(certified=False, plan_wide="failed", interval=(-2.0, 30.0))))
        self.assertIn("plan-wide", msg)
        self.assertIn("includes no change", msg)
        self.assertIn("not certified", msg)

    def test_a_withheld_candidate_does_not_even_with_a_certified_window(self):
        (msg,) = I.no_unproven_step(self._scaled(self._window(), applies=False))
        self.assertIn("withheld", msg)

    def test_a_change_certified_on_the_weekly_limit_alone_is_no_five_hour_step(self):
        # 22 September today: it applies on the seven-day meter, its window change is not
        # certified, and the ratio points the other way. No five-hour step is published.
        doc = _good()
        cand = _candidate(applies=True, change_pct=31.7, interval_pct=[20.6, 43.8],
                          ratio=1.0688, ratio_interval=(0.75, 1.6))
        cand["window_change"] = self._window(certified=False, plan_wide="failed")
        doc["credits"]["five_hour_on_meters"]["candidates"] = [cand]
        self.assertEqual(I.steps_agree_with_meters(doc), [])
        cand["window_change"]["certified"] = True
        (msg,) = I.steps_agree_with_meters(doc)
        self.assertIn("+31.7%", msg)


class NoWithheldBoundaryTest(unittest.TestCase):
    SEP29 = "2026-09-29T18:25:18.212000+00:00"

    def _live(self):
        """Shaped like the public JSON of 2026-10-04: per-week and account rows split at the
        two withheld changes, 22 and 29 September."""
        doc = _good()
        wt = _wt(doc)
        doc["credits"]["five_hour_on_meters"]["candidates"] = [_candidate(), _candidate(at=self.SEP29)]
        bounds = [(None, CUT), (CUT, SEP22), (SEP22, self.SEP29), (self.SEP29, None)]
        wt["per_week_regimes"] = [{"from": f, "until": u} for f, u in bounds]
        wt["account_regimes"] = {label: [{"from": f, "until": u, "window": 470} for f, u in bounds]
                                 for label in ("a1", "a2")}
        return doc

    def test_merged_rows_pass(self):
        self.assertEqual(I.no_withheld_boundary(_good()), [])
        doc = self._live()
        wt = _wt(doc)
        wt["per_week_regimes"] = [{"from": None, "until": CUT}, {"from": CUT, "until": None}]
        wt["account_regimes"] = {label: rows[:2] for label, rows in wt["account_regimes"].items()}
        self.assertEqual(I.no_withheld_boundary(doc), [])

    def test_rows_split_at_a_withheld_change_fail(self):
        msgs = I.no_withheld_boundary(self._live())
        self.assertEqual(len(msgs), 2)
        first = msgs[0]
        self.assertIn("22 September", first)
        self.assertIn("per_week_regimes", first)
        self.assertIn("a1", first)
        self.assertIn("a2", first)
        self.assertIn("interval test (ADR 0001 rule 9) failed", first)
        self.assertIn("29 September", msgs[1])

    def test_a_published_change_may_open_a_row(self):
        doc = self._live()
        doc["credits"]["five_hour_on_meters"]["candidates"] = [
            _candidate(applies=True, change_pct=-3.0, interval_pct=[-5.0, -1.0], separable=True),
            _candidate(at=self.SEP29, applies=True, change_pct=7.0, interval_pct=[2.0, 12.0],
                       separable=True)]
        self.assertEqual(I.no_withheld_boundary(doc), [])

    def test_a_long_withheld_reason_is_shortened(self):
        doc = self._live()
        for cand in doc["credits"]["five_hour_on_meters"]["candidates"]:
            cand["withheld_reason"] = "plan-wide test failed: " + "x" * 400
        for msg in I.no_withheld_boundary(doc):
            self.assertLess(len(msg), 400)
            self.assertIn("…", msg)


class DirectMeansDirectTest(unittest.TestCase):
    def test_passes_when_equal(self):
        self.assertEqual(I.direct_means_direct(_good()), [])

    def test_fails_when_the_direct_row_is_a_conversion(self):
        doc = _good()
        _wt(doc)["per_family"]["opus-5-5"]["all"]["value"] = 664
        (msg,) = I.direct_means_direct(doc)
        self.assertIn("664", msg)
        self.assertIn("665", msg)

    def test_not_direct_is_not_checked(self):
        doc = _good()
        _wt(doc)["per_family"]["opus-5-5"].update(measured_directly=False)
        _wt(doc)["per_family"]["opus-5-5"]["all"]["value"] = 664
        self.assertEqual(I.direct_means_direct(doc), [])


class AccountUnitsTest(unittest.TestCase):
    def test_passes_within_a_quarter(self):
        # a3: 395 x 665/500 = 525 against 620 direct, 15% low: inside the bound.
        self.assertEqual(I.account_units(_good()), [])

    def test_fails_outside_a_quarter_and_names_the_account_and_both_numbers(self):
        doc = _good()
        _wt(doc)["account_regimes"]["a3"][1]["window"] = 300  # 399 against 620
        (msg,) = I.account_units(doc)
        self.assertIn("a3", msg)
        self.assertIn("399", msg)
        self.assertIn("620", msg)
        self.assertIn(CUT, msg)

    def test_a_missing_reading_is_skipped(self):
        doc = _good()
        _wt(doc)["account_regimes"]["a3"][1]["measured_window"] = None
        _wt(doc)["account_regimes"]["a3"][1]["window"] = 1
        self.assertEqual(I.account_units(doc), [])


class StepsAgreeWithMetersTest(unittest.TestCase):
    def _doc(self, **cand):
        doc = _good()
        doc["credits"]["five_hour_on_meters"]["candidates"] = [_candidate(**cand)]
        return doc

    def test_a_step_the_ratio_points_the_same_way_passes(self):
        # +48% five-hour, ratio 0.97: fewer windows per week, a larger window. Agrees.
        doc = self._doc(applies=True, change_pct=48.3, interval_pct=[3.3, 89.2], separable=True)
        self.assertEqual(I.steps_agree_with_meters(doc), [])

    def test_a_step_against_an_inconclusive_ratio_fails(self):
        # The pre-fix 29 Sep step: +33% five-hour while windows per week rose 6% [-43, +108].
        doc = self._doc(at="2026-09-29T18:25:18.212000+00:00", applies=True, change_pct=33.3,
                        interval_pct=[17.3, 53.5], separable=True, ratio=1.0607,
                        ratio_interval=(0.5674, 2.0792))
        (msg,) = I.steps_agree_with_meters(doc)
        self.assertIn("+33.3%", msg)
        self.assertIn("1.0607", msg)

    def test_a_ratio_that_excludes_no_change_is_not_flagged(self):
        # Then the weekly cap moved too, measured: g and the ratio may point either way.
        doc = self._doc(applies=True, change_pct=10.0, interval_pct=[2.0, 18.0], separable=True,
                        ratio=1.3, ratio_interval=(1.1, 1.5))
        self.assertEqual(I.steps_agree_with_meters(doc), [])

    def test_a_withheld_candidate_is_not_a_published_step(self):
        doc = self._doc(applies=False, change_pct=33.3, interval_pct=[17.3, 53.5], ratio=1.0607,
                        ratio_interval=(0.5674, 2.0792))
        self.assertEqual(I.steps_agree_with_meters(doc), [])


class RunAllTest(unittest.TestCase):
    def test_every_check_runs_and_is_named(self):
        doc = _good()
        self.assertEqual(I.run_checks(doc), {name: [] for name, _ in I.CHECKS})
        self.assertEqual([name for name, _ in I.CHECKS],
                         ["headline_inside_accounts", "no_unproven_step", "direct_means_direct",
                          "account_units", "steps_agree_with_meters", "no_withheld_boundary",
                          "headline_matches_chart", "windows_per_week_implied",
                          "one_figure_per_change", "weekly_routes_agree",
                          "detected_windows_per_week_agree", "fourteen_sep_weekly_frozen"])

    def test_a_check_that_crashes_is_a_failure_not_a_crash(self):
        doc = _good()
        del _wt(doc)["regimes"]
        out = I.run_checks(doc)
        self.assertTrue(any("could not run" in m for msgs in out.values() for m in msgs))


class AlertTest(unittest.TestCase):
    """The same incident semantics as tracker.publish_gate: one alert, one recovery."""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.state, self.doc = d / "ops" / "inv.json", d / "claude-usage.json"
        self.sent: list[str] = []
        for name in ("whatsapp_sender", "email_sender"):
            p = mock.patch.object(supervise, name, side_effect=AssertionError("real send"))
            p.start()
            self.addCleanup(p.stop)

    def send(self, text):
        self.sent.append(text)
        return True

    def check(self, doc, t):
        self.doc.write_text(json.dumps(doc))
        return I.check_file(self.doc, self.state, self.send, now=lambda: t)

    def test_one_alert_when_failing_starts_and_one_on_recovery_never_blocking(self):
        bad = _headline(copy.deepcopy(_good()), 990)
        self.assertEqual(self.check(_good(), 0), 0)
        self.assertEqual(self.check(bad, 1000), 1)
        self.check(bad, 2800)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("headline_inside_accounts", self.sent[0])
        self.assertIn("Published anyway", self.sent[0])
        self.assertEqual(self.check(_good(), 4600), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("pass again after 60 min", self.sent[1])

    def test_an_unreadable_file_alerts(self):
        self.doc.write_text("{not json")
        self.assertEqual(I.check_file(self.doc, self.state, self.send, now=lambda: 0), 1)
        self.assertIn("cannot read", self.sent[0])


def _live_2026_10_05() -> dict:
    """The page as published on 2026-10-05 15:01Z, cut to what checks 7-9 read: a +30%
    weekly headline beside +32% steps on two charts, and a windows-per-week step at a
    withheld window change."""
    regimes = [{"from": None, "until": CUT, "value": 450593800},
               {"from": CUT, "until": SEP22, "value": 501060306},
               {"from": SEP22, "until": SEP29, "value": 501060306},
               {"from": SEP29, "until": None, "value": 501060306}]
    weeks = [{"from": None, "until": CUT, "value": 2575055593, "windows_per_week": 5.7148},
             {"from": CUT, "until": SEP22, "value": 2278966037, "windows_per_week": 4.5483},
             {"from": SEP22, "until": None, "value": 3007888270, "windows_per_week": 6.003}]
    accounts = {"a1": [{"from": None, "window": 411689170, "windows_per_week": 5.5444},
                       {"from": CUT, "window": 437652234, "windows_per_week": 4.8914},
                       {"from": SEP22, "window": 437652234, "windows_per_week": 7.2003}]}
    sep22 = dict(_candidate(applies=True, change_pct=31.8, interval_pct=[20.7, 43.8]),
                 certified_on=["weekly_limit"],
                 weekly_change={"certified": True, "change_pct": 29.6},
                 window_change={"certified": False, "change_pct": 31.8,
                                "interval_pct": [20.7, 43.8], "plan_wide": {"state": "failed"}})
    sep29 = dict(_candidate(at=SEP29), weekly_change={"certified": False},
                 window_change={"certified": False})
    change = {"kind": "change", "date": "2026-09-22", "scope": "weekly", "metric": "weekly_limit",
              "percent": 30, "change_pct": 29.6, "direction": "increased"}
    return {"last_change": dict(change), "events": [
        {"kind": "change", "date": "2026-09-14", "scope": "weekly", "percent": 25,
         "direction": "decreased", "metric": "weekly_to_five_hour_ratio"}, dict(change)],
        "credits": {"window_tokens": {"regimes": regimes, "per_week_regimes": weeks,
                                      "account_regimes": accounts},
                    "five_hour_on_meters": {"candidates": [sep22, sep29]}}}


def _frozen(doc: dict) -> dict:
    """A `_fixed` page with the 14 September week step bridged to the direct combined change
    (ADR 0001 rule 20) at the size rule 21 froze, the windows-per-week chart and its event
    following it (checks 7-9 and 12)."""
    weeks = _wt(doc)["per_week_regimes"]
    factor = weeks[1]["value"] / (1 + I.FROZEN_14SEP_WEEKLY_PCT / 100) / weeks[0]["value"]
    weeks[0]["value"] = round(weeks[0]["value"] * factor)
    weeks[0]["windows_per_week"] = round(weeks[0]["windows_per_week"] * factor, 4)
    doc["events"][0]["percent"] = 19
    return doc


def _fixed(doc: dict) -> dict:
    """The same page with this branch's data path: the 22 September week step is the
    certified +29.6%, and windows per week is carried across the withheld window."""
    weeks = _wt(doc)["per_week_regimes"]
    factor = weeks[2]["value"] / 1.296 / weeks[1]["value"]
    for row in weeks[:2]:
        row["value"] = round(row["value"] * factor)
        row["windows_per_week"] = round(row["windows_per_week"] * factor, 4)
    weeks[2]["windows_per_week"] = weeks[1]["windows_per_week"]
    a1 = _wt(doc)["account_regimes"]["a1"]
    a1[2]["windows_per_week"] = a1[1]["windows_per_week"]
    # The 14 September event states the windows-per-week chart's own step at its marker
    # (ADR 0001 rule 17), not one account's detector step.
    doc["events"][0].update(percent=20, metric="windows_per_week", at=CUT)
    return doc


SEP29 = "2026-09-29T18:25:18.212000+00:00"


class ChartStepsTest(unittest.TestCase):
    def test_reads_the_steps_the_page_draws(self):
        steps = I.chart_steps(_live_2026_10_05())
        self.assertEqual([(s["at"], round(s["pct"], 1)) for s in steps["window"]], [(CUT, 11.2)])
        self.assertEqual([(s["at"], round(s["pct"], 1)) for s in steps["tokens_per_week"]],
                         [(CUT, -11.5), (SEP22, 32.0)])
        self.assertEqual([(s["at"], round(s["pct"], 1)) for s in steps["windows_per_week"]],
                         [(CUT, -20.4), (SEP22, 32.0)])

    def test_percents_round_as_the_page_rounds_them(self):
        # JavaScript Math.round, not round-half-to-even: -11.5% prints -11%, +32.5% +33%.
        self.assertEqual([I._page_round(x) for x in (-11.5, 32.5, 29.6, -20.4)], [-11, 33, 30, -20])

    def test_nothing_to_read_without_per_week_regimes(self):
        doc = _live_2026_10_05()
        _wt(doc)["per_week_regimes"] = _wt(doc)["per_week_regimes"][:1]
        self.assertEqual(I.chart_steps(doc), {})
        self.assertEqual(I.headline_matches_chart(doc), [])

    def test_a_level_without_a_figure_is_not_drawn(self):
        doc = _live_2026_10_05()
        _wt(doc)["per_week_regimes"][1]["windows_per_week"] = None
        steps = I.chart_steps(doc)["windows_per_week"]
        self.assertEqual([(s["at"], round(s["pct"], 1)) for s in steps], [(SEP22, 5.0)])


class HeadlineMatchesChartTest(unittest.TestCase):
    def test_the_live_page_fails(self):
        out = I.headline_matches_chart(_live_2026_10_05())
        self.assertEqual(len(out), 1)
        self.assertIn("+30%", out[0])
        self.assertIn("tokens-per-week chart labels the same change +32%", out[0])

    def test_the_fixed_page_passes(self):
        self.assertEqual(I.headline_matches_chart(_fixed(_live_2026_10_05())), [])

    def test_no_step_where_the_headline_says_one_fails(self):
        doc = _fixed(_live_2026_10_05())
        doc["last_change"]["metric"] = "five_hour_limit"
        self.assertIn("window-size chart draws no step", I.headline_matches_chart(doc)[0])

    def test_change_pct_must_round_to_the_percent(self):
        doc = _fixed(_live_2026_10_05())
        doc["last_change"]["change_pct"] = 31.8
        self.assertIn("change_pct is 31.8", I.headline_matches_chart(doc)[0])


class WindowsPerWeekImpliedTest(unittest.TestCase):
    def test_the_live_page_fails_on_the_plan_and_the_account_line(self):
        out = I.windows_per_week_implied(_live_2026_10_05())
        self.assertEqual(len(out), 2)
        self.assertIn("steps +32.0%", out[0])
        self.assertIn("window change is withheld", out[0])
        self.assertIn("a1's windows-per-week line steps +47.2%", out[1])

    def test_the_fixed_page_passes(self):
        self.assertEqual(I.windows_per_week_implied(_fixed(_live_2026_10_05())), [])

    def test_a_step_the_weekly_and_window_steps_do_not_imply_fails(self):
        doc = _fixed(_live_2026_10_05())
        _wt(doc)["per_week_regimes"][0]["windows_per_week"] *= 1.05
        out = I.windows_per_week_implied(doc)
        self.assertEqual(len(out), 1)
        self.assertIn("implies -20.4%", out[0])

    def test_a_certified_window_change_may_step_by_division(self):
        # A +20% window certified at 22 September: windows per week steps 1.296 / 1.2.
        doc = _fixed(_live_2026_10_05())
        _wt(doc)["account_regimes"] = {}
        doc["credits"]["five_hour_on_meters"]["candidates"][0]["window_change"]["certified"] = True
        for reg in _wt(doc)["regimes"][2:]:
            reg["value"] = round(reg["value"] * 1.2)
        weeks = _wt(doc)["per_week_regimes"]
        weeks[2]["windows_per_week"] = round(weeks[1]["windows_per_week"] * 1.296 / 1.2, 4)
        self.assertEqual(I.windows_per_week_implied(doc), [])


class OneFigurePerChangeTest(unittest.TestCase):
    def test_the_live_page_fails(self):
        out = I.one_figure_per_change(_live_2026_10_05())
        self.assertIn("The tokens-per-week chart says +32% on 2026-09-22 and an event "
                      "says +30% on 2026-09-22, both for the tokens-per-week chart's "
                      "quantity.", out)
        # Its 14 September event (-25%) is read on the windows-per-week chart (-20%) too.
        self.assertIn("The windows-per-week chart says -20% on 2026-09-14 and an event says "
                      "-25% on 2026-09-14, both for the windows-per-week chart's quantity.", out)

    def test_the_fixed_page_passes(self):
        self.assertEqual(I.one_figure_per_change(_fixed(_live_2026_10_05())), [])

    def test_the_meter_ratio_is_read_on_the_windows_per_week_chart(self):
        # Five-hour over seven-day movement is windows per week: an event stating it is read
        # against the windows-per-week chart's step at its marker (ADR 0001 rule 17).
        doc = _fixed(_live_2026_10_05())
        doc["events"].append({"kind": "change", "date": "2026-09-14", "percent": 19,
                              "direction": "decreased", "metric": "weekly_to_five_hour_ratio"})
        out = I.one_figure_per_change(doc)
        self.assertEqual(len(out), 1)
        self.assertIn("-20% on 2026-09-14", out[0])
        self.assertIn("-19% on 2026-09-14", out[0])

    def test_two_published_changes_disagreeing_fail(self):
        doc = _fixed(_live_2026_10_05())
        doc["events"][1]["percent"] = 29
        self.assertTrue(I.one_figure_per_change(doc))


def _live_2026_10_06() -> dict:
    """The page as built on 2026-10-06 after #128, cut to what check 9 reads: the 14
    September event stated Max account 1's own detector step (-25%, dated 13 September)
    beside a windows-per-week chart stepping -10% at the 14 September 12:00Z boundary."""
    regimes = [{"from": None, "until": CUT, "value": 673637730},
               {"from": CUT, "until": SEP22, "value": 668922266},
               {"from": SEP22, "until": None, "value": 799362108}]
    weeks = [{"from": None, "until": CUT, "value": 3440589040, "windows_per_week": 5.1075},
             {"from": CUT, "until": SEP22, "value": 3074969024, "windows_per_week": 4.5969},
             {"from": SEP22, "until": None, "value": 3600788727, "windows_per_week": 4.5046}]
    sep22 = {"kind": "change", "date": "2026-09-22", "at": SEP22, "scope": "both",
             "metric": "five_hour_limit", "percent": 20, "change_pct": 19.5,
             "direction": "increased"}
    return {"last_change": {k: v for k, v in sep22.items() if k != "kind"}, "events": [
        {"kind": "change", "date": "2026-09-13", "scope": "weekly", "percent": 25,
         "direction": "decreased", "metric": "weekly_to_five_hour_ratio"}, sep22],
        "credits": {"window_tokens": {"regimes": regimes, "per_week_regimes": weeks},
                    "five_hour_on_meters": {"candidates": []}}}


A1_STEP = "2026-09-13T16:30:00.008149+00:00"


def _relabelled(doc: dict, at: str = A1_STEP) -> dict:
    """The 2026-10-06 page with this branch's rule 17: the boundary drawn at the event's own
    instant on every chart, and the event stating the windows-per-week step there."""
    wt = _wt(doc)
    for rows in (wt["regimes"], wt["per_week_regimes"]):
        rows[0]["until"], rows[1]["from"] = at, at
    doc["events"][0].update(date=at[:10], at=at, percent=10, metric="windows_per_week")
    return doc


class EventAtItsMarkerTest(unittest.TestCase):
    """Check 9 on 2026-10-06: an event's figure is its chart's step at its marker, and its
    date is the marker's."""

    def test_the_live_page_fails_on_the_percent_and_the_date(self):
        out = I.one_figure_per_change(_live_2026_10_06())
        self.assertIn("The windows-per-week chart says -10% on 2026-09-14 and an event says "
                      "-25% on 2026-09-13, both for the windows-per-week chart's quantity.", out)
        self.assertTrue(any("dated 2026-09-13" in o and "marker" in o for o in out), out)
        self.assertIn("one_figure_per_change", I.BLOCKING)

    def test_the_relabelled_page_passes(self):
        self.assertEqual(I.one_figure_per_change(_relabelled(_live_2026_10_06())), [])

    def test_one_point_off_fails(self):
        doc = _relabelled(_live_2026_10_06())
        doc["events"][0]["percent"] = 11
        self.assertEqual(len(I.one_figure_per_change(doc)), 1)

    def test_the_right_percent_on_another_instant_fails(self):
        doc = _relabelled(_live_2026_10_06())
        doc["events"][0]["at"] = CUT
        doc["events"][0]["date"] = CUT[:10]
        out = I.one_figure_per_change(doc)
        self.assertEqual(len(out), 1)
        self.assertIn(f"its marker on the windows-per-week chart is at {A1_STEP}", out[0])

    def test_an_event_with_no_step_at_its_marker_fails(self):
        doc = _relabelled(_live_2026_10_06())
        _wt(doc)["per_week_regimes"][1]["windows_per_week"] = 5.1075
        out = I.one_figure_per_change(doc)
        self.assertTrue(any("draws no step" in o for o in out), out)


def _weekly_event(direct: dict, ratio: dict, *, quality="certified") -> dict:
    """A weekly event carrying both routes: the direct weekly test and the ratio route.
    Each argument maps a label (or "pooled") to (change_pct, interval_pct)."""
    def pa(rows, key, iv):
        return {k: {key: v[0], iv: list(v[1]), "combined": True} for k, v in rows.items() if k != "pooled"}
    return {"kind": "change", "scope": "weekly", "date": "2026-09-14", "evidence_quality": quality,
            "five_hour_window_credits": {"direct_tests": {"weekly_change": {
                "change_pct": direct["pooled"][0], "interval_pct": list(direct["pooled"][1]),
                "per_account": pa(direct, "change_pct", "interval_pct")}}},
            "tokens_per_week_change": {
                "signed_pct": ratio["pooled"][0], "signed_interval_pct": list(ratio["pooled"][1]),
                "per_account": pa(ratio, "signed_pct", "signed_interval_pct")}}


class WeeklyRoutesAgreeTest(unittest.TestCase):
    """Check 10: on a certified weekly event the direct weekly change and the ratio route
    (windows per week times the window) agree within their combined interval."""

    # The live page of 2026-10-06 12:01Z, 14 September.
    LIVE = _weekly_event({"a1": (-9.0, (-29.3, 17.0)), "a2": (-10.4, (-26.3, 8.8)),
                          "pooled": (-9.9, (-22.3, 4.4))},
                         {"a1": (-28.0, (-37.6, -16.8)), "a2": (-28.2, (-40.3, -13.1)),
                          "pooled": (-28.0, (-38.6, -15.4))})

    def test_the_live_page_fails_on_the_pooled_figure(self):
        out = I.weekly_routes_agree({"events": [self.LIVE]})
        self.assertEqual(len(out), 1)
        self.assertIn("combined", out[0])
        self.assertIn("-9.9%", out[0])
        self.assertIn("-28.0%", out[0])

    def test_routes_inside_each_others_reach_pass(self):
        ok = _weekly_event({"a1": (-9.0, (-29.3, 17.0)), "pooled": (-9.9, (-22.3, 4.4))},
                           {"a1": (-14.7, (-22.0, -6.6)), "pooled": (-14.0, (-21.0, -6.0))})
        self.assertEqual(I.weekly_routes_agree({"events": [ok]}), [])

    def test_one_account_apart_fails_by_name(self):
        bad = _weekly_event({"a2": (-10.4, (-14.0, -6.0)), "pooled": (-10.4, (-14.0, -6.0))},
                            {"a2": (-30.0, (-33.0, -27.0)), "pooled": (-10.4, (-14.0, -6.0))})
        out = I.weekly_routes_agree({"events": [bad]})
        self.assertEqual(len(out), 1)
        self.assertIn("a2", out[0])

    def test_a_provisional_event_or_a_missing_route_is_not_read(self):
        prov = dict(self.LIVE, evidence_quality="provisional")
        bare = {k: v for k, v in self.LIVE.items() if k != "tokens_per_week_change"}
        self.assertEqual(I.weekly_routes_agree({"events": [prov, bare]}), [])

    def test_it_is_a_registered_check_that_does_not_block(self):
        self.assertIn("weekly_routes_agree", dict(I.CHECKS))
        self.assertNotIn("weekly_routes_agree", I.BLOCKING)

    @staticmethod
    def _charted(event: dict, before: int, after: int) -> dict:
        """`event` at 13 Sep 16:30Z beside a tokens-per-week chart stepping there."""
        at = "2026-09-13T16:30:00+00:00"
        return {"events": [dict(event, at=at)],
                "credits": {"window_tokens": {"per_week_regimes": [
                    {"from": None, "until": at, "value": before},
                    {"from": at, "until": None, "value": after}]}}}

    def test_the_charts_week_step_is_the_same_accounts_direct_change(self):
        # ADR 0001 rule 20. The page of 2026-10-08 12:02Z drew 1,313,529 to 1,172,293 credits
        # per 1% (-10.75%), Max account 3 on the after side only, beside a direct -9.9%.
        same = _weekly_event({"a1": (-9.1, (-29.3, 16.9)), "a2": (-10.4, (-25.9, 8.4)),
                              "pooled": (-9.9, (-22.1, 4.1))},
                             {"a1": (-9.1, (-29.3, 16.9)), "a2": (-10.4, (-25.9, 8.4)),
                              "pooled": (-9.9, (-22.1, 4.1))})
        out = I.weekly_routes_agree(self._charted(same, 1313529, 1172293))
        self.assertEqual(len(out), 1)
        self.assertIn("-10.75%", out[0])
        self.assertIn("rule 20", out[0])
        self.assertEqual(I.weekly_routes_agree(self._charted(same, 3369340315, 3035775624)), [])


class DetectedWindowsPerWeekAgreeTest(unittest.TestCase):
    """Check 11: the detector's windows-per-week levels, in interactive-equivalent units,
    agree with the week over the window (ADR 0001 rules 11 and 16) within their intervals."""

    def doc(self, detected, derived):
        return {"weekly_windows": {"max20": {"regimes": [
                    {"start": s, "end": e, "windows": w, "rounding_interval": list(iv)}
                    for s, e, w, iv in detected]}},
                "credits": {"window_tokens": {"per_week_regimes": [
                    {"from": f, "until": u, "windows_per_week": w,
                     "windows_per_week_interval": list(iv) if iv else None}
                    for f, u, w, iv in derived]}}}

    START, END = "2026-08-15T00:00:00+00:00", "2026-10-06T00:00:00+00:00"
    BEFORE_END = "2026-09-14T09:00:00+00:00"

    def test_levels_inside_each_others_reach_pass(self):
        doc = self.doc([(self.START, self.BEFORE_END, 5.6, (5.2, 6.0)), (CUT, self.END, 4.9, (4.5, 5.3))],
                       [(None, CUT, 5.5, (4.6, 6.6)), (CUT, None, 4.8, (4.0, 5.8))])
        self.assertEqual(I.detected_windows_per_week_agree(doc), [])

    def test_a_detected_level_outside_the_derived_one_fails_by_date(self):
        doc = self.doc([(self.START, self.BEFORE_END, 5.6, (5.2, 6.0)), (CUT, self.END, 4.0, (3.8, 4.2))],
                       [(None, CUT, 5.5, (4.6, 6.6)), (CUT, None, 5.5, (5.0, 6.0))])
        out = I.detected_windows_per_week_agree(doc)
        self.assertEqual(len(out), 1)
        self.assertIn("2026-09-14", out[0])
        self.assertIn("is 4 [3.8, 4.2]", out[0])

    def test_each_derived_row_is_read_against_the_level_it_overlaps_most(self):
        # The detector draws one level across 22 September; the derived rows step there by
        # less than either interval, so both read the one level and pass.
        doc = self.doc([(self.START, self.BEFORE_END, 5.6, (5.2, 6.0)), (CUT, self.END, 4.9, (4.5, 5.3))],
                       [(None, CUT, 5.5, (4.6, 6.6)), (CUT, SEP22, 4.6, (4.0, 5.3)),
                        (SEP22, None, 5.1, (4.4, 5.9))])
        self.assertEqual(I.detected_windows_per_week_agree(doc), [])

    def test_a_row_without_an_interval_or_an_unbounded_level_is_not_read(self):
        doc = self.doc([(self.START, self.END, 9.0, (5.0, None))], [(None, None, 5.0, None)])
        self.assertEqual(I.detected_windows_per_week_agree(doc), [])
        self.assertEqual(I.detected_windows_per_week_agree({}), [])

    def test_it_is_a_registered_check_that_does_not_block(self):
        self.assertIn("detected_windows_per_week_agree", dict(I.CHECKS))
        self.assertNotIn("detected_windows_per_week_agree", I.BLOCKING)


class FourteenSepFrozenTest(unittest.TestCase):
    """Check 12: the 14 September weekly step stays at the size ADR 0001 rule 21 froze."""

    AT = "2026-09-13T16:30:00+00:00"

    def _page(self, direct: float, route: float, before: int = 1000, after: int = 901) -> dict:
        event = _weekly_event({"pooled": (direct, (-22.1, 4.3))}, {"pooled": (route, (-22.1, 4.3))})
        return {"events": [dict(event, at=self.AT)],
                "credits": {"window_tokens": {"per_week_regimes": [
                    {"from": None, "until": self.AT, "value": before},
                    {"from": self.AT, "until": None, "value": after}]}}}

    def test_the_frozen_figure_passes(self):
        # The live page of 2026-10-09 15:02Z: -9.9% on both routes and the chart.
        self.assertEqual(I.fourteen_sep_weekly_frozen(self._page(-9.9, -9.9)), [])

    def test_a_moved_figure_fails_where_it_moved(self):
        out = I.fourteen_sep_weekly_frozen(self._page(-10.0, -9.9))
        self.assertEqual(len(out), 1)
        self.assertIn("direct weekly change", out[0])
        self.assertIn("rule 21", out[0])
        out = I.fourteen_sep_weekly_frozen(self._page(-9.9, -9.9, 1000, 880))
        self.assertEqual(len(out), 1)
        self.assertIn("chart", out[0])

    def test_a_later_weekly_event_is_not_read(self):
        page = self._page(17.4, 17.4)
        page["events"][0]["at"] = "2026-09-22T19:41:49+00:00"
        self.assertEqual(I.fourteen_sep_weekly_frozen(page), [])

    def test_it_is_a_registered_check_that_does_not_block(self):
        self.assertIn("fourteen_sep_weekly_frozen", dict(I.CHECKS))
        self.assertNotIn("fourteen_sep_weekly_frozen", I.BLOCKING)

    def test_the_adr_states_the_frozen_figure(self):
        # Moving the constant without changing ADR 0001 rule 21 (or the reverse) fails here.
        adr = (Path(__file__).resolve().parents[1] / "docs" / "adr"
               / "0001-acceptance-rules-for-published-figures.md").read_text(encoding="utf-8")
        m = re.search(r"^21\. \*\*The 14 September weekly step is frozen at (-?\d+\.\d)%", adr, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(float(m.group(1)), I.FROZEN_14SEP_WEEKLY_PCT)


class BlockingTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.state, self.doc = d / "ops" / "inv.json", d / "claude-usage.json"
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)
        return True

    def test_a_contradiction_on_the_page_exits_2_and_says_not_published(self):
        self.doc.write_text(json.dumps(_live_2026_10_05()))
        self.assertEqual(I.check_file(self.doc, self.state, self.send, now=lambda: 0), 2)
        self.assertIn("Not published", self.sent[0])

    def test_the_fixed_page_exits_0(self):
        self.doc.write_text(json.dumps(_frozen(_fixed(_live_2026_10_05()))))
        self.assertEqual(I.check_file(self.doc, self.state, self.send, now=lambda: 0), 0)


if __name__ == "__main__":
    unittest.main()
