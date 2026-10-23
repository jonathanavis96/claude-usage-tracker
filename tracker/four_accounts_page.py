"""What the publisher and the invariants read of the four-account detector's run: stdlib only.

The detector (tracker/four_accounts.py) needs numpy; the publisher, the invariants and every
tool that imports them do not, and must not start to (docs/adr/0001, rule 22). Everything
they read of a run -- the change records, the events, the headline, the window regimes, the
public block and which chart steps where -- lives here, on the run's JSON alone.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .atomic import write_text_atomic

UTC = timezone.utc
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
SCHEMA = 1
METHOD = "four_account_shared_step"

#: The three lines, the two the search runs on, and what each is.
LINES = ("seven_day", "ratio", "five_hour")
SEARCHED = ("seven_day", "ratio")
LINE_NAMES = {
    "seven_day": "seven-day meter: credits per 1%",
    "ratio": "five-hour points per seven-day point, headless work at the fitted factor",
    "five_hour": "five-hour meter: credits per 1%, headless work at the fitted factor",
}
#: The published metric each line's size is stated as (`events`, the window regimes).
LINE_METRIC = {"seven_day": "weekly_limit", "ratio": "windows_per_week",
               "five_hour": "five_hour_limit"}
#: The lines whose readings are valued in credits: a rate's uncertainty moves their sizes.
CREDIT_LINES = ("seven_day", "five_hour")

#: What a page says this method is, in one sentence.
SENTENCE = ("Each Max account is its own ruler: when every account gets the same percentage more "
            "or less work per 1% of a meter at the same moment, and shuffling their days cannot "
            "make a jump that big more than once in a thousand tries, the limit changed.")

#: The smallest change the rule catches 95% of the time, measured by injecting a step into
#: every account's real readings (the 2026-10-23 re-check of the rule with its own sides, and
#: the synthesis of the noise studies, on the records with the masterrig-local work in). Not
#: re-measured at each run: `measured_on` dates it. Percent, rises and falls apart; None means
#: not caught 95% of the time by `tested_up_to_pct`.
_NOT = {"rise": None, "fall": None}
SENSITIVITY = {
    "measured_on": "2026-10-23",
    "method": ("a step injected into every account's real readings at every six-hour start, the "
               "rule run 7 or 14 days later with its own sides; a hit is a flag within a day of "
               "the start, in the right direction"),
    "tested_up_to_pct": 50,
    "all_four_accounts_voting": {
        "starts": "October, when all four accounts had readings on both sides",
        "seven_day": {"7d": {"rise": None, "fall": 40}, "14d": {"rise": 30, "fall": 30}},
        "ratio": {"7d": {"rise": 30, "fall": 30}, "14d": {"rise": 20, "fall": 15}},
        "five_hour": {"7d": dict(_NOT), "14d": dict(_NOT)},
    },
    "whole_history": {
        "starts": "12 September to 15 October; in September at most two accounts voted",
        "seven_day": {"7d": dict(_NOT), "14d": dict(_NOT)},
        "ratio": {"7d": dict(_NOT), "14d": {"rise": None, "fall": 40}},
        "five_hour": {"7d": dict(_NOT), "14d": dict(_NOT)},
    },
    "false_alarms_per_start": [0.0, 2.8],
    "whole_run_false_alarms": {"flagged": 2, "of": 20,
                               "note": ("histories with every account's readings shifted in time "
                                        "by its own random offset, the two lines searched: 2 of 20 "
                                        "with the 9 October ratio step left in as noise, 0 of 20 "
                                        "with it divided out. The 7-day hold is what keeps such a "
                                        "flag from becoming measured")},
}


def hours(dt: datetime) -> float:
    return (dt - EPOCH).total_seconds() / 3600.0


def from_hours(h: float) -> datetime:
    return EPOCH + timedelta(hours=float(h))


def stamp(h: float) -> str:
    return from_hours(h).replace(microsecond=0).isoformat()


def parse(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def pct(x: float) -> float:
    """A log step as a percent, one decimal."""
    return round(100 * math.expm1(x), 1)


def p_value(p: float | None) -> float | None:
    """A permutation p as published: four significant figures, so 4/4001 reads 0.0009998
    (under the 0.001 it passed), never a rounded 0.001."""
    return None if p is None else float(f"{p:.4g}")


def public_label(label: str) -> str:
    """`a2` as the page names it: "Max account 2"."""
    return f"Max account {label[1:]}" if label[:1] == "a" and label[1:].isdigit() else label


# ------------------------------------------------------------------------------- which line moved
def moved(size: dict | None) -> bool:
    """Whether a line's size at a change says the line moved: its interval (widened by the
    line's coverage factor and by the rates' uncertainty) excludes no change, and its voting
    accounts agree (a step every account takes on that line)."""
    size = size or {}
    return bool(size.get("excludes_no_change")) and size.get("agree") is not False


def line_moved(change: dict, line: str) -> bool:
    """Whether `line` moved at `change`. A measured change's answer is the one decided on the
    day it was measured (`decided`) and never re-decided: the interval factor and the sizes are
    re-estimated every run, and an interval near zero would otherwise flip what the charts draw
    and the email's scope from one day to the next. Before it is measured, today's sizes."""
    decided = (change.get("decided") or {}).get("moved") or {}
    if line in decided:
        return bool(decided[line])
    return moved((change.get("sizes") or {}).get(line))


def reading(change: dict) -> str:
    """Which limit moved, from the three lines (`line_moved`): the ratio moved: one limit
    against the other; both credit lines moved the same way and the ratio did not: both
    limits; otherwise the figures alone."""
    sizes = change.get("sizes") or {}
    if line_moved(change, "ratio"):
        return "one_limit"
    s5, s7 = sizes.get("five_hour") or {}, sizes.get("seven_day") or {}
    if line_moved(change, "five_hour") and line_moved(change, "seven_day") \
            and s5.get("change_pct") is not None and s7.get("change_pct") is not None \
            and (s5["change_pct"] > 0) == (s7["change_pct"] > 0):
        return "both_limits"
    return "figures_only"


def drawn(change: dict, line: str) -> bool:
    """Whether every chart steps on `line` at `change`: always on the line that found it (the
    change's own figure), and on another line where that line moved (`line_moved`). Where
    neither holds the level is carried: the line is not shown to have moved there, and its
    size stays published as a figure with its interval."""
    return change["found_on"] == line or line_moved(change, line)


def drawn_step(change: dict, line: str) -> float:
    """The log step every chart draws on `line` at `change` (`drawn`), else 0."""
    size = (change.get("sizes") or {}).get(line) or {}
    d = size.get("_d")
    if d is None and size.get("change_pct") is not None:
        d = math.log1p(size["change_pct"] / 100)
    return d if d is not None and drawn(change, line) else 0.0


def levels(changes: list[dict], line: str) -> list[dict]:
    """The shared step line: the current segment at 1.0, each earlier one the next over 1 +
    the step drawn at the change between them (`drawn_step`)."""
    ordered = sorted(changes, key=lambda c: parse(c["at"]))
    bounds = [None] + [c["at"] for c in ordered] + [None]
    index, vals = 1.0, [1.0]
    for c in reversed(ordered):
        index = index / math.exp(drawn_step(c, line))
        vals.append(index)
    vals = list(reversed(vals))
    return [{"from": bounds[i], "to": bounds[i + 1], "index": round(vals[i], 4),
             "opened_by": ordered[i - 1]["state"] if i else None}
            for i in range(len(ordered) + 1)]


# ------------------------------------------------------------------------------- change records
#: How the page names each line's metric in a change record (`metric`), and in a label.
HEADLINE_NAMES = {"weekly_limit": "Weekly limit", "windows_per_week": "Windows per week",
                  "five_hour_limit": "Five-hour limit"}
#: A change's `scope`, from which direct limits moved (`certified_on`).
SCOPES = {("five_hour_limit",): "five_hour", ("weekly_limit",): "weekly",
          ("five_hour_limit", "weekly_limit"): "both"}


def page_round(x: float) -> int:
    """Whole percent as the page rounds it (JavaScript Math.round: halves go up)."""
    return math.floor(x + 0.5)


def _labelled(value, labels: dict[str, str]):
    """`value` with every account key and account-list entry turned into its page label."""
    if isinstance(value, dict):
        return {labels.get(k, k): _labelled(v, labels) for k, v in value.items()}
    if isinstance(value, list):
        return [labels.get(v, v) if isinstance(v, str) else _labelled(v, labels) for v in value]
    return value


def account_labels(block: dict) -> dict[str, str]:
    """Each account key of a run (`a1`...) to its page label (`Max account 1`...)."""
    return {a: row.get("label") or public_label(a) for a, row in (block.get("accounts") or {}).items()}


def certified_on(change: dict) -> list[str]:
    """The direct limits that moved at a change (`line_moved`)."""
    return [m for m, line in (("five_hour_limit", "five_hour"), ("weekly_limit", "seven_day"))
            if line_moved(change, line)]


def change_record(change: dict, labels: dict[str, str]) -> dict:
    """One change as `events` and `last_change` publish it.

    The figure is the change's size on the line that found it (`metric`): the seven-day line
    states the weekly limit, the ratio windows per week. Each direct limit's own size and
    interval rides beside it, and `scope` names the direct limits that moved
    (`windows_per_week` when only the ratio did). The interval is the published one: widened
    by the line's coverage factor and, on a credit line, by the rates' uncertainty
    (`rate_dependence`, the families whose rate interval moves the size most, and the
    statistical interval alone). Accounts appear only by label.
    """
    sizes = change.get("sizes") or {}
    found = sizes.get(change["found_on"]) or {}
    pct_ = found.get("change_pct")
    metric = LINE_METRIC[change["found_on"]]
    certified = certified_on(change)
    scope = SCOPES.get(tuple(certified)) or (
        "windows_per_week" if line_moved(change, "ratio") else "undetermined")

    def line(key: str) -> tuple:
        x = sizes.get(key) or {}
        return x.get("change_pct"), x.get("interval_pct")
    five, seven, ratio = line("five_hour"), line("seven_day"), line("ratio")
    span = change.get("instant_range") or [change["at"], change["at"]]
    return {
        "date": change["at"][:10], "at": change["at"], "state": change["state"], "kind": "change",
        "direction": "increased" if (pct_ or 0) > 0 else "decreased",
        "percent": abs(page_round(pct_)) if pct_ is not None else None,
        "change_pct": pct_, "interval_pct": found.get("interval_pct"),
        "statistical_interval_pct": found.get("statistical_interval_pct"),
        "interval_excludes_no_change": found.get("excludes_no_change"),
        "rate_dependence": _labelled(found.get("rate_dependence"), labels),
        "metric": metric, "found_on": change["found_on"], "line": LINE_NAMES[change["found_on"]],
        "scope": scope, "certified_on": certified, "reading": change.get("reading"),
        "five_hour_limit_change_pct": five[0], "five_hour_limit_change_interval_pct": five[1],
        "weekly_limit_change_pct": seven[0], "weekly_limit_change_interval_pct": seven[1],
        "windows_per_week_change_pct": ratio[0], "windows_per_week_change_interval_pct": ratio[1],
        "accounts": [labels.get(a, a) for a in change.get("accounts") or []],
        "per_account": {labels.get(a, a): x["change_pct"]
                        for a, x in (found.get("per_account") or {}).items()},
        "instant_range": span, "onset": {"earliest": span[0][:10], "latest": span[1][:10]},
        "first_found": change.get("first_found"), "measured_on": change.get("measured_on"),
        "held_days": change.get("held_days"), "hold_replayed": bool(change.get("hold_replayed")),
        "p": change.get("p"),
        "method": METHOD, "model": None, "observation_scope": "account",
        "evidence_quality": change["state"], "provisional": False, "legacy_uncertain": False,
        "label": (f"{HEADLINE_NAMES[metric]} {pct_:+g}% ({change['state']})"
                  if pct_ is not None else f"{HEADLINE_NAMES[metric]} ({change['state']})"),
    }


def measured_changes(block: dict | None) -> list[dict]:
    """The run's measured changes, oldest first."""
    return [c for c in (block or {}).get("changes") or [] if c.get("state") == "measured"]


def events(block: dict | None) -> list[dict]:
    """`events`: every measured change as a change record, oldest first."""
    labels = account_labels(block or {})
    return [change_record(c, labels) for c in measured_changes(block)]


def last_change(block: dict | None) -> dict | None:
    """`last_change`: the newest measured change, or None. Never a candidate or a provisional
    one: the page states it as what Anthropic last did to the limits."""
    found = events(block)
    if not found:
        return None
    return {k: v for k, v in max(found, key=lambda e: e["at"]).items() if k not in ("kind", "label")}


def regime_changes(block: dict | None) -> list[dict]:
    """The measured changes as the window and week regimes read them, oldest first.

    Each opens a regime at its instant. `window_scaled` (and `ratio`, its interval) is the
    five-hour size where the charts draw it (`drawn`), `weekly` the seven-day size and
    `windows_per_week` the ratio's, each with `drawn`. A size the charts do not draw moves
    nothing on its chart: the level is carried across the change.
    """
    out = []
    for c in measured_changes(block):
        sizes = c.get("sizes") or {}

        def size(line: str) -> dict:
            x = sizes.get(line) or {}
            return {"drawn": drawn(c, line) and x.get("change_pct") is not None,
                    "excludes_no_change": bool(x.get("excludes_no_change")),
                    "change_pct": x.get("change_pct"), "interval_pct": x.get("interval_pct")}
        five = size("five_hour")
        lo, hi = five["interval_pct"] or (0.0, 0.0)
        scaled = five["drawn"]
        out.append({"at": parse(c["at"]), "family": None, "found_on": c["found_on"],
                    "change_pct": five["change_pct"] if scaled else 0.0,
                    "scope": c.get("reading") or "undetermined", "window_scaled": scaled,
                    "ratio": 1 + (five["change_pct"] / 100 if scaled else 0.0),
                    "ratio_interval": (1 + lo / 100, 1 + hi / 100) if scaled else (1.0, 1.0),
                    "five_hour": five, "weekly": size("seven_day"),
                    "windows_per_week": size("ratio")})
    return sorted(out, key=lambda c: c["at"])


def public_block(block: dict | None) -> dict | None:
    """The `four_accounts` block of the public JSON: the newest daily run, every account by
    its page label, with the method sentence and the measured sensitivity."""
    if not block:
        return None
    labels = account_labels(block)
    order = sorted(labels, key=lambda a: (len(a), a))
    return {
        "schema": block["schema"], "method": block["method"], "sentence": SENTENCE,
        "day": block["day"], "data_until": block["data_until"],
        "replayed": bool(block.get("replayed")),
        "accounts": [labels[a] for a in order],
        "account_lines": {labels[a]: block["accounts"][a]["lines"] for a in order},
        "lines_searched": list(SEARCHED), "line_names": dict(LINE_NAMES),
        "line_metrics": dict(LINE_METRIC),
        "rule": block["rule"], "valuation": block["valuation"],
        "headless_factor": block["headless_factor"],
        "interval_coverage": block["interval_coverage"],
        "changes": [dict(_labelled({k: v for k, v in c.items() if k != "accounts"}, labels),
                         accounts=[labels.get(a, a) for a in c.get("accounts") or []])
                    for c in block["changes"]],
        "withdrawn": block["withdrawn"],
        "not_limit_changes": _labelled(block["not_limit_changes"], labels),
        "lines": {line: {labels.get(a, a): rows for a, rows in by.items()}
                  for line, by in block["lines"].items()},
        "line_columns": ["day", "level", "points", "level_pooled_7d"],
        "levels": block["levels"],
        "sensitivity": SENSITIVITY,
    }


# ------------------------------------------------------------------------------- state
def load_state(path: Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return body if isinstance(body, dict) and body.get("schema") == SCHEMA else None


def write_state(path: Path, state: dict) -> None:
    write_text_atomic(Path(path), json.dumps(state, indent=1, sort_keys=True) + "\n")
