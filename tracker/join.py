"""Join utilization samples with transcript turns into tokens-per-percent rates.

Two joins live here. `build_intervals`/`daily_rates` is masterrig's: 1%
intervals, tokens per percent, a daily median. `build_stretches` is the gs
one (tracker/gs_passive.py): meter dollars per percent over stretches of at
least STRETCH_PCT, pooled rather than taken as a median, because one whole
percent of a 1% interval is anywhere between 0 and 2% of real movement while
one point of a 10% stretch is a tenth of that.
"""
from __future__ import annotations
import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import median
from .samples import Sample
from .usage_api import same_reset
from .turns import Turn, normalize_model, normalized_raw_model
from .weekly import _window_point

CLASSES = ("input", "output", "cache_read", "cache_write")
DETAIL_CLASSES = ("cache_write_1h",)
ATTRIBUTION = 0.90
MIN_INTERVALS_PER_DAY = 5
STRETCH_PCT = 10
#: Two readings further apart than this are not paired: a gap in the log (a failed
#: read, the host down) could hide a window reset, and the tokens spent across it
#: cannot be set against a movement nobody saw.
MAX_PAIR_GAP = timedelta(minutes=15)
FIVE_HOURS = timedelta(hours=5)


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
    if not same_reset(a.resets_at, b.resets_at):
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
        # UTC day boundaries, so passive and probe segments of the series line up.
        by_day.setdefault(iv.end.astimezone(timezone.utc).date(), []).append(iv)
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


def bundle_meter_usd(model: str, tokens: dict, prices: dict) -> float | None:
    """Meter dollars of one model's token bundle, or None when the model has no price.

    The probe's own valuation, so passive and probe readings are one unit: per
    class, tokens x list price x class_weight, then x the model's meter_weight
    (tracker/publish.py usd_per_pct, for a single tick). It is restated here
    rather than imported because tracker/publish.py imports tracker/passive.py,
    which imports this module; tests/test_join.py pins the two together. The
    model id is normalised first (a date suffix or `[1m]` marker is the same
    model); anything that is not one of the three priced models is None.
    """
    m = normalize_model(model)
    price = prices.get(m) if m else None
    if price is None:
        return None
    weights = price.get("class_weight", {})
    value = sum(tokens.get(c, 0) * price[c] * weights.get(c, 1.0) / 1e6 for c in CLASSES)
    # cache_write_1h is a subset of cache_write.  Add only the price/weight
    # premium over the aggregate's default five-minute valuation.
    one_hour = tokens.get("cache_write_1h", 0)
    if one_hour:
        one_hour_price = price.get("cache_write_1h", 2 * price["input"])
        value += (one_hour * (one_hour_price * weights.get("cache_write_1h", weights.get("cache_write", 1.0))
                              - price["cache_write"] * weights.get("cache_write", 1.0)) / 1e6)
    return (value
            * price.get("meter_weight", 1.0))


def turn_meter_usd(turn: Turn, prices: dict) -> float | None:
    return bundle_meter_usd(turn.model, {c: getattr(turn, c) for c in CLASSES + DETAIL_CLASSES}, prices)


@dataclass
class Stretch:
    """One account's meter movement of at least STRETCH_PCT, and what its transcripts spent across it.

    Pooled from consecutive same-window sample pairs; a pair that straddles a
    reset or a gap counts for nothing, its tokens included. `windows` is the
    number of separate window pieces the movement was summed over: each piece's
    movement is the difference of two whole-percent readings, so each carries
    up to one point of rounding, and `bounds` widens the rate by exactly that.
    `tokens` keeps canonical model -> class -> count, so a consumer can revalue
    the stretch at later prices, as the publisher revalues probe rows.
    """
    start: datetime
    end: datetime
    delta_pct: float = 0.0
    windows: int = 1
    usd: float = 0.0
    tokens: dict = field(default_factory=dict)
    unpriced_tokens: int = 0
    unpriced: dict = field(default_factory=dict)
    turns: int = 0
    reset_verified: bool = True

    @property
    def usd_per_pct(self) -> float:
        return self.usd / self.delta_pct

    @property
    def bounds(self) -> tuple[float, float]:
        """Lowest and highest meter dollars per 1% the whole-percent readings allow."""
        lo = self.usd / (self.delta_pct + self.windows)
        hi = self.usd / (self.delta_pct - self.windows) if self.delta_pct > self.windows else float("inf")
        return lo, hi

    @property
    def priced_share(self) -> float:
        priced = sum(sum(by_class.values()) for by_class in self.tokens.values())
        total = priced + self.unpriced_tokens
        return priced / total if total else 1.0

    def add(self, turn: Turn, prices: dict) -> None:
        self.turns += 1
        usd = turn_meter_usd(turn, prices)
        if usd is None:
            self.unpriced_tokens += turn.total
            by_class = self.unpriced.setdefault(normalized_raw_model(turn.model), {c: 0 for c in CLASSES})
            for c in CLASSES:
                by_class[c] += getattr(turn, c)
            if turn.cache_write_1h:
                by_class["cache_write_1h"] = by_class.get("cache_write_1h", 0) + turn.cache_write_1h
            return
        self.usd += usd
        by_class = self.tokens.setdefault(normalize_model(turn.model), {c: 0 for c in CLASSES})
        for c in CLASSES:
            by_class[c] += getattr(turn, c)
        if turn.cache_write_1h:
            by_class["cache_write_1h"] = by_class.get("cache_write_1h", 0) + turn.cache_write_1h


