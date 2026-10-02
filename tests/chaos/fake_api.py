"""A local fake of the Claude OAuth usage endpoint, driven by a simulated meter.

Stdlib only. Binds 127.0.0.1 on an ephemeral port and never forwards anywhere.
Each request is answered from `FakeUsageAPI.next_fault` (one-shot) and the
simulated meter's state at the harness's fake clock, so a soak run is fully
deterministic for a given seed.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIVE_HOURS = timedelta(hours=5)
SEVEN_DAYS = timedelta(days=7)


@dataclass
class Window:
    """One true meter window: its reset time and the utilization it has reached."""
    resets_at: datetime
    used: float = 0.0


@dataclass
class SimMeter:
    """The ground truth the fake serves: a five-hour and a seven-day window."""
    now: datetime
    five: Window = None  # type: ignore[assignment]
    seven: Window = None  # type: ignore[assignment]
    history: list[tuple[str, Window]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.five = Window(self.now + FIVE_HOURS)
        self.seven = Window(self.now + SEVEN_DAYS)
        self.history = [("five", self.five), ("seven", self.seven)]

    def advance(self, to: datetime, burn: float) -> None:
        """Move the meter to `to`, rolling windows that expired, then add `burn` percent."""
        self.now = to
        if to >= self.five.resets_at:
            self.reset_five(to)
        if to >= self.seven.resets_at:
            self.reset_seven(to)
        self.five.used = min(100.0, self.five.used + burn)
        self.seven.used = min(100.0, self.seven.used + burn / 10)

    def reset_five(self, at: datetime) -> None:
        self.five = Window(at + FIVE_HOURS)
        self.history.append(("five", self.five))

    def reset_seven(self, at: datetime) -> None:
        self.seven = Window(at + SEVEN_DAYS)
        self.history.append(("seven", self.seven))

    def true_window(self, kind: str, at: datetime) -> Window:
        """The true window of `kind` whose span contains `at`."""
        span = FIVE_HOURS if kind == "five" else SEVEN_DAYS
        for k, w in reversed(self.history):
            if k == kind and w.resets_at - span <= at < w.resets_at + timedelta(seconds=1):
                return w
        raise LookupError(f"no true {kind} window at {at}")


def _iso(t: datetime, jitter_s: float) -> str:
    return (t + timedelta(seconds=jitter_s)).astimezone(timezone.utc).isoformat()


class FakeUsageAPI:
    """The server plus the knobs a test turns. Use as a context manager."""

    def __init__(self, meter: SimMeter, good_tokens: set[str]):
        self.meter = meter
        self.good_tokens = good_tokens
        self.next_fault: str | None = None
        self.fault_arg: float = 0.0
        self.jitter_s: float = 0.0
        self.requests = 0
        self.hold: threading.Barrier | None = None  # set to make concurrent requests meet
        self.lock = threading.Lock()
        self.timeout_sleep_s = 0.6
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 - silence
                pass

            def do_GET(self):  # noqa: N802
                with api.lock:
                    api.requests += 1
                    fault, api.next_fault = api.next_fault, None
                    arg = api.fault_arg
                if api.hold is not None:
                    try:
                        api.hold.wait(timeout=2)
                    except threading.BrokenBarrierError:
                        pass
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if fault == "timeout":
                    time.sleep(api.timeout_sleep_s)
                    return
                if fault == "reset":
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.connection.close()
                    return
                if fault == "401" or token not in api.good_tokens:
                    return self._send(401, b'{"error":"unauthorized"}')
                if fault == "429":
                    return self._send(429, b'{"error":"rate_limited"}', {"Retry-After": str(int(arg))})
                if fault in ("500", "502", "503"):
                    return self._send(int(fault), b"upstream sad")
                m = api.meter
                body = {"five_hour": {"utilization": m.five.used, "resets_at": _iso(m.five.resets_at, api.jitter_s)},
                        "seven_day": {"utilization": m.seven.used, "resets_at": _iso(m.seven.resets_at, -api.jitter_s)}}
                if fault == "missing":
                    body = {"seven_day": body["seven_day"]}
                elif fault == "null":
                    body["five_hour"]["utilization"] = None
                elif fault == "list":
                    body = [body]
                raw = json.dumps(body).encode()
                if fault == "malformed":
                    raw = b"{five_hour: oops"
                elif fault == "truncated":
                    raw = raw[: len(raw) // 2]
                self._send(200, raw)

            def _send(self, code, raw, headers=None):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/oauth/usage"

    def __enter__(self) -> "FakeUsageAPI":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
