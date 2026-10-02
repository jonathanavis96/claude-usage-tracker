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

Exit codes: 0 sampled, or skipped by MIN_READ_SPACING_S, 2 refused (the log belongs to another account, or the
config dir is signed out), 4 the usage read failed.
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import sys
import time
import urllib.error
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .usage_api import RETRY_429_MAX_S, AuthExpired, Utilization, _default_fetch, read_usage

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
#: Least time between two calls for one account. On gs from 2026-09-23 (the timers moved
#: to one tick a minute) to 2026-10-01, every account drew a 429 with Retry-After: 0 on
#: about every other tick -- 18,784 such lines, 55% of everything logged -- while a call
#: about two minutes after the previous one went through. So a tick within this long of
#: the last good read or 429 skips the call: the same readings, without the failures.
#: Just under two of the timers' ~65 s ticks.
MIN_READ_SPACING_S = 110
#: Longest a logged Retry-After may stop sampling. The endpoint's own long answer is 3,600 s
#: (failure inventory F9: 15 of them on gs, after a run of Retry-After 0), so a cap below an
#: hour has the sampler call inside a block the server asked it to respect, and the endpoint
#: answers persistence with longer blocks. Two hours covers that; the cap only stops an
#: absurd value such as 86400 from halting sampling for a day. (usage_api's in-process
#: retry cap, RETRY_429_MAX_S, is a different thing: how long one process sleeps.)
MAX_BACKOFF_S = 2 * 60 * 60
#: Longest this tick waits for another run on the same log to finish before giving up.
LOCK_WAIT_S = 45
#: When the clock reads earlier than the log's last line, ticks are skipped (the log stays
#: in order) until it catches up -- but only if the gap is this small. A last line further
#: ahead than this was stamped by a clock that ran fast; waiting it out could stop sampling
#: for hours, so it is ignored instead and one row lands out of order (the join sorts).
MAX_CLOCK_STALL_S = RETRY_429_MAX_S


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


def schema_problems(body: dict) -> list[str]:
    """What in a usage response no longer has the shape this collector reads.

    A renamed key reads as null, and null is also what an idle window returns, so without
    this a schema change would be logged as ordinary readings and freeze the published
    series with nobody told (red-team, 2026-10-02)."""
    out = []
    for bucket in ("five_hour", "seven_day"):
        b = body.get(bucket)
        if not isinstance(b, dict):
            out.append(f"no {bucket} bucket")
            continue
        for key in ("utilization", "resets_at"):
            if key not in b:
                out.append(f"{bucket}.{key} missing")
    return out


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


def _append(log: Path, line: dict) -> bool:
    """Append one line. False (with a stderr note) when the disk refuses the write.

    A run killed mid-write leaves a partial last line with no newline; a newline goes
    first in that case so this line starts on its own and is not glued onto the fragment.
    """
    data = (json.dumps(line) + "\n").encode("utf-8")
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab+") as fh:
            if fh.tell() > 0:
                fh.seek(-1, 2)
                if fh.read(1) != b"\n":
                    data = b"\n" + data
            fh.write(data)
        return True
    except OSError as e:
        print(f"cannot write {log}: {type(e).__name__}: {e}", file=sys.stderr)
        return False


@contextlib.contextmanager
def _log_lock(log: Path, wait_s: float = LOCK_WAIT_S):
    """Hold `<log>.lock` so two runs never interleave read-check-fetch-append on one log.

    Yields False when another run held it for `wait_s`. A lock file that cannot be created
    (read-only directory) yields True without a lock: the append will fail and say so.
    """
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log.with_name(log.name + ".lock"), "a")
    except OSError:
        yield True
        return
    with fh:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _last_line(log: Path, tail_bytes: int = 8192) -> dict | None:
    """The most recent parseable object line in `log`, or None for a new, empty or unparseable log.

    Reads only the file's tail: the log gains a line every tick and is never rotated,
    so reading it whole each minute grows without bound.
    """
    try:
        with open(log, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - tail_bytes))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            return d
    return None


