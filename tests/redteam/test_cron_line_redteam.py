"""Red-team: the installed cron line throws away everything tracker.supervise itself says.

The line deploy/install-schedule.sh writes is run for real under `sh -c` with a minimal
cron-like environment (HOME and PATH only), from a temp "checkout" whose
tracker/supervise.py crashes the way the supervisor does on a full disk or a broken
health check (see test_supervise_redteam). A fake crontab binary is used, so the live
crontab is never read or written.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "install-schedule.sh"
FAKE_CRONTAB = """#!/bin/sh
f="$HOME/crontab.txt"
if [ "$1" = "-l" ]; then [ -f "$f" ] && cat "$f" || exit 1; else cat > "$f"; fi
"""
MARKER = "SUPERVISOR-CRASHED-7f3a"


def test_supervisor_crash_leaves_a_trace(tmp_path, fake_home):
    fake = fake_home / "fakecrontab"
    fake.write_text(FAKE_CRONTAB)
    fake.chmod(0o755)
    repo = tmp_path / "repo"
    (repo / "tracker").mkdir(parents=True)
    (repo / "tracker" / "__init__.py").write_text("")
    (repo / "tracker" / "supervise.py").write_text(f"raise RuntimeError({MARKER!r})\n")
    log = fake_home / "ops" / "claude-usage-passive.log"
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin", "CRONTAB": str(fake)}
    out = subprocess.run(["bash", str(SCRIPT), "--repo", str(repo), "--log", str(log), "--dry-run"],
                         env=env, capture_output=True, text=True, check=True).stdout
    line = next(ln for ln in out.splitlines() if "tracker.supervise" in ln and not ln.startswith("#"))
    command = line.split(None, 5)[5]  # drop the five schedule fields
    log.parent.mkdir(parents=True, exist_ok=True)
    # cron runs the line with /bin/sh, HOME and a minimal PATH.
    r = subprocess.run(["/bin/sh", "-c", command], env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin"},
                       cwd=fake_home, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, "precondition: the stub supervisor crashed"
    traced = [p for p in tmp_path.rglob("*") if p.is_file() and MARKER in p.read_text(errors="replace")
              and p.name != "supervise.py" and "__pycache__" not in p.parts]
    assert traced, "the supervisor's crash went to /dev/null: no file holds its traceback"
