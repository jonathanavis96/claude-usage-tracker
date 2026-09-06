"""tracker.weight: the output class weight recomputed from the weekly Fable output run.

Nothing here touches the network: every alert goes to a recorded fake poster, and
the env file handed to the guard is a temp file with no address in it unless a
test says otherwise.
"""
from __future__ import annotations
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tracker.rows import is_output, prose_rows
from tracker.weight import (GUARD, RECORD_KEY, WEIGHT_MODEL, latest_pair, solve_output_weight,
                            update_output_weight)
from tracker.publish import build_public_json, main as publish_main, usd_per_pct

NOW = datetime(2026, 9, 7, 5, 30, tzinfo=timezone.utc)
FABLE = {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 12.5, "meter_weight": 1.0,
         "class_weight": {"input": 1.0, "output": 1.8, "cache_read": 1.0, "cache_write": 1.0}}
SONNET = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5, "meter_weight": 1.0,
          "class_weight": {"input": 1.0, "output": 1.8, "cache_read": 1.0, "cache_write": 1.0}}
PRICES = {"_source": "test", "claude-sonnet-5": SONNET, "claude-fable-5-1": FABLE}
PASSIVE = {"split": {"input": 0.0, "output": 0.02, "cache_read": 0.97, "cache_write": 0.01}, "history": {}}
EFFORT = {"claude-sonnet-5": {"low": 900000}}


def row(ts: str, model: str, tokens: dict, payload: str | None = None, ticks: int = 5) -> dict:
    """A probe row spanning `ticks` percent whose PER-PERCENT token bundle is `tokens`."""
    full = {cls: tokens.get(cls, 0) * ticks for cls in ("input", "output", "cache_read", "cache_write")}
    r = {"ts": ts, "model": model, "effort": "low", "tokens": full, "prompts": 9,
         "tokens_per_pct": sum(tokens.values()), "tick_from": 10, "tick_to": 10 + ticks,
         "elapsed_s": 100, "account": "dave"}
    if payload:
        r["payload"] = payload
    return r


# $1.00 of list value per 1%, all cache write: the invariant probe's shape.
PROSE = row("2026-09-06T11:16:00+00:00", WEIGHT_MODEL, {"cache_write": 80_000})
# $0.50 of output plus $0.10 of cache write per 1%: 1.00 - 0.10 = w * 0.50, so w = 1.8.
OUTPUT = row("2026-09-06T12:06:00+00:00", WEIGHT_MODEL, {"output": 10_000, "cache_write": 8_000}, "output")
# $0.20 of output plus $0.10 of cache write: w = 0.90 / 0.20 = 4.5, far outside the guard.
WILD_OUTPUT = row("2026-09-13T06:30:00+00:00", WEIGHT_MODEL, {"output": 4_000, "cache_write": 8_000}, "output")
SONNET_ROWS = [row(f"2026-09-{d:02d}T04:05:00+00:00", "claude-sonnet-5", {"cache_write": 400_000, "cache_read": 100_000})
               for d in (1, 2, 3, 4, 5, 6)]


class FakePost:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def __call__(self, url, headers, body):
        self.calls.append((url, dict(headers), json.loads(body)))
        return self.status, '{"ok":true}'


class RowTagTests(unittest.TestCase):
    def test_missing_payload_is_prose(self):
        self.assertFalse(is_output({"model": "claude-sonnet-5"}))
        self.assertFalse(is_output({"payload": "prose"}))
        self.assertTrue(is_output({"payload": "output"}))

    def test_prose_rows_drops_output_rows_only(self):
        self.assertEqual(prose_rows([PROSE, OUTPUT, SONNET_ROWS[0]]), [PROSE, SONNET_ROWS[0]])


