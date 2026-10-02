"""When an account's Claude Code cloud sessions ran, from the records the cloud tooling keeps.

A cloud session (`claude --cloud`) runs in Anthropic's cloud on the launching account, so it
moves that account's meter with no transcript on the host -- unless it is later copied back
with `claude --teleport`, when its turns land in the local transcripts marked
`remoteSourced` (tracker/turns.py `Turn.remote`). Neither is ordinary use of the host, so a
stretch that holds any of it is left out of every measurement (issue #133): its tokens are
either missing or were spent somewhere the meter-and-transcript join does not describe.

The durable record is the cloud tooling's own launch log. Both hosts' `~/bin/cloud-build`
append one row per launch to `~/private/cloud/sessions.tsv`: the UTC launch time, what was
built (gs: the worktree; masterrig: the repo and the task, two columns), the base commit,
the `session_...` id, and the brief. `cloud-grab` copies a finished session's teleported
transcript to `j-<what>.jsonl` beside it (gs also `j-<what>-<six characters of the
session id>.jsonl` when one worktree ran more than one). A span runs from the earlier of
the launch and the transcript's first cloud turn to its last cloud turn. A launch whose
transcript was never grabbed is known to have been running at its launch instant and at no
other known time, so its span is that instant: nothing here guesses an end.

Which account ran a launch. masterrig has one login, so every row is its own. gs has four,
and its log does not say which one `cloud-build` used (`CLAUDE_REVIEW_ACCOUNT`, default
`~/.claude-javiswork`); but `cloud-grab` teleports under `~/.claude-javiswork` alone, and a
session can only be teleported by the account that owns it. So on gs a row is that
account's only when its grabbed transcript exists (`require_grab`), and the other rows are
counted and attributed to nobody. Their work, if it was ever teleported, is still caught by
the `remoteSourced` turns.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .turns import parse_ts_or_none

#: How much later than its first cloud turn a launch may be logged. `cloud-build` writes
#: its row after `claude --cloud` returns, which it allows up to 240 seconds.
LAUNCH_LOG_LAG_S = 300

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class CloudSpan:
    session: str
    start: datetime
    end: datetime
    #: "transcript" when the span's end is the grabbed transcript's last cloud turn,
    #: "launch" when only the launch instant is known.
    source: str

    def record(self) -> dict:
        return {"session": self.session, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "source": self.source}


def launches(log: Path) -> list[dict]:
    """The rows of a `sessions.tsv`: launch time, the name its transcript is filed under, session id."""
    out = []
    if not log.exists():
        return out
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        cols = line.split("\t")
        sha = next((i for i, c in enumerate(cols) if _SHA.match(c)), None)
        session = next((c for c in cols if c.startswith("session_")), None)
        if sha is None or sha < 2 or not session:
            continue
        at = parse_ts_or_none(cols[0])
        if at is None:
            continue
        out.append({"at": at, "name": "-".join(cols[1:sha]), "session": session})
    return out


def _cloud_turn_times(path: Path) -> list[datetime]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = parse_ts_or_none(d.get("timestamp")) if isinstance(d, dict) and d.get("remoteSourced") else None
            if at is not None:
                out.append(at)
    return out


def cloud_spans(cloud_dir: Path | None, require_grab: bool = False) -> tuple[list[CloudSpan], int]:
    """(this account's cloud-session spans, the launches left unattributed), from `cloud_dir`.

    Each grabbed transcript `j-<name>[-<sid6>].jsonl` is matched to its launch: the row
    with that session id prefix, else the latest row for `<name>` logged no more than
    LAUNCH_LOG_LAG_S after the transcript's first cloud turn.
    """
    if cloud_dir is None:
        return [], 0
    rows = launches(Path(cloud_dir) / "sessions.tsv")
    grabbed: dict[str, list[datetime]] = {}
    for path in sorted(Path(cloud_dir).glob("j-*.jsonl")):
        times = _cloud_turn_times(path)
        if not times:
            continue
        stem = path.name[2:-len(".jsonl")]
        by_id = [r for r in rows if stem.endswith("-" + r["session"][len("session_"):][:6])
                 and stem[:-7] == r["name"]]
        by_name = [r for r in rows if r["name"] == stem
                   and (r["at"] - min(times)).total_seconds() <= LAUNCH_LOG_LAG_S]
        match = by_id or sorted(by_name, key=lambda r: r["at"])[-1:]
        if match:
            grabbed.setdefault(match[0]["session"], []).extend(times)
    spans, unattributed = [], 0
    for r in rows:
        times = grabbed.get(r["session"])
        if times:
            spans.append(CloudSpan(r["session"], min(min(times), r["at"]), max(times), "transcript"))
        elif require_grab:
            unattributed += 1
        else:
            spans.append(CloudSpan(r["session"], r["at"], r["at"], "launch"))
    return sorted(spans, key=lambda s: s.start), unattributed


def overlaps(spans: list[CloudSpan], start: datetime, end: datetime) -> bool:
    """Whether any span touches [start, end]."""
    return any(s.start <= end and s.end >= start for s in spans)
