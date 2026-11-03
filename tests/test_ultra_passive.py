"""Defects found in tracker/gs_passive.py, tracker/passive.py and tracker/meter_log.py (2026-11-03)."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker.gs_passive import passive_dollar_readings
from tracker.meter_log import EXIT_OK, EXIT_REFUSED, sample

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
PRICES = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5}}
EMPTY = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def _stretch(end: datetime, tokens: dict, delta: float = 10.0) -> dict:
    return {"start": (end - timedelta(hours=1)).isoformat(), "end": end.isoformat(), "delta_pct": delta,
            "windows": 1, "tokens": tokens, "unpriced_tokens": 0, "status": "accepted",
            "reset_verified": True}


class EmptySyntheticBundleTest(unittest.TestCase):
    """Claude Code's `<synthetic>` placeholder rows carry no tokens and no price. A stretch that
    holds one beside real work is still a reading: on the real 2026-11-02 record 59 of dave's
    223 accepted, reset-verified stretches carried such a bundle and every one of them was left
    out of the published dollar series, while the credit series (`passive_credit_points`)
    already kept them."""

    def test_an_empty_synthetic_bundle_does_not_drop_the_stretch(self):
        work = {"claude-sonnet-5": dict(EMPTY, cache_write=4_000_000)}  # $10 of meter value
        report = {"accounts": {"dave": {"stretches": [
            _stretch(T0, work),
            _stretch(T0 + timedelta(hours=2), {**work, "<synthetic>": dict(EMPTY)}),
        ]}}}
        readings = passive_dollar_readings(report, PRICES, by="stretch")
        self.assertEqual([(t, round(v, 6)) for t, v in readings],
                         [(T0, 100.0), (T0 + timedelta(hours=2), 100.0)])

    def test_a_synthetic_bundle_with_tokens_still_drops_the_stretch(self):
        work = {"claude-sonnet-5": dict(EMPTY, cache_write=4_000_000)}
        report = {"accounts": {"dave": {"stretches": [
            _stretch(T0, {**work, "<synthetic>": dict(EMPTY, output=50)})]}}}
        self.assertEqual(passive_dollar_readings(report, PRICES, by="stretch"), [])

    def test_a_stretch_of_only_an_empty_synthetic_bundle_is_no_reading(self):
        # Nothing priced explains the meter's movement: no $0 reading.
        report = {"accounts": {"dave": {"stretches": [_stretch(T0, {"<synthetic>": dict(EMPTY)})]}}}
        self.assertEqual(passive_dollar_readings(report, PRICES, by="stretch"), [])
        self.assertEqual(passive_dollar_readings(report, PRICES, by="day"), [])


class FitOutputWeightSyntheticTest(unittest.TestCase):
    """The same empty `<synthetic>` bundle kept 60 of the 385 capture-accepted stretches on the
    real 2026-11-02 record (49 of dave's 159) out of `fit_output_weight`."""

    def test_an_empty_synthetic_bundle_does_not_drop_the_stretch_from_the_fit(self):
        from tracker.gs_passive import fit_output_weight
        cw = {"claude-sonnet-5": dict(EMPTY, cache_write=800_000)}  # $2 of cache_write
        mixed = {"claude-sonnet-5": dict(EMPTY, output=100_000, cache_write=400_000)}  # $1 + $1
        rpt = {"accounts": {"x": {"stretches": [
            {"status": "accepted", "delta_pct": 10, "tokens": cw},
            {"status": "accepted", "delta_pct": 10, "tokens": mixed},
            {"status": "accepted", "delta_pct": 10, "tokens": {**mixed, "<synthetic>": dict(EMPTY)}},
        ]}}}
        fit = fit_output_weight(rpt, PRICES)
        self.assertEqual(fit["n"], 3)
        self.assertAlmostEqual(fit["output_weight"], 1.0, places=3)
        # One with tokens in it still cannot be valued, so that stretch stays out.
        rpt["accounts"]["x"]["stretches"][2]["tokens"]["<synthetic>"] = dict(EMPTY, output=5)
        self.assertEqual(fit_output_weight(rpt, PRICES)["n"], 2)


    def test_a_stretch_with_no_work_left_is_not_a_zero_row(self):
        # Review of 0a9515d6: a stretch whose only bundles are empty `<synthetic>` ones (or that
        # holds no bundle at all) is a collection gap, as passive_dollar_readings reads it, not a
        # (0, 0, delta_pct) row that moves n and the residual.
        from tracker.gs_passive import fit_output_weight
        cw = {"claude-sonnet-5": dict(EMPTY, cache_write=800_000)}
        mixed = {"claude-sonnet-5": dict(EMPTY, output=100_000, cache_write=400_000)}
        rpt = {"accounts": {"x": {"stretches": [
            {"status": "accepted", "delta_pct": 10, "tokens": cw},
            {"status": "accepted", "delta_pct": 10, "tokens": mixed},
            {"status": "accepted", "delta_pct": 10, "tokens": {"<synthetic>": dict(EMPTY)}},
            {"status": "accepted", "delta_pct": 10, "tokens": {}},
        ]}}}
        fit = fit_output_weight(rpt, PRICES)
        self.assertEqual(fit["n"], 2)
        self.assertEqual(fit["residual_rms_pct"], 0.0)

class MeterLogBadByteTest(unittest.TestCase):
    """One undecodable byte near the top of a meter log (a disk fault, a stray write) crashed
    every tick in `log_owner` with UnicodeDecodeError: the text reader decodes a whole chunk at
    once, so a bad byte anywhere in the first few kilobytes stopped the sampler for good. Every
    other reader of the log (`_last_line`, tracker/samples.py) lets a bad byte cost its line."""

    NOW = datetime(2026, 11, 1, 12, 0, 5, tzinfo=timezone.utc)
    BODY = {"five_hour": {"utilization": 12.0, "resets_at": "2026-11-01T15:00:00+00:00"},
            "seven_day": {"utilization": 30.0, "resets_at": "2026-11-05T23:00:00+00:00"}}

    def _cfg(self, root: Path) -> Path:
        cfg = root / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-dave"}}))
        (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "not-a-token"}}))
        return cfg

    def _log(self, root: Path, identity: str) -> Path:
        log = root / "meter.log"
        first = {"ts": (self.NOW - timedelta(hours=1)).isoformat(), "account": "dave", "identity": identity,
                 **self.BODY}
        log.write_bytes((json.dumps(first) + "\n").encode() + b"\xff\xfe torn\n")
        return log

    def test_a_bad_byte_costs_its_line_and_the_tick_still_samples(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = self._cfg(root)
            log = self._log(root, hashlib.sha256(b"uuid-dave").hexdigest()[:12])
            code = sample(cfg, "dave", log, fetch=lambda url, headers: self.BODY, now=lambda: self.NOW)
            self.assertEqual(code, EXIT_OK)
            last = json.loads(log.read_bytes().splitlines()[-1])
            self.assertEqual(last["ts"], self.NOW.isoformat(timespec="seconds"))

    def test_a_bad_byte_does_not_hide_another_accounts_log(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = self._cfg(root)
            log = self._log(root, hashlib.sha256(b"uuid-someone-else").hexdigest()[:12])
            before = log.read_bytes()
            self.assertEqual(sample(cfg, "dave", log, fetch=lambda url, headers: self.BODY,
                                    now=lambda: self.NOW), EXIT_REFUSED)
            self.assertEqual(log.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()


class NaiveStretchEndTests(unittest.TestCase):
    def test_a_naive_end_beside_aware_ones_is_utc_not_a_crash(self):
        # Code review round 7: one naive `end` made sorted() raise over the whole series.
        from tracker.gs_passive import passive_dollar_readings
        tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 400000}

        def st(end):
            return {"status": "accepted", "capture_status": "accepted", "reset_verified": True,
                    "start": end, "end": end, "delta_pct": 10, "windows": 1, "tokens": {"claude-sonnet-5": tok}}
        report = {"accounts": {"jwork": {"stretches": [st("2026-11-02T23:00:00"), st("2026-11-01T10:00:00+02:00")]}}}
        got = passive_dollar_readings(report, PRICES)
        self.assertEqual([ts.isoformat() for ts, _ in got],
                         ["2026-11-01T10:00:00+02:00", "2026-11-02T23:00:00+00:00"])
