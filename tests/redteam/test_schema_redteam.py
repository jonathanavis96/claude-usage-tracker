"""Red-team: a usage-endpoint schema change is logged as a good reading, with no warning.

Each test feeds tracker.meter_log.sample a fake endpoint body (no network, a fake token
in a temp config dir) shaped like a plausible change to /api/oauth/usage, and checks
whether the change reaches anyone. The real body today, from moonlighter's log on
masterrig: `five_hour`, `seven_day` and `seven_day_sonnet`, each
`{"utilization": float|null, "resets_at": iso|null}`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tracker import meter_log

NOW = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)
FIVE_RESET = (NOW + timedelta(hours=2)).isoformat()
SEVEN_RESET = (NOW + timedelta(days=3)).isoformat()


@pytest.fixture
def account(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "fake-not-a-token"}}))
    (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-redteam"}}))
    return cfg, tmp_path / "meter.log"


def run(account, body: dict, capsys):
    cfg, log = account
    code = meter_log.sample(cfg, "dave", log, fetch=lambda url, headers: body, now=lambda: NOW)
    err = capsys.readouterr().err
    line = json.loads(log.read_text().splitlines()[-1])
    return code, err, line


def flagged(code: int, err: str, line: dict) -> bool:
    """A schema change counts as noticed if the run fails, warns, or marks the line."""
    return code != 0 or "schema" in err.lower() or "warning" in err.lower() or "schema" in line


def test_renamed_resets_at_is_noticed(account, capsys):
    body = {"five_hour": {"utilization": 35.0, "reset_at": FIVE_RESET},
            "seven_day": {"utilization": 20.0, "reset_at": SEVEN_RESET}}
    code, err, line = run(account, body, capsys)
    assert line["five_hour"]["utilization"] == 35.0 and line["five_hour"]["resets_at"] is None
    assert flagged(code, err, line), "a reading with utilization but no resets_at key was logged as normal"


def test_missing_seven_day_bucket_is_noticed(account, capsys):
    body = {"five_hour": {"utilization": 35.0, "resets_at": FIVE_RESET},
            "weekly": {"utilization": 20.0, "resets_at": SEVEN_RESET}}
    code, err, line = run(account, body, capsys)
    assert line["seven_day"]["utilization"] is None
    assert flagged(code, err, line), "a body with no seven_day bucket was logged as a normal reading"


def test_new_bucket_is_kept_in_the_log(account, capsys):
    body = {"five_hour": {"utilization": 35.0, "resets_at": FIVE_RESET},
            "seven_day": {"utilization": 20.0, "resets_at": SEVEN_RESET},
            "seven_day_fable": {"utilization": 97.0, "resets_at": SEVEN_RESET},
            "limits": [{"name": "fable_weekly", "utilization": 97.0, "resets_at": SEVEN_RESET}]}
    code, err, line = run(account, body, capsys)
    assert code == 0
    assert "seven_day_fable" in line or "limits" in line, "the new bucket was dropped from the log"


def test_null_five_hour_is_a_gap_not_a_reading(account, capsys):
    """Control: null five-hour utilization is already a logged gap (exit 4), never a zero."""
    body = {"five_hour": {"utilization": None, "resets_at": None},
            "seven_day": {"utilization": 20.0, "resets_at": SEVEN_RESET}}
    code, err, line = run(account, body, capsys)
    assert code == meter_log.EXIT_READ_FAILED and "error" in line
