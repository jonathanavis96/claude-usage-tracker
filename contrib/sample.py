#!/usr/bin/env python3
"""Contribute one sample of your own Claude usage meter to alldonesites.com/claude-usage-tracker.

Self-contained, Python 3 stdlib only. It is meant to be fetched and run with:

    python3 -c "$(curl -fsSL https://raw.githubusercontent.com/jonathanavis96/claude-usage-tracker/main/contrib/sample.py)" --print

What it reads, all from this machine and nothing else:

  * your Claude Code OAuth token, from `$CLAUDE_CONFIG_DIR/.credentials.json`
    or `~/.claude/.credentials.json`, to call the same usage endpoint Claude
    Code itself calls (https://api.anthropic.com/api/oauth/usage);
  * token COUNTS from the `usage` field of assistant turns in your Claude Code
    transcripts (`<config dir>/projects/**/*.jsonl`), summed per model and token
    class since the current five-hour reset and since the current seven-day reset.

What it sends: the JSON body it prints. Never prompt text, file paths, project
names, session ids, account ids or email. The token is used for the usage call
and is never printed or sent anywhere else.

First run (no id file yet) prints the body and asks before posting. `--dry-run`
never sends. `--compact` prints only the `CUT1:` line for pasting into the page
and never sends. `--yes` posts without asking (for cron).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

CLIENT_VERSION = "contrib-sample/0.1.0"
DEFAULT_ENDPOINT = "https://alldonesites.com/api/contribute"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ID_FILE = Path.home() / ".claude-usage-contrib.json"
PLANS = ("pro", "max5", "max20")
MAX_BODY_BYTES = 2048
POST_TIMEOUT_S = 10
USAGE_TIMEOUT_S = 30

# Copied from tracker/turns.py so this file has no imports from the tracker.
CANONICAL_MODELS = {"claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"}
_DATE_SUFFIX = re.compile(r"-\d{8}$")
_1M_MARKER = re.compile(r"\s*\[1m\]$")
CLASSES = ("input", "output", "cache_read", "cache_write")

# Keys the usage endpoint might use for the subscription plan. Nobody has seen
# one yet (the tracker's fixtures show only five_hour / seven_day / seven_day_*),
# so the script checks and records where the plan came from (`plan_source`).
PLAN_KEYS = ("plan", "plan_type", "tier", "subscription", "subscription_type", "rate_limit_tier")

FIELD_HELP = {
    "contributor_id": "A random UUID made on this machine on the first run and kept in the id file "
                      "so later samples from you can be paired. It is not linked to your account.",
    "plan": "Your subscription plan (pro, max5, max20). Read from the usage endpoint if it exposes "
            "one, otherwise the value you gave with --plan, stored in the id file.",
    "plan_source": "Where `plan` came from: endpoint, stored (id file) or flag (--plan on this run).",
    "ts": "This machine's clock at the time of the sample, UTC.",
    "five_hour": "Your five-hour meter as the usage endpoint reports it: utilization in percent and "
                 "the time the window resets. Same numbers Claude Code shows you.",
    "seven_day": "Your seven-day meter, same shape as five_hour.",
    "tokens_since_five_hour_reset": "Token counts per model and class (input, output, cache_read, "
                                    "cache_write) summed from your transcripts' usage fields for turns "
                                    "since the current five-hour window started. Counts only.",
    "tokens_since_seven_day_reset": "Same sums for turns since the current seven-day window started.",
    "client_version": "The version string of this script, so the server can tell samples apart.",
}


# ---------------------------------------------------------------- credentials

def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def read_token(cfg: Path) -> str:
    """The OAuth access token, the way tracker/usage_api.py reads it.

    On macOS Claude Code keeps the same JSON in the login keychain instead of a
    file, so fall back to `security find-generic-password` there.
    """
    creds = cfg / ".credentials.json"
    if creds.exists():
        return json.loads(creds.read_text())["claudeAiOauth"]["accessToken"]
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)["claudeAiOauth"]["accessToken"]
    raise FileNotFoundError(f"no Claude Code login found at {creds} (set CLAUDE_CONFIG_DIR if it lives elsewhere)")


def fetch_usage(token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
    req = urllib.request.Request(USAGE_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=USAGE_TIMEOUT_S) as r:
        return json.loads(r.read().decode())


def _bucket(body: dict, key: str) -> dict:
    b = body.get(key) or {}
    u = b.get("utilization")
    return {"utilization": float(u) if u is not None else None, "resets_at": b.get("resets_at")}


def endpoint_plan(body: dict) -> str | None:
    """The plan if the usage response carries one under any known key, else None."""
    for key in PLAN_KEYS:
        v = body.get(key)
        if isinstance(v, dict):
            v = v.get("name") or v.get("type") or v.get("id")
        if isinstance(v, str) and v.strip():
            return normalize_plan(v)
    return None


def normalize_plan(raw: str) -> str:
    s = raw.strip().lower()
    if s in PLANS:
        return s
    compact = re.sub(r"[^a-z0-9]", "", s)
    if "max" in compact and "20" in compact:
        return "max20"
    if "max" in compact and "5" in compact:
        return "max5"
    if "pro" in compact:
        return "pro"
    return s[:32]


# ---------------------------------------------------------------- transcripts

def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_model(model_id: str) -> str | None:
    m = _1M_MARKER.sub("", model_id)
    m = _DATE_SUFFIX.sub("", m)
    return m if m in CANONICAL_MODELS else None


def iter_turns(paths: Iterable[Path]) -> Iterator[tuple[datetime, str, dict[str, int]]]:
    """(timestamp, raw model id, {class: count}) per assistant turn, deduplicated by message id."""
    seen: set[str] = set()
    for p in paths:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                mid = m.get("id")
                if not isinstance(u, dict) or not mid or mid in seen or not d.get("timestamp"):
                    continue
                seen.add(mid)
                yield (_parse_ts(d["timestamp"]), m.get("model") or "unknown", {
                    "input": int(u.get("input_tokens") or 0),
                    "output": int(u.get("output_tokens") or 0),
                    "cache_read": int(u.get("cache_read_input_tokens") or 0),
                    "cache_write": int(u.get("cache_creation_input_tokens") or 0),
                })


def transcript_paths(root: Path, since: datetime | None) -> list[Path]:
    if not root.is_dir():
        return []
    out = []
    for p in root.rglob("*.jsonl"):
        if since is None or datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= since:
            out.append(p)
    return sorted(out)


def window_start(resets_at: str | None, length: timedelta) -> datetime | None:
    if not resets_at:
        return None
    try:
        return _parse_ts(resets_at) - length
    except ValueError:
        return None


def tokens_since(turns: list[tuple[datetime, str, dict[str, int]]], start: datetime | None,
                 now: datetime) -> dict[str, dict[str, int]]:
    """Sum token counts per normalized model and class for turns in [start, now]."""
    out: dict[str, dict[str, int]] = {}
    if start is None:
        return out
    for ts, raw_model, counts in turns:
        if not (start <= ts <= now):
            continue
        model = normalize_model(raw_model)
        if model is None:
            continue
        acc = out.setdefault(model, {c: 0 for c in CLASSES})
        for c in CLASSES:
            acc[c] += counts[c]
    return {m: out[m] for m in sorted(out)}


# ---------------------------------------------------------------- id file

def load_id_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return d if isinstance(d, dict) and d.get("contributor_id") else None


def save_id_file(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- body

def build_body(contributor_id: str, plan: str, plan_source: str, usage: dict, turns: list, now: datetime) -> dict:
    fh, sd = _bucket(usage, "five_hour"), _bucket(usage, "seven_day")
    return {
        "contributor_id": contributor_id,
        "plan": plan,
        "plan_source": plan_source,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "five_hour": fh,
        "seven_day": sd,
        "tokens_since_five_hour_reset": tokens_since(turns, window_start(fh["resets_at"], timedelta(hours=5)), now),
        "tokens_since_seven_day_reset": tokens_since(turns, window_start(sd["resets_at"], timedelta(days=7)), now),
        "client_version": CLIENT_VERSION,
    }


def minify(body: dict) -> str:
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def compact_line(body: dict) -> str:
    return "CUT1:" + base64.urlsafe_b64encode(minify(body).encode()).decode()


def decode_compact_line(line: str) -> dict:
    if not line.startswith("CUT1:"):
        raise ValueError("not a CUT1 line")
    return json.loads(base64.urlsafe_b64decode(line[5:].encode()).decode())


def explain(body: dict) -> str:
    return "\n".join(f"  {k}: {FIELD_HELP.get(k, '')}" for k in body)


def post(endpoint: str, body: dict) -> tuple[int, dict | None]:
    """POST the body once, 10 s timeout, no retries. Returns (status, parsed response or None)."""
    data = minify(body).encode()
    req = urllib.request.Request(endpoint, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": CLIENT_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT_S) as r:
            status = r.status
            raw = r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode(errors="replace") if e.fp else ""
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None
    return status, parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------- main

def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="sample.py", description="Send one sample of your Claude usage meter.")
    ap.add_argument("--plan", choices=PLANS, help="your plan; required on first run unless the usage endpoint exposes it")
    ap.add_argument("--print", dest="print_body", action="store_true",
                    help="print the exact body with an explanation and ask before sending (default on first run)")
    ap.add_argument("--yes", action="store_true", help="send without asking")
    ap.add_argument("--dry-run", action="store_true", help="never send")
    ap.add_argument("--compact", action="store_true", help="print only the CUT1: line for pasting into the page; never sends")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--id-file", type=Path, default=ID_FILE, help=f"where the contributor id and plan live (default {ID_FILE})")
    ap.add_argument("--claude-dir", type=Path, default=None,
                    help="Claude Code config dir (default $CLAUDE_CONFIG_DIR or ~/.claude)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, usage_fetch=None, now: datetime | None = None,
         ask=None, out=None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    now = now or datetime.now(timezone.utc)
    cfg = a.claude_dir or config_dir()

    stored = load_id_file(a.id_file)
    first_run = stored is None
    if first_run:
        stored = {"contributor_id": str(uuid.uuid4()), "created": now.strftime("%Y-%m-%dT%H:%M:%SZ")}

    try:
        usage = (usage_fetch or (lambda: fetch_usage(read_token(cfg))))()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"error: could not read the usage endpoint: {e}", file=sys.stderr)
        return 2

    plan = endpoint_plan(usage)
    plan_source = "endpoint"
    if plan is None and a.plan:
        plan, plan_source = a.plan, "flag"
    if plan is None and stored.get("plan"):
        plan, plan_source = stored["plan"], "stored"
    if plan is None:
        print("error: the usage endpoint does not expose your plan; pass --plan pro|max5|max20 once, "
              "it is stored in the id file for later runs", file=sys.stderr)
        return 2
    stored["plan"] = plan
    stored["plan_from_endpoint"] = plan_source == "endpoint"
    save_id_file(a.id_file, stored)

    fh, sd = _bucket(usage, "five_hour"), _bucket(usage, "seven_day")
    since = window_start(sd["resets_at"], timedelta(days=7)) or window_start(fh["resets_at"], timedelta(hours=5))
    turns = list(iter_turns(transcript_paths(cfg / "projects", since)))
    body = build_body(stored["contributor_id"], plan, plan_source, usage, turns, now)

    encoded = minify(body)
    if len(encoded.encode()) > MAX_BODY_BYTES:
        print(f"error: body is {len(encoded.encode())} bytes, over the {MAX_BODY_BYTES} byte limit; not sending",
              file=sys.stderr)
        return 3

    if a.compact:
        print(compact_line(body), file=out)
        return 0

    show = a.print_body or first_run
    if show:
        print("This is the exact body the script would send:\n", file=out)
        print(json.dumps(body, indent=2, sort_keys=True), file=out)
        print("\nWhat each field is:\n" + explain(body), file=out)
        print(f"\nContributor id and plan are stored in {a.id_file} (nothing else is written).", file=out)
        print("\nSame body as one line, for pasting into the page instead of sending from here:\n", file=out)
        print(compact_line(body), file=out)
        print("", file=out)

    if a.dry_run:
        print("dry run: not sending.", file=out)
        return 0

    if not a.yes:
        if ask is None:
            if not sys.stdin.isatty():
                print("not sending: no terminal to ask on (use --yes to send, --dry-run to only look).", file=out)
                return 0
            ask = lambda prompt: input(prompt)  # noqa: E731
        if ask("Send? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not sent.", file=out)
            return 0

    try:
        status, resp = post(a.endpoint, body)
    except (urllib.error.URLError, OSError) as e:
        print(f"error: POST {a.endpoint} failed: {e}", file=sys.stderr)
        return 4
    print(f"POST {a.endpoint}: HTTP {status}", file=out)
    if resp and resp.get("me_url"):
        print(f"your page: {resp['me_url']}", file=out)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
