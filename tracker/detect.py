"""Detect step changes: in the probe's sparse dollar series, and in the weekly-windows series.

Dollar series: sample readings
------------------------------

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

Weekly windows: a weighted series, not a sample series
------------------------------------------------------

The weekly-windows series (tracker/weekly.py) is a different animal from the
probe's dollar readings and gets its own detector, detect_weighted_changes.
Each point there is one five-hour window's paired meter movement: d5 (how far
the five-hour meter moved) over d7 (how far the seven-day meter moved), and
the ratio d5/d7 is how many five-hour windows the week's cap holds. Three
things follow that the sample detector above gets wrong on it:

1. Resolution. Bucketing those pairs by calendar week (the shape the page
   charts) blends a mid-week step into the week's average (issue #25), so
   detection runs on the per-window points and a step is dated by the first
   window at the new level.

2. Weight. A window is not a reading of equal standing: what makes a level
   credible is accumulated seven-day movement, not a count of readings. Every
   level here is POOLED (total d5 over total d7, `pooled_windows`), and both
   sides of a split need MIN_POOL_POINTS windows and MIN_POOL_D7 points of d7.
   No single window has to be heavy enough to "vote": the detector that
   required one (a d7 >= 7 candidate) could miss a real cut indefinitely when
   every post-cut window was light (audit 2026-09-16, finding 4: four windows
   at 60/10 then twenty-one at 18/4 fired nothing).

3. Rounding. Every window point is a difference of two whole-percent readings
   of each meter, so each piece carries up to one point of error on d5 and on
   d7, and pieces are disconnected, so their errors do not cancel. The old
   rule conceded half a point to a whole pool, which let ten (11, 1) windows
   and two (47, 8) windows -- all compatible with a constant ratio of 6 --
   fire a -47% change (finding 4 again). A pool of n pieces now concedes

       e(n) = min(n, ROUNDING_Z * sqrt(n))

   points to each total: the worst case while n is small, and ROUNDING_Z
   standard deviations of the sum once it is not (a piece's error lies inside
   (-1, 1), so its variance is at most 1 whatever its distribution). Each
   level's `rounding_interval` is (d5 - e)/(d7 + e) .. (d5 + e)/(d7 - e).

A change is certified when a split of the series leaves both sides above
their floors, the pooled levels differ by more than `threshold`, AND the two
rounding intervals do not overlap. Among certified splits, the one that most
reduces the absolute residual |d5 - level * d7| is taken (binary
segmentation), and each side is then searched again, so one transition is
one event, a second step is measured against the level before it, and a
stray window cannot steal the onset of a persistent change (finding 5). The
event carries the adjacent levels it separates (the same levels the chart
draws), their intervals, the last window at the old level and the first at
the new one as onset bounds, and the first window by which the post-change
pool alone certified. Segmentation is retrospective: a later window can move
or remove an event, which is why the events are published as observed
account metric changes and never as a dated policy change.

Why ROUNDING_Z is 2
-------------------

Swept over the two audit counterexamples and over masterrig's whole usage log
as it stood on 2026-09-16T17:30Z, paired by tracker/weekly.py after the
finding-3 repair and routed through the publish path's plan split:

    Z     60/10 x4, 18/4 x21   (11,1) x10, (47,8) x2   max20 08-19..09-12   max5 06-13..08-14   max20 to 09-16T03
    1.5   -25% (correct)       -47% (false)            none                 none                -29% (09-14)
    2     -25% (correct)       none                    none                 none                -29% (09-14)
    2.5   none (missed)        none                    none                 none                none
    3     none (missed)        none                    none                 none                none

2 is the only value that both catches the persistent light-window cut and
refuses the rounding artefact, and neither flat stretch of the real log fires
at it. The real 2026-09-13 cut is close to its resolution: two days after it
the post-cut pool (about 9 pieces, d7 of 30) reads 4.6 against 6.5 before,
and the upper end of its interval sits within a few hundredths of the lower
end of the pre-cut one, so the event certifies on some publishes and not on
others until more post-cut windows arrive. That is the method stating its
resolution, not a fault to tune away: a narrower concession is exactly what
fired on (11, 1).

The quoted six windows of issue #25 on their own (116/19 before, 81/18 after,
three pieces each) no longer certify anything: three points of rounding on a
d7 of 19 or 18 lets both sides sit near 5.3.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from statistics import median

MIN_HISTORY = 3
LOOKBACK = 4

# detect_weighted_changes: all in whole points of seven-day meter movement (d7).
MIN_BASE_D7 = 10.0       # the level before a split needs this much movement behind it
MIN_POOL_D7 = 10.0       # ...and so does the level after it
MIN_POOL_POINTS = 2      # ...spread over at least this many windows on each side
ROUNDING_Z = 2.0         # standard deviations of pooled rounding error conceded (why 2: module docstring)


@dataclass(frozen=True)
class ChangeEvent:
    date: date
    direction: str
    percent: int
    model: str = "all"
    onset_earliest: date | None = None
    onset_latest: date | None = None
    confirmed_at: date | None = None
    evidence_points: int | None = None
    denominator_pct: float | None = None
    before_interval: tuple[float | None, float | None] | None = None
    after_interval: tuple[float | None, float | None] | None = None


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


SMOOTH_WINDOW = timedelta(days=7)
SMOOTH_MIN_POINTS = 4


def detect_smoothed_changes(readings: list[tuple[datetime, float]], threshold: float = 0.15,
                            window: timedelta = SMOOTH_WINDOW,
                            min_points: int = SMOOTH_MIN_POINTS) -> list[ChangeEvent]:
    """Step changes in a per-day series too noisy for detect_changes (the passive one).

    A passive day pools one account's real sessions and scatters 15-20% day to
    day, so two consecutive readings past a 15% line (detect_changes) fire on
    noise. Here the series is split where it best separates into two persistent
    levels: every split with at least `min_points` readings on each side whose
    medians differ by more than `threshold` is a candidate, the one that most
    reduces the total absolute deviation from each side's median is taken, and
    each side is searched again (binary segmentation). The event is dated at the
    first reading of the new level.

    Judging whole segments rather than a rolling window is what the 2026-09-16
    audit (finding 5) asked for: a rolling week's median took the first reading
    past a halfway line as the onset, so one stray low day before a real cut
    stole its date, and the remainder of the same transition fired a second
    event (100 x10, 80, 100 x2, 70 x12 read as -20% on day 10 and -22% on day
    13 for one cut on day 13). `window` is kept in the signature for callers and
    is unused. The readings carry no rounding bounds, so these events publish as
    provisional (tracker/publish.py).
    """
    ordered = sorted(readings, key=lambda r: r[0])
    events: list[ChangeEvent] = []

    def segment(lo: int, hi: int) -> None:
        """Binary segmentation using persistent levels on both sides.

        Looking at the entire candidate sides prevents one stray value in a
        rolling window from stealing the onset and prevents the remainder of
        one transition from firing a second event.
        """
        best: tuple[float, int, float, float] | None = None
        whole_values = [v for _, v in ordered[lo:hi]]
        whole_level = median(whole_values) if whole_values else 0
        unsplit_loss = sum(abs(v - whole_level) for v in whole_values)
        for split in range(lo + min_points, hi - min_points + 1):
            left = [v for _, v in ordered[lo:split]]
            right = [v for _, v in ordered[split:hi]]
            before, after = median(left), median(right)
            if not before:
                continue
            change = after / before - 1
            if abs(change) <= threshold:
                continue
            # Prefer the split that most reduces robust within-segment error.
            # This selects the one real boundary in a long new plateau instead
            # of a later blended split plus a duplicate of the same transition.
            split_loss = (sum(abs(v - before) for v in left)
                          + sum(abs(v - after) for v in right))
            score = unsplit_loss - split_loss
            if best is None or score > best[0]:
                best = (score, split, before, after)
        if best is None:
            return
        _, split, before, after = best
        segment(lo, split)
        ratio = after / before - 1
        events.append(ChangeEvent(
            ordered[split][0].date(), "increased" if ratio > 0 else "decreased",
            round(abs(ratio) * 100), onset_earliest=ordered[split - 1][0].date(),
            onset_latest=ordered[split][0].date(),
            confirmed_at=ordered[min(hi - 1, split + min_points - 1)][0].date(),
            evidence_points=hi - lo,
        ))
        segment(split, hi)

    segment(0, len(ordered))
    return sorted(events, key=lambda e: e.date)


def pooled_windows(points: list[tuple]) -> float | None:
    """Total five-hour movement over total seven-day movement, or None with no d7."""
    d5 = sum(p[1] for p in points)
    d7 = sum(p[2] for p in points)
    return d5 / d7 if d7 > 0 else None


def _pieces(point: tuple) -> int:
    """How many disconnected rounded differences a (ts, d5, d7[, pieces]) point sums."""
    return int(point[3]) if len(point) > 3 and point[3] else 1


def rounding_error(pieces: int) -> float:
    """Points of rounding error conceded to a total pooled from `pieces` separate differences."""
    return min(float(pieces), ROUNDING_Z * math.sqrt(pieces))


def ratio_interval(d5: float, d7: float, pieces: int) -> tuple[float | None, float | None]:
    """The lowest and highest d5/d7 the whole-percent readings allow (module docstring, 3.).

    The upper end is None when the conceded error could take d7 to zero: the pool
    cannot bound the ratio from above at all.
    """
    e = rounding_error(pieces)
    lo = max(0.0, d5 - e) / (d7 + e) if d7 + e > 0 else None
    hi = (d5 + e) / (d7 - e) if d7 > e else None
    return lo, hi


def pooled_interval(points: list[tuple]) -> tuple[float | None, float | None]:
    """ratio_interval over a pool of window points."""
    if not points:
        return None, None
    return ratio_interval(sum(p[1] for p in points), sum(p[2] for p in points), sum(_pieces(p) for p in points))


def _certified_change(before: list[tuple], after: list[tuple], threshold: float) -> float | None:
    """after's pooled level over before's, minus one, when it certifies a change; else None."""
    if len(before) < MIN_POOL_POINTS or len(after) < MIN_POOL_POINTS:
        return None
    if sum(p[2] for p in before) < MIN_BASE_D7 or sum(p[2] for p in after) < MIN_POOL_D7:
        return None
    level_before, level_after = pooled_windows(before), pooled_windows(after)
    if not level_before or level_after is None:
        return None
    change = level_after / level_before - 1
    if abs(change) <= threshold:
        return None
    blo, bhi = pooled_interval(before)
    alo, ahi = pooled_interval(after)
    if change < 0:
        separated = ahi is not None and blo is not None and ahi < blo
    else:
        separated = alo is not None and bhi is not None and alo > bhi
    return change if separated else None


def _residual(points: list[tuple]) -> float:
    """Total |d5 - level * d7| around the pool's own level: what a split is chosen to reduce."""
    level = pooled_windows(points)
    if level is None:
        return sum(p[1] for p in points)
    return sum(abs(p[1] - level * p[2]) for p in points)


