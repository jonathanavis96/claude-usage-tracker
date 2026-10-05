"""Does a counting error explain why headless work moves the five-hour meter more? (wf-143)

Read-only over the gs transcripts and meter logs, through tools/account_agreement.py and its
transcript cache. Findings: docs/findings/2026-10-05-headless-five-hour.md.

    python3 -m tools.headless_hypotheses          # every hypothesis, one JSON document

One row per tiled one-point seven-day step of each gs account from 14 September 12:00Z
(`account_agreement.crossing_spans`), with the five-hour points crossed in it and the step's
own turns split by source (interactive main session, sub-agent, headless `sdk-cli` main
session) and by token class, each class in input-rate units of its family at list price
(`all_at_list`). Then:

- `ttl`: the share of each source's credits that are one-hour and five-minute cache writes,
  and the headless factor on five-hour points per seven-day step under four write pricings;
- `runs`: headless runs (first turns of `sdk-cli` files) per step, credits per run, the
  first turn's cache write as a share of headless credits, and the factor with runs per
  credit added;
- `timing`: the headless factor with the turns re-binned 10 and 30 minutes either way;
- `other`: the factor beside weekday 12-18Z (US morning) share, turns, sessions, step length
  and burn rate; and per account.

Five-hour points per seven-day point is a ratio of two meters: no collector price enters it,
only the headless label. A pricing fix moves credits per 1% of both meters together.
"""
from __future__ import annotations

import bisect
import collections
import json
import math
from datetime import timedelta

import numpy as np

from tools import account_agreement as A
from tools.account_transcripts import entrypoint
from tracker import credits as C
from tracker import gs_passive as G
from tracker.publish import CREDITS

CLASSES = ("in", "out", "cw5", "cw1", "cr")
SOURCES = ("int", "sub", "head")


def steps(data: dict, shift: timedelta = timedelta(0)) -> list[dict]:
    """Every gs seven-day step in one regime, its five-hour points and its work by source/class."""
    rates = A.valuation_rates("all_at_list")
    fam_rate: dict[str, tuple[float, float] | None] = {}
    ep: dict[str, str] = {}

    def source(path: str) -> str:
        if "/subagents/" in path:
            return "sub"
        if path not in ep:
            ep[path] = "head" if entrypoint(path) == "sdk-cli" else "int"
        return ep[path]

    end = max(x.turn.ts for n in ("jwork", "dave", "avis") for x in data[n]["own"])
    rows = []
    for name in ("jwork", "dave", "avis"):
        smp = [x for x in G.load_samples(G.gs_accounts()[name]) if C.CUT_AT <= x.ts <= end]
        turns = sorted(data[name]["own"], key=lambda x: x.turn.ts)
        stamps = [x.turn.ts for x in turns]
        first: dict[str, object] = {}
        for x in turns:
            first.setdefault(x.path, x.turn.ts)
        for p in A.crossing_spans(smp):
            k = A.regime_of(p["start"])
            if k is None or k != A.regime_of(p["end"]):
                continue
            part = turns[bisect.bisect_left(stamps, p["start"] + shift):
                         bisect.bisect_left(stamps, p["end"] + shift)]
            X: dict[str, float] = collections.defaultdict(float)
            sessions: set[str] = set()
            for x in part:
                t = x.turn
                if t.remote:
                    continue
                fam = C.family(t.model, CREDITS) or "other"
                if fam not in fam_rate:
                    fam_rate[fam] = C.comparison_rate(fam, CREDITS, rates) if fam != "other" else None
                rate = fam_rate[fam]
                if not rate:
                    continue
                s = source(x.path)
                cw5 = t.cache_write - t.cache_write_1h
                for cls, amt in (("in", t.input * rate[0]), ("out", t.output * rate[1]),
                                 ("cw5", cw5 * rate[0]), ("cw1", t.cache_write_1h * rate[0]),
                                 ("cr", t.cache_read * rate[0])):
                    X[f"{s}:{cls}"] += amt
                X[f"{s}:turns"] += 1
                total = (t.input + t.cache_write) * rate[0] + t.output * rate[1]
                X["all"] += total
                if t.ts.weekday() < 5 and 12 <= t.ts.hour < 18:
                    X["us_morning"] += total
                sessions.add(x.path)
                if s == "head" and first.get(x.path) == t.ts:
                    X["head:runs"] += 1
                    X["head:first_cw"] += t.cache_write * rate[0]
            if X["all"] > 0:
                rows.append({"account": name, "regime": k, "d5": p["d5"],
                             "hours": (p["end"] - p["start"]).total_seconds() / 3600,
                             "sessions": len(sessions), "X": dict(X)})
    return rows


