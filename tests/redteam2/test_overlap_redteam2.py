"""Red-team 2: two runs overlapping in one checkout.

The masterrig cron runs bin/passive.sh in ~/code/claude-usage-tracker (the live crontab
line, read 2026-10-02: `15 * * * * /home/grafe/code/claude-usage-tracker/bin/passive.sh`),
which is also the checkout Jonathan and his agents work in. The supervisor's flock only
stops two supervised runs; a person or agent in the same checkout is the other "run".
"""
from __future__ import annotations

import subprocess

import pytest

from shellrepo import git, masterrig_checkout


@pytest.mark.xfail(strict=True, reason="bin/passive.sh:79 `git commit` commits the whole index, so "
                                       "work someone else staged in the checkout is pushed to main")
def test_cron_run_does_not_commit_someone_elses_staged_work(tmp_path, fake_home):
    work, origin, env = masterrig_checkout(tmp_path, fake_home)
    # A session in the same checkout, on main, has staged two changes it has not committed.
    (work / "notes-wip.md").write_text("draft, not for main\n")
    (work / "tracker" / "__init__.py").write_text("# work in progress\n")
    git(work, "add", "notes-wip.md", "tracker/__init__.py", env=env)

    r = subprocess.run(["bash", str(work / "bin" / "passive.sh"), "--force"], cwd=work, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"precondition: the run itself succeeds ({r.stderr!r})"
    pushed = git(origin, "show", "--name-only", "--format=", "main", env=env).splitlines()
    assert "history/passive.json" in pushed, f"precondition: the record was pushed ({pushed})"
    foreign = [p for p in pushed if not p.startswith("history/")]
    assert not foreign, f"the hourly record commit pushed someone else's staged work to main: {foreign}"
