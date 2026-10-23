"""Publish-time invariants over the built public JSON, and the alert when one breaks.

    python3 -m tracker.invariants --json PATH   # exit 0 all pass, 1 any fail; one line each

Each check reads only the public JSON and returns plain-sentence failures, an empty list when
it holds. The changes the page states come from one place, the four-account detector
(`four_accounts`, tracker/four_accounts.py, ADR 0001 rule 22); the blocking checks hold every
other figure on the page to it.

1. `headline_inside_accounts`: in the newest regime, with two or more accounts with a
   window, the headline family's figure lies between the lowest and highest account level,
   scaled the way the page scales them, within HEADLINE_TOLERANCE for rounding.
2. `direct_means_direct`: a headline family measured directly publishes exactly the newest
   regime's `measured_value`.
3. `account_units`: each account's anchor-unit `window` times its regime's scale agrees with
   its direct `measured_window` within ACCOUNT_UNITS_TOLERANCE.
4. `headline_is_last_measured_change`: `last_change` is the detector's newest measured change
   (its instant, its line's metric, its size and the page's whole percent of it), and there
   is none when the detector has measured none.
5. `every_change_drawn`: every change the detector publishes, of any state, is drawn on the
   line that found it: the step line (`four_accounts.levels`) opens a segment at its instant
   and steps there by its size. Every published event is one of the detector's measured
   changes.
6. `one_figure_per_change`: every figure the page states for one measured change on one line
   is the same whole percent: the event and the headline, the step line, and each chart that
   draws the line (the window chart for the five-hour line, the tokens-per-week chart for the
   seven-day line, the windows-per-week chart and `weekly_windows` for the ratio, the dollar
   history for the five-hour line). A line the detector does not draw at a change
   (`four_accounts.drawn`) steps 0 there everywhere.
7. `regimes_open_at_measured_changes`: every regime boundary on the page is a measured
   change, and every measured change opens one: no chart steps anywhere the detector did not
   measure a change.
8. `accounts_named_by_label`: the detector's block and the change records name accounts only
   by their page labels ("Max account N"): never a login name and never a bare `aN` key.
9. `measured_changes_still_found`: every measured change was found again by today's run
   (`found_today`). A measured change is never withdrawn; this is the alert that says one
   stopped being found.
10. `detector_ran`: the detector's newest run is no more than a day older than the publish.

Checks 4-8 read the page's own figures the way it draws them (`chart_steps`); on 2026-10-05
the page said "+30%" in its headline and "+32%" on two charts for 22 September.

A failure of checks 1-3, 9 or 10 does NOT block the publish (exit 1). A failure of checks 4-8
(`BLOCKING`) does (exit 2): bin/daily.sh then puts the last published JSON back and commits
nothing to the site, so the page never states two figures for one change, and exits 9, a
failed run. bin/daily.sh runs this after the publish, beside tracker.publish_gate, with the
same incident semantics (`publish_gate.track_incident`): one alert when a check starts
failing, nothing while it keeps failing or the failing set changes, one recovery when they
all pass again. A withheld page is said on its own: when an incident open on checks that do
not block starts blocking, and every 24 hours while it blocks (from 2026-10-13 the page was
withheld nine days inside an incident opened on 5 October by two advisory checks, and
nothing was sent). The last line printed says what was sent. State lives in --state.

Superseded 2026-10-23 (ADR 0001 rule 22): the checks on the known-date candidates
(`no_unproven_step`, `steps_agree_with_meters`, `no_withheld_boundary`), the windows-per-week
identity (`windows_per_week_implied`: the three lines are measured each on its own), the two
weekly routes (`weekly_routes_agree`), the detected windows per week (`detected_windows_per_week_agree`)
and the frozen 14 September weekly size (`fourteen_sep_weekly_frozen`, rule 21).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import four_accounts as detector
from tracker import health, publish_gate, supervise
from tracker.publish import ACCOUNT_LABELS

#: Check 1's allowance either side of the account range, for the rounding of published figures.
HEADLINE_TOLERANCE = 0.05
#: Check 3's bound. One figure is relative to the pool and one is direct, so this is a
#: sanity bound on units, not an equality.
ACCOUNT_UNITS_TOLERANCE = 0.25
#: Check 5's allowance in percentage points between a change's one-decimal size and the step
#: its step line draws, whose levels are published to four decimals.
DRAWN_TOLERANCE_PP = 0.06
#: Check 10: how many days the detector's newest run may trail the publish.
DETECTOR_MAX_AGE_DAYS = 1
STATE = health.GS_OPS / "claude-usage-invariants.json"
#: The account names behind the labels, which no published change record may carry.
ACCOUNT_NAMES = tuple(name for name, _ in ACCOUNT_LABELS)
BARE_LABEL = re.compile(r"^a\d+$")


def _wt(doc: dict) -> dict:
    return (doc.get("credits") or {}).get("window_tokens") or {}


def _label(fam: str) -> str:
    head, *version = fam.split("-")
    return head.capitalize() + (" " + ".".join(version) if version else "")


def _same_instant(a: str | None, b: str | None) -> bool:
    return bool(a and b) and datetime.fromisoformat(a) == datetime.fromisoformat(b)


def _n(x: float) -> str:
    return f"{x:,.0f}"


def headline_family(doc: dict) -> str:
    """The family the page leads with: `window_tokens.measured_family` where it has a
    per-family row, else the anchor (the row whose `rate_source` is `anchor`, else Opus)."""
    wt = _wt(doc)
    per = wt.get("per_family") or {}
    fam = wt.get("measured_family")
    if fam in per:
        return fam
    return next((f for f, row in per.items() if row.get("rate_source") == "anchor"), "opus")


def _scale(doc: dict, fam: str) -> float | None:
    """How the page scales anchor-unit figures to `fam`: its headline over the anchor's."""
    wt = _wt(doc)
    top = ((wt.get("per_family") or {}).get(fam) or {}).get("all", {}).get("value")
    base = (wt.get("all") or {}).get("value")
    return top / base if top and base else None


