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
   sides of a split need MIN_POOL_POINTS windows and MIN_BASE_D7 / MIN_POOL_D7
   points of d7.
   No single window has to be heavy enough to "vote": the detector that
   required one (a d7 >= 7 candidate) could miss a real cut indefinitely when
   every post-cut window was light (audit 2026-09-16, finding 4: four windows
   at 60/10 then twenty-one at 18/4 fired nothing).

3. Rounding and window scatter. Every window point is a difference of two
   whole-percent readings of each meter. The meters are polled on a clock
   (masterrig's log reads them every 30 minutes), not when a meter ticks, so
   a reading's rounding error is uniform on (-0.5, 0.5) whatever the meter's
   fractional part, and independent of the next reading's. A piece's error,
   the difference of two such errors, is triangular on (-1, 1) with variance
   1/6, and a total pooled from n disconnected pieces has variance n/6 on d5
   and on d7 alike.

   Real windows scatter more than rounding alone: which work fills a window
   moves its five-hour to seven-day ratio as well. That extra scatter is
   measured, not assumed (WINDOW_DISPERSION, below), and a pool of n pieces
   concedes

       e(n) = ROUNDING_Z * sqrt(WINDOW_DISPERSION * n / 6)

   points to each total, taken in opposite directions on d5 and d7: each
   level's `rounding_interval` is (d5 - e)/(d7 + e) .. (d5 + e)/(d7 - e).
   The published field keeps its name; the interval carries the measured
   window scatter as well as the rounding.

   Independence is the model's one assumption, and the one thing it cannot
   exclude is a run of same-sign rounding errors long enough to fake a step.
   Ten (11, 1) windows then two (47, 8) windows (finding 4) are all
   compatible with a constant ratio of 6 if every d7 rounded the same way;
   under independent rounding that is too improbable to concede, so the
   interval alone would certify it. The floors are what bound it: each side
   of a split needs MIN_BASE_D7 / MIN_POOL_D7 points of seven-day movement,
   20, one to two days of real use, so a faked step needs its same-sign run
   held across that many clock-polled readings on both sides. The finding-4
   example (10 points before, 16 after) is refused by the floor; the same
   example scaled to twenty (11, 1) and three (47, 8) windows clears it and
   certifies -47%, which under independent rounding is what that evidence
   says. The floors are equal because segmentation is symmetric: the level
   on either side of a split is the same kind of pooled evidence.

   WINDOW_DISPERSION is the variance ratio of window residuals over the
   rounding model on a stretch with no change in it. For a flat run at pooled
   level L, a window's residual d5 - L * d7 has rounding variance
   pieces * (1 + L^2) / 6; the sum of squared residuals over those variances,
   per degree of freedom, is the ratio. Measured on history/passive.json as
   regenerated on masterrig on 2026-09-16, routed through the publish path's
   plan split:

       stretch (window_ending, UTC)                       windows  d7    level  ratio
       Max 20x 2026-08-28T01:50 .. 2026-09-13T21:30       63       228   6.21   3.70   <- used
       Max 20x 2026-08-19T17:00 .. 2026-09-13T21:30       94       290   6.50   5.13   (with the 27 Aug spike)
       Max 5x  2026-06-13T01:30 .. 2026-08-14T16:20       204      773   10.86  1.51
       Max 20x 2026-09-14T11:30 .. 2026-09-16T17:30       9        31    4.68   0.27   (after the cut)

   The value used is the flat Max 20x run, the live plan's, excluding the
   27 Aug spike: the windows ending 2026-08-27T15:10Z and 20:09Z read 95/11
   and 87/9, a burst of heavy work at 8.6 and 9.7 against a level of 6.2.
   Two windows of real workload variation are not rounding, and letting them
   set the scatter for every pool widens every interval for their sake (at
   5.1, replayed window by window, the 14 Sep cut certifies with the window
   ending 2026-09-15T16:30Z, is lost again at 21:30Z and returns at
   2026-09-16T02:30Z).

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

Why these constants
-------------------

Swept with ROUNDING_Z = 2 and both floors at 20 over the audit's
counterexamples and over the committed history/passive.json (windows to
2026-09-16T17:30Z); "daily" replays the Max 20x series as masterrig's
03:15Z push would have delivered it (windows ending by 02:30Z):

    dispersion  60/10 x4, 18/4 x21  (11,1) x10, (47,8) x2  x20, x3        max20 to 09-13     max5 flat   max20 all    daily 09-15 / 09-16
    1           -25% (correct)      none (floor)           -47%           +33% 08-27, -32%   +18% 08-13  3 events     3 events / 3 events
    3.7         -25% (correct)      none (floor)           -47%           none               none        -28% 09-14   -19% 08-28 / -29% 09-14
    5.1         -25% (correct)      none (floor)           -47%           none               none        -28% 09-14   none / -29% 09-14

With no dispersion (the bare triangular model) the 27 Aug spike and the fall
back from it certify as changes, and so does a Max 5x step inside its flat
run. At the measured 3.7 the Max 20x series certifies exactly one event, a
decrease dated 2026-09-14, and nothing on the flat run before it. Max 5x
keeps its decrease dated 2026-08-14 (-39%), which is the plan move itself:
PLAN_CHANGE in tracker/publish.py is the account owner's approximate date
and the step is published as not independently verified.

Segmentation is retrospective, and a partial post-cut pool can be misread:
the 2026-09-15 daily replay certifies -19% dated 2026-08-28 (7.58 over the
windows to the 27 Aug spike against 6.10 after, a level the four post-cut
windows it holds have pulled down just far enough), and the next day's replay
replaces it with the real -29% dated 2026-09-14. bin/daily.sh
therefore announces a change only once two consecutive publishes of new
weekly evidence show it, dated within a day of each other, and never
announces a date twice.

The quoted six windows of issue #25 on their own (116/19 before, 81/18 after,
three pieces each) certify nothing: neither side carries 20 points of d7.

