"""Read subscription utilization from the Claude OAuth usage endpoint."""
from __future__ import annotations

import functools
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


@dataclass(frozen=True)
class Utilization:
    ts: datetime
    five_hour: float | None
    seven_day: float | None
    five_hour_resets_at: str | None
    seven_day_resets_at: str | None = None


def _bucket(body: dict, key: str) -> tuple[float | None, str | None]:
    b = body.get(key) or {}
    u = b.get("utilization")
    return (float(u) if u is not None else None, b.get("resets_at"))


def parse_usage(body: dict, now: datetime) -> Utilization:
    fh, fh_reset = _bucket(body, "five_hour")
    sd, sd_reset = _bucket(body, "seven_day")
    return Utilization(ts=now, five_hour=fh, seven_day=sd, five_hour_resets_at=fh_reset,
                        seven_day_resets_at=sd_reset)


class AuthExpired(Exception):
    """A 401 whose on-disk access token had not changed since the request that failed.

    Claude Code owns the OAuth refresh: it renews its own token and rewrites
    `.credentials.json` under the config dir, rotating the refresh token in the
    process. This sampler must never refresh the token itself -- a refresh here
    would rotate the refresh token out from under an active, logged-in Claude
    Code session and could sign it out. So a 401 is handled by re-reading the
    credentials file (Claude Code may have refreshed it between our read and the
    failed request) and retrying once only if the token on disk changed; if it
    is the same token, the account needs an interactive login, not a retry, and
    this is raised instead so the caller can log a gap with a reason distinct
    from a transient failure.
    """


RETRY_429_S = (30, 60, 120, 240, 480)
RETRY_429_MAX_S = 1200  # 20 minutes: doubling backoff caps here once RETRY_429_S is exhausted


def _default_fetch(url: str, headers: dict, sleep: Callable[[float], None] = time.sleep,
                    max_retries: int | None = None) -> dict:
    """GET the usage endpoint; on 429 back off and retry, honouring Retry-After when present.

    The first five waits are RETRY_429_S (30, 60, ..., 480 s); after that the wait keeps
    doubling, capped at RETRY_429_MAX_S (20 minutes), and retries continue indefinitely
    (`max_retries=None`, the default) rather than re-raising: a scheduled probe with a
    wall-clock deadline supplies a `sleep` that raises once the deadline is gone (see
    tracker/probe.py's `deadline_sleep`), which is the only thing that should stop this
    loop. A caller with no deadline of its own must pass a `max_retries` bound so a run
    cannot spin forever (see `read_usage`'s default fetch below). Non-429 HTTP errors
    still raise immediately.
    """
    req = urllib.request.Request(url, headers=headers)
    wait = RETRY_429_S[0]
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            if max_retries is not None and attempt >= max_retries:
                raise
            wait = RETRY_429_S[attempt] if attempt < len(RETRY_429_S) else min(wait * 2, RETRY_429_MAX_S)
            ra = e.headers.get("Retry-After") if e.headers else None
            actual_wait = min(max(float(ra), wait), RETRY_429_MAX_S) if ra and ra.isdigit() else wait
            sleep(actual_wait)
            attempt += 1


def _read_token(config_dir: Path) -> str:
    creds = Path(config_dir) / ".credentials.json"
    if not creds.exists():
        raise FileNotFoundError(f"no credentials at {creds}")
    return json.loads(creds.read_text())["claudeAiOauth"]["accessToken"]


def read_usage(config_dir: Path, fetch: Callable[[str, dict], dict] | None = None,
               now: Callable[[], datetime] | None = None) -> Utilization:
    config_dir = Path(config_dir)
    token = _read_token(config_dir)
    # No caller-supplied deadline here, so the default fetch is bounded: it retries a
    # 429 up to len(RETRY_429_S) extra times (matching the old fixed schedule's length)
    # rather than spinning forever, unlike tracker/probe.py's deadline-bounded fetch.
    default_fetch = functools.partial(_default_fetch, max_retries=len(RETRY_429_S))
    do_fetch = fetch or default_fetch

    def _call(tok: str) -> dict:
        headers = {"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20"}
        return do_fetch(USAGE_URL, headers)

    try:
        body = _call(token)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        # The token may have been refreshed (by Claude Code, never by us) between our
        # read above and this failed request. Re-read it and retry once only if it
        # actually changed; a 401 against the same token is not transient -- see
        # AuthExpired.
        fresh = _read_token(config_dir)
        if fresh == token:
            raise AuthExpired(f"401 with unchanged token at {config_dir}") from e
        body = _call(fresh)
    return parse_usage(body, (now or (lambda: datetime.now(timezone.utc)))())


def same_reset(a: str | None, b: str | None, tol_s: float = 60) -> bool:
    """True unless both stamps are present and differ by more than tol_s.

    The endpoint jitters resets_at by sub-second amounts between reads, and that
    jitter crosses minute boundaries, so string or minute comparison is wrong.
    """
    if not a or not b:
        return True
    try:
        da, db = datetime.fromisoformat(a), datetime.fromisoformat(b)
    except ValueError:
        return a == b
    return abs((da - db).total_seconds()) <= tol_s
