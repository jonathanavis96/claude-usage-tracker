"""Run the passive join on masterrig and summarise it for the publisher."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from .join import DailyRate, build_intervals, daily_rates
from .samples import merge_samples, parse_ceiling_log, parse_moonlighter
from .turns import iter_turns, session_tokens_by_model, transcript_paths
from .weekly import parse_rows as parse_weekly_rows
from .weekly import weekly_windows

PLAN_CHANGE = date(2026, 8, 14)
# The precise seam within PLAN_CHANGE day: the meter's weekly-to-window ratio drops
# from about 11 windows/week to about 6.6 between the passive window ending 16:20 UTC
# (last Max 5x) and the one ending 19:19 UTC (which itself straddles the seam --
# its own 5-hour span starts before it -- and so belongs to neither plan).
# tracker/publish.py's _max5_window_points/_max20_window_points split on this
# instant rather than on the whole day, so the plan-move seam lands where the
# meter itself moved.
PLAN_CHANGE_AT = datetime(2026, 8, 14, 17, 0, tzinfo=timezone.utc)


def _stored_days(previous: object, before: date | None) -> dict[date, dict]:
    """Days of a stored record's `history` earlier than `before`, as stored.

    Claude Code deletes transcripts after its cleanup period, so a rebuild from
    ~/.claude/projects reaches less far back each day: on 2026-09-21 masterrig's
    oldest transcript was 2026-08-20, history/passive.json lost 2026-08-11 to
    2026-08-19, and the 5x-to-20x ratio, whose "before" side sat in those days,
    went to None for good. A day the rebuild cannot reach any more is kept as it was
    last counted; a day it still reaches is recounted, and one it now drops is gone.
    Anything unreadable in `previous` is ignored.
    """
    hist = previous.get("history") if isinstance(previous, dict) else None
    if not isinstance(hist, dict):
        return {}
    kept = {}
    for ds, row in hist.items():
        try:
            d = date.fromisoformat(ds)
        except (TypeError, ValueError):
            continue
        if (before is None or d < before) and isinstance(row, dict) \
                and isinstance(row.get("tokens_per_pct"), (int, float)):
            kept[d] = {"tokens_per_pct": row["tokens_per_pct"],
                       "interpolated": bool(row.get("interpolated"))}
    return kept


def passive_summary(rates: dict[date, DailyRate], plan_change: date = PLAN_CHANGE,
                     session_tokens: dict[str, int] | None = None,
                     weekly: dict | None = None, previous: object = None) -> dict:
    stored = _stored_days(previous, min(rates) if rates else None) if previous else {}
    values = {d: (r["tokens_per_pct"], r["interpolated"]) for d, r in stored.items()}
    values.update({d: (r.tokens_per_pct, r.interpolated) for d, r in rates.items()})
    before = [v for d, (v, interp) in values.items() if plan_change - timedelta(days=14) <= d < plan_change and not interp]
    after = [v for d, (v, interp) in values.items() if plan_change <= d < plan_change + timedelta(days=14) and not interp]
    ratio = (median(before) / median(after)) if before and after else None
    recent = [r for d, r in sorted(rates.items())[-14:] if not r.interpolated]
    split = {}
    if recent:
        for c in recent[-1].split:
            split[c] = round(median(r.split[c] for r in recent), 4)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_ratio_5x_to_20x": None if ratio is None else round(ratio, 4),
        "split": split,
        "history": {d.isoformat(): {"tokens_per_pct": round(v), "interpolated": interp}
                    for d, (v, interp) in sorted(values.items())},
        "session_tokens": session_tokens or {},
    }
    if weekly is not None:
        summary["weekly_windows"] = weekly
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Passive join over masterrig logs")
    ap.add_argument("--out", type=Path, default=Path("history/passive.json"))
    a = ap.parse_args(argv)
    rates, session_tokens, weekly = _rebuild(Path.home())
    try:
        previous = json.loads(a.out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    summary = passive_summary(rates, session_tokens=session_tokens, weekly=weekly, previous=previous)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    tmp.replace(a.out)  # atomic: a killed run never leaves a truncated file
    print(f"wrote {a.out}: {len(summary['history'])} days ({len(rates)} recounted), "
          f"ratio {summary['plan_ratio_5x_to_20x']}")
    return 0


def _rebuild(home: Path) -> tuple[dict[date, DailyRate], dict[str, int], dict]:
    """The join over this host's logs and transcripts: daily rates, session tokens, weekly windows."""
    moonlighter_path = home / ".moonlighter/usage_log.jsonl"
    with open(moonlighter_path, encoding="utf-8") as f:
        ml = parse_moonlighter(f)
    cl_path = home / ".paperclip/ops/mis-usage-ceiling-systemd.log"
    cl = []
    if cl_path.exists():
        with open(cl_path, encoding="utf-8") as f:
            cl = parse_ceiling_log(f)
    samples = merge_samples(ml, cl)
    paths = transcript_paths(home / ".claude/projects", None)
    turns = list(iter_turns(paths))
    rates = daily_rates(build_intervals(samples, turns))
    session_tokens = session_tokens_by_model(paths)
    with open(moonlighter_path, encoding="utf-8") as f:
        weekly = weekly_windows(parse_weekly_rows(f))
    return rates, session_tokens, weekly


if __name__ == "__main__":
    raise SystemExit(main())
