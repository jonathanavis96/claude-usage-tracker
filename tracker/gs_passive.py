"""Passive measurement per gs account: each account's own transcripts against its own meter.

Why per account, and why gs (issue #39). The passive join on masterrig reads
Jonathan's own account, whose meter also counts the web, the phone and other
machines, so its day-to-day rate is noise. The two probe accounts, Jono Work
(`jwork`) and Dave, are used almost only through gs, so gs should hold the
transcripts for nearly every token their meters count. Each account is joined
separately -- its own transcripts, its own meter log -- because one meter
paired with another account's tokens, or two meters averaged in one log,
measures nothing. tracker/capture.py then withholds every stretch the
transcripts cannot account for; that check, not this join, is what makes a
passive reading publishable.

`gs_accounts` is the one place that says where each account's transcripts and
meter live. Assumptions it depends on, as of 2026-09-15:

- jwork's meter is tracker.meter_log's own reset-bearing log,
  claude-usage-meter-jwork.log (deploy/systemd, not yet installed on
  2026-09-16). Until its first reading, jwork is read from
  gs-usage-ceiling.log, written by greenscape-org's usage-ceiling.py. That log
  read the default `~/.claude` -- a different account -- until the seat.conf
  drop-in of 2026-09-05 (its first jwork reading is 06:14:32Z, the meter
  falling from 100%/31% to 2%/18%), so everything before JWORK_CEILING_SINCE
  is dropped. It records no reset times; resets are inferred from the meter
  falling, which misses a reset when the new window has already passed the old
  reading by the next read (audit finding 10). Stretches built on it are
  `reset_verified: false`, and the publisher keeps them out of the measured
  series.
- jwork's `projects/` is a symlink to `~/.claude/projects`, which
  `~/.claude` and `~/.claude-jono` also write to. Neither held credentials on
  2026-09-15, but the takeoff pipeline ran 95 sessions under `~/.claude-jono`
  on the night of 2026-09-04/05 on another account, and those transcripts
  sit in jwork's directory. The report names every config dir sharing an
  account's transcripts (`shared_with`); what they add shows up in the check
  as surplus.
- Dave's meter is tracker.meter_log's own log, sampled from 2026-09-15; there
  is no Dave meter history before that.

`masterrig_account` adds Jonathan's own account beside them (issue #52). It is
deliberately not part of `gs_accounts`: it is not a clean instrument, because its
meter counts the account everywhere and only this host's transcripts are read
(MASTERRIG_METER_NOTE). It is joined here all the same because it is the
tracker's longest meter record -- back to 2026-06-13 against gs's 2026-09-05 --
and the per-stretch, per-model token detail was previously thrown away:
tracker/passive.py keeps one tokens-per-percent number per day. Both paths run;
neither replaces the other.

    python3 -m tracker.gs_passive --masterrig --out history/masterrig-passive.json

Units. A stretch is valued exactly as the publisher valued a probe row
(tracker/join.py bundle_meter_usd), and `passive_dollar_readings` returns
(time, meter dollars per full window) pairs, the element shape of
tracker/publish.py's `dollar_readings`, revalued at whatever prices the caller
publishes with. Since 2026-09-16 this is the published series on its own: the
probe (retired, see bin/daily.sh) read the same meter on a different scale --
its payload was 98% cache_write by list value where real sessions are 59%
cache_read, 24% cache_write and 17% output -- and the two never joined
cleanly. The probe rows stay in history/probes.jsonl and `calibrate` stays
here for the record; nothing publishes through them.

The capture-completeness check (tracker/capture.py) still runs and its verdict
is recorded per stretch as `capture_status`, but it no longer decides what is
published: CAPTURE_GATE is False. With its 15% tolerance on a quantity whose
stretch-to-stretch spread is 15-20%, it withheld half of jwork's real
stretches (49 of 99), and the half it withheld read low, so the published
median was the median of the expensive half. Every priced stretch counts now
(bar one the transcripts leave empty, see `publishable`), pooled per UTC day, and step detection smooths over a rolling week
(tracker/detect.py detect_smoothed_changes) instead of gating stretch by stretch.

    python3 -m tracker.gs_passive --out history/passive-gs.json
    python3 -m tracker.gs_passive --account jwork --until 2026-09-13T00:00:00+00:00
    python3 -m tracker.gs_passive --account jwork --withhold 'jwork:-home-jonathan-code-takeoff*'

`--withhold ACCOUNT:GLOB` leaves out transcripts whose path under `projects/`
matches, to watch the check catch deliberately missing capture on real data.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from itertools import pairwise
from pathlib import Path
from statistics import mean, median, stdev

from .capture import ACCEPTED, COLLECTION_GAP, UNJUDGED, UNPRICED, Verdict, check
from .join import Stretch, build_stretches, bundle_meter_usd, window_points
from .rows import usable_rows
from .samples import Sample, merge_samples, parse_log
from .turns import iter_turns, transcript_paths, transcript_session_id

JWORK_CEILING_SINCE = datetime(2026, 9, 5, 6, 14, 32, tzinfo=timezone.utc)
#: When True, only stretches the capture check accepts are published (the
#: 2026-09-15 design); when False, every priced stretch is, and the check's
#: verdict is advisory (`capture_status`). Off since 2026-09-16 -- see the
#: module docstring. The check itself is kept, not deleted. The 2026-09-16
#: audit (finding 10) asked for incomplete capture to be exposed and the rate
#: qualified, and warned against reviving a narrow band around the expected
#: rate: the band selects observations toward the answer. So the gate stays off
#: and the publisher labels every passive rate conditional on capture instead.
CAPTURE_GATE = False
#: A stretch whose transcripts hold no tokens at all: the meter moved and this host saw
#: nothing of it. Recorded as its own status rather than left as the capture check's
#: `accepted`, so nothing downstream reads `status == accepted` and then values $0 of
#: work against real meter movement. See `publishable`.
NO_TOKENS = "no-tokens"


@dataclass(frozen=True)
class MeterLog:
    """One more log of the same account's meter, in any tracker/samples.py format."""
    path: Path
    format: str


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path
    meter_log: Path
    #: A tracker/samples.py format name: "meter" (tracker.meter_log), "gs-ceiling"
    #: (usage-ceiling.py), "moonlighter" or "ceiling" (masterrig's two).
    meter_format: str
    meter_since: datetime | None = None
    #: A reset-less log this account was read from before `meter_log` existed. Its
    #: readings are used only up to the first reading of `meter_log`, and every
    #: stretch built on them is `reset_verified: false` (audit finding 10).
    legacy_meter_log: Path | None = None
    #: Further logs of this same meter, merged with `meter_log` a minute at a time
    #: (tracker/samples.py merge_samples, reset-bearing sources winning). masterrig's
    #: meter is two logs: moonlighter's JSONL and its ceiling's systemd log.
    extra_meter_logs: tuple[MeterLog, ...] = ()
    #: What a reader of this account's stretches has to know about its meter. Recorded
    #: in the report's `meter` meta, not used in any arithmetic.
    meter_note: str | None = None


