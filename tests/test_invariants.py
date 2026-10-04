"""tracker.invariants: publish-time checks over the built public JSON, and their alerting.

Each check has a passing fixture and a failing one. The fixture is the smallest public JSON
the checks read: `credits.window_tokens` (headline, per-family rows, regimes, account
regimes) and `credits.five_hour_on_meters.candidates`.
"""
from __future__ import annotations

import copy
import json
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

    def acct(windows, measured):
        return [{"from": r["from"], "until": r["until"], "window": w, "measured_window": m}
                for r, w, m in zip(regimes, windows, measured)]
    return {"credits": {
        "window_tokens": {
            "all": {"value": 500}, "measured_family": "opus-5-5",
            "per_family": {"opus": {"all": {"value": 500}, "measured_directly": False,
                                    "rate_source": "anchor"},
                           "opus-5-5": {"all": {"value": 665}, "measured_directly": True,
                                        "rate_source": "measured"}},
            "regimes": regimes,
            "account_regimes": {"a1": acct([410, 470, 470], [None, None, None]),
                                "a2": acct([490, 610, 610], [460, 800, 800]),
                                "a3": acct([None, 395, 395], [None, 620, 620])}},
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
        _wt(doc)["account_regimes"]["a3"][2]["window"] = 300  # 399 against 620
        (msg,) = I.account_units(doc)
        self.assertIn("a3", msg)
        self.assertIn("399", msg)
        self.assertIn("620", msg)
        self.assertIn(SEP22, msg)

    def test_a_missing_reading_is_skipped(self):
        doc = _good()
        _wt(doc)["account_regimes"]["a3"][2]["measured_window"] = None
        _wt(doc)["account_regimes"]["a3"][2]["window"] = 1
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
                          "account_units", "steps_agree_with_meters"])

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


if __name__ == "__main__":
    unittest.main()
