"""Read subscription utilization from the rate-limit headers of a 1-token inference call.

For a seat whose only credential is a long-lived `claude setup-token` token (Dave's,
since his OAuth login was revoked on 2026-10-12). The usage endpoint refuses that token
(`403 OAuth token does not meet scope requirement user:profile`), but every inference
response carries the same two meters as `anthropic-ratelimit-unified-*` headers:

    anthropic-ratelimit-unified-5h-utilization: 0.05     (a 0-1 fraction)
    anthropic-ratelimit-unified-5h-reset: 1791840600     (epoch seconds)
    anthropic-ratelimit-unified-7d-utilization / -7d-reset, and -status,
    -5h-status, -7d-status, -representative-claim, -overage-status

So one `max_tokens: 1` Haiku call is a reading, converted here to the same
`Utilization` the endpoint path produces: percent 0-100 and ISO-8601 UTC reset times.
The call costs a little usage and starts the five-hour window if none is running, so
callers space it (tracker.meter_log: HEADER_PROBE_SPACING_S) and do not fire it into a
lapsed window (see meter_log's lapsed hold).

The token is read from the process environment or from one variable of an env file
(`~/.config/greenscape/env`), never from `.credentials.json`, and is never logged or
printed. A 401 is `TokenRejected`: a setup token is not refreshed by anything, so
there is nothing to re-read and retry.

    python3 -m tracker.header_usage --token-env GS_DAVE_CLAUDE_OAUTH_TOKEN

prints the parsed figures, one JSON object, and nothing else.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from .usage_api import Utilization

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_ENV_FILE = Path("~/.config/greenscape/env")
#: Claude Code's own system line: the subscription token is accepted for Claude Code traffic.
SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."
PREFIX = "anthropic-ratelimit-unified-"
#: Header fields kept beside the two meters, by their name after PREFIX.
EXTRA_FIELDS = ("status", "5h-status", "7d-status", "representative-claim", "overage-status", "reset")


class TokenRejected(Exception):
    """A 401 for a setup token. Nothing refreshes one, so this is not retried."""


def load_token(name: str, env_file: Path | None = DEFAULT_ENV_FILE) -> str:
    """The value of variable `name`, from the environment first, else from `env_file`.

    The file is parsed, never sourced: only `NAME=value` (optionally `export NAME=...`,
    optionally quoted) for the one name asked for is read. The error names the variable
    and the file, never a value.
    """
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if env_file is not None:
        path = Path(env_file).expanduser()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise LookupError(f"{name} is not set and {path} is unreadable ({type(e).__name__})") from None
        for line in lines:
            s = line.strip()
            if s.startswith("export "):
                s = s[len("export "):].lstrip()
            key, sep, v = s.partition("=")
            if sep and key.strip() == name:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if v:
                    return v
    raise LookupError(f"{name} is not set in the environment or in {env_file}")


def _iso(epoch: str | None) -> str | None:
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat() if epoch else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _pct(fraction: str | None) -> float | None:
    try:
        return round(float(fraction) * 100, 4) if fraction is not None else None
    except (TypeError, ValueError):
        return None


def parse_ratelimit_headers(headers: Mapping[str, str], now: datetime) -> tuple[Utilization, dict, list[str]]:
    """(reading, extra header fields, schema problems) from one response's headers.

    Header names are matched case-insensitively. A missing utilization or reset header
    is a schema problem, named, so a renamed header is not logged as an ordinary reading.
    """
    h = {k.lower(): v for k, v in headers.items()}
    problems = [f"{PREFIX}{k} missing" for k in ("5h-utilization", "5h-reset", "7d-utilization", "7d-reset")
                if PREFIX + k not in h]
    u = Utilization(ts=now, five_hour=_pct(h.get(PREFIX + "5h-utilization")),
                    seven_day=_pct(h.get(PREFIX + "7d-utilization")),
                    five_hour_resets_at=_iso(h.get(PREFIX + "5h-reset")),
                    seven_day_resets_at=_iso(h.get(PREFIX + "7d-reset")))
    extras = {k: h[PREFIX + k] for k in EXTRA_FIELDS if PREFIX + k in h}
    return u, extras, problems


def _default_post(url: str, headers: dict, body: bytes) -> Mapping[str, str]:
    """POST once and return the response headers. No retry: a 429 raises HTTPError."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()
        return dict(r.headers.items())


def read_header_usage(token: str, model: str = DEFAULT_MODEL,
                      post: Callable[[str, dict, bytes], Mapping[str, str]] | None = None,
                      now: Callable[[], datetime] | None = None) -> tuple[Utilization, dict, list[str]]:
    """Send one 1-token message as the token's seat and read the meters off the reply's headers."""
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = json.dumps({"model": model, "max_tokens": 1, "system": SYSTEM_PROMPT,
                       "messages": [{"role": "user", "content": "Hi"}]}).encode()
    try:
        got = (post or _default_post)(MESSAGES_URL, headers, body)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise TokenRejected("401: the setup token was refused") from e
        raise
    return parse_ratelimit_headers(got, (now or (lambda: datetime.now(timezone.utc)))())


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Read one seat's five-hour and seven-day meters from rate-limit "
                                             "headers (one 1-token call); prints figures only, never the token")
    ap.add_argument("--token-env", required=True, help="variable holding the setup token, e.g. GS_DAVE_CLAUDE_OAUTH_TOKEN")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    a = ap.parse_args(argv)
    u, extras, problems = read_header_usage(load_token(a.token_env, a.env_file), a.model)
    print(json.dumps({"ts": u.ts.isoformat(timespec="seconds"),
                      "five_hour": {"utilization": u.five_hour, "resets_at": u.five_hour_resets_at},
                      "seven_day": {"utilization": u.seven_day, "resets_at": u.seven_day_resets_at},
                      "ratelimit": extras, **({"schema": problems} if problems else {})}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