def gs_accounts(home: Path | None = None) -> dict[str, Account]:
    """Where each gs account's transcripts and meter live. The only copy of this mapping."""
    home = Path(home) if home is not None else Path.home()
    ops = home / ".paperclip" / "ops"
    return {
        "jwork": Account("jwork", home / ".claude-javiswork", ops / "claude-usage-meter-jwork.log", "meter",
                         JWORK_CEILING_SINCE, legacy_meter_log=ops / "gs-usage-ceiling.log"),
        "dave": Account("dave", home / ".claude-dave", ops / "claude-usage-meter-dave.log", "meter"),
    }


#: Why masterrig's stretches are kept but never published as a rate. Its meter counts
#: the account everywhere -- claude.ai in a browser, the phone app, any other machine --
#: while only this host's ~/.claude/projects is read, so a stretch's percent includes
#: movement its tokens cannot explain. That is the phantom the capture check measures
#: (`capture`, `capture_status`), and it is why the same join on this account spreads
#: fifteen-fold day to day where gs's spreads by a fifth. The stretches are written all
#: the same: they are the longest per-model token record the tracker has, and a reader
#: who wants only the clean ones has `capture` to filter on.
MASTERRIG_METER_NOTE = (
    "This meter counts the whole account -- web, phone and every other machine -- while "
    "only this host's transcripts are read, so every stretch may carry usage the tokens "
    "cannot see. Read `capture` and `capture_status` per stretch before using a rate; the "
    "capture check is advisory here (CAPTURE_GATE is False) and never withholds a stretch "
    "from this file."
)


def masterrig_account(home: Path | None = None) -> Account:
    """Jonathan's own account on masterrig: two logs of one meter, and ~/.claude's transcripts.

    The pair is what tracker/passive.py has always merged by hand -- moonlighter's
    usage_log.jsonl (reset-bearing) and the ceiling's systemd log (not) -- and it is
    the tracker's longest meter record, back to 2026-06-13. Unlike the gs accounts
    this one is not a clean instrument; see MASTERRIG_METER_NOTE.
    """
    home = Path(home) if home is not None else Path.home()
    ceiling = home / ".paperclip" / "ops" / "mis-usage-ceiling-systemd.log"
    return Account("masterrig", home / ".claude", home / ".moonlighter" / "usage_log.jsonl", "moonlighter",
                   extra_meter_logs=(MeterLog(ceiling, "ceiling"),), meter_note=MASTERRIG_METER_NOTE)


