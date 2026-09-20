import unittest
from datetime import datetime, timedelta, timezone

from tracker.capture import (
    BOOTSTRAP,
    TOLERANCE,
    UNACCOUNTED,
    UNKNOWN,
    Run,
    Verdict,
    _agrees,
    check,
    judge,
    published,
    runs,
)
from tracker.join import Stretch

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
STEADY = [1.00, 1.02, 0.98, 1.01, 0.99, 1.00]
SHIFTED = [0.72, 0.73, 0.71, 0.74, 0.72, 0.73]      # 27% down, and agreeing with each other


def st(i, rate, delta=10.0, windows=1, unpriced=0):
    start = T0 + timedelta(hours=i)
    return Stretch(start, start + timedelta(minutes=50), delta_pct=delta, windows=windows, usd=rate * delta,
                   tokens={"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 1_000_000}},
                   unpriced_tokens=unpriced)


def series(*rates):
    return [st(i, r) for i, r in enumerate(rates)]


def statuses(verdicts):
    return [v.status for v in verdicts]


class JudgeTests(unittest.TestCase):
    def test_steady_capture_is_accepted_once_the_reference_is_bootstrapped(self):
        vs = judge(series(*STEADY, 1.01, 0.99))
        self.assertEqual(statuses(vs), ["accepted"] * 8)
        self.assertAlmostEqual(vs[-1].reference, 1.0)

    def test_a_stretch_with_transcripts_withheld_is_caught_and_not_published(self):
        # Two fifths of the stretch's tokens never reached the transcripts: its meter
        # dollars read 0.6 of the account's own reference.
        vs = judge(series(*STEADY, 0.6, 1.0))
        self.assertEqual(statuses(vs)[-2:], ["unaccounted", "accepted"])
        self.assertAlmostEqual(vs[-2].capture, 0.6)
        self.assertNotIn(vs[-2], published(vs))
        self.assertEqual(len(published(vs)), 7)

    def test_rounding_alone_never_convicts_a_stretch(self):
        # 0.70 is outside the band, but a stretch summed over three window pieces
        # carries three points of rounding in ten, enough to put the truth back inside it.
        self.assertEqual(judge(series(*STEADY) + [st(6, 0.70, windows=3)])[-1].status, "accepted")
        self.assertEqual(judge(series(*STEADY) + [st(6, 0.70, windows=1)])[-1].status, "unaccounted")

    def test_transcripts_that_outspend_the_meter_are_withheld_as_surplus(self):
        self.assertEqual(judge(series(*STEADY, 1.5))[-1].status, "surplus")

    def test_a_stretch_with_unpriced_traffic_is_not_judged(self):
        vs = judge(series(*STEADY) + [st(6, 1.0, unpriced=100_000)])
        self.assertEqual(vs[-1].status, "unpriced")
        self.assertNotIn(vs[-1], published(vs))

    def test_too_few_stretches_to_bootstrap_a_reference_are_unjudged_not_accepted(self):
        vs = judge(series(*[1.0] * (BOOTSTRAP - 1)))
        self.assertEqual(statuses(vs), ["unjudged"] * (BOOTSTRAP - 1))
        self.assertEqual(published(vs), [])

    def test_the_reference_is_the_accounts_own_not_a_fixed_level(self):
        vs = judge(series(*[x * 3 for x in STEADY], 3.0))
        self.assertEqual(vs[-1].status, "accepted")
        self.assertAlmostEqual(vs[-1].reference, 3.0)

    def test_tolerance_is_the_trackers_standing_fifteen_percent(self):
        self.assertEqual(TOLERANCE, 0.15)


class RunTests(unittest.TestCase):
    def test_meter_moving_with_almost_no_transcripts_is_a_collection_gap(self):
        rs = runs(judge(series(*STEADY, 0.02, 0.01, 0.03)))
        self.assertEqual([(r.kind, r.direction, len(r.verdicts)) for r in rs], [("collection_gap", "low", 3)])

    def test_scattered_low_readings_are_unaccounted_traffic(self):
        rs = runs(judge(series(*STEADY, 0.5, 0.2, 0.7, 0.35)))
        self.assertEqual([(r.kind, len(r.verdicts)) for r in rs], [("unaccounted", 4)])

    def test_low_readings_that_agree_with_each_other_are_a_level_shift_candidate_not_a_rate(self):
        vs = judge(series(*STEADY, *SHIFTED[:4]))
        rs = runs(vs)
        self.assertEqual([r.kind for r in rs], ["level_shift"])
        self.assertAlmostEqual(rs[0].step, 0.725 - 1)
        self.assertEqual(len(published(vs)), len(STEADY))

    def test_an_accepted_stretch_ends_a_run(self):
        rs = runs(judge(series(*STEADY, 0.6, 1.0, 0.6)))
        self.assertEqual([(r.kind, len(r.verdicts)) for r in rs], [("unaccounted", 1), ("unaccounted", 1)])


class CheckTests(unittest.TestCase):
    def test_a_shift_on_both_accounts_at_once_is_a_limit_change_and_rebases_both(self):
        res = check({"jwork": series(*STEADY, *SHIFTED), "dave": series(*STEADY, *reversed(SHIFTED))})
        self.assertEqual(sorted((c["account"], c["corroborated_by"][0]) for c in res.changes),
                         [("dave", "jwork"), ("jwork", "dave")])
        self.assertEqual(res.changes[0]["at"], st(6, 0).start.isoformat())
        for account in ("jwork", "dave"):
            self.assertEqual(statuses(res.verdicts[account]), ["accepted"] * 12, account)
            self.assertEqual(res.runs[account], [])

    def test_a_shift_on_one_account_while_the_other_holds_steady_is_capture_not_a_change(self):
        res = check({"jwork": series(*STEADY, *SHIFTED), "dave": series(*STEADY, *STEADY)})
        self.assertEqual(res.changes, [])
        [run] = res.runs["jwork"]
        self.assertEqual((run.kind, run.corroborated_by, run.contradicted_by), ("level_shift", [], ["dave"]))
        self.assertEqual(statuses(res.verdicts["jwork"])[6:], ["unaccounted"] * 6)

    def test_a_probe_that_moved_the_same_way_confirms_the_change(self):
        before = [(T0 - timedelta(days=d), 0.96) for d in (4, 3, 2, 1)]
        res = check({"jwork": series(*STEADY, *SHIFTED)}, probe_readings=before + [(T0 + timedelta(hours=8), 0.70)])
        self.assertEqual([(c["account"], c["corroborated_by"]) for c in res.changes], [("jwork", ["probe"])])
        self.assertEqual(statuses(res.verdicts["jwork"]), ["accepted"] * 12)

    def test_a_probe_that_did_not_move_says_the_shift_is_capture(self):
        before = [(T0 - timedelta(days=d), 0.96) for d in (4, 3, 2, 1)]
        res = check({"jwork": series(*STEADY, *SHIFTED)}, probe_readings=before + [(T0 + timedelta(hours=8), 0.95)])
        self.assertEqual(res.changes, [])
        self.assertEqual(res.runs["jwork"][0].contradicted_by, ["probe"])

    def test_probe_readings_are_compared_as_steps_not_levels(self):
        # Real sessions and the probe payload value differently under the current class
        # weights (the passive level sits well above the probe's), so only relative steps compare.
        before = [(T0 - timedelta(days=d), 0.30) for d in (4, 3, 2, 1)]
        res = check({"jwork": series(*STEADY, *SHIFTED)}, probe_readings=before + [(T0 + timedelta(hours=8), 0.22)])
        self.assertEqual([c["corroborated_by"] for c in res.changes], [["probe"]])


class NoReferenceTests(unittest.TestCase):
    """A run whose stretches had nothing to be judged against (issue #52, masterrig data).

    Where an account's own bootstrap stretches spent nothing -- masterrig's meter moves on
    web, phone and other machines this host never sees -- the reference is zero, so no
    stretch judged against it has a capture, and the run's median had no data:
    `statistics.StatisticsError: no median for empty data`. The run is `unknown` now.
    """

    def _runs(self):
        # Five stretches the transcripts leave empty, then five real ones. The median of
        # the empty bootstrap is 0, and every later stretch reads as surplus against it.
        empty = [st(i, 0.0) for i in range(BOOTSTRAP)]
        for s in empty:
            s.tokens = {}
        return runs(judge(empty + [st(BOOTSTRAP + i, 1.0) for i in range(5)]), "masterrig")

    def test_the_run_is_unknown_and_neither_capture_nor_step_is_invented(self):
        rs = self._runs()
        self.assertEqual([r.kind for r in rs], [UNKNOWN])
        self.assertIsNone(rs[0].capture)
        self.assertIsNone(rs[0].step)

    def test_an_unknown_run_corroborates_nothing_and_a_missing_step_agrees_with_nothing(self):
        run = self._runs()[0]
        self.assertFalse(_agrees(None, run))     # nothing measured on the other side
        self.assertFalse(_agrees(-0.30, run))    # this run has no step of its own to match
        res = check({"masterrig": [st(i, 0.0) for i in range(BOOTSTRAP)] + [st(BOOTSTRAP + i, 1.0) for i in range(5)]})
        self.assertEqual(res.changes, [])

    def test_a_zero_reference_among_real_ones_still_gives_a_step(self):
        # median() of the non-zero references, not of all of them: one zero reference in a
        # run does not erase the level the rest were judged against.
        run = Run([Verdict(st(0, 0.7), UNACCOUNTED, 1.0), Verdict(st(1, 0.7), UNACCOUNTED, 0.0),
                   Verdict(st(2, 0.7), UNACCOUNTED, 1.0)], "x")
        self.assertAlmostEqual(run.step, -0.3)
