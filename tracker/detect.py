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
the ratio d5/d7 is how many five-hour windows the week's cap holds. Two
things follow that the sample detector above gets wrong on it:

1. Resolution. Bucketing those pairs by calendar week (the shape the page
   charts) blends a mid-week step into the week's average. Anthropic cut the
   weekly cap on 2026-09-13; the calendar week ending 09-18 published 5.43,
   a mix of ~6.1 pre-step days and 4.50 post-step days, which is -14.5%
   against the previous weeks and so under threshold -- and even had it
   crossed, the week-then-confirming-week rule could not have fired before
   2026-10-02, nineteen days after the change (issue #25). So detection runs
   on the per-window points, and a step is dated by the first window at the
   new level.

2. Weight. A window is not a reading of equal standing: the seven-day meter
   reports whole percent, so a window whose d7 is 3 carries a third of a
   point of rounding on its denominator, and its ratio is +-33% noise. What
   makes a step credible is accumulated seven-day movement, not a count of
   readings. So instead of medians of readings, every comparison here is a
   POOLED ratio (total d5 over total d7, tracker/detect.py:pooled_windows),
   and the guards are stated in points of d7:

   - The base is the pooled ratio of the BASE_DAYS before the candidate,
     within the current regime, and needs MIN_BASE_D7 points of d7 behind
     it before anything can be judged against it.
   - A point can only be a candidate ("vote") with d7 >= MIN_VOTE_D7, 7
     points (why 7: below), and only if its ratio still lies beyond
     threshold after conceding half a point of d7 rounding in its favour
     (d5/(d7-0.5) for a drop, d5/(d7+0.5) for a rise). Half a point is the
     typical error on the difference of two whole-percent reads; the
     concession is not a second threshold, it is what stops a window that
     reads 15% low from voting when its true value could as easily sit on
     the base. It scales with the bucket: at the floor a point needs to read
     about 21% low to vote, at d7=14 about 18%, at d7=20 about 17%.
   - The candidate confirms once the points from it onward pool to at least
     MIN_POOL_D7 of d7 across at least MIN_POOL_POINTS windows AND that pool,
     with the same half-point concession, still lies beyond threshold from
     the base in the candidate's direction. This is the weighted form of the
     two-agreeing-readings rule: the five-hour and seven-day meters do not
     always advance in step inside one window, so a single window, however
     heavy, never confirms itself; but the newest point does take part, so a
     step surfaces as soon as enough post-step movement has accumulated (two
     windows / a day or two at Jonathan's usage), not after the next
     calendar week closes.
   - The event's percent is the pool's raw ratio against the base (the
     concession is a gate, not the estimate), and the pool is always the
     shortest one that confirms, so the event does not drift as later
     windows arrive.

Why the vote floor is 7 points of d7
------------------------------------

Picked from the real log, not by feel. The detector first shipped with a
floor of 3. Fed masterrig's whole usage log as it stood on 2026-09-15
through the publish path (84 max20 windows, 2026-08-19 to 09-15), it
reported four changes: +38% on 08-23, -23% on 08-28, +27% on 09-03 and the
real -28% on 09-14. The first and third were voted by d7=6 windows (39/6,
42/6) against bases pooled mostly from thin windows of 0 to 4 points each,
and the second (a d7=8 window 12% under the plain fortnight) read as a
candidate only against the base the false 08-23 regime left behind; the
calendar weeks over the same span (6.58, 6.35, 6.02) have no step in them.
Swept over the same log (tests/fixtures/):

    floor  max20, 08-19 to 09-15      max20 to 09-12   max5, 06-13 to 08-14
    3-4    +38 -23 +27, -28 (09-14)   +38 -23 +27      -20 +21 -26 (July)
    5-6    +38 -23 +27, -28 (09-14)   +38 -23 +27      none
    7-8    -29 (09-14)                none             none
    9      none                       none             none

7 is the lowest floor at which neither flat stretch of the log reports
anything (max5 is frozen and never detected on in production, which makes
it a clean out-of-sample check), and it is where the seven-day meter's
rounding alone stops being able to carry a flat window past threshold: a
difference of two whole-percent reads can be off by up to a whole point,
1/6 = 17% of a d7=6 window but 1/7 = 14% of a d7=7 one. The ceiling is the
real cut: of its three post-cut windows only the first (09-14T16:30, 37/8)
is heavy enough to vote at all, so a floor of 9 misses it. 8 also passes
today but has no margin on that side.

The cost is latency. Only 7 of those 84 max20 windows clear the floor,
about one every four days, so a step waits for the first heavy window after
it before it can vote; the 09-13 cut had one the next day. A thin window's
movement is never wasted: it still pools into every base and confirmation.

Regimes replace the arm/re-arm rule: an event starts a new regime at its
candidate, and every later base is pooled from that regime only, so the
series does not re-fire while it sits on its new plateau and a second step
is measured against the new level, not the old one.

Measured against the real 2026-09-13 cut, on the whole max20 series in the
log: it fires "decreased 29%" dated 2026-09-14 as soon as the second
post-cut window closes (09-14T21:30: pool 54/12 = 4.50 against a fortnight
base of 711/112 = 6.35), and nothing else in the series fires (on the six
windows quoted in issue #25, whose base is only the three pre-cut ones
quoted there, it is -26%). The calendar-week detector could not have said
anything before 2026-10-02.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median

MIN_HISTORY = 3
LOOKBACK = 4

# detect_weighted_changes: all in whole points of seven-day meter movement (d7).
MIN_VOTE_D7 = 7.0        # a bucket under this is too noisy to be a candidate (why 7: module docstring)
MIN_BASE_D7 = 10.0       # a base with less behind it cannot judge a candidate
MIN_POOL_D7 = 10.0       # post-candidate movement needed to confirm a step
MIN_POOL_POINTS = 2      # ...spread over at least this many windows
BASE_DAYS = 14           # how far back the base pools, within the current regime
ROUNDING_CONCESSION = 0.5  # points of d7 conceded to a bucket's ratio before it may vote


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


def pooled_windows(points: list[tuple[datetime, float, float]]) -> float | None:
    """Total five-hour movement over total seven-day movement, or None with no d7."""
    d5 = sum(p[1] for p in points)
    d7 = sum(p[2] for p in points)
    return d5 / d7 if d7 > 0 else None


def _conceded_ratio(d5: float, d7: float, base: float) -> float:
    """d5/d7 after conceding ROUNDING_CONCESSION points of d7 towards `base`.

    A ratio below base (a drop) is read as if d7 were half a point smaller, a
    ratio above it as if d7 were half a point larger: the most charitable
    reading the meter's whole-percent rounding allows.
    """
    if d5 / d7 < base:
        return d5 / (d7 - ROUNDING_CONCESSION)
    return d5 / (d7 + ROUNDING_CONCESSION)


def detect_weighted_changes(points: list[tuple[datetime, float, float]],
                            threshold: float = 0.15) -> list[ChangeEvent]:
    """Step changes in a (window_ending, d5, d7) series -- see the module docstring.

    Points with d7 <= 0 still pool into bases and confirmations (their d5 is
    real movement); they just never vote. Events are dated by the candidate
    window's ending time, in whatever zone the timestamps carry.
    """
    return _detect_weighted(points, threshold)[0]


def current_regime_points(points: list[tuple[datetime, float, float]],
                          threshold: float = 0.15) -> list[tuple[datetime, float, float]]:
    """The points of the current regime, oldest first: from the newest event's
    candidate window on, or the whole series when nothing has fired. Compared by
    full timestamp, so an earlier window on the event's own day (still at the old
    level) stays out of the new regime."""
    _, ordered, start = _detect_weighted(points, threshold)
    return ordered[start:]


def _detect_weighted(points: list[tuple[datetime, float, float]],
                     threshold: float) -> tuple[list[ChangeEvent], list[tuple[datetime, float, float]], int]:
    ordered = sorted((p for p in points if p[1] > 0 and p[2] >= 0), key=lambda p: p[0])
    events: list[ChangeEvent] = []
    regime_start = 0
    i = 0
    while i < len(ordered):
        ts, d5, d7 = ordered[i]
        base_from = ts - timedelta(days=BASE_DAYS)
        base_pts = [p for p in ordered[regime_start:i] if p[0] >= base_from]
        base = pooled_windows(base_pts)
        if d7 < MIN_VOTE_D7 or base is None or sum(p[2] for p in base_pts) < MIN_BASE_D7:
            i += 1
            continue
        ratio = d5 / d7 / base - 1
        conceded = _conceded_ratio(d5, d7, base) / base - 1
        if abs(conceded) <= threshold or (conceded > 0) != (ratio > 0):
            i += 1
            continue
        # A candidate: pool forward until enough seven-day movement has accumulated.
        pool_d5 = pool_d7 = 0.0
        confirmed_at: int | None = None
        for j in range(i, len(ordered)):
            pool_d5 += ordered[j][1]
            pool_d7 += ordered[j][2]
            if pool_d7 >= MIN_POOL_D7 and j - i + 1 >= MIN_POOL_POINTS:
                confirmed_at = j
                break
        if confirmed_at is None:
            i += 1  # not enough movement after it yet: unconfirmed until more windows land
            continue
        pool_ratio = pool_d5 / pool_d7 / base - 1
        pool_conceded = _conceded_ratio(pool_d5, pool_d7, base) / base - 1
        if abs(pool_conceded) > threshold and (pool_conceded > 0) == (ratio > 0) and (pool_ratio > 0) == (ratio > 0):
            events.append(ChangeEvent(ts.date(), "increased" if ratio > 0 else "decreased",
                                      round(abs(pool_ratio) * 100)))
            regime_start = i
        i += 1
    return events, ordered, regime_start


def latest_change(events: list[ChangeEvent]) -> ChangeEvent | None:
    return max(events, key=lambda e: e.date) if events else None
