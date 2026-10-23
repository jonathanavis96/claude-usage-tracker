"""tracker.publish: the headline percent is the page's label for the signed figure.

The page labels a change with JavaScript Math.round on the signed percent (halves up), and
tracker/invariants.py compares the headline against that label. The publisher once wrote
round(abs(x)), which rounds halves to even: +44.5 published as 44 beside a +45% chart
label, -45.5 as 46 beside -45%, and the invariants then withheld the page over the
publisher's own rounding. Every change the page states is now a record of the four-account
detector (`four_accounts.change_record`, ADR 0001 rule 22), so that is where the rounding is
pinned.
"""
from __future__ import annotations

import math
import unittest

from tracker import four_accounts as F
from tracker import invariants

#: (signed percent, the page's label as an unsigned headline percent)
CASES = ((44.5, 45), (-44.5, 44), (-45.5, 45), (29.4, 29), (-29.6, 30), (-44.8, 45), (12.5, 13),
         (-37.5, 37))


def _signed(rec: dict) -> int:
    return -rec["percent"] if rec["direction"] == "decreased" else rec["percent"]


def _change(pct: float, line: str = "seven_day") -> dict:
    size = {"change_pct": pct, "interval_pct": [pct - 5, pct + 5], "excludes_no_change": True,
            "agree": True, "_d": math.log1p(pct / 100), "accounts": ["a1", "a2"],
            "per_account": {"a1": {"change_pct": pct}, "a2": {"change_pct": pct}}}
    return {"at": "2026-09-23T13:00:00+00:00", "instant_range": ["2026-09-23T13:00:00+00:00"] * 2,
            "found_on": line, "state": "measured", "sizes": {line: size}, "accounts": ["a1", "a2"],
            "reading": "figures_only", "held_days": 7, "first_found": "2026-10-05",
            "measured_on": "2026-10-11", "p": 0.00025}


class PublisherRoundsLikeThePageTest(unittest.TestCase):
    def test_a_change_record_percent_is_the_page_label(self):
        for signed, label in CASES:
            for line in ("seven_day", "ratio"):
                with self.subTest(signed=signed, line=line):
                    rec = F.change_record(_change(signed, line), {})
                    self.assertEqual(rec["percent"], label)
                    self.assertEqual(_signed(rec), invariants._page_round(signed))
                    # The invariants' own comparison of change_pct with the headline agrees.
                    self.assertEqual(invariants._page_round(rec["change_pct"]), _signed(rec))

    def test_the_detector_and_the_invariants_round_alike(self):
        for signed, _ in CASES:
            with self.subTest(signed=signed):
                self.assertEqual(F.page_round(signed), invariants._page_round(signed))

    def test_a_step_line_step_rounds_to_the_change_it_draws(self):
        # The step line's levels are published to four decimals; the step they draw rounds
        # to the change's own whole percent, halves included.
        for signed, _ in CASES:
            with self.subTest(signed=signed):
                c = dict(_change(signed), _h=F.hours(F.parse("2026-09-23T13:00:00+00:00")))
                segs = F.levels([c], "seven_day")
                step = (segs[1]["index"] / segs[0]["index"] - 1) * 100
                self.assertLessEqual(abs(step - signed), invariants.DRAWN_TOLERANCE_PP)


if __name__ == "__main__":
    unittest.main()
