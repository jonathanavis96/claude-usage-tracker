"""bin/claude-login-expiry-check: one damaged seat must not stop the daily check."""
import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "claude-login-expiry-check"


@pytest.fixture()
def check():
    loader = importlib.machinery.SourceFileLoader("login_expiry_check", str(SCRIPT))
    spec = importlib.util.spec_from_loader("login_expiry_check", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize("creds", ['{"claudeAiOauth": null}', '{"claudeAiOauth": 3}', "[]"])
def test_malformed_credentials_are_unreadable(check, tmp_path, creds):
    (tmp_path / ".credentials.json").write_text(creds)
    assert check.oauth_state(tmp_path) == ("unreadable", None)


def test_an_impossible_created_date_is_no_end_date(check, tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.write_text("# GS_FAKE: created 2026-13-45\nGS_FAKE=x\n")
    monkeypatch.setattr(check, "ENV_FILE", env)
    monkeypatch.setattr(check, "OPS", tmp_path)
    assert check.token_state("fake", "GS_FAKE") == ("no-end-date", None)


def _alert_run(check, tmp_path, monkeypatch, stdout, returncode=0):
    stamp = tmp_path / "stamp"
    monkeypatch.setattr(check, "STAMP", stamp)
    monkeypatch.setattr(check, "SEATS", {"jwork": {"dir": ".none"}})
    monkeypatch.setattr(check, "HOME", tmp_path)
    monkeypatch.setattr(check.sys, "argv", ["check", "--alert"])
    monkeypatch.setattr(
        check.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout=stdout, stderr=""),
    )
    assert check.main() == 1
    return stamp


def test_a_skipped_alert_is_not_stamped(check, tmp_path, monkeypatch):
    assert not _alert_run(check, tmp_path, monkeypatch, "").exists()


def test_a_sent_alert_is_stamped(check, tmp_path, monkeypatch):
    assert _alert_run(check, tmp_path, monkeypatch, "alert: sent 'x' to y (HTTP 200)\n").exists()