def headline_inside_accounts(doc: dict) -> list[str]:
    """Check 1: the headline lies inside its accounts' scaled range in the newest regime."""
    wt = _wt(doc)
    fam = headline_family(doc)
    value = ((wt.get("per_family") or {}).get(fam) or {}).get("all", {}).get("value")
    scale = _scale(doc, fam)
    levels = {label: rows[-1]["window"] for label, rows in (wt.get("account_regimes") or {}).items()
              if rows and rows[-1].get("window") is not None}
    if value is None or scale is None or len(levels) < 2:
        return []
    lo_label = min(levels, key=lambda k: levels[k])
    hi_label = max(levels, key=lambda k: levels[k])
    lo, hi = levels[lo_label] * scale, levels[hi_label] * scale
    if lo * (1 - HEADLINE_TOLERANCE) <= value <= hi * (1 + HEADLINE_TOLERANCE):
        return []
    side, edge, label = (("above", hi, hi_label) if value > hi else ("below", lo, lo_label))
    return [(f"The headline {_label(fam)} window, {_n(value)} tokens, is {side} every account in "
             f"the newest regime: the accounts run {_n(lo)} ({lo_label}) to {_n(hi)} ({hi_label}) "
             f"scaled to {_label(fam)} by {scale:.4f}, and {label}'s {_n(edge)} is the nearest, "
             f"more than {HEADLINE_TOLERANCE:.0%} away.")]


def direct_means_direct(doc: dict) -> list[str]:
    """Check 3: a headline family measured directly is the newest regime's direct figure."""
    wt = _wt(doc)
    fam = headline_family(doc)
    row = (wt.get("per_family") or {}).get(fam) or {}
    if not row.get("measured_directly"):
        return []
    value = (row.get("all") or {}).get("value")
    newest = wt["regimes"][-1]
    if value == newest.get("measured_value"):
        return []
    return [(f"{_label(fam)} is published as measured directly at {value}, but the newest "
             f"regime's measured_value is {newest.get('measured_value')}.")]


