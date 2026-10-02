"""One-off: put back history/passive.json days lost when Claude Code deleted old transcripts.

    python3 tools/restore_passive_days.py            # merge into history/passive.json
    python3 tools/restore_passive_days.py --check    # print what would be added, write nothing

The days 2026-07-30 to 2026-08-19 survive only in old commits of history/passive.json:
87aa061 (2026-09-05, covers 2026-07-30 onward) and 9e40788 (2026-09-19, covers 2026-08-11
onward, recounted with more transcripts, so it wins where both have a day). A day already in
the current file is never overwritten. Idempotent: a second run adds nothing. Once restored,
tracker.passive keeps these days on every rebuild (they are older than any transcript left).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("87aa061", "9e40788")  # oldest first; a later source wins for a day both hold
FIRST, LAST = "2026-07-30", "2026-08-19"


def recovered_days(repo: Path = ROOT) -> dict[str, dict]:
    days: dict[str, dict] = {}
    for rev in SOURCES:
        doc = json.loads(subprocess.run(["git", "-C", str(repo), "show", f"{rev}:history/passive.json"],
                                        capture_output=True, text=True, check=True).stdout)
        for ds, row in (doc.get("history") or {}).items():
            if FIRST <= ds <= LAST and isinstance(row, dict) and isinstance(row.get("tokens_per_pct"), (int, float)):
                days[ds] = {"tokens_per_pct": row["tokens_per_pct"], "interpolated": bool(row.get("interpolated"))}
    return days


def merge(current: dict, recovered: dict[str, dict]) -> tuple[dict, list[str]]:
    hist = dict(current.get("history") or {})
    added = sorted(ds for ds in recovered if ds not in hist)
    for ds in added:
        hist[ds] = recovered[ds]
    return {**current, "history": dict(sorted(hist.items()))}, added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", type=Path, default=ROOT / "history" / "passive.json")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    current = json.loads(a.file.read_text(encoding="utf-8"))
    merged, added = merge(current, recovered_days())
    print(f"{len(added)} days to add: {added[0]} .. {added[-1]}" if added else "nothing to add")
    if added and not a.check:
        sys.path.insert(0, str(ROOT))
        from tracker.atomic import write_text_atomic
        write_text_atomic(a.file, json.dumps(merged, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
