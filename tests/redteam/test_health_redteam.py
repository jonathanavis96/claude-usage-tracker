"""Red-team: health checks that pass while collection is broken, or crash outright."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tracker import health

NOW = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)


def _stamp(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


@pytest.mark.xfail(strict=True, reason=(
    "tracker/health.py:188-212 check_meter judges freshness by the newest line of any kind. "
    "An account whose token was revoked writes a fresh `auth_expired` error line every tick "
    "(meter_log.py:314; error lines never trigger the 110 s spacing), so the log looks live "
    "while it has not held a reading for hours. Only 429s are counted as bad lines"))
def test_meter_log_of_only_auth_errors_is_unhealthy(tmp_path):
    log = tmp_path / "claude-usage-meter-dave.log"
    lines = []
    # A good reading six hours ago, then one auth_expired gap a minute since.
    t = NOW - timedelta(hours=6)
    lines.append({"ts": _stamp(t), "account": "dave", "identity": "abc",
                  "five_hour": {"utilization": 12.0, "resets_at": _stamp(t + timedelta(hours=2))},
                  "seven_day": {"utilization": 30.0, "resets_at": _stamp(t + timedelta(days=3))}})
    while t < NOW - timedelta(minutes=1):
        t += timedelta(minutes=1)
        lines.append({"ts": _stamp(t), "account": "dave", "identity": "abc",
                      "error": "AuthExpired: 401 with unchanged token", "reason": "auth_expired"})
    log.write_text("".join(json.dumps(r) + "\n" for r in lines))
    reason = health.check_meter(health.Config(meter_logs=(log,)), NOW.timestamp())
    assert reason is not None, "six hours of auth_expired lines passed the meter health check"


@pytest.mark.xfail(strict=True, reason=(
    "tracker/health.py:102-112 check_collection takes the last line with a `ts` as the newest "
    "sample, whether or not it carries a utilization. A meter that logs failed reads (an "
    "error row with a ts, as tracker.meter_log does) reads as fresh collection"))
def test_collection_error_row_is_not_a_fresh_sample(tmp_path):
    log = tmp_path / "usage_log.jsonl"
    good = NOW - timedelta(hours=5)
    rows = [{"ts": _stamp(good), "five_hour": {"utilization": 20.0, "resets_at": None},
             "seven_day": {"utilization": 10.0, "resets_at": None}},
            {"ts": _stamp(NOW - timedelta(minutes=5)), "error": "HTTPError: HTTP Error 403: Forbidden"}]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    reason = health.check_collection(health.Config(usage_log=log), NOW.timestamp())
    assert reason is not None, "an error row five minutes old hid a five-hour-old last reading"


@pytest.mark.xfail(strict=True, reason=(
    "tracker/health.py:153-156 only guards the JSON parse. When `claudeAiOauth` is not an "
    "object (a credentials format change, a hand edit), `oauth.get` raises AttributeError "
    "outside the try, so `python3 -m tracker.health` crashes with a traceback and "
    "tracker.supervise dies before it can alert (see test_supervise_redteam)"))
def test_token_check_survives_unexpected_credentials_shape(tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": "migrated-to-keychain"}))
    reason = health.check_token(health.Config(credentials=creds), NOW.timestamp())
    assert reason is not None and reason.startswith("token:")


@pytest.mark.xfail(strict=True, reason=(
    "tracker/health.py:231 calls pid_alive, whose os.kill(pid, 0) at :217 takes any all-digit pid file. A corrupted "
    "pid file with a huge number raises OverflowError, which pid_alive does not catch, and "
    "the health run crashes"))
def test_lock_check_survives_corrupt_pidfile(tmp_path):
    pidfile = tmp_path / ".supervise.pid"
    pidfile.write_text("9" * 30)
    reason = health.check_lock(health.Config(lock_pidfile=pidfile), NOW.timestamp())
    assert reason is not None and reason.startswith("lock:")
