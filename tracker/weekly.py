"""How many full five-hour windows one week's cap holds, from the passive meter log.

Jonathan's account reports both a five-hour utilization percentage and a
seven-day (weekly) one in the same moonlighter samples. Watching how much the
five-hour meter moves for a given amount of seven-day movement, within a
single five-hour window and a single weekly window, gives the ratio directly
-- no need to assume the published site multiplies by a fixed 28.
"""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Iterable

_FIVE_HOUR_TOLERANCE = timedelta(seconds=120)
_MIN_FIVE_HOUR_PCT = 50.0
_MIN_PROBE_FIVE_HOUR_PCT = 20.0
_MIN_PROBE_SEVEN_DAY_PCT = 10.0


def parse_rows(lines: Iterable[str]) -> list[dict | None]:
    """Parse usage_log.jsonl lines into rows, or None for a row to skip.

    A None marks a line that could not be parsed, or had a null utilization
    or resets_at -- it breaks the prev/cur pairing in weekly_windows so a gap
    in the log is never mistaken for two consecutive samples.
    """
    rows: list[dict | None] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            rows.append(None)
            continue
        rows.append(parse_row(d))
    return rows


def parse_row(d: dict) -> dict | None:
    """One parsed usage_log row (the shape parse_rows returns), or None to skip it.

    A contributed sample (contrib/sample.py) carries the same `ts`, `five_hour`
    and `seven_day` fields, so tracker/contributed.py pairs its rows with the
    same code.
    """
    if not isinstance(d, dict):
        return None
    fh = d.get("five_hour") or {}
    sd = d.get("seven_day") or {}
    if (not d.get("ts") or fh.get("utilization") is None or fh.get("resets_at") is None
            or sd.get("utilization") is None or sd.get("resets_at") is None):
        return None
    return {
        "ts": d["ts"],
        "five_hour": float(fh["utilization"]),
        "five_resets_at": fh["resets_at"],
        "seven_day": float(sd["utilization"]),
        "seven_resets_at": sd["resets_at"],
    }


def _same_five_hour_window(prev: dict, cur: dict) -> bool:
    p = datetime.fromisoformat(prev["five_resets_at"])
    c = datetime.fromisoformat(cur["five_resets_at"])
    return abs((c - p).total_seconds()) < _FIVE_HOUR_TOLERANCE.total_seconds()


def _same_weekly_window(prev: dict, cur: dict) -> bool:
    return prev["seven_resets_at"][:13] == cur["seven_resets_at"][:13]


def _window_point(window_ending: str, d5: float, d7: float) -> dict:
    """One per-window point of the `by_window` series (weekly_windows, probe_weekly_windows).

    `windows` is null when the seven-day meter did not move: the point is real
    five-hour movement that the pooled figures still count, but has no ratio of
    its own.
    """
    return {"window_ending": window_ending, "windows": round(d5 / d7, 2) if d7 > 0 else None,
            "five_hour_pct": round(d5, 1), "seven_day_pct": round(d7, 1)}


def weekly_windows(rows: list[dict | None], now: datetime | None = None) -> dict:
    """Bucket consecutive same-window deltas by week, and turn each week's

    total five-hour movement over total seven-day movement into a count of
    five-hour windows the week holds. Returns
    {"current": float|None, "history": [{"week_ending", "windows",
    "five_hour_pct", "seven_day_pct"}, ...], "by_window": [{"window_ending",
    "windows", "five_hour_pct", "seven_day_pct"}, ...]}, both series sorted
    ascending.

    `current` is the median of the last two COMPLETE weeks in the history
    (a week is complete once its seven-day reset time is in the past); the
    newest, still-open week is kept in history but never counted as current.
    Each history row carries `"partial"`: true when its seven-day reset is
    still in the future, so a consumer never has to infer it from dates.

    `by_window` is the same paired movement bucketed by five-hour window
    instead of by week -- one point per window, keyed by the window's reset
    time (the first sample's `five_resets_at`; later samples in the window
    may drift by a few seconds). It exists because a calendar week blends a
    mid-week step in the weekly cap into the week's average, which is how
    the 2026-09-13 cut published as a -14.5% week and stayed under the
    detector's threshold (issue #25). Change detection and the live plan's
    `current` run on this series (tracker/detect.py, tracker/publish.py).
    It is deliberately NOT thinned by the 50-point weekly floor or by any
    per-window floor: a thin window is not a vote on its own, but its
    movement still belongs in the pooled sums, and the detector applies its
    own weight rules.
    """
    now = now or datetime.now(timezone.utc)
    buckets: dict[str, dict] = {}
    windows: list[dict] = []
    prev: dict | None = None
    for cur in rows:
        if cur is None:
            prev = None
            continue
        if prev is not None and _same_five_hour_window(prev, cur) and _same_weekly_window(prev, cur):
            d5 = cur["five_hour"] - prev["five_hour"]
            d7 = cur["seven_day"] - prev["seven_day"]
            if d5 > 0 and d7 >= 0:
                week_key = cur["seven_resets_at"][:10]
                b = buckets.setdefault(week_key, {"d5": 0.0, "d7": 0.0, "resets_at": cur["seven_resets_at"]})
                b["d5"] += d5
                b["d7"] += d7
                b["resets_at"] = cur["seven_resets_at"]
                if windows and _same_five_hour_window(windows[-1], cur):
                    windows[-1]["d5"] += d5
                    windows[-1]["d7"] += d7
                else:
                    windows.append({"five_resets_at": prev["five_resets_at"], "d5": d5, "d7": d7})
        prev = cur
    by_window = [_window_point(w["five_resets_at"], w["d5"], w["d7"]) for w in windows]

    history = []
    for week_key in sorted(buckets):
        b = buckets[week_key]
        if b["d7"] > 0 and b["d5"] >= _MIN_FIVE_HOUR_PCT:
            history.append({
                "week_ending": week_key,
                "windows": round(b["d5"] / b["d7"], 2),
                "five_hour_pct": round(b["d5"], 1),
                "seven_day_pct": round(b["d7"], 1),
                "_resets_at": b["resets_at"],
            })

    complete = [h for h in history if datetime.fromisoformat(h["_resets_at"]) <= now]
    current = round(median(h["windows"] for h in complete[-2:]), 2) if complete else None

    for h in history:
        h["partial"] = datetime.fromisoformat(h["_resets_at"]) > now
        del h["_resets_at"]

    return {"current": current, "history": history, "by_window": by_window}


