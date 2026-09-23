import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import speed
from tracker.speed import (
    MIN_REQUESTS,
    daily_rows,
    fast_mode,
    kept,
    merge,
    recompute_from,
    requests_from_extract,
    requests_from_files,
    requests_in,
    speed_block,
    update,
)

T0 = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def user(t, side=False):
    return {"type": "user", "timestamp": ts(t), "isSidechain": side}


def block(t, mid, out, model="claude-opus-5", side=False, entrypoint="cli", speed_label="standard",
          inp=2, cw=1000):
    return {"type": "assistant", "timestamp": ts(t), "isSidechain": side, "entrypoint": entrypoint,
            "message": {"id": mid, "model": model,
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cw,
                                  "cache_read_input_tokens": 50000, "output_tokens": out,
                                  "speed": speed_label}}}


def session(n, rate, start=0.0, prefix="m", out=600, model="claude-opus-5", entrypoint="cli"):
    """n requests of `out` tokens at `rate` tokens/s, each triggered 2 s after the last ended."""
    lines, t = [], start
    for i in range(n):
        lines.append(user(t))
        lines.append(block(t + out / rate, f"{prefix}{i}", out, model=model, entrypoint=entrypoint))
        t += out / rate + 2
    return lines


class TimingTest(unittest.TestCase):
    def test_trigger_is_last_user_line_before_first_block(self):
        lines = [user(0), user(3), block(5, "a", 400), block(9, "a", 400)]
        (r,) = requests_in(lines, "s")
        self.assertEqual(r.seconds, 6)
        self.assertEqual(r.ttfb, 2)
        self.assertEqual(r.output, 400)

    def test_parallel_tool_results_use_the_last(self):
        lines = [user(0), block(2, "a", 350), user(3), user(4), user(4.5), block(10.5, "b", 600)]
        rs = {r.id: r for r in requests_in(lines, "s")}
        self.assertEqual(rs["b"].seconds, 6)

    def test_multi_block_ends_at_latest_block_with_final_count(self):
        # A 2.1.278-style duplicate: blocks written again after the last one, with
        # output_tokens 0 and the first block's timestamp. The end stays the latest block.
        lines = [user(0), block(2, "a", 900), block(6, "a", 900), block(2, "a", 0), block(2, "a", 0)]
        (r,) = requests_in(lines, "s")
        self.assertEqual((r.seconds, r.ttfb, r.output), (6, 2, 900))

    def test_user_line_between_blocks_does_not_move_the_start(self):
        lines = [user(0), block(2, "a", 900), user(3), block(6, "a", 900)]
        (r,) = requests_in(lines, "s")
        self.assertEqual(r.seconds, 6)

    def test_sidechain_lines_keep_their_own_trigger(self):
        lines = [user(0), user(5, side=True), block(8, "side", 400, side=True), block(10, "main", 500)]
        rs = {r.id: r for r in requests_in(lines, "s")}
        self.assertEqual(rs["main"].seconds, 10)
        self.assertEqual(rs["side"].seconds, 3)
        self.assertTrue(rs["side"].sidechain)

    def test_no_trigger_means_no_request(self):
        self.assertEqual(requests_in([block(2, "a", 900), user(3), block(5, "b", 400)], "s")[0].id, "b")

    def test_unpriced_model_dropped_and_ids_normalised(self):
        lines = [user(0), block(4, "a", 500, model="<synthetic>"), user(5),
                 block(9, "b", 500, model="claude-haiku-4-5-20251001")]
        (r,) = requests_in(lines, "s")
        self.assertEqual((r.id, r.model), ("b", "claude-haiku-4-5"))

    def test_uncached_input_is_input_plus_cache_writes(self):
        (r,) = requests_in([user(0), block(5, "a", 500, inp=7, cw=1200)], "s")
        self.assertEqual(r.uncached_input, 1207)

    def test_filters(self):
        cases = {"short": ([user(0), block(5, "x", 299)], False),
                 "quick": ([user(0), block(1, "x", 900)], False),
                 "slow": ([user(0), block(900, "x", 900)], False),
                 "fast-label": ([user(0), block(5, "x", 900, speed_label="fast")], False),
                 "unlabelled": ([user(0), {**block(5, "x", 900)}], True),
                 "ok": ([user(0), block(5, "x", 300)], True)}
        del cases["unlabelled"][0][1]["message"]["usage"]["speed"]
        for name, (lines, want) in cases.items():
            (r,) = requests_in(lines, "s")
            self.assertEqual(kept(r), want, name)


