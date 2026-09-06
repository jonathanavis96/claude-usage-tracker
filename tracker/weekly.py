"""How many full five-hour windows one week's cap holds, from the passive meter log.

Jonathan's account reports both a five-hour utilization percentage and a
seven-day (weekly) one in the same moonlighter samples. Watching how much the
five-hour meter moves for a given amount of seven-day movement, within a
single five-hour window and a single weekly window, gives the ratio directly
-- no need to assume the published site multiplies by a fixed 28.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Iterable

_FIVE_HOUR_TOLERANCE = timedelta(seconds=120)
_MIN_FIVE_HOUR_PCT = 50.0


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
        fh = d.get("five_hour") or {}
        sd = d.get("seven_day") or {}
        if (not d.get("ts") or fh.get("utilization") is None or fh.get("resets_at") is None
                or sd.get("utilization") is None or sd.get("resets_at") is None):
            rows.append(None)
            continue
        rows.append({
            "ts": d["ts"],
            "five_hour": float(fh["utilization"]),
            "five_resets_at": fh["resets_at"],
            "seven_day": float(sd["utilization"]),
            "seven_resets_at": sd["resets_at"],
        })
    return rows


def _same_five_hour_window(prev: dict, cur: dict) -> bool:
    p = datetime.fromisoformat(prev["five_resets_at"])
    c = datetime.fromisoformat(cur["five_resets_at"])
    return abs((c - p).total_seconds()) < _FIVE_HOUR_TOLERANCE.total_seconds()


def _same_weekly_window(prev: dict, cur: dict) -> bool:
    return prev["seven_resets_at"][:13] == cur["seven_resets_at"][:13]


def weekly_windows(rows: list[dict | None], now: datetime | None = None) -> dict:
    """Bucket consecutive same-window deltas by week, and turn each week's

    total five-hour movement over total seven-day movement into a count of
    five-hour windows the week holds. Returns
    {"current": float|None, "history": [{"week_ending", "windows",
    "five_hour_pct", "seven_day_pct"}, ...]} sorted ascending by week_ending.

    `current` is the median of the last two COMPLETE weeks in the history
    (a week is complete once its seven-day reset time is in the past); the
    newest, still-open week is kept in history but never counted as current.
    """
    now = now or datetime.now(timezone.utc)
    buckets: dict[str, dict] = {}
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
        prev = cur

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
        del h["_resets_at"]

    return {"current": current, "history": history}
