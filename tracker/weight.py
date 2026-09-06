"""The output class weight, recomputed from the weekly Fable output run.

The 5-hour meter charges output tokens harder than their list price: the
2026-09-06 output-heavy Fable probe cost $0.59 of list value per 1% against
$1.08 for the cache-write-heavy prose probe an hour earlier, so one list
dollar of output moves the meter about 1.8x as far as one list dollar of cache
write (docs/spike-2026-09.md). `class_weight.output` in data/prices.json
carries that ratio and the publisher applies it to every token bundle.

Nine prompts over five ticks is a coarse first estimate, and the ratio may
move when Anthropic re-weights, so it is re-measured weekly: `bin/output-probe.sh`
runs a 5-tick Fable probe with `--payload output` on Sunday, and the next
daily publish calls `update_output_weight` here before it builds the JSON.

The weight is defined by the invariant the publisher already relies on: with
the right weight, the latest output row and the latest Fable prose row read
the same meter dollars per 1%. Writing each row's per-percent list value as
`other + output`, with `other` already carrying the other classes' weights,

    other_prose + w * output_prose == other_output + w * output_output
    w = (other_prose - other_output) / (output_output - output_prose)

which reduces to the crude ratio of the two rows' list values only when the
output row carries no cache traffic at all (it carries about 12%).

Guard: a recomputed weight more than GUARD (30%) from the one currently in
prices.json is not applied. It is recorded under `_output_weight` with
`applied: false` and one alert goes to Jonathan through tracker/alert.py, who
decides whether the meter really moved or the run was bad. A weight inside the
guard is written to every model's `class_weight.output` (the same class weights
are assumed for every model) and recorded the same way with `applied: true`.
The record names the pair of rows it came from, so a daily publish that sees
the same pair again does nothing: one recompute, and at most one alert, per
output row.

Everything here is advisory to the publish: a failed alert is a warning, and
tracker.publish carries on with whatever weight prices.json holds.
"""
from __future__ import annotations
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from .alert import ENV_FILE, Poster, _default_post, alert_config, send_alert
from .rows import is_output

WEIGHT_MODEL = "claude-fable-5-1"
GUARD = 0.30
RECORD_KEY = "_output_weight"
CLASSES = ("input", "output", "cache_read", "cache_write")


@dataclass(frozen=True)
class WeightUpdate:
    value: float          # the recomputed output weight, rounded to 2 dp
    current: float        # the weight prices.json held before this update
    change: float         # |value / current - 1|
    applied: bool         # False when the guard refused it
    output_row: str       # ts of the output row used
    prose_row: str        # ts of the Fable prose row used


def _weight(price: dict, cls: str) -> float:
    return price.get("class_weight", {}).get(cls, 1.0)


def _per_pct(row: dict, price: dict) -> dict[str, float]:
    """List dollars per 1% for each class, the other classes' weights applied."""
    span = row["tick_to"] - row["tick_from"]
    return {cls: row["tokens"][cls] * price[cls] / 1e6 / span * (1.0 if cls == "output" else _weight(price, cls))
            for cls in CLASSES}


def solve_output_weight(prose_row: dict, output_row: dict, price: dict) -> float:
    """The output weight at which both rows read the same meter dollars per 1%."""
    p, o = _per_pct(prose_row, price), _per_pct(output_row, price)
    other_p = sum(v for cls, v in p.items() if cls != "output")
    other_o = sum(v for cls, v in o.items() if cls != "output")
    contrast = o["output"] - p["output"]
    if contrast <= 0:
        raise ValueError("output row carries no more output value per 1% than the prose row")
    w = (other_p - other_o) / contrast
    if w <= 0:
        raise ValueError(f"solved output weight {w:.3f} is not positive; the rows disagree in the wrong direction")
    return w


def _usable(row: dict, model: str) -> bool:
    return row.get("model") == model and not row.get("outlier")


def latest_pair(rows: list[dict], model: str = WEIGHT_MODEL) -> tuple[dict, dict] | None:
    """(latest output row, latest prose row) for `model`, or None without both."""
    outputs = [r for r in rows if _usable(r, model) and is_output(r)]
    proses = [r for r in rows if _usable(r, model) and not is_output(r)]
    if not outputs or not proses:
        return None
    return max(outputs, key=lambda r: r["ts"]), max(proses, key=lambda r: r["ts"])


