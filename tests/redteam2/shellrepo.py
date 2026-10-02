"""Throwaway tracker checkouts for the shell-script red-team tests.

Each checkout holds the REAL bin/passive.sh or bin/daily.sh, with stub tracker modules
that only write their output files, and a local bare repository as `origin` (plus a bare
"site" repository for daily.sh). HOME is the test's temp dir, so nothing reads
~/.claude, the live checkout, GitHub or the network.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Writes a fresh value to the --out / --history file it is given, like the real joins.
OUT_STUB = """import pathlib, sys, time
flag = "--out" if "--out" in sys.argv else ("--history" if "--history" in sys.argv else "--json")
out = pathlib.Path(sys.argv[sys.argv.index(flag) + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text('{"generated_at": "%s"}\\n' % time.time_ns())
"""
NOOP_STUB = "raise SystemExit(0)\n"
RATES_STUB = """import datetime, json, pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index("--json") + 1])
out.write_text(json.dumps({"_meta": {"generated_at": datetime.datetime.now().astimezone().isoformat()}}))
"""


def env_for(home: Path) -> dict:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd: Path, *args: str, env: dict, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, capture_output=True,
                          text=True).stdout.strip()


def seed_bare(origin: Path, seed: Path, env: dict) -> None:
    """Commit everything in `seed`, push it to a new bare `origin` and track origin/main."""
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=env, check=True)
    git(seed, "init", "-q", "-b", "main", env=env)
    git(seed, "add", ".", env=env)
    git(seed, "commit", "-q", "-m", "init", env=env)
    git(seed, "remote", "add", "origin", str(origin), env=env)
    git(seed, "push", "-q", "origin", "main", env=env)
    git(seed, "branch", "-q", "--set-upstream-to=origin/main", env=env)


def masterrig_checkout(tmp_path: Path, home: Path) -> tuple[Path, Path, dict]:
    """A masterrig-shaped checkout: real bin/passive.sh, stub joins, bare origin."""
    env = env_for(home)
    work = tmp_path / "masterrig"
    (work / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "passive.sh", work / "bin" / "passive.sh")
    (work / "tracker").mkdir()
    (work / "tracker" / "__init__.py").write_text("")
    for mod in ("passive", "gs_passive", "speed"):
        (work / "tracker" / f"{mod}.py").write_text(OUT_STUB)
    (work / "history").mkdir()
    (work / "history" / "passive.json").write_text('{"history": {"2026-07-30": {"tokens_per_pct": 5}}}\n')
    (work / ".gitignore").write_text(".passive-last-ok\n__pycache__/\n")
    origin = tmp_path / "tracker-origin.git"
    seed_bare(origin, work, env)
    return work, origin, env


def gs_checkout(tmp_path: Path, home: Path) -> tuple[Path, Path, Path, dict]:
    """A gs-shaped checkout: real bin/daily.sh, stub modules, bare tracker and site origins."""
    env = env_for(home)
    work = tmp_path / "gs"
    (work / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "daily.sh", work / "bin" / "daily.sh")
    (work / "tracker").mkdir()
    (work / "tracker" / "__init__.py").write_text("")
    for mod in ("list_prices", "contributed", "alert", "publish_gate"):
        (work / "tracker" / f"{mod}.py").write_text(NOOP_STUB)
    for mod in ("gs_passive", "publish"):
        (work / "tracker" / f"{mod}.py").write_text(OUT_STUB)
    (work / "tools").mkdir()
    (work / "tools" / "__init__.py").write_text("")
    (work / "tools" / "model_rates.py").write_text(RATES_STUB)
    (work / "data").mkdir()
    (work / "data" / "prices.json").write_text('{"claude-opus-5": {"input": 15}}\n')
    (work / "history").mkdir()
    (work / "history" / "gs-passive.json").write_text("{}\n")
    (work / ".gitignore").write_text(".cron.lock\n.notified-change\n.notify-attempts\n"
                                     ".weekly-change-seen\n__pycache__/\n")
    origin = tmp_path / "tracker-origin.git"
    seed_bare(origin, work, env)

    site = home / "all-done-sites-platform"
    (site / "website" / "public" / "data").mkdir(parents=True)
    (site / "website" / "public" / "data" / "claude-usage.json").write_text('{"generated_at": "old"}\n')
    site_origin = tmp_path / "site-origin.git"
    seed_bare(site_origin, site, env)
    return work, origin, site_origin, env


def push_from_clone(origin: Path, tmp_path: Path, name: str, path: str, text: str, env: dict) -> None:
    """Someone else (a merged PR, the other host) changes `path` on origin/main."""
    other = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], env=env, check=True)
    (other / path).write_text(text)
    git(other, "commit", "-q", "-am", f"{name}: change {path}", env=env)
    git(other, "push", "-q", "origin", "main", env=env)


def rebase_in_progress(work: Path) -> bool:
    return (work / ".git" / "rebase-merge").exists() or (work / ".git" / "rebase-apply").exists()
