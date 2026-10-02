"""Fixtures for the installer tests: a throwaway checkout and a fake crontab.

The checkout is a real git repo on main with stub tracker/supervise.py and
tracker/health.py, so the installers' preflight passes and their post-deploy check
runs without touching the real tracker. Nothing here reads or writes a live crontab.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

FAKE_CRONTAB = """#!/bin/sh
f="$HOME/crontab.txt"
if [ "$1" = "-l" ]; then
  [ -f "$f" ] && cat "$f" || { echo "no crontab for $(id -un)" >&2; exit 1; }
else cat > "$f"; fi
"""

# Writes the state file the post-deploy check reads; SUPERVISE_EXIT sets its exit.
STUB_SUPERVISE = """import json
import os
import sys

a = sys.argv
json.dump({"last_exit": 0, "last_health": "ok", "last_ping": None}, open(a[a.index("--state") + 1], "w"))
sys.exit(int(os.environ.get("STUB_SUPERVISE_EXIT", "0")))
"""
STUB_HEALTH = 'print("ok")\n'


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "SKIP_RUFF": "1"}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def make_checkout(path: Path) -> Path:
    """A git checkout on main holding the stub tracker; returns its path."""
    (path / "tracker").mkdir(parents=True)
    (path / "tracker" / "__init__.py").write_text("")
    (path / "tracker" / "supervise.py").write_text(STUB_SUPERVISE)
    (path / "tracker" / "health.py").write_text(STUB_HEALTH)
    git(path, "init", "-q", "-b", "main")
    git(path, "add", ".")
    git(path, "commit", "-q", "--no-verify", "-m", "stub checkout")
    return path


def fake_crontab_bin(home: Path) -> Path:
    """A directory holding an executable `crontab` that keeps its table in $HOME/crontab.txt."""
    d = home / "fakebin"
    d.mkdir(exist_ok=True)
    f = d / "crontab"
    f.write_text(FAKE_CRONTAB)
    f.chmod(0o755)
    return d
