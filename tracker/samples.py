"""Parse the passive utilization logs into Sample rows.

masterrig keeps two: moonlighter's usage_log.jsonl and its ceiling's systemd log.
gs keeps two more, one per probe account: greenscape-org's usage-ceiling.py log,
which has read jwork's meter since 2026-09-05, and tracker.meter_log's JSONL,
which reads Dave's. The gs ceiling log records no reset times, so a window
boundary there can only be inferred from the meter dropping; the meter log
records them, in moonlighter's own row shape.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

_CEIL = re.compile(r"^(\S+) 5-hour (\d+)% / 7-day (\d+)%")
_GS_CEIL = re.compile(r"^(\S+) (?:ok|warn|HARD CEILING \([\w-]+\)) five_hour=(\d+)% seven_day=(\d+)%")
_PRIORITY = {"ceiling": 0, "gs-ceiling": 0, "meter": 0, "moonlighter": 1}  # lower wins


class MixedAccountLog(ValueError):
    """A meter log whose lines name more than one account.

    Two accounts' readings in one file would pair a reading of one meter with a
    reading of the other and average two different limits into one series, with
    nothing in the output to show it. So a mixed log is refused outright; the
    sampler (tracker/meter_log.py) refuses to write one in the first place.
    """


@dataclass(frozen=True)
class Sample:
    ts: datetime
    five_hour: float
    seven_day: float | None
    resets_at: str | None  # the five-hour window's reset
    source: str
    seven_resets_at: str | None = None  # the seven-day window's reset, when the log records it


def parse_moonlighter(lines: Iterable[str], source: str = "moonlighter") -> list[Sample]:
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        fh = (d.get("five_hour") or {})
        if fh.get("utilization") is None or not d.get("ts"):
            continue
        seven = d.get("seven_day") or {}
        sd = seven.get("utilization")
        out.append(Sample(datetime.fromisoformat(d["ts"]), float(fh["utilization"]),
                          float(sd) if sd is not None else None, fh.get("resets_at"), source,
                          seven.get("resets_at")))
    return out


def parse_meter_log(lines: Iterable[str]) -> list[Sample]:
    """tracker.meter_log's JSONL: moonlighter's row shape plus `account` and `identity`.

    Failed reads are `error` lines with no utilization and are skipped, leaving
    a gap in the samples. Raises MixedAccountLog when the lines carry more than
    one `identity`.
    """
    lines = list(lines)
    identities = set()
    for line in lines:
        try:
            ident = json.loads(line).get("identity")
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if ident:
            identities.add(ident)
    if len(identities) > 1:
        raise MixedAccountLog(f"meter log holds {len(identities)} accounts: {sorted(identities)}")
    return parse_moonlighter(lines, source="meter")


def parse_gs_ceiling_log(lines: Iterable[str], since: datetime | None = None) -> list[Sample]:
    """Reading lines of gs's usage-ceiling.py log (`ok`, `warn` and `HARD CEILING`).

    Everything else in that log (seat pauses, read failures) is skipped. The
    log has no reset times, so `resets_at` is None and a reset shows only as
    the meter dropping (tracker/join.py). `since` drops earlier readings: the
    log has not always read the account it reads now (tracker/gs_passive.py
    records from when it has).
    """
    out = []
    for line in lines:
        m = _GS_CEIL.match(line)
        if not m:
            continue
        ts = datetime.fromisoformat(m.group(1))
        if since is not None and ts < since:
            continue
        out.append(Sample(ts, float(m.group(2)), float(m.group(3)), None, "gs-ceiling"))
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


#: Log format name -> parser. The name is what an account's `meter_format` holds
#: (tracker/gs_passive.py), so one account can name several logs of different
#: formats: masterrig's meter is moonlighter's JSONL plus its ceiling's systemd
#: log, the pair tracker/passive.py has always merged by hand.
PARSERS = {"moonlighter": parse_moonlighter, "meter": parse_meter_log,
           "gs-ceiling": parse_gs_ceiling_log, "ceiling": parse_ceiling_log}


def parse_log(fmt: str, lines: Iterable[str], since: datetime | None = None) -> list[Sample]:
    """Parse one log in the named format, dropping readings before `since`.

    Only `gs-ceiling` takes `since` in its own signature (its log has not always
    read the account it reads now); for the others it is applied afterwards, so
    a caller can hand the same cutoff to any format.
    """
    parser = PARSERS.get(fmt)
    if parser is None:
        raise ValueError(f"unknown meter log format: {fmt!r} (known: {', '.join(sorted(PARSERS))})")
    if fmt == "gs-ceiling":
        return parser(lines, since=since)
    return [s for s in parser(lines) if since is None or s.ts >= since]
