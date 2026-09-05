"""Join utilization samples with transcript turns into tokens-per-percent rates."""
from __future__ import annotations
import bisect
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median
from .samples import Sample
from .turns import Turn

CLASSES = ("input", "output", "cache_read", "cache_write")
ATTRIBUTION = 0.90
MIN_INTERVALS_PER_DAY = 5


@dataclass
class Interval:
    start: datetime
    end: datetime
    delta_pct: float
    tokens: dict = field(default_factory=lambda: {c: 0 for c in CLASSES})
    by_model: dict = field(default_factory=dict)
    model: str | None = None

    @property
    def total(self) -> int:
        return sum(self.tokens.values())


@dataclass
class DailyRate:
    tokens_per_pct: float
    n: int
    interpolated: bool
    per_model: dict
    split: dict


def _is_reset(a: Sample, b: Sample) -> bool:
    if a.resets_at and b.resets_at and a.resets_at != b.resets_at:
        return True
    return b.five_hour < a.five_hour


def build_intervals(samples: list[Sample], turns: list[Turn]) -> list[Interval]:
    samples = sorted(samples, key=lambda s: s.ts)
    turns = sorted(turns, key=lambda t: t.ts)
    keys = [t.ts for t in turns]
    out: list[Interval] = []
    cur: Interval | None = None
    for a, b in zip(samples, samples[1:]):
        if _is_reset(a, b):
            cur = None
            continue
        if cur is None:
            cur = Interval(a.ts, b.ts, 0.0)
        cur.end = b.ts
        cur.delta_pct += b.five_hour - a.five_hour
        for t in turns[bisect.bisect_left(keys, a.ts):bisect.bisect_left(keys, b.ts)]:
            for c in CLASSES:
                cur.tokens[c] += getattr(t, c)
            cur.by_model[t.model] = cur.by_model.get(t.model, 0) + t.total
        if cur.delta_pct >= 1:
            if cur.total > 0:
                top = max(cur.by_model, key=cur.by_model.get)
                if cur.by_model[top] / cur.total >= ATTRIBUTION:
                    cur.model = top
                out.append(cur)
            cur = None
    return out


def daily_rates(intervals: list[Interval]) -> dict[date, DailyRate]:
    by_day: dict[date, list[Interval]] = {}
    for iv in intervals:
        by_day.setdefault(iv.end.date(), []).append(iv)
    if not by_day:
        return {}
    out: dict[date, DailyRate] = {}
    prev: DailyRate | None = None
    d = min(by_day)
    while d <= max(by_day):
        ivs = by_day.get(d, [])
        if len(ivs) >= MIN_INTERVALS_PER_DAY:
            rates = [iv.total / iv.delta_pct for iv in ivs]
            per_model: dict[str, list[float]] = {}
            for iv in ivs:
                if iv.model:
                    per_model.setdefault(iv.model, []).append(iv.total / iv.delta_pct)
            tot = {c: sum(iv.tokens[c] for iv in ivs) for c in CLASSES}
            grand = sum(tot.values()) or 1
            prev = DailyRate(median(rates), len(ivs), False,
                             {m: median(v) for m, v in per_model.items()},
                             {c: tot[c] / grand for c in CLASSES})
            out[d] = prev
        elif prev is not None:
            out[d] = DailyRate(prev.tokens_per_pct, len(ivs), True, dict(prev.per_model), dict(prev.split))
        d = date.fromordinal(d.toordinal() + 1)
    return out
