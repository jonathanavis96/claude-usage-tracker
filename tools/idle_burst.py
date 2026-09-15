"""Idle-bounded burst measurement of meter dollars per 1% (2026-09-15).

Measures how much list-price value one percent of the five-hour meter buys,
from ordinary usage rather than from a probe. No traffic is generated.

Why it works
------------
The five-hour meter updates with a lag, so tokens spent near the end of a
short sampling interval land on the next interval's reading. Fitting token
counts against per-interval meter movement therefore fails outright (a
non-negative least squares fit over 1225 five-minute intervals returned
R^2 = -0.377, worse than predicting the mean, on every subset tried).

Bounding the measurement by idle instead removes the lag. A stretch of turns
with at least 45 minutes of silence on both sides has nowhere for its meter
movement to leak: everything it caused is inside the window, and the window
starts from a settled meter. Padding the window 10 minutes before the first
turn and 20 minutes after the last one catches the trailing lag.

How a window is rejected
------------------------
- the meter crossed a five-hour reset inside the window. The window is SPLIT
  at the reset and each part measured separately; discarding these outright
  was the single largest source of lost samples (51 of 143 bursts), and it
  removed nearly every long Opus session.
- a sampling gap over 35 minutes, so the meter's path is unknown.
- total movement under 2 percent, where whole-integer meter rounding carries
  more than 25 percent error.
- no single model holds 90 percent of the window's tokens. Model ids must be
  normalised first (tracker.turns.normalize_model): `claude-opus-5[1m]` and
  `claude-opus-5` are the same model, and bucketing on the raw id splits one
  pure Opus window into two impure ones.

Result as of 2026-09-15 (20x era, masterrig, jonathanavis96@gmail.com)
---------------------------------------------------------------------
    claude-opus-5     n=15  median $4.940 per 1%   range $0.08-$12.74
    claude-fable-5-1  n=21  median $2.535 per 1%   range $0.28-$3.53

Fable's readings sit in $2.35-$2.99 apart from two outliers, a spread of about
+-10 percent, which is tighter than one three-tick probe run (+-8.7 percent).
The series is flat from 07 to 14 Sep, agreeing with the probe that the
five-hour window did not change while the weekly cap stepped down on 13 Sep.
So this method is usable on its own as a change detector.

The open disagreement
---------------------
On the same pricing function, probe runs read about $1.00 per 1% for Fable and
this method reads $2.535 -- a constant factor of 2.5. It is not a class weight:
setting cache_read's weight to zero still leaves $1.91, and setting output's
weight to zero still leaves $1.28. Two probe mixes of completely different
composition agree with each other (cache-write-heavy $1.006, output-heavy
$1.004), so the probe side is internally corroborated; this side is
corroborated by 21 consistent readings. Duration does not drive it (a 1-hour
and a 5-hour window both read $2.6), which rules out the five-hour meter
decaying as a rolling window.

Anything the meter charges for that is missing from the transcripts would push
this number DOWN, not up, so unlogged traffic on the account cannot explain it.
Settling it needs a controlled burst on masterrig: known traffic through the
ordinary Claude Code path on this account, with the meter read either side,
which puts both pipelines on the same traffic.

Run:  python3 tools/idle_burst.py
Reads ~/.moonlighter/usage_log.jsonl, the paperclip ceiling log, and the
transcripts under ~/.claude/projects. Masterrig only -- the transcripts under
~/.claude on gs belong to Dave's account (moved out of ~/.claude on 2026-08-29)
and must never be merged into this account's meter.
"""
import bisect
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.samples import merge_samples, parse_ceiling_log, parse_moonlighter
from tracker.turns import iter_turns, normalize_model, transcript_paths
from tracker.usage_api import same_reset

IDLE = timedelta(minutes=45)
MAXGAP = timedelta(minutes=35)
PAD_PRE = timedelta(minutes=10)
PAD_POST = timedelta(minutes=20)
MIN_D5 = 2.0
PURITY = 0.90
ERA_START = date(2026, 8, 18)
CLASSES = ("input", "output", "cache_read", "cache_write")


