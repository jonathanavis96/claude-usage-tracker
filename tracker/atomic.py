"""Replace a file's contents atomically.

Every writer here goes through a temporary file in the same directory and an
os.replace, so a killed run never leaves a truncated file. The temporary file is
uniquely named: a fixed `<stem>.tmp` let two overlapping runs (cron ticks, a
hand run beside the timer) write into one temp file and publish a torn one, or
fail when the other had already moved it.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # mkstemp makes the file 0600; keep the mode the target had (or a plain 0644).
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
