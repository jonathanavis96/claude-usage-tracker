"""Report: the human-readable half of a probe failure alert.

`bin/probe.sh` and `bin/output-probe.sh` mail Jonathan when `tracker.probe` writes
no row. Until 2026-09-13 that mail was an exit code and the last twenty log lines,
which answered none of the questions a reader has: did it succeed, why did it stop,
is anything broken, does it need a hand. This module turns the probe's own stderr
(kept in `out/probe-last.log`) plus the exit code into a plain-language summary, and
appends the raw log below it for the debugging that the summary cannot do.

The summary is derived only from what the log says. Each explanation names the
condition that actually tripped (`ProbeAbort` reasons in tracker/probe.py, the
`<account> busy:` lines from `choose_account`, a Python traceback for a crash), and
an unrecognised log falls back to quoting its last line rather than guessing.

CLI:  python3 -m tracker.report --model M --rc N --what "Rotation run" --log PATH
      [--payload output] [--tail 20]
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

# Mirrors tracker/probe.py: RESET_WAIT_S and the --max-wait default. Quoted in prose
# so the reader knows the thresholds without opening the code.
RESET_WAIT_MIN = 20
MAX_WAIT_H = 4

READING_RE = re.compile(
    r"^(?:\S+Z )?prompt (?P<n>\d+)(?: \(burst of (?P<k>\d+)\))?: five_hour=(?P<fh>[\d.]+|None) "
    r"resets_at=(?P<reset>\S+)")
BUSY_RE = re.compile(r"^(?:\S+Z )?(?P<name>\w+) busy: (?P<why>.+)$")
ABORT_RE = re.compile(r"^(?:\S+Z )?probe aborted on (?P<name>\w+): (?P<why>.+)$")
WAIT_RE = re.compile(r"^(?:\S+Z )?window resets in (?P<s>\d+)s: waiting for it")
# A traceback ends with the exception line: the first unindented line after the
# "Traceback" header. Matching on that position rather than the class name catches
# probe.ProbeAbort and subprocess.TimeoutExpired, which carry no Error/Exception suffix.
TRACEBACK_HEADER = "Traceback (most recent call last):"
EXC_RE = re.compile(r"^(?:[\w.]+\.)?\w*(?:Error|Exception|Exit|Interrupt)\b.*$")


def model_label(model: str) -> str:
    """`claude-sonnet-5` -> `Sonnet 5`, `claude-fable-5-1` -> `Fable 5.1`."""
    parts = model.removeprefix("claude-").split("-")
    if not parts or not parts[0]:
        return model
    name = parts[0].capitalize()
    version = ".".join(p for p in parts[1:] if p)
    return f"{name} {version}".strip()


def _pct(v: str | None) -> str:
    if v is None or v == "None":
        return "an unknown percent"
    return f"{float(v):g}%"


def _reset_clock(stamp: str) -> str:
    """`2026-09-13T12:30:00.111468+00:00` -> `12:30 UTC`; anything else verbatim."""
    m = re.match(r"\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})", stamp)
    return f"{m.group(1)} UTC" if m else stamp


def _parse(log_text: str) -> dict:
    readings, busy, abort, waited, exc = [], [], None, None, None
    in_traceback = False
    for raw in log_text.splitlines():
        line = raw.rstrip()
        if line == TRACEBACK_HEADER:
            in_traceback = True
        elif in_traceback and line and not line.startswith(" "):
            in_traceback = False
            exc = line
        elif (m := READING_RE.match(line)):
            readings.append(m.groupdict())
        elif (m := BUSY_RE.match(line)):
            busy.append((m.group("name"), m.group("why")))
        elif (m := ABORT_RE.match(line)):
            abort = (m.group("name"), m.group("why"))
        elif (m := WAIT_RE.match(line)):
            waited = int(m.group("s"))
        elif EXC_RE.match(line) and not line.startswith(" "):
            exc = line
    return {"readings": readings, "busy": busy, "abort": abort, "waited": waited, "exc": exc}


def _progress(readings: list[dict]) -> str:
    """One sentence on how far the meter got, from the reading lines."""
    if not readings:
        return "The log shows no meter readings, so the run stopped before its first prompt landed."
    prompts = readings[-1]["n"]
    first = readings[0]["fh"]
    # The reading before a reset shows the old window's high-water mark; the reset
    # reading itself is 0.0, so take the highest value seen rather than the last.
    values = [float(r["fh"]) for r in readings if r["fh"] != "None"]
    high = f"{max(values):g}%" if values else "an unknown percent"
    return (f"The probe had sent {prompts} prompt{'s' if prompts != '1' else ''} and moved the "
            f"5-hour meter from {_pct(first)} to {high}.")


def _abort_explanation(why: str, parsed: dict) -> str:
    readings = parsed["readings"]
    if why.startswith("window reset during probe"):
        due = _reset_clock(readings[0]["reset"]) if readings else "its scheduled time"
        # The reset reading is whatever the first prompt of the new window left on the
        # meter: usually 0%, but a heavy prompt can land at 1% or more.
        after = _pct(readings[-1]["fh"]) if readings else "0%"
        return (f"Then the account's 5-hour usage window reset (it was due at {due}) and the "
                f"meter dropped to {after}. A measurement cannot span a reset, so the probe threw "
                f"the run away.\n\n"
                f"Why it was not avoided: at start the probe waits only for a reset that is "
                f"less than {RESET_WAIT_MIN} minutes away. This run began further out than "
                f"that and still did not finish in time, so the wait threshold is shorter "
                f"than the probe itself. That is a design limit, not a one-off glitch: any "
                f"run that starts between {RESET_WAIT_MIN} minutes and its own duration "
                f"before a reset will end this way.")
    if "account not idle" in why or why.startswith("utilization jumped"):
        return (f"Then the meter rose more than the probe's own prompts can explain "
                f"({why}). Someone else was using the account at the same time, and a shared "
                f"reading would be contaminated, so the probe stopped.")
    if why == "deadline":
        return (f"Then the run used up its whole wall-clock budget ({MAX_WAIT_H} hours, shared "
                f"between waiting for an idle account and probing) before its last tick.")
    if why == "tick too early":
        return ("Then the meter ticked after less spend than the model's list price allows. "
                "That reading cannot be right, so the probe did not trust it.")
    if why.startswith("no second tick"):
        return f"Then the run hit its prompt limit without the meter moving again ({why})."
    return f"The probe gave up with: {why}"


def _refused_to_start(log_text: str) -> str | None:
    """Exit 4 before any account was chosen: a sizing or price refusal in main()."""
    for line in reversed(log_text.splitlines()):
        if "fits fewer than" in line:
            return ("The probe refused to start: the rotation's expectation for this model "
                    "is too low to size a well-averaged run (" + line.strip() + ").")
        if line.startswith("no price for"):
            return f"The probe refused to start: {line.strip()}."
    return None


def summary(model: str, rc: int, what: str, log_text: str, payload: str = "prose") -> str:
    """The plain-language part of the alert body, no log attached."""
    label = model_label(model)
    kind = f"{what} for {label}" + (" (output payload)" if payload == "output" else "")
    parsed = _parse(log_text)
    keeps = ("The output class weight keeps its current value until the next weekly run."
             if payload == "output" else
             "The next scheduled slot tries again on whichever account is idle.")

    if rc == 3:
        names = sorted({n for n, _ in parsed["busy"]})
        last_why = {n: w for n, w in parsed["busy"]}
        who = "; ".join(f"{n} was busy ({last_why[n]})" for n in names) if names else \
            "the log does not say which accounts were checked"
        pids = any("pid" in w for _, w in parsed["busy"])
        hint = (" A `pid` reason means a Claude session was running on that account; if "
                "those sessions are stale, closing them frees the account." if pids else "")
        return (f"Outcome: SKIPPED. {kind} never started, because no account was idle within "
                f"the {MAX_WAIT_H}-hour wait. No measurement was recorded.\n\n"
                f"Who was busy: {who}.{hint}\n\n"
                f"What happens next: nothing is broken. {keeps}")

    if rc == 4:
        head = f"Outcome: FAILED. {kind} stopped before it finished. No measurement was recorded."
        if parsed["abort"] is None:
            body = _refused_to_start(log_text) or \
                "The log does not show why the probe stopped; see the debug log below."
            return f"{head}\n\nWhat happened: {body}\n\nWhat happens next: {keeps}"
        account, why = parsed["abort"]
        waited = parsed["waited"]
        waited_note = (f" It first waited {waited // 60} minutes for a window reset." if waited else "")
        return (f"{head}\n\n"
                f"What happened: it ran on the \"{account}\" account.{waited_note} "
                f"{_progress(parsed['readings'])} {_abort_explanation(why, parsed)}\n\n"
                f"What happens next: nothing needs fixing by hand. {keeps} The tokens spent on "
                f"this run are the only loss.")

    exc = parsed["exc"]
    error = f"Last error: {exc}" if exc else "The log shows no Python error line; see the debug log below."
    hint = ""
    if exc and "401" in exc:
        hint = (" HTTP 401 means the usage API refused the account's login token, so the token "
                "in that account's .credentials.json has probably expired. Running any Claude "
                "command as that account normally refreshes it.")
    return (f"Outcome: CRASHED. {kind} exited with code {rc}, an error the probe does not "
            f"handle. No measurement was recorded.\n\n"
            f"What happened: {_progress(parsed['readings'])} {error}.{hint}\n\n"
            f"What happens next: this needs a look. The probe will hit the same error again "
            f"if the cause persists, and it sends no alert for exit codes it does not know "
            f"unless the wrapper catches them, as this one did.")


def report(model: str, rc: int, what: str, log_text: str, *, log_name: str = "out/probe-last.log",
           payload: str = "prose", tail: int = 20) -> str:
    """Summary first, then the raw log tail under a rule, for the alert body."""
    lines = log_text.rstrip("\n").splitlines()
    shown = lines[-tail:] if tail else lines
    header = (f"Debug log (last {len(shown)} of {len(lines)} lines of {log_name}; "
              f"tracker.probe exit code {rc}):")
    return summary(model, rc, what, log_text, payload) + "\n\n----\n" + header + "\n" + "\n".join(shown) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Compose the body of a probe failure alert")
    ap.add_argument("--model", required=True)
    ap.add_argument("--rc", type=int, required=True, help="tracker.probe's exit code")
    ap.add_argument("--what", required=True, help='e.g. "Rotation run", "Drift rerun"')
    ap.add_argument("--log", type=Path, required=True, help="the probe's captured stderr")
    ap.add_argument("--payload", default="prose", choices=("prose", "output"))
    ap.add_argument("--tail", type=int, default=20)
    a = ap.parse_args(argv)
    try:
        text = a.log.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        text = f"(could not read {a.log}: {e})"
    sys.stdout.write(report(a.model, a.rc, a.what, text, log_name=str(a.log), payload=a.payload,
                            tail=a.tail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