def account_units(doc: dict) -> list[str]:
    """Check 4: each account's window, in its regime's family, agrees with its direct reading.

    The scale is the regime's own `measured_value` over its `value` (the page's scale for the
    newest regime); an account row is matched to its regime by `from`.
    """
    wt = _wt(doc)
    out = []
    for reg in wt["regimes"]:
        if not reg.get("measured_family") or not reg.get("value") or not reg.get("measured_value"):
            continue
        scale = reg["measured_value"] / reg["value"]
        for label, rows in sorted((wt.get("account_regimes") or {}).items()):
            row = next((r for r in rows if r.get("from") == reg.get("from")), None)
            if not row or row.get("window") is None or not row.get("measured_window"):
                continue
            got, direct = row["window"] * scale, row["measured_window"]
            if abs(got - direct) > ACCOUNT_UNITS_TOLERANCE * direct:
                out.append(f"{label} in the regime from {reg.get('from') or 'the start'}: its window "
                           f"{_n(row['window'])} times {scale:.4f} is {_n(got)} "
                           f"{_label(reg['measured_family'])} tokens, but its direct reading is "
                           f"{_n(direct)}, more than {ACCOUNT_UNITS_TOLERANCE:.0%} apart.")
    return out


def _fa(doc: dict) -> dict:
    return doc.get("four_accounts") or {}


def _measured(doc: dict) -> list[dict]:
    return [c for c in _fa(doc).get("changes") or [] if c.get("state") == "measured"]


def _page_round(x: float) -> int:
    """Whole percent as the page rounds it (JavaScript `Math.round`: halves go up), not
    Python's round-half-to-even."""
    return math.floor(x + 0.5)


def _same_instant(a: str | None, b: str | None) -> bool:
    return bool(a and b) and datetime.fromisoformat(a) == datetime.fromisoformat(b)


def _signed(change: dict) -> int | None:
    pct = change.get("percent")
    if not isinstance(pct, (int, float)):
        return None
    return -_page_round(pct) if change.get("direction") == "decreased" else _page_round(pct)


def _expected(change: dict, line: str) -> float:
    """The percent every chart draws on `line` at `change`: its size where the detector draws
    the line there (`four_accounts.drawn`), else 0."""
    size = ((change.get("sizes") or {}).get(line) or {}).get("change_pct")
    drawn = change.get("found_on") == line or bool(
        ((change.get("sizes") or {}).get(line) or {}).get("excludes_no_change"))
    return size if drawn and size is not None else 0.0


def _level_step(segments: list[dict], at: str) -> float | None:
    """The step line's percent at the segment opening at `at`, or None without one."""
    for prev, seg in zip(segments, segments[1:]):
        if _same_instant(seg.get("from"), at) and prev.get("index"):
            return (seg["index"] / prev["index"] - 1) * 100
    return None


def _row_step(rows: list[dict], at: str, key: str, frm: str = "from") -> float | None:
    """A chart's percent step at the row opening at `at`, or None without one or a value."""
    for prev, row in zip(rows, rows[1:]):
        if _same_instant(row.get(frm), at) and row.get(key) and prev.get(key):
            return (row[key] / prev[key] - 1) * 100
    return None


def chart_steps(doc: dict, at: str) -> dict[str, float | None]:
    """Each chart's step at one instant, as the page draws it: the window chart on
    `window_tokens.regimes`, the tokens-per-week and windows-per-week charts on
    `per_week_regimes`, the pooled windows per week on `weekly_windows.max20.regimes`."""
    wt = _wt(doc)
    weeks = wt.get("per_week_regimes") or []
    return {"window": _row_step(wt.get("regimes") or [], at, "value"),
            "tokens_per_week": _row_step(weeks, at, "value"),
            "windows_per_week": _row_step(weeks, at, "windows_per_week"),
            "weekly_windows": _row_step(((doc.get("weekly_windows") or {}).get("max20") or {})
                                        .get("regimes") or [], at, "windows", "opened_by")}


#: The line each chart draws, and what the page calls it.
CHART_LINE = {"window": "five_hour", "tokens_per_week": "seven_day",
              "windows_per_week": "ratio", "weekly_windows": "ratio"}
CHART_NAMES = {"window": "window-size chart", "tokens_per_week": "tokens-per-week chart",
               "windows_per_week": "windows-per-week chart",
               "weekly_windows": "pooled windows per week (weekly_windows)"}


