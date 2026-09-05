"""Tick probe: loop a fixed small prompt until 5-hour utilization ticks twice."""
from __future__ import annotations
import json
import random
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable
from .usage_api import Utilization, same_reset
from .cli_run import RunUsage

CLASSES = ("input", "output", "cache_read", "cache_write")

PROBE_PAYLOAD_WORDS = 9000  # about 42k tokens: roughly 0.15% of a Max 20x window on Sonnet 5
_WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
          "mike november oscar papa quebec romeo sierra tango").split()
PROBE_PROMPT = "Below is a list of tokens. Reply with only the word DONE.\n\n"


def probe_prompt(salt: str, index: int, words: int = PROBE_PAYLOAD_WORDS) -> str:
    """A fixed-size prompt that is unique per (salt, index).

    An identical prompt re-sent within the cache TTL is served as a cache read, which the
    usage meter weighs far lighter than the cache write of the first send; uniqueness keeps
    every prompt the same class and the same cost. The size is deterministic.
    """
    rng = random.Random(f"{salt}:{index}")
    return PROBE_PROMPT + " ".join(f"{rng.choice(_WORDS)}{rng.randint(0, 999)}" for _ in range(words))


class ProbeAbort(Exception):
    pass


@dataclass
class ProbeResult:
    ts: datetime
    model: str
    effort: str
    tokens_per_pct: float
    tokens: dict
    prompts: int
    tick_from: float
    tick_to: float
    elapsed_s: float


def _same_window(a: Utilization, b: Utilization) -> bool:
    if not same_reset(a.five_hour_resets_at, b.five_hour_resets_at):
        return False
    return (b.five_hour or 0) >= (a.five_hour or 0)


def run_tick_probe(model: str, effort: str, prompt: str, read: Callable[[], Utilization],
                   run: Callable[[int], RunUsage], sleep: Callable[[float], None],
                   now: Callable[[], datetime], max_prompts: int = 40, settle_s: float = 60) -> ProbeResult:
    start = now()
    before = read()
    last = before
    prompts = 0
    tick1: int | None = None
    spent = {c: 0 for c in CLASSES}
    while prompts < max_prompts:
        u = run(prompts)
        prompts += 1
        if tick1 is not None:
            for c in CLASSES:
                spent[c] += getattr(u, c)
        sleep(settle_s)
        cur = read()
        if not _same_window(last, cur):
            raise ProbeAbort("window reset during probe")
        jump = (cur.five_hour or 0) - (last.five_hour or 0)
        if jump >= 2:
            raise ProbeAbort(f"utilization jumped {jump}% after one prompt; account not idle")
        if jump >= 1:
            if tick1 is None:
                tick1 = int(cur.five_hour)
            else:
                elapsed = (now() - start).total_seconds()
                total = sum(spent.values())
                return ProbeResult(start, model, effort, total / jump, spent, prompts, tick1, int(cur.five_hour), elapsed)
        last = cur
    raise ProbeAbort(f"no second tick after {prompts} prompts")


def is_idle(read: Callable[[], Utilization], sleep: Callable[[float], None], window_s: float = 120) -> bool:
    a = read()
    sleep(window_s)
    b = read()
    return a.five_hour == b.five_hour and _same_window(a, b)


def choose_account(accounts: list[tuple[str, Path]], read_for: Callable[[str], Callable[[], Utilization]],
                   sleep: Callable[[float], None], max_wait_s: float = 4 * 3600, retry_s: float = 900):
    waited = 0.0
    while True:
        for name, cfg in accounts:
            if is_idle(read_for(name), sleep):
                return (name, cfg)
        if waited >= max_wait_s:
            return None
        sleep(retry_s)
        waited += retry_s


def append_result(path: Path, r: ProbeResult, account: str) -> None:
    row = asdict(r)
    row["ts"] = r.ts.isoformat()
    row["account"] = account
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys
    import time
    from datetime import timezone
    from .usage_api import read_usage
    from .cli_run import run_prompt
    ap = argparse.ArgumentParser(description="Run one tick probe and append to probes.jsonl")
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default="low")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("PROBE_OUT", "probes.jsonl")))
    ap.add_argument("--account", action="append", default=[],
                    help="name=config_dir, in priority order; default dave and jono from $HOME")
    ap.add_argument("--max-wait", type=int, default=4 * 3600)
    a = ap.parse_args(argv)
    home = Path.home()
    accounts = [tuple(x.split("=", 1)) for x in a.account] or [("dave", home / ".claude-dave"), ("jono", home / ".claude-javiswork")]
    accounts = [(n, Path(p)) for n, p in accounts]
    account_cfgs = dict(accounts)

    def read_for(name):
        return lambda: read_usage(account_cfgs[name])

    picked = choose_account(accounts, read_for, time.sleep, max_wait_s=a.max_wait)
    if picked is None:
        print("probe skipped: no idle account within max wait", file=sys.stderr)
        return 3
    name, cfg = picked
    try:
        salt = datetime.now(timezone.utc).isoformat()
        r = run_tick_probe(a.model, a.effort, PROBE_PROMPT, lambda: read_usage(cfg),
                           lambda i: run_prompt(probe_prompt(salt, i), a.model, a.effort, cfg),
                           time.sleep, lambda: datetime.now(timezone.utc))
    except ProbeAbort as e:
        print(f"probe aborted on {name}: {e}", file=sys.stderr)
        return 4
    append_result(a.out, r, account=name)
    print(f"{name} {a.model} {a.effort}: {r.tokens_per_pct:.0f} tokens per 1% ({r.prompts} prompts, ticks {r.tick_from}->{r.tick_to})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
