"""tracker.rotate: which model probes next, what it should expect, and whether the
row it produced drifted.

The drift tests use the two real Sonnet rows from history/probes.jsonl: the 09:39
5-tick reading and the first burst-first row at 14:33. Drift is now compared in
meter dollars per 1% (tracker.publish.usd_per_pct), not raw tokens/1%, so these two
rows' dollar values (about $0.979 and $1.132, +16%) differ from their raw
tokens/1% ratio (467,778.6 vs 579,818.7, +24%): both are pure cache-write-heavy but
their class splits differ enough that the two measures disagree slightly.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tracker.probe import (
    FIXED_PROMPT_SPLIT,
    MAX_PAYLOAD_WORDS,
    MIN_PAYLOAD_WORDS,
    PROMPTS_PER_TICK,
    TOKENS_PER_WORD,
    payload_words_for,
    tokens_per_pct_for,
)
from tracker.publish import meter_usd, usd_per_pct
from tracker.rotate import (
    DRIFT_THRESHOLD,
    ROTATION,
    check_drift,
    decide,
    expectation,
    main,
    mark_outlier,
    next_model,
    prose_rows,
    usable_rows,
)

PRICES = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5,
                              "meter_weight": 1.0, "class_weight": {"output": 1.8}},
          "claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25,
                            "meter_weight": 1.0, "class_weight": {"output": 1.8}},
          "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 12.5,
                               "meter_weight": 1.0, "class_weight": {"output": 1.8}}}

# The two real rows (readings arrays dropped; they play no part here).
SONNET_0939 = {"ts": "2026-09-06T09:39:26.287942+00:00", "model": "claude-sonnet-5", "effort": "low",
               "tokens_per_pct": 467778.6,
               "tokens": {"input": 86, "output": 215, "cache_read": 415767, "cache_write": 1922825},
               "prompts": 48, "tick_from": 1, "tick_to": 6, "elapsed_s": 4742.686796, "account": "dave"}
SONNET_1433 = {"ts": "2026-09-06T14:33:05.254749+00:00", "model": "claude-sonnet-5", "effort": "low",
               "tokens_per_pct": 579818.6666666666,
               "tokens": {"input": 86, "output": 215, "cache_read": 415767, "cache_write": 1323388},
               "prompts": 63, "tick_from": 5, "tick_to": 8, "elapsed_s": 1569.897585,
               "payload": "prose", "payload_words": 12000, "ticks": 3, "skip": 1, "settle_s": 60,
               "expect_tokens_per_pct": 468000.0, "early_tick": False, "reset_start": False,
               "account": "dave"}


def row(ts, model, tpp, ticks=3, **extra):
    """A prose row whose token bundle is all cache_write, `tpp` tokens per 1% over `ticks`."""
    r = {"ts": ts, "model": model, "effort": "low", "tokens_per_pct": float(tpp),
         "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": int(tpp * ticks)},
         "prompts": 10, "tick_from": 1, "tick_to": 1 + ticks, "elapsed_s": 100.0, "account": "dave"}
    r.update(extra)
    return r


def write_history(tmp: str, rows: list[dict]) -> Path:
    p = Path(tmp) / "probes.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def write_prices(tmp: str) -> Path:
    p = Path(tmp) / "prices.json"
    p.write_text(json.dumps(dict(PRICES, _source="test")), encoding="utf-8")
    return p


def probe_row(ts, model, words, usd_per_pct, ticks=3):
    """The row a probe on `model` with a `words` payload writes when the meter charges
    `usd_per_pct` meter dollars per 1%: every prompt is the payload's cache_write plus the
    fixed per-prompt overhead (tracker.probe.FIXED_PROMPT_SPLIT), and the span holds as
    many such prompts as pay for `ticks` percent at that rate."""
    price = PRICES[model]
    per_prompt = dict(FIXED_PROMPT_SPLIT)
    per_prompt["cache_write"] += round(words * TOKENS_PER_WORD)
    prompts = ticks * usd_per_pct / (meter_usd(per_prompt, price) * price.get("meter_weight", 1.0))
    tokens = {c: round(n * prompts) for c, n in per_prompt.items()}
    return {"ts": ts, "model": model, "effort": "low", "tokens_per_pct": sum(tokens.values()) / ticks,
            "tokens": tokens, "prompts": round(prompts), "tick_from": 2, "tick_to": 2 + ticks,
            "elapsed_s": 100.0, "payload": "prose", "payload_words": words, "account": "dave"}


class RowFilterTests(unittest.TestCase):
    def test_a_row_without_a_payload_key_is_prose(self):
        # rows written before --payload existed have no key and were all prose
        rows = [SONNET_0939, dict(row("2026-09-06T12:00:00+00:00", "claude-fable-5-1", 30000), payload="output")]
        self.assertEqual(prose_rows(rows), [SONNET_0939])

    def test_usable_rows_drop_outliers_and_output_rows(self):
        out = row("2026-09-06T12:00:00+00:00", "claude-fable-5-1", 30000, payload="output")
        bad = row("2026-09-06T13:00:00+00:00", "claude-sonnet-5", 900000, outlier=True)
        self.assertEqual(usable_rows([SONNET_0939, out, bad, SONNET_1433]), [SONNET_0939, SONNET_1433])


class NextModelTests(unittest.TestCase):
    def test_rotation_order(self):
        self.assertEqual(ROTATION, ("claude-sonnet-5", "claude-opus-5", "claude-fable-5-1"))

    def test_next_after_sonnet_is_opus_then_fable_then_sonnet(self):
        self.assertEqual(next_model([row("2026-09-06T00:00:00+00:00", "claude-sonnet-5", 468000)]), "claude-opus-5")
        self.assertEqual(next_model([row("2026-09-06T00:00:00+00:00", "claude-opus-5", 200000)]), "claude-fable-5-1")
        self.assertEqual(next_model([row("2026-09-06T00:00:00+00:00", "claude-fable-5-1", 100000)]), "claude-sonnet-5")

    def test_empty_history_starts_at_sonnet(self):
        self.assertEqual(next_model([]), "claude-sonnet-5")

    def test_output_rows_do_not_move_the_rotation(self):
        rows = [row("2026-09-06T00:00:00+00:00", "claude-opus-5", 200000),
                row("2026-09-06T06:00:00+00:00", "claude-fable-5-1", 30000, payload="output")]
        self.assertEqual(next_model(rows), "claude-fable-5-1")

    def test_an_outlier_row_still_counts_as_that_models_turn(self):
        # the turn was taken even if the reading was thrown out
        rows = [row("2026-09-06T00:00:00+00:00", "claude-opus-5", 200000, outlier=True)]
        self.assertEqual(next_model(rows), "claude-fable-5-1")

    def test_unknown_last_model_restarts_at_sonnet(self):
        self.assertEqual(next_model([row("2026-09-06T00:00:00+00:00", "claude-haiku-4-5", 1)]), "claude-sonnet-5")

    def test_uses_the_latest_row_by_timestamp_not_file_order(self):
        rows = [row("2026-09-06T12:00:00+00:00", "claude-opus-5", 200000),
                row("2026-09-06T00:00:00+00:00", "claude-sonnet-5", 468000)]
        self.assertEqual(next_model(rows), "claude-fable-5-1")


class ExpectationTests(unittest.TestCase):
    """The expectation is meter dollars per 1% (the invariant); the payload and the token
    expectation that sizes the bursts both follow from it and the target model's price
    alone, never from the previous row's class split (issue #24)."""

    def test_same_model_round_trips_its_own_reading(self):
        e = expectation([SONNET_0939], "claude-sonnet-5", PRICES)
        self.assertAlmostEqual(e.usd_per_pct, usd_per_pct(SONNET_0939, PRICES["claude-sonnet-5"]), delta=1e-9)
        self.assertEqual(e.payload_words, payload_words_for(e.usd_per_pct, PRICES["claude-sonnet-5"]))
        self.assertEqual(e.tokens_per_pct, tokens_per_pct_for(e.payload_words))

    def test_other_model_gets_the_same_dollars_and_its_own_payload(self):
        # the dollar median is model-agnostic; Opus at 2.5x Sonnet's cache_write price
        # gets a smaller payload for the same twelfth of a tick
        sonnet = expectation([SONNET_0939], "claude-sonnet-5", PRICES)
        opus = expectation([SONNET_0939], "claude-opus-5", PRICES)
        self.assertEqual(opus.usd_per_pct, sonnet.usd_per_pct)
        self.assertEqual(opus.payload_words, payload_words_for(opus.usd_per_pct, PRICES["claude-opus-5"]))
        self.assertLess(opus.payload_words, sonnet.payload_words)
        self.assertEqual(opus.tokens_per_pct, tokens_per_pct_for(opus.payload_words))

    def test_meter_weight_of_the_target_model_shrinks_its_payload_not_the_dollars(self):
        prices = json.loads(json.dumps(PRICES))
        prices["claude-fable-5-1"]["meter_weight"] = 2.0
        heavy = expectation([SONNET_0939], "claude-fable-5-1", prices)
        light = expectation([SONNET_0939], "claude-fable-5-1", PRICES)
        self.assertEqual(heavy.usd_per_pct, light.usd_per_pct)
        self.assertEqual(heavy.payload_words, payload_words_for(light.usd_per_pct, prices["claude-fable-5-1"]))
        self.assertLess(heavy.payload_words, light.payload_words)

    def test_median_of_the_last_four_usable_rows_in_dollars(self):
        # five Sonnet rows; the oldest is out of the lookback, the median of the last
        # four (100k, 100k, 100k, 900k tokens/1%, all cache_write) is 100k x $2.5/1e6
        rows = [row(f"2026-09-0{i}T00:00:00+00:00", "claude-sonnet-5", v)
                for i, v in enumerate([5000, 100000, 100000, 900000, 100000], start=1)]
        self.assertAlmostEqual(expectation(rows, "claude-sonnet-5", PRICES).usd_per_pct, 100000 * 2.5e-6, delta=1e-9)

    def test_outlier_and_output_rows_are_ignored(self):
        rows = [row("2026-09-01T00:00:00+00:00", "claude-sonnet-5", 100000),
                row("2026-09-02T00:00:00+00:00", "claude-sonnet-5", 900000, outlier=True),
                row("2026-09-03T00:00:00+00:00", "claude-sonnet-5", 30000, payload="output")]
        self.assertAlmostEqual(expectation(rows, "claude-sonnet-5", PRICES).usd_per_pct, 100000 * 2.5e-6, delta=1e-9)

    def test_payload_does_not_depend_on_the_previous_rows_class_split(self):
        # Two histories reading the same meter dollars per 1% on Fable, one from a
        # 12,000-word payload and one from a 500-word one (cache_read about 27% against
        # 87% of the tokens): the next Fable probe is sized the same from either. The
        # old token conversion divided the dollars by each history's own blended price
        # and sized the second history's payload 18x the first's.
        big = probe_row("2026-09-02T00:00:00+00:00", "claude-fable-5-1", 12000, 0.96)
        small = probe_row("2026-09-02T00:00:00+00:00", "claude-fable-5-1", 500, 0.96)
        self.assertEqual(expectation([big], "claude-fable-5-1", PRICES).payload_words,
                         expectation([small], "claude-fable-5-1", PRICES).payload_words)

    def test_no_usable_rows_is_none(self):
        self.assertIsNone(expectation([], "claude-sonnet-5", PRICES))
        self.assertIsNone(expectation([row("2026-09-01T00:00:00+00:00", "claude-sonnet-5", 1, outlier=True)],
                                      "claude-sonnet-5", PRICES))

    def test_rows_for_unpriced_models_are_skipped(self):
        rows = [row("2026-09-01T00:00:00+00:00", "claude-haiku-4-5", 5), SONNET_0939]
        self.assertAlmostEqual(expectation(rows, "claude-sonnet-5", PRICES).usd_per_pct,
                               usd_per_pct(SONNET_0939, PRICES["claude-sonnet-5"]), delta=1e-9)


