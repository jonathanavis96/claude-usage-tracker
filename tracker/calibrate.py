"""One-off effort calibration: tokens one representative task burns per model and effort.

Sends a fixed prompt (CALIBRATION_PROMPT) `repeats` times per (model, effort) cell and
records the median total token count. The usage endpoint 429s when read more than about
once a minute per token, so it is read at most once before and once after each cell's
batch of runs -- never per run. Because the prompt is fixed and sent verbatim, later runs
within a cell (and across efforts/models sharing a cache window) can be served from cache:
the per-run class breakdown (input/output/cache_read/cache_write) is recorded under
`_meta.runs` so that shift from cache_write to cache_read is visible rather than hidden
inside the total.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Callable

MODELS = ["claude-sonnet-5", "claude-opus-5", "claude-fable-5-1"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

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


def calibrate(models: list[str], efforts: list[str], repeats: int, run: Callable, read: Callable, sleep: Callable,
              checkpoint: Callable[[dict], None] | None = None) -> dict:
    """Run every cell; a run that fails twice is skipped and its cell left out (recorded under _meta.failed).

    checkpoint, when given, is called with the matrix after every cell so a long real run survives a crash.
    """
    matrix: dict = {"_meta": {"repeats": repeats, "started": datetime.now(timezone.utc).isoformat(),
                               "usage_deltas": {}, "runs": {}, "failed": []}}
    for model in models:
        matrix[model] = {}
        for effort in efforts:
            before = read()
            totals = []
            runs = []
            for _ in range(repeats):
                try:
                    u = run(CALIBRATION_PROMPT, model, effort)
                except Exception:  # one retry, then give up on this run
                    sleep(10)
                    try:
                        u = run(CALIBRATION_PROMPT, model, effort)
                    except Exception as e2:
                        matrix["_meta"]["failed"].append({"cell": f"{model}/{effort}", "error": str(e2)[:200]})
                        continue
                totals.append(u.total)
                runs.append({"input": u.input, "output": u.output,
                             "cache_read": u.cache_read, "cache_write": u.cache_write, "total": u.total})
                sleep(3)
            after = read()
            cell = f"{model}/{effort}"
            if totals:
                matrix[model][effort] = round(median(totals))
            matrix["_meta"]["usage_deltas"][cell] = (after.five_hour or 0) - (before.five_hour or 0)
            matrix["_meta"]["runs"][cell] = runs
            if checkpoint:
                checkpoint(matrix)
    matrix["_meta"]["finished"] = datetime.now(timezone.utc).isoformat()
    return matrix


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time
    from .cli_run import run_prompt
    from .usage_api import read_usage
    ap = argparse.ArgumentParser(description="Run the effort calibration (spends real usage)")
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--efforts", nargs="*", default=EFFORTS)
    ap.add_argument("--out", type=Path, default=Path("data/effort_matrix.json"))
    a = ap.parse_args(argv)
    cfg = a.config_dir or Path.home() / ".claude"
    def write(m: dict) -> None:
        tmp = a.out.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8")
        tmp.replace(a.out)

    m = calibrate(a.models, a.efforts, a.repeats,
                  lambda p, mo, ef: run_prompt(p, mo, ef, a.config_dir), lambda: read_usage(cfg), time.sleep,
                  checkpoint=write)
    write(m)
    print(json.dumps({k: v for k, v in m.items() if not k.startswith("_")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
