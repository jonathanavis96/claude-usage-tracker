"""Red-team: ways tracker.supervise (masterrig's hourly cron wrapper) fails without alerting.

Every test drives the real `supervise()` with a fake sender and a fake clock. None of
them sends anything or touches the live checkout: lock, state and log live in tmp_path.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

import pytest

from tracker import health, supervise

PY = sys.executable


class FakeSender:
    def __init__(self, ok: bool = True):
        self.sent: list[str] = []
        self.ok = ok

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Belt and braces: never reach the real WhatsApp bridge.
    monkeypatch.setattr(supervise, "whatsapp_sender",
                        lambda text: (_ for _ in ()).throw(AssertionError("real send")))

    class Rig:
        lock = tmp_path / "s.lock"
        state = tmp_path / "state.json"
        log = tmp_path / "run.log"
        send = FakeSender()
        t = 1_000_000.0
        # A health config whose every input is a tmp path the test controls.
        cfg = health.Config(usage_log=tmp_path / "usage.jsonl", state=tmp_path / "state.json",
                            repo=tmp_path, credentials=tmp_path / "creds.json",
                            lock_pidfile=tmp_path / "s.pid", history_dir=tmp_path / "history")

        def run(self, cmd, **kw):
            kw.setdefault("log", self.log)
            kw.setdefault("state_path", self.state)
            kw.setdefault("health_cfg", self.cfg)
            return supervise.supervise(cmd, lock=self.lock, send=self.send,
                                       sleep=lambda s: None, now=lambda: self.t, **kw)

    return Rig()


def failing_cmd(code: int = 1) -> list[str]:
    return [PY, "-c", f"raise SystemExit({code})"]


@pytest.mark.xfail(strict=True, reason=(
    "tracker/supervise.py:109-121 run_with_retry writes the run log through a buffered file "
    "outside any error handling of its own: on a full disk the flush in `finally: out.close()` "
    "raises OSError (ENOSPC) out of supervise(), so the command never runs, no state is written "
    "and no alert is ever sent"))
def test_disk_full_run_log_does_not_crash_the_supervisor(rig, tmp_path):
    ran = tmp_path / "ran"
    # /dev/full accepts open() and fails every write with ENOSPC: a full disk.
    code = rig.run([PY, "-c", f"open({str(ran)!r}, 'w').close()"], log=Path("/dev/full"))
    assert ran.exists(), "the job must still run when its log cannot be written"
    assert code == 0


@pytest.mark.xfail(strict=True, reason=(
    "tracker/supervise.py:88-91 write_state raises when the state directory cannot be written "
    "(full disk, read-only mount). supervise() then dies before decide_alert, and because "
    "consecutive_failures is never persisted the 3-in-a-row threshold can never be reached: "
    "a persistently failing job is never alerted"))
def test_unwritable_state_still_alerts(rig, tmp_path):
    state_dir = tmp_path / "ro"
    state_dir.mkdir()
    state_dir.chmod(0o500)
    try:
        for _ in range(3):
            try:
                rig.run(failing_cmd(), state_path=state_dir / "state.json", retries=0)
            except OSError:
                pass
            rig.t += 3600
    finally:
        state_dir.chmod(0o700)
    assert rig.send.sent, "three failed runs with an unwritable state file sent no alert"


@pytest.mark.xfail(strict=True, reason=(
    "tracker/supervise.py:189 calls health.first_failure with no exception handling. A health "
    "check that raises (see test_health_redteam for a real trigger) kills supervise() after "
    "the run, before decide_alert: the failing run is never alerted and the state is left "
    "without last_health"))
def test_crashing_health_check_still_alerts(rig, monkeypatch):
    def boom(c, now):
        raise RuntimeError("health check bug")

    monkeypatch.setitem(health.CHECKS, "token", boom)
    for _ in range(3):
        try:
            rig.run(failing_cmd(), retries=0)
        except RuntimeError:
            pass
        rig.t += 3600
    assert rig.send.sent, "a crashing health check silenced the failure alert"


@pytest.mark.xfail(strict=True, reason=(
    "tracker/supervise.py:164-168 exits 0 with 'skipped' whenever the lock is held, before any "
    "health check runs, and run_with_retry (supervise.py:114) has no timeout. A run hung on "
    "a stalled git push or fetch holds the lock forever: every later hourly run is skipped "
    "silently, and the health check's own `lock` rule (held over 2 h) is never evaluated by "
    "the only thing that sends alerts"))
def test_run_hung_for_hours_is_alerted(rig):
    pidfile = rig.lock.with_suffix(".pid")
    fd = os.open(rig.lock, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        # The hung run: this very process holds the lock, its pid file is three hours old.
        pidfile.write_text(str(os.getpid()))
        old = time.time() - 3 * 3600
        os.utime(pidfile, (old, old))
        for _ in range(3):
            rig.run(failing_cmd())
            rig.t += 3600
    finally:
        os.close(fd)
    assert rig.send.sent, "a run holding the lock for 3+ hours produced no alert"


@pytest.mark.xfail(strict=True, reason=(
    "tracker/supervise.py:142-146 ignores a False from the sender without a word: nothing on "
    "stderr, nothing in the state file, nothing the health check reads. With pihome's "
    "WhatsApp bridge down (ssh key, wa_send.py, the phone) every incident is retried in "
    "silence forever and no one can tell the alert path itself is broken"))
def test_failed_alert_send_is_recorded(rig, capsys):
    rig.send.ok = False
    for _ in range(3):
        rig.run(failing_cmd(), retries=0)
        rig.t += 3600
    assert rig.send.sent, "precondition: an alert was attempted"
    err = capsys.readouterr().err.lower()
    state = json.loads(rig.state.read_text())
    recorded = any("alert" in k for k in state) or ("alert" in err and ("not sent" in err or "fail" in err))
    assert recorded, "a failed alert send left no trace in stderr or the state file"