def _read(path: Path, fmt: str, since: datetime | None) -> list[Sample]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return parse_log(fmt, fh, since)


def load_samples(account: Account, until: datetime | None = None) -> list[Sample]:
    """Every reading of this account's meter, in time order.

    `meter_log` and each of `extra_meter_logs` read the same meter, so they are
    merged a minute at a time rather than concatenated: two sources agreeing on
    one minute would otherwise pair a reading with itself and add a spurious
    zero-movement pair. `legacy_meter_log` is different -- a log the account was
    read from *before* `meter_log` existed -- so it is only used up to
    `meter_log`'s first reading.
    """
    samples = _read(account.meter_log, account.meter_format, account.meter_since)
    if account.extra_meter_logs:
        samples = merge_samples(samples, *(_read(m.path, m.format, account.meter_since)
                                           for m in account.extra_meter_logs))
    if account.legacy_meter_log is not None and account.legacy_meter_log.exists():
        first = min((s.ts for s in samples), default=None)
        legacy = [s for s in _read(account.legacy_meter_log, "gs-ceiling", account.meter_since)
                  if first is None or s.ts < first]
        samples = sorted(legacy + samples, key=lambda s: s.ts)
    return [s for s in samples if until is None or s.ts <= until]


def _session_ids(config_dir: Path) -> set[str]:
    """The sessions Claude Code has recorded under this config dir, by `session-env` entry."""
    session_env = config_dir / "session-env"
    if not session_env.is_dir():
        return set()
    return {p.name for p in session_env.iterdir() if p.is_dir()}


def transcript_files(account: Account, since: datetime | None, withhold: Iterable[str] = (),
                     home: Path | None = None) -> tuple[list[Path], dict | None]:
    """This account's transcripts, and how the pooled-projects filter split them, if it applied.

    jwork's `projects/` is a symlink shared with the bare `~/.claude`,
    `~/.claude-jono` and `~/.claude-avis` config dirs (see the module
    docstring): a raw glob over it would count those other logins' tokens as
    jwork's own -- `.claude-avis` alone holds 430 files and 3.09 billion tokens
    of another account in jwork's meter window. Claude Code writes a
    per-config-dir `<config dir>/session-env/<sessionId>/` for every session it
    runs under that login, and a transcript's session id is its filename stem, or
    its parent session's for a sub-agent file (tracker/turns.py
    transcript_session_id) -- the same rule contrib/sample.py's `own_session_filter` applies for
    the contributed export.

    The filter fires whenever the transcripts root is *shared* -- another config
    dir under `home` resolves to the same directory -- or is reached through a
    symlink at all, and the account has a `session-env` directory to filter by.
    Sharing is the condition that matters; the symlink test is kept beside it
    because a pooled root can be reached without `projects/` itself being the
    link (`~/.claude-javiswork` could be the link instead), and because a pooled
    root whose other config dirs have since been removed is still not this
    account's own. With no `session-env` the filter is a no-op even for a shared
    root, since there is nothing to tell sessions apart with.

    The second return value is `None` when the filter did not apply, and
    otherwise counts what it did, for the account's `transcripts` meta:
    `dropped_to` names the config dir that claims each dropped transcript, and
    `unclaimed` counts the dropped transcripts *no* config dir under `home`
    claims. That last number is the blind spot, and it is reported rather than
    silently folded into `dropped`: a session that never wrote a `session-env`
    entry -- a one-shot `claude -p` that starts no shell, which is what the
    tracker's own retired probe, the airlock bench and the filing judge all are
    -- cannot be attributed to any login, so it is dropped even when it was this
    account's own spend. On 2026-09-23 that was 1,407 files, 1,518 turns and
    39.7M tokens in jwork's meter window, against 1,782 files and 8,316M tokens
    kept: 0.48%, and adding every one of them lifts no unaccounted stretch into
    the accepted band. docs/findings-2026-09-23-unaccounted.md has the working.
    """
    root = account.config_dir / "projects"
    if not root.exists():
        return [], None
    patterns = list(withhold)
    paths = [p for p in transcript_paths(root, since)
            if not any(fnmatch(str(p.relative_to(root)), pat) for pat in patterns)]
    home = Path(home) if home is not None else account.config_dir.parent
    others = shared_with(account, home)
    if not others and not root.is_symlink():
        return paths, None
    if not (account.config_dir / "session-env").is_dir():
        return paths, None
    own_ids = _session_ids(account.config_dir)
    kept = [p for p in paths if transcript_session_id(p) in own_ids]
    claims = {name: _session_ids(home / name) for name in others}
    dropped_to: dict[str, int] = {}
    unclaimed = 0
    for p in paths:
        sid = transcript_session_id(p)
        if sid in own_ids:
            continue
        claimed = [name for name, ids in claims.items() if sid in ids]
        if claimed:
            dropped_to[claimed[0]] = dropped_to.get(claimed[0], 0) + 1
        else:
            unclaimed += 1
    return kept, {"kept": len(kept), "dropped": len(paths) - len(kept),
                  "subagent_files": sum(1 for p in kept if p.parent.name == "subagents"),
                  "dropped_to": dict(sorted(dropped_to.items())), "unclaimed": unclaimed}