Credit stretches: the same weighted detector, a second error model
---------------------------------------------------------------------

The passive dollar series had the opposite problem to the weekly one. It pooled
each account to one median per UTC day and fed that to detect_smoothed_changes,
which is a sample detector: every day counts once whether the meter moved 10%
or 60% that day, and a day's value was list-price dollars, so a day spent on
Opus read dearer than a day spent on Sonnet even when the meter charged the
same. Measured on history/gs-passive.json as committed 2026-09-23, the ratio of
credits to list dollars over a stretch has an IQR of 19% of its median on jwork
and 24% on dave (cv 0.13 and 0.21): that whole spread is model mix, and it is
what the list-dollar series carried as if it were the limit moving. It is the
mechanism behind the withdrawn 20 September event.

Both are fixed by feeding the same weighted machinery a different series:
one point per stretch, numerator its value in meter credits at the measured
per-family rates (tracker/credits.py), denominator the stretch's own
`delta_pct`. Pooling is then total credits over total meter percent, so a
stretch that moved the meter 40% weighs four times one that moved it 10%, and
the model mix inside a stretch is priced at what the meter actually charges.

What differs is the error model, which is why `WeightedSeries` exists rather
than a third detector. On the weekly series both d5 and d7 are whole-percent
meter readings, so the pooled error is conceded to each in opposite directions
(`numerator_scale` 1.0). On the credit series the numerator is not a meter
reading at all -- it is a token count times a rate, exact to as many digits as
the rate is -- and every unit of uncertainty sits in `delta_pct`, the
whole-percent denominator, so `numerator_scale` is 0.0 and the interval is
credits / (pct +/- e).

