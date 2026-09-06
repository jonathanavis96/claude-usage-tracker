"""Tick probe: loop a fixed small prompt until the 5-hour utilization meter has advanced
several percent past the first observed tick, and report tokens spent over that span.

A single tick-to-tick span is noisy: the meter reports ticks with a variable lag of
several prompts, so two probes with identical per-prompt cost can see very different
prompt counts (and therefore very different tokens-per-1%) between one tick and the next.
Summing spend over several ticks and dividing by the total percent advanced averages that
lag out.
"""
from __future__ import annotations
import json
import random
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from .usage_api import Utilization, same_reset
from .cli_run import RunUsage

CLASSES = ("input", "output", "cache_read", "cache_write")

# A 1% tick of a Max 20x window is worth far more than this in API-list terms, so a tick
# our own prompts cannot pay for came from someone else's traffic on the same account.
MIN_TICK_USD = 0.40

PROBE_PAYLOAD_WORDS = 12000  # prose measures ~3.47 tokens/word: ~42k tokens, matching the old payload's size
_SUBJECTS = ("the harbour master", "the shift supervisor", "the finance clerk", "the site foreman",
             "the duty officer", "the regional auditor", "the warehouse manager", "the compliance lead")
_VERBS = ("postponed", "reviewed", "confirmed", "escalated", "archived", "reissued", "verified", "logged")
_OBJECTS = ("the tide tables", "the expense report", "the delivery schedule", "the safety checklist",
            "the vendor invoice", "the shift roster", "the incident log", "the maintenance order")
_TAILS = ("before the holiday", "after the audit", "ahead of schedule", "during the handover",
          "prior to closing", "following the inspection", "without further delay", "pending final review")
PROBE_PROMPT = "Below is a long log of routine office notes. Reply with only the word DONE.\n\n"


def probe_prompt(salt: str, index: int, words: int = PROBE_PAYLOAD_WORDS) -> str:
    """A fixed-size prose prompt that is unique per (salt, index).

    Fable 5.1 reroutes a non-language payload (a list of tokens) to Opus 5 to serve it; the
    CLI's own JSON reports the substitute model in modelUsage, and cli_run refuses the run
    once it sees a model other than the one asked for. A payload of ordinary English
    sentences is served by Fable as asked, so the probe payload must read as prose, not as
    a token list.

    Sentences are assembled from four short phrase lists (subject, verb, object, tail) plus
    a random 4-digit reference number, one sentence per line, generated until the word count
    reaches `words`. An identical prompt re-sent within the cache TTL is served as a cache
    read, which the usage meter weighs far lighter than the cache write of the first send;
    uniqueness by (salt, index) keeps every prompt the same class and the same cost.
    """
    rng = random.Random(f"{salt}:{index}")
    lines = []
    total = 0
    while total < words:
        subject = rng.choice(_SUBJECTS).capitalize()
        verb = rng.choice(_VERBS)
        obj = rng.choice(_OBJECTS)
        tail = rng.choice(_TAILS)
        ref = rng.randint(1000, 9999)
        line = f"{subject} {verb} {obj} {tail} (ref {ref})."
        lines.append(line)
        total += len(line.split())
    return PROBE_PROMPT + "\n".join(lines)


_ROLES = ("a lighthouse keeper", "an overnight baker", "a subway dispatcher", "a wildlife vet",
          "a night-shift nurse", "a ferry captain", "a beekeeper", "a glacier guide")
_PLACES = ("in a coastal fishing town", "in a mountain village", "in a desert outpost",
           "in a river delta city", "on a remote island", "in a highland farming valley",
           "in a rainforest research station", "in an arctic research base")
_SEASONS = ("during the monsoon season", "during a harsh winter", "during the height of summer",
            "during the spring thaw", "during the autumn harvest", "during a long drought",
            "during the rainy season", "during the first snowfall")


