"""Red-team 2: two runs overlapping in one checkout.

The masterrig cron runs bin/passive.sh in ~/code/claude-usage-tracker (the live crontab
line, read 2026-10-02: `15 * * * * /home/grafe/code/claude-usage-tracker/bin/passive.sh`),
which is also the checkout Jonathan and his agents work in. The supervisor's flock only
stops two supervised runs; a person or agent in the same checkout is the other "run".
"""
from __future__ import annotations

import subprocess

from shellrepo import git, masterrig_checkout

RECORD = {"history/passive.json", "history/masterrig-passive.json", "history/masterrig-speed.json"}


def test_cron_run_does_not_commit_someone_elses_staged_work(tmp_path, fake_home):
    work, origin, env = masterrig_checkout(tmp_path, fake_home)
    # A session in the same checkout, on main, has staged two changes it has not committed.
    (work / "notes-wip.md").write_text("draft, not for main\n")
    (work / "history" / "notes.md").write_text("draft, not for main either\n")
    git(work, "add", "notes-wip.md", "history/notes.md", env=env)

    r = subprocess.run(["bash", str(work / "bin" / "passive.sh"), "--force"], cwd=work, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"precondition: the run itself succeeds ({r.stderr!r})"
    pushed = git(origin, "show", "--name-only", "--format=", "main", env=env).splitlines()
    assert "history/passive.json" in pushed, f"precondition: the record was pushed ({pushed})"
    foreign = [p for p in pushed if p not in RECORD]
    assert not foreign, f"the hourly record commit pushed someone else's staged work to main: {foreign}"
    staged = git(work, "diff", "--cached", "--name-only", env=env).splitlines()
    assert sorted(staged) == ["history/notes.md", "notes-wip.md"], f"the other session's staged work was lost: {staged}"


def test_cron_run_refuses_to_join_on_uncommitted_tracker_code(tmp_path, fake_home):
    work, origin, env = masterrig_checkout(tmp_path, fake_home)
    before = git(origin, "rev-parse", "main", env=env)
    (work / "tracker" / "__init__.py").write_text("# work in progress\n")
    r = subprocess.run(["bash", str(work / "bin" / "passive.sh"), "--force"], cwd=work, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 4, f"joined on uncommitted tracker/ code: exit {r.returncode} ({r.stderr!r})"
    assert "tracker/ has uncommitted changes" in r.stderr
    assert git(origin, "rev-parse", "main", env=env) == before, "something was pushed"
