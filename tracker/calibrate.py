"""One-off effort calibration: what one representative task costs per model and effort.

Sends a fixed prompt (CALIBRATION_PROMPT) `repeats` times per (model, effort) cell. The usage
endpoint 429s when read more than about once a minute per token, so it is read at most once
before and once after each cell's batch of runs -- never per run.

The total token count of a run is not a stable measure of the task. `claude -p` sometimes
answers in two API turns instead of one, and the second turn re-reads the whole prefix
(Sonnet low, 7 repeats on gs, 2026-09-09):

    input 2, output 1610, cache_read 19795, cache_write 0     total 21407   (one turn)
    input 4, output 1410, cache_read 39590, cache_write 877   total 41881   (two turns)

input doubles, cache_read is exactly 2 x 19795, and the total doubles with it, so the runs
of a cell split into two clusters and a median over totals lands on whichever cluster has
more members. Valued on the meter the way the probe values a tick (tracker.publish.meter_usd:
per class, tokens x list price x class_weight / 1e6, summed, then x meter_weight) the same
two runs are close -- the doubled prefix is cache_read at a tenth of the input price -- so
each cell publishes the median meter-dollar value over ALL its runs (CELL_RULE).

The cell's number in the matrix stays the median total tokens over all runs, because the
page reads it; the dollar value sits beside it in a top-level `usd` mapping
(model -> effort -> median meter dollars per task) and, with the per-run breakdown under
`_meta.runs`, the derivation of every cell under `_meta.cells` (`median_tokens`,
`median_usd`, `runs`, `turns`, `spread_usd`). `turns` is estimated as `input // 2` per run:
each turn of the fixed prompt costs 2 uncached input tokens, and tracker.cli_run does not
expose the real call count.

`--recompute PATH` re-derives tokens and usd from an existing file's `_meta.runs` without
sending anything, pricing them from `--prices` (default data/prices.json).

`--merge` adds cells to the existing `--out` file instead of replacing it, which is how a new
model joins the matrix (`--models claude-sonnet-5-5,claude-opus-5-5 --merge`). Every cell
already in `_meta.runs` is kept as it is and is not sent again, so a merge that dies part way is
resumed by running the same command: only the missing cells run. The file's own `started`,
`finished` and `repeats` stay those of the first run; each merged batch records its own span,
account, repeats and cells under `_meta.batches`, which tools/harness_runs.py reads so the
batch's traffic is excluded from that account's passive stretches like the first run's.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from .atomic import write_text_atomic
from .publish import meter_usd

MODELS = ["claude-sonnet-5", "claude-opus-5", "claude-fable-5-1"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

CELL_RULE = ("median over all runs of each run's meter-dollar value (per class: tokens x list price x "
             "class_weight / 1e6, summed, x meter_weight); tokens = median total tokens over all runs")

CALIBRATION_PROMPT = """Review the following Python module for bugs, unclear naming and missing edge cases.
List each finding as one line: severity, line, one-sentence fix. Then propose a corrected version of the module.

```python
import json, os
from datetime import datetime

def load(path):
    with open(path) as f:
        return [json.loads(l) for l in f]

def summarize(rows, since=None):
    out = {}
    for r in rows:
        ts = datetime.fromisoformat(r['ts'])
        if since and ts < since: continue
        d = ts.date()
        out[d] = out.get(d, 0) + r.get('tokens', 0)
    return out

def latest(rows):
    return max(rows, key=lambda r: r['ts'])['ts']

def write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({str(k): v for k, v in data.items()}, f)
    os.rename(tmp, path)

if __name__ == '__main__':
    rows = load(os.environ['ROWS'])
    write('out.json', summarize(rows, datetime(2026, 1, 1)))
    print(latest(rows))
```"""


def run_usd(run: dict, price: dict) -> float:
    """Meter-dollar value of one run: tracker.publish.meter_usd x the model's meter_weight."""
    return meter_usd(run, price) * price.get("meter_weight", 1.0)