def _credits(r: dict, w: dict, only: str | None = None) -> float:
    return sum(w[cls] * r["X"].get(f"{s}:{cls}", 0.0) for s in SOURCES if only in (None, s)
               for cls in CLASSES)


def _factor(rows: list[dict], cols: list[tuple[str, list[float]]], by_account: bool = True) -> dict:
    """Quasi-Poisson five-hour points per seven-day step on regime (and account) plus `cols`."""
    y = np.array([r["d5"] for r in rows], float)
    regs = sorted({r["regime"] for r in rows})[1:]
    accts = sorted({r["account"] for r in rows})[1:] if by_account else []
    X = [np.ones(len(rows))] + [np.array([r["regime"] == k for r in rows], float) for k in regs] \
        + [np.array([r["account"] == a for r in rows], float) for a in accts] \
        + [np.array(v, float) for _, v in cols]
    b, se, dev = A._poisson(y, np.column_stack(X))
    j0 = 1 + len(regs)
    # Account factors are against the first account in name order (avis where it is present).
    out: dict = {"deviance": round(dev, 1),
                 **{a: round(math.exp(b[j0 + i]), 3) for i, a in enumerate(accts)}}
    for i, (name, _) in enumerate(cols):
        j = j0 + len(accts) + i
        out[name] = [round(math.exp(b[j]), 2), round(math.exp(b[j] - 1.96 * se[j]), 2),
                     round(math.exp(b[j] + 1.96 * se[j]), 2)]
    return out


PRICINGS = {
    # The collector's credit model: a write is input, a read the fitted weight.
    "collector": {"in": 1, "out": 1, "cw5": 1, "cw1": 1, "cr": None},
    "api_ttl": {"in": 1, "out": 1, "cw5": 1.25, "cw1": 2.0, "cr": 0.1},
    "collector_1h_x1.6": {"in": 1, "out": 1, "cw5": 1, "cw1": 1.6, "cr": None},
    "collector_1h_x2": {"in": 1, "out": 1, "cw5": 1, "cw1": 2.0, "cr": None},
}


