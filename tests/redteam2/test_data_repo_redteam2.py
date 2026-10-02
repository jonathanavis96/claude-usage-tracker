"""Red-team 2: a diverged or conflicted data repo.

Both hosts push to the tracker repo's main, and PRs land there too: the integrate branch
itself rewrites history/passive.json (21 restored days) and data/prices.json gets rows by
PR. A host whose last push failed holds a local commit; when main has meanwhile changed
the same file, the next `git pull --rebase --autostash` stops on a conflict.
"""
from __future__ import annotations

import json
import subprocess
from datetime import date

import pytest

from shellrepo import git, gs_checkout, masterrig_checkout, push_from_clone, rebase_in_progress


def _conflict_masterrig(tmp_path, home):
    work, origin, env = masterrig_checkout(tmp_path, home)
    # masterrig's previous record was committed but its push failed (exit 5): a local commit.
    (work / "history" / "passive.json").write_text(
        '{"history": {"2026-07-30": {"tokens_per_pct": 5}, "2026-10-01": {"tokens_per_pct": 7}}}\n')
    git(work, "commit", "-q", "-am", "Passive history 2026-10-01", env=env)
    # Meanwhile a PR restoring lost days reaches main.
    push_from_clone(origin, tmp_path, "restore-pr", "history/passive.json",
                    '{"history": {"2026-07-29": {"tokens_per_pct": 4}, "2026-07-30": {"tokens_per_pct": 5}}}\n',
                    env)
    return work, env, ["bash", str(work / "bin" / "passive.sh"), "--force"]


def _conflict_gs(tmp_path, home):
    work, origin, _site, env = gs_checkout(tmp_path, home)
    # gs's previous state commit (a list_prices row) was committed but its push failed.
    (work / "data" / "prices.json").write_text('{"claude-opus-5": {"input": 15}, "claude-new": {"input": 3}}\n')
    git(work, "commit", "-q", "-am", "Daily publisher state", env=env)
    # Meanwhile a price-table PR reaches main.
    push_from_clone(origin, tmp_path, "prices-pr", "data/prices.json",
                    '{"claude-opus-5": {"input": 16}}\n', env)
    return work, env, ["bash", str(work / "bin" / "daily.sh")]


@pytest.mark.xfail(strict=True, reason="bin/passive.sh:57,69 and bin/daily.sh:39 never `git rebase --abort` "
                                       "a pull that stopped on a conflict: the checkout stays mid-rebase")
@pytest.mark.parametrize("host", ["masterrig_passive_sh", "gs_daily_sh"])
def test_conflicting_pull_leaves_no_rebase_in_progress(tmp_path, fake_home, host):
    work, env, cmd = (_conflict_masterrig if host == "masterrig_passive_sh" else _conflict_gs)(tmp_path, fake_home)
    r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=120)
    assert "pull --rebase" in r.stderr and "failed" in r.stderr, f"precondition: the pull conflicted ({r.stderr!r})"
    branch = git(work, "rev-parse", "--abbrev-ref", "HEAD", env=env)
    assert not rebase_in_progress(work) and branch == "main", (
        f"the checkout was left mid-rebase (HEAD is {branch!r}): every later run fails on it, and "
        "masterrig's alert says `git checkout main`, which does not end a rebase")


@pytest.mark.xfail(strict=True, reason="tracker/passive.py:87-90 reads an unparseable previous record "
                                       "(conflict markers) as no record, and rewrites it without the kept days")
def test_unreadable_previous_record_keeps_its_days(tmp_path, monkeypatch):
    from tracker import passive
    from tracker.join import DailyRate

    out = tmp_path / "history" / "passive.json"
    out.parent.mkdir()
    ours = {"history": {"2026-07-30": {"tokens_per_pct": 3_900_000, "interpolated": False},
                        "2026-10-01": {"tokens_per_pct": 4_100_000, "interpolated": False}}}
    theirs = {"history": {"2026-07-29": {"tokens_per_pct": 3_800_000, "interpolated": False},
                          "2026-07-30": {"tokens_per_pct": 3_900_000, "interpolated": False}}}
    # What `git pull --rebase` leaves in the file when both sides changed it.
    out.write_text("<<<<<<< HEAD\n" + json.dumps(theirs, indent=1) + "\n=======\n"
                   + json.dumps(ours, indent=1) + "\n>>>>>>> 1d0100a (Passive history 2026-10-01)\n")
    before = out.read_text()
    # The transcripts reach back only to 2026-09-20 (Claude Code's cleanup), as on masterrig.
    rates = {date(2026, 10, 2): DailyRate(4_200_000.0, 3, False, {}, {})}
    monkeypatch.setattr(passive, "_rebuild", lambda home: (rates, {}, None))

    code = passive.main(["--out", str(out)])
    if code != 0:
        assert out.read_text() == before, "refused, but still overwrote the record"
        return
    days = set(json.loads(out.read_text())["history"])
    assert {"2026-07-29", "2026-07-30"} <= days, (
        f"every day the transcripts no longer reach was dropped: the record now holds only {sorted(days)}")
