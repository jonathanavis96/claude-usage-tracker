"""Anomalies found by the long chaos soak (tests/chaos/long_soak.py), 2026-10-02.

Each is a strict xfail: when a fix lands it turns into an XPASS failure and the
marker should come off.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from chaos_rig import Rig


@pytest.mark.xfail(strict=True, reason="soak: a line stamped >20 min ahead by a fast clock "
                                       "is ignored, not caught up to, so the corrected clock appends "
                                       "earlier stamps after it and the log goes out of order")
@pytest.mark.parametrize("ahead", [timedelta(minutes=25), timedelta(hours=3)])
def test_fast_clock_line_leaves_log_monotonic_after_the_clock_is_corrected(tmp_path, ahead):
    rig = Rig(tmp_path)
    with rig.running():
        rig.tick()
        rig.tick(now=rig.clock + timedelta(minutes=2) + ahead)  # the clock ran fast for one tick
        for _ in range(5):  # NTP corrected it; ten minutes of the right time
            rig.tick()
    stamps = [datetime.fromisoformat(d["ts"]) for d in rig.lines()]
    assert all(a < b for a, b in zip(stamps, stamps[1:])), [s.isoformat() for s in stamps]


@pytest.mark.xfail(strict=True, reason="UT-S soak: a reading stamped ahead by a fast clock sorts "
                                       "into the join out of place; the dip and the climb back are "
                                       "two pieces of one window, so the climb is counted twice")
@pytest.mark.parametrize("ahead_min", [25, 61])
def test_fast_clock_reading_does_not_inflate_the_window_total(tmp_path, ahead_min):
    from tracker.join import window_points
    rig = Rig(tmp_path)
    with rig.running():
        for _ in range(20):
            rig.tick(burn=1.0)
        rig.tick(burn=1.0, now=rig.clock + timedelta(minutes=2 + ahead_min))  # one fast tick
        for _ in range(60):
            rig.tick(burn=1.0)
    truth = rig.meter.five.used
    points = window_points(rig.samples())
    total = sum(p["five_hour_pct"] for p in points)
    assert total <= round(truth, 1) + 0.05, f"join counts {total}% for a window that moved {truth}%: {points}"