class DedupeTest(unittest.TestCase):
    def test_one_message_id_counted_once_across_files_and_calls(self):
        with tempfile.TemporaryDirectory() as d:
            a, b, c = Path(d, "a.jsonl"), Path(d, "b.jsonl"), Path(d, "c.jsonl")
            body = "\n".join(json.dumps(x) for x in [user(0), block(5, "m1", 500)]) + "\n"
            a.write_text(body)
            b.write_text(body + "not json\n")
            c.write_text(body)
            seen: set[str] = set()
            self.assertEqual(len(requests_from_files([a, b], seen)), 1)
            self.assertEqual(requests_from_files([c], seen), [])

    def test_extract_lines_grouped_by_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "x.jsonl.gz")
            # Interleaved files: each keeps its own trigger.
            lines = [{**user(0), "file": "f1"}, {**user(4), "file": "f2"},
                     {**block(5, "a", 500), "file": "f1"}, {**block(6, "b", 500), "file": "f2"}]
            with gzip.open(p, "wt") as fh:
                fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
            rs = {r.id: r for r in requests_from_extract(p)}
            self.assertEqual((rs["a"].seconds, rs["b"].seconds), (5, 2))


class FastModeTest(unittest.TestCase):
    def test_fast_session_flagged_standard_not(self):
        lines_std = session(40, 70, prefix="s")
        lines_fast = session(20, 170, start=5000, prefix="f")
        reqs = requests_in(lines_std, "std") + requests_in(lines_fast, "fast")
        flagged = fast_mode(reqs)
        self.assertEqual(flagged, {f"f{i}" for i in range(20)})

    def test_mid_session_fallback_keeps_the_standard_stretch(self):
        lines = session(15, 170, prefix="f") + session(30, 70, start=5000, prefix="s")
        flagged = fast_mode(requests_in(lines, "one"))
        self.assertTrue({f"f{i}" for i in range(11)} <= flagged)
        self.assertFalse({f"s{i}" for i in range(5, 30)} & flagged)

    def test_every_also_takes_a_fast_sessions_short_requests(self):
        lines = session(20, 170, prefix="f") + [user(1000), block(1001, "short", 40)]
        lines += session(40, 70, start=5000, prefix="s") + [user(9000), block(9001, "short-std", 40)]
        reqs = requests_in(lines[:42], "fast") + requests_in(lines[42:], "std")
        self.assertNotIn("short", fast_mode(reqs))
        flagged = fast_mode(reqs, every=True)
        self.assertEqual(flagged, {f"f{i}" for i in range(20)} | {"short"})

    def test_only_opus_is_flagged(self):
        lines = session(40, 70, prefix="s", model="claude-sonnet-5") + \
            session(20, 170, start=5000, prefix="f", model="claude-sonnet-5")
        self.assertEqual(fast_mode(requests_in(lines, "x")), set())


