"""Parse the two passive utilization logs on masterrig into Sample rows."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

_CEIL = re.compile(r"^(\S+) 5-hour (\d+)% / 7-day (\d+)%")
_PRIORITY = {"ceiling": 0, "moonlighter": 1}  # lower wins


@dataclass(frozen=True)
class Sample:
    ts: datetime
    five_hour: float
    seven_day: float | None
    resets_at: str | None
    source: str


def parse_moonlighter(lines: Iterable[str]) -> list[Sample]:
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        fh = (d.get("five_hour") or {})
        if fh.get("utilization") is None or not d.get("ts"):
            continue
        sd = (d.get("seven_day") or {}).get("utilization")
        out.append(Sample(datetime.fromisoformat(d["ts"]), float(fh["utilization"]),
                          float(sd) if sd is not None else None, fh.get("resets_at"), "moonlighter"))
    return out


def parse_ceiling_log(lines: Iterable[str]) -> list[Sample]:
    out = []
    for line in lines:
        m = _CEIL.match(line)
        if not m:
            continue
        out.append(Sample(datetime.fromisoformat(m.group(1)), float(m.group(2)), float(m.group(3)), None, "ceiling"))
    return out


def merge_samples(*lists: list[Sample]) -> list[Sample]:
    by_minute: dict[datetime, Sample] = {}
    for s in sorted((s for lst in lists for s in lst), key=lambda s: s.ts):
        key = s.ts.replace(second=0, microsecond=0)
        cur = by_minute.get(key)
        if cur is None or _PRIORITY[s.source] < _PRIORITY[cur.source]:
            by_minute[key] = s
    return [by_minute[k] for k in sorted(by_minute)]
