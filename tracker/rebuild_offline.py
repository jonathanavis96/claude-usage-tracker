"""Rebuild a reviewable public snapshot from files, without network or price writes.

Unlike the scheduled publisher, this command never samples an account, recalibrates
stored prices, fetches contributors, sends an alert, or invokes Git. Existing raw
history is read-only. It cannot restore reset or capture evidence absent from it.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .calibrate import recompute
from .contributed import aggregate, load_history
from .publish import build_public_json, load_probes, write_json


def rebuild(root: Path, now: datetime) -> dict:
    """Compute a public snapshot using only archived inputs below ``root``."""
    def read(relative: str, default=None):
        path = root / relative
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    raw_prices = read("data/prices.json")
    if not isinstance(raw_prices, dict):
        raise TypeError("data/prices.json must contain a price table")
    prices = {k: v for k, v in raw_prices.items() if not k.startswith("_")}
    matrix = read("data/effort_matrix.json", {})
    if matrix.get("_status") == "placeholder":
        raise ValueError("effort matrix is still a placeholder")
    if matrix.get("_meta", {}).get("runs"):
        matrix = recompute(matrix, prices)
    effort = {k: v for k, v in matrix.items() if not k.startswith("_") and k != "usd"}
    effort_usd = {k: v for k, v in matrix.get("usd", {}).items() if not k.startswith("_")}
    result = build_public_json(
        load_probes(root / "history/probes.jsonl"),
        read("history/passive.json", {}), effort, prices, now,
        effort_usd=effort_usd, gs_passive=read("history/gs-passive.json", {}),
    )
    result["contributed"] = aggregate(load_history(root / "history/contributed.jsonl"), now, prices)
    result["rebuild"] = {
        "mode": "offline_archive",
        "note": "Recomputed from stored evidence; missing reset and capture metadata cannot be recovered.",
    }
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("."), help="collector checkout containing data/ and history/")
    ap.add_argument("--now", required=True, help="explicit as-of time including UTC offset")
    ap.add_argument("--out", type=Path, required=True, help="review output; may not replace an input")
    args = ap.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("--now requires a UTC offset")
        output = args.out.resolve()
        for folder in (args.root / "history", args.root / "data"):
            if output.is_relative_to(folder.resolve()):
                raise ValueError("--out must not overwrite archived data or history")
        result = rebuild(args.root, now.astimezone(timezone.utc))
        write_json(output, result)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        ap.exit(1, f"offline rebuild failed: {exc}\n")
    print(f"Wrote offline review snapshot: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
