"""Tests for tracker/contributed.py: fetch, merge and the per-plan aggregate.

Nothing here opens a socket: fetch takes a fake urlopen. Fixtures are three
contributors on two plans (two on max20, one on pro) with two samples each
inside one five-hour and one seven-day window, so the weekly pairing has
something to pair. Every accepted sample is its own point and its own vote
in the tokens_per_pct/usd_per_pct medians (the pre-2026-09-16-audit rule,
restored 2026-09-17 by decision, reversing that audit's finding 7).
"""
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker.contributed import _weighted_median, aggregate, fetch, main, merge

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
# Both windows are already closed at NOW, so every paired week is complete.
FIVE_RESET = "2026-09-02T14:00:00+00:00"
SEVEN_RESET = "2026-09-05T10:00:00+00:00"
A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"
PRICES = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5,
                              "meter_weight": 1.0, "class_weight": {"output": 1.8}},
          "claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25,
                            "meter_weight": 1.0, "class_weight": {"output": 1.8}}}


def sample(cid, plan, ts, five, seven, tokens=None, five_reset=FIVE_RESET, seven_reset=SEVEN_RESET, capture=True):
    r = {"contributor_id": cid, "plan": plan, "plan_source": "flag", "ts": ts,
         "five_hour": {"utilization": five, "resets_at": five_reset},
         "seven_day": {"utilization": seven, "resets_at": seven_reset},
         "tokens_since_five_hour_reset": tokens or {}, "tokens_since_seven_day_reset": {},
         "client_version": "contrib-sample/0.2.0", "received_at": ts}
    if capture:
        # contrib/sample.py 0.2.0's capture block; a reading without it (0.1.0) is legacy.
        r["capture"] = {"collected_at": ts,
                        "five_hour_started_at": (datetime.fromisoformat(five_reset) - timedelta(hours=5)).isoformat(),
                        "seven_day_started_at": (datetime.fromisoformat(seven_reset) - timedelta(days=7)).isoformat(),
                        "ownership": "local_transcripts_unverified"}
    return r


def week_sample(cid, plan, ts, five, seven, five_tokens, seven_tokens, **kw):
    """A sample that also carries its tokens since the seven-day reset."""
    r = sample(cid, plan, ts, five, seven, five_tokens, **kw)
    r["tokens_since_seven_day_reset"] = seven_tokens
    return r


def sonnet(cache_write, output=0):
    return {"claude-sonnet-5": {"input": 0, "output": output, "cache_read": 0, "cache_write": cache_write}}


def fixture_rows():
    """Three contributors, two plans. Each pair sits in the same windows and moves
    the five-hour meter by 60 and the seven-day meter by 2 (A, B) or 3 (C)."""
    return [
        # A on max20: the span spends 600k over 60 points, 10000 per 1%
        sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0, sonnet(200_000)),
        sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0, sonnet(800_000)),
        # B on max20: 300k over 60 points, 5000 per 1%
        sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 30.0, sonnet(300_000)),
        sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 32.0, sonnet(600_000)),
        # C on pro, alone
        sample(C, "pro", "2026-09-02T10:00:00Z", 10.0, 50.0, sonnet(50_000)),
        sample(C, "pro", "2026-09-02T12:00:00Z", 70.0, 53.0, sonnet(350_000)),
    ]


class FixtureShapeTests(unittest.TestCase):
    def test_counts_per_plan_and_updated_at(self):
        j = aggregate(fixture_rows(), NOW, PRICES)
        self.assertEqual(j["updated_at"], "2026-09-09T12:00:00+00:00")
        self.assertEqual((j["max20"]["contributors"], j["max20"]["samples"]), (2, 4))
        self.assertEqual((j["pro"]["contributors"], j["pro"]["samples"]), (1, 2))
        self.assertEqual((j["max5"]["contributors"], j["max5"]["samples"]), (0, 0))
        self.assertEqual(j["max5"]["tokens_per_pct"], {})
        self.assertEqual(j["max5"]["weekly_windows"]["measured"], None)
        self.assertEqual(j["max5"]["weekly_windows"]["reason"], "no samples")

    def test_duplicate_rows_count_once(self):
        rows = fixture_rows()
        j = aggregate(rows + rows, NOW, PRICES)
        self.assertEqual(j["max20"]["samples"], 4)

    def test_unknown_plan_and_broken_meter_are_ignored(self):
        rows = fixture_rows()
        rows.append(sample(A, "team", "2026-09-02T13:00:00Z", 90.0, 13.0))
        broken = sample(B, "max20", "2026-09-02T13:00:00Z", None, 33.0)
        rows.append(broken)
        j = aggregate(rows, NOW, PRICES)
        self.assertEqual(j["max20"]["samples"], 4)
        self.assertNotIn("team", j)

    def test_old_history_is_excluded_and_freshness_explains_the_cohort(self):
        old = sample(A, "max20", "2026-07-01T10:00:00Z", 80.0, 10.0, sonnet(9_000_000))
        j = aggregate(fixture_rows() + [old], NOW, PRICES)["max20"]
        self.assertEqual(j["samples"], 4)
        self.assertEqual(j["evidence"]["ignored_old_samples"], 1)
        self.assertFalse(j["evidence"]["stale"])


