"""Use it or lose it: is this week's allowance on pace to be used, or to expire unused?

The seven-day meter resets at a fixed time, and whatever is left of it then is gone. This
reads a meter log (moonlighter's usage_log.jsonl, or tracker.meter_log's JSONL: the row
shape tracker.samples parses), takes the current seven-day window and answers:

- where the meter is now and when it resets;
- where it lands at the reset if the recent pace holds, or when it hits 100% first;
- what pace lands it on exactly 100% at the reset.

    python3 -m tracker.pace                          # ~/.moonlighter/usage_log.jsonl
    python3 -m tracker.pace path/to/meter.jsonl --lookback 12
    python3 -m tracker.pace --json
    python3 -m tracker.pace --now 2026-11-03T05:00:00+02:00   # replay an earlier moment
    python3 -m tracker.pace --each ~/.paperclip/ops/claude-usage-meter-*.log   # one line per account

Read-only: it never calls the usage endpoint and only reads what the logs already hold.
Give it one account's logs; a meter log naming two accounts is refused
(tracker.samples.MixedAccountLog), because two meters in one series make a nonsense pace.
With `--each`, every log is its own account and gets one line: which seat has the most of
its week left to spend.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from .samples import MixedAccountLog, Sample, parse_meter_log
from .usage_api import as_utc, parse_instant

WEEK = timedelta(days=7)
#: Readings of one weekly window carry the same `resets_at` up to sub-second jitter
#: ("...04:00:00.557429" then "...04:00:00.448842"); anything further apart is another window.
RESET_TOLERANCE = timedelta(hours=1)
#: A projection landing at or above this counts as using the week's allowance.
ON_TARGET = 95.0
#: A latest reading older than this is called out: the collector may have stopped, so every
#: figure assumes the meter has not moved since (the samplers read every 5 to 30 minutes).
STALE_AFTER = timedelta(hours=2)
SPARK = "▁▂▃▄▅▆▇█"
DEFAULT_LOG = Path.home() / ".moonlighter/usage_log.jsonl"


@dataclass(frozen=True)
class Pace:
    now: datetime
    reading_at: datetime                 # the latest seven-day reading of the window
    utilization: float                   # the seven-day meter at that reading, percent
    resets_at: datetime
    window_start: datetime               # resets_at - 7 days, where the meter read 0
    rate: float | None                   # percent per hour over the lookback; None if unmeasurable
    rate_since: datetime | None          # where that rate's span starts
    projected: float | None              # the meter at the reset if the rate holds, capped at 100
    full_at: datetime | None             # when the meter reaches 100% at that rate, if before the reset
    needed_rate: float | None            # percent per hour from now that lands on 100% at the reset
    five_hour: float | None              # the five-hour meter at the latest reading (None: weekly-only)
    five_resets_at: datetime | None
    timeline: tuple[float | None, ...]   # the meter per slice of the window; None for slices still to come

    @property
    def window_over(self) -> bool:
        return self.now >= self.resets_at

    @property
    def five_window_over(self) -> bool:
        """The five-hour window of the latest reading has reset by now: its figure is gone."""
        return self.five_resets_at is not None and self.now >= self.five_resets_at

    @property
    def stale(self) -> bool:
        """The latest reading is old enough that the collector may have stopped."""
        return self.now - self.reading_at > STALE_AFTER

    @property
    def unused(self) -> float | None:
        """Points of this week's meter that expire unused if the rate holds."""
        return None if self.projected is None else max(0.0, 100.0 - self.projected)

    @property
    def verdict(self) -> str:
        if self.window_over:
            return "RESET"
        if self.utilization >= 100:
            return "MAXED"
        if self.full_at is not None:
            return "RUNNING HOT"
        if self.projected is not None and self.projected >= ON_TARGET:
            return "ON TARGET"
        if self.projected is None:
            return "UNKNOWN"
        return "USE IT OR LOSE IT"




def _hours(d: timedelta) -> float:
    return d.total_seconds() / 3600