class PayloadConvergenceTests(unittest.TestCase):
    """Regression for issue #24. Each generation sizes a payload from the history, the
    probe writes the row a meter charging a constant METER dollars per 1% would give
    for that payload, and the next generation is sized from the history with that row
    in it. The size must settle, not chase the previous row's class split: sized in
    tokens through that split, a smaller payload raised the cache_read share of the
    next row, which changed the blended price the next expectation was divided by,
    which resized the payload again -- Fable flipped between MIN_PAYLOAD_WORDS and
    about 8,900 words every generation and Opus swung by a third for six or more."""
    METER = 0.96  # meter dollars per 1%: where the real rows cluster (0.68-1.13)
    GENERATIONS = 6

    def _generations(self, model):
        rows = [probe_row("2026-09-01T00:00:00+00:00", "claude-sonnet-5", 12000, self.METER)]
        sizes = []
        for g in range(self.GENERATIONS):
            e = expectation(rows, model, PRICES)
            sizes.append(e.payload_words)
            rows.append(probe_row(f"2026-09-{g + 2:02d}T00:00:00+00:00", model, e.payload_words, self.METER))
        return sizes

    def test_payload_size_is_a_fixed_point_for_every_model(self):
        for model in ROTATION:
            sizes = self._generations(model)
            # feeding a generation's own row back returns the same size (a word of
            # rounding allowed), and the fixed point is a real size, not a clamp
            self.assertLessEqual(max(sizes) - min(sizes), 1, (model, sizes))
            self.assertGreater(min(sizes), MIN_PAYLOAD_WORDS, (model, sizes))
            self.assertLess(max(sizes), MAX_PAYLOAD_WORDS, (model, sizes))

    def test_every_generation_prices_one_prompt_at_a_twelfth_of_a_tick(self):
        for model in ROTATION:
            for words in self._generations(model):
                per_prompt = dict(FIXED_PROMPT_SPLIT)
                per_prompt["cache_write"] += round(words * TOKENS_PER_WORD)
                self.assertAlmostEqual(PROMPTS_PER_TICK * meter_usd(per_prompt, PRICES[model]), self.METER,
                                       delta=self.METER * 0.005, msg=model)


