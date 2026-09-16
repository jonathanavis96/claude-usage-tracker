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

- jwork's meter is gs-usage-ceiling.log, written by greenscape-org's
  usage-ceiling.py. That log read the default `~/.claude` -- a different
  account -- until the seat.conf drop-in of 2026-09-05 (its first jwork
  reading is 06:14:32Z, the meter falling from 100%/31% to 2%/18%), so
  everything before JWORK_CEILING_SINCE is dropped. The log records no reset
  times; resets are inferred from the meter falling.
- jwork's `projects/` is a symlink to `~/.claude/projects`, which
  `~/.claude` and `~/.claude-jono` also write to. Neither held credentials on
  2026-09-15, but the takeoff pipeline ran 95 sessions under `~/.claude-jono`
  on the night of 2026-09-04/05 on another account, and those transcripts
  sit in jwork's directory. The report names every config dir sharing an
  account's transcripts (`shared_with`); what they add shows up in the check
  as surplus.
- Dave's meter is tracker.meter_log's own log, sampled from 2026-09-15; there
  is no Dave meter history before that.

Units. A stretch is valued exactly as the publisher values a probe row
(tracker/join.py bundle_meter_usd), and `passive_dollar_readings` returns
(time, meter dollars per full window) pairs, the element shape of
tracker/publish.py's `dollar_readings`, revalued at whatever prices the caller
publishes with. The units are the probe's; the levels are not yet comparable.
Real sessions are about 97% cache reads by tokens and the probe payload is
cache writes, and `class_weight.cache_read` in data/prices.json is unmeasured
(assumed 1.0), so passive readings sit well above the probe's on the same
meter until that weight is measured. Only steps compare across the two, which
is how tracker/capture.py uses the probe.

    python3 -m tracker.gs_passive --out history/passive-gs.json
    python3 -m tracker.gs_passive --account jwork --until 2026-09-13T00:00:00+00:00
    python3 -m tracker.gs_passive --account jwork --withhold 'jwork:-home-jonathan-code-takeoff*'

`--withhold ACCOUNT:GLOB` leaves out transcripts whose path under `projects/`
matches, to watch the check catch deliberately missing capture on real data.
"""
from __future__ import annotations
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from statistics import mean, median, stdev
from typing import Iterable
from .capture import ACCEPTED, UNJUDGED, UNPRICED, Verdict, check
from .join import Stretch, build_stretches, bundle_meter_usd, window_points
from .rows import usable_rows
from .samples import Sample, parse_gs_ceiling_log, parse_meter_log
from .turns import iter_turns, transcript_paths

JWORK_CEILING_SINCE = datetime(2026, 9, 5, 6, 14, 32, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path
    meter_log: Path
    meter_format: str  # "meter" (tracker.meter_log) or "gs-ceiling" (usage-ceiling.py)
    meter_since: datetime | None = None


def gs_accounts(home: Path | None = None) -> dict[str, Account]:
    """Where each gs account's transcripts and meter live. The only copy of this mapping."""
    home = Path(home) if home is not None else Path.home()
    ops = home / ".paperclip" / "ops"
    return {
        "jwork": Account("jwork", home / ".claude-javiswork", ops / "gs-usage-ceiling.log", "gs-ceiling",
                         JWORK_CEILING_SINCE),
        "dave": Account("dave", home / ".claude-dave", ops / "claude-usage-meter-dave.log", "meter"),
    }


def load_samples(account: Account, until: datetime | None = None) -> list[Sample]:
    if not account.meter_log.exists():
        return []
    with open(account.meter_log, encoding="utf-8") as fh:
        if account.meter_format == "gs-ceiling":
            samples = parse_gs_ceiling_log(fh, since=account.meter_since)
        else:
            samples = [s for s in parse_meter_log(fh) if account.meter_since is None or s.ts >= account.meter_since]
    return [s for s in samples if until is None or s.ts <= until]


def transcript_files(account: Account, since: datetime | None,
                     withhold: Iterable[str] = ()) -> tuple[list[Path], dict | None]:
    """This account's transcripts, and (kept, dropped) if the pooled-projects filter applied.

    jwork's `projects/` is a symlink shared with the bare `~/.claude` and
    `~/.claude-jono` config dirs (see the module docstring): a raw glob over it
    would count those other logins' tokens as jwork's own. Claude Code writes a
    per-config-dir `<config dir>/session-env/<sessionId>/` for every session it
    runs under that login, and a transcript's own session id is its filename
    stem -- the same rule contrib/sample.py's `own_session_filter` applies for
    the contributed export. Here it fires whenever `projects/` is itself a
    symlink and the account has a `session-env` directory to filter by; with
    no `session-env` the filter is a no-op even for a symlinked root, since
    there is nothing to tell sessions apart with. The second return value is
    `None` when the filter did not apply, or `{"kept": n, "dropped": m}` when
    it did, for the account's `transcripts` meta.
    """
    root = account.config_dir / "projects"
    if not root.exists():
        return [], None
    patterns = list(withhold)
    paths = [p for p in transcript_paths(root, since)
            if not any(fnmatch(str(p.relative_to(root)), pat) for pat in patterns)]
    if not root.is_symlink():
        return paths, None
    session_env = account.config_dir / "session-env"
    if not session_env.is_dir():
        return paths, None
    own_ids = {p.name for p in session_env.iterdir() if p.is_dir()}
    kept = [p for p in paths if p.stem in own_ids]
    return kept, {"kept": len(kept), "dropped": len(paths) - len(kept)}


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


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None or x == float("inf") else round(x, n)