def output_prompt(salt: str, index: int) -> str:
    """A short, unique-per-(salt, index) prompt whose entire cost is in the reply.

    Where `probe_prompt` measures cache-write-heavy traffic (a big payload, a one-word
    reply), this measures output-heavy traffic: a prompt under 200 words asking for
    roughly 4,000 words of original narrative prose, so nearly all the token spend lands
    on the output side of the ledger instead of the input side.

    The topic is a deterministic triple (role, place, season) picked from short phrase
    lists by `random.Random(f"{salt}:{index}")`, the same uniqueness contract as
    `probe_prompt`, so no two prompts in a run are identical and none can be served from
    cache.
    """
    rng = random.Random(f"{salt}:{index}")
    role = rng.choice(_ROLES)
    place = rng.choice(_PLACES)
    season = rng.choice(_SEASONS)
    return (
        f"Write approximately 4,000 words of original narrative prose about a day in the "
        f"life of {role} {place} {season}. Write it as continuous prose: no lists, no "
        f"headings, no bullet points, no section breaks, just plain paragraphs telling the "
        f"story in order. Invent whatever specific detail you need; none of it needs to be "
        f"factual. Keep writing until you reach roughly 4,000 words, then stop; do not "
        f"summarize, do not wrap up early, and do not add any closing remarks after the "
        f"story ends."
    )


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
    seven_day_before: float | None = None
    seven_day_after: float | None = None
    readings: list = field(default_factory=list)
    payload: str = "prose"


def _same_window(a: Utilization, b: Utilization) -> bool:
    if not same_reset(a.five_hour_resets_at, b.five_hour_resets_at):
        return False
    return (b.five_hour or 0) >= (a.five_hour or 0)


def run_tick_probe(model: str, effort: str, prompt: str, read: Callable[[], Utilization],
                   run: Callable[[int], RunUsage], sleep: Callable[[float], None],
                   now: Callable[[], datetime], max_prompts: int = 80, settle_s: float = 60,
                   deadline: datetime | None = None, usd_per_token: dict | None = None,
                   ticks: int = 5, payload: str = "prose") -> ProbeResult:
    """Loop prompts until the 5-hour meter has advanced `ticks` percent past the first
    observed tick, and report tokens spent over that whole span.

    A single tick-to-tick span sees a variable lag of several prompts, so it is too noisy
    to publish alone; summing spend across `ticks` ticks and dividing by the total percent
    advanced averages that lag out. `deadline` is the one wall-clock budget for the whole
    run: every sleep is charged against it and the probe aborts once it passes.
    `usd_per_token` is the model's entry from data/prices.json (USD per million tokens);
    when given, a span our own prompts cannot pay for is rejected rather than published
    as a rate. `payload` is recorded on the result only, so the publisher can tell a
    cache-write-heavy prose probe apart from an output-heavy one; the actual prompt text
    comes from `run`, not from this function.
    """
    from .publish import tokens_usd
    start = now()
    before = read()
    last = before
    prompts = 0
    tick1: int | None = None
    advanced = 0
    spent = {c: 0 for c in CLASSES}
    readings = []
    while prompts < max_prompts:
        if deadline is not None and now() >= deadline:
            raise ProbeAbort("deadline")
        u = run(prompts)
        prompts += 1
        if tick1 is not None:
            for c in CLASSES:
                spent[c] += getattr(u, c)
        sleep(settle_s)
        if deadline is not None and now() >= deadline:
            raise ProbeAbort("deadline")
        cur = read()
        readings.append({"prompt": prompts, "five_hour": cur.five_hour,
                          "tokens": {c: getattr(u, c) for c in CLASSES}})
        print(f"prompt {prompts}: five_hour={cur.five_hour} resets_at={cur.five_hour_resets_at} "
              f"spent=input={u.input} output={u.output} cache_read={u.cache_read} cache_write={u.cache_write}",
              file=sys.stderr)
        if not _same_window(last, cur):
            raise ProbeAbort("window reset during probe")
        jump = (cur.five_hour or 0) - (last.five_hour or 0)
        if jump >= 2:
            raise ProbeAbort(f"utilization jumped {jump}% after one prompt; account not idle")
        if jump >= 1:
            if tick1 is None:
                tick1 = int(cur.five_hour)
            else:
                advanced += jump
                if advanced >= ticks:
                    if usd_per_token is not None and tokens_usd(spent, usd_per_token) < MIN_TICK_USD * advanced:
                        raise ProbeAbort("tick too early")
                    elapsed = (now() - start).total_seconds()
                    total = sum(spent.values())
                    return ProbeResult(start, model, effort, total / advanced, spent, prompts, tick1,
                                       int(cur.five_hour), elapsed, before.seven_day, cur.seven_day, readings,
                                       payload)
        last = cur
    raise ProbeAbort(f"no second tick after {prompts} prompts")


