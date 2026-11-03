"""Defects in tracker/publish.py found by the 2026-11-03 sweep, each pinned by a test."""
import os
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from tests.test_publish import (EFFORT, PASSIVE, PRICES, _detector_change, _detector_size,
                                detector_run)
from tracker.publish import build_public_json


class NaiveReadingStampTests(unittest.TestCase):
    def test_a_naive_reading_stamp_is_utc_not_the_hosts_local_time(self):
        # Code review round 6: gs_passive files a naive stamp under its UTC day, so _in_utc
        # must not read it in the host's zone.
        from tracker.publish import _in_utc
        with mock.patch.dict(os.environ, {"TZ": "Africa/Johannesburg"}):
            time.tzset()
            try:
                got = _in_utc([(datetime(2026, 11, 3, 1, 0), 1.0)])
            finally:
                os.environ.pop("TZ", None)
        time.tzset()
        self.assertEqual(got, [(datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc), 1.0)])


class EvidencePastTheFiveHourEndTests(unittest.TestCase):
    """A change the detector finds on the seven-day line after the five-hour limit's end
    opens no five-hour regime with readings: the frozen figure is still the regime that holds
    the end, so its `evidence` must describe that regime's readings, not an empty one."""

    NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    END = datetime(2026, 10, 30, 23, 37, tzinfo=timezone.utc)

    def _stretch(self, end):
        return {"status": "accepted", "capture_status": "accepted", "reset_verified": True,
                "start": end.isoformat(), "end": end.isoformat(), "delta_pct": 10, "windows": 1,
                "tokens": {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0,
                                               "cache_write": 400000}}}

    def _gs(self):
        last = (self.NOW - timedelta(hours=1)).isoformat()
        return {"generated_at": last, "accounts": {
            "jwork": {"meter": {"last": last, "five_hour_last": self.END.isoformat(),
                                "weekly_only_since": (self.END + timedelta(minutes=6)).isoformat()},
                      "stretches": [self._stretch(self.END - timedelta(days=d)) for d in (1, 2, 3)]}}}

    def test_a_weekly_change_after_the_end_leaves_the_frozen_figures_evidence_in_place(self):
        self._check_change_after_the_end(timedelta(days=2))

    def test_a_weekly_change_later_on_the_end_day_leaves_it_in_place_too(self):
        # Code review round 1: the end day's readings all precede the end instant, so a change
        # ten minutes after it holds none of them; filtering changes by date let it open the
        # current regime and cut the evidence down to that one day.
        self._check_change_after_the_end(timedelta(minutes=10))

    def test_a_change_after_the_end_on_a_day_with_readings_keeps_them_as_the_evidence(self):
        # The workflow's publish review: regimes split by date, so a reading earlier on the end
        # day falls in the regime that opens that day, and the frozen figure is its median.
        # The evidence must be that regime's reading, not the regime before it.
        gs = self._gs()
        gs["accounts"]["jwork"]["stretches"].append(self._stretch(self.END - timedelta(hours=1)))
        before = _detector_change((self.END - timedelta(days=20)).isoformat(), "seven_day", "measured",
                                  {"seven_day": _detector_size(-30.0, -36.0, -24.0),
                                   "five_hour": _detector_size(-30.0, -38.0, -21.0),
                                   "ratio": _detector_size(1.0, -4.0, 6.0)})
        after = _detector_change((self.END + timedelta(minutes=10)).isoformat(), "seven_day", "provisional",
                                 {"seven_day": _detector_size(12.0, 5.0, 19.0)})
        out = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=gs,
                                four_accounts_run=detector_run(before, after))
        ev = out["rates"]["claude-sonnet-5"]["evidence"]
        self.assertEqual(ev["readings"], 1)
        self.assertTrue(ev["observed_from"].startswith(self.END.date().isoformat()), ev["observed_from"])

    def test_a_reading_in_another_offset_is_dated_by_its_utc_day(self):
        # Code review round 5: the last stretch ends 2026-10-31T01:07+02:00, which is 30 Oct in
        # UTC, the day passive_dollar_readings filed it under. Dated by its own offset it fell
        # after a change at 31 Oct 01:37Z that it preceded, and the evidence shrank to it alone.
        sast = timezone(timedelta(hours=2))
        gs = self._gs()
        gs["accounts"]["jwork"]["stretches"] = [
            self._stretch((self.END - timedelta(minutes=30, days=d)).astimezone(sast)) for d in (0, 1, 2)]
        self._check_change_after_the_end(timedelta(hours=2), gs=gs)

    def test_the_evidence_counts_stretches_by_their_utc_day_too(self):
        # Code review round 6: `since` is a UTC day now, but stretches were still filtered by
        # their own offset's date, so a +02:00 stretch from the UTC day before was counted.
        # The 30 Oct 23:07Z stretch, written +02:00 as 31 Oct, is the evidence's first reading;
        # one 23:30Z on 29 Oct, written as 30 Oct 01:30+02:00, is not inside it.
        sast = timezone(timedelta(hours=2))
        gs = self._gs()
        recent = [self._stretch((self.END - timedelta(minutes=30, hours=h)).astimezone(sast)) for h in (0, 2)]
        before = self._stretch(datetime(2026, 10, 29, 23, 30, tzinfo=timezone.utc).astimezone(sast))
        gs["accounts"]["jwork"]["stretches"] = [before, *recent]
        change = _detector_change(datetime(2026, 10, 30, 6, tzinfo=timezone.utc).isoformat(), "seven_day",
                                  "measured", {"seven_day": _detector_size(-30.0, -36.0, -24.0),
                                               "five_hour": _detector_size(-30.0, -38.0, -21.0),
                                               "ratio": _detector_size(1.0, -4.0, 6.0)})
        out = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=gs,
                                four_accounts_run=detector_run(change))
        ev = out["rates"]["claude-sonnet-5"]["evidence"]
        self.assertEqual((ev["readings"], ev["stretches"]), (1, 2), ev)

    def _check_change_after_the_end(self, delay, gs=None):
        before = _detector_change((self.END - timedelta(days=20)).isoformat(), "seven_day", "measured",
                                  {"seven_day": _detector_size(-30.0, -36.0, -24.0),
                                   "five_hour": _detector_size(-30.0, -38.0, -21.0),
                                   "ratio": _detector_size(1.0, -4.0, 6.0)})
        after = _detector_change((self.END + delay).isoformat(), "seven_day", "provisional",
                                 {"seven_day": _detector_size(12.0, 5.0, 19.0)})
        without = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=gs or self._gs(),
                                    four_accounts_run=detector_run(before))
        with_after = build_public_json([], PASSIVE, EFFORT, PRICES, self.NOW, gs_passive=gs or self._gs(),
                                       four_accounts_run=detector_run(before, after))
        model = "claude-sonnet-5"
        ev, ref = with_after["rates"][model]["evidence"], without["rates"][model]["evidence"]
        # The frozen figure is the same either way...
        self.assertEqual(with_after["rates"][model]["meter_budget_per_window"],
                         without["rates"][model]["meter_budget_per_window"])
        self.assertGreater(ref["readings"], 0)
        # ...and so is what it rests on: a change past the end holds no five-hour reading.
        for key in ("readings", "stretches", "observed_from", "observed_to", "meter_movement_pct",
                    "account_count"):
            self.assertEqual(ev[key], ref[key], key)
        self.assertEqual(with_after["passive_account_count"], without["passive_account_count"])