def shared_with(account: Account, home: Path) -> list[str]:
    """Other config dirs under `home` whose `projects/` is this account's directory."""
    root = account.config_dir / "projects"
    if not root.exists():
        return []
    mine = root.resolve()
    out = []
    for d in sorted(Path(home).glob(".claude*")):
        if d == account.config_dir or not d.is_dir():
            continue
        p = d / "projects"
        if p.exists() and p.resolve() == mine:
            out.append(d.name)
    return out


def probe_readings(rows: list[dict], prices: dict) -> list[tuple[datetime, float]]:
    """(time, meter dollars per 1%) for usable prose probe rows, the evidence tracker/capture.py weighs steps against."""
    out = []
    for r in usable_rows(rows):
        ticks = r.get("tick_to", 0) - r.get("tick_from", 0)
        usd = bundle_meter_usd(r["model"], r["tokens"], prices) if ticks > 0 else None
        if usd is not None:
            out.append((datetime.fromisoformat(r["ts"]), usd / ticks))
    return sorted(out)


def publishable(v: Verdict) -> bool:
    """Whether a stretch is published (see CAPTURE_GATE).

    With the gate off every priced stretch is, except one the transcripts leave
    all but empty: capture under COLLECTION_GAP (a tenth of the account's own
    reference). That is not the 15% band -- it is the meter moving with nothing
    on gs to explain it, which is off-gs use of the account. Real case: jwork's
    meter rose 67% between 21:47 and 00:51 on 2026-09-13/14 with zero transcript
    turns; published, those seven stretches read $0.00 per 1% and dragged the day
    to nothing.

    A stretch with any unpriced token is never published, whatever its share of
    the raw tokens (audit finding 9): a small share of unpriced output can be most
    of the meter dollars next to a large free cache-read bundle, so no raw-token
    tolerance bounds the error.

    Whether the stretch's meter log carried reset ids is not decided here: it is
    recorded as `reset_verified` and the publisher treats reset-less stretches as
    legacy, conditional evidence.
    """
    if v.status == UNPRICED or v.stretch.unpriced_tokens > 0:
        return False
    if not any(sum(by_class.values()) for by_class in v.stretch.tokens.values()):
        # No tokens at all is the extreme of the collection gap above, and it slips
        # past that rule whenever the stretch has no reference to divide by (capture
        # None: unjudged, or an account whose own bootstrap median is zero). masterrig
        # has 28 such stretches in 237; on gs they already read capture 0.0 and were
        # withheld, so this changes nothing there.
        return False
    if CAPTURE_GATE:
        return v.status == ACCEPTED
    return v.capture is None or v.capture >= COLLECTION_GAP


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None or x == float("inf") else round(x, n)


def _stretch_record(v: Verdict) -> dict:
    s = v.stretch
    lo, hi = s.bounds
    # Unpriced work is kept by raw model id beside the priced models, so the record
    # shows what the meter was charged for even when no price covers it.
    tokens = {**s.tokens, **s.unpriced}
    if publishable(v):
        status = ACCEPTED
    elif s.unpriced_tokens:
        status = UNPRICED
    elif v.status == ACCEPTED:
        # The capture check accepted it and `publishable` did not: the one case is a
        # stretch with no tokens at all, which has no reference to be judged against
        # (see `publishable`). Naming it keeps `accepted` meaning publishable, without
        # relabelling a stretch the check itself withheld.
        status = NO_TOKENS
    else:
        status = v.status
    return {"start": s.start.isoformat(), "end": s.end.isoformat(), "delta_pct": s.delta_pct, "windows": s.windows,
            "usd": _r(s.usd), "usd_per_pct": _r(s.usd_per_pct), "bounds": [_r(lo), _r(hi)], "tokens": tokens,
            "unpriced_tokens": s.unpriced_tokens, "unpriced": s.unpriced, "turns": s.turns,
            "reset_verified": s.reset_verified, "status": status, "capture_status": v.status,
            "reference": _r(v.reference), "capture": _r(v.capture)}


