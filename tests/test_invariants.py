"""tracker.invariants: publish-time checks over the built public JSON, and their alerting.

Each check has a passing fixture and a failing one. The fixture is the smallest public JSON
the checks read: `credits.window_tokens` (headline, per-family rows, regimes, account
regimes), `weekly_windows.max20`, and the four-account detector's `four_accounts`,
`last_change` and `events` (ADR 0001 rule 22), built with the detector's own publishing
functions from two measured changes and one provisional one.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tracker import four_accounts as F
from tracker import invariants as I
from tracker import supervise

SEP14 = "2026-09-14T16:00:00+00:00"
SEP24 = "2026-09-24T14:00:00+00:00"
OCT09 = "2026-10-09T16:00:00+00:00"


def _size(pct, lo, hi, accounts=("a1", "a2")):
    return {"change_pct": pct, "interval_pct": [lo, hi], "excludes_no_change": lo > 0 or hi < 0,
            "_d": math.log1p(pct / 100), "accounts": list(accounts),
            "per_account": {a: {"change_pct": pct} for a in accounts}}


def _change(at, found_on, state, sizes):
    found = sizes[found_on]
    h = F.hours(F.parse(at))
    return {"at": at, "_h": h, "instant_range": [h, h], "found_on": found_on,
            "sign": 1 if found["change_pct"] > 0 else -1, "state": state, "carried": state == "measured",
            "found_today": True, "held_days": 9 if state == "measured" else 3,
            "first_found": "2026-09-19", "measured_on": "2026-09-25" if state == "measured" else None,
            "p": 0.00025, "z": -6.9, "change_pct": found["change_pct"],
            "interval_pct": found["interval_pct"], "accounts": found["accounts"],
            "reading": F.reading(sizes), "sizes": sizes}


def _run() -> dict:
    """A detector run: 14 Sep on the ratio (-17.1%), 24 Sep on the seven-day line (+34.4%,
    five-hour +31.4% drawn too), 9 Oct provisional."""
    changes = [
        _change(SEP14, "ratio", "measured", {"ratio": _size(-17.1, -24.5, -9.0),
                                             "seven_day": _size(-2.2, -13.9, 10.9),
                                             "five_hour": _size(10.3, -9.2, 34.1)}),
        _change(SEP24, "seven_day", "measured", {"ratio": _size(-1.3, -9.5, 7.6),
                                                 "seven_day": _size(34.4, 25.7, 43.7),
                                                 "five_hour": _size(31.4, 14.0, 51.4)}),
        _change(OCT09, "ratio", "provisional", {"ratio": _size(-8.9, -15.2, -2.2),
                                                "seven_day": _size(4.1, -2.7, 11.3),
                                                "five_hour": _size(9.9, 1.2, 19.2)}),
    ]
    lines = {line: {a: [["2026-10-20", 1.0, 3.0, 1.0]] for a in ("a1", "a2", "a3", "a4")}
             for line in F.LINES}
    return {"schema": F.SCHEMA, "method": F.METHOD, "day": "2026-10-23",
            "data_until": "2026-10-23T00:00:00+00:00", "rule": F.RULE, "valuation": F.VALUATION,
            "headless_factor": {"value": 1.42}, "interval_coverage": {},
            "accounts": {a: {"label": F.public_label(a), "lines": {}} for a in ("a1", "a2", "a3", "a4")},
            "changes": [F._public_change(c) | {"sizes": {k: {kk: vv for kk, vv in v.items() if kk != "_d"}
                                                         for k, v in c["sizes"].items()}}
                        for c in changes],
            "withdrawn": [], "not_limit_changes": [], "lines": lines,
            "levels": {line: F.levels(changes, line) for line in F.LINES}, "seconds": 1.2}


def _good() -> dict:
    """A page consistent with the detector: every regime opens at a measured change and steps
    by the detector's size on its line, where the detector draws that line."""
    run = _run()
    spans = [(None, SEP14), (SEP14, SEP24), (SEP24, None)]
    # The window (five-hour): carried at 14 Sep, +31.4% at 24 Sep.
    window = [500, 500, 657]
    # The week (seven-day): carried at 14 Sep, +34.4% at 24 Sep; windows per week (the ratio):
    # -17.1% at 14 Sep, carried at 24 Sep.
    week = [3000, 3000, 4032]
    wpw = [6.0, 4.974, 4.974]
    regimes = [{"from": f, "until": u, "value": v, "source": "known_date_regime_cluster",
                "measured_family": "opus-5-5", "measured_value": round(v * 665 / 657)}
               for (f, u), v in zip(spans, window)]

    def acct(windows, measured):
        return [{"from": f, "until": u, "window": w, "measured_window": m}
                for (f, u), w, m in zip(spans, windows, measured)]
    return {
        "generated_at": "2026-10-23T02:45:00+00:00",
        "credits": {"window_tokens": {
            "all": {"value": 657}, "measured_family": "opus-5-5",
            "per_family": {"opus": {"all": {"value": 657}, "measured_directly": False,
                                    "rate_source": "anchor"},
                           "opus-5-5": {"all": {"value": 665}, "measured_directly": True,
                                        "rate_source": "measured"}},
            "regimes": regimes,
            "per_week_regimes": [{"from": f, "until": u, "value": v, "windows_per_week": w}
                                 for (f, u), v, w in zip(spans, week, wpw)],
            "account_regimes": {"Max account 1": acct([450, 460, 600], [None, None, None]),
                                "Max account 2": acct([490, 520, 700], [460, 500, 800]),
                                "Max account 3": acct([None, None, 640], [None, None, 620])}},
            "window_credits": {"regimes": [{"from": f, "until": u} for f, u in spans]}},
        "weekly_windows": {"max20": {
            "regimes": [{"opened_by": None, "windows": 6.0}, {"opened_by": SEP14, "windows": 4.974},
                        {"opened_by": SEP24, "windows": 4.974}],
            "by_account": {"Max account 1": {"regimes": [{"opened_by": None}, {"opened_by": SEP14}]}}}},
        "four_accounts": F.public_block(run),
        "last_change": F.last_change(run),
        "events": F.events(run),
    }


