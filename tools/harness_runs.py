"""Collect every harness run that sent traffic on a gs account into history/harness-runs.jsonl.

The exclusion list in tools/reconcile_window.py reads history/probes.jsonl and the
effort-matrix _meta. A probe only writes a probes.jsonl row when it finishes, so a probe that
aborted (tick too early, no second tick, window reset mid-probe, account not idle) or crashed
on an HTTP error never appears there. Probe traffic leaves no transcript either, so those runs
moved the meter with nothing to attribute the movement to, and the passive stretches that
contain them look like ordinary work that cost more than it did.

This tool parses the two ops logs as well as probes.jsonl and the effort matrix, and writes one
row per run: account, start, end, kind, outcome, and the source line it came from.

    python3 -m tools.harness_runs                      # rewrite history/harness-runs.jsonl
    python3 -m tools.harness_runs --print              # to stdout, write nothing

The file it writes is the single source of the exclusion: tracker/credits.py reads it and
nothing else, which is how an aborted probe reaches the publisher's selection at all.

Accounts and times, in order of preference:

1. A completed probe carries its own account and an exact interval (ts, ts + elapsed_s) in
   probes.jsonl. A result line in the log is matched to that row by account, model,
   tokens-per-1% and proximity in time, so the run is emitted once with the exact interval.
   Time is part of the key because the other three repeat: the probe runs the same model on
   the same account night after night, and two runs reading the same rounded tokens per 1%
   would otherwise collapse into one, losing the span of the later.
2. An abort line names its account ("probe aborted on dave: tick too early").
3. A crashed run names nothing. Its prompt lines carry the five-hour meter's resets_at, which
   identifies the account's window, so the run is jwork when jwork's own meter shows a reset
   whose window end matches that resets_at, and dave otherwise. jwork's meter covers every one
   of these runs; dave's begins on 2026-09-15, which is why the test runs one way round.

Timestamps come from the log's own HH:MM:SSZ prefix where the line has one (the prefix was
added part way through the log's life); the date is the one that puts the line inside the
five-hour window its resets_at closes. Where the lines are undated, the run is bracketed by
the account's meter: it starts no earlier than the first sample holding the run's first
five-hour reading and ends no later than the first sample above its last reading. Where there
is no meter either, the bracket is the whole five-hour window, which over-excludes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.credits import EFFORT_MATRIX_ACCOUNT, HARNESS_RUNS_PATH

OPS = Path.home() / ".paperclip" / "ops"
PROBE_LOG = OPS / "claude-usage-probe.log"
OUTPUT_PROBE_LOG = OPS / "claude-usage-output-probe.log"
OUT = HARNESS_RUNS_PATH
WINDOW = timedelta(hours=5)
#: How far apart a log run's bracket and a probes.jsonl row's span may sit and still be the
#: same run (`same_run`). One five-hour window, because an undated run's bracket is at worst
#: the whole window its prompts name, and nothing closer would hold; it is tight enough for
#: its purpose, which is to stop a run being deduplicated against another day's.
MATCH_TOLERANCE = WINDOW
P = datetime.fromisoformat

TIMED = re.compile(r"^(\d{2}):(\d{2}):(\d{2})Z\s+(.*)$")
PROMPT = re.compile(r"^prompt (\d+)(?: \(burst of (\d+)\))?: five_hour=([\d.]+) resets_at=(\S+)")
RESULT = re.compile(r"^(\w+) (claude-[\w.-]+) (\w+): ([\d.]+) tokens per 1% \((\d+) prompts")
ABORT = re.compile(r"^probe aborted on (\w+): (.*)$")
LEGACY = re.compile(r"^(\S+) .*five_hour=(\d+)%")


def _strip_time(line: str) -> tuple[str | None, str]:
    m = TIMED.match(line)
    return (m.group(1) + ":" + m.group(2) + ":" + m.group(3), m.group(4)) if m else (None, line)


def _resolve(clock: str, resets_at: datetime) -> datetime | None:
    """The instant with that wall clock time inside the five-hour window closing at resets_at."""
    for day in (resets_at.date(), resets_at.date() - timedelta(days=1)):
        t = P(f"{day.isoformat()}T{clock}+00:00")
        if resets_at - WINDOW <= t <= resets_at:
            return t
    return None


def parse_log(path: Path, kind: str) -> list[dict]:
    """Split one ops log into runs: prompt lines, then whatever ended the run."""
    if not path.exists():
        return []
    runs, buf = [], []
    lines = path.read_text(errors="replace").splitlines()
    for i, raw in enumerate(lines, 1):
        clock, line = _strip_time(raw)
        m = PROMPT.match(line)
        if m:
            buf.append({"line": i, "clock": clock, "five_hour": float(m.group(3)), "resets_at": P(m.group(4))})
            continue
        m = RESULT.match(line)
        if m:
            runs.append({"kind": kind, "outcome": "completed", "account": m.group(1), "model": m.group(2),
                         "effort": m.group(3), "tokens_per_pct": float(m.group(4)), "line": i,
                         "prompts": buf})
            buf = []
            continue
        m = ABORT.match(line)
        if m:
            runs.append({"kind": kind, "outcome": "aborted", "account": m.group(1), "model": None,
                         "effort": None, "detail": m.group(2), "line": i, "prompts": buf})
            buf = []
            continue
        if line.startswith("Traceback (most recent call last)"):
            detail = next((t.strip() for t in lines[i:] if t.strip() and not t.startswith((" ", "\t"))), "")
            runs.append({"kind": kind, "outcome": "crashed", "account": None, "model": None,
                         "effort": None, "detail": detail, "line": i, "prompts": buf})
            buf = []
            continue
    if buf:
        runs.append({"kind": kind, "outcome": "unterminated", "account": None, "model": None,
                     "effort": None, "line": buf[-1]["line"], "prompts": buf})
    return [r for r in runs if r["prompts"]]


def meter_series() -> dict[str, list[tuple[datetime, float]]]:
    """Each gs account's five-hour readings, from the logs gs-passive.json names."""
    out: dict[str, list[tuple[datetime, float]]] = {}
    accounts = json.load(open("history/gs-passive.json"))["accounts"]
    for name, body in accounts.items():
        rows: list[tuple[datetime, float]] = []
        for key in ("log", "legacy_log"):
            p = body.get("meter", {}).get(key)
            if not p or not Path(p).exists():
                continue
            for line in open(p, errors="replace"):
                line = line.strip()
                if line.startswith("{"):
                    r = json.loads(line)
                    if "five_hour" in r:  # the rest are error rows, which read nothing
                        rows.append((P(r["ts"]), float(r["five_hour"]["utilization"])))
                else:
                    m = LEGACY.match(line)
                    if m:
                        rows.append((P(m.group(1)), float(m.group(2))))
        out[name] = sorted(rows)
    return out


