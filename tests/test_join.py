import json
import unittest
from datetime import date, datetime, timedelta, timezone

from tracker.join import (
    Interval,
    Stretch,
    build_intervals,
    build_stretches,
    bundle_meter_usd,
    daily_rates,
    turn_meter_usd,
    window_points,
)
from tracker.publish import usd_per_pct
from tracker.samples import Sample, parse_moonlighter
from tracker.turns import Turn
from tracker.weekly import weekly_windows

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
def S(mins, fh, reset="r1"): return Sample(T0 + timedelta(minutes=mins), fh, None, reset, "ceiling")
def T(mins, model="claude-sonnet-5", cr=100_000, inp=1000, out=500, cw=0):
    return Turn(T0 + timedelta(minutes=mins), model, inp, out, cr, cw)

class IntervalTests(unittest.TestCase):
    def test_basic_interval_and_attribution(self):
        iv = build_intervals([S(0, 10), S(5, 12)], [T(1), T(2), T(3)])
        self.assertEqual(len(iv), 1)
        self.assertEqual(iv[0].delta_pct, 2.0)
        self.assertEqual(iv[0].tokens["cache_read"], 300_000)
        self.assertEqual(iv[0].model, "claude-sonnet-5")

    def test_mixed_interval_has_no_model(self):
        iv = build_intervals([S(0, 10), S(5, 12)], [T(1), T(2, model="claude-opus-5")])
        self.assertIsNone(iv[0].model)

    def test_zero_delta_pooled_into_next(self):
        iv = build_intervals([S(0, 10), S(5, 10), S(10, 11)], [T(1), T(7)])
        self.assertEqual(len(iv), 1)
        self.assertEqual((iv[0].start, iv[0].end, iv[0].delta_pct), (S(0, 10).ts, S(10, 11).ts, 1.0))
        self.assertEqual(iv[0].tokens["cache_read"], 200_000)

    def test_reset_straddle_dropped(self):
        iv = build_intervals([S(0, 90), S(5, 3, reset="r2"), S(10, 5, reset="r2")], [T(1), T(7)])
        self.assertEqual(len(iv), 1)
        self.assertEqual(iv[0].start, S(5, 3).ts)

    def test_sub_minute_resets_at_jitter_is_not_a_reset(self):
        a = "2026-07-01T02:59:59.627759+00:00"
        b = "2026-07-01T03:00:00.496943+00:00"
        iv = build_intervals([S(0, 3, reset=a), S(30, 4, reset=b)], [T(1)])
        self.assertEqual(len(iv), 1)

    def test_utilization_drop_without_reset_field_is_a_reset(self):
        iv = build_intervals([S(0, 90, reset=None), S(5, 3, reset=None)], [T(1)])
        self.assertEqual(iv, [])