def run_turns(run: dict) -> int:
    """Estimated API turns in a run: each turn of the fixed prompt costs 2 uncached input tokens."""
    return max(1, run["input"] // 2)


def derive_cell(runs: list[dict], price: dict | None) -> dict | None:
    """The `_meta.cells` entry for a cell from its per-run breakdowns; None when there are no runs.

    `price` is the model's entry from data/prices.json; without one the usd fields are None.
    """
    if not runs:
        return None
    info = {"median_tokens": round(median(r["total"] for r in runs)),
            "median_usd": None, "runs": len(runs), "turns": [run_turns(r) for r in runs], "spread_usd": None}
    if price is not None:
        usd = [run_usd(r, price) for r in runs]
        info["median_usd"] = round(median(usd), 6)
        info["spread_usd"] = round(max(usd) / min(usd), 4) if min(usd) else None
    return info


def _publish_cell(matrix: dict, cell: str, runs: list[dict], prices: dict) -> None:
    model, effort = cell.rsplit("/", 1)
    info = derive_cell(runs, prices.get(model))
    if info is None:
        return
    matrix.setdefault(model, {})[effort] = info["median_tokens"]
    if info["median_usd"] is not None:
        matrix.setdefault("usd", {}).setdefault(model, {})[effort] = info["median_usd"]
    matrix["_meta"].setdefault("cells", {})[cell] = info


def recompute(matrix: dict, prices: dict) -> dict:
    """Re-derive every cell's tokens, usd and `_meta.cells` entry from `_meta.runs`, in place; nothing is sent."""
    matrix["_meta"]["cell_rule"] = CELL_RULE
    matrix["_meta"]["cells"] = {}
    matrix["usd"] = {}
    for cell, runs in matrix["_meta"].get("runs", {}).items():
        _publish_cell(matrix, cell, runs, prices)
    return matrix


def calibrate(models: list[str], efforts: list[str], repeats: int, run: Callable, read: Callable, sleep: Callable,
              checkpoint: Callable[[dict], None] | None = None, prices: dict | None = None,
              skip: set[str] | frozenset[str] = frozenset()) -> dict:
    """Run every cell; a run that fails twice is skipped and its cell left out (recorded under _meta.failed).

    A cell named in `skip` ("model/effort") is not run and does not appear in the result.

    Each cell publishes median tokens and median meter dollars per CELL_RULE (see derive_cell);
    `prices` is data/prices.json's content, and a model missing from it gets no usd value.
    checkpoint, when given, is called with the matrix after every cell so a long real run survives a crash.
    """
    prices = prices or {}
    matrix: dict = {"_meta": {"repeats": repeats, "started": datetime.now(timezone.utc).isoformat(),
                               "cell_rule": CELL_RULE, "usage_deltas": {}, "runs": {}, "cells": {}, "failed": []},
                    "usd": {}}
    for model in models:
        matrix[model] = {}
        for effort in efforts:
            if f"{model}/{effort}" in skip:
                continue
            before = read()
            runs = []
            for _ in range(repeats):
                try:
                    u = run(CALIBRATION_PROMPT, model, effort)
                except Exception:  # noqa: BLE001 -- any failure of a run: one retry, then give up on this run
                    sleep(10)
                    try:
                        u = run(CALIBRATION_PROMPT, model, effort)
                    except Exception as e2:  # noqa: BLE001 -- recorded as a failed cell, never raised
                        matrix["_meta"]["failed"].append({"cell": f"{model}/{effort}", "error": str(e2)[:200]})
                        continue
                runs.append({"input": u.input, "output": u.output,
                             "cache_read": u.cache_read, "cache_write": u.cache_write, "total": u.total})
                sleep(3)
            after = read()
            cell = f"{model}/{effort}"
            _publish_cell(matrix, cell, runs, prices)
            matrix["_meta"]["usage_deltas"][cell] = (after.five_hour or 0) - (before.five_hour or 0)
            matrix["_meta"]["runs"][cell] = runs
            if checkpoint:
                checkpoint(matrix)
    matrix["_meta"]["finished"] = datetime.now(timezone.utc).isoformat()
    return matrix


def merge(base: dict, new: dict, account: str | None = None) -> dict:
    """`base` with `new`'s cells added, as a new dict; neither argument is changed.

    Every cell of `base` is kept exactly as it is -- its runs, usage delta, tokens, usd and
    `_meta.cells` entry -- and a cell that `new` also holds is refused rather than replaced:
    a calibration run is never discarded. A cell of `base` with no runs at all (every run
    failed) holds nothing to discard and is the one a later batch may fill. `new`'s failures are appended to `_meta.failed`,
    and its span, `account`, repeats and cell names become one entry of `_meta.batches`.
    """
    out = json.loads(json.dumps(base))
    meta, nmeta = out["_meta"], new["_meta"]
    clash = {c for c, r in nmeta.get("runs", {}).items() if meta.get("runs", {}).get(c)}
    if clash:
        raise ValueError(f"cells already calibrated, refusing to replace them: {sorted(clash)}")
    for cell, runs in nmeta.get("runs", {}).items():
        model, effort = cell.rsplit("/", 1)
        meta.setdefault("runs", {})[cell] = runs
        meta.setdefault("usage_deltas", {})[cell] = nmeta.get("usage_deltas", {}).get(cell)
        if cell in nmeta.get("cells", {}):
            meta.setdefault("cells", {})[cell] = nmeta["cells"][cell]
        if effort in new.get(model, {}):
            out.setdefault(model, {})[effort] = new[model][effort]
        if effort in new.get("usd", {}).get(model, {}):
            out.setdefault("usd", {}).setdefault(model, {})[effort] = new["usd"][model][effort]
    meta.setdefault("failed", []).extend(nmeta.get("failed", []))
    # `merged` is when this merge was made: for a checkpoint of a batch still running (or one
    # that died) it is the last moment the batch is known to have been sending, so a batch
    # without `finished` still has an end.
    batch = {"started": nmeta.get("started"), "finished": nmeta.get("finished"),
             "merged": datetime.now(timezone.utc).isoformat(), "account": account,
             "repeats": nmeta.get("repeats"), "cells": list(nmeta.get("runs", {}))}
    meta.setdefault("batches", []).append(batch)
    return out


def _meter_log_reader(path: Path) -> Callable:
    """A `read` for calibrate() that takes the last successful reading from a tracker.meter_log log.

    The usage endpoint's limiter is shared by every account on a host and the meters already
    poll it every minute, so a second reader of the same account is refused often and costs the
    other accounts too (tracker/usage_cache.py). The log already holds the reading.
    """
    from .usage_api import Utilization

    def read() -> Utilization:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            fh = row.get("five_hour")
            if isinstance(fh, dict) and fh.get("utilization") is not None:
                sd = row.get("seven_day") or {}
                return Utilization(datetime.fromisoformat(row["ts"]), fh["utilization"],
                                   sd.get("utilization"), fh.get("resets_at"))
        raise RuntimeError(f"no five-hour reading in {path}")
    return read


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Run the effort calibration (spends real usage), "
                                             "or --recompute an existing matrix file without sending anything")
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--models", nargs="*", default=MODELS, help="space- or comma-separated model ids")
    ap.add_argument("--efforts", nargs="*", default=EFFORTS)
    ap.add_argument("--merge", action="store_true",
                    help="add the cells to the existing --out file, keeping every cell it holds; "
                         "cells it already holds are not run again")
    ap.add_argument("--account", default=None,
                    help="the account label the batch runs on, recorded under _meta.batches with --merge")
    ap.add_argument("--meter-log", type=Path, default=None,
                    help="read the five-hour meter from this tracker.meter_log log instead of the usage endpoint")
    ap.add_argument("--out", type=Path, default=None,
                    help="default data/effort_matrix.json, or the --recompute file itself")
    ap.add_argument("--recompute", type=Path, default=None, metavar="PATH",
                    help="re-derive cell values from PATH's _meta.runs; sends nothing")
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"),
                    help="per-model list prices and weights used to value each run in meter dollars")
    a = ap.parse_args(argv)
    a.models = [m for arg in a.models for m in arg.split(",") if m]
    a.efforts = [e for arg in a.efforts for e in arg.split(",") if e]
    out = a.out or a.recompute or Path("data/effort_matrix.json")
    prices = {k: v for k, v in json.loads(a.prices.read_text(encoding="utf-8")).items() if not k.startswith("_")}

    base = json.loads(out.read_text(encoding="utf-8")) if a.merge and not a.recompute else None
    skip = {c for c, r in base["_meta"].get("runs", {}).items() if r} if base else set()

    def write(m: dict) -> None:
        write_text_atomic(out, json.dumps(m, indent=1) + "\n")

    def checkpoint(m: dict) -> None:
        write(merge(base, m, a.account) if base is not None else m)

    if a.recompute:
        m = recompute(json.loads(a.recompute.read_text(encoding="utf-8")), prices)
        m["_meta"]["recomputed_from"] = str(a.recompute)
        m["_meta"]["recomputed"] = datetime.now(timezone.utc).isoformat()
    else:
        import time

        from .cli_run import run_prompt
        from .usage_api import read_usage
        cfg = a.config_dir or Path.home() / ".claude"
        read = _meter_log_reader(a.meter_log) if a.meter_log else (lambda: read_usage(cfg))
        m = calibrate(a.models, a.efforts, a.repeats,
                      lambda p, mo, ef: run_prompt(p, mo, ef, a.config_dir), read, time.sleep,
                      checkpoint=checkpoint, prices=prices, skip=skip)
        if base is not None:
            m = merge(base, m, a.account)
    write(m)
    print(json.dumps({k: v for k, v in m.items() if not k.startswith("_")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
