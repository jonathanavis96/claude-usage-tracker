"""One poller per host and account for the OAuth usage endpoint; everyone else reads its cache.

WHY THIS EXISTS
---------------
On 2026-10-02 at least seven processes across masterrig and gs polled
https://api.anthropic.com/api/oauth/usage independently (inventory and switch-over
plan: docs/burn-20261002/usage-poll.md). The endpoint's limiter is shared across the
accounts on one host: in the week to 2026-10-02 the three gs meters were refused in the
same minute far more often than one at a time (3,454 minutes all three, 1,278 only one).
So every extra reader of one account also costs the other accounts on that host, and
the meters alone were refused on 45-65% of their calls.

This module is the single reader. `poll_once` is the only code that calls the endpoint
for an (account, host) pair; it holds `<account>.lock` with a non-blocking flock so a
second poller on the same host and account returns "busy" instead of calling. It writes
`<account>.json` atomically (temp file in the same directory, fsync, os.replace, fsync
of the directory), so a reader sees either the previous record or the new one, never a
torn file. `read_cached` only ever reads that file and never calls the API.

THE CACHE RECORD
----------------
    schema                 1
    account                the label the poller was started with ("avis", "jwork", ...)
    identity               short hash of the config dir's accountUuid (meter_log.account_identity)
    host                   socket.gethostname() of the poller
    fetched_at             UTC stamp of the last successful read (kept through failures)
    buckets                the raw response body of that read, every bucket as sent
    last_attempt_at        UTC stamp of the last call (successful or not)
    last_error             None after a success; otherwise a short error string (never a token)
    last_error_reason      "rate_limited", "auth_expired", "schema" or "error"
    consecutive_refusals   429s in a row; reset by a success
    next_allowed_at        no call for this account before this UTC stamp

BACKOFF
-------
After a success the next call waits MIN_READ_SPACING_S (meter_log's measured spacing:
calls two minutes apart went through while one-minute calls drew a 429 every other
tick). After the n-th consecutive 429 it waits

    max(Retry-After, MIN_READ_SPACING_S * 2 ** (n - 1)), capped at MAX_BACKOFF_S

so a Retry-After header is always honoured, and the endpoint's usual Retry-After: 0
still backs off. Because the limiter is per host, a 429 also writes `_host.json` with
the same next_allowed_at, and every poller on the host respects it before calling.

    python3 -m tracker.usage_cache poll --account avis --config-dir ~/.claude-avis [--loop]
    python3 -m tracker.usage_cache read --account avis [--max-age 600]

`read` prints the record plus `age_s` and `stale` as JSON. Exit codes: 0 fresh, 3
stale (older than --max-age), 4 missing or corrupt.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .meter_log import (MAX_BACKOFF_S, MIN_READ_SPACING_S, NO_RETRY_FETCH, account_identity,
                        schema_problems)
from .usage_api import AuthExpired, Utilization, parse_usage, read_usage

SCHEMA = 1
HOST_FILE = "_host.json"
#: A dead token is not a rate limit, but calling it every two minutes forever helps nobody.
AUTH_EXPIRED_SPACING_S = 600
EXIT_FRESH, EXIT_STALE, EXIT_MISSING = 0, 3, 4

Fetch = Callable[[str, dict], dict]
Clock = Callable[[], datetime]


def default_cache_dir() -> Path:
    env = os.environ.get("USAGE_CACHE_DIR")
    return Path(env) if env else Path.home() / ".cache" / "claude-usage-tracker" / "usage"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None


def _parse_stamp(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _check_account(account: str) -> str:
    if not account or "/" in account or account.startswith(".") or account.startswith("_"):
        raise ValueError(f"bad account label {account!r}")
    return account


def cache_path(account: str, cache_dir: Path | None = None) -> Path:
    return Path(cache_dir or default_cache_dir()) / f"{_check_account(account)}.json"


# --------------------------------------------------------------------------- writing

def write_json_durable(path: Path, doc: dict) -> None:
    """Replace `path` with `doc` so that a crash or a concurrent reader never sees a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)  # the record holds no token
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _load(path: Path) -> dict | None:
    """The JSON object at `path`; None when missing; raises ValueError when corrupt."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    doc = json.loads(text)  # ValueError on a torn or garbage file
    if not isinstance(doc, dict):
        raise ValueError("cache file is not a JSON object")
    return doc


# --------------------------------------------------------------------------- reading

@dataclass(frozen=True)
class CachedUsage:
    account: str
    status: str                      # "ok", "missing" or "corrupt"
    age_s: float | None = None       # seconds since fetched_at; None when never fetched
    stale: bool = True
    fetched_at: datetime | None = None
    buckets: dict | None = None
    last_error: str | None = None
    last_error_reason: str | None = None
    consecutive_refusals: int = 0
    next_allowed_at: datetime | None = None
    record: dict = field(default_factory=dict)

    def utilization(self) -> Utilization | None:
        """The cached buckets as usage_api.Utilization, stamped with fetched_at."""
        if not self.buckets or not self.fetched_at:
            return None
        return parse_usage(self.buckets, self.fetched_at)


def read_cached(account: str, cache_dir: Path | None = None, now: Clock | None = None,
                max_age_s: float | None = None) -> CachedUsage:
    """The cached reading for `account` and how old it is. Never calls the API."""
    path = cache_path(account, cache_dir)
    try:
        doc = _load(path)
    except (ValueError, OSError) as e:
        return CachedUsage(account=account, status="corrupt", last_error=f"{type(e).__name__}: {e}"[:200])
    if doc is None:
        return CachedUsage(account=account, status="missing")
    fetched_at = _parse_stamp(doc.get("fetched_at"))
    age = ((now or _utc_now)() - fetched_at).total_seconds() if fetched_at else None
    buckets = doc.get("buckets") if isinstance(doc.get("buckets"), dict) else None
    stale = age is None or buckets is None or (max_age_s is not None and age > max_age_s)
    refusals = doc.get("consecutive_refusals")
    return CachedUsage(
        account=account, status="ok", age_s=age, stale=stale, fetched_at=fetched_at, buckets=buckets,
        last_error=doc.get("last_error"), last_error_reason=doc.get("last_error_reason"),
        consecutive_refusals=refusals if isinstance(refusals, int) else 0,
        next_allowed_at=_parse_stamp(doc.get("next_allowed_at")), record=doc)


# --------------------------------------------------------------------------- polling

@contextlib.contextmanager
def _poller_lock(path: Path):
    """Non-blocking exclusive flock on `<account>.lock`: True when this process is the poller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def retry_after_s(e: urllib.error.HTTPError) -> float:
    """Retry-After in seconds when the 429 carried a numeric one; 0 otherwise."""
    ra = e.headers.get("Retry-After") if e.headers else None
    try:
        return max(0.0, float(ra)) if ra is not None else 0.0
    except ValueError:
        return 0.0  # an HTTP-date form: fall back to the exponential schedule


