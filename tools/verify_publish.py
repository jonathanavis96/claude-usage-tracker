"""Rebuild the published claude-usage.json from committed files and compare it with the live one.

Anyone can check the public figures with one command:

    python3 tools/verify_publish.py

It runs the publisher exactly as the hourly cron on gs does (the `tracker.publish`
line in bin/daily.sh, same arguments, same code path), over the history and data
files committed in this checkout, at the published file's own `generated_at`. The
result is compared field by field with the published JSON, and every difference is
printed. Exit 0 means the two are identical apart from the ignored timestamp fields;
exit 1 means at least one figure differs; exit 2 means the check could not run.

The published file is read from the live site by default (the page itself fetches
`/data/claude-usage.json`), or from a local copy with `--published`. The rebuild
matches the live file only when this checkout holds the inputs of that publish:
the cron commits them to this repository as "Daily publisher state <hour>Z" in the
same run that pushes the JSON, so pull first, or check out that commit to verify an
older publish.

Nothing in the checkout is written. The publisher also recomputes the output class
weight and may rewrite the prices file it is given, so it is handed a temporary copy;
its alert sender is replaced with one that sends nothing.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tempfile
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIVE_URL = "https://alldonesites.com/data/claude-usage.json"
# Only fields that record when a file was written, not what it says. `generated_at`
# is the publish's own clock; the rebuild runs at that same instant, so every figure
# derived from it is still compared.
IGNORED = frozenset({"generated_at"})


def read_published(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        # The site refuses urllib's default user agent (HTTP 403); curl's is let through.
        req = urllib.request.Request(source, headers={"User-Agent": "claude-usage-tracker verify_publish"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return json.loads(Path(source).read_text(encoding="utf-8"))


def publisher_argv(root: Path, prices: Path, out: Path) -> list[str]:
    """The `tracker.publish` arguments bin/daily.sh passes, with prices and output redirected."""
    return [
        "--probes", str(root / "history/probes.jsonl"),
        "--passive", str(root / "history/passive.json"),
        "--contributed", str(root / "data/contributed.json"),
        "--gs-passive", str(root / "history/gs-passive.json"),
        "--effort", str(root / "data/effort_matrix.json"),
        "--masterrig-passive", str(root / "history/masterrig-passive.json"),
        "--prices", str(prices),
        "--alert-env-file", str(out.parent / "no-alert.env"),
        "--out", str(out),
    ]


def rebuild(root: Path, now: datetime) -> dict:
    """Run the cron's publisher over the files under `root`, at `now`, into a scratch file."""
    from tracker import publish

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prices = tmp / "prices.json"
        shutil.copyfile(root / "data/prices.json", prices)
        out = tmp / "claude-usage.json"
        with redirect_stdout(io.StringIO()):
            rc = publish.main(publisher_argv(root, prices, out), post=lambda *a, **k: None,
                              environ={}, now=now)
        if rc != 0:
            raise RuntimeError(f"tracker.publish exited {rc}")
        return json.loads(out.read_text(encoding="utf-8"))


def diff(published, rebuilt, path: str = "", ignored=IGNORED) -> list[str]:
    """Every place the two documents differ, one line each, in a stable order."""
    if isinstance(published, dict) and isinstance(rebuilt, dict):
        lines = []
        for key in sorted(set(published) | set(rebuilt)):
            if key in ignored:
                continue
            where = f"{path}.{key}" if path else key
            if key not in rebuilt:
                lines.append(f"{where}: published {json.dumps(published[key])[:200]}, missing from rebuild")
            elif key not in published:
                lines.append(f"{where}: missing from published, rebuilt {json.dumps(rebuilt[key])[:200]}")
            else:
                lines += diff(published[key], rebuilt[key], where, ignored)
        return lines
    if isinstance(published, list) and isinstance(rebuilt, list):
        lines = []
        if len(published) != len(rebuilt):
            lines.append(f"{path}: published {len(published)} items, rebuilt {len(rebuilt)}")
        for i, (p, r) in enumerate(zip(published, rebuilt)):
            lines += diff(p, r, f"{path}[{i}]", ignored)
        return lines
    # JSON does not tell 1 from 1.0 apart once written; neither does this.
    if published == rebuilt and isinstance(published, bool) == isinstance(rebuilt, bool):
        return []
    return [f"{path}: published {json.dumps(published)}, rebuilt {json.dumps(rebuilt)}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--published", default=LIVE_URL,
                    help=f"the published JSON: a URL or a local path (default {LIVE_URL})")
    ap.add_argument("--root", type=Path, default=ROOT,
                    help="checkout holding the committed history/ and data/ files (default: this one)")
    a = ap.parse_args(argv)
    try:
        published = read_published(a.published)
        stamp = published.get("generated_at") if isinstance(published, dict) else None
        if not stamp:
            raise ValueError("the published file carries no generated_at to rebuild at")
        now = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(timezone.utc)
        rebuilt = rebuild(a.root, now)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"verify_publish: could not run: {e}", file=sys.stderr)
        return 2
    lines = diff(published, rebuilt)
    for line in lines:
        print(line)
    if lines:
        print(f"{len(lines)} difference(s) between {a.published} (generated_at {stamp}) "
              f"and the rebuild from {a.root}")
        return 1
    print(f"identical: {a.published} (generated_at {stamp}) rebuilds exactly from {a.root}, "
          f"ignoring {', '.join(sorted(IGNORED))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
