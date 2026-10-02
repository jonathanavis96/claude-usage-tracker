"""Red-team: bin/passive.sh reports success when its record never left masterrig.

Each test builds a throwaway clone in tmp_path holding the real bin/passive.sh and stub
tracker.passive / tracker.gs_passive / tracker.speed modules (they only write their
output file), with a local bare repository as `origin`. Nothing reads ~/.claude or
touches the live checkout or GitHub.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tracker import health

ROOT = Path(__file__).resolve().parents[2]

STUB = """import pathlib, sys, time
flag = "--out" if "--out" in sys.argv else "--history"
out = pathlib.Path(sys.argv[sys.argv.index(flag) + 1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(str(time.time_ns()) + "\\n")
"""


def git(cwd: Path, *args: str, env: dict) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout


def make_checkout(tmp_path: Path, home: Path, init_date: str | None = None) -> tuple[Path, Path, dict]:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=env, check=True)
    work = tmp_path / "work"
    (work / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "passive.sh", work / "bin" / "passive.sh")
    (work / "tracker").mkdir()
    (work / "tracker" / "__init__.py").write_text("")
    for mod in ("passive", "gs_passive", "speed"):
        (work / "tracker" / f"{mod}.py").write_text(STUB)
    (work / "history").mkdir()
    (work / "history" / "passive.json").write_text("{}\n")
    (work / ".gitignore").write_text(".passive-last-ok\n__pycache__/\n")
    git(work, "init", "-q", "-b", "main", env=env)
    git(work, "add", ".", env=env)
    dated = {**env, "GIT_AUTHOR_DATE": init_date, "GIT_COMMITTER_DATE": init_date} if init_date else env
    git(work, "commit", "-q", "-m", "init", env=dated)
    git(work, "remote", "add", "origin", str(origin), env=env)
    git(work, "push", "-q", "origin", "main", env=env)
    git(work, "branch", "-q", "--set-upstream-to=origin/main", env=env)
    return work, origin, env


def run_passive(work: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(work / "bin" / "passive.sh"), "--force"], cwd=work, env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("fault", ["push_rejected", "origin_unreachable"])
def test_unpushed_record_is_not_reported_as_success(tmp_path, fake_home, fault):
    work, origin, env = make_checkout(tmp_path, fake_home)
    if fault == "push_rejected":
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'rejected: protected branch' >&2\nexit 1\n")
        hook.chmod(0o755)
    else:
        # The network is down: origin cannot be reached at all.
        shutil.move(str(origin), str(tmp_path / "gone.git"))
    r = run_passive(work, env)
    assert "push failed" in r.stderr, f"precondition: the push must fail ({r.stderr!r})"
    assert r.returncode != 0, "a record that never left masterrig was reported as a successful run"


def test_failed_commit_is_not_reported_as_success(tmp_path, fake_home):
    work, _, env = make_checkout(tmp_path, fake_home)
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'fatal: Unable to create index.lock: File exists' >&2\nexit 1\n")
    hook.chmod(0o755)
    head_before = git(work, "rev-parse", "HEAD", env=env)
    r = run_passive(work, env)
    assert git(work, "rev-parse", "HEAD", env=env) == head_before, "precondition: nothing was committed"
    assert not (work / ".passive-last-ok").exists()
    assert r.returncode != 0, "a run that committed nothing was reported as a success"


def test_harness_happy_path_pushes(tmp_path, fake_home):
    """Control: with a healthy origin the same harness commits, pushes and exits 0."""
    work, origin, env = make_checkout(tmp_path, fake_home)
    r = run_passive(work, env)
    assert r.returncode == 0, r.stderr
    assert git(work, "rev-parse", "HEAD", env=env) == git(origin, "rev-parse", "main", env=env)
    assert (work / ".passive-last-ok").exists()
    assert not os.environ.get("PYTEST_XDIST_WORKER")  # single worker only


@pytest.mark.xfail(strict=True, reason=(
    "tracker/health.py:137-148 check_publisher runs `git log -1 -- history/gs-passive.json` on "
    "the checkout's HEAD. bin/passive.sh only moves HEAD on a full run (about once in 20 h): "
    "its hourly early exit (passive.sh:42-48) just fetches. So 2 h after each full run the "
    "check reads gs's publisher as stale while origin/main holds a commit minutes old. Under "
    "tracker.supervise that is a daily false incident, and while it is open no other alert "
    "is sent, so a real failure in the same hours is masked"))
def test_publisher_check_reads_what_was_fetched(tmp_path, fake_home):
    three_hours_ago = f"@{int(time.time()) - 3 * 3600} +0000"
    work, origin, env = make_checkout(tmp_path, fake_home, init_date=three_hours_ago)
    (work / "history" / "gs-passive.json").write_text("{}\n")
    git(work, "add", "history/gs-passive.json", env=env)
    dated = {**env, "GIT_AUTHOR_DATE": three_hours_ago, "GIT_COMMITTER_DATE": three_hours_ago}
    git(work, "commit", "-q", "-m", "Daily publisher state (old)", env=dated)
    git(work, "push", "-q", "origin", "main", env=env)
    r = run_passive(work, env)  # a full run: commits, pushes, writes the 20 h stamp
    assert r.returncode == 0 and (work / ".passive-last-ok").exists(), r.stderr

    # gs publishes again, now.
    gs = tmp_path / "gs"
    subprocess.run(["git", "clone", "-q", str(origin), str(gs)], env=env, check=True)
    (gs / "history" / "gs-passive.json").write_text('{"generated_at": "now"}\n')
    git(gs, "commit", "-q", "-am", "Daily publisher state (new)", env=env)
    git(gs, "push", "-q", "origin", "main", env=env)

    # masterrig's next hourly run: stamp is fresh, so it only fetches and exits 0.
    head = git(work, "rev-parse", "HEAD", env=env)
    r = subprocess.run(["bash", str(work / "bin" / "passive.sh")], cwd=work, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and git(work, "rev-parse", "HEAD", env=env) == head, "precondition: early exit"
    assert git(work, "log", "-1", "--format=%s", "origin/main", env=env).strip() == "Daily publisher state (new)"

    reason = health.check_publisher(health.Config(repo=work), time.time())
    assert reason is None, f"fresh publisher on origin/main reported stale: {reason}"