class SolveTests(unittest.TestCase):
    def test_solves_the_weight_that_makes_both_rows_agree(self):
        w = solve_output_weight(PROSE, OUTPUT, FABLE)
        self.assertAlmostEqual(w, 1.8, places=9)
        # The definition: with that weight, both rows read the same meter dollars per 1%.
        price = {**FABLE, "class_weight": {**FABLE["class_weight"], "output": w}}
        self.assertAlmostEqual(usd_per_pct(PROSE, price), usd_per_pct(OUTPUT, price), places=9)

    def test_prose_rows_own_output_tokens_are_in_the_solve(self):
        # 1,000 output tokens on the prose row ($0.05 list) sit on the weighted side too.
        prose = row("2026-09-06T11:16:00+00:00", WEIGHT_MODEL, {"cache_write": 80_000, "output": 1_000})
        w = solve_output_weight(prose, OUTPUT, FABLE)
        price = {**FABLE, "class_weight": {**FABLE["class_weight"], "output": w}}
        self.assertAlmostEqual(usd_per_pct(prose, price), usd_per_pct(OUTPUT, price), places=9)
        self.assertAlmostEqual(w, 0.90 / 0.45, places=9)

    def test_other_class_weights_are_honoured(self):
        price = {**FABLE, "class_weight": {**FABLE["class_weight"], "cache_write": 2.0}}
        # prose: $1.00 list x 2.0 = 2.00 meter; output row: $0.10 x 2.0 = 0.20 + w x 0.50.
        self.assertAlmostEqual(solve_output_weight(PROSE, OUTPUT, price), 3.6, places=9)

    def test_refuses_a_pair_with_no_output_contrast(self):
        with self.assertRaises(ValueError):
            solve_output_weight(PROSE, PROSE, FABLE)


class PairTests(unittest.TestCase):
    def test_latest_output_row_against_latest_prose_row_of_the_weight_model(self):
        older_prose = row("2026-09-01T11:00:00+00:00", WEIGHT_MODEL, {"cache_write": 70_000})
        sonnet_output = row("2026-09-08T06:00:00+00:00", "claude-sonnet-5", {"output": 1}, "output")
        pair = latest_pair([older_prose, OUTPUT, PROSE, WILD_OUTPUT, sonnet_output] + SONNET_ROWS)
        self.assertEqual(pair, (WILD_OUTPUT, PROSE))

    def test_no_pair_without_both_shapes(self):
        self.assertIsNone(latest_pair([PROSE] + SONNET_ROWS))
        self.assertIsNone(latest_pair([OUTPUT] + SONNET_ROWS))

    def test_flagged_outlier_rows_are_skipped(self):
        flagged = {**WILD_OUTPUT, "outlier": True}
        self.assertEqual(latest_pair([PROSE, OUTPUT, flagged]), (OUTPUT, PROSE))