class DriftTests(unittest.TestCase):
    def test_threshold_is_fifteen_percent(self):
        self.assertEqual(DRIFT_THRESHOLD, 0.15)

    def test_the_first_burst_first_sonnet_row_drifted_against_the_0939_reading(self):
        # real data: the two rows differ in class split (see module docstring), so
        # the dollar ratio (+16%) differs from the raw tokens/1% ratio (+24%) but is
        # still outside the 15% band
        want_median = usd_per_pct(SONNET_0939, PRICES["claude-sonnet-5"])
        want_value = usd_per_pct(SONNET_1433, PRICES["claude-sonnet-5"])
        d = check_drift([SONNET_0939, SONNET_1433], PRICES)
        self.assertTrue(d.drifted)
        self.assertAlmostEqual(d.median, want_median, delta=1e-9)
        self.assertAlmostEqual(d.value, want_value, delta=1e-9)
        self.assertEqual(round(d.ratio * 100), 16)
        self.assertEqual(d.model, "claude-sonnet-5")

    def test_the_full_real_history_still_calls_it_drift(self):
        # with the 23:04 (490,713) and 04:07 (272,631) tokens/1% rows in front, the
        # median of the three prior Sonnet prose rows is still the 09:39 reading
        rows = [row("2026-09-05T23:04:47+00:00", "claude-sonnet-5", 490713.0),
                row("2026-09-06T04:07:08+00:00", "claude-sonnet-5", 272631.0),
                SONNET_0939,
                row("2026-09-06T11:16:29+00:00", "claude-fable-5-1", 117896.8),
                row("2026-09-06T12:06:58+00:00", "claude-fable-5-1", 31724.4, payload="output"),
                SONNET_1433]
        d = check_drift(rows, PRICES)
        self.assertTrue(d.drifted)
        self.assertAlmostEqual(d.median, usd_per_pct(SONNET_0939, PRICES["claude-sonnet-5"]), delta=1e-9)

    def test_within_band_is_not_drift(self):
        d = check_drift([SONNET_0939, row("2026-09-07T00:00:00+00:00", "claude-sonnet-5", 420000)], PRICES)
        self.assertFalse(d.drifted)
        self.assertLess(abs(d.ratio), DRIFT_THRESHOLD)

    def test_exactly_fifteen_percent_is_not_drift(self):
        # chosen so the dollar conversion (tokens -> meter_usd -> /1%) lands at
        # exactly 0.15 rather than a hair over, unlike 100000/115000 (float rounding)
        d = check_drift([row("2026-09-06T00:00:00+00:00", "claude-sonnet-5", 80000),
                         row("2026-09-07T00:00:00+00:00", "claude-sonnet-5", 92000)], PRICES)
        self.assertFalse(d.drifted)

    def test_drift_downwards(self):
        d = check_drift([row("2026-09-06T00:00:00+00:00", "claude-sonnet-5", 100000),
                         row("2026-09-07T00:00:00+00:00", "claude-sonnet-5", 80000)], PRICES)
        self.assertTrue(d.drifted)
        self.assertAlmostEqual(d.ratio, -0.2)

    def test_median_is_of_that_models_own_last_four_prose_rows(self):
        rows = [row("2026-09-01T00:00:00+00:00", "claude-sonnet-5", 1000),  # outside the lookback
                row("2026-09-02T00:00:00+00:00", "claude-sonnet-5", 100000),
                row("2026-09-02T12:00:00+00:00", "claude-opus-5", 40000),  # other model
                row("2026-09-03T00:00:00+00:00", "claude-sonnet-5", 100000),
                row("2026-09-04T00:00:00+00:00", "claude-sonnet-5", 900000, outlier=True),  # flagged
                row("2026-09-04T12:00:00+00:00", "claude-sonnet-5", 20000, payload="output"),  # output
                row("2026-09-05T00:00:00+00:00", "claude-sonnet-5", 110000),
                row("2026-09-06T00:00:00+00:00", "claude-sonnet-5", 120000),
                row("2026-09-07T00:00:00+00:00", "claude-sonnet-5", 130000)]
        d = check_drift(rows, PRICES)
        # median of 100k, 100k, 110k, 120k tokens/1% -> 105000 * $2.5/1e6 cache_write price
        self.assertAlmostEqual(d.median, 105000 * 2.5e-6)
        self.assertTrue(d.drifted)  # 130k / 105k = +24%

    def test_first_row_for_a_model_cannot_drift(self):
        d = check_drift([SONNET_0939, row("2026-09-07T00:00:00+00:00", "claude-opus-5", 200000)], PRICES)
        self.assertFalse(d.drifted)
        self.assertIsNone(d.median)

    def test_an_output_row_is_never_checked(self):
        rows = [SONNET_0939, row("2026-09-07T00:00:00+00:00", "claude-sonnet-5", 20000, payload="output")]
        self.assertIsNone(check_drift(rows, PRICES))

    def test_empty_history_is_none(self):
        self.assertIsNone(check_drift([], PRICES))

    def test_a_model_with_no_price_cannot_be_checked(self):
        rows = [row("2026-09-06T00:00:00+00:00", "claude-haiku-4-5", 100000),
                row("2026-09-07T00:00:00+00:00", "claude-haiku-4-5", 150000)]
        self.assertIsNone(check_drift(rows, PRICES))

    def test_different_splits_equal_dollars_is_not_drift(self):
        # two same-model rows whose token-class splits differ (cache_read 27% vs 51%
        # of a cache-write-heavy split) but whose meter dollars/1% agree closely
        cheap_split = row("2026-09-06T00:00:00+00:00", "claude-fable-5-1", 1)
        cheap_split["tokens"] = {"input": 28, "output": 70, "cache_read": 160622, "cache_write": 428764}
        cheap_split["tick_from"], cheap_split["tick_to"] = 8, 13
        rich_split = row("2026-09-07T00:00:00+00:00", "claude-fable-5-1", 1)
        rich_split["tokens"] = {"input": 48, "output": 120, "cache_read": 275352, "cache_write": 261782}
        rich_split["tick_from"], rich_split["tick_to"] = 3, 6
        d = check_drift([cheap_split, rich_split], PRICES)
        # tokens/1% differ by +52% (117,896.8 vs 179,100.7) but dollars/1% only +3%
        self.assertLess(abs(d.ratio), DRIFT_THRESHOLD)
        self.assertFalse(d.drifted)

    def test_equal_splits_thirty_percent_token_difference_is_drift(self):
        rows = [row("2026-09-06T00:00:00+00:00", "claude-fable-5-1", 100000),
                row("2026-09-07T00:00:00+00:00", "claude-fable-5-1", 130000)]
        d = check_drift(rows, PRICES)
        self.assertTrue(d.drifted)
        self.assertAlmostEqual(d.ratio, 0.3, delta=0.01)