class AggregateTest(unittest.TestCase):
    def rows(self):
        reqs = requests_in(session(40, 70, prefix="s"), "a") + requests_in(session(20, 170, start=5000, prefix="f"), "b")
        return daily_rows(reqs, "a2")

    def test_daily_row(self):
        (row,) = self.rows()
        self.assertEqual((row["day"], row["model"], row["account"], row["entrypoint"]),
                         ("2026-09-20", "claude-opus-5", "a2", "cli"))
        self.assertEqual((row["n"], row["fast_excluded"]), (40, 20))
        self.assertEqual(sum(row["output_hist"].values()), 40)

    def test_block_median_within_bin_and_min_requests(self):
        block_ = speed_block({"rows": self.rows()})
        (day,) = block_["models"]["claude-opus-5"]["daily"]
        self.assertAlmostEqual(day["output_tokens_per_s"]["median"], 70, delta=70 * 0.02)
        self.assertAlmostEqual(day["time_to_first_block_s"]["median"], 600 / 70, delta=0.2)
        self.assertEqual(list(block_["models"]["claude-opus-5"]["by_account_entrypoint"]), ["a2:cli"])
        few = daily_rows(requests_in(session(MIN_REQUESTS - 1, 70), "a"), "a2")
        self.assertEqual(speed_block({"rows": few})["models"], {})

    def test_pooling_across_histories(self):
        a = daily_rows(requests_in(session(20, 60, prefix="x"), "a"), "a1")
        b = daily_rows(requests_in(session(20, 100, prefix="y"), "b"), "a2")
        m = speed_block({"rows": a}, {"rows": b})["models"]["claude-opus-5"]
        self.assertEqual(m["daily"][0]["n"], 40)
        self.assertEqual(m["by_account"], {})  # 20 each, under MIN_REQUESTS
        self.assertTrue(60 <= m["daily"][0]["output_tokens_per_s"]["median"] <= 100)

    def test_since_day_drops_older_requests(self):
        reqs = requests_in(session(40, 70), "a")
        self.assertEqual(daily_rows(reqs, "a2", since_day="2026-09-21"), [])


def row(day, n=1, account="a1"):
    return {"day": day, "model": "claude-opus-5", "account": account, "entrypoint": "cli", "n": n,
            "fast_excluded": 0, "output_hist": {"215": n}, "ttfb_hist": {}}


class HistoryTest(unittest.TestCase):
    def test_merge_keeps_old_days_and_replaces_recent(self):
        stored = [row("2026-08-20", 5), row("2026-09-19", 5), row("2026-09-21", 5), row("2026-09-22", 5)]
        fresh = [row("2026-08-20", 1), row("2026-09-21", 9)]
        got = {r["day"]: r["n"] for r in merge(stored, fresh, "2026-09-21")}
        # Old days never recomputed; a covered day replaced; a covered day the fresh scan
        # has nothing for is kept rather than erased.
        self.assertEqual(got, {"2026-08-20": 5, "2026-09-19": 5, "2026-09-21": 9, "2026-09-22": 5})

    def test_recompute_window(self):
        now = datetime(2026, 9, 23, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(recompute_from(None, now), (None, None))
        day, mtime = recompute_from({"rows": [row("2026-09-01")]}, now)
        self.assertEqual(day, "2026-09-21")
        self.assertEqual(mtime, datetime(2026, 9, 21, tzinfo=timezone.utc))

    def test_cli_appends_to_history(self):
        with tempfile.TemporaryDirectory() as d:
            hist = Path(d, "h.json")
            old = row("2026-08-01", 7)
            hist.write_text(json.dumps({"rows": [old]}))
            extract = Path(d, "x.jsonl")
            extract.write_text("\n".join(json.dumps({**x, "file": "f"}) for x in session(40, 70)) + "\n")
            speed.main(["--lines", str(extract), "--history", str(hist)])
            body = json.loads(hist.read_text())
            self.assertEqual(body["method"], speed.METHOD_ID)
            self.assertIn(old, body["rows"])
            # The 20 Sep fixture is older than the recompute window, and the history
            # already has rows, so nothing is recomputed.
            self.assertEqual(len(body["rows"]), 1)
            body2 = update(None, daily_rows(requests_in(session(40, 70), "f"), "a1"), "0001-01-01",
                           datetime(2026, 9, 23, tzinfo=timezone.utc))
            self.assertEqual([r["day"] for r in body2["rows"]], ["2026-09-20"])


class PublishTest(unittest.TestCase):
    def test_block_carries_only_labels_and_plain_english(self):
        rows_ = daily_rows(requests_in(session(40, 70), "a"), "a3")
        text = json.dumps(speed_block({"rows": rows_}))
        for name in ("jwork", "dave", "avis", "masterrig", "Jonathan"):
            self.assertNotIn(name, text)
        b = speed_block({"rows": rows_})
        self.assertIsInstance(b["method"], str)
        self.assertTrue(all(isinstance(c, str) for c in b["caveats"]))
        self.assertEqual(b["accounts"], {"a3": {"first_day": "2026-09-20", "last_day": "2026-09-20"}})

    def test_empty(self):
        self.assertEqual(speed_block(None, {})["models"], {})


if __name__ == "__main__":
    unittest.main()
