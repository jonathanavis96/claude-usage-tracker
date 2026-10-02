"""Red-team fixtures: repo root on sys.path, no network, and HOME pointed at a temp dir.

tests/redteam has no __init__.py on purpose, like tests/chaos: `python3 -m unittest
discover -s tests` skips it, because every test here is a pytest strict xfail that
documents a confirmed failure on the current code. Run one file at a time:

    timeout 300 nice -n 10 python3 -m pytest tests/redteam/test_supervise_redteam.py -q

When a fix lands, its test turns into a strict XPASS failure; remove the marker then.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
    """No test may read the real ~/.claude, ~/.moonlighter or the notify env file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CUT_ALERT_DRY_RUN", "1")
    return home
