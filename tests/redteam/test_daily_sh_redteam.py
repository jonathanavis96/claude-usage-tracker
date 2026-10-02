"""Red-team: gs's bin/daily.sh can stop updating the page while every health check stays green.

The test builds a throwaway tracker checkout (the real bin/daily.sh plus stub modules
that only write their output files), a local bare `origin` for it, and a local bare
"site" repository whose pre-receive hook rejects every push: a revoked deploy key, a
branch-protection rule, or a site repo that diverged in a way the rebase cannot fix.
HOME is a temp dir, so $HOME/all-done-sites-platform is the fake site checkout. No
network, no live checkout.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tracker import health

ROOT = Path(__file__).resolve().parents[2]

OUT_STUB = """import pathlib, sys, time
args = sys.argv
out = pathlib.Path(args[args.index("--out") + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text('{"generated_at": "%s"}\\n' % time.time_ns())
"""
NOOP_STUB = "raise SystemExit(0)\n"
RATES_STUB = """import datetime, json, pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index("--json") + 1])
out.write_text(json.dumps({"_meta": {"generated_at": datetime.datetime.now().astimezone().isoformat()}}))
"""


def _env(home: Path) -> dict:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd: Path, *args: str, env: dict) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def _bare_with_main(path: Path, seed: Path, env: dict) -> None:
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], env=env, check=True)
    git(seed, "init", "-q", "-b", "main", env=env)
    git(seed, "add", ".", env=env)
    git(seed, "commit", "-q", "-m", "init", env=env)
    git(seed, "remote", "add", "origin", str(path), env=env)
    git(seed, "push", "-q", "origin", "main", env=env)
    git(seed, "branch", "-q", "--set-upstream-to=origin/main", env=env)


def make_gs(tmp_path: Path, home: Path) -> tuple[Path, Path, Path, dict]:
    env = _env(home)
    work = tmp_path / "tracker"
    (work / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "daily.sh", work / "bin" / "daily.sh")
    (work / "tracker").mkdir()
    (work / "tracker" / "__init__.py").write_text("")
    for mod in ("list_prices", "contributed", "alert"):
        (work / "tracker" / f"{mod}.py").write_text(NOOP_STUB)
    for mod in ("gs_passive", "publish"):
        (work / "tracker" / f"{mod}.py").write_text(OUT_STUB)
    (work / "tools").mkdir()
    (work / "tools" / "__init__.py").write_text("")
    (work / "tools" / "model_rates.py").write_text(RATES_STUB)
    (work / "data").mkdir()
    (work / "data" / "prices.json").write_text("{}\n")
    (work / "history").mkdir()
    (work / "history" / "gs-passive.json").write_text("{}\n")
    (work / ".gitignore").write_text(".cron.lock\n.notified-change\n.notify-attempts\n"
                                     ".weekly-change-seen\n__pycache__/\n")
    origin = tmp_path / "tracker-origin.git"
    _bare_with_main(origin, work, env)

    site = home / "all-done-sites-platform"
    (site / "website" / "public" / "data").mkdir(parents=True)
    (site / "website" / "public" / "data" / "claude-usage.json").write_text('{"generated_at": "old"}\n')
    site_origin = tmp_path / "site-origin.git"
    _bare_with_main(site_origin, site, env)
    hook = site_origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'ERROR: Permission to all-done-sites-platform denied' >&2\nexit 1\n")
    hook.chmod(0o755)
    return work, origin, site_origin, env


@pytest.mark.xfail(strict=True, reason=(
    "tracker/health.py:137-148 check_publisher only asks when history/gs-passive.json was last "
    "committed to the tracker repo. bin/daily.sh:157-162 commits and pushes that file before "
    "it tries the site push (:164-174), so a site push that fails every run (revoked "
    "github-cut-site deploy key, branch protection, an unfixable divergence) leaves the "
    "tracker side fresh and the health check green while the public page never updates. "
    "gs is not under tracker.supervise, so daily.sh's exit 1 reaches only its cron log"))
def test_rejected_site_push_is_seen_by_health(tmp_path, fake_home):
    work, origin, site_origin, env = make_gs(tmp_path, fake_home)
    site_before = git(site_origin, "rev-parse", "main", env=env)
    tracker_before = git(origin, "rev-parse", "main", env=env)
    r = subprocess.run(["bash", str(work / "bin" / "daily.sh")], cwd=work, env=env,
                       capture_output=True, text=True, timeout=120)
    # Preconditions: the site never received the publish, the tracker state did.
    assert "push to site repo failed" in r.stderr, r.stderr
    assert git(site_origin, "rev-parse", "main", env=env) == site_before
    assert git(origin, "rev-parse", "main", env=env) != tracker_before

    # masterrig's view: a clone of the tracker origin, checked the way supervise checks it.
    mirror = tmp_path / "masterrig"
    subprocess.run(["git", "clone", "-q", str(origin), str(mirror)], env=env, check=True)
    reasons = [r for _, r in health.run_checks(
        health.Config(usage_log=tmp_path / "none.jsonl", state=tmp_path / "none.json", repo=mirror,
                      credentials=tmp_path / "none-creds.json", lock_pidfile=tmp_path / "none.pid",
                      history_dir=mirror / "history"),
        time.time(), only=["publisher"])]
    assert any(reasons), ("the page stopped updating (site push rejected) and the publisher "
                          "health check is still green")
    assert json.loads((fake_home / "all-done-sites-platform" / "website" / "public" / "data"
                       / "claude-usage.json").read_text())["generated_at"] != "old"
