"""Sample one account's usage meter into that account's own log.

The passive join on gs (tracker/gs_passive.py) divides what an account's
transcripts spent by how far that same account's meter moved, so each account
needs its meter read every few minutes. jwork's already is, by greenscape-org's
usage-ceiling.py (gs-usage-ceiling.timer), but that script exists to pause
Paperclip seats: its log path is hard-coded and it records no reset times.
Pointing it at Dave would have written Dave's readings into jwork's log and let
`--enforce` pause the company's seats on Dave's meter. This is the tracker's
own sampler instead: read-only, one line per reading, one account per file.

One account per file is enforced here rather than asked for. Every line
carries `identity`, a short hash of the config dir's `oauthAccount.accountUuid`
(the uuid itself stays out of the log), and `sample` refuses (exit 2) to append
to a log whose first line names another identity, or to write a line it cannot
label. tracker.samples.parse_meter_log refuses to read a mixed log as well. Two
accounts in one file silently average two different meters, and the ceiling
log has already done exactly that: until the 2026-09-05 seat.conf drop-in it
read ~/.claude, a different account from the jwork seat it has read since.

Lines take moonlighter's usage_log.jsonl shape (`ts`, then `five_hour` and
`seven_day`, each with `utilization` and `resets_at`), so parse_moonlighter
and tracker/weekly.py's parse_row read them unchanged, and reset times make
window boundaries exact rather than inferred from a drop. A failed read is
logged as an `error` line, never with the token, and exits 4 as usage-ceiling.py
does, so the gap shows in the log instead of passing silently; the unit
counts 4 as success and the next tick tries again. Two failures get their own
`reason` beside `error`, both still exit 4: `auth_expired` (a 401 whose token
was the same before and after a re-read of the credentials file -- see
usage_api.AuthExpired; this sampler never refreshes the token itself, only
notices when Claude Code already has) and `rate_limited` (a 429, which also
carries `retry_after_s`; a later tick reads that back off the log itself and
skips the network call entirely until it has elapsed, rather than retrying
inside one tick or hammering the endpoint again next minute).

    python3 -m tracker.meter_log --config-dir ~/.claude-dave --account dave \\
        --log ~/.paperclip/ops/claude-usage-meter-dave.log

Exit codes: 0 sampled, 2 refused (the log belongs to another account, or the
config dir is signed out), 4 the usage read failed.
"""
from __future__ import annotations

import functools
import hashlib
import json
import sys
import urllib.error
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .usage_api import AuthExpired, Utilization, _default_fetch, read_usage

EXIT_OK, EXIT_REFUSED, EXIT_READ_FAILED = 0, 2, 4
#: A 429 is logged and left for the next tick. read_usage's own default retries
#: for up to ~15 minutes, which would carry one oneshot through three of its
#: own timer's ticks.
NO_RETRY_FETCH = functools.partial(_default_fetch, max_retries=0)
#: Fallback backoff when a 429 carries no Retry-After: the measured ceiling on gs is
#: five calls in ten seconds before a 429 with Retry-After: 300, across three accounts
#: sampling once a minute each, so 300s is what a real block from this endpoint looks
#: like.
DEFAULT_429_BACKOFF_S = 300


