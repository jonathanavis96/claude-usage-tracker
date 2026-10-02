"""Anomalies found by the long chaos soak (tests/chaos/long_soak.py), 2026-10-02 UT-S.

Each is a strict xfail: when a fix lands it turns into an XPASS failure and the
marker should come off. Report: docs/reliability-2026-10-02/soak.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from chaos_rig import Rig


@pytest.mark.xfail(strict=True, reason="UT-S soak: a line stamped >20 min ahead by a fast clock "
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
