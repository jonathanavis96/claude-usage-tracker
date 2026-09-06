"""Stream Claude Code transcripts into deduplicated per-turn token usage."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Iterable, Iterator

CANONICAL_MODELS = {"claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"}
_DATE_SUFFIX = re.compile(r"-\d{8}$")
_1M_MARKER = re.compile(r"\s*\[1m\]$")


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


def normalize_model(model_id: str) -> str | None:
    """Strip a date suffix (-YYYYMMDD) or a [1m] marker and map to one of the
    three current model ids. Anything that still doesn't match one of those
    three after stripping (an older version, a haiku variant, `<synthetic>`)
    is dropped -- returns None.
    """
    m = _1M_MARKER.sub("", model_id)
    m = _DATE_SUFFIX.sub("", m)
    return m if m in CANONICAL_MODELS else None


def session_tokens_by_model(paths: Iterable[Path], now: datetime | None = None,
                            window: timedelta = timedelta(days=30)) -> dict[str, int]:
    """Median cumulative tokens of one session, per model, over the last `window`.

    One transcript file (subagent transcripts included, each counted as its
    own session) is one session. A session's tokens are the sum of every
    assistant turn's total tokens in that file; the session is assigned to
    whichever raw model id spent the most tokens in it, then that model id
    is normalized (see normalize_model) -- sessions whose top model doesn't
    normalize to one of the three current models are dropped. Only sessions
    with at least 2 turns, and whose last turn falls within `window` of
    `now`, count.
    """
    now = now or datetime.now(timezone.utc)
    since = now - window
    totals: dict[str, list[int]] = {}
    for p in paths:
        turns = list(iter_turns([p]))
        if len(turns) < 2:
            continue
        session_ts = max(t.ts for t in turns)
        if not (since <= session_ts <= now):
            continue
        by_model: dict[str, int] = {}
        for t in turns:
            by_model[t.model] = by_model.get(t.model, 0) + t.total
        top_model = max(by_model, key=lambda m: by_model[m])
        model = normalize_model(top_model)
        if model is None:
            continue
        totals.setdefault(model, []).append(sum(t.total for t in turns))
    return {model: round(median(values)) for model, values in totals.items()}


def transcript_paths(root: Path, since: datetime | None) -> list[Path]:
    out = []
    for p in Path(root).rglob("*.jsonl"):
        if since is None or datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= since:
            out.append(p)
    return sorted(out)