def _pieces(stretches: list[Stretch]) -> int:
    """Separate window pieces under a pooled set of stretches.

    Consecutive stretches that meet at one reading telescope: the reading
    between them cancels, so they share a piece and its one point of rounding.
    """
    joins = sum(1 for a, b in pairwise(stretches) if b.start == a.end)
    return sum(s.windows for s in stretches) - joins


def _daily(verdicts: list[Verdict]) -> list[dict]:
    """Published stretches pooled per UTC day, with their rounding bounds and what qualifies them.

    `bounds` concedes one point of rounding per separate window piece, as a
    stretch's own bounds do; `reset_verified` is true only when every stretch of
    the day came from a log with reset ids; `capture_diagnosed` counts the day's
    stretches the capture check did not accept (published with CAPTURE_GATE off).
    """
    days: dict[str, list[Verdict]] = {}
    for v in verdicts:
        if publishable(v):
            days.setdefault(v.stretch.end.astimezone(timezone.utc).date().isoformat(), []).append(v)
    out = []
    for day, vs in sorted(days.items()):
        ss = [v.stretch for v in vs]
        delta = sum(s.delta_pct for s in ss)
        pieces = _pieces(ss)
        usd = sum(s.usd for s in ss)
        lo = usd / (delta + pieces)
        hi = usd / (delta - pieces) if delta > pieces else None
        out.append({"date": day, "usd_per_pct": _r(usd / delta), "delta_pct": delta,
                    "stretches": len(ss), "rounding": _r(pieces / delta),
                    "bounds": [_r(lo), _r(hi)], "pieces": pieces,
                    "reset_verified": all(s.reset_verified for s in ss),
                    "capture_diagnosed": sum(1 for v in vs if v.status != ACCEPTED)})
    return out


def spread(values: list[float]) -> dict:
    """How tightly a series of readings agrees: coefficient of variation, half-range and MAD, each relative."""
    if not values:
        return {"n": 0}
    m = median(values)
    if not m:
        # Every relative measure below divides by the median. A zero median is a set of
        # readings that spent nothing, which has a count and nothing else to say.
        return {"n": len(values), "median": 0.0, "half_range": None, "mad": None, "cv": None}
    out = {"n": len(values), "median": _r(m), "half_range": _r((max(values) - min(values)) / 2 / m),
           "mad": _r(1.4826 * median(abs(v - m) for v in values) / m)}
    out["cv"] = _r(stdev(values) / mean(values)) if len(values) > 1 else None
    return out


def _state(verdicts: list[Verdict], account_runs: list) -> dict:
    judged = [v for v in verdicts if v.status not in (UNJUDGED, UNPRICED)]
    if not verdicts:
        return {"state": "no data"}
    if not CAPTURE_GATE:
        return {"state": "ok"} if any(publishable(v) for v in verdicts) else {"state": "nothing publishable"}
    if not judged:
        return {"state": "unjudged"}
    if judged[-1].status == ACCEPTED:
        return {"state": "ok"}
    run = account_runs[-1]
    return {"state": "withholding", "since": run.start.isoformat(), "kind": run.kind}


def _run_record(run) -> dict:
    return {"start": run.start.isoformat(), "end": run.end.isoformat(), "kind": run.kind, "direction": run.direction,
            "stretches": len(run.verdicts), "step": _r(run.step), "capture": _r(run.capture),
            "corroborated_by": run.corroborated_by, "contradicted_by": run.contradicted_by}


def _split(stretches: list[Stretch]) -> dict:
    """Token-class shares over a set of stretches, what a full window buys divided into classes."""
    tot = {c: 0 for c in ("input", "output", "cache_read", "cache_write")}
    for s in stretches:
        for by_class in s.tokens.values():
            for c in tot:
                tot[c] += by_class.get(c, 0)
    grand = sum(tot.values())
    if not grand:
        return {}
    out = {c: round(n / grand, 6) for c, n in tot.items()}
    one_hour = sum(by_class.get("cache_write_1h", 0)
                   for s in stretches for by_class in s.tokens.values())
    if one_hour:
        out["cache_write_1h"] = round(one_hour / grand, 6)
    return out


