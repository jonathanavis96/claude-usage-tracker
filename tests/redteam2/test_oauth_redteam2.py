"""Red-team 2: an OAuth access token expiring, for each of gs's three metered accounts.

tracker/meter_log.py never refreshes a token (usage_api.AuthExpired: a refresh would
rotate the refresh token under a live Claude Code session). Only Claude Code refreshes
it, when it runs under that config dir, and the access token lapses about 8 h after the
last refresh. So an account nobody uses on gs for that long reads `auth_expired` on
every tick until someone next uses it, while its refresh token is still good.

Readings here are written by the real `meter_log.sample`, with a stub fetch (200 with a
body, then 401 against the same token). No real token or endpoint is touched.
"""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from tracker import health, meter_log

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)   # a Saturday morning
ACCOUNTS = {"avis": ".claude-avis", "dave": ".claude-dave", "jwork": ".claude-javiswork"}


def _config_dir(home, name, uuid):
    d = home / name
    d.mkdir()
    (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}))
    ms = lambda t: int(t.timestamp() * 1000)  # noqa: E731
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": f"stub-access-{uuid}", "refreshToken": f"stub-refresh-{uuid}",
        "expiresAt": ms(T0 - timedelta(hours=3)),                  # lapsed 3 h ago, idle since 11 h ago
        "refreshTokenExpiresAt": ms(T0 + timedelta(days=20))}}))   # still good: a login is NOT needed
    return d


def _ok(url, headers):
    reset = (T0 + timedelta(hours=2)).isoformat()
    return {"five_hour": {"utilization": 4.0, "resets_at": reset},
            "seven_day": {"utilization": 31.0, "resets_at": (T0 + timedelta(days=3)).isoformat()}}


def _expired(url, headers):
    raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))


@pytest.mark.xfail(strict=True, reason="tracker/health.py:295-331 `meters` fails after 15 min with no "
                                       "reading, whatever the cause: an idle account's lapsed access "
                                       "token opens a gs incident (Kuma down) an hour later")
@pytest.mark.parametrize("idle", sorted(ACCOUNTS))
def test_idle_account_token_lapse_is_not_a_meter_failure(tmp_path, fake_home, idle):
    ops = tmp_path / "ops"
    for i, (account, dirname) in enumerate(sorted(ACCOUNTS.items())):
        cfg_dir = _config_dir(fake_home, dirname, f"uuid-{i}")
        log = ops / f"claude-usage-meter-{account}.log"
        t = T0 - timedelta(hours=11)
        while t <= T0 - timedelta(minutes=1):
            # The idle account's token lapses at T0 - 3 h; the other two stay in use.
            fetch = _expired if account == idle and t >= T0 - timedelta(hours=3) else _ok
            meter_log.sample(cfg_dir, account, log, fetch=fetch, now=lambda t=t: t)
            t += timedelta(seconds=65)   # the timer's tick; meter_log spaces calls to >= 110 s
    lines = [json.loads(x) for x in (ops / f"claude-usage-meter-{idle}.log").read_text().splitlines()]
    assert lines[-1].get("reason") == "auth_expired", "precondition: the idle account's reads are auth_expired"
    assert all("five_hour" in x for x in lines if datetime.fromisoformat(x["ts"]) < T0 - timedelta(hours=3))

    cfg = health.gs_config(state=tmp_path / "state.json", pidfile=tmp_path / "x.pid", ops=ops, repo=tmp_path)
    [(_, reason)] = health.run_checks(cfg, T0.timestamp(), only=["meters"])
    assert reason is None, (
        f"{idle}: three hours of an idle account's lapsed access token (refresh token valid for 20 more "
        f"days) is reported as a broken meter and becomes an incident after an hour: {reason}")
