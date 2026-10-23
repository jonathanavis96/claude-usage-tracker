"""Record how each advisory step of bin/daily.sh ended, for tracker.health's `advisory` check.

    python3 -m tracker.advisory --step tools.model_rates --exit 1

bin/daily.sh runs five steps whose failure must never stop the publish: tracker.list_prices,
tracker.contributed, tracker.gs_passive, the tools.model_rates refit and the
tracker.four_accounts daily run. Each failure is one warning line in the run log and the
publish carries on with the step's previous output.
That is right for one run and wrong for nine days: from 2026-10-13T19:30Z the refit crashed
on every run, 48 a day, the page kept the 13 October rates, and only the warning line said
so. This keeps, per step, when it last ran, its exit, and since when it has failed on every
run (`failing_since`, cleared by its next success). tracker.health fails `advisory` once a
step has been failing for its grace period, and the supervisor alerts on that like any
other health failure, each step its own incident.

State: ~/.paperclip/ops/claude-usage-advisory.json (health.GS_ADVISORY), written atomically.
Never fails the caller: a state file it cannot write is one stderr line and exit 0.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from tracker import health
from tracker.atomic import write_text_atomic


#: Exit codes a step uses for an outcome that is not a failure. tools.model_rates exits 3 when
#: it holds a refit that would move a published rate outside its interval (the new fit goes to
#: history/model-rates.candidate.json and the rates in force stay) and 4 while that hold
#: stands. The refit ran and did its job; counting it a failure would open an "advisory"
#: incident three hours into every hold, on top of the one alert bin/daily.sh sends for it.
HELD_EXITS: dict[str, frozenset[int]] = {"tools.model_rates": frozenset({3, 4})}


def record(state_path: Path, step: str, code: int, now: float) -> dict:
    """Record one run of `step` that exited `code`; returns that step's record."""
    state = health.read_state(state_path)
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    rec = steps.get(step) if isinstance(steps.get(step), dict) else {}
    rec.update(last_run=now, last_exit=code)
    if code == 0 or code in HELD_EXITS.get(step, frozenset()):
        rec["last_ok"] = now
        rec.pop("failing_since", None)
    else:
        rec.setdefault("failing_since", now)
    steps[step] = rec
    state["steps"] = steps
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(state_path, json.dumps(state, indent=1, sort_keys=True))
    return rec


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--step", required=True, help="the step's module, as daily.sh names it")
    p.add_argument("--exit", type=int, required=True, dest="code", help="the step's exit status")
    p.add_argument("--state", type=Path, default=health.GS_ADVISORY)
    a = p.parse_args(argv)
    try:
        record(a.state, a.step, a.code, time.time())
    except OSError as e:
        print(f"advisory: cannot record {a.step} in {a.state}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