def _wt(doc):
    return doc["credits"]["window_tokens"]


def _headline(doc, anchor_value):
    """Move the headline the way the page does: the anchor figure and the family row together."""
    wt = _wt(doc)
    scale = wt["per_family"]["opus-5-5"]["all"]["value"] / wt["all"]["value"]
    wt["all"]["value"] = wt["per_family"]["opus"]["all"]["value"] = anchor_value
    wt["per_family"]["opus-5-5"]["all"]["value"] = round(anchor_value * scale)
    wt["regimes"][-1]["measured_value"] = wt["per_family"]["opus-5-5"]["all"]["value"]
    return doc


def _contradiction() -> dict:
    """A page whose headline states the provisional 9 October change: blocking."""
    doc = _good()
    run = _run()
    run["changes"][2]["state"] = "measured"
    doc["last_change"] = F.last_change(run)
    return doc


class GoodPageTest(unittest.TestCase):
    def test_the_fixture_passes_every_check(self):
        self.assertEqual(I.run_checks(_good()), {name: [] for name, _ in I.CHECKS})


class HeadlineInsideItsAccountsTest(unittest.TestCase):
    def test_passes_inside_the_scaled_account_range(self):
        self.assertEqual(I.headline_inside_accounts(_good()), [])

    def test_fails_above_every_account(self):
        doc = _headline(_good(), 990)
        (msg,) = I.headline_inside_accounts(doc)
        self.assertIn("Opus 5.5", msg)
        self.assertIn("1,002", msg)
        self.assertIn("Max account 2", msg)  # names the account at the edge it left

    def test_allows_five_percent_for_rounding(self):
        self.assertEqual(I.headline_inside_accounts(_headline(_good(), round(700 * 1.04))), [])
        self.assertEqual(len(I.headline_inside_accounts(_headline(_good(), round(700 * 1.06)))), 1)

    def test_needs_two_accounts_with_a_window(self):
        doc = _good()
        for label in ("Max account 2", "Max account 3"):
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
        self.assertEqual(I.account_units(_good()), [])

    def test_fails_outside_a_quarter_and_names_the_account_and_both_numbers(self):
        doc = _good()
        _wt(doc)["account_regimes"]["Max account 3"][2]["window"] = 300
        (msg,) = I.account_units(doc)
        self.assertIn("Max account 3", msg)
        self.assertIn("304", msg)
        self.assertIn("620", msg)
        self.assertIn(SEP24, msg)

    def test_a_missing_reading_is_skipped(self):
        doc = _good()
        _wt(doc)["account_regimes"]["Max account 3"][2]["measured_window"] = None
        _wt(doc)["account_regimes"]["Max account 3"][2]["window"] = 1
        self.assertEqual(I.account_units(doc), [])


