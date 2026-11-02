"""The end of the five-hour limit, found from each account's own meter log.

From 2026-10-30 Anthropic began removing the five-hour limit from Max 20x accounts, which it
now names "Ultra", one account at a time. The usage endpoint sends `five_hour: null` beside a
normal seven-day meter (a weekly-only reading, tracker/meter_log.py). No date is written down
here. Each account's switch is read off its own log by tracker.gs_passive (`meter_fields`,
recorded in `accounts.<name>.meter`):

- `five_hour_last`: the account's newest reading with a five-hour figure;
- `weekly_only_since`: the first weekly-only reading after it, when the account's newest
  reading is weekly-only. Null while the account still has a five-hour limit.

An account has `switched` once it has read weekly-only for MIN_WEEKLY_ONLY_HOURS, so one odd
reading cannot end a series. The five-hour limit ends for the tracker at the first account's
switch (`end_of`: the earliest `five_hour_last` among the switched accounts). After that
instant the four-account rule cannot hold on a five-hour line: fewer accounts carry one, and
those that still do are losing it. So from there the tracker measures the weekly limit only.
`trimmed` cuts every five-hour reading at that instant, which freezes every five-hour figure
(tokens per window, windows per week, the five-hour and ratio lines of the detector) at its
level there:

- a stretch (a five-hour unit) ending after it is dropped;
- a window point (five-hour against seven-day) ending after it is dropped;
- a seven-day step ending after it keeps its credits and its seven-day point, but its
  five-hour movement is marked unknown (`five_hour_measured` false), as a weekly-only step
  already is (tracker/weekly_meter.py). One that a dropped stretch the capture check
  withheld overlaps is marked `withheld_by_stretch`, so `clean_steps` still leaves it out.

The weekly limit reads every seven-day step on both sides of the end. An account that still
has a five-hour limit (avis, on 2026-11-02) needs nothing done when it switches: its readings
past the end were never read as five-hour evidence.

Stdlib only: the invariants read the public block.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

#: How long an account must read weekly-only before it counts as switched.
MIN_WEEKLY_ONLY_HOURS = 6.0

RULE = ("The five-hour limit ends at the first Max account's switch to weekly-only readings "
        "(the usage endpoint's five_hour null), each switch read off that account's own meter "
        "log. Every five-hour figure is frozen there; the weekly limit carries on.")


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def meter_fields(samples: list, weekly_samples: list) -> dict:
    """`five_hour_last` and `weekly_only_since` for one account's meter block.

    `samples` are its five-hour readings, and `weekly_samples` every reading including the
    weekly-only ones (`five_hour` None), both in time order (tracker.gs_passive `load_samples`).
    """
    last5 = samples[-1].ts if samples else None
    since = None
    if weekly_samples and weekly_samples[-1].five_hour is None:
        since = next((s.ts for s in weekly_samples
                      if s.five_hour is None and (last5 is None or s.ts > last5)), None)
    return {"five_hour_last": _iso(last5), "weekly_only_since": _iso(since)}


def _accounts(*reports: dict | None) -> Iterable[tuple[str, dict]]:
    for report in reports:
        for name, body in ((report or {}).get("accounts") or {}).items():
            if isinstance(body, dict):
                yield name, body


def switches(*reports: dict | None) -> dict[str, dict]:
    """Every account's switch record by account name, from the passive reports' meter blocks.

    `switched` is true once the account's newest reading (`meter.last`) is at least
    MIN_WEEKLY_ONLY_HOURS past `weekly_only_since`. A report written before these fields
    existed gives an account with both null, which has not switched.
    """
    out = {}
    for name, body in _accounts(*reports):
        meter = body.get("meter") or {}
        last5, since, newest = (_parse(meter.get(k)) for k in ("five_hour_last", "weekly_only_since", "last"))
        switched = bool(since and newest and newest - since >= timedelta(hours=MIN_WEEKLY_ONLY_HOURS))
        out[name] = {"five_hour_last": _iso(last5), "weekly_only_since": _iso(since), "switched": switched}
    return out


def end_of(*reports: dict | None) -> datetime | None:
    """The instant the tracker's five-hour series ends: the earliest `five_hour_last` among the
    switched accounts, or None while no account has switched."""
    ends = [_parse(s["five_hour_last"]) or _parse(s["weekly_only_since"])
            for s in switches(*reports).values() if s["switched"]]
    ends = [t for t in ends if t is not None]
    return min(ends) if ends else None


def _after(stamp: str | None, end: datetime) -> bool:
    t = _parse(stamp)
    return t is not None and t > end


def _trimmed_step(step: dict, end: datetime, withheld: list[tuple[datetime, datetime]]) -> dict:
    """One seven-day step past `end`: its five-hour movement unknown, and marked
    `withheld_by_stretch` where a dropped, withheld stretch overlaps it."""
    if not _after(step.get("end"), end):
        return step
    out = {**step, "five_hour_measured": False}
    a, b = _parse(step.get("start")), _parse(step.get("end"))
    if a and b and any(s < b and e > a for s, e in withheld):
        out["withheld_by_stretch"] = True
    return out


def trimmed(report: dict | None, end: datetime | None) -> dict | None:
    """`report` (a tracker.gs_passive report) with every five-hour reading after `end` cut, as
    the module docstring lists. The input is not changed. None in, None out; no end, as is."""
    if report is None or end is None:
        return report
    accounts = {}
    for name, body in (report.get("accounts") or {}).items():
        if not isinstance(body, dict):
            accounts[name] = body
            continue
        body = dict(body)
        # A stretch the capture check withheld still withholds the seven-day steps it overlaps
        # (tracker/weekly_meter.py `clean_steps`), so dropping it must not admit them: they are
        # marked `withheld_by_stretch` instead.
        withheld = [(_parse(s["start"]), _parse(s["end"])) for s in body.get("stretches") or []
                    if _after(s.get("end"), end) and s.get("start") and s.get("status") != "accepted"]
        if body.get("stretches") is not None:
            body["stretches"] = [s for s in body["stretches"] if not _after(s.get("end"), end)]
        if body.get("weekly_by_window") is not None:
            body["weekly_by_window"] = [p for p in body["weekly_by_window"]
                                        if not _after(p.get("window_ending"), end)]
        if body.get("weekly_steps") is not None:
            body["weekly_steps"] = [_trimmed_step(s, end, withheld) for s in body["weekly_steps"]]
        accounts[name] = body
    return {**report, "accounts": accounts}


def trimmed_passive(passive: dict | None, end: datetime | None) -> dict | None:
    """history/passive.json (tracker.passive) with its window points after `end` cut."""
    if not passive or end is None:
        return passive
    ww = passive.get("weekly_windows")
    if not isinstance(ww, dict) or ww.get("by_window") is None:
        return passive
    return {**passive, "weekly_windows": {
        **ww, "by_window": [p for p in ww["by_window"] if not _after(p.get("window_ending"), end)]}}


def apply(gs_passive: dict | None, masterrig_passive: dict | None
          ) -> tuple[dict | None, dict | None, datetime | None]:
    """Both passive reports trimmed at their common end, and the end itself."""
    end = end_of(gs_passive, masterrig_passive)
    return trimmed(gs_passive, end), trimmed(masterrig_passive, end), end


def public_block(gs_passive: dict | None, masterrig_passive: dict | None,
                 labels: dict[str, str]) -> dict:
    """The page's `five_hour_limit` block: whether the five-hour limit is still measured, the
    instant its series ended, and each account's switch by page label (never by name)."""
    sw = switches(gs_passive, masterrig_passive)
    end = end_of(gs_passive, masterrig_passive)
    return {
        "status": "removed" if end is not None else "in_force",
        "ended_at": _iso(end),
        "plan": "max20",
        "plan_display_name": "Ultra",
        # Only accounts with a page label are listed: an account's name is never published.
        "accounts": {labels[n]: {"switched": s["switched"],
                                 "five_hour_last": s["five_hour_last"] if s["switched"] else None,
                                 "weekly_only_since": s["weekly_only_since"] if s["switched"] else None}
                     for n, s in sorted(sw.items(), key=lambda kv: labels.get(kv[0], ""))
                     if n in labels},
        "rule": RULE,
    }
