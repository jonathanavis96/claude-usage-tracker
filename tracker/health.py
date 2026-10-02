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
  meter        (only with --meter-log) each gs-style per-account meter log has a line
               younger than --meter-max-age (default 180 s). meter_log skips a tick
               within 110 s of the last call, exiting 0 with no line, so reads land
               about 130 s apart; a 429 gap line extends the limit by its Retry-After.
  lock         no lock pid file whose process is dead, and none older than --lock-max-age.
  sizes        each log under --max-log-bytes and the history/ directory under
               --max-history-bytes.

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
    meter_max_age_s: int = 180


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
    try:
        out = subprocess.run(["git", "-C", str(c.repo), "log", "-1", "--format=%ct", "--", c.publisher_path],
                             capture_output=True, text=True, timeout=30, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"publisher: git log failed: {e}"
    if not out.isdigit():
        return f"publisher: no commit touches {c.publisher_path}"
    age = now - int(out)
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


def check_meter(c: Config, now: float) -> str | None:
    for log in c.meter_logs:
        try:
            row = _last_line(log)
        except OSError as e:
            return f"meter: cannot read {log}: {e.strerror}"
        ts = _parse_ts(row.get("ts")) if row else None
        if ts is None:
            return f"meter: no timestamped line in {log}"
        limit = c.meter_max_age_s
        if row.get("reason") == "rate_limited":
            try:
                limit += min(float(row.get("retry_after_s") or 0), 1200)
            except (TypeError, ValueError):
                pass
        if now - ts > limit:
            return f"meter: newest line in {log.name} is {_age(now - ts)} old (limit {int(limit)}s)"
    return None


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


CHECKS: dict[str, Callable[[Config, float], str | None]] = {
    "collection": check_collection,
    "run": check_run,
    "publisher": check_publisher,
    "token": check_token,
    "meter": check_meter,
    "lock": check_lock,
    "sizes": check_sizes,
}


def run_checks(c: Config, now: float | None = None, only: list[str] | None = None) -> list[tuple[str, str | None]]:
    now = time.time() if now is None else now
    return [(name, fn(c, now)) for name, fn in CHECKS.items() if not only or name in only]


def first_failure(c: Config, now: float | None = None, skip: tuple[str, ...] = ()) -> str | None:
    for name, reason in run_checks(c, now):
        if reason and name not in skip:
            return reason
    return None


def build_parser() -> argparse.ArgumentParser:
    d = Config()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
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
