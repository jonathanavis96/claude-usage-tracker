"""tracker.publish: the headline percent is the page's label for the signed figure.

The page labels a change with JavaScript Math.round on the signed percent (halves up), and
tracker/invariants.py check 7 compares the headline against that label. The publisher wrote
round(abs(x)), which rounds halves to even: +44.5 published as 44 beside a +45% chart
label, -45.5 as 46 beside -45%, and check 7 then withholds the page over the publisher's
own rounding.
"""
from __future__ import annotations

import unittest

from tracker import invariants, publish

#: (signed percent, the page's label as an unsigned headline percent)
CASES = ((44.5, 45), (-44.5, 44), (-45.5, 45), (29.4, 29), (-29.6, 30), (-44.8, 45))


def _signed(rec: dict) -> int:
    return -rec["percent"] if rec["direction"] == "decreased" else rec["percent"]


class PublisherRoundsLikeThePageTest(unittest.TestCase):
    def test_tokens_per_week_percent_is_the_page_label(self):
        for signed, label in CASES:
            with self.subTest(signed=signed):
                leg = {"change_pct": signed, "interval_pct": [signed - 5, signed + 5]}
                route = {"per_account": {"a1": {"windows_per_week": leg, "window": leg, "weekly": leg}},
                         "combined": {"weekly": leg, "windows_per_week": leg, "window": leg}}
                rec = publish._tokens_per_week_change({"ratio_route": route})
                self.assertEqual(rec["percent"], label)
                self.assertEqual(_signed(rec), invariants._page_round(signed))

    def test_a_meter_measured_change_percent_is_the_page_label(self):
        for signed, label in CASES:
            with self.subTest(signed=signed):
                cand = {"headline": {"change_pct": signed, "interval_pct": [signed - 9, signed + 9],
                                     "metric": "five_hour_limit"},
                        "window_change": {"change_pct": signed, "interval_pct": [signed - 9, signed + 9],
                                          "per_account": {}},
                        "weekly_change": {}, "certified_on": ["five_hour_limit"],
                        "at": "2026-10-17T02:16:18+00:00", "state": "provisional", "family": "opus-4-6",
                        "windows_per_week_change_pct": None, "windows_per_week_change_interval_pct": None,
                        "windows_per_week_ratio": None, "windows_per_week_ratio_interval": None,
                        "readings": None}
                rec = publish._announced_event_record(cand)
                self.assertEqual(rec["percent"], label)
                self.assertEqual(_signed(rec), invariants._page_round(signed))
                # check 7's own comparison of change_pct with the headline then agrees
                self.assertEqual(invariants._page_round(rec["change_pct"]), _signed(rec))


if __name__ == "__main__":
    unittest.main()