def detect_weighted_changes(points: list[tuple], threshold: float = 0.15) -> list[ChangeEvent]:
    """Certified step changes in a (window_ending, d5, d7[, pieces]) series -- see the module docstring.

    Points where neither meter moved are dropped; a point with d7 == 0 still pools.
    Events are dated by the first window at the new level, in whatever zone the
    timestamps carry.
    """
    return _detect_weighted(points, threshold)[0]


def current_regime_points(points: list[tuple], threshold: float = 0.15) -> list[tuple]:
    """The points of the current regime, oldest first: from the newest event's
    first window on, or the whole series when nothing has been certified. Compared
    by full timestamp, so an earlier window on the event's own day (still at the old
    level) stays out of the new regime."""
    _, ordered, starts = _detect_weighted(points, threshold)
    return ordered[starts[-1]:]


def weighted_regimes(points: list[tuple], threshold: float = 0.15) -> list[dict]:
    """Every regime the detector found, oldest first, each held flat at its pooled level.

    Windows per week is a plan constant: it moves when the limit moves and not
    otherwise, so the honest series is a step function, not one point per
    calendar week, and each regime carries how far its own rounding lets the
    level sit.

    Each regime is {"start", "end", "windows", "seven_day_pct", "points", "pieces",
    "rounding_interval", "quality"}: `windows` is `pooled_windows` over the regime,
    `seven_day_pct` its pooled denominator, `points` its window count and `pieces`
    the separate rounded differences behind it; `rounding_interval` is
    `pooled_interval`, and `quality` is "bounded", or "insufficient_precision" when
    the rounding could take the denominator to zero. `end` is the last window's
    timestamp; the newest regime's end is simply the newest window, not a claim
    that it has finished.
    """
    _, ordered, starts = _detect_weighted(points, threshold)
    regimes = []
    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(ordered)
        span = ordered[start:stop]
        level = pooled_windows(span)
        if not span or level is None:
            continue
        lo, hi = pooled_interval(span)
        regimes.append({
            "start": span[0][0].isoformat(),
            "end": span[-1][0].isoformat(),
            "windows": round(level, 2),
            "seven_day_pct": round(sum(p[2] for p in span), 1),
            "points": len(span),
            "pieces": sum(_pieces(p) for p in span),
            "rounding_interval": [round(x, 4) if x is not None else None for x in (lo, hi)],
            "quality": "bounded" if hi is not None else "insufficient_precision",
        })
    return regimes