def current_window(samples: Iterable[Sample]) -> tuple[datetime, list[Sample]] | None:
    """The latest seven-day window's reset and its readings, oldest first.

    Only readings with a seven-day figure and a parseable seven-day reset count. The window
    is the one the newest such reading belongs to; a reading counts as that window's when its
    reset is within RESET_TOLERANCE of the newest one's.
    """
    usable = []
    for s in samples:
        reset = parse_instant(s.seven_resets_at)
        if s.seven_day is not None and reset is not None:
            usable.append((as_utc(s.ts), reset, s))
    if not usable:
        return None
    usable.sort(key=lambda u: u[0])
    reset = usable[-1][1]
    window = [s for _, r, s in usable if abs(r - reset) <= RESET_TOLERANCE]
    return reset, window


def _timeline(points: Sequence[tuple[datetime, float]], start: datetime, end: datetime,
              last: datetime, slices: int) -> tuple[float | None, ...]:
    """The meter's highest reading in each of `slices` equal slices of [start, end).

    A slice with no reading carries the level before it forward (the meter never falls
    inside a window); a slice starting after `last` (now) is still to come (None).
    """
    width = (end - start) / slices
    out: list[float | None] = []
    level = 0.0
    i = 0
    for k in range(slices):
        lo, hi = start + k * width, start + (k + 1) * width
        if lo > last:
            out.append(None)
            continue
        while i < len(points) and points[i][0] < hi:
            level = max(level, points[i][1])
            i += 1
        out.append(level)
    return tuple(out)


def measure(samples: Iterable[Sample], now: datetime | None = None, lookback_hours: float = 24.0,
            slices: int = 28) -> Pace | None:
    """Where the current weekly window stands and where it is heading. None without a
    seven-day reading that carries its reset time."""
    if not (math.isfinite(lookback_hours) and lookback_hours > 0):
        raise ValueError("lookback_hours must be positive")
    now = as_utc(now) if now is not None else datetime.now(timezone.utc)
    # Only what was known at `now`: a replay of an earlier moment must not see later readings.
    found = current_window(s for s in samples if as_utc(s.ts) <= now)
    if found is None:
        return None
    resets_at, window = found
    start = resets_at - WEEK
    latest = window[-1]
    at = as_utc(latest.ts)
    level = float(latest.seven_day or 0.0)  # current_window keeps only readings with a figure
    # The meter reads 0 at the window's start, so that instant anchors the pace even before
    # a second reading exists. Readings stamped before the start (a skewed clock) are dropped.
    points = [(start, 0.0)] + [(as_utc(s.ts), float(s.seven_day or 0.0)) for s in window if as_utc(s.ts) >= start]
    since = max(at - timedelta(hours=lookback_hours), start)
    base = max((p for p in points if p[0] <= since), key=lambda p: p[0])
    rate = rate_since = None
    if at > base[0]:
        rate = max(0.0, (level - base[1]) / _hours(at - base[0]))
        rate_since = base[0]
    projected = full_at = needed = None
    # Every figure runs from now, not from the reading: a stale log has fewer hours left than
    # its last reading did, and the meter is assumed not to have moved since (`stale` says so).
    if resets_at > now:
        left = _hours(resets_at - now)
        needed = max(0.0, (100.0 - level) / left)
        if rate is not None:
            projected = min(100.0, level + rate * left)
            if level < 100 and rate > 0:
                hit = now + timedelta(hours=(100.0 - level) / rate)
                if hit < resets_at:
                    full_at = hit
    five = latest.five_hour
    return Pace(now=now, reading_at=at, utilization=level, resets_at=resets_at, window_start=start,
                rate=rate, rate_since=rate_since, projected=projected, full_at=full_at,
                needed_rate=needed, five_hour=five,
                five_resets_at=parse_instant(latest.resets_at) if five is not None else None,
                timeline=_timeline(points, start, resets_at, now, slices))


def sparkline(timeline: Iterable[float | None]) -> str:
    """One block per slice, ▁ for 0% up to █ for 100%; · for a slice still to come."""
    out = []
    for v in timeline:
        if v is None:
            out.append("·")
        else:
            out.append(SPARK[round(min(max(v, 0.0), 100.0) / 100 * (len(SPARK) - 1))])
    return "".join(out)