class HeadlineIsLastMeasuredChangeTest(unittest.TestCase):
    def test_passes_on_the_newest_measured_change(self):
        self.assertEqual(I.headline_is_last_measured_change(_good()), [])

    def test_a_provisional_change_in_the_headline_fails(self):
        msgs = I.headline_is_last_measured_change(_contradiction())
        self.assertTrue(any(OCT09 in m and SEP24 in m for m in msgs), msgs)
        self.assertTrue(any("windows_per_week" in m for m in msgs), msgs)

    def test_another_figure_for_the_same_change_fails(self):
        doc = _good()
        doc["last_change"]["change_pct"] = 30.0
        doc["last_change"]["percent"] = 30
        msgs = I.headline_is_last_measured_change(doc)
        self.assertTrue(any("30.0%" in m and "34.4%" in m for m in msgs), msgs)
        self.assertTrue(any("+30%" in m and "+34%" in m for m in msgs), msgs)

    def test_a_headline_with_nothing_measured_fails_and_none_passes(self):
        doc = _good()
        for c in doc["four_accounts"]["changes"]:
            c["state"] = "provisional"
        (msg,) = I.headline_is_last_measured_change(doc)
        self.assertIn("measured none", msg)
        doc["last_change"] = None
        self.assertEqual(I.headline_is_last_measured_change(doc), [])

    def test_a_measured_change_with_no_headline_fails(self):
        doc = _good()
        doc["last_change"] = None
        (msg,) = I.headline_is_last_measured_change(doc)
        self.assertIn("no headline", msg)


class EveryChangeDrawnTest(unittest.TestCase):
    def test_passes_when_each_change_steps_its_own_line(self):
        self.assertEqual(I.every_change_drawn(_good()), [])

    def test_a_change_its_step_line_does_not_open_fails(self):
        doc = _good()
        levels = doc["four_accounts"]["levels"]["seven_day"]
        doc["four_accounts"]["levels"]["seven_day"] = [levels[0], levels[2]]
        doc["four_accounts"]["levels"]["seven_day"][1]["from"] = SEP14
        (msg,) = I.every_change_drawn(doc)
        self.assertIn(SEP24, msg)
        self.assertIn("opens no segment", msg)

    def test_a_step_line_stepping_by_another_size_fails(self):
        # The ratio's line also steps at the provisional 9 October change (-8.9%), so the
        # segment from 14 September sits at 1 / 0.911 = 1.0977.
        doc = _good()
        doc["four_accounts"]["levels"]["ratio"][0]["index"] = 1.1
        (msg,) = I.every_change_drawn(doc)
        self.assertIn("-17.1%", msg)
        self.assertIn("-0.21%", msg)

    def test_an_event_that_is_not_a_measured_change_fails(self):
        doc = _good()
        doc["events"].insert(0, dict(doc["events"][0], at="2026-09-05T10:00:00+00:00", date="2026-09-05"))
        (msg,) = I.every_change_drawn(doc)
        self.assertIn("2026-09-05", msg)


class OneFigurePerChangeTest(unittest.TestCase):
    def test_passes_when_every_chart_steps_by_the_detectors_figure(self):
        self.assertEqual(I.one_figure_per_change(_good()), [])

    def test_a_window_step_of_another_size_fails(self):
        doc = _good()
        _wt(doc)["regimes"][1]["value"] = 520  # +26% at 24 Sep where the detector says +31%
        msgs = I.one_figure_per_change(doc)
        self.assertTrue(any("window-size chart" in m and "+31%" in m for m in msgs), msgs)

    def test_a_chart_stepping_where_the_detector_draws_nothing_fails(self):
        doc = _good()
        _wt(doc)["per_week_regimes"][2]["windows_per_week"] = 4.5  # the ratio is carried at 24 Sep
        msgs = I.one_figure_per_change(doc)
        self.assertTrue(any("windows-per-week chart" in m and "+0%" in m for m in msgs), msgs)

    def test_the_weekly_windows_block_is_read_on_the_ratio(self):
        doc = _good()
        doc["weekly_windows"]["max20"]["regimes"][1]["windows"] = 5.5
        msgs = I.one_figure_per_change(doc)
        self.assertTrue(any("weekly_windows" in m for m in msgs), msgs)

    def test_an_event_with_another_figure_fails(self):
        doc = _good()
        doc["events"][0]["percent"] = 22
        msgs = I.one_figure_per_change(doc)
        self.assertTrue(any("An event says -22%" in m for m in msgs), msgs)

    def test_a_side_figure_off_the_detectors_size_fails(self):
        doc = _good()
        doc["last_change"]["five_hour_limit_change_pct"] = 20.0
        msgs = I.one_figure_per_change(doc)
        self.assertTrue(any("five_hour_limit_change_pct 20.0" in m for m in msgs), msgs)

    def test_the_dollar_history_steps_by_the_five_hour_size(self):
        doc = _good()
        doc["history"] = {"opus": [{"date": "2026-09-23", "meter_budget_per_window": 100.0},
                                   {"date": "2026-09-24", "meter_budget_per_window": 131.4}]}
        self.assertEqual(I.one_figure_per_change(doc), [])
        doc["history"]["opus"][1]["meter_budget_per_window"] = 120.0
        msgs = I.one_figure_per_change(doc)
        self.assertTrue(any("dollar history" in m for m in msgs), msgs)

    def test_half_a_percent_rounds_the_way_the_page_does(self):
        self.assertEqual(I._page_round(-12.5), -12)
        self.assertEqual(I._page_round(12.5), 13)