class UpdateTests(unittest.TestCase):
    def _prices(self, tmp: str, prices: dict = PRICES) -> Path:
        p = Path(tmp) / "prices.json"
        p.write_text(json.dumps(prices, indent=2) + "\n", encoding="utf-8")
        return p

    def _env(self, tmp: str, text: str = "NOTIFY_SEND_SECRET=s3cr3t\nNOTIFY_ALERT_TO=jonathan@example.com\n") -> Path:
        p = Path(tmp) / "notify.env"
        p.write_text(text, encoding="utf-8")
        return p

    def _update(self, rows, prices_path, env_path, post=None, prices_before=None):
        post = post or FakePost()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = update_output_weight(rows, prices_path, post=post, env_file=env_path, environ={}, now=NOW)
        return result, post, out.getvalue(), err.getvalue()

    def test_applies_a_weight_inside_the_guard_to_every_model(self):
        with tempfile.TemporaryDirectory() as t:
            prices = {**PRICES, "claude-fable-5-1": {**FABLE, "class_weight": {**FABLE["class_weight"], "output": 1.6}},
                      "claude-sonnet-5": {**SONNET, "class_weight": {**SONNET["class_weight"], "output": 1.6}}}
            p = self._prices(t, prices)
            result, post, out, _ = self._update([PROSE, OUTPUT] + SONNET_ROWS, p, self._env(t))
            self.assertTrue(result.applied)
            self.assertEqual(result.value, 1.8)
            self.assertEqual(result.current, 1.6)
            written = json.loads(p.read_text())
            self.assertEqual(written["claude-fable-5-1"]["class_weight"]["output"], 1.8)
            self.assertEqual(written["claude-sonnet-5"]["class_weight"]["output"], 1.8)
            self.assertEqual(written["claude-fable-5-1"]["class_weight"]["cache_write"], 1.0)
            self.assertEqual(written["_source"], "test")
            record = written[RECORD_KEY]
            self.assertEqual((record["output_row"], record["prose_row"]), (OUTPUT["ts"], PROSE["ts"]))
            self.assertEqual((record["value"], record["applied"]), (1.8, True))
            self.assertEqual(post.calls, [], "an applied weight is not an alert")
            self.assertIn("output weight: applied 1.8 (was 1.6)", out)

    def test_refuses_a_weight_outside_the_guard_and_alerts(self):
        with tempfile.TemporaryDirectory() as t:
            p = self._prices(t)
            before = p.read_text()
            result, post, out, _ = self._update([PROSE, OUTPUT, WILD_OUTPUT] + SONNET_ROWS, p, self._env(t))
            self.assertFalse(result.applied)
            self.assertEqual(result.value, 4.5)
            self.assertGreater(result.change, GUARD)
            written = json.loads(p.read_text())
            for model in ("claude-fable-5-1", "claude-sonnet-5"):
                self.assertEqual(written[model]["class_weight"]["output"], 1.8, "refused weight must not be written")
            record = written[RECORD_KEY]
            self.assertEqual((record["output_row"], record["value"], record["applied"]), (WILD_OUTPUT["ts"], 4.5, False))
            self.assertEqual(len(post.calls), 1)
            url, headers, body = post.calls[0]
            self.assertEqual(headers["authorization"], "Bearer s3cr3t")
            self.assertEqual(body["to"], "jonathan@example.com")
            self.assertIn("Output class weight refused", body["subject"])
            self.assertIn("4.5", body["text"])
            self.assertIn("1.8", body["text"])
            self.assertIn(WILD_OUTPUT["ts"], body["text"])
            self.assertIn("output weight: refused", out)
            self.assertNotEqual(before, p.read_text(), "the refusal is recorded so it alerts once")

    def test_same_pair_is_not_recomputed_or_alerted_twice(self):
        with tempfile.TemporaryDirectory() as t:
            p = self._prices(t)
            rows = [PROSE, OUTPUT, WILD_OUTPUT] + SONNET_ROWS
            first, post, _, _ = self._update(rows, p, self._env(t))
            self.assertFalse(first.applied)
            self.assertEqual(len(post.calls), 1)
            after_first = p.read_text()
            second, post2, _, _ = self._update(rows, p, self._env(t))
            self.assertIsNone(second)
            self.assertEqual(post2.calls, [])
            self.assertEqual(p.read_text(), after_first)

    def test_no_output_row_leaves_prices_alone(self):
        with tempfile.TemporaryDirectory() as t:
            p = self._prices(t)
            before = p.read_text()
            result, post, _, _ = self._update([PROSE] + SONNET_ROWS, p, self._env(t))
            self.assertIsNone(result)
            self.assertEqual(p.read_text(), before)
            self.assertEqual(post.calls, [])

    def test_unconfigured_alert_is_a_note_not_a_failure(self):
        with tempfile.TemporaryDirectory() as t:
            p = self._prices(t)
            result, post, _, err = self._update([PROSE, WILD_OUTPUT] + SONNET_ROWS, p, self._env(t, "# nothing\n"))
            self.assertFalse(result.applied)
            self.assertEqual(post.calls, [])
            self.assertIn("alert: skipped", err)

    def test_refused_post_is_a_warning_not_a_failure(self):
        with tempfile.TemporaryDirectory() as t:
            p = self._prices(t)
            result, post, _, err = self._update([PROSE, WILD_OUTPUT] + SONNET_ROWS, p, self._env(t), post=FakePost(status=500))
            self.assertFalse(result.applied)
            self.assertEqual(len(post.calls), 1)
            self.assertIn("HTTP 500", err)


