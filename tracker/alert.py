"""Alert: one email to Jonathan through the site's send endpoint.

The probe redesign wants a human told about three things: an outlier (a drifted
row whose rerun agreed with the earlier median), a confirmed change (two
agreeing readings that both differ from the earlier median), and an output
class weight the weekly weight guard refused to apply. All three go through
this one helper so there is one place that knows the address, the secret and
the endpoint. `bin/probe.sh` and `bin/daily.sh` call the CLI; the publisher's
weight guard can call `send_alert` in-process.

The email itself is sent by alldonesites.com: `POST /api/notify/send` with the
existing bearer secret and a `to` field mails that one address instead of the
subscriber list (site repo, `website/functions/api/notify/send.js`).

Everything here is advisory. Nothing that calls it may fail because the alert
did not go out, so a missing env file, a missing address or a refused POST is a
warning on stderr and the caller's own exit status is untouched (the CLI still
exits 1 on a refused POST so a log reader can tell; callers append `|| true`).

Config comes from `~/.claude-usage-notify.env` (mode 600, never printed), one
`KEY=VALUE` per line:

  NOTIFY_SEND_SECRET  bearer secret for the send endpoint (daily.sh already reads it)
  NOTIFY_ALERT_TO     the address alerts go to (Jonathan's inbox)
  NOTIFY_SEND_URL     optional endpoint override (default: the production URL)

An environment variable of the same name overrides the file, which is how the
tests and a dry run point the helper at a local server.
"""
from __future__ import annotations
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

ENV_FILE = Path.home() / ".claude-usage-notify.env"
SEND_URL = "https://alldonesites.com/api/notify/send"
SUBJECT_PREFIX = "Claude usage tracker: "
KEYS = ("NOTIFY_SEND_SECRET", "NOTIFY_ALERT_TO", "NOTIFY_SEND_URL")

# (url, headers, body_bytes) -> (http status, response text). Raises OSError when no
# response came back at all (DNS, refused connection, timeout).
Poster = Callable[[str, Mapping[str, str], bytes], tuple[int, str]]


@dataclass(frozen=True)
class AlertConfig:
    to: str
    secret: str
    url: str = SEND_URL


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Comments and blank lines are ignored, and a key is only
    taken from a line that starts with it, so a comment naming the variable can never
    be read as its value (the same anchoring daily.sh's sed uses)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out.setdefault(key, value)
    return out


def alert_config(env_file: Path = ENV_FILE, environ: Mapping[str, str] | None = None,
                 to: str | None = None, url: str | None = None) -> AlertConfig | str:
    """Resolve where an alert goes. Returns an AlertConfig, or a one-line reason why the
    alert is not configured (the caller prints it and carries on)."""
    environ = os.environ if environ is None else environ
    values = read_env_file(env_file)
    for key in KEYS:
        if environ.get(key):
            values[key] = environ[key]
    secret = values.get("NOTIFY_SEND_SECRET", "")
    to = to or values.get("NOTIFY_ALERT_TO", "")
    if not secret:
        return f"no NOTIFY_SEND_SECRET in {env_file} or the environment"
    if not to:
        return f"no NOTIFY_ALERT_TO in {env_file} or the environment"
    return AlertConfig(to=to, secret=secret, url=url or values.get("NOTIFY_SEND_URL") or SEND_URL)


def _default_post(url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read().decode("utf-8", "replace")


def alert_body(subject: str, text: str, to: str, *, source: str, now: datetime) -> dict:
    """The JSON the send endpoint's admin mode takes. The subject is prefixed so the
    mail is recognisable in an inbox, and the text ends with where and when it came
    from, because a cron mail with no origin is a mystery a week later."""
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return {"to": to, "subject": SUBJECT_PREFIX + subject.strip(),
            "text": text.rstrip() + f"\n\n-- claude-usage-tracker on {source}, {stamp}"}


def send_alert(subject: str, text: str, cfg: AlertConfig, *, post: Poster = _default_post,
               source: str | None = None, now: datetime | None = None) -> tuple[int, str]:
    """POST one alert. Returns (http status, response text); raises OSError when the
    endpoint could not be reached at all. Never logs the secret."""
    body = alert_body(subject, text, cfg.to, source=source or socket.gethostname(),
                      now=now or datetime.now(timezone.utc))
    headers = {"authorization": f"Bearer {cfg.secret}", "content-type": "application/json"}
    return post(cfg.url, headers, json.dumps(body).encode("utf-8"))


def main(argv: list[str] | None = None, *, post: Poster = _default_post,
         environ: Mapping[str, str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Email Jonathan one alert via alldonesites.com")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--text", default=None, help="body text; read from stdin when omitted")
    ap.add_argument("--env-file", type=Path, default=ENV_FILE)
    ap.add_argument("--to", default=None, help="override NOTIFY_ALERT_TO")
    ap.add_argument("--endpoint", default=None, help="override NOTIFY_SEND_URL")
    ap.add_argument("--source", default=None, help="host name for the footer (default: this host)")
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.text is None else a.text

    cfg = alert_config(a.env_file, environ, to=a.to, url=a.endpoint)
    if isinstance(cfg, str):
        print(f"alert: skipped, {cfg}", file=sys.stderr)
        return 0
    try:
        status, reply = send_alert(a.subject, text, cfg, post=post, source=a.source)
    except OSError as e:
        print(f"warning: alert '{a.subject}' not sent: {e}", file=sys.stderr)
        return 1
    if 200 <= status < 300:
        print(f"alert: sent '{a.subject}' to {cfg.to} (HTTP {status})")
        return 0
    print(f"warning: alert '{a.subject}' refused: HTTP {status} {reply[:200]!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