def report() -> dict:
    data = A._data()
    rows = steps(data)
    rates = A.valuation_rates("all_at_list")
    opus = C.comparison_rate("opus", CREDITS, rates)
    fit = C.pooled_fit_prices(rates)
    assert opus and fit, "no Opus rate or pooled fit to value cache reads against"
    crw = fit["cache_read_rate"] / opus[0]
    base = dict(PRICINGS["collector"], cr=crw)
    head = [_credits(r, base, "head") / _credits(r, base) for r in rows]
    out: dict = {"steps": len(rows), "per_account_steps": dict(collections.Counter(r["account"] for r in rows))}

    ttl: dict = {"write_shares": {}, "pricings": {}}
    for a in ("jwork", "dave", "avis"):
        for s in SOURCES:
            tot = {c: sum(r["X"].get(f"{s}:{c}", 0.0) for r in rows if r["account"] == a)
                   for c in ("in", "out", "cw5", "cw1")}
            T = sum(tot.values())
            if T:
                ttl["write_shares"][f"{a}/{s}"] = {"cw1h_share": round(tot["cw1"] / T, 3),
                                                   "cw5m_share": round(tot["cw5"] / T, 3)}
    for name, w in PRICINGS.items():
        w = dict(w, cr=crw if w["cr"] is None else w["cr"])
        c = [_credits(r, w) for r in rows]
        h = [_credits(r, w, "head") / x for r, x in zip(rows, c)]
        lo = [(x, r["d5"]) for r, x, hh in zip(rows, c, h) if hh < 0.2]
        hi = [(x, r["d5"]) for r, x, hh in zip(rows, c, h) if hh > 0.8]
        ttl["pricings"][name] = {
            "five_hour_headless_factor": _factor(rows, [("headless", h)])["headless"],
            "credits_per_7d_pct_k": {"under_0.2": round(sum(x for x, _ in lo) / len(lo) / 1e3),
                                     "over_0.8": round(sum(x for x, _ in hi) / len(hi) / 1e3)},
            "credits_per_5h_pct_k": {"under_0.2": round(sum(x for x, _ in lo) / sum(d for _, d in lo) / 1e3),
                                     "over_0.8": round(sum(x for x, _ in hi) / sum(d for _, d in hi) / 1e3)}}
    out["ttl"] = ttl

    hr = [r for r, hh in zip(rows, head) if hh > 0.8]
    runs_per_mc = [r["X"].get("head:runs", 0) / r["X"]["all"] * 1e6 for r in rows]
    out["runs"] = {
        "headless_steps": len(hr),
        "runs_per_headless_step": round(float(np.mean([r["X"].get("head:runs", 0) for r in hr])), 1),
        "credits_per_run_k": round(sum(r["X"]["all"] for r in hr) / sum(r["X"].get("head:runs", 0) for r in hr) / 1e3, 1),
        "first_turn_write_share_of_headless_credits": round(
            sum(r["X"].get("head:first_cw", 0) for r in rows)
            / sum(_credits(r, base, "head") for r in rows), 3),
        "runs_alone": _factor(rows, [("runs_per_Mcredit", runs_per_mc)]),
        "headless_plus_runs": _factor(rows, [("headless", head), ("runs_per_Mcredit", runs_per_mc)])}

    out["timing"] = {}
    for minutes in (-30, -10, 0, 10, 30):
        shifted = steps(data, timedelta(minutes=minutes)) if minutes else rows
        h = [_credits(r, base, "head") / _credits(r, base) for r in shifted]
        out["timing"][f"{minutes:+d} min"] = _factor(shifted, [("headless", h)])

    us = [r["X"].get("us_morning", 0) / r["X"]["all"] for r in rows]
    turns = [sum(r["X"].get(f"{s}:turns", 0) for s in SOURCES) / r["X"]["all"] * 1e6 for r in rows]
    out["other"] = {
        "headless": _factor(rows, [("headless", head)]),
        "us_morning_alone": _factor(rows, [("us_morning", us)]),
        "headless_plus_us_morning": _factor(rows, [("headless", head), ("us_morning", us)]),
        "headless_plus_turns_per_Mcredit": _factor(rows, [("headless", head), ("turns", turns)]),
        "headless_plus_sessions_per_Mcredit": _factor(
            rows, [("headless", head), ("sessions", [r["sessions"] / r["X"]["all"] * 1e6 for r in rows])]),
        "headless_plus_log_hours": _factor(rows, [("headless", head), ("log_hours", [math.log(r["hours"]) for r in rows])]),
        "per_account": {a: {**_factor([r for r in rows if r["account"] == a],
                                      [("headless", [h for r, h in zip(rows, head) if r["account"] == a])],
                                      by_account=False),
                            "mean_headless": round(float(np.mean([h for r, h in zip(rows, head) if r["account"] == a])), 2)}
                        for a in ("jwork", "dave", "avis")}}
    return out


if __name__ == "__main__":
    print(json.dumps(report(), indent=1))