def report(accounts: dict[str, Account], prices: dict, probe_rows: Iterable[dict] = (), now: datetime | None = None,
           withhold: dict[str, list[str]] | None = None, until: datetime | None = None,
           home: Path | None = None) -> dict:
    """Join, judge and summarise every account; the JSON tracker.gs_passive writes."""
    now = now or datetime.now(timezone.utc)
    withhold = withhold or {}
    stretches, meta, weekly = {}, {}, {}
    for name, account in accounts.items():
        samples = load_samples(account, until)
        since = samples[0].ts if samples else None
        files, own_sessions = transcript_files(account, since, withhold.get(name, ())) if samples else ([], None)
        turns = [t for t in iter_turns(files) if until is None or t.ts <= until]
        stretches[name] = build_stretches(samples, turns, prices)
        weekly[name] = window_points(samples)
        root = account.config_dir / "projects"
        meta[name] = {
            "meter": {"log": str(account.meter_log), "format": account.meter_format,
                      "legacy_log": str(account.legacy_meter_log) if account.legacy_meter_log else None,
                      "extra_logs": [{"log": str(m.path), "format": m.format} for m in account.extra_meter_logs],
                      "note": account.meter_note,
                      "reset_verified_samples": sum(1 for s in samples if s.resets_at is not None),
                      "since": account.meter_since.isoformat() if account.meter_since else None,
                      "samples": len(samples), "first": since.isoformat() if since else None,
                      "last": samples[-1].ts.isoformat() if samples else None},
            "transcripts": {"root": str(root), "resolves_to": str(root.resolve()),
                            "shared_with": shared_with(account, home or account.config_dir.parent),
                            "files": len(files), "turns": len(turns), "withheld_patterns": list(withhold.get(name, ())),
                            "own_sessions": own_sessions},
        }
    checked = check(stretches, probe_readings(list(probe_rows), prices))
    out_accounts = {}
    for name in accounts:
        vs, rs = checked.verdicts[name], checked.runs[name]
        daily = _daily(vs)
        accepted = [v for v in vs if publishable(v)]
        out_accounts[name] = {
            "account": name, **meta[name],
            "stretches": [_stretch_record(v) for v in vs],
            "runs": [_run_record(r) for r in rs],
            "daily": daily,
            "split": _split([v.stretch for v in accepted]),
            "last_usable_at": accepted[-1].stretch.end.isoformat() if accepted else None,
            "state": _state(vs, rs),
            "spread": {"stretch": spread([v.stretch.usd_per_pct for v in accepted]),
                       "daily": spread([d["usd_per_pct"] for d in daily])},
            "weekly_by_window": weekly[name],
        }
    return {"generated_at": now.isoformat(), "until": until.isoformat() if until else None,
            "capture_gate": CAPTURE_GATE, "model_normalization": {"version": 1,
             "aliases": {"claude-fable-5": "claude-fable-5-1"},
             "rule": "strip date suffix and [1m]; retain every other raw model id"},
            "accounts": out_accounts, "changes": checked.changes}


def passive_dollar_readings(report: dict, prices: dict, by: str = "day",
                            allow_legacy_unverified: bool = False) -> list[tuple[datetime, float]]:
    """Accepted passive readings as (time, meter dollars per full window), revalued at `prices`.

    The publisher's hook: the same element shape as build_public_json's
    `dollar_readings` (usd_per_pct x 100 per probe row). Only stretches whose
    `status` is accepted are read: every priced one with CAPTURE_GATE off,
    only what the capture check accepted with it on. A stretch whose meter log
    carried no reset ids (`reset_verified` false, or absent in a report from
    before the field existed) is read only with `allow_legacy_unverified`: it can
    miss a reset between two readings (audit finding 10), so the publisher keeps
    it out of the measured series.
    `by="day"` pools each account's accepted stretches per UTC day (the
    precision the issue is after); `by="stretch"` returns them one by one.
    """
    out = []
    for account in report.get("accounts", {}).values():
        groups: dict[str, list[dict]] = {}
        for s in account["stretches"]:
            if s["status"] != ACCEPTED:
                continue
            # Archived ceiling-log stretches lack reset ids.  They remain
            # available as explicitly conditional references, but never enter
            # the certified series or its change detector.
            if s.get("reset_verified") is not True and not allow_legacy_unverified:
                continue
            if s.get("unpriced_tokens", 0) > 0 or not s.get("tokens"):
                # No captured work at all is a collection gap, not a free window.
                continue
            # A stretch with a model these prices do not cover is left out whole:
            # valuing that model at $0 would deflate the reading and look like a
            # cheaper meter, and the stretch's percent is spent on it all the same.
            valued = {m: bundle_meter_usd(m, tok, prices) for m, tok in s["tokens"].items()}
            if any(v is None for v in valued.values()):
                continue
            key = s["end"][:10] if by == "day" else s["end"]
            groups.setdefault(key, []).append((s, sum(valued.values())))
        for ss in groups.values():
            usd = sum(v for _, v in ss)
            out.append((datetime.fromisoformat(ss[-1][0]["end"]), usd / sum(s["delta_pct"] for s, _ in ss) * 100))
    return sorted(out)


