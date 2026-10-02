"""Red-team 2: the usage endpoint refusing requests for asking too often.

docs/burn-20261002/failure-inventory.md F9 measured gs's 429s: mostly Retry-After 0 about
every other tick (fixed by the 110 s spacing), and "15 more carried Retry-After of about
3,600 s". "A run of Retry-After 0 answers escalates to a Retry-After 3600 block": the
endpoint answers persistence with longer blocks. Since the chaos fix (6), meter_log caps
any Retry-After at RETRY_429_MAX_S (1200 s) to stop an absurd 86400 from halting
sampling for a day.

The 429 is a stub HTTPError; the real endpoint is never called.
"""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from tracker import meter_log

T0 = datetime(2026, 9, 26, 14, 28, tzinfo=timezone.utc)   # jwork's 3,600 s block began here


@pytest.mark.xfail(strict=True, reason="tracker/meter_log.py:222 caps Retry-After at 1200 s, so a tick "
                                       "20 and 40 min into the endpoint's measured 3,600 s block calls it again")
def test_measured_one_hour_retry_after_is_honoured(tmp_path, fake_home):
    cfg = fake_home / ".claude-javiswork"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-jwork"}}))
    (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "stub"}}))
    log = tmp_path / "claude-usage-meter-jwork.log"

    def blocked(url, headers):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "3600"}, io.BytesIO(b""))

    assert meter_log.sample(cfg, "jwork", log, fetch=blocked, now=lambda: T0) == meter_log.EXIT_READ_FAILED
    assert json.loads(log.read_text().splitlines()[-1])["reason"] == "rate_limited", "precondition: 429 logged"

    calls: list[datetime] = []
    t = T0 + timedelta(seconds=65)
    while t < T0 + timedelta(seconds=3600):
        def counting(url, headers, t=t):
            calls.append(t)
            return blocked(url, headers)
        meter_log.sample(cfg, "jwork", log, fetch=counting, now=lambda t=t: t)
        t += timedelta(seconds=65)
    assert not calls, (f"the endpoint said Retry-After: 3600 and was called {len(calls)} more times inside "
                       f"that hour, first at +{int((calls[0] - T0).total_seconds())} s")