def _stretch_record(v: Verdict) -> dict:
    s = v.stretch
    lo, hi = s.bounds
    return {"start": s.start.isoformat(), "end": s.end.isoformat(), "delta_pct": s.delta_pct, "windows": s.windows,
            "usd": _r(s.usd), "usd_per_pct": _r(s.usd_per_pct), "bounds": [_r(lo), _r(hi)], "tokens": s.tokens,
            "unpriced_tokens": s.unpriced_tokens, "turns": s.turns, "status": v.status,
            "reference": _r(v.reference), "capture": _r(v.capture)}


def _pieces(stretches: list[Stretch]) -> int:
    """Separate window pieces under a pooled set of stretches.

    Consecutive stretches that meet at one reading telescope: the reading
    between them cancels, so they share a piece and its one point of rounding.
    """
    joins = sum(1 for a, b in zip(stretches, stretches[1:]) if b.start == a.end)
    return sum(s.windows for s in stretches) - joins


def _daily(verdicts: list[Verdict]) -> list[dict]:
    days: dict[str, list[Stretch]] = {}
    for v in verdicts:
        if v.status == ACCEPTED:
            days.setdefault(v.stretch.end.astimezone(timezone.utc).date().isoformat(), []).append(v.stretch)
    out = []
    for day, ss in sorted(days.items()):
        delta = sum(s.delta_pct for s in ss)
        out.append({"date": day, "usd_per_pct": _r(sum(s.usd for s in ss) / delta), "delta_pct": delta,
                    "stretches": len(ss), "rounding": _r(_pieces(ss) / delta)})
    return out


def spread(values: list[float]) -> dict:
    """How tightly a series of readings agrees: coefficient of variation, half-range and MAD, each relative."""
    if not values:
        return {"n": 0}
    m = median(values)
    out = {"n": len(values), "median": _r(m), "half_range": _r((max(values) - min(values)) / 2 / m),
           "mad": _r(1.4826 * median(abs(v - m) for v in values) / m)}
    out["cv"] = _r(stdev(values) / mean(values)) if len(values) > 1 else None
    return out


def _state(verdicts: list[Verdict], account_runs: list) -> dict:
    judged = [v for v in verdicts if v.status not in (UNJUDGED, UNPRICED)]
    if not verdicts:
        return {"state": "no data"}
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
        accepted = [v for v in vs if v.status == ACCEPTED]
        out_accounts[name] = {
            "account": name, **meta[name],
            "stretches": [_stretch_record(v) for v in vs],
            "runs": [_run_record(r) for r in rs],
            "daily": daily,
            "last_usable_at": accepted[-1].stretch.end.isoformat() if accepted else None,
            "state": _state(vs, rs),
            "spread": {"stretch": spread([v.stretch.usd_per_pct for v in accepted]),
                       "daily": spread([d["usd_per_pct"] for d in daily])},
            "weekly_by_window": weekly[name],
        }
    return {"generated_at": now.isoformat(), "until": until.isoformat() if until else None,
            "accounts": out_accounts, "changes": checked.changes}


def passive_dollar_readings(report: dict, prices: dict, by: str = "day") -> list[tuple[datetime, float]]:
    """Accepted passive readings as (time, meter dollars per full window), revalued at `prices`.

    The publisher's hook: the same element shape as build_public_json's
    `dollar_readings` (usd_per_pct x 100 per probe row). Only accepted
    stretches are read, so nothing the capture check withheld can reach it.
    `by="day"` pools each account's accepted stretches per UTC day (the
    precision the issue is after); `by="stretch"` returns them one by one.
    """
    out = []
    for account in report.get("accounts", {}).values():
        groups: dict[str, list[dict]] = {}
        for s in account["stretches"]:
            if s["status"] != ACCEPTED:
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
    ap = argparse.ArgumentParser(description="Passive join per gs account, with the capture-completeness check")
    ap.add_argument("--home", type=Path, default=Path.home())
    ap.add_argument("--account", action="append", help="limit to these accounts (default: every gs account)")
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--probes", type=Path, default=Path("history/probes.jsonl"))
    ap.add_argument("--until", type=datetime.fromisoformat, help="replay as of this time")
    ap.add_argument("--withhold", action="append", default=[], metavar="ACCOUNT:GLOB",
                    help="leave out that account's transcripts matching GLOB under projects/")
    ap.add_argument("--calibrate", action="store_true",
                    help="print the passive/probe dollar-per-window ratio for --since..--until as JSON "
                         "and exit; never writes --out or prices.json (Jonathan pastes the ratio in by hand)")
    ap.add_argument("--since", type=datetime.fromisoformat, help="calibration window start (with --calibrate)")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    if a.calibrate and (a.since is None or a.until is None):
        ap.error("--calibrate requires --since and --until")
    accounts = gs_accounts(a.home)
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