def is_idle(read: Callable[[], Utilization], sleep: Callable[[float], None], window_s: float = 120) -> bool:
    a = read()
    sleep(window_s)
    b = read()
    return a.five_hour == b.five_hour and _same_window(a, b)


def choose_account(accounts: list[tuple[str, Path]], read_for: Callable[[str], Callable[[], Utilization]],
                   sleep: Callable[[float], None], max_wait_s: float = 4 * 3600, retry_s: float = 900,
                   now: Callable[[], datetime] | None = None, deadline: datetime | None = None):
    """Return the first idle account, or None once the wait or the deadline runs out."""
    waited = 0.0
    while True:
        if deadline is not None and now is not None and now() >= deadline:
            return None
        for name, cfg in accounts:
            if is_idle(read_for(name), sleep):
                return (name, cfg)
        if waited >= max_wait_s:
            return None
        if deadline is not None and now is not None and now() >= deadline:
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
    from datetime import timedelta, timezone
    from .usage_api import _default_fetch, read_usage
    from .cli_run import run_prompt
    ap = argparse.ArgumentParser(description="Run one tick probe and append to probes.jsonl")
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default="low")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("PROBE_OUT", "probes.jsonl")))
    ap.add_argument("--account", action="append", default=[],
                    help="name=config_dir, in priority order; default dave and jono from $HOME")
    ap.add_argument("--max-wait", type=int, default=4 * 3600)
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--ticks", type=int, default=5, help="percent to advance past the first tick before reporting")
    ap.add_argument("--payload", choices=("prose", "output"), default="prose",
                    help="prose: cache-write-heavy fixed payload with a one-word reply (default). "
                         "output: short prompt asking for ~4,000 words of reply, to measure output-token weight")
    a = ap.parse_args(argv)
    builder = output_prompt if a.payload == "output" else probe_prompt
    home = Path.home()
    accounts = [tuple(x.split("=", 1)) for x in a.account] or [("dave", home / ".claude-dave"), ("jono", home / ".claude-javiswork")]
    accounts = [(n, Path(p)) for n, p in accounts]
    account_cfgs = dict(accounts)

    def clock():
        return datetime.now(timezone.utc)

    prices = json.loads(a.prices.read_text(encoding="utf-8"))
    usd_per_token = prices.get(a.model)
    if usd_per_token is None:
        print(f"no price for {a.model} in {a.prices}", file=sys.stderr)
        return 4
    # One wall-clock budget for the whole run: waiting for an idle account and the probe
    # itself share it, so a slow start cannot push the probe into the next cron slot.
    deadline = clock() + timedelta(seconds=a.max_wait)

    def deadline_sleep(seconds: float) -> None:
        # Charged against the same budget: a 429 backoff must not outlive the run.
        remaining = (deadline - clock()).total_seconds()
        if remaining <= 0:
            raise ProbeAbort("deadline")
        time.sleep(min(seconds, remaining))

    def fetch(url, headers):
        return _default_fetch(url, headers, sleep=deadline_sleep)

    def read_for(name):
        return lambda: read_usage(account_cfgs[name], fetch=fetch)
    picked = choose_account(accounts, read_for, time.sleep, max_wait_s=a.max_wait,
                            now=clock, deadline=deadline)
    if picked is None:
        print("probe skipped: no idle account within max wait", file=sys.stderr)
        return 3
    name, cfg = picked
    try:
        salt = clock().isoformat()
        r = run_tick_probe(a.model, a.effort, PROBE_PROMPT, lambda: read_usage(cfg, fetch=fetch),
                           lambda i: run_prompt(builder(salt, i), a.model, a.effort, cfg),
                           time.sleep, clock, deadline=deadline, usd_per_token=usd_per_token, ticks=a.ticks,
                           payload=a.payload)
    except ProbeAbort as e:
        print(f"probe aborted on {name}: {e}", file=sys.stderr)
        return 4
    append_result(a.out, r, account=name)
    print(f"{name} {a.model} {a.effort}: {r.tokens_per_pct:.0f} tokens per 1% ({r.prompts} prompts, ticks {r.tick_from}->{r.tick_to})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
