"""Drives the real collector (tracker.meter_log.sample -> tracker.usage_api's urllib
fetch) against tests/chaos/fake_api.py with a fake clock, and checks the log it
writes, plus what the passive join (tracker.samples / tracker.join) reads from it,
against the simulated meter's ground truth.

The only seams used are ones production already exposes: `sample(fetch=, now=)`.
The fetch is production's own NO_RETRY_FETCH with the URL pointed at the fake;
its hard-coded 30 s urlopen timeout is shortened here (and the host checked) so a
timeout fault costs 0.3 s, not 30.
"""
from __future__ import annotations

import json
import random
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fake_api import FakeUsageAPI, SimMeter

from tracker import meter_log
from tracker.join import window_points
from tracker.samples import parse_log
from tracker.usage_api import same_reset

T0 = datetime(2026, 10, 1, 22, 0, 0, tzinfo=timezone.utc)
CLIENT_TIMEOUT_S = 0.3
TOKEN = "chaos-token-A"
#: Faults that make a request and must leave an error (gap) line, never a reading.
REQUEST_FAULTS = ("401", "429", "500", "502", "503", "timeout", "reset", "malformed",
                  "truncated", "missing", "null", "list")


@dataclass
class Tick:
    at: datetime
    fault: str | None
    rc: int | None = None
    crashed: BaseException | None = None
    requested: bool = False
    truth_five: float = 0.0
    truth_five_reset: datetime | None = None
    truth_seven_reset: datetime | None = None


@dataclass
class Rig:
    root: Path
    seed: int = 1
    clock: datetime = T0
    ticks: list[Tick] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cfg = self.root / "cfg"
        self.cfg.mkdir(parents=True, exist_ok=True)
        (self.cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-chaos"}}))
        self.set_token(TOKEN)
        self.log = self.root / "data" / "meter-chaos.log"
        self.meter = SimMeter(self.clock)
        self.api = FakeUsageAPI(self.meter, {TOKEN})
        self.rng = random.Random(self.seed)

    def set_token(self, tok: str) -> None:
        (self.cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": tok}}))

    # -- the fetch production would use, aimed at the fake ------------------------
    def fetch(self, url: str, headers: dict) -> dict:
        assert url == meter_log.read_usage.__globals__["USAGE_URL"]
        return meter_log.NO_RETRY_FETCH(self.api.url, headers)

    @contextmanager
    def running(self):
        real = urllib.request.urlopen

        def urlopen(req, timeout=None, **kw):
            full = req.full_url if hasattr(req, "full_url") else req
            assert full.startswith("http://127.0.0.1:"), full
            return real(req, timeout=CLIENT_TIMEOUT_S, **kw)

        with self.api, mock.patch.object(urllib.request, "urlopen", urlopen):
            yield self

    # -- one scheduler tick ---------------------------------------------------------
    def tick(self, fault: str | None = None, step: timedelta = timedelta(minutes=2), burn: float = 0.5,
             now: datetime | None = None, fault_arg: float = 120) -> Tick:
        self.clock += step
        self.meter.advance(self.clock, burn)
        self.api.jitter_s = self.rng.uniform(-3, 3)
        self.api.next_fault, self.api.fault_arg = fault, fault_arg
        before = self.api.requests
        stamp = now or self.clock
        t = Tick(stamp, fault, truth_five=self.meter.five.used,
                 truth_five_reset=self.meter.five.resets_at, truth_seven_reset=self.meter.seven.resets_at)
        try:
            t.rc = meter_log.sample(self.cfg, "chaos", self.log, fetch=self.fetch, now=lambda: stamp)
        except BaseException as e:  # noqa: BLE001 - a crash is what the harness records
            t.crashed = e
        t.requested = self.api.requests > before
        self.api.next_fault = None
        self.ticks.append(t)
        return t

    # -- reading the result back ----------------------------------------------------
    def lines(self) -> list[dict]:
        if not self.log.exists():
            return []
        out = []
        for raw in self.log.read_text(encoding="utf-8").splitlines():
            out.append(json.loads(raw))  # an unparseable line is itself a failure
        return out

    def readings(self) -> list[dict]:
        return [d for d in self.lines() if (d.get("five_hour") or {}).get("utilization") is not None]

    def samples(self):
        with open(self.log, encoding="utf-8") as fh:
            return parse_log("meter", fh)


def stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def check_invariants(rig: Rig) -> None:
    """Assert every soak invariant; raises AssertionError naming the first broken one."""
    crashes = [t for t in rig.ticks if t.crashed]
    assert not crashes, f"no crash: {len(crashes)} ticks raised, first {crashes[0].crashed!r}"
    assert all(t.rc in (0, 2, 4) for t in rig.ticks), "exit codes are only 0/2/4"

    lines = rig.lines()
    stamps = [d["ts"] for d in lines]
    parsed = [datetime.fromisoformat(s) for s in stamps]
    assert all(a < b for a, b in zip(parsed, parsed[1:])), "monotonic timestamps in the log"

    readings = rig.readings()
    rts = [d["ts"] for d in readings]
    assert len(rts) == len(set(rts)), "no duplicate reading rows"

    by_stamp = {stamp(t.at): t for t in rig.ticks}
    for d in readings:
        t = by_stamp[d["ts"]]
        assert t.fault is None, f"gap is a gap: a reading was written on a {t.fault} tick at {d['ts']}"
        assert abs(d["five_hour"]["utilization"] - t.truth_five) < 1e-9, f"reading at {d['ts']} matches truth"
        got = datetime.fromisoformat(d["five_hour"]["resets_at"])
        assert abs((got - t.truth_five_reset).total_seconds()) <= 5, f"five-hour reset attributed right at {d['ts']}"
        got7 = datetime.fromisoformat(d["seven_day"]["resets_at"])
        assert abs((got7 - t.truth_seven_reset).total_seconds()) <= 5, f"seven-day reset right at {d['ts']}"

    for t in rig.ticks:
        if t.fault in REQUEST_FAULTS and t.requested:
            row = next((d for d in lines if d["ts"] == stamp(t.at)), None)
            assert row is not None and "error" in row, f"{t.fault} at {stamp(t.at)} recorded as an error gap"
            assert row.get("five_hour") is None, f"{t.fault} gap carries no utilization (never zero)"
            assert t.rc == 4, f"{t.fault} exits 4"
            if t.fault == "401":
                assert row.get("reason") == "auth_expired", "auth failure has its own clear reason"
            if t.fault == "429":
                assert row.get("reason") == "rate_limited", "429 recorded as rate_limited"

    # End to end: the passive join's per-window pairing never mixes two true windows.
    samples = rig.samples()
    assert len(samples) == len(readings), "every reading row reaches the join, no zero-filled gaps"
    true_resets = [w.resets_at for k, w in rig.meter.history if k == "five"]
    for p in window_points(samples):
        end = datetime.fromisoformat(p["window_ending"])
        assert p["reset_verified"] and any(abs((end - r).total_seconds()) <= 60 for r in true_resets), \
            f"window point {p} ends on a true five-hour reset"
        w = next(w for k, w in rig.meter.history if k == "five" and same_reset(w.resets_at.isoformat(), end.isoformat()))
        assert p["five_hour_pct"] <= round(w.used, 1) + 0.05, f"window {end} gained no more than its true usage"