def _detect_weighted(points: list[tuple], threshold: float) -> tuple[list[ChangeEvent], list[tuple], list[int]]:
    """(events, ordered points, regime start indices). `starts` always begins with 0 --
    the series before any event is itself a regime -- and gains each certified split,
    so starts[-1] is the current regime's start."""
    ordered = sorted((p for p in points if p[1] >= 0 and p[2] >= 0 and (p[1] > 0 or p[2] > 0)),
                     key=lambda p: p[0])
    found: list[tuple[int, int, int]] = []  # (lo, split, hi) of each certified split

    def segment(lo: int, hi: int) -> None:
        whole = _residual(ordered[lo:hi])
        best: tuple[float, int] | None = None
        for split in range(lo + MIN_POOL_POINTS, hi - MIN_POOL_POINTS + 1):
            before, after = ordered[lo:split], ordered[split:hi]
            if _certified_change(before, after, threshold) is None:
                continue
            gain = whole - _residual(before) - _residual(after)
            if best is None or gain > best[0]:
                best = (gain, split)
        if best is None:
            return
        split = best[1]
        found.append((lo, split, hi))
        segment(lo, split)
        segment(split, hi)

    segment(0, len(ordered))
    starts = sorted({0, *(split for _, split, _ in found)})
    events = []
    for lo, split, hi in found:
        # Magnitude and intervals from the adjacent final regimes: the levels the chart draws.
        n = starts.index(split)
        before = ordered[starts[n - 1]:split]
        after = ordered[split:starts[n + 1] if n + 1 < len(starts) else len(ordered)]
        change = pooled_windows(after) / pooled_windows(before) - 1
        # The first window by which the post-change pool alone certified against the old level.
        confirmed = next(j for j in range(split + MIN_POOL_POINTS, hi + 1)
                         if _certified_change(ordered[lo:split], ordered[split:j], threshold) is not None)
        events.append(ChangeEvent(
            ordered[split][0].date(), "increased" if change > 0 else "decreased", round(abs(change) * 100),
            onset_earliest=ordered[split - 1][0].date(), onset_latest=ordered[split][0].date(),
            confirmed_at=ordered[confirmed - 1][0].date(), evidence_points=len(before) + len(after),
            denominator_pct=round(sum(p[2] for p in after), 1),
            before_interval=pooled_interval(before), after_interval=pooled_interval(after),
        ))
    return sorted(events, key=lambda e: e.date), ordered, starts


def latest_change(events: list[ChangeEvent]) -> ChangeEvent | None:
    return max(events, key=lambda e: e.date) if events else None
