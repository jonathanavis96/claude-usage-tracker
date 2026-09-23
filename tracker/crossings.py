"""Measure between exact whole-percent crossings instead of stretch/week endpoints.

Both meters report whole percent, so any figure built from two raw readings (a
stretch's start/end, a week's first/last row) carries up to one point of
rounding at each end (tracker/weekly.py, tracker/detect.py `ratio_interval`).
A reading that steps from 41 to 42 between two samples marks a moment -- known
only to within that sample gap -- at which the true value crossed 42.0.
Measuring between two such crossings removes the end rounding entirely; what
is left is timing error, at most one sample gap at each end.

A `Crossing` is not a single instant: it is a bracket, `(lower, upper)`, the
two consecutive readings between which the true value passed through `value`.
Nothing here claims to know where inside that bracket the crossing actually
fell -- every figure downstream that uses a crossing's bound is stating a
worst case, not a point estimate, and callers that need one report both.

This module is read-only over `tracker/samples.py` Sample objects and
`tracker/turns.py` Turn objects; it does not write logs or reports.
"""
from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .join import MAX_PAIR_GAP
from .samples import Sample
from .turns import Turn
from .usage_api import same_reset

METERS = ("five_hour", "seven_day")


@dataclass(frozen=True)
class Crossing:
    """One upward integer crossing of a meter, bracketed by the two readings that saw it.

    `lower`/`upper` are the timestamps of the reading before and after the
    crossing; the true crossing instant is known only to lie in between, so
    `gap` is the timing error this crossing alone carries. `value` is the
    integer percent crossed (the meter's new whole-percent reading, or one of
    several when a pair jumped by more than one point -- see `crossings`).
    `window_id` is the five-hour or seven-day reset this crossing's window
    belongs to: the reset's own timestamp string when the log names it, else
    the timestamp of the first reading of the window's first paired piece
    (matching `tracker/join.py` `window_points`'s fallback), so crossings
    never straddle a reset even on a reset-less log.
    """
    lower: datetime
    upper: datetime
    value: int
    window_id: str
    reset_verified: bool
    meter: str = "five_hour"

    @property
    def gap(self) -> timedelta:
        return self.upper - self.lower


def crossings(samples: Iterable[Sample], meter: str = "five_hour",
              max_gap: timedelta = MAX_PAIR_GAP) -> list[Crossing]:
    """Every upward integer crossing of one account's `meter` ("five_hour" or "seven_day").

    Consecutive readings pair only within one window: a value drop, a reset id
    change (`tracker.usage_api.same_reset`), or a gap wider than `max_gap`
    (which could hide a reset the log never named) all start a fresh window,
    so a crossing is never reported as spanning one. A pair that rises by more
    than one point (a missed sample, or a busy window) yields one `Crossing`
    per integer passed, all sharing that pair's `(lower, upper)` bracket --
    the log saw only the two ends, so every intermediate value is bounded by
    the same gap.

    Readings a whole-percent step could not have crossed (no rise, or the
    meter value itself missing, e.g. `seven_day` on a log that doesn't record
    it) produce nothing.
    """
    if meter not in METERS:
        raise ValueError(f"unknown meter: {meter!r} (known: {', '.join(METERS)})")
    resets_attr = "resets_at" if meter == "five_hour" else "seven_resets_at"
    ordered = sorted(samples, key=lambda s: s.ts)
    out: list[Crossing] = []
    window_start: datetime | None = None
    window_id: str | None = None
    window_reset_verified = False
    for a, b in zip(ordered, ordered[1:]):
        av, bv = getattr(a, meter), getattr(b, meter)
        ar, br = getattr(a, resets_attr), getattr(b, resets_attr)
        if av is None or bv is None:
            window_start = window_id = None
            continue
        is_reset = not same_reset(ar, br) or bv < av
        if is_reset or b.ts - a.ts > max_gap:
            window_start = window_id = None
            continue
        if window_start is None:
            # The window's id is fixed once, at the first reading of the chain, and reused
            # for every later pair -- the endpoint's own resets_at jitters by sub-second
            # amounts between reads (tracker/weekly.py `_same_weekly_window`), so re-reading
            # it per pair would give almost every crossing its own window.
            window_start = a.ts
            window_id = ar if ar is not None else window_start.isoformat()
            window_reset_verified = ar is not None
        lo_int, hi_int = int(av), int(bv)
        for v in range(lo_int + 1, hi_int + 1):
            out.append(Crossing(a.ts, b.ts, v, window_id, window_reset_verified, meter))
    return out


def _overlaps(c: Crossing, lo: datetime, hi: datetime) -> bool:
    return c.upper > lo and c.lower < hi