class RegimesOpenAtMeasuredChangesTest(unittest.TestCase):
    def test_passes_when_every_boundary_is_a_measured_change(self):
        self.assertEqual(I.regimes_open_at_measured_changes(_good()), [])

    def test_a_boundary_the_detector_did_not_measure_fails(self):
        doc = _good()
        _wt(doc)["regimes"][1]["from"] = "2026-09-13T22:45:34+00:00"
        msgs = I.regimes_open_at_measured_changes(doc)
        self.assertTrue(any("2026-09-13T22:45:34" in m for m in msgs), msgs)
        self.assertTrue(any(f"{SEP14} opens no regime" in m for m in msgs), msgs)

    def test_a_provisional_change_opens_no_regime(self):
        doc = _good()
        doc["weekly_windows"]["max20"]["regimes"].append({"opened_by": OCT09, "windows": 4.974})
        (msg,) = I.regimes_open_at_measured_changes(doc)
        self.assertIn(OCT09, msg)


class AccountsNamedByLabelTest(unittest.TestCase):
    def test_passes_with_labels(self):
        self.assertEqual(I.accounts_named_by_label(_good()), [])

    def test_a_bare_key_or_an_account_name_fails(self):
        doc = _good()
        doc["four_accounts"]["lines"]["ratio"]["a2"] = []
        doc["events"][0]["accounts"] = ["jwork", "Max account 1"]
        msgs = I.accounts_named_by_label(doc)
        self.assertTrue(any("four_accounts" in m and "lines.ratio.a2" in m for m in msgs), msgs)
        self.assertTrue(any("events[0]" in m for m in msgs), msgs)


class MeasuredChangesStillFoundTest(unittest.TestCase):
    def test_a_measured_change_not_found_today_alerts_but_stays(self):
        doc = _good()
        doc["four_accounts"]["changes"][0]["found_today"] = False
        (msg,) = I.measured_changes_still_found(doc)
        self.assertIn(SEP14, msg)
        self.assertIn("never withdrawn", msg)
        self.assertNotIn("measured_changes_still_found", I.BLOCKING)


class DetectorRanTest(unittest.TestCase):
    def test_a_run_more_than_a_day_old_fails(self):
        doc = _good()
        self.assertEqual(I.detector_ran(doc), [])
        doc["four_accounts"]["day"] = "2026-10-21"
        (msg,) = I.detector_ran(doc)
        self.assertIn("2026-10-21", msg)

    def test_no_detector_block_fails(self):
        doc = _good()
        del doc["four_accounts"]
        (msg,) = I.detector_ran(doc)
        self.assertIn("no four_accounts block", msg)


