"""Detect persistent step changes in daily tokens-per-percent series."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from statistics import median


@dataclass(frozen=True)
class ChangeEvent:
    date: date
    direction: str
    percent: int
    model: str


def _ratio_on(days: list[date], vals: dict[date, float], i: int, recent_days: int, base_days: int) -> float | None:
    if i < base_days + recent_days - 1:
        return None
    recent = [vals[d] for d in days[i - recent_days + 1:i + 1]]
    base = [vals[d] for d in days[i - recent_days - base_days + 1:i - recent_days + 1]]
    b = median(base)
    return (median(recent) - b) / b if b else None


def detect_changes(series: dict[str, dict[date, float]], threshold: float = 0.05, recent_days: int = 3,
                   base_days: int = 7, persist_days: int = 2) -> list[ChangeEvent]:
    events: list[ChangeEvent] = []
    for model, vals in series.items():
        days = sorted(vals)
        run = 0
        armed = True
        for i in range(len(days)):
            r = _ratio_on(days, vals, i, recent_days, base_days)
            if r is None:
                continue
            if abs(r) > threshold:
                run += 1
                if run == persist_days and armed:
                    step_day = days[i - recent_days - persist_days + 3]
                    events.append(ChangeEvent(step_day, "increased" if r > 0 else "decreased", round(abs(r) * 100), model))
                    armed = False
            else:
                run = 0
                armed = True
    return sorted(events, key=lambda e: e.date)


def latest_change(events: list[ChangeEvent]) -> ChangeEvent | None:
    return max(events, key=lambda e: e.date) if events else None
