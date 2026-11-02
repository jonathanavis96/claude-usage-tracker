import unittest
from datetime import datetime, timedelta, timezone

from tracker import five_hour_end as E
from tracker.samples import Sample

T0 = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def iso(h: float) -> str:
    return (T0 + timedelta(hours=h)).isoformat()


def account(last5, since, last, stretches=(), steps=(), points=()):
    return {"meter": {"five_hour_last": last5, "weekly_only_since": since, "last": last},
            "stretches": [{"start": iso(a), "end": iso(b)} for a, b in stretches],
            "weekly_steps": [{"start": iso(a), "end": iso(b), "d5": 3, "five_hour_measured": True}
                             for a, b in steps],
            "weekly_by_window": [{"window_ending": iso(h)} for h in points]}


class MeterFieldsTest(unittest.TestCase):
    def sample(self, h, five):
        return Sample(T0 + timedelta(hours=h), five, 10.0, None, "moonlighter")

    def test_the_switch_is_the_first_weekly_only_reading_after_the_last_five_hour_one(self):
        five = [self.sample(0, 1.0), self.sample(1, 2.0)]
        weekly = five + [self.sample(2, None), self.sample(3, None)]
        self.assertEqual(E.meter_fields(five, weekly), {"five_hour_last": iso(1), "weekly_only_since": iso(2)})

    def test_an_account_whose_newest_reading_has_a_five_hour_figure_has_not_switched(self):
        # A weekly-only gap that ended (the limit came back) is no switch.
        five = [self.sample(0, 1.0), self.sample(3, 2.0)]
        weekly = [five[0], self.sample(1, None), self.sample(2, None), five[1]]
        self.assertEqual(E.meter_fields(five, weekly)["weekly_only_since"], None)

    def test_an_account_that_never_had_a_five_hour_reading(self):
        weekly = [self.sample(0, None), self.sample(1, None)]
        self.assertEqual(E.meter_fields([], weekly), {"five_hour_last": None, "weekly_only_since": iso(0)})


class EndTest(unittest.TestCase):
    def reports(self):
        gs = {"accounts": {
            "jwork": account(iso(11), iso(11.1), iso(60)),         # switched first
            "dave": account(iso(30), iso(31), iso(60)),            # switched later
            "avis": account(iso(60), None, iso(60)),               # still has its five-hour limit
        }}
        mr = {"accounts": {"masterrig": account(iso(20), iso(59), iso(60))}}  # weekly-only 1h: not yet
        return gs, mr

    def test_the_end_is_the_earliest_switched_accounts_last_five_hour_reading(self):
        gs, mr = self.reports()
        self.assertEqual(E.end_of(gs, mr), T0 + timedelta(hours=11))
        sw = E.switches(gs, mr)
        self.assertEqual({n for n, s in sw.items() if s["switched"]}, {"jwork", "dave"})

    def test_no_end_while_every_account_has_its_five_hour_limit(self):
        gs = {"accounts": {"avis": account(iso(5), None, iso(5))}}
        self.assertIsNone(E.end_of(gs, None))
        # A report from before the fields existed has not switched either.
        self.assertIsNone(E.end_of({"accounts": {"a": {"meter": {"last": iso(5)}}}}))
        self.assertEqual(E.apply(gs, None), (gs, None, None))

    def test_trimming_cuts_five_hour_readings_after_the_end_and_keeps_the_weekly_steps(self):
        gs = {"accounts": {"avis": account(iso(60), None, iso(60),
                                           stretches=[(0, 5), (9, 12), (20, 25)],
                                           steps=[(0, 2), (20, 22)], points=[5, 25])}}
        out = E.trimmed(gs, T0 + timedelta(hours=11))
        body = out["accounts"]["avis"]
        self.assertEqual([s["end"] for s in body["stretches"]], [iso(5)])
        self.assertEqual([p["window_ending"] for p in body["weekly_by_window"]], [iso(5)])
        self.assertEqual([s["five_hour_measured"] for s in body["weekly_steps"]], [True, False])
        self.assertEqual(len(body["weekly_steps"]), 2)
        # The input is untouched.
        self.assertEqual(len(gs["accounts"]["avis"]["stretches"]), 3)
        self.assertTrue(gs["accounts"]["avis"]["weekly_steps"][1]["five_hour_measured"])

    def test_a_dropped_withheld_stretch_still_withholds_the_steps_it_overlapped(self):
        # weekly_meter.clean_steps leaves out a step overlapping a stretch the capture check
        # withheld; trimming that stretch away must not admit the step.
        body = account(iso(60), None, iso(60), steps=[(20, 22), (30, 32), (40, 42)])
        body["stretches"] = [{"start": iso(19), "end": iso(23), "status": "withheld"},
                             {"start": iso(29), "end": iso(33), "status": "accepted"}]
        out = E.trimmed({"accounts": {"avis": body}}, T0 + timedelta(hours=11))["accounts"]["avis"]
        self.assertEqual(out["stretches"], [])
        self.assertEqual([s.get("withheld_by_stretch", False) for s in out["weekly_steps"]], [True, False, False])
        from tracker.weekly_meter import clean_steps
        kept = clean_steps({"avis": out["weekly_steps"]}, [], {"avis": out["stretches"]})
        self.assertEqual([s["start"] for s in kept["avis"]], [iso(30), iso(40)])

    def test_passive_json_window_points_are_cut_too(self):
        passive = {"weekly_windows": {"by_window": [{"window_ending": iso(5)}, {"window_ending": iso(25)}]}}
        out = E.trimmed_passive(passive, T0 + timedelta(hours=11))
        self.assertEqual(out["weekly_windows"]["by_window"], [{"window_ending": iso(5)}])

    def test_public_block_names_accounts_by_page_label_only(self):
        gs, mr = self.reports()
        labels = {"masterrig": "Max account 1", "jwork": "Max account 2", "dave": "Max account 3",
                  "avis": "Max account 4"}
        b = E.public_block(gs, mr, labels)
        self.assertEqual(b["status"], "removed")
        self.assertEqual(b["ended_at"], iso(11))
        self.assertEqual(sorted(b["accounts"]), sorted(labels.values()))
        self.assertTrue(b["accounts"]["Max account 2"]["switched"])
        self.assertEqual(b["accounts"]["Max account 4"],
                         {"switched": False, "five_hour_last": None, "weekly_only_since": None})
        self.assertEqual(E.public_block({"accounts": {}}, None, labels)["status"], "in_force")
        # An account with no page label is left out, never published by name.
        gs["accounts"]["someone"] = account(iso(1), iso(2), iso(60))
        self.assertNotIn("someone", __import__("json").dumps(E.public_block(gs, mr, labels)))


if __name__ == "__main__":
    unittest.main()
