"""Detect step changes in a sparse, sample-based dollar series.

The probe now runs about once a week (one model, Sonnet 5), so change
detection can no longer bucket by calendar day -- a week's gap between
readings is the cadence, not a gap in coverage. Instead this compares
successive dollar-value readings directly, in the order they were taken:
for a reading with at least MIN_HISTORY earlier readings, the ratio compares
it to the median of up to the previous LOOKBACK readings (fewer, down to
MIN_HISTORY, near the start of the series).

There is no persist-days requirement the way a noisy daily series would need
one, but a single reading is still not enough to call a step on its own: a
model's first row, or an early-tick run, can land far enough from the median
to look like a step and then settle back next time (real series: 1.081,
1.132, 0.682, 1.118 -- the third reading alone looked like a 34% drop, but
the fourth was back in band). So a candidate reading at index i only fires
once the *next* reading also lands more than threshold from i's own base, in
the same direction -- two agreeing readings, not one. The event still keeps
reading i's date and percent; the confirming reading is not itself a new
candidate. A reading with no later reading yet (the newest in the series)
can therefore never fire on its own -- it is unconfirmed until the next probe.

Once fired, detection re-arms only once a later reading returns to within
threshold of the (then-current) median. That stops one real step from firing
an event on every subsequent reading while the series sits on its new
plateau. A candidate that fails to confirm leaves the armed state unchanged.
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
        if armed and abs(ratio) > threshold and i + 1 < len(ordered):
            _, next_value = ordered[i + 1]
            next_ratio = next_value / base - 1
            confirmed = abs(next_ratio) > threshold and (next_ratio > 0) == (ratio > 0)
            if confirmed:
                events.append(ChangeEvent(ts.date(), "increased" if ratio > 0 else "decreased",
                                          round(abs(ratio) * 100)))
                armed = False
        elif not armed and abs(ratio) <= threshold:
            armed = True
    return events


def latest_change(events: list[ChangeEvent]) -> ChangeEvent | None:
    return max(events, key=lambda e: e.date) if events else None
