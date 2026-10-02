"""Fault-injection soak of the usage collector. Run: python3 -m pytest tests/chaos -q

The six invariants that failed on 2026-10-02 (docs/burn-20261002/chaos-findings.md)
were fixed in tracker/meter_log.py the same night; their tests are ordinary tests now.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from chaos_rig import REQUEST_FAULTS, Rig, check_invariants, stamp

CYCLES = 240


def soak(rig: Rig, cycles: int = CYCLES) -> None:
    """Seeded mix: ~30% faulted ticks, two early five-hour resets, one weekly reset."""
    weights = {f: 1 for f in REQUEST_FAULTS}
    weights["timeout"] = 0.4  # each costs CLIENT_TIMEOUT_S of wall time
    faults, w = list(weights), list(weights.values())
    with rig.running():
        for i in range(cycles):
            if i in (60, 150):
                rig.meter.reset_five(rig.clock + timedelta(seconds=30))  # a window reset mid-cycle
            if i == 180:
                rig.meter.reset_seven(rig.clock + timedelta(seconds=30))  # a weekly reset
            fault = rig.rng.choices(faults, w)[0] if rig.rng.random() < 0.3 else None
            rig.tick(fault, burn=rig.rng.choice((0.0, 0.5, 1.0, 2.0)))


def test_soak_200_plus_cycles_holds_every_invariant(tmp_path):
    rig = Rig(tmp_path, seed=20261002)
    started = time.monotonic()
    soak(rig)
    assert len(rig.ticks) >= 200
    assert sum(1 for t in rig.ticks if t.fault) > 40, "the soak actually injected faults"
    assert any(t.fault == "429" and t.requested for t in rig.ticks)
    check_invariants(rig)
    assert time.monotonic() - started < 30


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_soak_other_seeds(tmp_path, seed):
    rig = Rig(tmp_path, seed=seed)
    soak(rig, cycles=120)
    check_invariants(rig)


def test_host_timezone_does_not_change_what_is_recorded(tmp_path):
    logs = {}
    old = os.environ.get("TZ")
    try:
        for tz in ("UTC", "Africa/Johannesburg"):
            os.environ["TZ"] = tz
            time.tzset()
            rig = Rig(tmp_path / tz, seed=7)
            soak(rig, cycles=60)
            check_invariants(rig)
            logs[tz] = rig.log.read_text().replace(str(tmp_path / tz), "<root>")
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    assert logs["UTC"] == logs["Africa/Johannesburg"]


def test_a_clock_reporting_local_offset_still_stamps_utc(tmp_path):
    rig = Rig(tmp_path)
    sast = timezone(timedelta(hours=2))
    with rig.running():
        t = rig.tick()
        rig.tick(now=(t.at + timedelta(minutes=2)).astimezone(sast))
    assert [d["ts"][-6:] for d in rig.lines()] == ["+00:00", "+00:00"]
    check_invariants(rig)


def test_auth_failure_is_a_clear_error_state_with_no_data(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        rig.tick()
        rig.set_token("expired-token")  # nothing the fake accepts
        bad = [rig.tick() for _ in range(3)]
    assert all(t.rc == 4 and t.requested for t in bad)
    rows = rig.lines()[1:]
    assert [r.get("reason") for r in rows] == ["auth_expired"] * 3
    assert all("five_hour" not in r for r in rows)
    assert "expired-token" not in rig.log.read_text(), "the token never reaches the log"


def test_401_then_token_refreshed_by_claude_code_is_retried_once(tmp_path):
    rig = Rig(tmp_path)
    rig.api.good_tokens = {"fresh"}
    orig = rig.fetch
    calls = []

    def fetch(url, headers):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            rig.set_token("fresh")  # Claude Code refreshed between our read and the 401
        return orig(url, headers)

    rig.fetch = fetch
    with rig.running():
        t = rig.tick()
    assert t.rc == 0 and len(calls) == 2
    assert rig.readings()[0]["five_hour"]["utilization"] == t.truth_five


def test_429_retry_after_is_honoured_across_ticks(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        first = rig.tick("429", fault_arg=300)
        skipped = [rig.tick() for _ in range(2)]  # 4 min of a 5 min block
        resumed = rig.tick()
    assert first.requested and first.rc == 4
    assert not any(t.requested for t in skipped) and all(t.rc == 4 for t in skipped)
    assert resumed.requested and resumed.rc == 0
    check_invariants(rig)


def test_resets_at_jitter_never_splits_a_window(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        for _ in range(60):
            rig.tick(burn=1.0)
    rig.api.jitter_s = 0
    from tracker.join import window_points
    points = window_points(rig.samples())
    assert len(points) == 1 and points[0]["pieces"] == 1


def test_deleted_data_file_starts_a_fresh_log(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        for _ in range(5):
            rig.tick()
        rig.log.unlink()
        for _ in range(5):
            rig.tick()
    assert len(rig.readings()) == 5
    check_invariants(rig)


def test_crash_mid_write_does_not_lose_the_next_reading(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        rig.tick()
        with open(rig.log, "a") as fh:  # the process died half way through a line
            fh.write('{"ts": "2026-10-01T22:03:00+00:00", "account": "chaos", "five_h')
        rig.tick()
        rig.tick()
    good = []
    for raw in rig.log.read_text().splitlines():
        try:
            import json
            d = json.loads(raw)
        except ValueError:
            continue
        if (d.get("five_hour") or {}).get("utilization") is not None:
            good.append(d)
    assert len(good) == 3, "every reading after a torn line survives"


def test_read_only_data_dir_fails_cleanly(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        rig.tick()
        rig.log.parent.chmod(0o500)
        rig.log.chmod(0o400)
        try:
            ok = rig.tick()
            bad = rig.tick("500")
        finally:
            rig.log.parent.chmod(0o700)
            rig.log.chmod(0o600)
    assert ok.crashed is None and bad.crashed is None
    assert ok.rc == 4 and bad.rc == 4


def test_two_runs_at_once_write_one_row(tmp_path):
    rig = Rig(tmp_path)
    rig.api.hold = threading.Barrier(2)  # both requests are in flight together
    results = []
    with rig.running():
        rig.clock += timedelta(minutes=2)
        rig.meter.advance(rig.clock, 1.0)
        at = rig.clock

        def run():
            from tracker import meter_log
            results.append(meter_log.sample(rig.cfg, "chaos", rig.log, fetch=rig.fetch, now=lambda: at))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(5)
    assert sorted(results) == [0, 4] or sorted(results) == [0, 0]
    assert len(rig.readings()) == 1, "one writer at a time: one row per tick"


def test_clock_stepped_back_keeps_log_monotonic(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        for _ in range(3):
            rig.tick()
        rig.tick(now=rig.clock - timedelta(minutes=10))  # NTP steps the clock back
        rig.tick()
    stamps = [datetime.fromisoformat(d["ts"]) for d in rig.lines()]
    assert all(a < b for a, b in zip(stamps, stamps[1:]))


def test_429_logged_by_a_fast_clock_does_not_block_for_hours(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        rig.tick("429", fault_arg=60, now=rig.clock + timedelta(minutes=2) + timedelta(hours=3))
        later = [rig.tick() for _ in range(5)]  # 10 minutes of correct clock
    assert any(t.requested for t in later), "polling resumes within a few Retry-After periods"


def test_weekly_reset_attributes_usage_to_the_new_week(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        for i in range(40):
            if i == 20:
                rig.meter.reset_seven(rig.clock + timedelta(seconds=30))
            rig.tick(burn=2.0)
    check_invariants(rig)
    rows = rig.readings()
    weeks = [datetime.fromisoformat(r["seven_day"]["resets_at"]) for r in rows]
    jumps = [b - a for a, b in zip(weeks, weeks[1:]) if abs((b - a).total_seconds()) > 60]
    assert len(jumps) == 1, "exactly one weekly reset seen, jitter never read as one"
    assert stamp(rig.ticks[0].at) == rows[0]["ts"]


def test_absurd_retry_after_is_capped(tmp_path):
    rig = Rig(tmp_path)
    with rig.running():
        rig.tick("429", fault_arg=86400)
        later = [rig.tick(step=timedelta(minutes=25)) for _ in range(6)]  # 2 h 30 min
    assert not any(t.requested for t in later[:4]), "a long Retry-After is honoured up to the 2-hour cap"
    assert any(t.requested for t in later), "sampling resumes within the 2-hour cap"