def _write(path: Path, raw: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _alert(subject: str, text: str, *, post: Poster, env_file: Path, environ: Mapping[str, str] | None,
           now: datetime) -> None:
    cfg = alert_config(env_file, environ)
    if isinstance(cfg, str):
        print(f"alert: skipped, {cfg}", file=sys.stderr)
        return
    try:
        status, reply = send_alert(subject, text, cfg, post=post, now=now)
    except OSError as e:
        print(f"warning: alert '{subject}' not sent: {e}", file=sys.stderr)
        return
    if 200 <= status < 300:
        print(f"alert: sent '{subject}' to {cfg.to} (HTTP {status})")
    else:
        print(f"warning: alert '{subject}' refused: HTTP {status} {reply[:200]!r}", file=sys.stderr)


def update_output_weight(rows: list[dict], prices_path: Path, *, post: Poster = _default_post,
                         env_file: Path = ENV_FILE, environ: Mapping[str, str] | None = None,
                         now: datetime | None = None, guard: float = GUARD,
                         model: str = WEIGHT_MODEL) -> WeightUpdate | None:
    """Recompute the output weight from the newest pair of rows and write prices.json.

    Returns None when there is nothing new to do: no output row, no prose row
    for the model, or the same pair was already handled (applied or refused).
    """
    prices_path = Path(prices_path)
    now = now or datetime.now(timezone.utc)
    pair = latest_pair(rows, model)
    if pair is None:
        return None
    output_row, prose_row = pair
    raw = json.loads(prices_path.read_text(encoding="utf-8"))
    record = raw.get(RECORD_KEY) or {}
    if record.get("output_row") == output_row["ts"] and record.get("prose_row") == prose_row["ts"]:
        return None
    price = raw[model]
    current = _weight(price, "output")
    value = round(solve_output_weight(prose_row, output_row, price), 2)
    change = abs(value / current - 1)
    applied = change <= guard
    raw[RECORD_KEY] = {"output_row": output_row["ts"], "prose_row": prose_row["ts"], "value": value,
                       "previous": current, "applied": applied, "computed_at": now.isoformat(timespec="seconds"),
                       "guard": guard}
    if applied:
        for key, entry in raw.items():
            if key.startswith("_") or not isinstance(entry, dict):
                continue
            entry.setdefault("class_weight", {})["output"] = value
    _write(prices_path, raw)
    update = WeightUpdate(value, current, change, applied, output_row["ts"], prose_row["ts"])
    pct = round(change * 100)
    if applied:
        print(f"output weight: applied {value:g} (was {current:g}), {pct}% change, "
              f"from output row {output_row['ts']} against prose row {prose_row['ts']}")
        return update
    print(f"output weight: refused {value:g} (current {current:g}, {pct}% change exceeds {round(guard * 100)}%) "
          f"from output row {output_row['ts']} against prose row {prose_row['ts']}; prices.json weight unchanged")
    _alert(f"Output class weight refused: {value:g} vs {current:g}",
           f"The weekly output run recomputed class_weight.output = {value:g}, which is {pct}% from the "
           f"current {current:g}; the guard is {round(guard * 100)}%, so it was not applied.\n\n"
           f"output row:  {output_row['ts']} ({model}, {output_row.get('prompts')} prompts, "
           f"ticks {output_row.get('tick_from')}->{output_row.get('tick_to')})\n"
           f"prose row:   {prose_row['ts']} ({model}, {prose_row.get('prompts')} prompts, "
           f"ticks {prose_row.get('tick_from')}->{prose_row.get('tick_to')})\n\n"
           f"Either the meter's output weighting moved or one of the runs is bad. To accept the new "
           f"value, set class_weight.output on every model in data/prices.json by hand; this pair will "
           f"not be recomputed again. Otherwise the next Sunday run supplies a fresh pair.\n"
           f"Record: {prices_path} key {RECORD_KEY}.",
           post=post, env_file=env_file, environ=environ, now=now)
    return update
