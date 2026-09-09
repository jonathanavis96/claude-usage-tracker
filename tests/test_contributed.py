"""Tests for tracker/contributed.py: fetch, merge and the per-plan aggregate.

Nothing here opens a socket: fetch takes a fake urlopen. Fixtures are three
contributors on two plans (two on max20, one on pro) with two samples each
inside one five-hour and one seven-day window, so the weekly pairing has
something to pair.
"""
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker.contributed import aggregate, fetch, main, merge, _weighted_median

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


def sample(cid, plan, ts, five, seven, tokens=None, five_reset=FIVE_RESET, seven_reset=SEVEN_RESET):
    return {"contributor_id": cid, "plan": plan, "plan_source": "flag", "ts": ts,
            "five_hour": {"utilization": five, "resets_at": five_reset},
            "seven_day": {"utilization": seven, "resets_at": seven_reset},
            "tokens_since_five_hour_reset": tokens or {}, "tokens_since_seven_day_reset": {},
            "client_version": "contrib-sample/0.1.0", "received_at": ts}


def sonnet(cache_write, output=0):
    return {"claude-sonnet-5": {"input": 0, "output": output, "cache_read": 0, "cache_write": cache_write}}


def fixture_rows():
    """Three contributors, two plans. Each pair sits in the same windows and moves
    the five-hour meter by 60 and the seven-day meter by 2 (A, B) or 3 (C)."""
    return [
        # A on max20: tokens_per_pct 400k/40 = 10000, then 800k/100... second sample is util 100
        sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0, sonnet(200_000)),
        sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0, sonnet(800_000)),
        # B on max20: tokens_per_pct 300k/20 = 15000, then 600k/80 = 7500
        sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 30.0, sonnet(300_000)),
        sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 32.0, sonnet(600_000)),
        # C on pro, alone: never enough for a measured weekly figure
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


class TokensPerPctTests(unittest.TestCase):
    def test_median_across_contributors_of_each_contributors_median(self):
        j = aggregate(fixture_rows(), NOW, PRICES)
        s = j["max20"]["tokens_per_pct"]["claude-sonnet-5"]
        # A: 200k/20 = 10000 and 800k/80 = 10000 -> 10000. B: 300k/20 = 15000 and 600k/80 = 7500 -> 11250.
        self.assertEqual(s["median"], round((10000 + 11250) / 2))
        self.assertEqual((s["contributors"], s["samples"]), (2, 4))

    def test_usd_per_pct_values_tokens_as_publish_does(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(400_000, output=100_000)),
                sample(B, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, sonnet(400_000, output=100_000))]
        j = aggregate(rows, NOW, PRICES)
        # 400k cache_write at $2.5/M = $1.00, 100k output at $10/M x class_weight 1.8 = $1.80: $2.80 over 50%.
        self.assertAlmostEqual(j["max20"]["usd_per_pct"]["claude-sonnet-5"]["median"], 2.80 / 50, places=4)
        self.assertEqual(j["max20"]["tokens_per_pct"]["claude-sonnet-5"]["median"], 500_000 / 50)

    def test_usd_per_pct_applies_meter_weight_and_skips_unpriced_models(self):
        prices = {"claude-sonnet-5": {**PRICES["claude-sonnet-5"], "meter_weight": 2.0}}
        tokens = {**sonnet(400_000), "claude-mystery-9": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 1000}}
        j = aggregate([sample(A, "max20", "2026-09-02T10:00:00Z", 50.0, 10.0, tokens)], NOW, prices)
        self.assertAlmostEqual(j["max20"]["usd_per_pct"]["claude-sonnet-5"]["median"], 2.0 / 50, places=4)
        self.assertIn("claude-mystery-9", j["max20"]["tokens_per_pct"])
        self.assertNotIn("claude-mystery-9", j["max20"]["usd_per_pct"])

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