class DecideTests(unittest.TestCase):
    """After a drift, the rerun's row is last and the drifted row is the same model's
    previous prose row. The earlier median is taken from the rows before the drifted one.

    Judged in meter dollars/1%: the earlier median is $0.979 (SONNET_0939) and the
    drifted reading is $1.132 (SONNET_1433). The rerun rows below are pure cache-write,
    so their dollars/1% is tpp * $2.5/1e6 (the sonnet cache_write price)."""

    def _rows(self, rerun_tpp):
        return [SONNET_0939, SONNET_1433, row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", rerun_tpp, ticks=2)]

    def test_rerun_agreeing_with_the_median_makes_the_drifted_row_an_outlier(self):
        v = decide(self._rows(391541), PRICES)  # $0.979/1%, agrees with the earlier median
        self.assertEqual(v.verdict, "outlier")
        self.assertEqual(v.outlier_ts, SONNET_1433["ts"])
        self.assertAlmostEqual(v.median, usd_per_pct(SONNET_0939, PRICES["claude-sonnet-5"]), delta=1e-6)
        self.assertAlmostEqual(v.first, usd_per_pct(SONNET_1433, PRICES["claude-sonnet-5"]), delta=1e-6)
        self.assertAlmostEqual(v.rerun, 391541 * 2.5e-6, delta=1e-9)

    def test_rerun_agreeing_with_the_drifted_row_is_a_change(self):
        v = decide(self._rows(452755), PRICES)  # $1.132/1%, agrees with the drifted reading
        self.assertEqual(v.verdict, "change")
        self.assertIsNone(v.outlier_ts)
        self.assertEqual(round(v.ratio * 100), 16)  # mean of the two readings against the median
        self.assertEqual(v.direction, "increased")

    def test_change_downwards(self):
        rows = [row("2026-09-05T00:00:00+00:00", "claude-sonnet-5", 100000),
                row("2026-09-06T00:00:00+00:00", "claude-sonnet-5", 70000),
                row("2026-09-06T02:00:00+00:00", "claude-sonnet-5", 72000, ticks=2)]
        v = decide(rows, PRICES)
        self.assertEqual((v.verdict, v.direction), ("change", "decreased"))

    def test_rerun_agreeing_with_neither_is_inconclusive_and_flags_nothing(self):
        v = decide(self._rows(1174622), PRICES)  # about 3x the earlier median in dollars
        self.assertEqual(v.verdict, "inconclusive")
        self.assertIsNone(v.outlier_ts)

    def test_rerun_closer_to_the_median_wins_when_it_agrees_with_both(self):
        # 417,557 tpp ($1.044/1%) is within 15% of both the median and the drifted
        # reading: the earlier median is the null hypothesis, so the drifted row is
        # the outlier
        v = decide(self._rows(417557), PRICES)
        self.assertEqual(v.verdict, "outlier")

    def test_rerun_on_a_different_model_is_an_error(self):
        rows = [SONNET_0939, SONNET_1433, row("2026-09-06T16:00:00+00:00", "claude-opus-5", 200000)]
        with self.assertRaises(ValueError):
            decide(rows, PRICES)

    def test_no_earlier_median_is_an_error(self):
        rows = [SONNET_1433, row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 470000)]
        with self.assertRaises(ValueError):
            decide(rows, PRICES)

    def test_model_with_no_price_is_an_error(self):
        rows = [row("2026-09-05T00:00:00+00:00", "claude-haiku-4-5", 100000),
                row("2026-09-06T00:00:00+00:00", "claude-haiku-4-5", 100000),
                row("2026-09-06T02:00:00+00:00", "claude-haiku-4-5", 100000, ticks=2)]
        with self.assertRaises(ValueError):
            decide(rows, PRICES)