def account_identity(config_dir: Path) -> str | None:
    """A short hash of the account a config dir is signed into, or None if signed out.

    Read from `.claude.json`'s `oauthAccount.accountUuid`; the credentials file
    is never opened for this.
    """
    try:
        doc = json.loads((Path(config_dir) / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    account = doc.get("oauthAccount") if isinstance(doc, dict) else None
    uuid = account.get("accountUuid") if isinstance(account, dict) else None
    return hashlib.sha256(uuid.encode()).hexdigest()[:12] if isinstance(uuid, str) and uuid else None


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def sample_line(u: Utilization, account: str, identity: str) -> dict:
    return {"ts": _stamp(u.ts), "account": account, "identity": identity,
            "five_hour": {"utilization": u.five_hour, "resets_at": u.five_hour_resets_at},
            "seven_day": {"utilization": u.seven_day, "resets_at": u.seven_day_resets_at}}


def log_owner(log: Path) -> str | None:
    """The identity on the log's first labelled line; None for a new or empty log."""
    try:
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ident = json.loads(line).get("identity")
                except (ValueError, AttributeError):
                    continue
                if ident:
                    return ident
    except FileNotFoundError:
        return None
    return None


def _append(log: Path, line: dict) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _last_line(log: Path) -> dict | None:
    """The most recent parseable line in `log`, or None for a new, empty or unparseable log."""
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _retry_after_s(e: urllib.error.HTTPError) -> float:
    ra = e.headers.get("Retry-After") if e.headers else None
    return float(ra) if ra and ra.replace(".", "", 1).isdigit() else DEFAULT_429_BACKOFF_S


def _backoff_remaining_s(last: dict | None, now: datetime) -> float | None:
    """Seconds still to wait, if `last` is an unexpired 429 gap this tick should skip.

    A 429 is rate limiting shared across gs's three accounts (measured ceiling: five
    calls in ten seconds trips one with Retry-After: 300), so a tick that fires again
    before that window is over just adds another call against the same block instead
    of recovering from it. The gap line itself carries the backoff, so a later tick --
    a separate process, run a minute apart by systemd -- can read it back without any
    state of its own and skip the network call entirely until it has elapsed.
    """
    if not last or last.get("reason") != "rate_limited":
        return None
    try:
        since = datetime.fromisoformat(last["ts"])
        retry_after_s = float(last["retry_after_s"])
    except (KeyError, ValueError, TypeError):
        return None
    remaining = (since + timedelta(seconds=retry_after_s) - now).total_seconds()
    return remaining if remaining > 0 else None


def sample(config_dir: Path, account: str, log: Path, fetch: Callable[[str, dict], dict] | None = None,
           now: Callable[[], datetime] | None = None) -> int:
    """Read the meter once and append one line to `log`. Returns the exit code."""
    config_dir, log = Path(config_dir), Path(log)
    identity = account_identity(config_dir)
    if identity is None:
        print(f"refused: {config_dir} has no signed-in account to label a reading with", file=sys.stderr)
        return EXIT_REFUSED
    owner = log_owner(log)
    if owner is not None and owner != identity:
        print(f"refused: {log} holds account {owner}, not {identity} ({account}); one account per log",
              file=sys.stderr)
        return EXIT_REFUSED
    clock = now or (lambda: datetime.now(timezone.utc))
    now_ts = clock()
    remaining = _backoff_remaining_s(_last_line(log), now_ts)
    if remaining is not None:
        # Still inside a previous tick's 429 backoff: skip the network call rather than
        # retrying inside this tick, and write nothing -- the gap line already on the
        # log carries the backoff the next tick will check.
        print(f"{account}: skipping tick, {remaining:.0f}s left in 429 backoff", file=sys.stderr)
        return EXIT_READ_FAILED
    try:
        u = read_usage(config_dir, fetch=fetch or NO_RETRY_FETCH, now=clock)
        if u.five_hour is None:
            raise ValueError("no five_hour utilization in the usage response")
    except AuthExpired as e:
        # Same token before and after the 401: not transient. Logged with its own
        # reason so it reads differently from a network hiccup, but still exit 4 --
        # the next tick tries again, and by then Claude Code may have refreshed it.
        _append(log, {"ts": _stamp(now_ts), "account": account, "identity": identity,
                      "error": f"{type(e).__name__}: {e}"[:200], "reason": "auth_expired"})
        print(f"{account}: usage read failed (token expired), logged as a gap", file=sys.stderr)
        return EXIT_READ_FAILED
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after_s = _retry_after_s(e)
            _append(log, {"ts": _stamp(now_ts), "account": account, "identity": identity,
                          "error": f"{type(e).__name__}: {e}"[:200], "reason": "rate_limited",
                          "retry_after_s": retry_after_s})
            print(f"{account}: usage read failed (429), backing off {retry_after_s:.0f}s", file=sys.stderr)
            return EXIT_READ_FAILED
        _append(log, {"ts": _stamp(now_ts), "account": account, "identity": identity,
                      "error": f"{type(e).__name__}: {e}"[:200]})
        print(f"{account}: usage read failed ({type(e).__name__}), logged as a gap", file=sys.stderr)
        return EXIT_READ_FAILED
    except Exception as e:  # noqa: BLE001 - any other failed read is one logged gap, never a crashed timer
        _append(log, {"ts": _stamp(now_ts), "account": account, "identity": identity,
                      "error": f"{type(e).__name__}: {e}"[:200]})
        print(f"{account}: usage read failed ({type(e).__name__}), logged as a gap", file=sys.stderr)
        return EXIT_READ_FAILED
    _append(log, sample_line(u, account, identity))
    print(f"{account}: five_hour={u.five_hour:.0f}% seven_day={u.seven_day}")
    return EXIT_OK


def main(argv: list[str] | None = None, *, fetch: Callable[[str, dict], dict] | None = None,
         now: Callable[[], datetime] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Append one usage-meter reading for one account to that account's own log")
    ap.add_argument("--config-dir", type=Path, required=True)
    ap.add_argument("--account", required=True,
                    help="label for the line, e.g. dave; the log is bound to the account's identity, not to this label")
    ap.add_argument("--log", type=Path, required=True)
    a = ap.parse_args(argv)
    return sample(a.config_dir, a.account, a.log, fetch=fetch, now=now)


if __name__ == "__main__":
    raise SystemExit(main())