def resets(series: list[tuple[datetime, float]]) -> list[tuple[datetime, datetime]]:
    """Window ends implied by each drop in the meter, as the bracket the sampling gap allows."""
    out = []
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if v1 < v0:
            out.append((t0 + WINDOW, t1 + WINDOW))
    return out


def attribute(run: dict, series: dict[str, list[tuple[datetime, float]]]) -> tuple[str, str]:
    """Account for a run whose log line does not name one, and how it was decided."""
    r = max(p["resets_at"] for p in run["prompts"])
    for lo, hi in resets(series.get("jwork", [])):
        if lo <= r <= hi:
            return "jwork", "meter-match"
    covered = any(r - WINDOW <= t <= r for t, _ in series.get("jwork", []))
    return "dave", "meter-match" if covered else "assumed"


def bracket(run: dict, account: str, series: dict[str, list[tuple[datetime, float]]]) -> tuple[datetime, datetime, str]:
    """Start and end of a run that has no probes.jsonl row."""
    stamped = [(_resolve(p["clock"], p["resets_at"]), p) for p in run["prompts"] if p["clock"]]
    stamped = [(t, p) for t, p in stamped if t]
    if len(stamped) == len(run["prompts"]) and stamped:
        return stamped[0][0], stamped[-1][0], "log-clock"
    r = max(p["resets_at"] for p in run["prompts"])
    lo, hi = r - WINDOW, r
    inside = [(t, v) for t, v in series.get(account, []) if lo <= t <= hi]
    if inside:
        v0, v1 = run["prompts"][0]["five_hour"], run["prompts"][-1]["five_hour"]
        start = next((t for t, v in inside if v >= v0), None)
        if start is not None:
            end = next((t for t, v in inside if t > start and v > v1), hi)
            return start, end, "meter-bracket"
    return lo, hi, "five-hour-window"