def headline_is_last_measured_change(doc: dict) -> list[str]:
    """Check 4: the headline is the detector's newest measured change, or there is none."""
    measured = _measured(doc)
    c = doc.get("last_change")
    if not measured:
        return ([f"The headline states a change on {c.get('date')} but the four-account detector "
                 f"has measured none."] if c else [])
    newest = max(measured, key=lambda m: datetime.fromisoformat(m["at"]))
    if not c:
        return [f"The detector measured a change at {newest['at']} but the page has no headline."]
    out = []
    metric = detector.LINE_METRIC[newest["found_on"]]
    if not _same_instant(c.get("at"), newest["at"]):
        out.append(f"The headline is dated {c.get('at')} but the detector's newest measured change "
                   f"is at {newest['at']}.")
    if c.get("metric") != metric:
        out.append(f"The headline states {c.get('metric')} but the change was found on the "
                   f"{newest['found_on']} line ({metric}).")
    if c.get("change_pct") != newest.get("change_pct"):
        out.append(f"The headline says {c.get('change_pct')}% but the detector measured "
                   f"{newest.get('change_pct')}%.")
    said = _signed(c)
    if newest.get("change_pct") is not None and said != _page_round(newest["change_pct"]):
        out.append(f"The headline's whole percent is {said:+d}% but the detector's "
                   f"{newest['change_pct']:+g}% reads {_page_round(newest['change_pct']):+d}%.")
    if c.get("state") != "measured":
        out.append(f"The headline's state is {c.get('state')}, not measured.")
    return out


def every_change_drawn(doc: dict) -> list[str]:
    """Check 5: every detector change is drawn on its line, and every event is one of them."""
    fa = _fa(doc)
    levels = fa.get("levels") or {}
    out = []
    for c in fa.get("changes") or []:
        step = _level_step(levels.get(c["found_on"]) or [], c["at"])
        if step is None:
            out.append(f"The {c['state']} change at {c['at']} ({c['found_on']}) opens no segment "
                       f"on its line's step line.")
        elif c.get("change_pct") is not None and abs(step - c["change_pct"]) > DRAWN_TOLERANCE_PP:
            out.append(f"The {c['state']} change at {c['at']} is {c['change_pct']:+g}% on the "
                       f"{c['found_on']} line, but its step line steps {step:+.2f}% there.")
    measured = {(m["at"], m["found_on"]) for m in _measured(doc)}
    for e in doc.get("events") or []:
        if e.get("kind") != "change":
            continue
        if not any(_same_instant(e.get("at"), at) and e.get("found_on") == line
                   for at, line in measured):
            out.append(f"An event dated {e.get('at') or e.get('date')} ({e.get('metric')}) is not a "
                       f"measured change of the four-account detector.")
    return out


def one_figure_per_change(doc: dict) -> list[str]:
    """Check 6: one measured change, one line, one whole percent everywhere the page states it."""
    fa = _fa(doc)
    levels = fa.get("levels") or {}
    records = [("an event", e) for e in doc.get("events") or [] if e.get("kind") == "change"]
    if doc.get("last_change"):
        records.append(("the headline", doc["last_change"]))
    out = []
    for c in _measured(doc):
        at = c["at"]
        for line in detector.LINES:
            want = _page_round(_expected(c, line))
            step = _level_step(levels.get(line) or [], at)
            if step is not None and _page_round(step) != want:
                out.append(f"At {at} the {line} step line steps {step:+.1f}% but the detector's "
                           f"figure there is {want:+d}%.")
        for chart, step in chart_steps(doc, at).items():
            want = _page_round(_expected(c, CHART_LINE[chart]))
            if step is not None and _page_round(step) != want:
                out.append(f"At {at} the {CHART_NAMES[chart]} steps {step:+.1f}% but the detector's "
                           f"{CHART_LINE[chart]} figure there is {want:+d}%.")
        hist = doc.get("history") or {}
        rows = next(iter(hist.values()), []) if hist else []
        day = at[:10]
        for prev, row in zip(rows, rows[1:]):
            if row.get("date") == day and prev.get("meter_budget_per_window"):
                step = (row["meter_budget_per_window"] / prev["meter_budget_per_window"] - 1) * 100
                want = _page_round(_expected(c, "five_hour"))
                if _page_round(step) != want:
                    out.append(f"On {day} the dollar history steps {step:+.1f}% but the detector's "
                               f"five_hour figure there is {want:+d}%.")
        for where, rec in records:
            if not _same_instant(rec.get("at"), at):
                continue
            size = c.get("change_pct")
            if size is not None and (_signed(rec) != _page_round(size) or rec.get("change_pct") != size):
                out.append(f"{where.capitalize()} says {_signed(rec):+d}% ({rec.get('change_pct')}) at "
                           f"{at}, but the detector measured {size:+g}% on the {c['found_on']} line.")
            for line, key in (("five_hour", "five_hour_limit_change_pct"),
                              ("seven_day", "weekly_limit_change_pct"),
                              ("ratio", "windows_per_week_change_pct")):
                mine = ((c.get("sizes") or {}).get(line) or {}).get("change_pct")
                if rec.get(key) != mine:
                    out.append(f"{where.capitalize()} at {at} states {key} {rec.get(key)} but the "
                               f"detector's {line} size is {mine}.")
    return out


