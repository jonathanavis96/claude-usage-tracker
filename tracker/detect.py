"""Detect step changes in a sparse, sample-based dollar series.

The probe now runs about once a week (one model, Sonnet 5), so change
detection can no longer bucket by calendar day -- a week's gap between
readings is the cadence, not a gap in coverage. Instead this compares
successive dollar-value readings directly, in the order they were taken:
for a reading with at least MIN_HISTORY earlier readings, the ratio compares
it to the median of up to the previous LOOKBACK readings (fewer, down to
MIN_HISTORY, near the start of the series).

There is no persist-days requirement the way a noisy daily series would need
one. A 5-tick probe already measures a window to about one prompt in ten, so
a single reading crossing the threshold is precise enough to call a step: one
data point is not a blip here the way one noisy calendar day would be.

Once fired, detection re-arms only once a later reading returns to within
threshold of the (then-current) median. That stops one real step from firing
an event on every subsequent reading while the series sits on its new
plateau.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median

MIN_HISTORY = 3
LOOKBACK = 4


@dataclass(frozen=True)
class ChangeEvent:
    date: date
    direction: str
    percent: int
    model: str = "all"


def detect_changes(readings: list[tuple[datetime, float]], threshold: float = 0.15) -> list[ChangeEvent]:
    ordered = sorted(readings, key=lambda r: r[0])
    events: list[ChangeEvent] = []
    armed = True
    for i in range(MIN_HISTORY, len(ordered)):
        history = [v for _, v in ordered[max(0, i - LOOKBACK):i]]
        base = median(history)
        if not base:
            continue
        ts, value = ordered[i]
        ratio = value / base - 1
        if armed and abs(ratio) > threshold:
            events.append(ChangeEvent(ts.date(), "increased" if ratio > 0 else "decreased", round(abs(ratio) * 100)))
            armed = False
        elif not armed and abs(ratio) <= threshold:
            armed = True
    return events


def latest_change(events: list[ChangeEvent]) -> ChangeEvent | None:
    return max(events, key=lambda e: e.date) if events else None