class DailyTests(unittest.TestCase):
    def test_median_and_interpolation(self):
        samples = [S(i * 5, 10 + i) for i in range(7)]           # 6 intervals of 1% on day 1
        turns = [T(i * 5 + 1, cr=100_000 * (i + 1)) for i in range(6)]
        day2 = [Sample(T0 + timedelta(days=1, minutes=m), 10 + m // 5, None, "r9", "ceiling") for m in (0, 5)]
        iv = build_intervals(samples + day2, turns + [Turn(T0 + timedelta(days=1, minutes=1), "claude-sonnet-5", 0, 0, 5, 0)])
        rates = daily_rates(iv)
        d1 = rates[date(2026, 9, 1)]
        self.assertEqual(d1.n, 6)
        self.assertFalse(d1.interpolated)
        # tokens per interval: 101.5k, 201.5k, ... 601.5k -> median of 6 = (301500+401500)/2
        self.assertAlmostEqual(d1.tokens_per_pct, 351_500)
        self.assertIn("claude-sonnet-5", d1.per_model)
        self.assertAlmostEqual(sum(d1.split.values()), 1.0)
        d2 = rates[date(2026, 9, 2)]
        self.assertTrue(d2.interpolated)
        self.assertAlmostEqual(d2.tokens_per_pct, d1.tokens_per_pct)


class UtcBucketTests(unittest.TestCase):
    def test_intervals_bucket_on_the_utc_day_not_the_local_one(self):
        from datetime import timezone as tz
        east = tz(timedelta(hours=4))
        # 2026-09-02T01:00+04:00 is 2026-09-01T21:00Z: the local date is a day ahead
        end = datetime(2026, 9, 2, 1, 0, tzinfo=east)
        iv = Interval(T0, end, 1.0)
        iv.tokens["cache_read"] = 1000
        rates = daily_rates([iv] * 5)
        self.assertEqual(list(rates), [date(2026, 9, 1)])


PRICES = {"claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25,
                            "meter_weight": 1.2, "class_weight": {"output": 1.8}},
          "claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}}
DOLLAR = 400_000  # Sonnet cache-write tokens worth exactly $1 of meter value


def D(mins, model="claude-sonnet-5", cw=DOLLAR):
    return Turn(T0 + timedelta(minutes=mins), model, 0, 0, 0, cw)


class MeterDollarTests(unittest.TestCase):
    def test_a_bundle_is_valued_the_way_the_publisher_values_a_one_tick_probe_row(self):
        tokens = {"input": 120, "output": 3_000, "cache_read": 900_000, "cache_write": 40_000}
        row = {"tokens": tokens, "tick_from": 4, "tick_to": 5}
        self.assertAlmostEqual(bundle_meter_usd("claude-opus-5", tokens, PRICES),
                               usd_per_pct(row, PRICES["claude-opus-5"]))
        self.assertAlmostEqual(bundle_meter_usd("claude-sonnet-5", tokens, PRICES),
                               usd_per_pct(row, PRICES["claude-sonnet-5"]))

    def test_model_ids_normalise_before_pricing_and_an_unpriced_model_is_none(self):
        self.assertAlmostEqual(turn_meter_usd(D(0, "claude-sonnet-5[1m]"), PRICES), 1.0)
        self.assertAlmostEqual(turn_meter_usd(D(0, "claude-sonnet-5-20260901"), PRICES), 1.0)
        self.assertIsNone(turn_meter_usd(D(0, "claude-haiku-4-5-20251001"), PRICES))


class StretchTests(unittest.TestCase):
    def test_pairs_pool_until_the_meter_has_moved_the_stretch_size(self):
        samples = [S(m, m * 2 / 5) for m in range(0, 35, 5)]          # 0, 2, 4 ... 12
        st = build_stretches(samples, [D(m) for m in (1, 6, 11, 16, 21, 26)], PRICES, stretch_pct=10)
        self.assertEqual(len(st), 1)                                  # the 25->30 remainder stays open
        self.assertEqual((st[0].start, st[0].end, st[0].delta_pct, st[0].windows), (T0, S(25, 0).ts, 10.0, 1))
        self.assertAlmostEqual(st[0].usd, 5.0)
        self.assertAlmostEqual(st[0].usd_per_pct, 0.5)
        self.assertEqual(st[0].tokens, {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0,
                                                            "cache_write": 5 * DOLLAR}})

    def test_a_reset_opens_a_second_window_piece_and_its_straddling_pair_counts_for_nothing(self):
        samples = [S(0, 0), S(5, 3), S(10, 6), S(15, 1, reset="r2"), S(20, 4, reset="r2"), S(25, 7, reset="r2")]
        st = build_stretches(samples, [D(1), D(6), D(12), D(16), D(21)], PRICES, stretch_pct=10)
        self.assertEqual((st[0].delta_pct, st[0].windows), (12.0, 2))
        self.assertAlmostEqual(st[0].usd, 4.0)                        # the turn at 12 min fell in the straddle

    def test_a_gap_longer_than_max_gap_is_not_paired(self):
        samples = [S(0, 0), S(5, 5), S(70, 6), S(75, 11)]
        st = build_stretches(samples, [D(1), D(30), D(71)], PRICES, stretch_pct=10)
        self.assertEqual((st[0].delta_pct, st[0].windows), (10.0, 2))
        self.assertAlmostEqual(st[0].usd, 2.0)

    def test_unpriced_tokens_are_counted_but_not_valued(self):
        st = build_stretches([S(0, 0), S(5, 10)], [D(1), D(2, "claude-haiku-4-5-20251001", cw=100_000)],
                             PRICES, stretch_pct=10)
        self.assertAlmostEqual(st[0].usd, 1.0)
        self.assertEqual(st[0].unpriced_tokens, 100_000)
        self.assertAlmostEqual(st[0].priced_share, DOLLAR / (DOLLAR + 100_000))

    def test_a_fast_mode_turn_loses_exactly_its_tokens_and_is_recorded(self):
        samples = [S(0, 0), S(5, 10)]
        turns = [D(1), D(2, "claude-opus-5", cw=300_000), D(3, "claude-opus-5", cw=50_000)]
        turns = [Turn(t.ts, t.model, t.input, t.output, t.cache_read, t.cache_write, id=f"m{i}")
                 for i, t in enumerate(turns)]
        whole = build_stretches(samples, turns, PRICES, stretch_pct=10)[0]
        (st,) = build_stretches(samples, turns, PRICES, stretch_pct=10, fast={"m1"})
        self.assertEqual(st.tokens["claude-opus-5"]["cache_write"], 50_000)
        self.assertEqual(st.tokens["claude-sonnet-5"], whole.tokens["claude-sonnet-5"])
        self.assertEqual(st.fast_mode_tokens, {"claude-opus-5": {"input": 0, "output": 0, "cache_read": 0,
                                                                 "cache_write": 300_000}})
        self.assertEqual((st.turns, st.fast_mode_turns), (2, 1))
        self.assertAlmostEqual(whole.usd - st.usd, turn_meter_usd(turns[1], PRICES))

    def test_a_stretch_with_no_fast_mode_turn_is_unchanged(self):
        samples = [S(0, 0), S(5, 10)]
        turns = [Turn(T0 + timedelta(minutes=1), "claude-opus-5", 10, 20, 30, 40, id="a")]
        (plain,) = build_stretches(samples, turns, PRICES, stretch_pct=10)
        (st,) = build_stretches(samples, turns, PRICES, stretch_pct=10, fast={"elsewhere"})
        self.assertEqual(st, plain)
        self.assertEqual((st.fast_mode_tokens, st.fast_mode_turns), ({}, 0))

    def test_bounds_allow_one_point_of_rounding_per_window_piece(self):
        st = Stretch(T0, T0, delta_pct=10.0, windows=2, usd=10.0)
        self.assertEqual(st.bounds, (10 / 12, 10 / 8))
        self.assertEqual(Stretch(T0, T0, delta_pct=2.0, windows=2, usd=1.0).bounds[1], float("inf"))


def S7(mins, fh, sd, reset=None):
    return Sample(T0 + timedelta(minutes=mins), fh, sd, reset, "gs-ceiling")


class WindowPointTests(unittest.TestCase):
    def test_points_follow_the_weekly_pairing_rule_per_five_hour_window(self):
        samples = [S7(0, 10, 20), S7(5, 16, 21),   # d5 6, d7 1
                   S7(10, 16, 22),                 # seven-day moved alone: kept (audit finding 3)
                   S7(15, 22, 23),                 # d5 6, d7 1
                   S7(20, 2, 23),                  # five-hour drop: a new window
                   S7(25, 8, 24),                  # d5 6, d7 1
                   S7(30, 14, 0),                  # seven-day drop: its weekly reset, not paired
                   S7(35, 20, 1)]                  # d5 6, d7 1, in the new weekly window: its own point
        pts = window_points(samples)
        self.assertEqual([(p["five_hour_pct"], p["seven_day_pct"], p["windows"]) for p in pts],
                         [(12.0, 3.0, 4.0), (6.0, 1.0, 6.0), (6.0, 1.0, 6.0)])
        self.assertEqual([p["window_ending"] for p in pts],
                         [S7(m, 0, 0).ts.isoformat() for m in (15, 25, 35)])
        self.assertEqual({p["reset_verified"] for p in pts}, {False})

    def test_a_gap_joins_its_window_as_a_second_piece_only_when_the_reset_is_recorded(self):
        r5 = "2026-09-01T15:00:00+00:00"
        with_reset = [Sample(T0 + timedelta(minutes=m), fh, sd, r5, "meter")
                      for m, fh, sd in ((0, 0, 0), (5, 10, 2), (40, 10, 2), (45, 20, 4))]
        pts = window_points(with_reset)
        self.assertEqual([(p["five_hour_pct"], p["seven_day_pct"], p["pieces"], p["reset_verified"]) for p in pts],
                         [(20.0, 4.0, 2, True)])
        no_reset = [Sample(s.ts, s.five_hour, s.seven_day, None, "gs-ceiling") for s in with_reset]
        self.assertEqual([(p["five_hour_pct"], p["pieces"]) for p in window_points(no_reset)], [(10.0, 1), (10.0, 1)])

    def test_a_recorded_weekly_reset_splits_a_window_even_when_the_meter_climbed_back(self):
        # Review of PR 57, round 2, finding 2: the seven-day meter's fall was the only
        # weekly-reset check here. Across the reset between minutes 5 and 10 it reads
        # 1 then 1 again (the new week has already used a point), so d7 = 0 while the
        # true movement is at least the old week's last point: pooled, the window read
        # 18/2 = 9. Pairs and rejoins now also compare the recorded seven-day reset,
        # as tracker/weekly.py does, and both give two points of 6/1.
        r5 = (T0 + timedelta(hours=4)).isoformat()
        r7a = (T0 + timedelta(minutes=7, seconds=30)).isoformat()                 # the weekly reset, between readings
        r7b = (T0 + timedelta(days=7, minutes=7, seconds=30)).isoformat()
        readings = [(0, 0, 0, r7a), (5, 6, 1, r7a), (10, 12, 1, r7b), (15, 18, 2, r7b)]
        lines = [json.dumps({"ts": (T0 + timedelta(minutes=m)).isoformat(),
                             "five_hour": {"utilization": fh, "resets_at": r5},
                             "seven_day": {"utilization": sd, "resets_at": r7}}) for m, fh, sd, r7 in readings]
        samples = parse_moonlighter(lines, source="meter")
        self.assertEqual([s.seven_resets_at for s in samples], [r7a, r7a, r7b, r7b])
        pts = window_points(samples)
        self.assertEqual([(p["five_hour_pct"], p["seven_day_pct"], p["pieces"]) for p in pts], [(6.0, 1.0, 1), (6.0, 1.0, 1)])
        rows = [{"ts": (T0 + timedelta(minutes=m)).isoformat(), "five_hour": float(fh), "five_resets_at": r5,
                 "seven_day": float(sd), "seven_resets_at": r7} for m, fh, sd, r7 in readings]
        by_window = weekly_windows(rows, now=T0)["by_window"]
        self.assertEqual([(p["five_hour_pct"], p["seven_day_pct"]) for p in by_window],
                         [(p["five_hour_pct"], p["seven_day_pct"]) for p in pts])
        # A gap across the reset rejoins nothing either: the piece after it is its own point.
        gapped = [samples[0], samples[1]] + [Sample(s.ts + timedelta(minutes=30), s.five_hour, s.seven_day, s.resets_at,
                                                    s.source, s.seven_resets_at) for s in samples[2:]]
        self.assertEqual([(p["five_hour_pct"], p["pieces"]) for p in window_points(gapped)], [(6.0, 1), (6.0, 1)])

    def test_a_window_whose_weekly_meter_reaches_the_cap_is_left_out_whole(self):
        # dave, 21 September: the seven-day meter reached 100 and stopped while the
        # five-hour meter kept counting. The window that ends at 100 goes, the movement it
        # had before the cap included; so does one that starts there. The window before is kept.
        samples = [S7(0, 10, 90), S7(5, 20, 92),              # 10 over 2: kept
                   S7(10, 2, 92), S7(15, 30, 98), S7(20, 51, 100),   # reaches the cap: out
                   S7(25, 1, 100), S7(30, 25, 100)]            # starts at the cap: out
        pts = window_points(samples)
        self.assertEqual([(p["five_hour_pct"], p["seven_day_pct"]) for p in pts], [(10.0, 2.0)])
        rows = [{"ts": s.ts.isoformat(), "five_hour": float(s.five_hour),
                 "five_resets_at": ("a" if i < 2 else "b" if i < 5 else "c"),
                 "seven_day": float(s.seven_day), "seven_resets_at": "2026-09-24T23:00:00+00:00"}
                for i, s in enumerate(samples)]
        for r in rows:  # tracker/weekly.py compares recorded resets as instants
            r["five_resets_at"] = {"a": "2026-09-01T15:00:00+00:00", "b": "2026-09-01T20:00:00+00:00",
                                   "c": "2026-09-02T01:00:00+00:00"}[r["five_resets_at"]]
        out = weekly_windows(rows, now=T0)
        self.assertEqual([(p["five_hour_pct"], p["seven_day_pct"]) for p in out["by_window"]],
                         [(10.0, 2.0)])
        # The week's own sums lose the capped windows too, not only the per-window series.
        self.assertEqual(out["history"], [])

    def test_sums_match_tracker_weekly_on_the_same_readings(self):
        fh, sd = [0, 10, 20, 30, 40, 50, 60], [0, 1, 3, 4, 6, 7, 9]
        r5, r7 = "2026-09-01T15:00:00+00:00", "2026-09-04T04:00:00+00:00"
        rows = [{"ts": (T0 + timedelta(minutes=5 * i)).isoformat(), "five_hour": float(a), "five_resets_at": r5,
                 "seven_day": float(b), "seven_resets_at": r7} for i, (a, b) in enumerate(zip(fh, sd))]
        weekly = weekly_windows(rows, now=T0)["history"][0]
        pts = window_points([Sample(T0 + timedelta(minutes=5 * i), a, b, r5, "meter") for i, (a, b) in enumerate(zip(fh, sd))])
        self.assertEqual((sum(p["five_hour_pct"] for p in pts), sum(p["seven_day_pct"] for p in pts)),
                         (weekly["five_hour_pct"], weekly["seven_day_pct"]))
        self.assertEqual(pts[0]["window_ending"], r5)                 # a recorded reset time names the window
