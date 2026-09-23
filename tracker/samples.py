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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

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


_FIVE_HOURS = timedelta(hours=5)


def infer_resets(samples: Iterable[Sample]) -> list[Sample]:
    """Fill `resets_at` on reset-less samples from utilization alone (audit finding 10).

    Two pieces of evidence, used together, each gated so a guess is never made:

    - A drop in five-hour utilization between consecutive samples means a reset
      happened somewhere in that gap. A reset can only recur once its window has
      run its full five hours, so a gap of five hours or less can hide at most
      one reset -- the drop is then unambiguous, and could not also be read as
      two resets landing on the observed value. A gap over five hours could hide
      a whole extra window (a reset opening and closing unseen), so a drop across
      one is left unresolved. A reading pinned at the same value for several
      samples running up to the drop (real data: a meter pinned at its 100%
      ceiling) can hide the true reset anywhere across that whole flat run, not
      just in its last gap, so the five-hour bound is measured from the start of
      that run, not from the sample immediately before the drop.
    - The sample right after that drop is the first reading of the new window --
      a window starts at the first use after the previous one ended, and this is
      the earliest evidence of it there is -- so the window's own reset is five
      hours after that sample's own timestamp. That value is only trusted for a
      window that goes on to record any utilization above zero somewhere before
      its own projected reset time (real data: a window that gets used before it
      naturally expires does expire on schedule, five hours after its own start,
      even across stretches read as zero before that use arrives). A window that
      stays at zero for as long as it is observed is exactly the case the second
      piece of evidence warns about -- idle, it can sit with no window open at
      all and pick a fresh one up on whatever next touches it, arbitrarily later
      than five hours out, and a flat reading cannot tell a single skipped reset
      from several. So the projected reset time is written to every sample of
      the window (from the drop up to the next drop, or the sample reaching that
      projected time, or the samples running out) only once the window has shown
      it is more than that -- otherwise the whole span is left unresolved. This
      anchor is measured, not assumed (validated against three real,
      reset-bearing logs -- dave and jwork's post-16-Sep meter logs, and
      masterrig's moonlighter log -- with their own recorded resets stripped
      and compared back): it is accurate to within about one sample gap in the
      overwhelming majority of windows, including every one for a continuously
      active account, where the new window opens close enough to the old one's
      end that the drop itself is already good evidence. It misses low by one
      sample gap on the rarer window that sits at zero for a poll or two after
      the drop before real use resumes -- anchoring on the first such sample
      that shows use instead of the one right after the drop looks like the
      fix, but breaks the dominant continuously-active case far worse (multi-
      hour errors, tested against the same three logs), so it is not made.

    Samples that already carry a `resets_at` are returned unchanged and are
    never used to seed inference (only observed utilization is), and no sample
    before the first anchoring drop can be resolved -- there is nothing yet to
    pin it to. Returns a new list; the input is not mutated.
    """
    out = sorted(samples, key=lambda s: s.ts)
    n = len(out)
    i = 1
    while i < n:
        prev, cur = out[i - 1], out[i]
        if cur.resets_at is None and cur.five_hour < prev.five_hour:
            # A drop; find where the pre-drop plateau actually began (usually just
            # `prev`, but a reading pinned flat -- typically at the 100% ceiling --
            # could have already been sitting on the far side of the true reset).
            p = i - 1
            while p > 0 and out[p - 1].five_hour == prev.five_hour:
                p -= 1
            plateau_start = out[p]
            if cur.ts - plateau_start.ts > _FIVE_HOURS:
                i += 1
                continue
            # Unambiguous reset somewhere in (plateau_start.ts, cur.ts]; cur is the
            # new window's first reading, so its own reset is five hours after cur's
            # timestamp -- but only trustworthy if the window goes on to show real use.
            reset_at = cur.ts + _FIVE_HOURS
            iso = reset_at.isoformat()
            k = i
            used = False
            while k < n:
                if k > i and (out[k].ts - out[k - 1].ts > _FIVE_HOURS
                              or out[k].five_hour < out[k - 1].five_hour):
                    break
                if out[k].ts >= reset_at:
                    break
                if out[k].five_hour > 0:
                    used = True
                k += 1
            if used:
                for j in range(i, k):
                    if out[j].resets_at is None:
                        out[j] = replace(out[j], resets_at=iso)
            i = k if k > i else i + 1
            continue
        i += 1
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