def windows_per_week_from_crossings(five_hour_crossings: list[Crossing],
                                     seven_day_crossings: list[Crossing], k: int = 5) -> list[dict]:
    """The five-hour/seven-day ratio between pairs of seven-day crossings `k` points apart.

    For every pair of seven-day crossings in the same seven-day window whose
    values are `k` apart, the five-hour points crossed between them are
    counted in three bands:

    - "definite": a five-hour crossing whose whole bracket sits inside the
      confirmed span between the two seven-day crossings (after the first
      crossing's upper bound, before the second's lower bound) -- it happened
      between them beyond doubt.
    - the two ambiguous end bands: a five-hour crossing whose bracket
      overlaps either seven-day crossing's own uncertain span, so whether it
      happened before or after that crossing is not decided by the readings.

    `windows` is the midpoint estimate (definite plus half the ambiguous
    crossings, divided by k); `windows_bounds` is [min, max] using zero and
    all of the ambiguous crossings respectively -- the interval this method's
    timing error concedes, the analogue of `tracker.detect.ratio_interval`'s
    rounding interval. A pair whose seven-day crossings' brackets already
    overlap (the two are closer together than one sample gap) is skipped: the
    "definite" span would be empty or negative and nothing safe can be said.
    """
    five_hour_crossings = sorted(five_hour_crossings, key=lambda c: c.lower)
    by_window: dict[str, list[Crossing]] = {}
    for c in seven_day_crossings:
        by_window.setdefault(c.window_id, []).append(c)
    out: list[dict] = []
    for wid, cs in by_window.items():
        cs = sorted(cs, key=lambda c: c.value)
        for i in range(len(cs) - k):
            lo7, hi7 = cs[i], cs[i + k]
            if hi7.lower < lo7.upper:
                continue
            definite = [f for f in five_hour_crossings if f.lower >= lo7.upper and f.upper <= hi7.lower]
            amb_lo = [f for f in five_hour_crossings if f not in definite and _overlaps(f, lo7.lower, lo7.upper)]
            amb_hi = [f for f in five_hour_crossings if f not in definite and _overlaps(f, hi7.lower, hi7.upper)]
            n_definite, n_amb = len(definite), len(amb_lo) + len(amb_hi)
            n_min, n_max = n_definite, n_definite + n_amb
            n_mid = n_definite + n_amb / 2
            out.append({
                "window_id": wid, "k": k,
                "seven_day_from": lo7.value, "seven_day_to": hi7.value,
                "at": hi7.upper.isoformat(),
                "windows": round(n_mid / k, 4),
                "windows_bounds": [round(n_min / k, 4), round(n_max / k, 4)],
                "five_hour_points_definite": n_definite,
                "five_hour_points_ambiguous": n_amb,
                "reset_verified": lo7.reset_verified and hi7.reset_verified,
            })
    return out


def tokens_between_crossings(five_hour_crossings: list[Crossing], turns: Iterable[Turn],
                              n: int = 1) -> list[dict]:
    """Transcript tokens spent between five-hour crossings `n` points apart, in one window.

    Reuses the same turn stream `tracker.gs_passive.report` builds
    (`tracker.turns.iter_turns` over `transcript_paths`); this function only
    sums it. `tokens` counts turns strictly between the first crossing's upper
    bound and the second's lower bound -- the confirmed span, free of the
    ambiguity at either end -- giving tokens spent for exactly `n` percent.
    `timing_error_tokens` reports the tokens that instead fall inside each
    crossing's own bracket (`lower_gap`, `upper_gap`): tokens that may or may
    not belong to the `n`-percent span, depending on exactly when inside that
    gap the crossing happened. A pair whose brackets already overlap is
    skipped, as in `windows_per_week_from_crossings`.
    """
    turns = sorted(turns, key=lambda t: t.ts)
    ts_keys = [t.ts for t in turns]

    def _sum(lo: datetime, hi: datetime) -> int:
        i, j = bisect.bisect_left(ts_keys, lo), bisect.bisect_left(ts_keys, hi)
        return sum(t.total for t in turns[i:j])

    by_window: dict[str, list[Crossing]] = {}
    for c in five_hour_crossings:
        by_window.setdefault(c.window_id, []).append(c)
    out: list[dict] = []
    for wid, cs in by_window.items():
        cs = sorted(cs, key=lambda c: c.value)
        for i in range(len(cs) - n):
            lo, hi = cs[i], cs[i + n]
            if hi.lower < lo.upper:
                continue
            tokens = _sum(lo.upper, hi.lower)
            out.append({
                "window_id": wid, "n": n,
                "from_value": lo.value, "to_value": hi.value,
                "at": hi.upper.isoformat(),
                "tokens": tokens, "tokens_per_pct": round(tokens / n, 2),
                "timing_error_tokens": {"lower_gap": _sum(lo.lower, lo.upper), "upper_gap": _sum(hi.lower, hi.upper)},
                "reset_verified": lo.reset_verified and hi.reset_verified,
            })
    return out