def _iso_week_ending(ts: str) -> str:
    """The Sunday date (ISO week end) of the ISO week `ts` falls in, as YYYY-MM-DD.

    A probe row does not carry the seven-day meter's own resets_at, so its week
    key falls back to calendar ISO weeks rather than the meter's own reset
    boundary.
    """
    d = datetime.fromisoformat(ts).date()
    iso_year, iso_week, iso_weekday = d.isocalendar()
    sunday = d + timedelta(days=7 - iso_weekday)
    return sunday.isoformat()


def probe_weekly_windows(rows: list[dict], now: datetime | None = None) -> dict:
    """Same shape as weekly_windows, but derived from probe rows' whole-run deltas.

    Each probe row (tracker/probe.py's ProbeResult, asdict'd) already spans one
    probe run: five_hour_before/after are its first read (before any prompt)
    and its last read at exit, and seven_day_before/after mirror it. A row
    missing any of those four fields (every row from before this field existed)
    is skipped outright, so this is empty until fresh probe rows accumulate.

    d5 = five_hour_after - five_hour_before is discarded when <= 0 (the run
    crossed a five-hour reset mid-probe, so the delta is not a true five-hour
    movement); d7 = seven_day_after - seven_day_before is discarded when < 0.
    Rows are bucketed by the row's own weekly reset date when present
    (`seven_day_resets_at`, first 10 chars), else by the ISO week (Sunday) its
    `ts` falls in. windows = d5/d7 for weeks with d5 >= 20 and d7 >= 10 (a
    single tick-probe run rarely moves the five-hour meter by much, so the
    passive log's 50-point floor would empty this out entirely). The d7 floor
    guards against the seven-day meter's own whole-percent quantisation: a
    week whose summed d7 is only a few points carries error on the order of
    +-33% (one point out of three), which is enough to fabricate a spurious
    jump in `windows` on its own -- three probe rows once published a week
    with d5=21, d7=3 (windows=7.0) purely from that rounding.

    `current` is the median of the last two COMPLETE weeks (a week is complete
    once its week-ending date is in the past). Each history row carries
    `"partial"`: true when its week-ending date is today or later.

    `by_window` is one point per usable row (same shape as weekly_windows's),
    keyed by the row's `ts` since a probe row does not record its five-hour
    reset time: a probe run sits inside one five-hour window by construction
    (d5 <= 0 rows, which crossed a reset, are already out). It carries every
    row that clears the sign checks, with no d5/d7 floor -- the floors above
    are for publishing a week's ratio on its own; per-window points are
    pooled by weight downstream (tracker/detect.py).
    """
    now = now or datetime.now(timezone.utc)
    buckets: dict[str, dict] = {}
    by_window: list[dict] = []
    for r in sorted(rows, key=lambda r: r["ts"]):
        fhb, fha = r.get("five_hour_before"), r.get("five_hour_after")
        sdb, sda = r.get("seven_day_before"), r.get("seven_day_after")
        if fhb is None or fha is None or sdb is None or sda is None:
            continue
        d5 = fha - fhb
        if d5 <= 0:
            continue
        d7 = sda - sdb
        if d7 < 0:
            continue
        by_window.append(_window_point(r["ts"], d5, d7))
        resets_at = r.get("seven_day_resets_at")
        week_key = resets_at[:10] if resets_at else _iso_week_ending(r["ts"])
        b = buckets.setdefault(week_key, {"d5": 0.0, "d7": 0.0})
        b["d5"] += d5
        b["d7"] += d7

    history = []
    for week_key in sorted(buckets):
        b = buckets[week_key]
        if b["d7"] >= _MIN_PROBE_SEVEN_DAY_PCT and b["d5"] >= _MIN_PROBE_FIVE_HOUR_PCT:
            history.append({
                "week_ending": week_key,
                "windows": round(b["d5"] / b["d7"], 2),
                "five_hour_pct": round(b["d5"], 1),
                "seven_day_pct": round(b["d7"], 1),
                "partial": date.fromisoformat(week_key) >= now.date(),
            })

    complete = [h for h in history if date.fromisoformat(h["week_ending"]) < now.date()]
    current = round(median(h["windows"] for h in complete[-2:]), 2) if complete else None

    return {"current": current, "history": history, "by_window": by_window}
