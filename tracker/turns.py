"""Stream Claude Code transcripts into deduplicated per-turn token usage."""
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Turn:
    ts: datetime
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iter_turns(paths: Iterable[Path]) -> Iterator[Turn]:
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
                yield Turn(_parse_ts(d["timestamp"]), m.get("model") or "unknown",
                           int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                           int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0))


def transcript_paths(root: Path, since: datetime | None) -> list[Path]:
    out = []
    for p in Path(root).rglob("*.jsonl"):
        if since is None or datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= since:
            out.append(p)
    return sorted(out)