def refusal_backoff_s(consecutive: int, retry_after: float = 0.0) -> float:
    """Wait after the `consecutive`-th 429 in a row (1-based)."""
    exp = MIN_READ_SPACING_S * (2 ** max(consecutive - 1, 0))
    return float(min(max(retry_after, exp), MAX_BACKOFF_S))


def _host_gate(cache_dir: Path) -> datetime | None:
    try:
        doc = _load(cache_dir / HOST_FILE)
    except (ValueError, OSError):
        return None
    return _parse_stamp(doc.get("next_allowed_at")) if doc else None


def poll_once(account: str, config_dir: Path, *, fetch: Fetch | None = None, now: Clock | None = None,
              cache_dir: Path | None = None) -> str:
    """Make at most one call for `account` and record it. Returns what happened:

    fetched, rate_limited, auth_expired, schema, error -- a call was made;
    busy -- another poller on this host holds the account's lock, nothing done;
    backoff -- next_allowed_at (the account's or the host's) is still in the future.
    """
    cache_dir = Path(cache_dir or default_cache_dir())
    path = cache_path(account, cache_dir)
    clock = now or _utc_now
    with _poller_lock(path.with_suffix(".lock")) as mine:
        if not mine:
            return "busy"
        try:
            prev = _load(path) or {}
        except (ValueError, OSError):
            prev = {}  # corrupt: start over; the next write replaces it
        t = clock()
        gates = [g for g in (_parse_stamp(prev.get("next_allowed_at")), _host_gate(cache_dir)) if g]
        if gates and t < max(gates):
            return "backoff"

        seen: dict = {}
        base = fetch or NO_RETRY_FETCH

        def capturing(url: str, headers: dict) -> dict:
            body = base(url, headers)
            seen["body"] = body
            return body

        rec = {k: prev.get(k) for k in ("fetched_at", "buckets")}
        rec.update(schema=SCHEMA, account=account, identity=account_identity(Path(config_dir)),
                   host=socket.gethostname(), last_attempt_at=_stamp(t),
                   consecutive_refusals=prev.get("consecutive_refusals") or 0)
        outcome, wait = "error", MIN_READ_SPACING_S
        try:
            read_usage(Path(config_dir), fetch=capturing, now=lambda: t)
            body = seen.get("body")
            problems = schema_problems(body) if isinstance(body, dict) else ["response is not an object"]
            if problems:
                outcome = "schema"
                rec.update(last_error="schema changed: " + "; ".join(problems), last_error_reason="schema")
            else:
                outcome = "fetched"
                rec.update(fetched_at=_stamp(t), buckets=body, last_error=None, last_error_reason=None,
                           consecutive_refusals=0)
        except AuthExpired as e:
            outcome, wait = "auth_expired", AUTH_EXPIRED_SPACING_S
            rec.update(last_error=f"{type(e).__name__}: {e}"[:200], last_error_reason="auth_expired")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                outcome = "rate_limited"
                n = int(rec["consecutive_refusals"]) + 1
                wait = refusal_backoff_s(n, retry_after_s(e))
                rec.update(consecutive_refusals=n, last_error=f"HTTP 429 (Retry-After {retry_after_s(e):.0f}s)",
                           last_error_reason="rate_limited")
            else:
                rec.update(last_error=f"HTTP {e.code}", last_error_reason="error")
        except Exception as e:  # noqa: BLE001 - one recorded failure, never a crashed poller
            rec.update(last_error=f"{type(e).__name__}: {e}"[:200], last_error_reason="error")
        nxt = t + timedelta(seconds=wait)
        rec["next_allowed_at"] = _stamp(nxt)
        write_json_durable(path, rec)
        if outcome == "rate_limited":
            gate = _host_gate(cache_dir)
            if gate is None or gate < nxt:
                write_json_durable(cache_dir / HOST_FILE, {"next_allowed_at": _stamp(nxt), "set_by": account,
                                                           "set_at": _stamp(t)})
        return outcome