class TokensPerPctTests(unittest.TestCase):
    # Restored pre-2026-09-16-audit rule (finding 7 reversed by decision): every
    # accepted sample -- not just a source's newest paired span -- is its own
    # vote in these medians, each contributor first reduced to the median of
    # their own samples.

    def test_median_across_contributors_of_each_contributors_median(self):
        j = aggregate(fixture_rows(), NOW, PRICES)
        s = j["max20"]["tokens_per_pct"]["claude-sonnet-5"]
        # A: 200k/20 = 10000 and 800k/80 = 10000 -> 10000. B: 300k/20 = 15000 and 600k/80 = 7500 -> 11250.
        self.assertEqual(s["median"], round((10000 + 11250) / 2))
        self.assertEqual((s["contributors"], s["samples"]), (2, 4))

    def test_a_single_accepted_sample_produces_a_point_and_a_usd_per_pct_median(self):
        rows = [sample(A, "max20", "2026-09-09T10:00:00Z", 50.0, 10.0, sonnet(400_000))]
        j = aggregate(rows, NOW, PRICES)["max20"]
        # 400k cache_write @ $2.5/M = $1.00 over utilization 50.
        self.assertEqual(j["usd_per_pct"], {"median": 0.02, "spread": None, "contributors": 1, "samples": 1})
        self.assertEqual(j["tokens_per_pct"]["claude-sonnet-5"]["median"], 8000)
        self.assertEqual(len(j["points"]), 1)
        self.assertEqual(j["points"][0]["usd_per_pct"], 0.02)
        self.assertEqual(j["missing_reasons"], [])

    def test_a_coarse_only_sample_is_a_point_but_the_cost_figure_is_still_missing(self):
        # Utilization 3% is under MIN_UTILIZATION: the reading is drawn as a coarse point,
        # votes in no median, and missing_reasons says why the cost figure is absent.
        rows = [sample(A, "max20", "2026-09-09T10:00:00Z", 3.0, 10.0, sonnet(400_000))]
        j = aggregate(rows, NOW, PRICES)["max20"]
        self.assertEqual(len(j["points"]), 1)
        self.assertTrue(j["points"][0]["coarse"])
        self.assertIsNone(j["usd_per_pct"])
        self.assertEqual(j["tokens_per_pct"], {})
        self.assertEqual(j["missing_reasons"],
                         ["no current sample cleared the utilization floor with priced tokens"])

    def test_usd_per_pct_values_tokens_as_publish_does(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(400_000, output=100_000)),
                sample(B, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(400_000, output=100_000))]
        j = aggregate(rows, NOW, PRICES)
        # 400k cache_write at $2.5/M = $1.00, 100k output at $10/M x class_weight 1.8 = $1.80: $2.80 over 50%.
        self.assertAlmostEqual(j["max20"]["usd_per_pct"]["median"], 2.80 / 50, places=4)
        self.assertEqual(j["max20"]["tokens_per_pct"]["claude-sonnet-5"]["median"], 500_000 / 50)

    def test_usd_per_pct_applies_meter_weight_and_a_sample_with_an_unpriced_model_is_dropped(self):
        prices = {"claude-sonnet-5": {**PRICES["claude-sonnet-5"], "meter_weight": 2.0}}
        tokens = {**sonnet(400_000), "claude-mystery-9": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 1000}}
        j = aggregate([sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, tokens)], NOW, prices)
        # claude-mystery-9 has no price, so the whole sample drops for both figures.
        self.assertIsNone(j["max20"]["usd_per_pct"])
        self.assertEqual(j["max20"]["tokens_per_pct"], {})
        # The same sample without it: $1.00 x meter_weight 2 over 50%.
        rows = [sample(B, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(400_000))]
        usd = aggregate(rows, NOW, prices)["max20"]["usd_per_pct"]
        self.assertEqual((usd["median"], usd["contributors"]), (0.04, 1))

    def test_two_model_sample_gives_one_combined_dollar_figure_and_share_attributed_tokens(self):
        # Sonnet: 400k cache_write @ $2.5/M = $1.00. Opus: 200k output @ $25/M x class_weight 1.8 = $9.00.
        # Combined value $10.00 over utilization 40 -> usd_per_pct 0.25.
        # Sonnet's share of the meter: 40 x 1.00/10.00 = 4 -> under the 5-point floor, so no Sonnet figure.
        # Opus's share: 40 x 9.00/10.00 = 36 -> tokens_per_pct 200_000/36.
        tokens = {**sonnet(400_000), "claude-opus-5": {"input": 0, "output": 200_000, "cache_read": 0, "cache_write": 0}}
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 40.0, 10.0, tokens)]
        j = aggregate(rows, NOW, PRICES)
        self.assertAlmostEqual(j["max20"]["usd_per_pct"]["median"], 10.0 / 40, places=4)
        self.assertEqual(j["max20"]["tokens_per_pct"]["claude-opus-5"]["median"], round(200_000 / 36))
        self.assertNotIn("claude-sonnet-5", j["max20"]["tokens_per_pct"])
        doubled = {m: {c: 2 * n for c, n in counts.items()} for m, counts in tokens.items()}
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 80.0, 10.0, doubled)]
        self.assertEqual(aggregate(rows, NOW, PRICES)["max20"]["tokens_per_pct"]["claude-sonnet-5"]["median"], 100_000)

    def test_spread_is_iqr_over_median_and_null_for_one_contributor(self):
        rows = [sample(cid, "max20", "2026-09-02T10:00:00Z", 10.0, 10.0, sonnet(t))
                for cid, t in ((A, 100_000), (B, 200_000), (C, 400_000))]
        j = aggregate(rows, NOW, PRICES)
        s = j["max20"]["tokens_per_pct"]["claude-sonnet-5"]
        # per-contributor values 10000, 20000, 40000: median 20000, inclusive quartiles 15000 and 30000.
        self.assertEqual(s["median"], 20000)
        self.assertAlmostEqual(s["spread"], 15000 / 20000, places=3)
        solo = aggregate(rows[:1], NOW, PRICES)["max20"]["tokens_per_pct"]["claude-sonnet-5"]
        self.assertIsNone(solo["spread"])
        self.assertEqual(solo["contributors"], 1)

    def test_utilization_floor_drops_samples_under_five_percent(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 4.9, 10.0, sonnet(1_000_000)),   # would be 204081/pct
                sample(A, "max20", "2026-09-02T10:30:00Z", 5.0, 10.0, sonnet(50_000)),
                sample(B, "max20", "2026-09-02T10:00:00Z", 0.0, 10.0, sonnet(1_000_000))]
        j = aggregate(rows, NOW, PRICES)
        s = j["max20"]["tokens_per_pct"]["claude-sonnet-5"]
        self.assertEqual(s["median"], 10000)
        self.assertEqual((s["contributors"], s["samples"]), (1, 1))
        self.assertEqual(j["max20"]["samples"], 3)  # the floor is for the rate, not the sample count

    def test_zero_token_models_do_not_count(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0,
                       {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}})]
        self.assertEqual(aggregate(rows, NOW, PRICES)["max20"]["tokens_per_pct"], {})

    def test_every_accepted_reading_votes_including_older_overlapping_ones(self):
        # Restored rule: each cumulative reading is its own accepted sample and its own
        # vote, not just a source's newest span (this is the behaviour the 2026-09-16
        # audit's finding 7 replaced; reversed here by decision).
        rows, spent, pct = [], 0, 0.0
        for hour in range(1, 12):
            spent += 2_000_000 if hour <= 10 else 1_000_000
            pct += 5.0
            rows.append(sample(A, "max20", f"2026-09-02T{hour:02d}:00:00Z", pct, 10.0, sonnet(spent)))
        j = aggregate(rows, NOW, PRICES)["max20"]
        self.assertEqual(j["usd_per_pct"]["samples"], 11)
        self.assertEqual(len(j["points"]), 11)

    def test_a_sample_without_capture_metadata_still_counts_but_is_flagged(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 12.0, sonnet(400_000), capture=False)]
        j = aggregate(rows, NOW, PRICES)["max20"]
        self.assertIsNotNone(j["usd_per_pct"])
        self.assertEqual(len(j["points"]), 1)
        self.assertEqual(j["evidence"]["samples_without_capture"], 1)


