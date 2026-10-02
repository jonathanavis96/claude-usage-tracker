"""One health check for the masterrig side of the tracker.

    python3 -m tracker.health            # exit 0 "ok", or exit 1 and one line naming the first failure
    python3 -m tracker.health --all      # every check, one line each

Checks, in order (the first failure is the one-line reason):

  collection   the newest meter sample (last line's `ts` in the usage log, default
               moonlighter's ~/.moonlighter/usage_log.jsonl, sampled every 30 min) is
               younger than two intervals.
  run          the supervisor's last successful passive run (tracker/supervise.py state
               file, `last_ok`) is younger than two intervals of the hourly cron.
  publisher    the last commit touching history/gs-passive.json (gs's "Daily publisher
               state", every 30 min) is younger than --publisher-max-age.
  token        ~/.claude/.credentials.json is readable and claudeAiOauth.expiresAt is
               not further in the past than --token-grace. Claude Code refreshes the
               access token only when it runs, so an idle few hours is normal; a token
               long expired, or an expired refresh token, means a re-login is needed.
  lock         no lock pid file whose process is dead, and none older than --lock-max-age.
  sizes        each log under --max-log-bytes and the history/ directory under
               --max-history-bytes.
  meters       (gs profile) each per-account meter log's newest usable reading (a line
               with `five_hour`, not a logged gap) is younger than --meter-max-age. The
               timers read every minute and 429s are routine, so 15 min is a stopped
               timer or an account whose reads all fail, never one slow tick. It is far
               above the ~130 s between reads that meter_log's 110 s spacing gives
               (a skipped tick exits 0 and writes nothing). Also fails when more than
               half of the last 40+ lines are 429s (2026-09-23: 55%, unseen nine days).

Profiles pick which checks run. `masterrig` (default) is collection, run, publisher,
token, lock, sizes. `gs` is run (the supervised bin/daily.sh, every 30 min), meters,
lock and sizes: gs has no moonlighter log, and its publisher is the job itself.

Each check reads its file fresh on every call; nothing is cached between runs.
Timestamps are parsed, never compared as strings.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("HOME", str(Path.home())))


@dataclass
class Config:
    usage_log: Path = HOME / ".moonlighter" / "usage_log.jsonl"
    collection_interval_s: int = 30 * 60
    state: Path = ROOT / ".supervise-state.json"
    run_interval_s: int = 60 * 60
    repo: Path = ROOT
    publisher_path: str = "history/gs-passive.json"
    publisher_max_age_s: int = 2 * 60 * 60
    credentials: Path = HOME / ".claude" / ".credentials.json"
    token_grace_s: int = 12 * 60 * 60
    lock_pidfile: Path = ROOT / ".supervise.pid"
    lock_max_age_s: int = 2 * 60 * 60
    logs: tuple[Path, ...] = ()
    max_log_bytes: int = 5 * 1024 * 1024
    history_dir: Path = ROOT / "history"
    max_history_bytes: int = 200 * 1024 * 1024
    meter_logs: tuple[Path, ...] = ()
    meter_max_age_s: int = 15 * 60
    checks: tuple[str, ...] | None = None  # None: every check but meters (masterrig)


GS_OPS = HOME / ".paperclip" / "ops"
GS_METER_ACCOUNTS = ("avis", "dave", "jwork")
GS_LOCK = GS_OPS / "claude-usage-daily.lock"   # the supervisor's flock; its pid file is .pid
GS_STATE = GS_OPS / "claude-usage-daily-state.json"
GS_LOG = GS_OPS / "claude-usage-daily.log"


def gs_config(state: Path, pidfile: Path, logs: tuple[Path, ...] = (), ops: Path = GS_OPS,
              repo: Path = ROOT) -> Config:
    """Health on gs: the supervised publisher run every 30 min and the three meter timers."""
    return Config(state=state, run_interval_s=30 * 60, lock_pidfile=pidfile, logs=logs, repo=repo,
                  history_dir=repo / "history",
                  meter_logs=tuple(ops / f"claude-usage-meter-{a}.log" for a in GS_METER_ACCOUNTS),
                  checks=("run", "meters", "lock", "sizes"))


def _parse_ts(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value / 1000 if value > 1e11 else value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _last_line(path: Path) -> dict | None:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 65536))
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            return row
    return None


def _age(seconds_ago: float) -> str:
    m = int(seconds_ago // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def check_collection(c: Config, now: float) -> str | None:
    try:
        row = _last_line(c.usage_log)
    except OSError as e:
        return f"collection: cannot read {c.usage_log}: {e.strerror}"
    ts = _parse_ts(row.get("ts")) if row else None
    if ts is None:
        return f"collection: no timestamped sample in {c.usage_log}"
    if now - ts > 2 * c.collection_interval_s:
        return f"collection: newest meter sample is {_age(now - ts)} old (limit {_age(2 * c.collection_interval_s)})"
    return None


def read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def check_run(c: Config, now: float) -> str | None:
    st = read_state(c.state)
    last_ok = st.get("last_ok")
    last_fail = st.get("last_fail")
    if st.get("last_exit") == 3 and isinstance(last_fail, (int, float)) and last_fail >= (last_ok or 0):
        return "run: bin/passive.sh refused to run, the checkout is off main (exit 3)"
    if not isinstance(last_ok, (int, float)):
        return f"run: no successful supervised run recorded in {c.state.name}"
    if now - last_ok > 2 * c.run_interval_s:
        reason = st.get("last_reason") or "unknown"
        return f"run: last successful run {_age(now - last_ok)} ago, last failure: {reason}"
    return None


def check_publisher(c: Config, now: float) -> str | None:
    # The newest of HEAD and its upstream: on masterrig bin/passive.sh fetches origin every
    # hour but pulls only about once a day, so HEAD alone would read gs's half-hourly
    # publisher commits as a day old and alert every day.
    stamps = []
    for rev in ("HEAD", "@{upstream}"):
        try:
            out = subprocess.run(["git", "-C", str(c.repo), "log", "-1", "--format=%ct", rev, "--", c.publisher_path],
                                 capture_output=True, text=True, timeout=30, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"publisher: git log failed: {e}"
        if out.isdigit():
            stamps.append(int(out))
    if not stamps:
        return f"publisher: no commit touches {c.publisher_path}"
    age = now - max(stamps)
    if age > c.publisher_max_age_s:
        return f"publisher: {c.publisher_path} last committed {_age(age)} ago (limit {_age(c.publisher_max_age_s)})"
    return None


def check_token(c: Config, now: float) -> str | None:
    try:
        oauth = json.loads(c.credentials.read_text()).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        return f"token: {c.credentials} unreadable"
    if not oauth.get("accessToken"):
        return "token: no claudeAiOauth.accessToken (signed out)"
    refresh = _parse_ts(oauth.get("refreshTokenExpiresAt"))
    if refresh is not None and refresh < now:
        return "token: refresh token expired, run `claude /login`"
    exp = _parse_ts(oauth.get("expiresAt"))
    if exp is None:
        return "token: no expiresAt"
    if now - exp > c.token_grace_s:
        return f"token: access token expired {_age(now - exp)} ago and not refreshed"
    return None


def _tail_rows(path: Path, n: int = 60) -> list[dict]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - 65536)
        f.seek(start)
        lines = f.read().splitlines()
    if start:
        lines = lines[1:]  # the first line is cut part way
    rows = []
    for raw in lines[-n:]:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_lock(c: Config, now: float) -> str | None:
    try:
        text = c.lock_pidfile.read_text().strip()
        mtime = c.lock_pidfile.stat().st_mtime
    except OSError:
        return None
    if not text.isdigit() or not pid_alive(int(text)):
        return f"lock: stale lock {c.lock_pidfile.name} (pid {text or '?'} not running)"
    if now - mtime > c.lock_max_age_s:
        return f"lock: run pid {text} has held the lock {_age(now - mtime)}"
    return None


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def check_sizes(c: Config, now: float) -> str | None:
    for log in c.logs:
        try:
            size = log.stat().st_size
        except OSError:
            continue
        if size > c.max_log_bytes:
            return f"sizes: {log.name} is {size // 1024} KiB (limit {c.max_log_bytes // 1024} KiB)"
    if c.history_dir.is_dir():
        size = _dir_bytes(c.history_dir)
        if size > c.max_history_bytes:
            return f"sizes: {c.history_dir.name}/ is {size // (1024 * 1024)} MiB (limit {c.max_history_bytes // (1024 * 1024)} MiB)"
    return None


def check_meters(c: Config, now: float) -> str | None:
    for log in c.meter_logs:
        try:
            with open(log, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 262144))
                lines = f.read().splitlines()
        except OSError as e:
            return f"meters: cannot read {log.name}: {e.strerror}"
        newest = None
        for raw in reversed(lines):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("five_hour"), dict):
                newest = _parse_ts(row.get("ts"))
                if newest is not None:
                    break
        try:
            rows = _tail_rows(log)
        except OSError:
            rows = []
        limited = sum(1 for r in rows if r.get("reason") == "rate_limited")
        if len(rows) >= 40 and limited * 2 > len(rows):
            return f"meters: {limited} of the last {len(rows)} lines in {log.name} are 429s"
        if newest is None:
            return f"meters: no usable reading near the end of {log.name}"
        if now - newest > c.meter_max_age_s:
            return (f"meters: newest usable reading in {log.name} is {_age(now - newest)} old "
                    f"(limit {_age(c.meter_max_age_s)})")
    return None


CHECKS: dict[str, Callable[[Config, float], str | None]] = {
    "collection": check_collection,
    "run": check_run,
    "publisher": check_publisher,
    "token": check_token,
    "lock": check_lock,
    "sizes": check_sizes,
    "meters": check_meters,
}


def run_checks(c: Config, now: float | None = None, only: list[str] | None = None) -> list[tuple[str, str | None]]:
    now = time.time() if now is None else now
    chosen = c.checks if c.checks is not None else tuple(n for n in CHECKS if n != "meters")
    return [(name, fn(c, now)) for name, fn in CHECKS.items()
            if name in chosen and (not only or name in only)]


def first_failure(c: Config, now: float | None = None, skip: tuple[str, ...] = ()) -> str | None:
    for name, reason in run_checks(c, now):
        if reason and name not in skip:
            return reason
    return None


def build_parser() -> argparse.ArgumentParser:
    d = Config()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--profile", choices=("masterrig", "gs"), default="masterrig",
                   help="gs: run, meters, lock, sizes against ~/.paperclip/ops (see module doc)")
    p.add_argument("--all", action="store_true", help="print every check, not just the first failure")
    p.add_argument("--skip", action="append", default=[], choices=list(CHECKS), help="leave a check out")
    p.add_argument("--usage-log", type=Path, default=d.usage_log)
    p.add_argument("--collection-interval", type=int, default=d.collection_interval_s)
    p.add_argument("--state", type=Path, default=d.state)
    p.add_argument("--run-interval", type=int, default=d.run_interval_s)
    p.add_argument("--repo", type=Path, default=d.repo)
    p.add_argument("--publisher-max-age", type=int, default=d.publisher_max_age_s)
    p.add_argument("--credentials", type=Path, default=d.credentials)
    p.add_argument("--token-grace", type=int, default=d.token_grace_s)
    p.add_argument("--pidfile", type=Path, default=d.lock_pidfile)
    p.add_argument("--log", type=Path, action="append", default=[], help="a log whose size is bounded (repeatable)")
    p.add_argument("--max-log-bytes", type=int, default=d.max_log_bytes)
    p.add_argument("--max-history-bytes", type=int, default=d.max_history_bytes)
    p.add_argument("--meter-log", type=Path, action="append", default=[],
                   help="a per-account meter log (tracker.meter_log) to check for freshness (repeatable)")
    p.add_argument("--meter-max-age", type=int, default=d.meter_max_age_s)
    return p


def config_from_args(a: argparse.Namespace) -> Config:
    if a.profile == "gs":
        d = Config()
        return gs_config(state=GS_STATE if a.state == d.state else a.state,
                         pidfile=GS_LOCK.with_suffix(".pid") if a.pidfile == d.lock_pidfile else a.pidfile,
                         logs=tuple(a.log) or (GS_LOG,), repo=a.repo)
    return Config(usage_log=a.usage_log, collection_interval_s=a.collection_interval, state=a.state,
                  run_interval_s=a.run_interval, repo=a.repo, history_dir=a.repo / "history",
                  publisher_max_age_s=a.publisher_max_age, credentials=a.credentials,
                  token_grace_s=a.token_grace, lock_pidfile=a.pidfile, logs=tuple(a.log),
                  max_log_bytes=a.max_log_bytes, max_history_bytes=a.max_history_bytes,
                  meter_logs=tuple(a.meter_log), meter_max_age_s=a.meter_max_age)


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    c = config_from_args(a)
    results = [(n, r) for n, r in run_checks(c) if n not in a.skip]
    if a.all:
        for name, reason in results:
            print(f"{name}: ok" if reason is None else reason)
    bad = [r for _, r in results if r]
    if not a.all:
        print(bad[0] if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
