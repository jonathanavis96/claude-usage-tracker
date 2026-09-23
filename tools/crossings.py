"""Measure from exact whole-percent crossings instead of stretch/week endpoints (issue #66).

A stretch or a week today is read from its first and last meter samples, so
each end carries up to one point of whole-percent rounding (see
`tracker.weekly.weekly_windows`, `tracker.detect.ratio_interval`). A reading
that steps from 41% to 42% between two samples marks a moment, known only to
within that sample gap, at which the true value passed 42.0. `tracker.crossings`
measures between two such moments instead: the rounding at each end drops out
and what is left is timing error, bounded by the sample gap at each end.

This tool runs that over real data:

    python3 -m tools.crossings --masterrig
    python3 -m tools.crossings --gs-account jwork --gs-account dave

`--masterrig` reads this host's own meter and transcripts (`tracker.gs_passive
masterrig_account`). `--gs-account NAME` reads `tracker.gs_passive gs_accounts`'s
paths for that account -- correct when run on gs itself (or against a `--home`
laid out the same way); the common brief's scratch-dir copy holds only the
meter logs; a copy with no transcripts under it still measures crossings and
weekly ratios, just not `tokens_between_crossings`, which needs the token
stream. `--ad-hoc` points at an arbitrary meter log/transcripts root pair for
a account, useful for a log copied to a scratch dir.

For each account it reports: samples read, crossings found per meter, the
weekly five-hour/seven-day ratio from `windows_per_week_from_crossings`
against the account's existing `tracker.weekly.weekly_windows` figure for the
same weeks (narrower interval or not), and, when transcripts are available,
`tokens_between_crossings` summarised per window.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from tracker.crossings import Crossing, crossings, tokens_between_crossings, windows_per_week_from_crossings
from tracker.gs_passive import gs_accounts, masterrig_account
from tracker.samples import Sample
from tracker.samples import parse_log as parse_meter_log
from tracker.turns import iter_turns, transcript_paths
from tracker.weekly import _week_key, weekly_windows


def _load_samples(meter_log: Path, meter_format: str, since: datetime | None = None) -> list[Sample]:
    if not meter_log.exists():
        return []
    with open(meter_log, encoding="utf-8") as fh:
        return parse_meter_log(meter_format, fh, since)


def _gap_stats(cs: list[Crossing]) -> dict:
    gaps = sorted(c.gap.total_seconds() for c in cs)
    if not gaps:
        return {"n": 0}
    return {"n": len(gaps), "median_s": round(median(gaps), 1), "max_s": round(max(gaps), 1),
            "over_5min": sum(1 for g in gaps if g > 300)}


def _weekly_from_pairs(pairs: list[dict]) -> dict[str, dict]:
    """Pool crossing pairs (windows_per_week_from_crossings' output) by the seven-day window's week.

    Each pair's `window_id` is the seven-day crossings' own reset id (a raw
    resets_at string when the log names one, else a synthetic window-start
    ISO string that is not a real reset date and buckets under itself, same
    as `tracker.weekly._week_key` treats any string it is handed).
    """
    weeks: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        weeks[_week_key(p["window_id"])].append(p)
    out = {}
    for wk, ps in sorted(weeks.items()):
        n_def = sum(p["five_hour_points_definite"] for p in ps)
        n_amb = sum(p["five_hour_points_ambiguous"] for p in ps)
        k_tot = sum(p["k"] for p in ps)
        out[wk] = {"pairs": len(ps), "k_total": k_tot,
                   "windows": round((n_def + n_amb / 2) / k_tot, 4) if k_tot else None,
                   "windows_bounds": [round(n_def / k_tot, 4) if k_tot else None,
                                     round((n_def + n_amb) / k_tot, 4) if k_tot else None]}
    return out


def _rows_from_samples(samples: list[Sample]) -> list[dict | None]:
    """Samples as `tracker.weekly.parse_row`'s row shape, so `weekly_windows` runs on them directly."""
    out: list[dict | None] = []
    for s in samples:
        if s.seven_day is None or s.resets_at is None or s.seven_resets_at is None:
            out.append(None)
            continue
        out.append({"ts": s.ts.isoformat(), "five_hour": s.five_hour, "five_resets_at": s.resets_at,
                    "seven_day": s.seven_day, "seven_resets_at": s.seven_resets_at})
    return out


def _existing_weekly(samples: list[Sample]) -> dict[str, dict]:
    """`tracker.weekly.weekly_windows`'s own calendar-week history for the same samples,
    to compare interval widths against -- the published figure crossings would replace."""
    history = weekly_windows(_rows_from_samples(samples))["history"]
    return {h["week_ending"]: {"pieces": h["pieces"], "windows": h["windows"],
                               "windows_bounds": h["rounding_interval"]} for h in history}


def _interval_width(bounds: list[float | None]) -> float | None:
    lo, hi = bounds
    return None if lo is None or hi is None else hi - lo


def run_account(name: str, samples: list[Sample], turns_paths: list[Path], k: int, n: int) -> dict:
    five = crossings(samples, meter="five_hour")
    seven = crossings(samples, meter="seven_day")
    pairs = windows_per_week_from_crossings(five, seven, k=k)
    weekly_crossing = _weekly_from_pairs(pairs)
    weekly_existing = _existing_weekly(samples)
    compare = {}
    for wk in sorted(set(weekly_crossing) | set(weekly_existing)):
        c, e = weekly_crossing.get(wk), weekly_existing.get(wk)
        cw = _interval_width(c["windows_bounds"]) if c else None
        ew = _interval_width(e["windows_bounds"]) if e else None
        compare[wk] = {"crossing": c, "existing": e,
                       "crossing_narrower": (cw is not None and ew is not None and cw < ew) if (cw is not None and ew is not None) else None}
    out = {"account": name, "samples": len(samples),
          "five_hour_crossings": len(five), "seven_day_crossings": len(seven),
          "five_hour_gap": _gap_stats(five), "seven_day_gap": _gap_stats(seven),
          "weekly_pairs": pairs, "weekly_by_week": compare}
    if turns_paths:
        turns = list(iter_turns(turns_paths))
        tok = tokens_between_crossings(five, turns, n=n)
        out["turns"] = len(turns)
        out["tokens_between_crossings_n"] = n
        out["tokens_between_crossings_sample"] = tok[:5]
        out["tokens_between_crossings_count"] = len(tok)
        # A crossing with zero tokens in its confirmed span is not a bug: local transcripts
        # can miss meter movement entirely (off-machine use, or a symlinked account another
        # login also writes to -- see MASTERRIG_METER_NOTE / capture.py). Reporting only the
        # blended median with those zeros in it hides that, so both are recorded.
        nonzero = [t["tokens_per_pct"] for t in tok if t["tokens"] > 0]
        out["tokens_between_crossings_zero"] = len(tok) - len(nonzero)
        if tok:
            out["tokens_per_pct_median_blended"] = round(median(t["tokens_per_pct"] for t in tok), 1)
        if nonzero:
            out["tokens_per_pct_median_captured"] = round(median(nonzero), 1)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, default=Path.home())
    ap.add_argument("--masterrig", action="store_true")
    ap.add_argument("--gs-account", action="append", default=[], help="jwork and/or dave, via gs_accounts() paths")
    ap.add_argument("--ad-hoc", action="append", default=[], metavar="NAME:LOG:FORMAT[:TRANSCRIPTS]",
                    help="a meter log and optional transcripts root under any name, e.g. "
                         "jwork:/scratch/jwork.log:meter")
    ap.add_argument("--k", type=int, default=5, help="seven-day crossings apart for the weekly ratio")
    ap.add_argument("--n", type=int, default=1, help="five-hour crossings apart for tokens_between_crossings")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)

    results = {}
    if a.masterrig:
        acct = masterrig_account(a.home)
        from tracker.gs_passive import load_samples
        samples = load_samples(acct)
        root = acct.config_dir / "projects"
        paths = transcript_paths(root, samples[0].ts if samples else None) if root.exists() else []
        results["masterrig"] = run_account("masterrig", samples, paths, a.k, a.n)
    for name in a.gs_account:
        accounts = gs_accounts(a.home)
        if name not in accounts:
            ap.error(f"unknown gs account: {name}")
        acct = accounts[name]
        from tracker.gs_passive import load_samples
        samples = load_samples(acct)
        root = acct.config_dir / "projects"
        paths = transcript_paths(root, samples[0].ts if samples else None) if root.exists() else []
        results[name] = run_account(name, samples, paths, a.k, a.n)
    for item in a.ad_hoc:
        parts = item.split(":")
        if len(parts) < 3:
            ap.error(f"--ad-hoc needs NAME:LOG:FORMAT[:TRANSCRIPTS]: {item!r}")
        name, log, fmt = parts[0], Path(parts[1]), parts[2]
        transcripts_root = Path(parts[3]) if len(parts) > 3 else None
        samples = _load_samples(log, fmt)
        paths = transcript_paths(transcripts_root, samples[0].ts if samples else None) if transcripts_root and transcripts_root.exists() else []
        results[name] = run_account(name, samples, paths, a.k, a.n)

    if not results:
        ap.error("nothing to run: pass --masterrig, --gs-account or --ad-hoc")

    for name, r in results.items():
        print(f"{name}: {r['samples']} samples, {r['five_hour_crossings']} five-hour crossings "
              f"(median gap {r['five_hour_gap'].get('median_s')}s), {r['seven_day_crossings']} seven-day crossings")
        for wk, c in sorted(r["weekly_by_week"].items()):
            cw, ew = c["crossing"], c["existing"]
            print(f"  {wk}: crossing={cw['windows'] if cw else None} {cw['windows_bounds'] if cw else None}  "
                  f"existing={ew['windows'] if ew else None} {ew['windows_bounds'] if ew else None}  "
                  f"narrower={c['crossing_narrower']}")
        if "tokens_per_pct_median_blended" in r:
            print(f"  tokens per {r['tokens_between_crossings_n']}%, n={r['tokens_between_crossings_count']} "
                  f"({r['tokens_between_crossings_zero']} zero): blended median "
                  f"{r['tokens_per_pct_median_blended']}, captured-only median "
                  f"{r.get('tokens_per_pct_median_captured')}")

    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "accounts": results},
                                    indent=1) + "\n", encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
