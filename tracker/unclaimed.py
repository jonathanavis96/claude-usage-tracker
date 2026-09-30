"""Whose meter an unclaimed pooled-root transcript spent on, where a durable record says.

On gs, `~/.claude`, `~/.claude-javiswork`, `~/.claude-avis` and `~/.claude-jono` share one
`projects` directory, and a transcript belongs to the config dir whose `session-env` holds
its session (tracker/gs_passive.py `transcript_files`). Claude Code writes that entry only
for a session that starts a shell. Headless `claude -p` runs start none, so no config dir
claims them. On 2026-09-30 every unclaimed transcript in the measured span (14 to 30
September) was one (`entrypoint: sdk-cli`), from three launchers:

- the airlock bench's tuning judge (`~/.local/share/airlock`, a systemd timer), whose
  `~/.config/airlock/tune.env` names the config dir it bills;
- the auto-mail filing judge (`/tmp/filing-judge-*`, `/tmp/judge-*` working directories),
  run from cron;
- this tracker's own probe, whose runs history/harness-runs.jsonl records per account.

Each rule below reads the record that says which config dir a launcher used, and applies
only where that record covers the run. A transcript no rule covers is left unclaimed, and
tracker/credits.py `unclaimed_shares` fits how much of it each account carried.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: The auto-mail commit that made every `claude -p` call run on a seat picked by usage
#: (auto-mail 510c222, "Every claude call runs on a seat picked by usage", #45). Before it
#: nothing in auto-mail set `CLAUDE_CONFIG_DIR`, and its crontab entries set none, so a judge
#: ran on the default `~/.claude` login. After it the seat is resolved per run and not
#: written down anywhere, so those runs have no record here.
AUTO_MAIL_SEAT_PICKER_FROM = datetime(2026, 9, 21, 21, 44, 13, tzinfo=timezone.utc)
#: The config dir a `claude -p` run uses when nothing sets `CLAUDE_CONFIG_DIR`.
DEFAULT_CONFIG_DIR = ".claude"
AUTO_MAIL_CWD_PREFIXES = ("/tmp/filing-judge-", "/tmp/judge-")
AIRLOCK_TUNE_ENV = Path(".config") / "airlock" / "tune.env"
AIRLOCK_RELEASES = Path(".local") / "share" / "airlock" / "releases"
TRACKER_CHECKOUT_NAME = "claude-usage-tracker"

_WRITTEN = re.compile(r"on (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")


@dataclass(frozen=True)
class Attribution:
    config_dir: str
    rule: str


def first_record(path: Path) -> tuple[datetime | None, str | None]:
    """(the first timestamp, the working directory) a transcript records."""
    ts = cwd = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            if ts is None and d.get("timestamp"):
                ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
            cwd = cwd or d.get("cwd")
            if ts is not None and cwd:
                break
    return ts, cwd


def airlock_record(home: Path) -> tuple[str, datetime] | None:
    """(the config dir airlock's tuning judge bills, from when), per its `tune.env`.

    The file names the dir in `AIRLOCK_TUNE_CLAUDE_CONFIG_DIR` (unset means `~/.claude`)
    and was written by airlock's installer with a "Written by ... on <UTC>" line; the file's
    own time stands in for that line if it is missing. Only runs after that time are
    covered: the file says nothing about what an earlier install named.
    """
    path = home / AIRLOCK_TUNE_ENV
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    written = _WRITTEN.search(text)
    since = (datetime.fromisoformat(written.group(1).replace("Z", "+00:00")) if written
             else datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    config_dir = DEFAULT_CONFIG_DIR
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("AIRLOCK_TUNE_CLAUDE_CONFIG_DIR="):
            config_dir = Path(line.split("=", 1)[1].strip().strip('"\'')).name or DEFAULT_CONFIG_DIR
    return config_dir, since


def attribute(path: Path, home: Path, harness: Sequence[tuple[str, datetime, datetime]] = (),
              airlock: tuple[str, datetime] | None = None) -> Attribution | None:
    """The config dir an unclaimed transcript spent on, where a record says; else None.

    `harness` is (config dir, start, end) for each of the tracker's own runs; `airlock` is
    `airlock_record(home)`.
    """
    ts, cwd = first_record(path)
    if ts is None or not cwd:
        return None
    if Path(cwd).name == TRACKER_CHECKOUT_NAME:
        for config_dir, start, end in harness:
            if start <= ts <= end:
                return Attribution(config_dir, "harness_run")
        return None
    releases = str(home / AIRLOCK_RELEASES)
    if cwd == releases or cwd.startswith(releases + "/"):
        if airlock is not None and ts >= airlock[1]:
            return Attribution(airlock[0], "airlock_tune_env")
        return None
    if cwd.startswith(AUTO_MAIL_CWD_PREFIXES):
        if ts < AUTO_MAIL_SEAT_PICKER_FROM:
            return Attribution(DEFAULT_CONFIG_DIR, "auto_mail_default_login")
        return None
    return None
