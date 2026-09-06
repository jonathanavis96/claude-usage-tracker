"""Detect persistent step changes in daily tokens-per-percent series.

Windows are calendar based, not index based: for a calendar day d the recent
window is days d-2..d and the base window days d-9..d-3, so a gap in probe
coverage widens the windows in time instead of silently comparing readings that
are weeks apart. A day only produces a ratio when its recent window holds at
least MIN_RECENT values and its base window at least MIN_BASE; a day that
produces no ratio contributes nothing, neither extending a run nor resetting it.

The threshold sits above the probe's own quantisation. A tick probe measures a
window to about one prompt in ten, so anything under roughly 10% is measurement
noise; 0.15 is the first level a step has to clear to be real (a deviation from
the original 0.05 spec, recorded by the controller).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

MIN_RECENT = 2
MIN_BASE = 3


@dataclass(frozen=True)
class ChangeEvent:
    date: date
    direction: str
    percent: int
    model: str


def _ratio_on(vals: dict[date, float], d: date, recent_days: int, base_days: int) -> float | None:
    recent = [v for dd, v in vals.items() if d - timedelta(days=recent_days - 1) <= dd <= d]
    base = [v for dd, v in vals.items()
            if d - timedelta(days=recent_days + base_days - 1) <= dd <= d - timedelta(days=recent_days)]
    if len(recent) < MIN_RECENT or len(base) < MIN_BASE:
        return None
    b = median(base)
    return (median(recent) - b) / b if b else None


def detect_changes(series: dict[str, dict[date, float]], threshold: float = 0.15, recent_days: int = 3,
                   base_days: int = 7, persist_days: int = 2) -> list[ChangeEvent]:
    events: list[ChangeEvent] = []
    for model, vals in series.items():
        if not vals:
            continue
        run = 0
        armed = True
        prev_ratio_day: date | None = None
        d = min(vals)
        last = max(vals)
        while d <= last:
            r = _ratio_on(vals, d, recent_days, base_days)
            if r is None:
                d += timedelta(days=1)
                continue
            if abs(r) > threshold:
                run = run + 1 if prev_ratio_day == d - timedelta(days=1) and run else 1
                if run == persist_days and armed:
                    step_day = d - timedelta(days=recent_days - 1)
                    events.append(ChangeEvent(step_day, "increased" if r > 0 else "decreased", round(abs(r) * 100), model))
                    armed = False
            else:
                run = 0
                armed = True
            prev_ratio_day = d
            d += timedelta(days=1)
    return sorted(events, key=lambda e: e.date)


def latest_change(events: list[ChangeEvent]) -> ChangeEvent | None:
    return max(events, key=lambda e: e.date) if events else None
