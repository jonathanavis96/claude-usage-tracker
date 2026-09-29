"""Break down the per-stretch scatter the limit-change measurements rest on.

Every limit-change figure divides a stretch's credits by the meter movement it bought, and
compares an account's level before a change with its level after. How far one stretch lands
from its account's level sets every interval. This tool measures that scatter and how much of
it each candidate source explains, on the stretches the fits use:

- the selection is `credits.announced_change_stretches` (status accepted, harness runs out,
  at least MIN_DELTA_PCT of movement, masterrig from MASTERRIG_FROM), valued the way the
  publisher values them (`credits.comparison_value` at the rates `absorb_new_family_rates`
  settles on);
- each stretch's log credits per 1% is taken from its own account's mean within a regime, the
  regimes split at the 14 September weekly change and at every change candidate, so account
  level and the regime steps are out of the residual;
- a source's share is the fall in residual sum of squares when its covariates are regressed
  out within account and regime (R squared); whole-percent rounding is predicted outright,
  from each stretch's own `credits.rounding_variance`.

The capture columns (`capture_status`) are judged on the same ratio the residual is made of,
so their share describes the residual and does not explain it; they are shown for that.

    python3 -m tools.scatter
    python3 -m tools.scatter --json scatter.json

Read-only arithmetic over committed files: history/gs-passive.json,
history/masterrig-passive.json, history/model-rates.json and history/harness-runs.jsonl.
docs/findings-2026-09-28-scatter.md is the write-up; the transcript-side checks there (message
usage lines, session attribution, unseen headless work) read ~/.claude* on gs and are not here.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import credits as C
from tracker.publish import ACCOUNT_LABELS

HISTORY = Path(__file__).resolve().parent.parent / "history"
#: The movement buckets the rounding comparison is read in.
BUCKETS = ((3, 5), (5, 10), (10, 20), (20, math.inf))
#: The interval half-width the "days needed" line solves for: plus or minus 10%.
TARGET = math.log(1.10)
CLASSES = ("output", "cache_write_1h", "cache_write_5m", "input")


def load() -> tuple[list[dict], dict, dict]:
    """(rows, the joint fits keyed by family and instant, the credits table)."""
    def read(name):
        p = HISTORY / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    by = C.stretches_by_account(read("gs-passive.json"), read("masterrig-passive.json"))
    runs = C.harness_runs()
    credits = C.load_credits()
    labels = dict(ACCOUNT_LABELS)

    def value_for(rates):
        return C.comparison_value(credits, rates)

    rates, fits = C.absorb_new_family_rates(by, runs, credits, C.load_model_rates(), labels, value_for)
    value = value_for(rates)
    cands = C.change_candidates(by, credits, [])
    bounds = sorted({C.CUT_AT, *(c["at"] for c in cands)})
    rows = []
    for name, stretches in C.announced_change_stretches(by, runs).items():
        for st in stretches:
            start, end = datetime.fromisoformat(st["start"]), datetime.fromisoformat(st["end"])
            regime = sum(1 for b in bounds if b <= start)
            if regime != sum(1 for b in bounds if b < end):
                continue  # spans a boundary
            credit = value(st["tokens"])
            if not credit or credit <= 0:
                continue
            rows.append({"account": labels.get(name, name), "regime": regime, "st": st,
                         "credits": credit, "y": math.log(credit / st["delta_pct"]),
                         "x": features(st, credit, credits, rates, start, end)})
    return rows, fits, credits


def features(st: dict, credit: float, credits: dict, rates: dict, start: datetime,
             end: datetime) -> dict[str, float]:
    """The candidate covariates of one stretch."""
    shares: dict[str, float] = defaultdict(float)
    fams: dict[str, float] = defaultdict(float)
    cache_read = turns_fast = 0.0
    for model, tok in st["tokens"].items():
        if not isinstance(tok, dict) or not C.raw_tokens(tok):
            continue
        fam = C.family(model, credits)
        value = C.comparison_value(credits, rates)
        one = value({model: {"input": 1}}) or 0.0
        out_rate = value({model: {"output": 1}}) or 0.0
        parts = {"output": tok.get("output", 0) * out_rate,
                 "cache_write_1h": tok.get("cache_write_1h", 0) * one,
                 "cache_write_5m": (tok.get("cache_write", 0) - tok.get("cache_write_1h", 0)) * one,
                 "input": tok.get("input", 0) * one}
        for k, v in parts.items():
            shares[k] += v / credit
        fams[fam or model] += sum(parts.values()) / credit
        cache_read += tok.get("cache_read", 0) * one / credit
    fast = st.get("fast_session_tokens") or {}
    for tok in fast.values():
        turns_fast += sum(tok.get(k, 0) for k in ("input", "output", "cache_write"))
    charged = sum(tok.get(k, 0) for tok in st["tokens"].values() if isinstance(tok, dict)
                  for k in ("input", "output", "cache_write"))
    mid = start + (end - start) / 2
    hour = mid.astimezone(timezone.utc).hour + mid.minute / 60
    return {
        "rounding": C.rounding_variance(st),
        "capture_unaccounted": float(st.get("capture_status") == "unaccounted"),
        "capture_surplus": float(st.get("capture_status") == "surplus"),
        "reset_unverified": float(st.get("reset_verified") is not True),
        "reset_inferred": float(st.get("reset_source") == "inferred"),
        **{f"share_{k}": shares[k] for k in CLASSES},
        "cache_read_per_credit": cache_read,
        **{f"family_{f}": fams.get(f, 0.0) for f in ("sonnet", "fable", "haiku", "opus-5-5")},
        "fast_share": turns_fast / charged if charged else 0.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24), "hour_cos": math.cos(2 * math.pi * hour / 24),
        "weekend": float(mid.astimezone(timezone.utc).weekday() >= 5),
        "log_hours": math.log(max((end - start).total_seconds() / 3600, 0.05)),
        "log_turns_per_credit": math.log(max(st.get("turns") or 1, 1) / credit),
        "windows": float(st.get("windows") or 1),
    }


#: The sources, as groups of covariates. The capture group is outcome-defined (see the
#: module docstring); the rest are measured independently of the residual.
SOURCES = {
    "capture status (outcome-defined)": ["capture_unaccounted", "capture_surplus"],
    "reset verification": ["reset_unverified", "reset_inferred"],
    "token-class shares (pricing weights)": ["share_output", "share_cache_write_1h",
                                             "share_cache_write_5m", "cache_read_per_credit"],
    "model-family shares (family rates)": ["family_sonnet", "family_fable", "family_haiku",
                                           "family_opus-5-5"],
    "fast-session share": ["fast_share"],
    "hour of day and weekday (UTC)": ["hour_sin", "hour_cos", "weekend"],
    "stretch duration and window pieces": ["log_hours", "windows"],
    "turns per credit": ["log_turns_per_credit"],
}
MEASURED = [k for k in SOURCES if not k.startswith("capture")]


def _groups(rows: list[dict]) -> np.ndarray:
    keys = {}
    return np.array([keys.setdefault((r["account"], r["regime"]), len(keys)) for r in rows])


def _demean(v: np.ndarray, g: np.ndarray) -> np.ndarray:
    out = v.astype(float).copy()
    for k in np.unique(g):
        out[g == k] -= out[g == k].mean()
    return out


def r_squared(rows: list[dict], names: list[str]) -> tuple[float, float]:
    """(share of the within-regime residual SS the covariates explain, residual variance after)."""
    g = _groups(rows)
    y = _demean(np.array([r["y"] for r in rows]), g)
    X = np.column_stack([_demean(np.array([r["x"][n] for r in rows]), g) for n in names])
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ b
    dof = len(rows) - len(set(g.tolist())) - np.linalg.matrix_rank(X)
    return 1 - float(res @ res) / float(y @ y), float(res @ res) / max(dof, 1)


def residual_variance(rows: list[dict]) -> float:
    g = _groups(rows)
    y = _demean(np.array([r["y"] for r in rows]), g)
    return float(y @ y) / (len(rows) - len(set(g.tolist())))


def rounding_table(rows: list[dict]) -> list[dict]:
    g = _groups(rows)
    y = _demean(np.array([r["y"] for r in rows]), g)
    sizes = {k: int((g == k).sum()) for k in set(g.tolist())}
    out = []
    for lo, hi in BUCKETS:
        idx = [i for i, r in enumerate(rows) if lo <= r["st"]["delta_pct"] < hi]
        if not idx:
            out.append({"bucket": [lo, hi], "n": 0})
            continue
        # Each residual's own degrees-of-freedom correction, so a bucket's figure is unbiased.
        obs = sum(y[i] ** 2 * sizes[g[i]] / (sizes[g[i]] - 1) for i in idx if sizes[g[i]] > 1) / len(idx)
        pred = sum(rows[i]["x"]["rounding"] for i in idx) / len(idx)
        out.append({"bucket": [lo, hi], "n": len(idx), "observed_variance": obs,
                    "rounding_variance": pred, "rounding_share": pred / obs if obs else None})
    return out


def autocorrelation(rows: list[dict]) -> dict[str, float]:
    """Lag-one correlation of consecutive residuals within each account and regime."""
    by = defaultdict(list)
    for r in rows:
        by[(r["account"], r["regime"])].append(r)
    out = {}
    for (acc, reg), rs in sorted(by.items()):
        rs.sort(key=lambda r: r["st"]["start"])
        e = np.array([r["y"] for r in rs]) - np.mean([r["y"] for r in rs])
        if len(e) > 5:
            out[f"{acc}/regime {reg}"] = round(float(np.corrcoef(e[:-1], e[1:])[0, 1]), 2)
    return out


def days_needed(fit: dict | None, rows: list[dict]) -> dict | None:
    """Days of post-change data a ±10% five-hour interval needs at the present rate and scatter.

    The joint fit's bootstrap half-width in logs shrinks as one over the root of the after
    side's count; the after side accrues at the rate it has since the candidate instant. The
    bootstrap resamples stretches as independent, which the lag-one correlation says they are
    not, so this is a lower bound.
    """
    if not fit or not fit.get("five_hour_limit_change_interval_pct"):
        return None
    lo, hi = fit["five_hour_limit_change_interval_pct"]
    half = (math.log(1 + hi / 100) - math.log(1 + lo / 100)) / 2
    combined = fit["accounts_combined"]
    n_after = sum(fit["per_account"][a]["n_after"] for a in combined)
    at = datetime.fromisoformat(fit["_at"])
    ends = [datetime.fromisoformat(r["st"]["end"]) for r in rows
            if r["account"] in combined and datetime.fromisoformat(r["st"]["start"]) >= at]
    span = (max(ends) - at).total_seconds() / 86400 if ends else None
    if not n_after or not span:
        return None
    need = n_after * (half / TARGET) ** 2
    return {"n_after": n_after, "days_so_far": round(span, 1), "log_half_width": round(half, 4),
            "n_after_needed": math.ceil(need), "days_needed": round(need / (n_after / span), 1)}


def run(rows: list[dict], fits: dict) -> dict:
    total = residual_variance(rows)
    table = []
    for name, cols in SOURCES.items():
        share, after = r_squared(rows, cols)
        table.append({"source": name, "covariates": cols, "share": share, "variance_after": after})
    all_measured = [c for k in MEASURED for c in SOURCES[k]]
    combined, left = r_squared(rows, all_measured)
    per_account = {a: residual_variance([r for r in rows if r["account"] == a])
                   for a in sorted({r["account"] for r in rows})}
    latest = None
    for (fam, at), fit in sorted(fits.items(), key=lambda kv: kv[0][1]):
        if fit.get("five_hour_limit_change_pct") is not None:
            latest = {**fit, "_at": at}
    return {
        "n": len(rows),
        "residual_variance": total, "residual_sd": math.sqrt(total),
        "per_account_variance": per_account,
        "rounding_predicted_share": float(np.mean([r["x"]["rounding"] for r in rows])) / total,
        "rounding_by_bucket": rounding_table(rows),
        "sources": table,
        "measured_sources_together": {"share": combined, "variance_after": left},
        "unexplained_share": 1 - combined,
        "lag1_autocorrelation": autocorrelation(rows),
        "joint_fit": ({k: latest.get(k) for k in (
            "family", "_at", "rate_relative_to_base", "rate_relative_interval",
            "five_hour_limit_change_pct", "five_hour_limit_change_interval_pct",
            "log_residual_sd", "scatter_sd", "rounding_sd")} if latest else None),
        "days_to_ten_percent": days_needed(latest, rows),
    }


def show(res: dict) -> str:
    lines = [(f"{res['n']} stretches; residual variance {res['residual_variance']:.4f} "
              f"(sd {res['residual_sd']:.3f}) about each account's level within a regime"),
             "  per account: " + ", ".join(f"{a} {v:.4f}" for a, v in res["per_account_variance"].items()),
             f"rounding alone predicts {res['rounding_predicted_share']:.1%} of it"]
    for b in res["rounding_by_bucket"]:
        lo, hi = b["bucket"]
        label = f"{lo}-{hi}%" if hi != math.inf else f"{lo}%+"
        if not b["n"]:
            lines.append(f"  {label:7s} no stretches")
            continue
        lines.append(f"  {label:7s} n {b['n']:3d}  observed {b['observed_variance']:.4f}  "
                     f"rounding {b['rounding_variance']:.4f}  ({b['rounding_share']:.1%})")
    lines.append("share of the residual each source explains (within account and regime):")
    for row in res["sources"]:
        lines.append(f"  {row['source']:40s} {row['share']:6.1%}   variance after {row['variance_after']:.4f}")
    t = res["measured_sources_together"]
    lines.append(f"  {'measured sources together':40s} {t['share']:6.1%}   variance after {t['variance_after']:.4f}")
    lines.append(f"  unexplained: {res['unexplained_share']:.1%}")
    lines.append("lag-one autocorrelation: " + ", ".join(f"{k} {v:+.2f}" for k, v in res["lag1_autocorrelation"].items()))
    j = res["joint_fit"]
    if j:
        lines.append(f"joint fit {j['family']} at {j['_at']}: r {j['rate_relative_to_base']} "
                     f"{j['rate_relative_interval']}, five-hour {j['five_hour_limit_change_pct']:+}% "
                     f"{j['five_hour_limit_change_interval_pct']}, residual sd {j['log_residual_sd']}, "
                     f"scatter sd {j.get('scatter_sd')}, rounding sd {j.get('rounding_sd')}")
    d = res["days_to_ten_percent"]
    if d:
        lines.append(f"a ±10% five-hour interval needs about {d['n_after_needed']} stretches after "
                     f"({d['n_after']} in {d['days_so_far']} days so far): about {d['days_needed']} days")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", type=Path, help="also write the breakdown as JSON")
    a = ap.parse_args(argv)
    rows, fits, _ = load()
    res = run(rows, fits)
    print(show(res), end="")
    if a.json:
        a.json.write_text(json.dumps(res, indent=1, default=float) + "\n", encoding="utf-8")
        print(f"JSON -> {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