def regimes_open_at_measured_changes(doc: dict) -> list[str]:
    """Check 7: every regime boundary is a measured change, and every measured change is one."""
    instants = {datetime.fromisoformat(c["at"]) for c in _measured(doc)}
    wt = _wt(doc)
    credits = doc.get("credits") or {}
    max20 = (doc.get("weekly_windows") or {}).get("max20") or {}
    rows = [("window_tokens.regimes", r.get("from")) for r in (wt.get("regimes") or [])[1:]]
    rows += [("per_week_regimes", r.get("from")) for r in (wt.get("per_week_regimes") or [])[1:]]
    rows += [("window_credits.regimes", r.get("from"))
             for r in ((credits.get("window_credits") or {}).get("regimes") or [])[1:]]
    for label, account_rows in sorted((wt.get("account_regimes") or {}).items()):
        rows += [(f"account_regimes for {label}", r.get("from")) for r in account_rows if r.get("from")]
    rows += [("weekly_windows.max20.regimes", r.get("opened_by"))
             for r in max20.get("regimes") or [] if r.get("opened_by")]
    for label, block in sorted((max20.get("by_account") or {}).items()):
        rows += [(f"weekly_windows.max20.by_account.{label}", r.get("opened_by"))
                 for r in block.get("regimes") or [] if r.get("opened_by")]
    out = []
    for where, frm in rows:
        if not frm or datetime.fromisoformat(frm) not in instants:
            out.append(f"{where} opens a row at {frm}, which is not a measured change of the "
                       f"four-account detector.")
    opened = {datetime.fromisoformat(r["from"]) for r in (wt.get("regimes") or [])[1:] if r.get("from")}
    if wt.get("regimes"):
        for at in sorted(instants - opened):
            out.append(f"The measured change at {at.isoformat()} opens no regime on the window chart.")
    return out


