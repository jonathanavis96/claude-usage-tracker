"""Exact percent-crossing events from a meter's whole-percent samples.

The API only ever returns whole percents (`5.0`, `33.0`; see
docs/reference-2026-09-20-shellac-credits-model.md and tools/float_probe.py), so any
single reading carries +-0.5 points of quantisation error, and a delta between two
readings carries it at both ends. The meter is a staircase, though: you cannot read a
fraction of a step, but you can time the step. If consecutive samples read 5 then 6,
cumulative usage crossed exactly 6% somewhere between those two timestamps -- a
*crossing*. The difference between two crossings in the same window is exact (6% to
16% is exactly 10%, not "10% +- 1%"); the only remaining uncertainty is *when* inside
each bracket the crossing happened, which is bounded by the bracket width and shrinks
as sampling gets faster.

A jump of more than one percent between adjacent samples still bounds each intervening
level to the same bracket, just less tightly: represent that honestly (`step` on the
`Crossing`) rather than discarding it or pretending it was a clean single-percent step.

Window resets are handled the same way tracker/join.py's `_is_reset` does: a boundary
is a reset (never a crossing) whenever the reset timestamp moves by more than
`same_reset`'s tolerance, or the value drops while the timestamp doesn't move enough to
register -- the endpoint jitters `resets_at` by sub-second amounts between reads, so a
naive string/minute comparison of it would reject almost every same-window pair.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from .samples import Sample
from .usage_api import same_reset

#: The two meters this module knows how to read off a Sample, and the Sample
#: attribute holding each one's reset stamp.
METERS = ("five_hour", "seven_day")
_RESET_ATTR = {"five_hour": "resets_at", "seven_day": "seven_resets_at"}


def _value(s: Sample, meter: str) -> float | None:
    return getattr(s, meter)


def _reset(s: Sample, meter: str) -> str | None:
    return getattr(s, _RESET_ATTR[meter])


@dataclass(frozen=True)
class Crossing:
    """One integer percent level crossed by a meter, bounded by two samples.

    `before_ts`/`after_ts` are the last sample strictly below `level` and the first
    sample at or above it -- the bracket the crossing happened somewhere inside.
    `step` is how many whole percents the meter moved in that same bracket: 1 for a
    clean single-percent crossing (the tight, valuable case), or more when several
    levels -- `level` among them -- were crossed between the same two samples, in
    which case every one of those levels shares this same bracket and this same
    `step`, and none of them can be pinned down more tightly than that.
    `reset_at` is the window's reset stamp in force across the bracket (may be None
    for a source that records none, e.g. gs's ceiling log), carried so two crossings
    can be checked for being in the same window without re-deriving it from samples.
    """

    meter: str
    level: int
    before_ts: datetime
    after_ts: datetime
    step: int
    reset_at: str | None

    @property
    def bracket_width_s(self) -> float:
        return (self.after_ts - self.before_ts).total_seconds()

    @property
    def clean(self) -> bool:
        """True for a clean single-percent crossing -- the meter moved by exactly 1."""
        return self.step == 1


def _is_reset(a: Sample, b: Sample, meter: str) -> bool:
    """Same rule as tracker.join._is_reset, generalised to either meter.

    Not-same-reset is the primary signal; a value drop despite a same-looking reset
    stamp catches a reset the timestamp comparison alone would miss.
    """
    if not same_reset(_reset(a, meter), _reset(b, meter)):
        return True
    bv, av = _value(b, meter), _value(a, meter)
    return bv is not None and av is not None and bv < av


def extract_crossings(samples: Iterable[Sample], meter: str) -> list[Crossing]:
    """Crossings for one meter, never spanning a reset.

    `samples` should already be readings only (tracker.samples' parsers drop error/gap
    lines before producing a Sample), sorted or not -- this sorts by `ts` itself.
    Adjacent same-window samples whose value does not move produce no crossing;
    adjacent samples across a reset (detected via `_is_reset`) produce no crossing
    either, whatever their values are.
    """
    if meter not in METERS:
        raise ValueError(f"unknown meter {meter!r}, expected one of {METERS}")
    ordered = sorted(samples, key=lambda s: s.ts)
    out: list[Crossing] = []
    for a, b in pairwise(ordered):
        av, bv = _value(a, meter), _value(b, meter)
        if av is None or bv is None:
            continue
        if _is_reset(a, b, meter):
            continue
        lo, hi = int(av), int(bv)
        step = hi - lo
        if step <= 0:
            continue
        for level in range(lo + 1, hi + 1):
            out.append(Crossing(meter, level, a.ts, b.ts, step, _reset(b, meter)))
    return out


def extract_all(samples: Iterable[Sample]) -> dict[str, list[Crossing]]:
    """`extract_crossings` for every meter in METERS, keyed by meter name."""
    samples = list(samples)
    return {meter: extract_crossings(samples, meter) for meter in METERS}


def bracket_events(crossings: Iterable[Crossing]) -> list[Crossing]:
    """One Crossing per observed bracket, dropping the extra per-level entries a
    multi-percent jump adds to `extract_crossings`'s output.

    `extract_crossings` records one Crossing per integer level so any single level can
    be looked up and fed to `exact_delta`. That over-counts *events*: a single bracket
    where the meter jumped from 5 to 8 is one observed step, not three, even though it
    bounds three levels. This keeps the highest level from each (before_ts, after_ts)
    bracket -- a clean single-percent crossing (step 1) is unaffected either way.
    """
    seen: dict[tuple, Crossing] = {}
    for c in crossings:
        key = (c.before_ts, c.after_ts)
        if key not in seen or c.level > seen[key].level:
            seen[key] = c
    return sorted(seen.values(), key=lambda c: c.after_ts)


@dataclass(frozen=True)
class CrossingDelta:
    """The exact percent movement between two crossings of the same meter and window.

    `percent` is exact -- an integer difference of integer levels, with none of the
    +-0.5-per-endpoint error a plain sample-to-sample delta carries. What remains is
    *timing* uncertainty: `low` might have happened anywhere in its own bracket and
    `high` anywhere in its, so the true elapsed time between them is only known to
    within `uncertainty_s` (the sum of both bracket widths), between `min_span_s`
    (last-possible-low to first-possible-high) and `max_span_s` (first-possible-low to
    last-possible-high).
    """

    meter: str
    low: Crossing
    high: Crossing
    percent: int
    min_span_s: float
    max_span_s: float

    @property
    def uncertainty_s(self) -> float:
        return self.max_span_s - self.min_span_s


def exact_delta(low: Crossing, high: Crossing) -> CrossingDelta:
    """The exact percent difference between two crossings, plus its timing uncertainty.

    Both crossings must be the same meter and the same window (`same_reset` on their
    `reset_at`); raises ValueError otherwise -- a delta across a reset is not a
    movement, it is two different meters' worth of history.
    """
    if low.meter != high.meter:
        raise ValueError(f"crossings are of different meters: {low.meter!r} vs {high.meter!r}")
    if not same_reset(low.reset_at, high.reset_at):
        raise ValueError("crossings are in different windows (reset stamps do not match)")
    if high.level < low.level:
        low, high = high, low
    return CrossingDelta(
        meter=low.meter,
        low=low,
        high=high,
        percent=high.level - low.level,
        min_span_s=(high.before_ts - low.after_ts).total_seconds(),
        max_span_s=(high.after_ts - low.before_ts).total_seconds(),
    )