class LastMeterReadTests(unittest.TestCase):
    """`meter_read_at` is the newest instant across the accounts' meter reads, compared as
    instants: a string max reads an offset or a 'Z' stamp out of time order."""

    def test_the_newest_instant_wins_whatever_its_spelling(self):
        from tracker.publish import _last_meter_read
        gs = {"accounts": {"jwork": {"meter": {"last": "2026-11-02T23:00:00Z"}},
                           "dave": {"meter": {"last": "2026-11-03T00:30:00+02:00"}},
                           "avis": {"meter": {"last": "not a stamp"}}}}
        self.assertEqual(_last_meter_read(gs), "2026-11-02T23:00:00+00:00")

    def test_utc_stamps_are_published_unchanged(self):
        from tracker.publish import _last_meter_read
        gs = {"accounts": {"jwork": {"meter": {"last": "2026-11-02T21:49:54+00:00"}},
                           "dave": {"meter": {"last": "2026-11-02T21:57:06+00:00"}},
                           "avis": {"meter": None}}}
        self.assertEqual(_last_meter_read(gs), "2026-11-02T21:57:06+00:00")
        self.assertIsNone(_last_meter_read({"accounts": {"x": {"meter": {"last": ""}}}}))


if __name__ == "__main__":
    unittest.main()


class EligibleStretchesMirrorTheDollarSeriesTests(unittest.TestCase):
    """The evidence beside a rate counts the stretches passive_dollar_readings reads, so the
    two must select alike: a zero-token `<synthetic>` bundle no longer drops a stretch from
    the series (afe20044), and must not drop it from the evidence either."""

    def _report(self, *token_sets):
        end = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        stretches = [{"status": "accepted", "reset_verified": True, "delta_pct": 4, "windows": 1,
                      "start": (end + timedelta(hours=i)).isoformat(),
                      "end": (end + timedelta(hours=i, minutes=30)).isoformat(), "tokens": t}
                     for i, t in enumerate(token_sets)]
        return {"accounts": {"jwork": {"account": "jwork", "stretches": stretches}}}

    def test_evidence_and_series_select_the_same_stretches(self):
        from tracker.gs_passive import passive_dollar_readings
        from tracker.publish import _eligible_stretches
        work = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 400000}
        synthetic = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        report = self._report({"claude-sonnet-5": work},
                              {"claude-sonnet-5": work, "<synthetic>": synthetic},
                              {"<synthetic>": synthetic},
                              {})
        series = passive_dollar_readings(report, PRICES, by="stretch")
        evidence = _eligible_stretches(report, PRICES, allow_legacy_unverified=False)
        self.assertEqual(len(series), 2)
        self.assertEqual(len(evidence), len(series))
        self.assertEqual(sorted(datetime.fromisoformat(s["end"]) for s in evidence), [t for t, _ in series])