def probe_rows() -> list[dict]:
    rows = []
    for n, line in enumerate(open("history/probes.jsonl"), 1):
        if line.strip():
            r = json.loads(line)
            start = P(r["ts"])
            rows.append({"account": r["account"], "start": start,
                         "end": start + timedelta(seconds=r.get("elapsed_s", 3600)),
                         "kind": "probe", "outcome": "completed", "model": r.get("model"),
                         "effort": r.get("effort"), "tokens_per_pct": r.get("tokens_per_pct"),
                         "precision": "elapsed_s",
                         "account_source": "probes.jsonl", "source": f"history/probes.jsonl:{n}"})
    return rows


def effort_matrix_row() -> dict:
    meta = json.load(open("data/effort_matrix.json"))["_meta"]
    return {"account": EFFORT_MATRIX_ACCOUNT, "start": P(meta["started"]), "end": P(meta["finished"]),
            "kind": "effort-matrix", "outcome": "completed", "model": None, "effort": None,
            "tokens_per_pct": None,
            "precision": "recorded", "account_source": "effort_matrix", "source": "data/effort_matrix.json:_meta"}


def same_run(row: dict, account: str, model: str | None, tokens_per_pct: float | None,
             start: datetime, end: datetime) -> bool:
    """Is this probes.jsonl row the same run as a completed run in the log?

    Account, model and tokens per 1% to the nearest token, *and* the two spans within
    `MATCH_TOLERANCE` of each other. The first three alone are not a key: the probe runs the
    same model on the same account night after night, and two runs that happen to read the
    same rounded tokens per 1% are then indistinguishable -- the log's run would be dropped as
    a duplicate of a row from another day and its span would excuse no stretch. The log run's
    span is a bracket rather than an instant (`bracket`), which can be a whole five-hour
    window wide, so the test is proximity rather than containment: the two spans must overlap
    once each is widened by one window.
    """
    if row["account"] != account or row["model"] != model:
        return False
    if row["tokens_per_pct"] is None or tokens_per_pct is None:
        return False
    if round(row["tokens_per_pct"]) != round(tokens_per_pct):
        return False
    return (start - MATCH_TOLERANCE) < row["end"] and row["start"] < (end + MATCH_TOLERANCE)


def collect() -> list[dict]:
    series = meter_series()
    rows = probe_rows() + [effort_matrix_row()]
    for path, kind in ((PROBE_LOG, "probe"), (OUTPUT_PROBE_LOG, "output-probe")):
        for run in parse_log(path, kind):
            account, how = (run["account"], "log") if run["account"] else attribute(run, series)
            start, end, precision = bracket(run, account, series)
            if run["outcome"] == "completed":
                match = [r for r in rows
                         if same_run(r, account, run["model"], run["tokens_per_pct"], start, end)]
                if match:
                    match[0]["source"] += f" ({path.name}:{run['line']})"
                    continue
            rows.append({"account": account, "start": start, "end": end, "kind": kind,
                         "outcome": run["outcome"], "model": run["model"], "effort": run.get("effort"),
                         "tokens_per_pct": run.get("tokens_per_pct"), "precision": precision,
                         "account_source": how, "detail": run.get("detail"),
                         "source": f"{path}:{run['line']}"})
    rows.sort(key=lambda r: (r["start"], r["account"]))
    for r in rows:
        r["start"], r["end"] = r["start"].isoformat(), r["end"].isoformat()
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--print", dest="show", action="store_true", help="write nothing, print the rows")
    a = ap.parse_args(argv)
    rows = collect()
    text = "".join(json.dumps(r) + "\n" for r in rows)
    if a.show:
        sys.stdout.write(text)
    else:
        a.out.write_text(text)
        print(f"{len(rows)} runs -> {a.out}")
        for r in rows:
            if r["outcome"] != "completed" or r["kind"] != "probe":
                print(f"   {r['account']:6} {r['start'][:19]} {r['end'][:19]} {r['kind']:12} "
                      f"{r['outcome']:8} {r['precision']:16} {(r.get('detail') or '')[:48]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
