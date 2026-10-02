"""Red-team 2: one open incident hides a second account's fault.

UT-I's fix for the red-team's incident masking (tracker/supervise.py decide_alert) keys an
incident by its kind: "runs", or the failing check's name. On gs the `meters` check covers
all three meter logs and returns only the first failing one, so every account shares the
one kind "meters". The same holds for `publisher` on masterrig (a stale gs commit and a
failing site push are both "publisher").
"""
from __future__ import annotations

from tracker import supervise

T0 = 1_790_000_000.0


def test_second_account_fault_during_meter_incident_is_alerted():
    sent: list[str] = []

    def send(text: str) -> bool:
        sent.append(text)
        return True

    dave = "meters: newest usable reading in claude-usage-meter-dave.log is 1h20m old (limit 15m)"
    state: dict = {"consecutive_failures": 0}
    supervise.decide_alert(state, T0, dave, 2, 3600.0, send, "gs")
    supervise.decide_alert(state, T0 + 3600, dave, 2, 3600.0, send, "gs")
    assert len(sent) == 1 and "dave" in sent[0] and state.get("incident_open"), "precondition: dave's incident opened"

    # dave's timer is fixed; avis's meter has meanwhile stopped too. The check now names avis.
    avis = "meters: newest usable reading in claude-usage-meter-avis.log is 1h05m old (limit 15m)"
    for k in range(2, 6):   # four more half-hourly runs
        supervise.decide_alert(state, T0 + k * 1800 + 1800, avis, 2, 3600.0, send, "gs")
    assert any("avis" in s for s in sent[1:]), (
        "avis's meter failed while dave's incident was open: no message names it, and no recovery was "
        f"sent for dave either, so the fix for dave reads as not working. Sent: {sent}")