class WeeklyWindowsTests(unittest.TestCase):
    # Each source publishes its own paired estimate; no plan figure is pooled from
    # unverified sources, weighted by weeks, or cut down by dropping a source that
    # disagrees (audit 2026-09-16, findings 7 and 14).

    def test_pairs_two_samples_per_contributor_and_publishes_each_as_its_own_estimate(self):
        j = aggregate(fixture_rows(), NOW, PRICES)
        w = j["max20"]["weekly_windows"]
        # A: 60 five-hour points over 2 seven-day points = 30; B: 60/2 = 30.
        self.assertIsNone(w["measured"])
        self.assertEqual([(e["value"], e["through"], e["partial"]) for e in w["estimates"]],
                         [(30.0, "2026-09-05", False)] * 2)
        self.assertTrue(all(e["interval"][0] < 30.0 < e["interval"][1] for e in w["estimates"]))
        self.assertEqual((w["contributors"], w["with_complete_week"], w["dropped"], w["weeks"]), (2, 2, 0, 2))
        self.assertEqual(w["reason"], "2 contributors, 2 with a complete week; per-source estimates only, "
                                      "never pooled into a plan figure")

    def test_one_contributor_is_not_a_measurement(self):
        w = aggregate(fixture_rows(), NOW, PRICES)["pro"]["weekly_windows"]
        self.assertIsNone(w["measured"])
        self.assertEqual(w["contributors"], 1)
        self.assertEqual(w["with_complete_week"], 1)
        self.assertEqual([e["value"] for e in w["estimates"]], [20.0])
        self.assertIn("never pooled", w["reason"])

    def test_one_contributor_none_complete_gives_the_singular_reason(self):
        rows = [sample(A, "pro", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "pro", "2026-09-02T16:00:00Z", 80.0, 12.0, five_reset="2026-09-02T19:00:00+00:00")]
        w = aggregate(rows, NOW, PRICES)["pro"]["weekly_windows"]
        self.assertIsNone(w["measured"])
        self.assertEqual(w["contributors"], 1)
        self.assertEqual(w["with_complete_week"], 0)
        self.assertEqual(w["estimates"], [])
        self.assertEqual(w["reason"], "1 contributor, none with a complete week yet; "
                                      "per-source estimates only, never pooled into a plan figure")

    def test_an_open_week_does_not_count_towards_the_gate(self):
        still_open = (NOW + timedelta(days=2)).isoformat()
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0, seven_reset=still_open),
                sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0, seven_reset=still_open),
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0)]
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertIsNone(w["measured"])
        # A and B are both contributors on the plan; only B has a closed week, and A's
        # open one is published flagged partial.
        self.assertEqual(w["contributors"], 2)
        self.assertEqual(w["with_complete_week"], 1)
        self.assertEqual(sorted(e["partial"] for e in w["estimates"]), [False, True])

    def test_samples_in_different_windows_do_not_pair(self):
        other_reset = "2026-09-02T19:00:00+00:00"
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "max20", "2026-09-02T16:00:00Z", 80.0, 12.0, five_reset=other_reset),
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0)]
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertEqual(w["contributors"], 2)
        self.assertEqual(w["with_complete_week"], 1)
        self.assertEqual(len(w["estimates"]), 1)
        self.assertIsNone(w["measured"])

    def test_a_contributor_far_from_the_others_is_kept_not_dropped(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0),   # 30
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0),   # 30
                sample(C, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(C, "max20", "2026-09-02T12:00:00Z", 80.0, 20.0)]   # 6: a distinct cohort, kept
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertEqual(sorted(e["value"] for e in w["estimates"]), [6.0, 30.0, 30.0])
        self.assertEqual((w["contributors"], w["dropped"], w["weeks"]), (3, 0, 3))

    def test_two_contributors_far_apart_give_no_figure(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0),   # 30
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 20.0)]   # 6
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertIsNone(w["measured"])
        self.assertEqual(w["dropped"], 0)
        self.assertEqual(sorted(e["value"] for e in w["estimates"]), [6.0, 30.0])

    def test_each_source_publishes_its_newest_week_not_a_weighted_median(self):
        # A has two complete weeks at 20, B one week at 24: each publishes its newest
        # week; nothing is weighted by how many weeks a source has.
        week1 = ("2026-08-26T14:00:00+00:00", "2026-08-29T10:00:00+00:00")
        rows = [sample(A, "max20", "2026-08-26T10:00:00Z", 20.0, 10.0, five_reset=week1[0], seven_reset=week1[1]),
                sample(A, "max20", "2026-08-26T12:00:00Z", 80.0, 13.0, five_reset=week1[0], seven_reset=week1[1]),
                sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 13.0),
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 12.5)]
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertIsNone(w["measured"])
        self.assertEqual([(e["value"], e["through"]) for e in w["estimates"]], [(20.0, "2026-09-05"), (24.0, "2026-09-05")])
        self.assertEqual((w["contributors"], w["dropped"], w["weeks"]), (2, 0, 3))

    def test_a_changing_token_mix_cannot_make_a_hundred_window_week(self):
        # Audit finding 7: $100 of meter work per window and $600 per week, a true ratio of
        # six. The window's 1M output tokens move the five-hour meter 10 points; the week's
        # map also holds 94M cache reads. Raw tokens per point then read 100 windows a
        # week. The weekly figure comes from paired meter movement only.
        window = {"claude-sonnet-5": {"input": 0, "output": 1_000_000, "cache_read": 0, "cache_write": 0}}
        week = {"claude-sonnet-5": {"input": 0, "output": 6_000_000, "cache_read": 94_000_000, "cache_write": 0}}
        rows = [week_sample(A, "max20", "2026-09-02T09:00:00Z", 0.0, 20.0, {}, {}),
                week_sample(A, "max20", "2026-09-02T12:00:00Z", 60.0, 30.0, window, week)]
        j = aggregate(rows, NOW, PRICES)["max20"]
        self.assertEqual([e["value"] for e in j["weekly_windows"]["estimates"]], [6.0])
        self.assertTrue(all("windows" not in p for p in j["points"]))

    def test_weighted_median_reduces_to_median_with_equal_weights(self):
        self.assertEqual(_weighted_median([(1, 1), (2, 1), (3, 1), (4, 1)]), 2.5)
        self.assertEqual(_weighted_median([(3, 1), (1, 1), (2, 1)]), 2)
        self.assertEqual(_weighted_median([(10, 3), (30, 1)]), 10)


