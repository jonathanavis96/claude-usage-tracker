"""Red-team round 2 fixtures: repo root on sys.path, no network, HOME pointed at a temp dir.

tests/redteam2 has no __init__.py on purpose, like tests/redteam and tests/chaos:
`python3 -m unittest discover -s tests` skips it. Every test here began as a pytest strict
xfail documenting a confirmed failure; each finding is now fixed and its test passes.
Run one file at a time:

    timeout 300 nice -n 10 python3 -m pytest tests/redteam2/test_overlap_redteam2.py -q


Nothing here touches a live crontab, systemd unit, Kuma monitor, ssh host, the live data
repo or the usage API: git work happens in throwaway repos under tmp_path, crontabs are
a fake `crontab` script over a file, and every usage read is a stub.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
for p in (ROOT, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any connection to anything but loopback fails the test instead of leaving the box."""
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def guard(addr):
        host = addr[0] if isinstance(addr, tuple) else addr
        if host not in _LOOPBACK:
            raise AssertionError(f"red-team test tried to reach {addr!r}: loopback only")

    def connect(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            guard(addr)
        return real_connect(self, addr)

    def create_connection(addr, *a, **kw):
        guard(addr)
        return real_create(addr, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    yield


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """No test may read the real ~/.claude*, ~/.moonlighter, crontab or notify env file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CUT_ALERT_DRY_RUN", "1")
    return home