class WeeklyWindowsTests(unittest.TestCase):
    def test_pairs_two_samples_per_contributor_and_measures_with_two_contributors(self):
        j = aggregate(fixture_rows(), NOW, PRICES)
        w = j["max20"]["weekly_windows"]
        # A: 60 five-hour points over 2 seven-day points = 30; B: 60/2 = 30.
        self.assertEqual(w["measured"], 30.0)
        self.assertIsNone(w["reason"])
        self.assertEqual((w["contributors"], w["dropped"], w["weeks"]), (2, 0, 2))

    def test_one_contributor_is_not_a_measurement(self):
        w = aggregate(fixture_rows(), NOW, PRICES)["pro"]["weekly_windows"]
        self.assertIsNone(w["measured"])
        self.assertEqual(w["contributors"], 1)
        self.assertIn("2 needed", w["reason"])

    def test_an_open_week_does_not_count_towards_the_gate(self):
        still_open = (NOW + timedelta(days=2)).isoformat()
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0, seven_reset=still_open),
                sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0, seven_reset=still_open),
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0)]
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertIsNone(w["measured"])
        self.assertEqual(w["contributors"], 1)

    def test_samples_in_different_windows_do_not_pair(self):
        other_reset = "2026-09-02T19:00:00+00:00"
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "max20", "2026-09-02T16:00:00Z", 80.0, 12.0, five_reset=other_reset),
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0)]
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertEqual(w["contributors"], 1)
        self.assertIsNone(w["measured"])

    def test_a_contributor_more_than_30_percent_from_the_median_is_dropped(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0),   # 30
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0),   # 30
                sample(C, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(C, "max20", "2026-09-02T12:00:00Z", 80.0, 20.0)]   # 6: dropped
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertEqual(w["measured"], 30.0)
        self.assertEqual((w["contributors"], w["dropped"], w["weeks"]), (3, 1, 2))

    def test_two_contributors_far_apart_give_no_figure(self):
        rows = [sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 12.0),   # 30
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 20.0)]   # 6
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertIsNone(w["measured"])
        self.assertEqual(w["dropped"], 2)
        self.assertIn("30%", w["reason"])

    def test_weighted_by_weeks_contributed(self):
        # A has two complete weeks at 20, B one week at 24 (within 30% of the plan
        # median, so kept): the weighted median is A's figure, not the mean.
        week1 = ("2026-08-26T14:00:00+00:00", "2026-08-29T10:00:00+00:00")
        rows = [sample(A, "max20", "2026-08-26T10:00:00Z", 20.0, 10.0, five_reset=week1[0], seven_reset=week1[1]),
                sample(A, "max20", "2026-08-26T12:00:00Z", 80.0, 13.0, five_reset=week1[0], seven_reset=week1[1]),
                sample(A, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(A, "max20", "2026-09-02T12:00:00Z", 80.0, 13.0),
                sample(B, "max20", "2026-09-02T10:00:00Z", 20.0, 10.0),
                sample(B, "max20", "2026-09-02T12:00:00Z", 80.0, 12.5)]
        w = aggregate(rows, NOW, PRICES)["max20"]["weekly_windows"]
        self.assertEqual(w["measured"], 20.0)
        self.assertEqual((w["contributors"], w["dropped"], w["weeks"]), (2, 0, 3))

    def test_weighted_median_reduces_to_median_with_equal_weights(self):
        self.assertEqual(_weighted_median([(1, 1), (2, 1), (3, 1), (4, 1)]), 2.5)
        self.assertEqual(_weighted_median([(3, 1), (1, 1), (2, 1)]), 2)
        self.assertEqual(_weighted_median([(10, 3), (30, 1)]), 10)


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
            keys = {(json.loads(l)["contributor_id"], json.loads(l)["ts"]) for l in lines}
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
        self._body = "".join(json.dumps(l) + "\n" for l in lines).encode()
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
            self.assertEqual(out["max20"]["weekly_windows"]["measured"], 30.0)
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