class PointsTests(unittest.TestCase):
    # One point per accepted sample (see TokensPerPctTests).

    def test_pseudonym_is_stable_and_never_the_contributor_id(self):
        rows = [sample(A, "max20", "2026-09-02T14:00:00Z", 50.0, 10.0, sonnet(100_000)),
                sample(B, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(100_000))]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        by_t = {p["t"]: p["c"] for p in pts}
        self.assertNotEqual(by_t["2026-09-02T10:00:00Z"], by_t["2026-09-02T14:00:00Z"])
        again = {p["t"]: p["c"] for p in aggregate(list(reversed(rows)), NOW, PRICES)["max20"]["points"]}
        self.assertEqual(by_t, again)
        self.assertNotIn(A, json.dumps(pts))
        self.assertNotIn(B, json.dumps(pts))

    def test_coarse_sample_is_included_with_a_usd_figure(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 4.0, 10.0, sonnet(100_000))]
        j = aggregate(rows, NOW, PRICES)["max20"]
        pts = j["points"]
        self.assertEqual(len(pts), 1)
        self.assertTrue(pts[0]["coarse"])
        # 100k cache_write @ $2.5/M = $0.25, over utilization 4.
        self.assertAlmostEqual(pts[0]["usd_per_pct"], 0.25 / 4, places=4)
        # Under the utilization floor, so it does not vote in the summary either.
        self.assertIsNone(j["usd_per_pct"])

    def test_unpriced_sample_gives_null_usd_per_pct(self):
        tokens = {"claude-mystery-9": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 1000}}
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, tokens)]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        self.assertEqual(len(pts), 1)
        self.assertIsNone(pts[0]["usd_per_pct"])
        self.assertFalse(pts[0]["coarse"])

    def test_unknown_model_work_is_kept_and_withholds_every_monetary_and_per_model_figure(self):
        # A model the price table does not know still moved the meter. Its tokens stay
        # in the sample's count, and nothing that needs its price is published: no
        # dollars, no per-model split, and never the combined total under a model's name.
        tokens = {**sonnet(400_000), "claude-haiku-4-5": {"input": 0, "output": 40_000, "cache_read": 0, "cache_write": 0}}
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 44.0, 10.0, tokens)]
        j = aggregate(rows, NOW, PRICES)["max20"]
        point = j["points"][0]
        self.assertEqual(point["tokens_per_pct"], round(440_000 / 44))
        self.assertIsNone(point["usd_per_pct"])
        self.assertNotIn("tokens_per_pct_by_model", point)
        self.assertEqual((j["usd_per_pct"], j["tokens_per_pct"]), (None, {}))

    def test_hourly_thinning_keeps_the_latest_sample_in_the_hour(self):
        rows = [sample(A, "max20", "2026-09-02T10:05:00Z", 50.0, 10.0, sonnet(100_000)),
                sample(A, "max20", "2026-09-02T10:45:00Z", 60.0, 10.0, sonnet(200_000))]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]["t"], "2026-09-02T10:45:00Z")

    def test_sample_older_than_31_days_is_excluded(self):
        old_ts = (NOW - timedelta(days=31)).isoformat().replace("+00:00", "Z")
        rows = [sample(A, "max20", old_ts, 50.0, 10.0, sonnet(100_000)),
                sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(100_000))]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]["t"], "2026-09-02T10:00:00Z")

    def test_point_never_claims_weekly_windows_from_two_token_mixes(self):
        # 100k tokens moved the five-hour meter 50% (2,000 per 1%) and the seven-day meter
        # 10% (10,000 per 1%). Their quotient is not published as windows per week.
        rows = [week_sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(100_000), sonnet(100_000))]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        self.assertEqual(pts[0]["tokens_per_pct"], 2000)
        self.assertEqual(pts[0]["tokens_per_pct_week"], 10_000)
        self.assertNotIn("windows", pts[0])

    def test_a_meter_under_the_floor_nulls_its_own_side_and_the_quotient(self):
        rows = [week_sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 1.0, sonnet(100_000), sonnet(100_000)),
                week_sample(B, "max20", "2026-09-02T11:00:00Z", 1.0, 10.0, sonnet(100_000), sonnet(100_000))]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        self.assertTrue(all("windows" not in p for p in pts))
        self.assertEqual([p["tokens_per_pct_week"] for p in pts], [None, 10_000])
        self.assertEqual([p["tokens_per_pct"] for p in pts], [2000, None])

    def test_no_seven_day_tokens_leaves_the_weekly_figures_null(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(100_000))]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        self.assertEqual(pts[0]["tokens_per_pct"], 2000)
        self.assertIsNone(pts[0]["tokens_per_pct_week"])
        self.assertNotIn("windows", pts[0])

    def test_by_model_splits_the_meter_by_dollar_share(self):
        tokens = {"claude-sonnet-5": {"input": 0, "output": 100_000, "cache_read": 0, "cache_write": 0},
                  "claude-opus-5": {"input": 0, "output": 100_000, "cache_read": 0, "cache_write": 0}}
        rows = [week_sample(A, "max20", "2026-09-02T10:00:00Z", 60.0, 10.0, tokens, tokens)]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        # Opus output is priced 2.5x Sonnet's, so it is charged 5/7 of the 60% meter and
        # Sonnet 2/7. Same tokens, so Sonnet reads 2.5x more per 1%.
        by_model = pts[0]["tokens_per_pct_by_model"]
        self.assertAlmostEqual(by_model["claude-sonnet-5"] / by_model["claude-opus-5"], 2.5, places=2)

    def test_a_model_whose_slice_is_under_the_floor_is_left_out(self):
        # Sonnet here is almost all cache_read, which the meter does not charge, so its
        # slice collapses -- exactly the case that made one week read three times the plan.
        prices = {m: {**p, "class_weight": {**p["class_weight"], "cache_read": 0.0}}
                  for m, p in PRICES.items()}
        tokens = {"claude-opus-5": {"input": 0, "output": 1_000_000, "cache_read": 0, "cache_write": 0},
                  "claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 50_000_000, "cache_write": 0}}
        rows = [week_sample(A, "max20", "2026-09-02T10:00:00Z", 60.0, 10.0, tokens, tokens)]
        pts = aggregate(rows, NOW, prices)["max20"]["points"]
        self.assertEqual(list(pts[0]["tokens_per_pct_by_model"]), ["claude-opus-5"])

    def test_an_unpriced_model_drops_the_split_whole(self):
        tokens = {"claude-sonnet-5": {"input": 0, "output": 100_000, "cache_read": 0, "cache_write": 0},
                  "claude-mystery-9": {"input": 0, "output": 100_000, "cache_read": 0, "cache_write": 0}}
        rows = [week_sample(A, "max20", "2026-09-02T10:00:00Z", 60.0, 10.0, tokens, tokens)]
        pts = aggregate(rows, NOW, PRICES)["max20"]["points"]
        self.assertNotIn("tokens_per_pct_by_model", pts[0])
        self.assertNotIn("tokens_per_pct_week_by_model", pts[0])

    def test_points_are_sorted_by_t_ascending(self):
        pts = aggregate(fixture_rows(), NOW, PRICES)["max20"]["points"]
        self.assertEqual([p["t"] for p in pts], sorted(p["t"] for p in pts))


class MergeTests(unittest.TestCase):
    def test_appends_new_rows_and_dedupes_by_contributor_and_ts(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "history" / "contributed.jsonl"
            rows = fixture_rows()
            self.assertEqual(merge(p, rows[:3]), 3)
            self.assertEqual(merge(p, rows), 3)
            self.assertEqual(merge(p, rows), 0)
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 6)
            keys = {(json.loads(line)["contributor_id"], json.loads(line)["ts"]) for line in lines}
            self.assertEqual(len(keys), 6)

    def test_a_changed_copy_of_an_existing_row_is_not_appended(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "contributed.jsonl"
            rows = fixture_rows()[:1]
            merge(p, rows)
            changed = dict(rows[0], five_hour={"utilization": 99.0, "resets_at": FIVE_RESET})
            self.assertEqual(merge(p, [changed]), 0)
            self.assertEqual(json.loads(p.read_text())["five_hour"]["utilization"], 20.0)

    def test_never_rewrites_or_drops_existing_lines(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "contributed.jsonl"
            p.write_text('not json at all\n{"contributor_id": "x", "ts": "t", "plan": "pro"}')  # no trailing newline
            before = p.read_text()
            self.assertEqual(merge(p, fixture_rows()[:2]), 2)
            after = p.read_text()
            self.assertTrue(after.startswith(before + "\n"))
            self.assertEqual(len(after.splitlines()), 4)

    def test_rows_without_a_key_are_ignored(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "contributed.jsonl"
            self.assertEqual(merge(p, [{"plan": "pro"}, {"contributor_id": A}]), 0)
            self.assertFalse(p.exists())


class _Response:
    def __init__(self, lines, header=""):
        self._body = "".join(json.dumps(line) + "\n" for line in lines).encode()
        self.headers = {"x-next-cursor": header}
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetchTests(unittest.TestCase):
    def test_follows_the_cursor_to_the_end(self):
        rows = fixture_rows()
        calls = []

        def urlopen(req, timeout):
            calls.append((req.full_url, req.get_header("Authorization"), timeout))
            if "cursor=" not in req.full_url:
                return _Response(rows[:2] + [{"next_cursor": "c/1"}], header="c/1")
            if req.full_url.endswith("cursor=c%2F1"):
                return _Response(rows[2:5] + [{"next_cursor": "c2"}], header="c2")
            return _Response(rows[5:], header="")

        got = fetch("https://example.test/api/contribute/export", "s3cret", urlopen=urlopen)
        self.assertEqual(got, rows)
        self.assertEqual([c[0] for c in calls], ["https://example.test/api/contribute/export",
                                                 "https://example.test/api/contribute/export?cursor=c%2F1",
                                                 "https://example.test/api/contribute/export?cursor=c2"])
        self.assertTrue(all(c[1] == "Bearer s3cret" and c[2] == 20 for c in calls))

    def test_cursor_line_alone_is_enough(self):
        rows = fixture_rows()

        def urlopen(req, timeout):
            if "cursor=" not in req.full_url:
                return _Response(rows[:3] + [{"next_cursor": "z"}])
            return _Response(rows[3:])

        self.assertEqual(fetch("https://example.test/x?limit=5", "s", urlopen=urlopen), rows)

    def test_http_error_returns_what_was_fetched_and_warns_once(self):
        rows = fixture_rows()

        def urlopen(req, timeout):
            if "cursor=" not in req.full_url:
                return _Response(rows[:4] + [{"next_cursor": "n"}], header="n")
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

        err = io.StringIO()
        from unittest import mock
        with mock.patch("sys.stderr", err):
            got = fetch("https://example.test/x", "s", urlopen=urlopen)
        self.assertEqual(got, rows[:4])
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("HTTP 500", err.getvalue())
        self.assertNotIn("s3cret", err.getvalue())

    def test_network_error_on_the_first_page_returns_nothing(self):
        def urlopen(req, timeout):
            raise urllib.error.URLError("no route")

        err = io.StringIO()
        from unittest import mock
        with mock.patch("sys.stderr", err):
            self.assertEqual(fetch("https://example.test/x", "s", urlopen=urlopen), [])
        self.assertIn("no route", err.getvalue())

    def test_a_repeated_cursor_ends_the_loop(self):
        def urlopen(req, timeout):
            return _Response([fixture_rows()[0]], header="same")

        self.assertEqual(len(fetch("https://example.test/x", "s", urlopen=urlopen)), 2)


class MainTests(unittest.TestCase):
    def _dir(self, t):
        d = Path(t)
        (d / "prices.json").write_text(json.dumps({"_source": "x", **PRICES}))
        (d / "env").write_text("# NOTIFY_SEND_SECRET in a comment\nNOTIFY_SEND_SECRET=topsecret\n")
        return d

    def test_fetches_merges_aggregates_and_writes(self):
        rows = fixture_rows()
        seen = []

        def urlopen(req, timeout):
            seen.append(req.get_header("Authorization"))
            return _Response(rows)

        with tempfile.TemporaryDirectory() as t:
            d = self._dir(t)
            rc = main(["--env-file", str(d / "env"), "--endpoint", "https://example.test/x",
                       "--history", str(d / "h.jsonl"), "--prices", str(d / "prices.json"),
                       "--out", str(d / "out.json")], urlopen=urlopen, environ={}, now=NOW)
            self.assertEqual(rc, 0)
            self.assertEqual(seen, ["Bearer topsecret"])
            self.assertEqual(len((d / "h.jsonl").read_text().splitlines()), 6)
            out = json.loads((d / "out.json").read_text())
            self.assertEqual([e["value"] for e in out["max20"]["weekly_windows"]["estimates"]], [30.0, 30.0])
            self.assertIsNone(out["max20"]["weekly_windows"]["measured"])
            self.assertEqual(out["updated_at"], "2026-09-09T12:00:00+00:00")

    def test_offline_aggregates_what_is_on_disk_without_a_secret(self):
        def urlopen(req, timeout):
            raise AssertionError("offline must not fetch")

        with tempfile.TemporaryDirectory() as t:
            d = self._dir(t)
            merge(d / "h.jsonl", fixture_rows())
            rc = main(["--offline", "--env-file", str(d / "missing"), "--history", str(d / "h.jsonl"),
                       "--prices", str(d / "prices.json"), "--out", str(d / "out.json")],
                      urlopen=urlopen, environ={}, now=NOW)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads((d / "out.json").read_text())["max20"]["samples"], 4)

    def test_missing_secret_warns_and_still_writes(self):
        with tempfile.TemporaryDirectory() as t:
            d = self._dir(t)
            err = io.StringIO()
            from unittest import mock
            with mock.patch("sys.stderr", err):
                rc = main(["--env-file", str(d / "missing"), "--history", str(d / "h.jsonl"),
                           "--prices", str(d / "prices.json"), "--out", str(d / "out.json")],
                          urlopen=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")),
                          environ={}, now=NOW)
            self.assertEqual(rc, 0)
            self.assertIn("NOTIFY_SEND_SECRET", err.getvalue())
            self.assertEqual(json.loads((d / "out.json").read_text())["max20"]["samples"], 0)

    def test_fetch_failure_keeps_the_previous_rows_and_exits_zero(self):
        def urlopen(req, timeout):
            raise urllib.error.URLError("down")

        with tempfile.TemporaryDirectory() as t:
            d = self._dir(t)
            merge(d / "h.jsonl", fixture_rows())
            err = io.StringIO()
            from unittest import mock
            with mock.patch("sys.stderr", err):
                rc = main(["--env-file", str(d / "env"), "--history", str(d / "h.jsonl"),
                           "--prices", str(d / "prices.json"), "--out", str(d / "out.json")],
                          urlopen=urlopen, environ={}, now=NOW)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads((d / "out.json").read_text())["max20"]["samples"], 4)

    def test_unreadable_prices_exits_one_and_leaves_output(self):
        with tempfile.TemporaryDirectory() as t:
            d = self._dir(t)
            (d / "prices.json").write_text("nope")
            (d / "out.json").write_text('{"previous": true}')
            err = io.StringIO()
            from unittest import mock
            with mock.patch("sys.stderr", err):
                rc = main(["--offline", "--history", str(d / "h.jsonl"), "--prices", str(d / "prices.json"),
                           "--out", str(d / "out.json")], environ={}, now=NOW)
            self.assertEqual(rc, 1)
            self.assertEqual((d / "out.json").read_text(), '{"previous": true}')


if __name__ == "__main__":
    unittest.main()