def calibrate(rpt: dict, probe_rows: list[dict], prices: dict, since: datetime, until: datetime) -> dict:
    """The passive/probe dollar-per-window ratio over [since, until] (issue #39 follow-up).

    Jono Work's accepted passive days and the probe read the same meter on
    different scales -- $1.26-$1.81 per window passive against $0.97 probed,
    on the real 2026-09-05..15 gs data -- so joining them raw invents change
    events the live page does not have. `ratio` = median(accepted passive
    daily readings in the window) / median(usable probe rows' dollars per
    window in the same window); tracker/publish.py divides a passive reading
    by this ratio before joining it to the probe series. `None` when either
    side has no readings in the window, so the caller can refuse to publish
    a ratio computed from nothing.

    Probe dollars-per-window is restated via `bundle_meter_usd` rather than
    importing tracker/publish.py's `usd_per_pct` (same formula: meter dollars
    per tick times 100, see bundle_meter_usd's own docstring) -- publish.py
    imports this module for `passive_dollar_readings`, so importing back
    would be circular.
    """
    passive_vals = [v for t, v in passive_dollar_readings(rpt, prices, by="day") if since <= t <= until]
    probe_vals = []
    for r in usable_rows(probe_rows):
        ts = datetime.fromisoformat(r["ts"])
        if not (since <= ts <= until):
            continue
        ticks = r.get("tick_to", 0) - r.get("tick_from", 0)
        usd = bundle_meter_usd(r["model"], r["tokens"], prices) if ticks > 0 else None
        if usd is not None:
            probe_vals.append(usd / ticks * 100)
    ratio = median(passive_vals) / median(probe_vals) if passive_vals and probe_vals else None
    return {"ratio": _r(ratio, 6), "window": [since.date().isoformat(), until.date().isoformat()],
            "passive_n": len(passive_vals), "probe_n": len(probe_vals)}


def fit_output_weight(rpt: dict, prices: dict) -> dict:
    """Least-squares fit of class_weight.output from the passive stretches themselves.

    Each published stretch says: meter movement (delta_pct) = a x (list dollars of
    input + cache_write) + b x (list dollars of output), with cache_read at its
    measured weight of 0.0. The output weight relative to cache_write is b / a.
    Two unknowns, so the 2x2 normal equations are solved directly; no numpy.
    The 1.8 hand-entered on 2026-09-06 came from one probe pair; this reads it
    from every stretch the capture check accepted instead. `--fit-output-weight` prints it; the
    value is pasted into data/prices.json by hand, as the calibration ratio
    was meant to be, so it stays fixed while real movement in the series shows.
    """
    rows = []
    for account in rpt.get("accounts", {}).values():
        for st in account["stretches"]:
            # The capture check's own accepted set, not everything published: a
            # stretch with part of its use off gs has the wrong left-hand side.
            if st.get("capture_status", st["status"]) != ACCEPTED:
                continue
            base = out = 0.0
            for m, tok in st["tokens"].items():
                price = prices.get(m)
                if price is None:
                    break
                base += (tok.get("input", 0) * price["input"] + tok.get("cache_write", 0) * price["cache_write"]) / 1e6
                out += tok.get("output", 0) * price["output"] / 1e6
            else:
                rows.append((base, out, st["delta_pct"]))
    if len(rows) < 2:
        return {"n": len(rows), "output_weight": None}
    sxx = sum(b * b for b, _, _ in rows)
    sxy = sum(b * o for b, o, _ in rows)
    syy = sum(o * o for _, o, _ in rows)
    sxz = sum(b * d for b, _, d in rows)
    syz = sum(o * d for _, o, d in rows)
    det = sxx * syy - sxy * sxy
    if not det:
        return {"n": len(rows), "output_weight": None}
    a = (sxz * syy - syz * sxy) / det
    b = (sxx * syz - sxy * sxz) / det
    fitted = [a * base + b * out for base, out, _ in rows]
    resid = [d - f for (_, _, d), f in zip(rows, fitted)]
    rms = (sum(r * r for r in resid) / len(rows)) ** 0.5
    return {"n": len(rows), "output_weight": _r(b / a) if a else None,
            "usd_per_pct_at_fit": _r(1 / a) if a else None, "residual_rms_pct": _r(rms, 2),
            "output_share_of_list_value": _r(sum(o for _, o, _ in rows) / sum(b + o for b, o, _ in rows))}


