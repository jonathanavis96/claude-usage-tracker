"""Read subscription utilization from the Claude OAuth usage endpoint."""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


@dataclass(frozen=True)
class Utilization:
    ts: datetime
    five_hour: float | None
    seven_day: float | None
    five_hour_resets_at: str | None


def _bucket(body: dict, key: str) -> tuple[float | None, str | None]:
    b = body.get(key) or {}
    u = b.get("utilization")
    return (float(u) if u is not None else None, b.get("resets_at"))


def parse_usage(body: dict, now: datetime) -> Utilization:
    fh, fh_reset = _bucket(body, "five_hour")
    sd, _ = _bucket(body, "seven_day")
    return Utilization(ts=now, five_hour=fh, seven_day=sd, five_hour_resets_at=fh_reset)


RETRY_429_S = (30, 60, 120, 240, 480)


def _default_fetch(url: str, headers: dict, sleep: Callable[[float], None] = time.sleep) -> dict:
    """GET the usage endpoint; on 429 back off and retry, honouring Retry-After when present."""
    req = urllib.request.Request(url, headers=headers)
    for wait in (*RETRY_429_S, None):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 429 or wait is None:
                raise
            ra = e.headers.get("Retry-After") if e.headers else None
            sleep(float(ra) if ra and ra.isdigit() else wait)
    raise AssertionError("unreachable")


def read_usage(config_dir: Path, fetch: Callable[[str, dict], dict] | None = None,
               now: Callable[[], datetime] | None = None) -> Utilization:
    creds = Path(config_dir) / ".credentials.json"
    if not creds.exists():
        raise FileNotFoundError(f"no credentials at {creds}")
    token = json.loads(creds.read_text())["claudeAiOauth"]["accessToken"]
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
    body = (fetch or _default_fetch)(USAGE_URL, headers)
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
