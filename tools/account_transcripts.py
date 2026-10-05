"""The gs accounts' transcripts read the way the collector reads them, kept per file.

Read-only over `~/.claude*`: it opens transcripts and session-env listings and writes only
a cache under --cache. Used by tools/account_agreement.py for the transcript checks in
docs/findings/2026-10-04-account-agreement.md.

For each gs account (tracker/gs_passive.py `gs_accounts`), from 2026-09-14:

- `own`: the transcripts `_split_transcripts` keeps, read through one `seen` set exactly as
  `report` does (`iter_turns`), each turn tagged with the file it came from;
- `unclaimed`: the pooled root's transcripts no config dir claims, read the same way;
- `raw`: every own or unclaimed file read with its own `seen` set, so a message id that
  occurs in two files (inside one account or across two) shows up twice.
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tracker import gs_passive as G
from tracker.turns import Turn, _json_lines, turns_in

SINCE = datetime(2026, 9, 14, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Tagged:
    turn: Turn
    path: str


def _read(paths: list[Path], seen: set[str] | None) -> list[Tagged]:
    out = []
    for p in paths:
        try:
            fh = open(p, "r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        with fh:
            own_seen = seen if seen is not None else set()
            out.extend(Tagged(t, str(p)) for t in turns_in(_json_lines(fh), own_seen))
    return out


def load(since: datetime = SINCE) -> dict[str, dict]:
    """Per account: own and unclaimed turns (collector dedup), and raw per-file turns."""
    out = {}
    for name, account in G.gs_accounts().items():
        own_files, meta, unclaimed_files = G._split_transcripts(account, since)
        out[name] = {
            "meta": meta,
            "own_files": [str(p) for p in own_files],
            "unclaimed_files": [str(p) for p in unclaimed_files],
            "own": _read(own_files, set()),
            "unclaimed": _read(unclaimed_files, set()),
            "raw": _read(own_files + unclaimed_files, None),
        }
    return out


def cached(cache: Path, since: datetime = SINCE) -> dict[str, dict]:
    """`load`, kept in a local pickle this tool itself wrote (never one from elsewhere)."""
    if cache.exists():
        return pickle.loads(cache.read_bytes())
    data = load(since)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps(data))
    return data


def entrypoint(path: str, max_lines: int = 200) -> str | None:
    """The `entrypoint` the transcript's first lines record (`cli`, `sdk-cli`, ...), or None."""
    import json
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                if '"entrypoint"' not in line:
                    continue
                try:
                    return json.loads(line).get("entrypoint")
                except ValueError:
                    continue
    except FileNotFoundError:
        return None
    return None


def delegate_runs(root: Path = Path.home() / "var" / "delegates") -> list[dict]:
    """Each `dave-delegate` run's own total, from the stream-json `result` event it ends with.

    `modelUsage` is Claude Code's per-model count for the whole run (every request it made,
    the run's own and any side calls), independent of the session transcript.
    """
    import json
    out = []
    for p in sorted(root.glob("*.jsonl")):
        result = None
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type":"result"' in line or '"type": "result"' in line:
                    try:
                        result = json.loads(line)
                    except ValueError:
                        pass
        if result and result.get("session_id"):
            out.append({"file": str(p), "session_id": result["session_id"],
                        "model_usage": result.get("modelUsage") or {},
                        "cost_usd": result.get("total_cost_usd"), "num_turns": result.get("num_turns")})
    return out


def session_transcripts(session_id: str) -> list[Path]:
    """Every transcript file of a session (main file and sub-agent files) on any config dir."""
    out = []
    for cfg in sorted(Path.home().glob(".claude*")):
        proj = cfg / "projects"
        if not proj.is_dir() or proj.is_symlink():
            continue
        out += list(proj.glob(f"*/{session_id}.jsonl")) + list(proj.glob(f"*/{session_id}/subagents/*.jsonl"))
    return out
