"""The hardened installers (deploy/lib.sh), deploy/rollback.sh and scripts/deploy.sh.

Every run uses a temporary HOME, a fake `crontab` first on PATH and a throwaway git
checkout, so the live crontab and the real tracker are never read or written.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._deploy_fixture import fake_crontab_bin, git, make_checkout

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "deploy" / "install-schedule.sh"
GS = ROOT / "deploy" / "install-gs.sh"
ROLLBACK = ROOT / "deploy" / "rollback.sh"
WRAPPER = ROOT / "scripts" / "deploy.sh"


def snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Every file under root: (mtime_ns, sha256). Any write, even an identical one, shows."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(root))] = (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.repo = make_checkout(self.home / "cut")
        self.env = {**os.environ, "HOME": str(self.home), "CUT_PIHOME_SSH": "none",
                    "PATH": f"{fake_crontab_bin(self.home)}:{os.environ['PATH']}"}
        self.env.pop("CRONTAB", None)
        self.tab = self.home / "crontab.txt"
        self.legacy = f"0 0 * * * other\n15 * * * * {self.repo}/bin/passive.sh >> x.log 2>&1\n"
        self.backups = self.home / ".local" / "state" / "claude-usage-tracker" / "deploy-backups"

    def run_script(self, script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(script), *args], env=env or self.env, capture_output=True,
                              text=True, timeout=120)

    def install(self, *args: str, script: Path = SCHEDULE, env: dict | None = None) -> subprocess.CompletedProcess:
        return self.run_script(script, "--repo", str(self.repo), *args, env=env)

    # -- the merge ------------------------------------------------------------------

    def test_merge_is_idempotent(self):
        self.tab.write_text(self.legacy)
        once = self.install("--print").stdout
        self.assertEqual(once.count("tracker.supervise"), 1)
        self.assertNotIn(">> x.log", once)
        self.assertIn("0 0 * * * other", once)
        self.tab.write_text(once)
        self.assertEqual(self.install("--print").stdout, once)

    def test_apply_twice_changes_nothing_the_second_time(self):
        self.tab.write_text(self.legacy)
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("schedule installed", first.stdout)
        after = self.tab.read_text()
        backups = list(self.backups.iterdir())
        second = self.install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already up to date", second.stdout)
        self.assertEqual(self.tab.read_text(), after)
        self.assertEqual(list(self.backups.iterdir()), backups, "a no-op run made a backup")

    # -- dry run --------------------------------------------------------------------

    def assert_dry_run_changes_nothing(self, script: Path):
        self.tab.write_text(self.legacy)
        before = snapshot(self.home)
        r = self.install("--dry-run", script=script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.backups.exists())
        self.assertIn("+++ ", r.stdout)
        self.assertIn("tracker.supervise", r.stdout)
        self.assertIn("RESULT: a real run would proceed", r.stderr)
        return r

    def test_dry_run_changes_nothing(self):
        r = self.assert_dry_run_changes_nothing(SCHEDULE)
        self.assertIn(f"-15 * * * * {self.repo}/bin/passive.sh >> x.log 2>&1", r.stdout)

    def test_gs_dry_run_changes_nothing(self):
        self.assert_dry_run_changes_nothing(GS)

    def test_dry_run_reports_a_refusal_and_still_changes_nothing(self):
        (self.repo / "tracker" / "health.py").write_text("print('edited')\n")
        self.tab.write_text(self.legacy)
        before = snapshot(self.home)
        r = self.install("--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertIn("REFUSE: checkout has 1 uncommitted change(s)", r.stderr)
        self.assertIn("RESULT: a real run would REFUSE", r.stderr)
        self.assertEqual(snapshot(self.home), before)

    # -- refusals -------------------------------------------------------------------

    def assert_refused(self, *args: str, reason: str):
        self.tab.write_text(self.legacy)
        r = self.install(*args)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn(reason, r.stderr)
        self.assertEqual(self.tab.read_text(), self.legacy)
        self.assertFalse(self.backups.exists())

    def test_refuses_a_dirty_checkout(self):
        (self.repo / "tracker" / "health.py").write_text("print('edited')\n")
        self.assert_refused(reason="uncommitted change(s) to tracked files")

    def test_refuses_a_checkout_off_main(self):
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.assert_refused(reason="checkout is on 'feature', not main")

    def test_refuses_a_checkout_without_the_expected_commit(self):
        self.assert_refused("--expect", "0123456789abcdef0123456789abcdef01234567",
                            reason="is not in this checkout")

    def test_refuses_a_checkout_behind_the_expected_commit(self):
        old = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "later").write_text("x")
        git(self.repo, "add", "later")
        git(self.repo, "commit", "-q", "--no-verify", "-m", "later")
        new = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "reset", "-q", "--hard", old)
        self.assert_refused("--expect", new, reason="does not contain expected commit")

    def test_refuses_a_checkout_that_predates_the_supervisor(self):
        git(self.repo, "rm", "-q", "tracker/supervise.py")
        git(self.repo, "commit", "-q", "--no-verify", "-m", "old")
        self.assert_refused(reason="HEAD has no tracker/supervise.py")

    def test_proceeds_when_the_expected_commit_is_contained(self):
        self.tab.write_text(self.legacy)
        r = self.install("--expect", git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("contains expected commit", r.stderr)

    # -- backup, rollback, post-deploy check ----------------------------------------

    def test_backup_then_rollback_restores_the_old_crontab(self):
        self.tab.write_text(self.legacy)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        (b,) = self.backups.iterdir()
        self.assertEqual((b / "crontab.before").read_text(), self.legacy)
        self.assertEqual((b / "crontab.after").read_text(), self.tab.read_text())
        self.assertEqual(oct(b.stat().st_mode & 0o777), "0o700")
        installed = self.tab.read_text()
        dry = self.run_script(ROLLBACK, str(b))
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self.tab.read_text(), installed, "a rollback dry run changed the crontab")
        r = self.run_script(ROLLBACK, str(b), "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tab.read_text(), self.legacy)
        self.assertEqual(len(list(b.glob("crontab.pre-rollback-*"))), 1)
        self.assertIn("nothing to do", self.run_script(ROLLBACK, str(b), "--apply").stderr)

    def test_rollback_refuses_when_the_crontab_changed_since(self):
        self.tab.write_text(self.legacy)
        self.install()
        (b,) = self.backups.iterdir()
        changed = self.tab.read_text() + "5 5 * * * added later\n"
        self.tab.write_text(changed)
        r = self.run_script(ROLLBACK, str(b), "--apply")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.tab.read_text(), changed)
        r = self.run_script(ROLLBACK, str(b), "--apply", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tab.read_text(), self.legacy)

    def test_post_check_runs_the_supervisor_once(self):
        self.tab.write_text(self.legacy)
        r = self.install()
        self.assertIn("supervisor exit 0; last_exit=0 last_health='ok'", r.stderr)
        self.assertIn("RESULT: installed and checked", r.stderr)

    def test_failed_post_check_exits_4_with_the_crontab_installed(self):
        self.tab.write_text(self.legacy)
        r = self.install(env={**self.env, "STUB_SUPERVISE_EXIT": "1"})
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("post-deploy check FAILED", r.stderr)
        self.assertIn("tracker.supervise", self.tab.read_text())

    # -- wrapper --------------------------------------------------------------------

    def test_wrapper_dry_run_on_masterrig_changes_nothing(self):
        self.tab.write_text(self.legacy)
        before = snapshot(self.home)
        r = self.run_script(WRAPPER, "--host", "masterrig", "--expect", git(self.repo, "rev-parse", "HEAD"),
                            env={**self.env, "CUT_MASTERRIG_REPO": str(self.repo)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RESULT: a real run would proceed", r.stderr)
        self.assertEqual(snapshot(self.home), before)

    # -- an unreadable crontab is never replaced -------------------------------------

    def test_no_crontab_yet_is_an_empty_crontab(self):
        self.assertFalse(self.tab.exists())
        r = self.install("--no-check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tracker.supervise", self.tab.read_text())

    def test_unreadable_crontab_refuses_in_every_mode_and_writes_nothing(self):
        fake = self.home / "fakebin" / "crontab"
        fake.write_text('#!/bin/sh\n[ "$1" = "-l" ] && { echo "crontab: cannot open: Permission denied" >&2;'
                        ' exit 1; }\necho w >> "$HOME/writes.txt"; cat > "$HOME/crontab.txt"\n')
        self.tab.write_text(self.legacy)
        for script in (SCHEDULE, GS):
            for mode in ([], ["--no-check"], ["--dry-run"], ["--print"]):
                with self.subTest(script=script.name, mode=mode):
                    r = self.install(*mode, script=script)
                    self.assertEqual(r.returncode, 3, r.stderr)
                    self.assertIn("cannot read the current crontab", r.stderr)
                    self.assertEqual(r.stdout, "")
                    self.assertEqual(self.tab.read_text(), self.legacy)
                    self.assertFalse((self.home / "writes.txt").exists())

    def test_rollback_refuses_an_unreadable_crontab(self):
        self.tab.write_text(self.legacy)
        r = self.install("--no-check")
        self.assertEqual(r.returncode, 0, r.stderr)
        backup = next(self.backups.iterdir())
        (self.home / "fakebin" / "crontab").write_text(
            '#!/bin/sh\n[ "$1" = "-l" ] && { echo "Permission denied" >&2; exit 1; }\ncat > "$HOME/crontab.txt"\n')
        installed = self.tab.read_text()
        r = self.run_script(ROLLBACK, str(backup), "--apply", "--force")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.tab.read_text(), installed)

    def test_lost_lines_guard_names_any_other_dropped_line(self):
        script = (f'. "{ROOT}/deploy/lib.sh"\n'
                  'cur=$(printf "%s\\n" "0 0 * * * other" "# c" "B" "x old" "E" "5 * * * * /r/bin/passive.sh")\n'
                  'printf "lost:%s\\n" "$(cut_lost_lines "$cur" "$(printf "%s\\n" "B" "new" "E")" B E /r/bin/passive.sh)"\n'
                  'printf "kept:%s\\n" "$(cut_lost_lines "$cur" "$(printf "%s\\n" "0 0 * * * other" "# c" "B" "new" "E")" B E /r/bin/passive.sh)"\n')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30).stdout
        self.assertIn("lost:0 0 * * * other\n# c\n", out)
        self.assertIn("kept:\n", out)


if __name__ == "__main__":
    unittest.main()