def _retry_after_s(e: urllib.error.HTTPError) -> float:
    ra = e.headers.get("Retry-After") if e.headers else None
    s = float(ra) if ra and ra.replace(".", "", 1).isdigit() else DEFAULT_429_BACKOFF_S
    return min(s, MAX_BACKOFF_S)


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
        retry_after_s = min(float(last["retry_after_s"]), MAX_BACKOFF_S)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return None
    remaining = min((since + timedelta(seconds=retry_after_s) - now).total_seconds(), retry_after_s)
    return remaining if remaining > 0 else None


def _ahead_s(last: dict | None, now: datetime) -> float | None:
    """How far `last`'s stamp is ahead of `now`, in seconds; None when it is not ahead."""
    if not last:
        return None
    try:
        since = datetime.fromisoformat(last["ts"])
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return None
    ahead = (since - now).total_seconds()
    return ahead if ahead > 0 else None


def _since_last_call_s(last: dict | None, now: datetime) -> float | None:
    """Seconds since `last`, when it is a good reading or a 429; None otherwise.

    An expired token is spaced too (otherwise a dead account calls every minute for
    good). Other failures (a DNS error) say nothing about the endpoint's limit, so they
    never delay the next tick.
    """
    if not last or ("error" in last and last.get("reason") not in ("rate_limited", "auth_expired")):
        return None
    try:
        since = datetime.fromisoformat(last["ts"])
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        return (now - since).total_seconds()
    except (KeyError, ValueError, TypeError):
        return None


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
    with _log_lock(log) as locked:
        if not locked:
            print(f"{account}: skipping tick, another run holds {log}.lock", file=sys.stderr)
            return EXIT_READ_FAILED
        return _sample_locked(config_dir, account, log, identity, fetch, now)


def _sample_locked(config_dir: Path, account: str, log: Path, identity: str,
                   fetch: Callable[[str, dict], dict] | None, now: Callable[[], datetime] | None) -> int:
    clock = now or (lambda: datetime.now(timezone.utc))
    now_ts = clock()
    last = _last_line(log)
    ahead = _ahead_s(last, now_ts)
    if ahead is not None:
        if ahead <= MAX_CLOCK_STALL_S:
            print(f"{account}: skipping tick, clock is {ahead:.0f}s behind the log's last line", file=sys.stderr)
            return EXIT_READ_FAILED
        print(f"{account}: log's last line is {ahead:.0f}s in the future (fast clock); ignoring it",
              file=sys.stderr)
        last = None
    remaining = _backoff_remaining_s(last, now_ts)
    if remaining is not None:
        # Still inside a previous tick's 429 backoff: skip the network call rather than
        # retrying inside this tick, and write nothing -- the gap line already on the
        # log carries the backoff the next tick will check.
        print(f"{account}: skipping tick, {remaining:.0f}s left in 429 backoff", file=sys.stderr)
        return EXIT_READ_FAILED
    since_last = _since_last_call_s(last, now_ts)
    if since_last is not None and 0 <= since_last < MIN_READ_SPACING_S:
        print(f"{account}: skipping tick, last call {since_last:.0f}s ago (spacing {MIN_READ_SPACING_S}s)",
              file=sys.stderr)
        return EXIT_OK
    seen: dict = {}
    base_fetch = fetch or NO_RETRY_FETCH

    def capturing_fetch(url: str, headers: dict) -> dict:
        body = base_fetch(url, headers)
        seen["body"] = body
        return body

    try:
        u = read_usage(config_dir, fetch=capturing_fetch, now=clock)
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
    line = sample_line(u, account, identity)
    body = seen.get("body")
    if isinstance(body, dict):
        problems = schema_problems(body)
        if problems:
            line["schema"] = problems
            print(f"{account}: warning: usage response schema changed: {'; '.join(problems)}", file=sys.stderr)
        # Keep every other bucket (seven_day_sonnet today, whatever comes next) as sent:
        # a dropped bucket can never be recovered later. A name that clashes with one of
        # this line's own keys is skipped.
        for k, v in body.items():
            if k not in line:
                line[k] = v
    if not _append(log, line):
        print(f"{account}: reading not recorded, the log could not be written", file=sys.stderr)
        return EXIT_READ_FAILED
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