class MarkOutlierTests(unittest.TestCase):
    def test_marks_the_row_in_place_and_leaves_every_other_line_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_history(d, [SONNET_0939, SONNET_1433])
            before = path.read_text(encoding="utf-8").splitlines()
            mark_outlier(path, SONNET_1433["ts"])
            after = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(after[0], before[0])
        marked = json.loads(after[1])
        self.assertTrue(marked["outlier"])
        del marked["outlier"]
        self.assertEqual(marked, SONNET_1433)
        self.assertEqual(len(after), 2)

    def test_unknown_ts_is_an_error_and_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_history(d, [SONNET_0939])
            text = path.read_text(encoding="utf-8")
            with self.assertRaises(ValueError):
                mark_outlier(path, "2030-01-01T00:00:00+00:00")
            self.assertEqual(path.read_text(encoding="utf-8"), text)


class CliTests(unittest.TestCase):
    def _run(self, argv, rows):
        with tempfile.TemporaryDirectory() as d:
            history = write_history(d, rows)
            prices = write_prices(d)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = main(argv + ["--history", str(history), "--prices", str(prices)])
            return rc, out.getvalue(), err.getvalue(), history.read_text(encoding="utf-8")

    def test_plan_prints_the_flags_for_every_model_and_names_the_next(self):
        rc, out, _err, _ = self._run(["plan"], [SONNET_0939])
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        for model in ROTATION:
            self.assertTrue(any(f"--model {model} --expect-usd-per-pct " in ln for ln in lines), model)
        # the one Sonnet row is $0.9789/1%, and the dollars are the same for every model;
        # the dry run also says what that buys: the payload and the token expectation
        self.assertIn("--model claude-sonnet-5 --expect-usd-per-pct 0.9789", out)
        self.assertIn("--model claude-opus-5 --expect-usd-per-pct 0.9789", out)
        self.assertIn(f"{payload_words_for(0.978852, PRICES['claude-opus-5'])} words", out)
        self.assertIn("tokens per 1%", out)
        self.assertEqual(sum("next" in ln for ln in lines), 1)
        self.assertIn("next", next(ln for ln in lines if "claude-opus-5" in ln))

    def test_plan_is_the_default_subcommand(self):
        rc, out, _err, _ = self._run([], [SONNET_0939])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 3)

    def test_next_prints_the_model(self):
        rc, out, _, _ = self._run(["next"], [SONNET_0939])
        self.assertEqual((rc, out.strip()), (0, "claude-opus-5"))

    def test_flags_prints_one_shell_splittable_line_for_the_next_model(self):
        rc, out, _, _ = self._run(["flags"], [SONNET_0939])
        self.assertEqual((rc, out.strip()), (0, "--model claude-opus-5 --expect-usd-per-pct 0.9789"))

    def test_flags_for_a_named_model(self):
        rc, out, _, _ = self._run(["flags", "--model", "claude-sonnet-5"], [SONNET_0939])
        self.assertEqual((rc, out.strip()), (0, "--model claude-sonnet-5 --expect-usd-per-pct 0.9789"))

    def test_flags_with_no_usable_history_fails_loudly(self):
        rc, out, err, _ = self._run(["flags"], [])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no usable prose row", err)

    def test_flags_rerun_targets_the_drifted_rows_model_with_two_ticks(self):
        rc, out, _, _ = self._run(["flags", "--rerun"], [SONNET_0939, SONNET_1433])
        self.assertEqual(rc, 0)
        # the smaller of the median ($0.979) and the drifted reading ($1.132), in
        # dollars, so the burst cannot overshoot whichever of the two turns out to be true
        self.assertEqual(out.strip(), "--model claude-sonnet-5 --expect-usd-per-pct 0.9789 --ticks 2")

    def test_flags_rerun_after_a_downward_drift_uses_the_lower_reading(self):
        rows = [row("2026-09-06T00:00:00+00:00", "claude-sonnet-5", 100000),
                row("2026-09-07T00:00:00+00:00", "claude-sonnet-5", 80000)]
        rc, out, _, _ = self._run(["flags", "--rerun"], rows)
        self.assertEqual((rc, out.strip()), (0, "--model claude-sonnet-5 --expect-usd-per-pct 0.2000 --ticks 2"))

    def test_check_reports_drift_with_exit_10(self):
        rc, out, _, _ = self._run(["check"], [SONNET_0939, SONNET_1433])
        self.assertEqual(rc, 10)
        self.assertTrue(out.startswith("drift claude-sonnet-5 "), out)
        self.assertIn("$1.132/1%", out)
        self.assertIn("$0.979/1%", out)
        self.assertIn("+16%", out)

    def test_check_reports_ok_with_exit_0(self):
        # $1.00/1% against a median of $0.979/1% (+2%): well within band
        rc, out, _, _ = self._run(["check"], [SONNET_0939, row("2026-09-07T00:00:00+00:00", "claude-sonnet-5", 400000)])
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("ok claude-sonnet-5 "), out)

    def test_check_with_nothing_to_compare_is_ok(self):
        rc, out, _, _ = self._run(["check"], [SONNET_0939])
        self.assertEqual(rc, 0)
        self.assertIn("no earlier", out)
        rc, _, _, _ = self._run(["check"], [])
        self.assertEqual(rc, 0)

    def test_decide_outlier_marks_the_row_and_says_so(self):
        rows = [SONNET_0939, SONNET_1433, row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 391541, ticks=2)]
        rc, out, _, text = self._run(["decide"], rows)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("outlier claude-sonnet-5 "), out)
        lines = [json.loads(ln) for ln in text.splitlines()]
        self.assertEqual([r.get("outlier", False) for r in lines], [False, True, False])

    def test_decide_change_marks_nothing(self):
        rows = [SONNET_0939, SONNET_1433, row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 452755, ticks=2)]
        rc, out, _, text = self._run(["decide"], rows)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("change claude-sonnet-5 "), out)
        self.assertIn("increased", out)
        self.assertNotIn("outlier", text)

    def test_decide_inconclusive_marks_nothing(self):
        rows = [SONNET_0939, SONNET_1433, row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 800000, ticks=2)]
        rc, out, _, text = self._run(["decide"], rows)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("inconclusive claude-sonnet-5 "), out)
        self.assertNotIn("outlier", text)

    def test_decide_without_a_rerun_pair_fails(self):
        rc, _out, err, _ = self._run(["decide"], [SONNET_0939, row("2026-09-07T00:00:00+00:00", "claude-opus-5", 200000)])
        self.assertEqual(rc, 1)
        self.assertIn("decide:", err)

    def test_missing_history_file_is_an_empty_history_for_plan_and_an_error_for_flags(self):
        with tempfile.TemporaryDirectory() as d:
            prices = write_prices(d)
            missing = str(Path(d) / "nope.jsonl")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc_plan = main(["plan", "--history", missing, "--prices", str(prices)])
                rc_flags = main(["flags", "--history", missing, "--prices", str(prices)])
        self.assertEqual(rc_plan, 0)
        self.assertIn("next", out.getvalue())
        self.assertEqual(rc_flags, 1)


if __name__ == "__main__":
    unittest.main()