class PublisherExcludesOutputRowsTests(unittest.TestCase):
    def test_output_rows_do_not_enter_medians_detection_or_freshness(self):
        prices = {k: v for k, v in PRICES.items() if not k.startswith("_")}
        without = build_public_json(SONNET_ROWS, PASSIVE, EFFORT, prices, NOW)
        # An output row on Fable, later than every Sonnet row and wildly cheaper per 1%.
        wild = row("2026-09-06T12:06:00+00:00", WEIGHT_MODEL, {"output": 100}, "output")
        with_output = build_public_json(SONNET_ROWS + [wild], PASSIVE, EFFORT, prices, NOW)
        self.assertEqual(with_output["rates"], without["rates"])
        self.assertEqual(with_output["history"], without["history"])
        self.assertEqual(with_output["last_change"], without["last_change"])
        self.assertEqual(with_output["last_sample_at"], SONNET_ROWS[-1]["ts"])
        self.assertEqual(with_output["rates"]["claude-fable-5-1"]["source"], "derived")

    def test_only_output_rows_is_nothing_to_publish(self):
        prices = {k: v for k, v in PRICES.items() if not k.startswith("_")}
        with self.assertRaises(ValueError):
            build_public_json([OUTPUT], PASSIVE, EFFORT, prices, NOW)


class PublishMainTests(unittest.TestCase):
    """tracker.publish's CLI applies the weight before it builds the JSON."""

    def _files(self, t: str, rows: list[dict]) -> Path:
        d = Path(t)
        (d / "probes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (d / "passive.json").write_text(json.dumps(PASSIVE), encoding="utf-8")
        (d / "effort.json").write_text(json.dumps(EFFORT), encoding="utf-8")
        prices = {**PRICES, "claude-fable-5-1": {**FABLE, "class_weight": {**FABLE["class_weight"], "output": 1.6}},
                  "claude-sonnet-5": {**SONNET, "class_weight": {**SONNET["class_weight"], "output": 1.6}}}
        (d / "prices.json").write_text(json.dumps(prices, indent=2) + "\n", encoding="utf-8")
        (d / "notify.env").write_text("# unconfigured on purpose\n", encoding="utf-8")
        return d

    def _run(self, d: Path, now: datetime, post: FakePost) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = publish_main(["--probes", str(d / "probes.jsonl"), "--passive", str(d / "passive.json"),
                               "--effort", str(d / "effort.json"), "--prices", str(d / "prices.json"),
                               "--alert-env-file", str(d / "notify.env"), "--out", str(d / "out.json")],
                              post=post, environ={}, now=now)
        return rc, out.getvalue(), err.getvalue()

    def test_published_json_carries_the_freshly_applied_weight(self):
        now = datetime.now(timezone.utc)
        rows = [{**r, "ts": now.isoformat()} for r in SONNET_ROWS] + [PROSE, OUTPUT]
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t, rows)
            rc, out, err = self._run(d, now, FakePost())
            self.assertEqual(rc, 0, err)
            self.assertIn("output weight: applied 1.8 (was 1.6)", out)
            j = json.loads((d / "out.json").read_text())
            self.assertEqual(j["api_price_per_mtok"]["claude-fable-5-1"]["class_weight"]["output"], 1.8)
            self.assertEqual(j["api_price_per_mtok"]["claude-sonnet-5"]["class_weight"]["output"], 1.8)
            self.assertNotIn(RECORD_KEY, j["api_price_per_mtok"])
            self.assertEqual(json.loads((d / "prices.json").read_text())["claude-fable-5-1"]["class_weight"]["output"], 1.8)

    def test_a_refused_weight_still_publishes_with_the_old_one(self):
        now = datetime.now(timezone.utc)
        rows = [{**r, "ts": now.isoformat()} for r in SONNET_ROWS] + [PROSE, WILD_OUTPUT]
        with tempfile.TemporaryDirectory() as t:
            d = self._files(t, rows)
            rc, out, err = self._run(d, now, FakePost())
            self.assertEqual(rc, 0, err)
            self.assertIn("output weight: refused 4.5", out)
            self.assertIn("alert: skipped", err)
            j = json.loads((d / "out.json").read_text())
            self.assertEqual(j["api_price_per_mtok"]["claude-fable-5-1"]["class_weight"]["output"], 1.6)


if __name__ == "__main__":
    unittest.main()
