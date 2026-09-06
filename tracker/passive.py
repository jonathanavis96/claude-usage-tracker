"""Run the passive join on masterrig and summarise it for the publisher."""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from .join import DailyRate, build_intervals, daily_rates
from .samples import merge_samples, parse_ceiling_log, parse_moonlighter
from .turns import iter_turns, session_tokens_by_model, transcript_paths

PLAN_CHANGE = date(2026, 8, 18)


def passive_summary(rates: dict[date, DailyRate], plan_change: date = PLAN_CHANGE,
                     session_tokens: dict[str, int] | None = None) -> dict:
    before = [r.tokens_per_pct for d, r in rates.items() if plan_change - timedelta(days=14) <= d < plan_change and not r.interpolated]
    after = [r.tokens_per_pct for d, r in rates.items() if plan_change <= d < plan_change + timedelta(days=14) and not r.interpolated]
    ratio = (median(before) / median(after)) if before and after else None
    recent = [r for d, r in sorted(rates.items())[-14:] if not r.interpolated]
    split = {}
    if recent:
        for c in recent[-1].split:
            split[c] = round(median(r.split[c] for r in recent), 4)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_ratio_5x_to_20x": None if ratio is None else round(ratio, 4),
        "split": split,
        "history": {d.isoformat(): {"tokens_per_pct": round(r.tokens_per_pct), "interpolated": r.interpolated} for d, r in sorted(rates.items())},
        "session_tokens": session_tokens or {},
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Passive join over masterrig logs")
    ap.add_argument("--out", type=Path, default=Path("history/passive.json"))
    a = ap.parse_args(argv)
    home = Path.home()
    with open(home / ".moonlighter/usage_log.jsonl", encoding="utf-8") as f:
        ml = parse_moonlighter(f)
    cl_path = home / ".paperclip/ops/mis-usage-ceiling-systemd.log"
    cl = parse_ceiling_log(open(cl_path, encoding="utf-8")) if cl_path.exists() else []
    samples = merge_samples(ml, cl)
    paths = transcript_paths(home / ".claude/projects", None)
    turns = list(iter_turns(paths))
    rates = daily_rates(build_intervals(samples, turns))
    session_tokens = session_tokens_by_model(paths)
    summary = passive_summary(rates, session_tokens=session_tokens)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    tmp.replace(a.out)  # atomic: a killed run never leaves a truncated file
    print(f"wrote {a.out}: {len(rates)} days, ratio {summary['plan_ratio_5x_to_20x']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