def _utc_arg(value: str, *, end_of_day: bool = False) -> datetime:
    """An ISO date or date-time from the command line as an aware UTC datetime.

    Meter samples and transcript turns are aware, so a naive argument would raise
    on comparison. A bare date means the whole day: its start for --since, its
    last second for --until, so `--since 2026-09-05 --until 2026-09-15` spans
    both days inclusive."""
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if end_of_day and len(value) == 10:
        t = t.replace(hour=23, minute=59, second=59)
    return t


def _utc_until(value: str) -> datetime:
    return _utc_arg(value, end_of_day=True)


def _summary(name: str, a: dict) -> str:
    statuses: dict[str, int] = {}
    for s in a["stretches"]:
        statuses[s["status"]] = statuses.get(s["status"], 0) + 1
    sp = a["spread"]["daily"]
    return (f"{name}: {a['meter']['samples']} meter samples, {a['transcripts']['turns']} turns, "
            f"stretches {statuses or 0}, runs {[r['kind'] for r in a['runs']]}, state {a['state']['state']}, "
            f"daily median ${sp.get('median')}/1% cv {sp.get('cv')} (n={sp['n']})")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Passive join per account; the capture check is advisory (CAPTURE_GATE)")
    ap.add_argument("--home", type=Path, default=Path.home())
    ap.add_argument("--masterrig", action="store_true",
                    help="join masterrig's own account (moonlighter + ceiling logs against ~/.claude/projects) "
                         "instead of the gs accounts; its meter also counts web, phone and other machines, so read "
                         "each stretch's capture before using a rate")
    ap.add_argument("--account", action="append", help="limit to these accounts (default: every gs account)")
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--probes", type=Path, default=Path("history/probes.jsonl"))
    ap.add_argument("--until", type=_utc_until, help="replay as of this time (a bare date means its last second)")
    ap.add_argument("--withhold", action="append", default=[], metavar="ACCOUNT:GLOB",
                    help="leave out that account's transcripts matching GLOB under projects/")
    ap.add_argument("--calibrate", action="store_true",
                    help="print the passive/probe dollar-per-window ratio for --since..--until as JSON "
                         "and exit; never writes --out or prices.json (Jonathan pastes the ratio in by hand)")
    ap.add_argument("--since", type=_utc_arg, help="calibration window start (with --calibrate); a bare date means its first second")
    ap.add_argument("--fit-output-weight", action="store_true",
                    help="print the least-squares class_weight.output over the published stretches as JSON and exit")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    if a.calibrate and (a.since is None or a.until is None):
        ap.error("--calibrate requires --since and --until")
    accounts = {"masterrig": masterrig_account(a.home)} if a.masterrig else gs_accounts(a.home)
    if a.account:
        unknown = set(a.account) - set(accounts)
        if unknown:
            ap.error(f"unknown account(s): {', '.join(sorted(unknown))}")
        accounts = {k: v for k, v in accounts.items() if k in a.account}
    withhold: dict[str, list[str]] = {}
    for item in a.withhold:
        name, _, pattern = item.partition(":")
        withhold.setdefault(name, []).append(pattern)
    prices = {k: v for k, v in json.loads(a.prices.read_text()).items() if not k.startswith("_")}
    rows = ([json.loads(line) for line in a.probes.read_text(encoding="utf-8").splitlines() if line.strip()]
            if a.probes.exists() else [])
    r = report(accounts, prices, rows, withhold=withhold, until=a.until, home=a.home)
    if a.calibrate:
        print(json.dumps(calibrate(r, rows, prices, a.since, a.until)))
        return 0
    if a.fit_output_weight:
        print(json.dumps(fit_output_weight(r, prices)))
        return 0
    for name, acct in r["accounts"].items():
        print(_summary(name, acct))
    for c in r["changes"]:
        print(f"change: {c['account']} at {c['at']}, step {c['step']:+.1%}, corroborated by {', '.join(c['corroborated_by'])}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = a.out.with_suffix(".tmp")
        tmp.write_text(json.dumps(r, indent=1) + "\n", encoding="utf-8")
        tmp.replace(a.out)  # atomic: a killed run never leaves a truncated file
        print(f"wrote {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