def poll_loop(account: str, config_dir: Path, *, cache_dir: Path | None = None, interval_s: float | None = None,
              fetch: Fetch | None = None, now: Clock | None = None, sleep: Callable[[float], None] = time.sleep,
              max_iterations: int | None = None) -> None:
    """Poll forever (or `max_iterations` times), sleeping until the cache says the next call is allowed."""
    clock = now or _utc_now
    floor = max(float(interval_s or MIN_READ_SPACING_S), 1.0)
    i = 0
    while max_iterations is None or i < max_iterations:
        i += 1
        outcome = poll_once(account, config_dir, fetch=fetch, now=clock, cache_dir=cache_dir)
        if outcome == "busy":
            print(f"{account}: another poller holds the lock on this host; exiting", file=sys.stderr)
            return
        c = read_cached(account, cache_dir, now=clock)
        gate = _host_gate(Path(cache_dir or default_cache_dir()))
        until = max([g for g in (c.next_allowed_at, gate) if g] or [clock()])
        sleep(max(floor, (until - clock()).total_seconds()))


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Shared usage-endpoint poller and cache reader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="call the endpoint for one account (the only command that does)")
    p.add_argument("--account", required=True)
    p.add_argument("--config-dir", required=True, type=Path)
    p.add_argument("--cache-dir", type=Path)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=float, help=f"least seconds between calls (default {MIN_READ_SPACING_S})")
    r = sub.add_parser("read", help="print the cached reading and its age; never calls the API")
    r.add_argument("--account", required=True)
    r.add_argument("--cache-dir", type=Path)
    r.add_argument("--max-age", type=float, default=600.0)
    a = ap.parse_args(argv)
    if a.cmd == "poll":
        if a.loop:
            poll_loop(a.account, a.config_dir.expanduser(), cache_dir=a.cache_dir, interval_s=a.interval)
            return 0
        outcome = poll_once(a.account, a.config_dir.expanduser(), cache_dir=a.cache_dir)
        print(f"{a.account}: {outcome}")
        return 0 if outcome in ("fetched", "backoff", "busy") else 4
    c = read_cached(a.account, a.cache_dir, max_age_s=a.max_age)
    out = dict(c.record, account=a.account, status=c.status, age_s=c.age_s, stale=c.stale)
    if c.status == "corrupt":
        out["last_error"] = c.last_error
    print(json.dumps(out, sort_keys=True))
    if c.status != "ok" or c.buckets is None:
        return EXIT_MISSING
    return EXIT_STALE if c.stale else EXIT_FRESH


if __name__ == "__main__":
    sys.exit(main())
