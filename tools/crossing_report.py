"""Report on exact percent-crossing events recoverable from a raw meter log.

The API only returns whole percents (docs/reference-2026-09-20-shellac-credits-model.md,
tools/float_probe.py), so any single reading -- and any delta between two readings --
carries +-0.5 points of quantisation error per endpoint. tracker/crossings.py turns the
same samples into *crossings*: the exact moment a bracket the meter passed through an
integer percent, timed to within that bracket's width rather than read to within a
+-0.5-point band. This is a read-only report on how many of those a log holds and how
tight the brackets are; it changes nothing and writes nothing.

    python3 tools/crossing_report.py ~/.paperclip/ops/claude-usage-meter-dave.log
    python3 tools/crossing_report.py ~/.paperclip/ops/claude-usage-meter-{dave,jwork}.log
"""
from __future__ import annotations

import argparse
import statistics as stats
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `tracker` imports whether
                                                                   # this runs as `python3 tools/crossing_report.py`
                                                                   # or `python3 -m tools.crossing_report`
from tracker.crossings import METERS, Crossing, bracket_events, exact_delta, extract_all  # noqa: E402
from tracker.samples import Sample, parse_meter_log  # noqa: E402
from tracker.usage_api import same_reset  # noqa: E402


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile; the series is small, so this is plenty exact."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[index]


def load(path: Path) -> list[Sample]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return parse_meter_log(lines)


_LOG_PREFIX = "claude-usage-meter-"


def label_for(path: Path) -> str:
    """The account label for a log, e.g. claude-usage-meter-dave.log -> dave."""
    stem = path.stem
    return stem[len(_LOG_PREFIX):] if stem.startswith(_LOG_PREFIX) else stem


def report_one(label: str, samples: list[Sample]) -> dict[str, list[Crossing]]:
    print(f"\n=== {label} ===")
    print(f"samples: {len(samples)}")
    if not samples:
        print("  (no readings)")
        return {}

    # "Crossings" below counts observed brackets/events, not the extra per-level entries a
    # multi-percent jump adds internally (a 5->8 jump is one observed step bounding three
    # levels, not three separate events); a clean single-percent crossing is one of each.
    by_meter = extract_all(samples)
    events = {meter: bracket_events(cs) for meter, cs in by_meter.items()}
    total = sum(len(es) for es in events.values())
    clean = sum(1 for es in events.values() for c in es if c.clean)
    print(f"crossings: {total}  (clean single-percent: {clean}, {100 * clean / total:.0f}%)" if total
          else "crossings: 0")

    for meter in METERS:
        es = events[meter]
        levels = by_meter[meter]
        print(f"  {meter:<10} crossings={len(es):<5} clean={sum(1 for c in es if c.clean):<5}"
              + (f" (levels crossed: {len(levels)})" if len(levels) != len(es) else "")
              + (f" range {min(c.level for c in levels)}..{max(c.level for c in levels)}" if levels else ""))

    widths = [c.bracket_width_s / 60 for es in events.values() for c in es]
    if widths:
        print(f"bracket width (minutes): median={stats.median(widths):.1f}  "
              f"p90={percentile(widths, 0.9):.1f}  max={max(widths):.1f}")

    # Achievable resolution vs. the current whole-percent method: a plain sample-to-sample
    # delta carries +-0.5 points of quantisation error at EACH endpoint (+-1 point total,
    # independent of how far apart the two readings are). A crossing-to-crossing delta has
    # none -- it is an exact integer difference -- and its only residual error is timing:
    # the true crossing could be anywhere in its bracket, so a *rate* computed from it is
    # uncertain by the bracket widths, not the percent by which it moved.
    for meter in METERS:
        clean_cs = [c for c in by_meter[meter] if c.clean]
        if len(clean_cs) < 2:
            continue
        # consecutive clean crossings only, and only pairs the same window (a reset between
        # them would make the level pairing meaningless -- exact_delta would refuse it anyway)
        deltas = [exact_delta(a, b) for a, b in zip(clean_cs, clean_cs[1:])
                  if b.level > a.level and same_reset(a.reset_at, b.reset_at)]
        if not deltas:
            continue
        unc_min = [d.uncertainty_s / 60 for d in deltas]
        print(f"  {meter}: {len(deltas)} consecutive clean-crossing pairs, "
              f"exact percent delta (0 quantisation error, vs +-1 point whole-percent), "
              f"timing uncertainty median={stats.median(unc_min):.1f} min p90={percentile(unc_min, 0.9):.1f} min")

    return by_meter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", type=Path,
                    help="raw meter log(s), tracker.meter_log's JSONL shape; one file per account "
                         "(that is what the sampler writes), so pass both accounts' logs to compare them")
    args = ap.parse_args(argv)

    all_by_meter: list[dict[str, list[Crossing]]] = []
    for path in args.logs:
        all_by_meter.append(report_one(label_for(path), load(path)))

    if len(args.logs) > 1:
        print("\n=== combined ===")
        for meter in METERS:
            es = [c for bm in all_by_meter for c in bracket_events(bm.get(meter, []))]
            clean = sum(1 for c in es if c.clean)
            print(f"  {meter:<10} crossings={len(es):<5} clean={clean}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
