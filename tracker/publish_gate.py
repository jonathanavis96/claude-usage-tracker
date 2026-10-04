"""Fast test gate the gs publisher runs before it commits tracker state.

    python3 -m tracker.publish_gate            # exit 0 pass, 1 fail; one summary line

On 2026-09-29 tracker.list_prices filed a claude-sonnet-5-5 price row that broke 11
tests, and bin/daily.sh committed it and kept publishing for days with nobody told.
This runs the test files that read the data the publisher writes (GATE_TESTS, about
25 s, hard limit --timeout 60 s) in one pytest process.

A failure does NOT block the publish: bin/daily.sh carries on, so the published
numbers keep flowing, and a broken data row is a regression to fix, not a reason to
freeze the page. What a failure does is alert, through the supervisor's sender
(tracker.supervise.default_sender: WhatsApp, or stderr under CUT_ALERT_DRY_RUN=1 or
--dry-run), with the same incident semantics: one alert when the gate starts failing,
nothing while it keeps failing, one recovery when it passes again. A send that fails
leaves the incident unopened, so the next run tries again. State lives in --state.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from tracker import health, supervise

ROOT = Path(__file__).resolve().parent.parent
GATE_TESTS = ("tests/test_list_prices.py", "tests/test_credits.py", "tests/test_publish.py",
              "tests/test_weight.py", "tests/test_contributed.py", "tests/test_gs_passive.py",
              "tests/test_prices_data.py")
STATE = health.GS_OPS / "claude-usage-publish-gate.json"
Runner = Callable[[list[str], float], tuple[int, str]]


def pytest_runner(files: list[str], timeout: float) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *files]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {int(timeout)} s"
    except OSError as e:
        return 1, f"cannot run pytest: {e}"
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    summary = lines[-1].strip("= ") if lines else (r.stderr.strip().splitlines() or ["no output"])[-1]
    return (0 if r.returncode == 0 else 1), summary


def track_incident(state_path: Path, send: supervise.Sender, ok: bool, summary: str,
                   failed_text: str, recovered_text: Callable[[int, str], str],
                   now: Callable[[], float] = time.time) -> None:
    """The incident semantics every publish-time alert here shares.

    One alert (`failed_text`) when a check starts failing, nothing while it keeps failing,
    one recovery (`recovered_text(minutes, reason)`) when it passes again. A send that fails
    leaves the incident unopened (or open), so the next run tries again. State lives in
    `state_path`. tracker.invariants uses it too.
    """
    state = health.read_state(state_path)
    t = now()
    state.update(last_run=t, last_summary=summary, last_result="pass" if ok else "fail")
    if not ok and not state.get("incident_open"):
        if send(failed_text):
            state.update(incident_open=True, incident_since=t, incident_reason=summary)
    elif ok and state.get("incident_open"):
        mins = int((t - state.get("incident_since", t)) // 60)
        if send(recovered_text(mins, state.get("incident_reason"))):
            for k in ("incident_open", "incident_since", "incident_reason"):
                state.pop(k, None)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    supervise.write_state(state_path, state)


def gate(state_path: Path, send: supervise.Sender, runner: Runner = pytest_runner,
         files: tuple[str, ...] = GATE_TESTS, timeout: float = 60.0,
         now: Callable[[], float] = time.time) -> int:
    code, summary = runner(list(files), timeout)
    track_incident(
        state_path, send, code == 0, summary,
        f"Claude usage tracker (gs publisher) test gate failed: {summary}. "
        f"Published anyway. Run: cd ~/claude-usage-tracker && python3 -m pytest -q {' '.join(files)}",
        lambda mins, was: (f"Claude usage tracker (gs publisher) test gate passes again after "
                           f"{mins} min (was: {was})"),
        now)
    print(f"publish gate: {'pass' if code == 0 else 'FAIL'}: {summary}")
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--state", type=Path, default=STATE)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    a = p.parse_args(argv)
    send = supervise.dry_run_sender if a.dry_run else supervise.default_sender()
    return gate(a.state, send, timeout=a.timeout)


if __name__ == "__main__":
    sys.exit(main())