def _labels_in(value, path: str = "") -> list[str]:
    """Every dict key and string in `value` that names an account other than by label."""
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ACCOUNT_NAMES or BARE_LABEL.match(k):
                out.append(f"{path}.{k}" if path else k)
            out += _labels_in(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            if isinstance(v, str) and (v in ACCOUNT_NAMES or BARE_LABEL.match(v)):
                out.append(f"{path}[{i}]")
            else:
                out += _labels_in(v, f"{path}[{i}]")
    return out


def accounts_named_by_label(doc: dict) -> list[str]:
    """Check 8: the detector's block and the change records name accounts only by label."""
    out = []
    fa = _fa(doc)
    for name in fa.get("accounts") or []:
        if not re.fullmatch(r"Max account \d+", str(name)):
            out.append(f"four_accounts.accounts names an account {name!r}, not by its page label.")
    blocks = [("four_accounts", fa), ("last_change", doc.get("last_change") or {})]
    blocks += [(f"events[{i}]", e) for i, e in enumerate(doc.get("events") or [])]
    for where, block in blocks:
        bad = _labels_in(block)
        if bad:
            out.append(f"{where} names an account other than by its page label at "
                       f"{', '.join(bad[:5])}{' and more' if len(bad) > 5 else ''}.")
    return out


def measured_changes_still_found(doc: dict) -> list[str]:
    """Check 9: today's run found every measured change again."""
    return [f"The measured change at {c['at']} ({c['found_on']}, {c.get('change_pct')}%) was not found "
            f"by the detector's run of {_fa(doc).get('day')}; it stays published (never withdrawn), "
            f"and its figure is re-measured at its own instant."
            for c in _measured(doc) if c.get("found_today") is False]


def detector_ran(doc: dict) -> list[str]:
    """Check 10: the detector's newest run is no more than DETECTOR_MAX_AGE_DAYS old."""
    fa = _fa(doc)
    if not fa or not doc.get("generated_at"):
        return ["The page carries no four_accounts block: no limit change can be stated."] if not fa else []
    published = datetime.fromisoformat(doc["generated_at"]).astimezone(timezone.utc).date()
    day = datetime.fromisoformat(fa["day"]).date() if fa.get("day") else None
    if day is None or published - day > timedelta(days=DETECTOR_MAX_AGE_DAYS):
        return [f"The four-account detector's newest run is from {fa.get('day')}, more than "
                f"{DETECTOR_MAX_AGE_DAYS} day before this publish ({published}): "
                f"python3 -m tracker.four_accounts did not run."]
    return []


CHECKS: list[tuple[str, Callable[[dict], list[str]]]] = [
    ("headline_inside_accounts", headline_inside_accounts),
    ("direct_means_direct", direct_means_direct),
    ("account_units", account_units),
    ("headline_is_last_measured_change", headline_is_last_measured_change),
    ("every_change_drawn", every_change_drawn),
    ("one_figure_per_change", one_figure_per_change),
    ("regimes_open_at_measured_changes", regimes_open_at_measured_changes),
    ("accounts_named_by_label", accounts_named_by_label),
    ("measured_changes_still_found", measured_changes_still_found),
    ("detector_ran", detector_ran),
]
#: The checks whose failure stops the page being published (exit 2): figures that contradict
#: each other on the page itself, a step the detector did not measure, or an account named by
#: anything but its label. The rest alert and publish anyway (exit 1).
BLOCKING = frozenset({"headline_is_last_measured_change", "every_change_drawn",
                      "one_figure_per_change", "regimes_open_at_measured_changes",
                      "accounts_named_by_label"})


def run_checks(doc: dict) -> dict[str, list[str]]:
    """Every check's failures by name. A check that cannot run on the document fails."""
    out = {}
    for name, check in CHECKS:
        try:
            out[name] = check(doc)
        except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as e:
            out[name] = [f"{name} could not run on this document: {type(e).__name__}: {e}"]
    return out


def check_file(path: Path, state_path: Path, send: supervise.Sender,
               now: Callable[[], float] = time.time) -> int:
    """Run every check on the public JSON at `path`, alert per incident, print one line each."""
    try:
        results = run_checks(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        results = {"read": [f"cannot read {path}: {e}"]}
    failing = {k: v for k, v in results.items() if v}
    for name, msgs in results.items():
        print(f"invariant {name}: {'FAIL' if msgs else 'pass'}")
        for m in msgs:
            print(f"  {m}")
    blocking = bool(BLOCKING & set(failing))
    blocked = ("Not published: the last published JSON stays." if blocking else "Published anyway.")
    summary = ("; ".join(f"{k}: {' '.join(v)}" for k, v in failing.items()) if failing
               else f"all {len(results)} pass")
    rerun = (f"Run: cd ~/claude-usage-tracker && python3 -m tracker.invariants "
             f"--json {path} --dry-run --state /tmp/claude-usage-invariants-check.json")
    held = "; ".join(f"{k}: {' '.join(v)}" for k, v in failing.items() if k in BLOCKING)

    def withheld(since: float) -> str:
        stamp = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        return (f"Claude usage tracker (gs publisher): the public JSON is WITHHELD, the page keeps "
                f"its last published JSON (withheld since {stamp}, "
                f"{publish_gate._ago(now() - since)}). Blocking: {held} {rerun}")

    did = publish_gate.track_incident(
        state_path, send, not failing, summary,
        f"Claude usage tracker (gs publisher) public JSON invariants failed: {summary} "
        f"{blocked} {rerun}",
        lambda mins, was: (f"Claude usage tracker (gs publisher) public JSON invariants pass "
                           f"again after {mins} min (was: {was})"),
        now, blocking=blocking, withheld_text=withheld)
    print(f"invariants alert: {did}")
    return 2 if blocking else 1 if failing else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--json", type=Path, required=True, help="the built public JSON")
    p.add_argument("--state", type=Path, default=STATE)
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    a = p.parse_args(argv)
    send = supervise.dry_run_sender if a.dry_run else supervise.default_sender()
    return check_file(a.json, a.state, send)


if __name__ == "__main__":
    sys.exit(main())
