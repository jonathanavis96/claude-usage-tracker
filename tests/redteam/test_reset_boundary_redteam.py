"""Red-team: masterrig's merged meter series loses the reset ids it was given.

masterrig's meter is two logs (tracker/passive.py `_rebuild`, tracker/gs_passive.py
`masterrig_account`): moonlighter's usage_log.jsonl every 30 min, which records
`resets_at`, and the ceiling's systemd log every 5 min, which does not (69% of the merged
series on 2026-10-02). Timestamps carry +02:00 (SAST) on both, resets_at is UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tracker.join import build_intervals
from tracker.samples import Sample, merge_samples
from tracker.turns import Turn

SAST = timezone(timedelta(hours=2))
UTC = timezone.utc
RESET_1 = datetime(2026, 10, 2, 8, 0, 0, 120000, tzinfo=UTC)   # 10:00 SAST
RESET_2 = RESET_1 + timedelta(hours=5)


def ml(h: int, m: int, s: int, util: float, reset: datetime) -> Sample:
    return Sample(datetime(2026, 10, 2, h, m, s, tzinfo=SAST), util, 30.0, reset.isoformat(), "moonlighter",
                  (RESET_1 + timedelta(days=3)).isoformat())


def ceil(h: int, m: int, s: int, util: float) -> Sample:
    return Sample(datetime(2026, 10, 2, h, m, s, tzinfo=SAST), util, 30.0, None, "ceiling")


@pytest.mark.xfail(strict=True, reason=(
    "tracker/samples.py:20 _PRIORITY gives ceiling 0 and moonlighter 1 (lower wins), so when "
    "both logs read in one minute merge_samples keeps the reset-less ceiling reading and "
    "drops moonlighter's reset id, the opposite of what tracker/gs_passive.py:142 documents "
    "('reset-bearing sources winning'). On masterrig's real logs 281 reset-bearing readings "
    "are dropped this way"))
def test_merge_keeps_the_reset_bearing_reading():
    merged = merge_samples([ml(9, 55, 1, 5.0, RESET_1)], [ceil(9, 55, 40, 5.0)])
    assert len(merged) == 1
    assert merged[0].resets_at == RESET_1.isoformat(), "the minute kept the reading with no reset id"


@pytest.mark.xfail(strict=True, reason=(
    "tracker/join.py:59-62 _is_reset uses same_reset, which returns True whenever either side "
    "has no resets_at (tracker/usage_api.py:139). A moonlighter reading that says its window "
    "ends at 10:00, followed by a ceiling reading at 10:03 that is higher, is paired across "
    "the reset: the interval's movement is new-window minus old-window instead of the new "
    "window's own climb, and the tokens across the reset are divided by it. At most 17 such pairs "
    "with prior use in masterrig's logs, 2026-06-13 to 2026-10-02 (most flat, so the "
    "measured damage is small)"))
def test_known_reset_between_readings_splits_the_interval():
    samples = [ml(9, 30, 1, 3.0, RESET_1), ml(9, 55, 1, 5.0, RESET_1),
               ceil(10, 3, 0, 8.0),                      # new window, no reset id
               ml(10, 30, 2, 12.0, RESET_2)]
    turns = [Turn(s.ts + timedelta(seconds=30), "claude-opus-4-1", 1000, 1000, 0, 0) for s in samples]
    intervals = build_intervals(samples, turns)
    assert intervals, "precondition: the join produced intervals"
    reset_local = RESET_1.astimezone(SAST)
    spanning = [iv for iv in intervals if iv.start < reset_local < iv.end]
    assert not spanning, f"interval {spanning[0].start}..{spanning[0].end} straddles the 10:00 reset"