That leaves the real scatter to `dispersion`, exactly as it does on the weekly
series: a stretch's credits per percent varies because the work inside a
stretch varies, and the constant is measured, not assumed. Fitted the same way
(residual variance over the rounding model's, per degree of freedom) on each
account's own verified stretches in the live regime of history/gs-passive.json
as committed 2026-09-23:

    account  stretches  span               pooled credits/pct  dispersion
    jwork    21         17-21 Sep          169,658             80.0   <- used
    dave     29         18-21 Sep          141,104             54.4
    jwork    99         5-21 Sep (legacy)  162,575             60.6

STRETCH_DISPERSION takes the largest of the three, 80. It is two orders above
WINDOW_DISPERSION because it is absorbing something else: a window point's
scatter around its pooled ratio is mostly rounding, a stretch's credits per
percent scatters 36-39% (cv) on real work, and at 10 percent a stretch the
rounding of `delta_pct` is only about 4%. So the interval this produces is in
practice a standard error of the pooled mean at ROUNDING_Z sigma -- with all of
jwork's 21 verified stretches on one side it is +/-16%, and 0.36/sqrt(21) x 2 is
15.7%. Calling the constant a dispersion keeps one mechanism for two series;
what it is calibrated to is stated here so nobody reads the interval as
rounding alone.

MIN_STRETCH_PCT is the minimum-evidence rule, and it is total meter percent per
side per account, not a count of stretches: two 40% stretches are better
evidence of a level than eight 10% ones, and a count cannot say so. 100 points
is one full five-hour window of meter movement, about ten stretches at the size
these accounts produce, which is the smallest side that pools work from more
than one day.

The floor is a bound on a degenerate split, not what makes the test safe -- the
non-overlapping intervals are that, and at this dispersion they are demanding.
Swept over a noiseless synthetic step of 10-percent, one-piece stretches, the
smallest side that certifies:

    step   stretches per side   meter percent per side
    -15%   82                   820
    -20%   44                   440
    -25%   27                   270
    -30%   18                   180
    -40%   10                   100   <- the floor binds, not the interval
    -50%   10                   100   <- the floor binds, not the interval

So the floor is set exactly where it stops mattering: above a 40% step the
interval would certify on less evidence than one window of meter movement, and
a side that small is one session's work. Everything below that the interval
governs. Against the 217 (jwork) and 300 (dave) points of verified movement the
series holds on 2026-09-23, that means a real change smaller than about 25% is
not yet detectable on either account, and nothing certifies on both. That is
the honest reading of the data there is, not a reason to loosen the test.

What credits do not fix: capture
--------------------------------

Run on history/gs-passive.json as committed 2026-09-23, this finds exactly one
single-account step, dave -35% on 20 September, and it is not a limit change.
Every stretch on the low side of it carries the report's own `unaccounted`
verdict, with `capture` from 0.14 to 0.65: the meter moved further than the
transcripts on this host explain. That understates the numerator and leaves the
denominator alone, so it can only push credits per percent down, and it cannot
be a rise. Credits remove the model mix from a reading; they do nothing about
work the host never saw, which is a separate defect (the "unaccounted"
stretches, 49 of jwork's 139).

Nothing publishes it -- it is one account, and tracker/publish.py
`_agreeing_credit_events` needs two in the same direction -- and it is recorded
here because the two-account rule is the only thing standing in front of it. It
is NOT filtered out by a capture threshold: the 2026-09-16 audit (finding 10)
warned specifically against selecting observations by how close they sit to the
expected rate, CAPTURE_GATE is off for that reason, and reinstating a gate here
under another name would be the same mistake. The fix belongs where the
capture gap is, not in the detector.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median

MIN_HISTORY = 3
LOOKBACK = 4

# detect_weighted_changes: all in whole points of seven-day meter movement (d7).
MIN_BASE_D7 = 20.0       # the level before a split needs this much movement behind it
MIN_POOL_D7 = 20.0       # ...and so does the level after it (the bound on same-sign rounding runs)
MIN_POOL_POINTS = 2      # ...spread over at least this many windows on each side
ROUNDING_Z = 2.0         # standard deviations of pooled error conceded
# Variance of a window's error over the triangular rounding model's, measured on the
# flat Max 20x run 2026-08-28T01:50Z..2026-09-13T21:30Z, excluding the 27 Aug spike
# (derivation: module docstring, 3.).
WINDOW_DISPERSION = 3.7

# detect_credit_changes: one point per stretch, numerator meter credits, denominator
# the stretch's own delta_pct (module docstring, "Credit stretches").
MIN_STRETCH_PCT = 100.0   # points of five-hour meter movement each side of a split needs
MIN_STRETCH_POINTS = 2    # ...spread over at least this many stretches
# Variance of a stretch's credits-per-percent residual over the denominator-only
# rounding model's, measured on jwork's 21 verified stretches of 17-21 Sep 2026
# (derivation and the two lower estimates: module docstring).
STRETCH_DISPERSION = 80.0


@dataclass(frozen=True)
class WeightedSeries:
    """The error model and evidence floors one weighted series certifies under.

    `numerator_scale` is how many points of error are conceded to the numerator per
    point conceded to the denominator: 1.0 where both are whole-percent meter
    readings (the weekly series), 0.0 where the numerator is exact and only the
    denominator is a meter reading (the credit series). `min_base` and `min_pool`
    are in the denominator's own unit -- points of seven-day movement on the weekly
    series, points of five-hour movement on the credit one.
    """
    name: str
    dispersion: float
    min_base: float
    min_pool: float
    min_points: int
    numerator_scale: float


#: Five-hour over seven-day meter movement, one point per window (tracker/weekly.py).
WINDOWS = WeightedSeries("windows_per_week", WINDOW_DISPERSION, MIN_BASE_D7, MIN_POOL_D7,
                         MIN_POOL_POINTS, 1.0)
#: Meter credits over five-hour meter percent, one point per stretch (tracker/gs_passive.py).
CREDIT_STRETCHES = WeightedSeries("credits_per_window", STRETCH_DISPERSION, MIN_STRETCH_PCT,
                                  MIN_STRETCH_PCT, MIN_STRETCH_POINTS, 0.0)


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
    found: list[tuple[int, int, int]] = []  # (lo, split, hi) of each split taken

    def segment(lo: int, hi: int) -> None:
        """Binary segmentation using persistent levels on both sides.

        Looking at the entire candidate sides prevents one stray value in a
        rolling window from stealing the onset and prevents the remainder of
        one transition from firing a second event.
        """
        best: tuple[float, int] | None = None
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
                best = (score, split)
        if best is None:
            return
        split = best[1]
        found.append((lo, split, hi))
        segment(lo, split)
        segment(split, hi)

    segment(0, len(ordered))
    starts = sorted({0, *(split for _, split, _ in found)})
    events: list[ChangeEvent] = []
    for lo, split, hi in found:
        # Magnitude from the adjacent final segments, as detect_weighted_changes does:
        # the side a split was chosen on can still hold a later step, and its median
        # blends the two (100 x5, 50 x5, 25 x5 read -62% for the first step).
        n = starts.index(split)
        before = median(v for _, v in ordered[starts[n - 1]:split])
        after = median(v for _, v in ordered[split:starts[n + 1] if n + 1 < len(starts) else len(ordered)])
        if not before:
            continue
        ratio = after / before - 1
        events.append(ChangeEvent(
            ordered[split][0].date(), "increased" if ratio > 0 else "decreased",
            round(abs(ratio) * 100), onset_earliest=ordered[split - 1][0].date(),
            onset_latest=ordered[split][0].date(),
            confirmed_at=ordered[min(hi - 1, split + min_points - 1)][0].date(),
            evidence_points=hi - lo,
        ))
    return sorted(events, key=lambda e: e.date)


def pooled_windows(points: list[tuple]) -> float | None:
    """Total five-hour movement over total seven-day movement, or None with no d7."""
    d5 = sum(p[1] for p in points)
    d7 = sum(p[2] for p in points)
    return d5 / d7 if d7 > 0 else None


def _pieces(point: tuple) -> int:
    """How many disconnected rounded differences a (ts, d5, d7[, pieces]) point sums."""
    return int(point[3]) if len(point) > 3 and point[3] else 1


def rounding_error(pieces: int, series: WeightedSeries = WINDOWS) -> float:
    """Points of error conceded to a total pooled from `pieces` separate differences (module docstring, 3.)."""
    return ROUNDING_Z * math.sqrt(series.dispersion * pieces / 6)


def ratio_interval(d5: float, d7: float, pieces: int,
                   series: WeightedSeries = WINDOWS) -> tuple[float | None, float | None]:
    """The lowest and highest d5/d7 the whole-percent readings allow (module docstring, 3.).

    The upper end is None when the conceded error could take d7 to zero: the pool
    cannot bound the ratio from above at all. `series.numerator_scale` says how much
    of the same error the numerator carries -- all of it where the numerator is a
    meter reading too, none of it where it is an exact credit total.
    """
    e = rounding_error(pieces, series)
    en = e * series.numerator_scale
    lo = max(0.0, d5 - en) / (d7 + e) if d7 + e > 0 else None
    hi = (d5 + en) / (d7 - e) if d7 > e else None
    return lo, hi


def pooled_interval(points: list[tuple], series: WeightedSeries = WINDOWS) -> tuple[float | None, float | None]:
    """ratio_interval over a pool of window points."""
    if not points:
        return None, None
    return ratio_interval(sum(p[1] for p in points), sum(p[2] for p in points),
                          sum(_pieces(p) for p in points), series)


def _certified_change(before: list[tuple], after: list[tuple], threshold: float,
                      series: WeightedSeries = WINDOWS) -> float | None:
    """after's pooled level over before's, minus one, when it certifies a change; else None."""
    if len(before) < series.min_points or len(after) < series.min_points:
        return None
    if sum(p[2] for p in before) < series.min_base or sum(p[2] for p in after) < series.min_pool:
        return None
    level_before, level_after = pooled_windows(before), pooled_windows(after)
    # A zero level on either side is not a window budget: over at least MIN_POOL_D7
    # points of seven-day movement the five-hour meter never moved, which is a
    # capped or stale five-hour meter, not a plan with no windows. Without this a
    # capped tail certified as "decreased 100%".
    if not level_before or not level_after:
        return None
    change = level_after / level_before - 1
    if abs(change) <= threshold:
        return None
    blo, bhi = pooled_interval(before, series)
    alo, ahi = pooled_interval(after, series)
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


def detect_weighted_changes(points: list[tuple], threshold: float = 0.15,
                            series: WeightedSeries = WINDOWS) -> list[ChangeEvent]:
    """Certified step changes in a (window_ending, d5, d7[, pieces]) series -- see the module docstring.

    Points where neither meter moved are dropped; a point with d7 == 0 still pools.
    Events are dated by the first window at the new level, in whatever zone the
    timestamps carry.
    """
    return _detect_weighted(points, threshold, series)[0]


def detect_credit_changes(points: list[tuple], threshold: float = 0.15) -> list[ChangeEvent]:
    """Certified step changes in a (stretch_end, credits, delta_pct[, pieces]) series.

    The same detector under the credit series' own error model and floors
    (CREDIT_STRETCHES, and the module docstring's "Credit stretches"): pooled
    credits per meter percent, each stretch weighted by the meter movement it
    carries, both sides of a split above MIN_STRETCH_PCT points of that movement,
    and the two intervals separated. `denominator_pct` on the event is the
    five-hour meter percent behind the new level, not seven-day percent.
    """
    return _detect_weighted(points, threshold, CREDIT_STRETCHES)[0]


def current_regime_points(points: list[tuple], threshold: float = 0.15,
                          series: WeightedSeries = WINDOWS) -> list[tuple]:
    """The points of the current regime, oldest first: from the newest event's
    first window on, or the whole series when nothing has been certified. Compared
    by full timestamp, so an earlier window on the event's own day (still at the old
    level) stays out of the new regime."""
    _, ordered, starts = _detect_weighted(points, threshold, series)
    return ordered[starts[-1]:]


def weighted_regimes(points: list[tuple], threshold: float = 0.15,
                     series: WeightedSeries = WINDOWS) -> list[dict]:
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
    _, ordered, starts = _detect_weighted(points, threshold, series)
    regimes = []
    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(ordered)
        span = ordered[start:stop]
        level = pooled_windows(span)
        if not span or level is None:
            continue
        lo, hi = pooled_interval(span, series)
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


def _detect_weighted(points: list[tuple], threshold: float,
                     series: WeightedSeries = WINDOWS) -> tuple[list[ChangeEvent], list[tuple], list[int]]:
    """(events, ordered points, regime start indices). `starts` always begins with 0 --
    the series before any event is itself a regime -- and gains each certified split,
    so starts[-1] is the current regime's start."""
    ordered = sorted((p for p in points if p[1] >= 0 and p[2] >= 0 and (p[1] > 0 or p[2] > 0)),
                     key=lambda p: p[0])
    found: list[tuple[int, int, int]] = []  # (lo, split, hi) of each certified split

    def segment(lo: int, hi: int) -> None:
        whole = _residual(ordered[lo:hi])
        best: tuple[float, int] | None = None
        for split in range(lo + series.min_points, hi - series.min_points + 1):
            before, after = ordered[lo:split], ordered[split:hi]
            if _certified_change(before, after, threshold, series) is None:
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
        confirmed = next(j for j in range(split + series.min_points, hi + 1)
                         if _certified_change(ordered[lo:split], ordered[split:j], threshold, series) is not None)
        events.append(ChangeEvent(
            ordered[split][0].date(), "increased" if change > 0 else "decreased", round(abs(change) * 100),
            onset_earliest=ordered[split - 1][0].date(), onset_latest=ordered[split][0].date(),
            confirmed_at=ordered[confirmed - 1][0].date(), evidence_points=len(before) + len(after),
            denominator_pct=round(sum(p[2] for p in after), 1),
            before_interval=pooled_interval(before, series), after_interval=pooled_interval(after, series),
        ))
    return sorted(events, key=lambda e: e.date), ordered, starts


def latest_change(events: list[ChangeEvent]) -> ChangeEvent | None:
    return max(events, key=lambda e: e.date) if events else None
