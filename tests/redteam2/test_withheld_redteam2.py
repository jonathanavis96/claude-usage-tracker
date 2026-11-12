"""Red-team 2: a withheld publish must be a failed run, and a crashed advisory step recorded.

From 2026-10-13T11:00Z to 2026-10-22 a blocking invariant withheld the public JSON on 450
runs in a row. bin/daily.sh put the last published JSON back, printed "no change" and
exited 0, so tracker.supervise recorded success, health ok and a Kuma "up" ping. The same
nine days the model_rates refit crashed on every run and left one warning line each.

Runs the REAL bin/daily.sh in a throwaway gs checkout (tests/redteam2/shellrepo.py), with
a stub tracker.invariants exiting the code under test, and the real tracker.advisory
(with the health and atomic modules it imports) so its record is the one health reads.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from shellrepo import ROOT, git, gs_checkout

from tracker import health


def _gs(tmp_path: Path, home: Path, inv_rc: int, gs_passive_rc: int = 0):
    work, origin, site_origin, env = gs_checkout(tmp_path, home)
    (work / "tracker" / "invariants.py").write_text(f"raise SystemExit({inv_rc})\n")
    if gs_passive_rc:
        (work / "tracker" / "gs_passive.py").write_text(f"raise SystemExit({gs_passive_rc})\n")
    for mod in ("advisory", "health", "atomic"):
        shutil.copy(ROOT / "tracker" / f"{mod}.py", work / "tracker" / f"{mod}.py")
    git(work, "add", "tracker", env=env)
    git(work, "commit", "-q", "-m", "stubs", env=env)
    git(work, "push", "-q", "origin", "main", env=env)
    return work, origin, site_origin, env


def _run(work: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(work / "bin" / "daily.sh")], cwd=work, env=env,
                          capture_output=True, text=True, timeout=120)


def test_a_withheld_publish_exits_9_and_keeps_the_page(tmp_path, fake_home):
    work, origin, site_origin, env = _gs(tmp_path, fake_home, inv_rc=2)
    site_before = git(site_origin, "rev-parse", "main", env=env)
    tracker_before = git(origin, "rev-parse", "main", env=env)
    r = _run(work, env)
    assert r.returncode == 9, (r.returncode, r.stderr)
    assert "keeping the last published JSON" in r.stderr
    assert "withheld the public JSON (exit 9)" in r.stderr
    page = fake_home / "all-done-sites-platform" / health.SITE_JSON
    assert json.loads(page.read_text())["generated_at"] == "old"
    assert git(site_origin, "rev-parse", "main", env=env) == site_before, "a withheld run pushed the site"
    # The run still finished: its tracker state went out.
    assert git(origin, "rev-parse", "main", env=env) != tracker_before


def test_an_advisory_invariant_failure_still_publishes_and_exits_0(tmp_path, fake_home):
    work, _origin, site_origin, env = _gs(tmp_path, fake_home, inv_rc=1)
    site_before = git(site_origin, "rev-parse", "main", env=env)
    r = _run(work, env)
    assert r.returncode == 0, r.stderr
    assert "publishing anyway" in r.stderr
    assert git(site_origin, "rev-parse", "main", env=env) != site_before


def test_a_failing_advisory_step_is_recorded_for_health(tmp_path, fake_home):
    work, _origin, _site, env = _gs(tmp_path, fake_home, inv_rc=0, gs_passive_rc=3)
    r = _run(work, env)
    assert r.returncode == 0, r.stderr
    assert "warning: tracker.gs_passive failed with exit 3, publishing with the previous gs-passive.json" in r.stderr
    state = json.loads((fake_home / ".paperclip" / "ops" / "claude-usage-advisory.json").read_text())
    steps = state["steps"]
    assert steps["tracker.gs_passive"]["last_exit"] == 3
    assert "failing_since" in steps["tracker.gs_passive"]
    assert steps["tracker.list_prices"]["last_exit"] == 0 and "failing_since" not in steps["tracker.list_prices"]
    # Health reads that record: inside the grace nothing, past it the step by name.
    cfg = health.Config(advisory_state=fake_home / ".paperclip" / "ops" / "claude-usage-advisory.json",
                        checks=("advisory",))
    since = steps["tracker.gs_passive"]["failing_since"]
    assert health.check_advisory(cfg, since + 600) is None
    reason = health.check_advisory(cfg, since + 4 * 3600)
    # The step last ran at `since`: 4 h later it is still read (it runs every 30 min on gs).
    assert reason and reason.startswith("advisory: tracker.gs_passive has failed on every run for 4h00m"), reason