class RunAllTest(unittest.TestCase):
    def test_every_check_runs_and_is_named(self):
        self.assertEqual([name for name, _ in I.CHECKS],
                         ["headline_inside_accounts", "direct_means_direct", "account_units",
                          "headline_is_last_measured_change", "every_change_drawn",
                          "one_figure_per_change", "regimes_open_at_measured_changes",
                          "accounts_named_by_label", "measured_changes_still_found", "detector_ran"])
        self.assertEqual(I.BLOCKING, {"headline_is_last_measured_change", "every_change_drawn",
                                      "one_figure_per_change", "regimes_open_at_measured_changes",
                                      "accounts_named_by_label"})

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

    ADVISORY = staticmethod(lambda: _headline(copy.deepcopy(_good()), 990))   # check 1: exit 1
    BLOCKING = staticmethod(_contradiction)                                   # check 4: exit 2
    DAY = 24 * 3600

    def test_an_incident_that_starts_withholding_says_so_once(self):
        self.assertEqual(self.check(self.ADVISORY(), 0), 1)
        self.assertIn("Published anyway", self.sent[0])
        self.assertEqual(self.check(self.BLOCKING(), 1800), 2)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("the public JSON is WITHHELD", self.sent[1])
        self.assertIn("withheld since 1970-01-01T00:30Z", self.sent[1])
        self.assertIn("headline_is_last_measured_change", self.sent[1])
        self.assertNotIn("headline_inside_accounts", self.sent[1], "the withheld message names what blocks")
        for t in range(3600, 6 * 3600, 1800):
            self.check(self.BLOCKING(), t)
        self.assertEqual(len(self.sent), 2, "nothing more while it keeps withholding")

    def test_a_change_in_which_checks_fail_is_not_a_new_alert(self):
        units = _good()
        _wt(units)["account_regimes"]["Max account 3"][2]["window"] = 300   # check 3 instead of 1
        drawn = _good()
        drawn["four_accounts"]["levels"]["ratio"][0]["index"] = 1.1         # check 5 instead of 4
        self.assertEqual([k for k, v in I.run_checks(units).items() if v], ["account_units"])
        self.assertEqual([k for k, v in I.run_checks(drawn).items() if v],
                         ["every_change_drawn", "one_figure_per_change"])
        t = 0
        for doc in [self.ADVISORY(), units] * 4:                            # warn-only sets flapping
            self.assertEqual(self.check(doc, t), 1)
            t += 1800
        self.assertEqual(len(self.sent), 1, self.sent)
        for doc in [self.BLOCKING(), drawn] * 4:                            # blocking sets flapping
            self.assertEqual(self.check(doc, t), 2)
            t += 1800
        self.assertEqual(len(self.sent), 2, self.sent)
        self.assertIn("WITHHELD", self.sent[1])

    def test_a_withheld_page_is_said_again_every_24_hours(self):
        self.check(self.BLOCKING(), 0)
        self.assertIn("Not published", self.sent[0])
        for t in range(1800, 3 * self.DAY + 1, 1800):
            self.check(self.BLOCKING(), t)
        self.assertEqual(len(self.sent), 4, [s[:80] for s in self.sent])
        self.assertTrue(all("WITHHELD" in s for s in self.sent[1:]))
        self.assertIn("(withheld since 1970-01-01T00:00Z, 72 h)", self.sent[3])

    def test_a_withhold_that_flaps_waits_for_the_reminder(self):
        self.check(self.ADVISORY(), 0)
        self.check(self.BLOCKING(), 1800)            # withheld: said
        self.check(self.ADVISORY(), 3600)            # published again: nothing
        self.check(self.BLOCKING(), 5400)            # withheld again within a day: nothing
        self.assertEqual(len(self.sent), 2)
        self.check(self.BLOCKING(), 1800 + self.DAY)
        self.assertEqual(len(self.sent), 3)
        self.assertIn("WITHHELD", self.sent[2])

    def test_recovery_closes_everything_and_a_new_withhold_alerts_at_once(self):
        self.check(self.BLOCKING(), 0)
        self.check(_good(), 1800)
        self.assertIn("pass again after 30 min", self.sent[-1])
        self.assertEqual(json.loads(self.state.read_text()).get("withheld_alerted_at"), None)
        self.check(self.BLOCKING(), 3600)
        self.assertEqual(len(self.sent), 3)
        self.assertIn("Not published", self.sent[2])

    def test_a_failed_withheld_send_is_tried_again_next_run(self):
        self.check(self.ADVISORY(), 0)
        ok = [False]
        sent = self.sent

        def flaky(text):
            sent.append(text)
            return ok[0]
        self.doc.write_text(json.dumps(self.BLOCKING()))
        I.check_file(self.doc, self.state, flaky, now=lambda: 1800)
        ok[0] = True
        I.check_file(self.doc, self.state, flaky, now=lambda: 3600)
        self.assertEqual([("WITHHELD" in s) for s in sent], [False, True, True])

    def test_the_log_line_says_what_was_sent(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.check(self.BLOCKING(), 0)
            self.check(self.BLOCKING(), 1800)
        lines = [ln for ln in out.getvalue().splitlines() if ln.startswith("invariants alert:")]
        self.assertEqual(lines[0], "invariants alert: alert sent")
        self.assertEqual(lines[1], "invariants alert: incident open 30 min, withheld 30 min, last said 30 min ago")


class BlockingTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.state, self.doc = d / "ops" / "inv.json", d / "claude-usage.json"
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)
        return True

    def test_a_contradiction_on_the_page_exits_2_and_says_not_published(self):
        self.doc.write_text(json.dumps(_contradiction()))
        self.assertEqual(I.check_file(self.doc, self.state, self.send, now=lambda: 0), 2)
        self.assertIn("Not published", self.sent[0])

    def test_the_consistent_page_exits_0(self):
        self.doc.write_text(json.dumps(_good()))
        self.assertEqual(I.check_file(self.doc, self.state, self.send, now=lambda: 0), 0)


if __name__ == "__main__":
    unittest.main()
