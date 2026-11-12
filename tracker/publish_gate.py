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

An incident that withholds the page (tracker.invariants' blocking checks) is said again,
because the page has stopped updating: once when it starts withholding, even inside an
incident already open on checks that only warn, and every REMIND_AFTER_S while it lasts.
On 2026-10-05 the invariants incident opened on two advisory checks ("Published anyway");
from 2026-10-13T11:00Z blocking checks withheld the page for nine days and nothing more
was sent. A change in WHICH checks fail is not a new alert: from 4 to 22 October the
failing set changed 19 times inside that one incident.
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


#: How often an incident that withholds the page is said again while it lasts.
REMIND_AFTER_S = 24 * 60 * 60


def _ago(seconds: float) -> str:
    h = int(seconds // 3600)
    return f"{h} h" if h else f"{int(seconds // 60)} min"


def track_incident(state_path: Path, send: supervise.Sender, ok: bool, summary: str,
                   failed_text: str, recovered_text: Callable[[int, str], str],
                   now: Callable[[], float] = time.time, blocking: bool = False,
                   withheld_text: Callable[[float], str] | None = None) -> str:
    """The incident semantics every publish-time alert here shares; returns what it did.

    One alert (`failed_text`) when a check starts failing, nothing while it keeps failing,
    one recovery (`recovered_text(minutes, reason)`) when it passes again. A send that fails
    leaves the incident unopened (or open), so the next run tries again. State lives in
    `state_path`. tracker.invariants uses it too.

    `blocking` says this run's failure withholds the page. Then `withheld_text(since)` is
    sent when an open incident starts withholding, and again every REMIND_AFTER_S after the
    last such message while it does; an incident that opens withholding says so in
    `failed_text`, which counts as that message. A run that publishes again inside the
    incident sends nothing, and a withhold that comes back within REMIND_AFTER_S of the last
    message waits for the reminder, so a check flapping across the line cannot send one
    message a run. The line returned is for the caller's log, which used to print
    "(alerted)" on every run whether or not anything was sent.
    """
    state = health.read_state(state_path)
    t = now()
    state.update(last_run=t, last_summary=summary, last_result="pass" if ok else "fail")
    did = "no alert"
    if not ok and not state.get("incident_open"):
        if send(failed_text):
            state.update(incident_open=True, incident_since=t, incident_reason=summary)
            if blocking:
                state.update(withheld_since=t, withheld_alerted_at=t)
            did = "alert sent"
        else:
            did = "alert NOT sent (every channel failed), the next run tries again"
    elif not ok and blocking and withheld_text is not None:
        state.setdefault("withheld_since", t)
        last = state.get("withheld_alerted_at")
        if last is None or t - last >= REMIND_AFTER_S:
            kind = "withheld alert" if last is None else "withheld reminder"
            if send(withheld_text(state["withheld_since"])):
                state["withheld_alerted_at"] = t
                did = f"{kind} sent"
            else:
                did = f"{kind} NOT sent (every channel failed), the next run tries again"
        else:
            did = (f"incident open {_ago(t - state.get('incident_since', t))}, withheld "
                   f"{_ago(t - state['withheld_since'])}, last said {_ago(t - last)} ago")
    elif not ok:
        # Publishing again inside the incident: this withhold is over. Its last message is
        # kept, so a quick return to withholding waits for the reminder.
        state.pop("withheld_since", None)
        did = f"incident open {_ago(t - state.get('incident_since', t))}, already alerted"
    elif state.get("incident_open"):
        mins = int((t - state.get("incident_since", t)) // 60)
        if send(recovered_text(mins, state.get("incident_reason"))):
            for k in ("incident_open", "incident_since", "incident_reason", "withheld_since",
                      "withheld_alerted_at"):
                state.pop(k, None)
            did = "recovery sent"
        else:
            did = "recovery NOT sent (every channel failed), the next run tries again"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    supervise.write_state(state_path, state)
    return did


def gate(state_path: Path, send: supervise.Sender, runner: Runner = pytest_runner,
         files: tuple[str, ...] = GATE_TESTS, timeout: float = 60.0,
         now: Callable[[], float] = time.time) -> int:
    code, summary = runner(list(files), timeout)
    did = track_incident(
        state_path, send, code == 0, summary,
        f"Claude usage tracker (gs publisher) test gate failed: {summary}. "
        f"Published anyway. Run: cd ~/claude-usage-tracker && python3 -m pytest -q {' '.join(files)}",
        lambda mins, was: (f"Claude usage tracker (gs publisher) test gate passes again after "
                           f"{mins} min (was: {was})"),
        now)
    print(f"publish gate: {'pass' if code == 0 else 'FAIL'}: {summary} ({did})")
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
