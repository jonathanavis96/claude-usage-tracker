"""Publish-time invariants over the built public JSON, and the alert when one breaks.

    python3 -m tracker.invariants --json PATH   # exit 0 all pass, 1 any fail; one line each

On 2026-10-04 the page published a five-hour window chained through two joint-fit factors
(+48% at 22 September, +33% at 29 September) that the fits could not separate: a headline
above every account's own level, scaled by steps the meters did not show. Nothing noticed.
These checks would have: each reads only the public JSON and returns plain-sentence
failures, an empty list when it holds.

1. `headline_inside_accounts`: in the newest regime, with two or more accounts with a
   window, the headline family's figure lies between the lowest and highest account level,
   scaled the way the page scales them, within HEADLINE_TOLERANCE for rounding.
2. `no_unproven_step`: a regime whose `source` scales by a change points at a change whose
   direct window change is certified (ADR 0001 rule 9), or, on a record without one, whose
   joint fit is separable and passes rules 8 and 9.
3. `direct_means_direct`: a headline family measured directly publishes exactly the newest
   regime's `measured_value`.
4. `account_units`: each account's anchor-unit `window` times its regime's scale agrees with
   its direct `measured_window` within ACCOUNT_UNITS_TOLERANCE.
5. `steps_agree_with_meters`: a published five-hour step (certified on its window change)
   whose windows-per-week ratio interval includes no change points the same way as the
   ratio's own five-hour reading.
6. `no_withheld_boundary`: no `per_week_regimes` row and no `account_regimes` row opens at a
   five-hour change the tracker withholds (ADR 0001 rule 9); such a change opens no regime.

A failure does NOT block the publish. bin/daily.sh runs this after the publish, beside
tracker.publish_gate, with the same incident semantics (`publish_gate.track_incident`): one
alert when a check starts failing, nothing while it keeps failing, one recovery when they
all pass again. State lives in --state.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from tracker import credits as credit_model
from tracker import health, publish_gate, supervise

#: Check 1's allowance either side of the account range, for the rounding of published figures.
HEADLINE_TOLERANCE = 0.05
#: How much of a withheld reason check 6 quotes.
REASON_CHARS = 160
#: Check 4's bound. One figure is relative to the pool and one is direct, so this is a
#: sanity bound on units, not an equality.
ACCOUNT_UNITS_TOLERANCE = 0.25
STATE = health.GS_OPS / "claude-usage-invariants.json"


def _wt(doc: dict) -> dict:
    return (doc.get("credits") or {}).get("window_tokens") or {}


def _candidates(doc: dict) -> list[dict]:
    return ((doc.get("credits") or {}).get("five_hour_on_meters") or {}).get("candidates") or []


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


def _candidate_at(doc: dict, at: str) -> dict | None:
    return next((c for c in _candidates(doc) if _same_instant(c.get("at"), at)), None)


def _proof_gaps(cand: dict, credits: dict | None = None) -> list[str]:
    """What a candidate lacks to scale a window: separable, rule 8, rule 9.

    A candidate that records its direct window change (`window_change`) scales a window by
    that change, so it is proved by that change's own certificate: plan-wide, its interval
    excluding no change, certified, and the candidate applying. An older record is read as
    before, against its joint fit:

    Besides the candidate's own records, its published rate interval is read again: against
    the span test (`credits.JOINT_SEPARABLE_SPAN`) and against the rate check on
    data/prices.json (`credits.joint_rate_check`), so a record written by older code, or
    none, cannot vouch for a fit today's rules refuse.
    """
    window = cand.get("window_change")
    if window is not None:
        # The step is the direct window change itself (`credits.known_date_changes`), so its
        # proof is that measurement's own rule 9, and that the candidate applies.
        gaps = []
        pw = window.get("plan_wide") or {}
        if pw.get("state") != "passed":
            gaps.append("its window change failed the plan-wide test (ADR 0001 rule 9)")
        iv = window.get("interval_pct")
        if not iv or iv[0] <= 0 <= iv[1]:
            gaps.append("its window change's interval includes no change (ADR 0001 rule 9)")
        if not window.get("certified"):
            gaps.append("its window change is not certified")
        if "withheld_reason" not in cand:
            gaps.append("it carries no record of whether it was withheld (ADR 0001 rule 9)")
        elif not cand.get("applies"):
            gaps.append("it is withheld")
        return gaps
    fit = cand.get("joint_fit") or {}
    gaps = []
    if not fit.get("separable"):
        gaps.append("its joint fit is not separable")
    r_iv = fit.get("rate_relative_interval")
    if r_iv and r_iv[0] > 0:
        span = r_iv[1] / r_iv[0]
        if span > credit_model.JOINT_SEPARABLE_SPAN:
            gaps.append(f"its rate interval [{r_iv[0]:g}, {r_iv[1]:g}] spans {span:.2f}x, wider than "
                        f"the span test's {credit_model.JOINT_SEPARABLE_SPAN:g}x")
        if cand.get("family") and fit.get("base_family"):
            again = credit_model.joint_rate_check(
                cand["family"], fit["base_family"],
                credits if credits is not None else credit_model.load_credits(), r_iv)
            if again["state"] == "failed":
                gaps.append(f"read against data/prices.json, its rate fails the rate check "
                            f"(ADR 0001 rule 8): {again['reason']}")
    check = fit.get("rate_check")
    if check is None:
        gaps.append("it carries no record that it passed the rate check (ADR 0001 rule 8)")
    elif check.get("state") == "failed":
        gaps.append("it failed the rate check (ADR 0001 rule 8)")
    pw = cand.get("plan_wide")
    if pw is None:
        gaps.append("it carries no record that it passed the plan-wide test (ADR 0001 rule 9)")
    elif pw.get("state") != "passed":
        gaps.append("it failed the plan-wide test (ADR 0001 rule 9)")
    iv = cand.get("interval_pct")
    if not iv or iv[0] <= 0 <= iv[1]:
        gaps.append("its interval includes no change (ADR 0001 rule 9)")
    if "withheld_reason" not in cand:
        gaps.append("it carries no record of whether it was withheld (ADR 0001 rule 9)")
    elif not cand.get("applies"):
        gaps.append("it is withheld")
    return gaps


def no_unproven_step(doc: dict) -> list[str]:
    """Check 2: every regime scaled by a change points at a change that passed rules 8 and 9.

    The regime opened at the 14 September cut is scaled by the certified weekly change across
    it, not by a five-hour candidate, so it is not one of these. A carried or directly
    measured regime passes.
    """
    wt = _wt(doc)
    cut = wt.get("cut_at") or credit_model.CUT_AT.isoformat()
    out = []
    for reg in wt["regimes"]:
        if "scaled_by" not in (reg.get("source") or "") or _same_instant(reg.get("from"), cut):
            continue
        cand = _candidate_at(doc, reg.get("from"))
        if cand is None:
            out.append(f"The regime from {reg.get('from')} is scaled by a change "
                       f"({reg['source']}), but no five-hour change candidate is at that instant.")
            continue
        gaps = _proof_gaps(cand)
        if gaps:
            out.append(f"The regime from {reg['from']} is scaled by the {cand.get('family')} change "
                       f"({reg['source']}), but {'; '.join(gaps)}.")
    return out


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


def _publishes_five_hour_step(cand: dict) -> bool:
    """Whether a candidate publishes a five-hour step: it applies, and where it records its
    direct window change, that is what it was certified on. A candidate certified on the
    weekly limit alone applies and steps the week, not the window."""
    window = cand.get("window_change")
    return bool(cand.get("applies")) and (window is None or bool(window.get("certified")))


def steps_agree_with_meters(doc: dict) -> list[str]:
    """Check 5: a published five-hour step agrees in sign with the meters where they are mute.

    A published step is a candidate that `applies` with a five-hour change, certified on its
    direct window change where it records one (`_publishes_five_hour_step`). Where its
    windows-per-week ratio interval includes no change, the weekly cap is not shown to have
    moved, so a larger window means fewer windows per week: the ratio's own five-hour reading
    (1 / ratio - 1) must point the same way as the step.
    """
    out = []
    for cand in _candidates(doc):
        pct, ratio = cand.get("change_pct"), cand.get("windows_per_week_ratio")
        iv = cand.get("windows_per_week_ratio_interval")
        if not _publishes_five_hour_step(cand) or not pct or not ratio or not iv \
                or not iv[0] <= 1 <= iv[1]:
            continue
        reading = 1 / ratio - 1
        if reading * pct < 0:
            out.append(f"The {cand.get('family')} step at {cand.get('at')} is {pct:+g}% on the "
                       f"five-hour limit, but the windows-per-week ratio is {ratio:g} "
                       f"[{iv[0]:g}, {iv[1]:g}], a five-hour reading of {reading * 100:+.1f}% the "
                       f"other way, with an interval that includes no change.")
    return out


def _short(text: str, limit: int = REASON_CHARS) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def no_withheld_boundary(doc: dict) -> list[str]:
    """Check 6: no per-week or account row opens at a withheld five-hour change.

    A withheld change (`applies` false) is no reading of the limit, so the regime it would
    open is pooled with the one before; a row starting at its instant draws it as a step.
    """
    wt = _wt(doc)
    out = []
    for cand in _candidates(doc):
        if cand.get("applies") or not cand.get("at"):
            continue
        blocks = []
        if any(_same_instant(r.get("from"), cand["at"]) for r in wt.get("per_week_regimes") or []):
            blocks.append("per_week_regimes")
        labels = [label for label, rows in sorted((wt.get("account_regimes") or {}).items())
                  if any(_same_instant(r.get("from"), cand["at"]) for r in rows)]
        if labels:
            blocks.append(f"account_regimes for {', '.join(labels)}")
        if not blocks:
            continue
        day = datetime.fromisoformat(cand["at"])
        out.append(f"{' and '.join(blocks)} open a "
                   f"row at {day.day} {day:%B} ({cand['at']}), where the {cand.get('family')} "
                   f"five-hour change is withheld: "
                   f"{_short(cand.get('withheld_reason') or 'no reason recorded')}.")
    return out


CHECKS: list[tuple[str, Callable[[dict], list[str]]]] = [
    ("headline_inside_accounts", headline_inside_accounts),
    ("no_unproven_step", no_unproven_step),
    ("direct_means_direct", direct_means_direct),
    ("account_units", account_units),
    ("steps_agree_with_meters", steps_agree_with_meters),
    ("no_withheld_boundary", no_withheld_boundary),
]


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
    summary = ("; ".join(f"{k}: {' '.join(v)}" for k, v in failing.items()) if failing
               else f"all {len(results)} pass")
    publish_gate.track_incident(
        state_path, send, not failing, summary,
        f"Claude usage tracker (gs publisher) public JSON invariants failed: {summary} "
        f"Published anyway. Run: cd ~/claude-usage-tracker && python3 -m tracker.invariants "
        f"--json {path} --dry-run --state /tmp/claude-usage-invariants-check.json",
        lambda mins, was: (f"Claude usage tracker (gs publisher) public JSON invariants pass "
                           f"again after {mins} min (was: {was})"),
        now)
    return 1 if failing else 0


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
