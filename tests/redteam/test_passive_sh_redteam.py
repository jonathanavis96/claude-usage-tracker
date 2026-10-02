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
from pathlib import Path

import pytest

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


def make_checkout(tmp_path: Path, home: Path) -> tuple[Path, Path, dict]:
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
    git(work, "commit", "-q", "-m", "init", env=env)
    git(work, "remote", "add", "origin", str(origin), env=env)
    git(work, "push", "-q", "origin", "main", env=env)
    git(work, "branch", "-q", "--set-upstream-to=origin/main", env=env)
    return work, origin, env


def run_passive(work: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(work / "bin" / "passive.sh"), "--force"], cwd=work, env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.xfail(strict=True, reason=(
    "bin/passive.sh:78-83 handles a push that fails even after a rebase retry with a warning "
    "line and falls off the end with exit 0. tracker.supervise records a success, health's "
    "`run` check stays green, and no alert is ever sent, while masterrig's record never "
    "reaches main (the page only shows a1 `stopped` 36 h later)"))
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


@pytest.mark.xfail(strict=True, reason=(
    "bin/passive.sh:77 `git commit ... || exit 0`: a commit that fails (a stale "
    ".git/index.lock from a killed git, a full disk, a failing hook) exits 0. The stamp is "
    "not written, so every hourly run repeats the join and fails the same way, each one "
    "recorded by tracker.supervise as a success"))
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