def duration(d: timedelta) -> str:
    """'2d 23h', '15h 40m', '40m': the two largest units, rounded down to the minute."""
    minutes = max(0, int(d.total_seconds() // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m"


def _stamp(dt: datetime, tz: tzinfo | None) -> str:
    local = dt.astimezone(tz)
    return f"{local:%a} {local.day} {local:%b %H:%M}"


def _row(label: str, text: str) -> str:
    return f"{label:<15}{text}"


def render(p: Pace, tz: tzinfo | None = None) -> str:
    """The text report. `tz` is the display time zone (None: this machine's local zone)."""
    lines = [_row("Weekly meter", f"{p.utilization:.0f}%  [{sparkline(p.timeline)}]  "
                  f"resets {_stamp(p.resets_at, tz)}"
                  + ("" if p.window_over else f" (in {duration(p.resets_at - p.now)})"))]
    if p.window_over:
        lines.append(_row("Verdict", f"RESET: the window reset {duration(p.now - p.resets_at)} ago; "
                          "no reading of the new one yet."))
        return "\n".join(lines)
    if p.stale:
        lines.append(_row("Latest reading", f"{duration(p.now - p.reading_at)} old: the figures assume "
                          "nothing has changed since."))
    if p.rate is None or p.rate_since is None:
        lines.append(_row("Recent pace", "unknown: not enough readings in this window yet."))
    else:
        lines.append(_row("Recent pace", f"+{p.rate:.2f}%/h since {_stamp(p.rate_since, tz)}"))
    v = p.verdict
    if v == "MAXED":
        lines.append(_row("Verdict", f"MAXED: nothing left until the reset, in {duration(p.resets_at - p.now)}."))
    elif v == "RUNNING HOT" and p.full_at is not None:
        lines.append(_row("Verdict", f"RUNNING HOT: 100% on {_stamp(p.full_at, tz)}, "
                          f"{duration(p.resets_at - p.full_at)} before the reset."))
    elif v == "ON TARGET" and p.projected is not None:
        lines.append(_row("Verdict", f"ON TARGET: about {p.projected:.0f}% at the reset. This week gets used."))
    elif v == "USE IT OR LOSE IT" and p.projected is not None and p.unused is not None:
        lines.append(_row("Verdict", f"USE IT OR LOSE IT: about {p.projected:.0f}% at the reset, "
                          f"so {p.unused:.0f} points expire unused."))
    elif v == "UNKNOWN":
        lines.append(_row("Verdict", "UNKNOWN: not enough readings in this window to tell where it lands."))
    if p.needed_rate is not None and v in ("USE IT OR LOSE IT", "UNKNOWN"):
        times = f" ({p.needed_rate / p.rate:.1f}x your pace)" if p.rate else ""
        lines.append(_row("To use it all", f"{p.needed_rate:.2f}%/h from here{times}, "
                          f"about {p.needed_rate * 24:.0f} points a day."))
    if p.five_hour is None:
        # No figure is either an idle meter with no window open or an account with no five-hour
        # limit at all (weekly-only): the meter log's samples do not tell the two apart.
        lines.append(_row("5-hour meter", "no figure at the latest reading (no window open, or no 5-hour limit)"))
    elif p.five_window_over:
        lines.append(_row("5-hour meter", f"reset {_stamp(p.five_resets_at, tz)}, no reading since"))
    else:
        resets = f", resets {_stamp(p.five_resets_at, tz)}" if p.five_resets_at else ""
        lines.append(_row("5-hour meter", f"{p.five_hour:.0f}%{resets}"))
    return "\n".join(lines)


def as_json(p: Pace) -> dict:
    def iso(dt: datetime | None) -> str | None:
        return None if dt is None else dt.isoformat()

    def num(x: float | None, places: int = 3) -> float | None:
        return None if x is None else round(x, places)

    return {
        "now": iso(p.now), "reading_at": iso(p.reading_at), "stale": p.stale,
        "reading_age_hours": num(_hours(p.now - p.reading_at), 2), "utilization": p.utilization,
        "resets_at": iso(p.resets_at), "window_start": iso(p.window_start),
        "rate_per_hour": num(p.rate), "rate_since": iso(p.rate_since),
        "projected_at_reset": num(p.projected, 1), "unused_at_reset": num(p.unused, 1),
        "full_at": iso(p.full_at), "needed_rate_per_hour": num(p.needed_rate),
        "five_hour": p.five_hour, "five_hour_resets_at": iso(p.five_resets_at),
        "five_hour_window_over": p.five_window_over,
        "verdict": p.verdict, "timeline": list(p.timeline),
    }


def summary(p: Pace, tz: tzinfo | None = None) -> str:
    """The verdict in one line, for `--each`."""
    line = _summary(p, tz)
    return f"{line} (latest reading {duration(p.now - p.reading_at)} old)" if p.stale and not p.window_over else line


def _summary(p: Pace, tz: tzinfo | None) -> str:
    v = p.verdict
    if v == "RESET":
        return f"RESET {duration(p.now - p.resets_at)} ago, no reading since"
    if v == "MAXED":
        return f"MAXED until the reset, {_stamp(p.resets_at, tz)}"
    if v == "RUNNING HOT" and p.full_at is not None:
        return f"RUNNING HOT: 100% {duration(p.resets_at - p.full_at)} before the reset"
    if v == "ON TARGET" and p.projected is not None:
        return f"ON TARGET: about {p.projected:.0f}% at the reset"
    if v == "USE IT OR LOSE IT" and p.projected is not None and p.unused is not None:
        need = f", needs {p.needed_rate:.2f}%/h" if p.needed_rate is not None else ""
        return f"USE IT OR LOSE IT: about {p.projected:.0f}% at the reset, {p.unused:.0f} points unused{need}"
    return "pace unknown: not enough readings yet"


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _each(paths: Sequence[Path], now: datetime | None, lookback: float, as_json_out: bool, out) -> int:
    """One line per log, each its own account: which seat has the most week left to spend."""
    rows: list[tuple[str, Pace | None, str | None]] = []
    for path in paths:
        try:
            p = measure(parse_meter_log(_read(path), weekly=True), now=now, lookback_hours=lookback)
            rows.append((str(path), p, None if p is not None else "no seven-day reading with a reset time"))
        except OSError as e:
            rows.append((str(path), None, f"cannot read: {e.strerror or e}"))
        except MixedAccountLog as e:
            rows.append((str(path), None, str(e)))
    if as_json_out:
        print(json.dumps([{"log": name, **(as_json(p) if p else {}), **({"error": err} if err else {})}
                          for name, p, err in rows], indent=2), file=out)
    else:
        width = max(len(Path(name).name) for name, _, _ in rows)
        for name, p, err in rows:
            label = Path(name).name.ljust(width)
            if p is None:
                print(f"{label}   --  {err}", file=out)
            else:
                print(f"{label}  {p.utilization:>3.0f}%  {sparkline(p.timeline)}  {summary(p)}", file=out)
    return 0 if any(p is not None for _, p, _ in rows) else 1


def main(argv: Sequence[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python3 -m tracker.pace",
                                 description="Is this week's Claude allowance on pace to be used before the reset?")
    ap.add_argument("logs", nargs="*", type=Path,
                    help=f"meter logs of one account (default: {DEFAULT_LOG}); with --each, one per account")
    ap.add_argument("--now", help="evaluate at this ISO time instead of now (a naive time is UTC)")
    ap.add_argument("--lookback", type=float, default=24.0, help="hours of readings the pace is measured over")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the text report")
    ap.add_argument("--each", action="store_true",
                    help="read each log as its own account and print one line per log")
    args = ap.parse_args(argv)
    paths = args.logs or [DEFAULT_LOG]
    if not (math.isfinite(args.lookback) and args.lookback > 0):
        print("pace: --lookback must be positive", file=sys.stderr)
        return 2
    now = None
    if args.now:
        now = parse_instant(args.now)
        if now is None:
            print(f"pace: --now is not an ISO time: {args.now!r}", file=sys.stderr)
            return 2
    if args.each:
        return _each(paths, now, args.lookback, args.json, out)
    lines: list[str] = []
    for path in paths:
        try:
            lines.extend(_read(path))
        except OSError as e:
            print(f"pace: cannot read {path}: {e.strerror or e}", file=sys.stderr)
            return 2
    try:
        samples = parse_meter_log(lines, weekly=True)
    except MixedAccountLog as e:
        print(f"pace: {e}", file=sys.stderr)
        return 2
    if len(paths) > 1:
        # A meter log's rows may carry no account identity (moonlighter's do not), so two
        # accounts' logs cannot be told apart from one account's rotated logs: say so.
        print(f"pace: {len(paths)} logs read as one account; for one line per account use --each",
              file=sys.stderr)
    p = measure(samples, now=now, lookback_hours=args.lookback)
    if p is None:
        print("pace: no seven-day reading with a reset time in " + ", ".join(map(str, paths)), file=sys.stderr)
        return 1
    print(json.dumps(as_json(p), indent=2) if args.json else render(p), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