def build_stretches(samples: list[Sample], turns: list[Turn], prices: dict, stretch_pct: float = STRETCH_PCT,
                    max_gap: timedelta = MAX_PAIR_GAP) -> list[Stretch]:
    """Stretches of one account's meter, each closed once it has moved `stretch_pct`.

    A turn belongs to the pair whose [earlier, later) readings contain its
    timestamp, as in build_intervals. The stretch still open at the end of the
    samples is not returned: it has not moved far enough to be read.
    """
    samples = sorted(samples, key=lambda s: s.ts)
    turns = sorted(turns, key=lambda t: t.ts)
    keys = [t.ts for t in turns]
    out: list[Stretch] = []
    cur: Stretch | None = None
    new_piece = True
    for a, b in zip(samples, samples[1:]):
        if _is_reset(a, b) or b.ts - a.ts > max_gap:
            new_piece = True
            continue
        if cur is None:
            cur = Stretch(a.ts, b.ts)
        elif new_piece:
            cur.windows += 1
        new_piece = False
        if a.resets_at is None or b.resets_at is None:
            cur.reset_verified = False
        cur.end = b.ts
        cur.delta_pct += b.five_hour - a.five_hour
        for t in turns[bisect.bisect_left(keys, a.ts):bisect.bisect_left(keys, b.ts)]:
            cur.add(t, prices)
        if cur.delta_pct >= stretch_pct:
            out.append(cur)
            cur = None
    return out


def window_points(samples: list[Sample], max_gap: timedelta = MAX_PAIR_GAP) -> list[dict]:
    """Five-hour and seven-day movement per five-hour window, for weekly windows per account.

    The pairing rule is tracker/weekly.py's: consecutive readings inside one
    window pair whenever neither meter fell (a seven-day fall is its weekly
    reset), whichever of them moved -- a seven-day tick with the five-hour meter
    still is denominator the ratio needs (audit 2026-09-16, finding 3). A
    five-hour reset starts a new window; a gap longer than `max_gap` starts a
    new piece, which joins the window it came from only when the log names that
    window's reset (without one, a gap could hide a reset, so it starts a new
    point). The point shape is tracker/weekly.py's `by_window` point.
    `window_ending` is the window's recorded reset time when the log has one;
    gs's ceiling log records none, so there it is the window's last paired
    reading and `reset_verified` is false.
    """
    samples = sorted(samples, key=lambda s: s.ts)
    windows: list[dict] = []
    chain: dict | None = None
    for a, b in zip(samples, samples[1:]):
        if _is_reset(a, b) or b.ts - a.ts >= FIVE_HOURS:
            chain = None
            continue
        if b.ts - a.ts > max_gap or a.seven_day is None or b.seven_day is None:
            chain = None
            continue
        d5, d7 = b.five_hour - a.five_hour, b.seven_day - a.seven_day
        if d5 < 0 or d7 < 0:
            chain = None
            continue
        if chain is None:
            last = windows[-1] if windows else None
            if (last is not None and a.resets_at is not None and last["resets_at"] is not None
                    and same_reset(last["resets_at"], a.resets_at)):
                chain = last
                chain["pieces"] += 1
            else:
                chain = {"resets_at": a.resets_at, "end": b.ts, "d5": 0.0, "d7": 0.0, "pieces": 1}
                windows.append(chain)
        chain["d5"] += d5
        chain["d7"] += d7
        chain["end"] = b.ts
    return [_window_point(w["resets_at"] or w["end"].isoformat(), w["d5"], w["d7"], w["pieces"],
                          reset_verified=w["resets_at"] is not None)
            for w in windows if w["d5"] > 0 or w["d7"] > 0]