def load_samples(home):
    with open(home / ".moonlighter/usage_log.jsonl", encoding="utf-8") as fh:
        ml = parse_moonlighter(fh)
    ceiling = home / ".paperclip/ops/mis-usage-ceiling-systemd.log"
    cl = []
    if ceiling.exists():
        with open(ceiling, encoding="utf-8") as fh:
            cl = parse_ceiling_log(fh)
    return sorted(merge_samples(ml, cl), key=lambda s: s.ts)


def meter_segments(samples):
    """Runs of consecutive samples with no reset, no decrease and no long gap."""
    segments = []
    run = [samples[0]]
    for a, b in zip(samples, samples[1:]):
        broken = (not same_reset(a.resets_at, b.resets_at)
                  or b.five_hour < a.five_hour
                  or b.ts - a.ts > MAXGAP)
        if broken:
            segments.append(run)
            run = [b]
        else:
            run.append(b)
    segments.append(run)
    return segments


def idle_bursts(turns):
    """Stretches of turns separated by at least IDLE of silence."""
    bursts = []
    run = [turns[0]]
    for a, b in zip(turns, turns[1:]):
        if b.ts - a.ts >= IDLE:
            bursts.append(run)
            run = []
        run.append(b)
    bursts.append(run)
    return [b for b in bursts if b]


def measure(bursts, segments, turns):
    """One row per (burst, meter segment) overlap, so a reset splits rather than kills."""
    turn_ts = [t.ts for t in turns]
    rows = []
    for burst in bursts:
        lo = burst[0].ts - PAD_PRE
        hi = burst[-1].ts + PAD_POST
        for segment in segments:
            if segment[-1].ts <= lo or segment[0].ts >= hi:
                continue
            seg_ts = [s.ts for s in segment]
            i = bisect.bisect_left(seg_ts, lo)
            j = bisect.bisect_right(seg_ts, hi)
            part = segment[max(i - 1, 0):min(j + 1, len(segment))]
            if len(part) < 2:
                continue
            delta_pct = part[-1].five_hour - part[0].five_hour
            if delta_pct < MIN_D5:
                continue
            start, end = part[0].ts, part[-1].ts
            spanned = turns[bisect.bisect_left(turn_ts, start):bisect.bisect_left(turn_ts, end)]
            if not spanned:
                continue
            tokens = {c: sum(getattr(t, c) for t in spanned) for c in CLASSES}
            by_model = {}
            for t in spanned:
                model = normalize_model(t.model)
                by_model[model] = by_model.get(model, 0) + t.total
            total = sum(by_model.values())
            if total <= 0:
                continue
            top = max(by_model, key=by_model.get)
            rows.append((start, end, delta_pct, tokens, top, by_model[top] / total))
    return rows


def meter_usd(tokens, price):
    """List-price value of these tokens as the meter weighs it."""
    weight = price["class_weight"]
    listed = sum(tokens[c] / 1e6 * price[c] * weight[c] for c in tokens)
    return listed * price["meter_weight"]


def main():
    home = Path.home()
    samples = load_samples(home)
    turns = sorted(iter_turns(transcript_paths(home / ".claude/projects", None)),
                   key=lambda t: t.ts)
    rows = measure(idle_bursts(turns), meter_segments(samples), turns)

    prices = json.loads((Path(__file__).resolve().parent.parent / "data/prices.json")
                        .read_text(encoding="utf-8"))
    models = {k: v for k, v in prices.items() if not k.startswith("_")}

    per_model = {}
    for start, end, delta_pct, tokens, model, purity in sorted(rows):
        if start.date() < ERA_START or purity < PURITY or model not in models:
            continue
        rate = meter_usd(tokens, models[model]) / delta_pct
        per_model.setdefault(model, []).append(rate)
        print(f"{start:%m-%d %H:%M}-{end:%H:%M} {model:16s} "
              f"pure={purity:.2f} d5={delta_pct:5.1f}%  ${rate:6.3f}/1%")

    print()
    for model, rates in sorted(per_model.items()):
        print(f"{model:16s} n={len(rates):2d}  median ${statistics.median(rates):.3f}/1%"
              f"  range ${min(rates):.2f}-${max(rates):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
