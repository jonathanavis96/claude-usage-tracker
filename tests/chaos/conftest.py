"""Chaos-harness fixtures: repo root on sys.path, and a hard block on non-loopback sockets.

tests/chaos has no __init__.py on purpose: `python3 -m unittest discover -s tests`
(the repo's suite) then skips it, since these tests use pytest's strict xfail.
Run them with `python3 -m pytest tests/chaos`.
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
def loopback_only(monkeypatch):
    """Any connection to anything but 127.0.0.1 fails the test instead of leaving the box."""
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def guard(addr):
        host = addr[0] if isinstance(addr, tuple) else addr
        if host not in _LOOPBACK:
            raise AssertionError(f"chaos harness tried to reach {addr!r}: loopback only")

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
