"""Tick probe: send prompts until the 5-hour utilization meter has advanced a chosen
number of ticks past the first observed tick, and report tokens spent per 1%.

A single tick-to-tick span is noisy: the meter reports ticks with a variable lag of
several prompts, so two probes with identical per-prompt cost can see very different
prompt counts (and therefore very different tokens-per-1%) between one tick and the next.
Summing spend over several ticks and dividing by the total percent advanced averages that
lag out. Defaults: 3 measured ticks (`--ticks`), after 1 skipped span (`--skip`).

Every run needs an expectation, `--expect-tokens-per-pct` (the tokens per 1% the probe
assumes before it starts, normally the last published rate for the model). It sizes the
prompt and the bursts:

Prompt size. `payload_words_for(expectation)` picks the prose payload size so one prompt's
total tokens — payload plus the fixed per-prompt overhead (`FIXED_PROMPT_TOKENS`: the
CLI's ~11,480-token system-prefix cache_read plus a few input/output tokens, paid on
every prompt regardless of payload) — are about a twelfth of a tick (`PROMPTS_PER_TICK`),
clamped to 500 to 12,000 words, and the row records it as `payload_words`. Sizing
PROMPTS_PER_TICK to 12 rather than 10 keeps a span at 8+ prompts even when the true rate
runs 30% below the expectation that sized it. An output payload keeps its fixed
4,000-word reply and records that.

Burst-first spans. Every span, the alignment span included, opens with one burst of
concurrent prompts sized to 80% of the expected span (expected tokens per 1% over the
tokens per prompt seen so far, or the word estimate before any prompt has run), then
single prompts until the tick. The reading after a burst records `"burst": K`.

Early tick. A tick that arrives during a burst in a full span means the span was shorter
than expected and the limit has probably fallen: the row is flagged `early_tick` and
every later burst is sized to 50% of the expected span instead. A tick inside the
alignment burst is not early when a span is skipped, because the alignment span is a
partial and the span it corrupts is the one skip discards.

Reset wait. If the window's `resets_at` is within 20 minutes when the run starts, the
probe waits for the reset and, if the meter then reads 0.0, starts measuring there with
no alignment span (`reset_start` on the row): 0 is the first tick.

`--settle SECONDS` is the sleep between a prompt returning and the meter being read
(default 60). The row records it as `"settle_s"`.
"""
from __future__ import annotations
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
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

TOKENS_PER_WORD = 3.47  # measured on the prose payload: 12,000 words is about 42k tokens
MIN_PAYLOAD_WORDS = 500  # low enough that a span keeps >= 8 prompts down to about 106k tokens per 1%
MAX_PAYLOAD_WORDS = 12000
MIN_PROMPTS_PER_SPAN = 8  # below this the +-1 prompt quantisation exceeds the drift tolerance
PROBE_PAYLOAD_WORDS = MAX_PAYLOAD_WORDS
PROMPTS_PER_TICK = 12  # a prompt is sized to a twelfth of a tick
# Every prompt carries a fixed overhead beyond its own payload: the CLI's system-prefix
# cache_read plus a handful of input/output tokens. Measured on 2026-09-09
# (history/probes.jsonl, last two rows, single-prompt readings): cache_read 11,473 +
# input 2 + output 5 = 11,480 tokens, regardless of payload size.
FIXED_PROMPT_TOKENS = 11480
BURST_FRACTION = 0.8  # of the expected span, for the burst that opens each span
EARLY_BURST_FRACTION = 0.5  # once a tick has arrived inside a burst
RESET_WAIT_S = 20 * 60  # wait for a window reset this close rather than straddle it
RESET_MARGIN_S = 30  # slack after resets_at before reading the fresh window
OUTPUT_REPLY_WORDS = 4000
_SUBJECTS = ("the harbour master", "the shift supervisor", "the finance clerk", "the site foreman",
             "the duty officer", "the regional auditor", "the warehouse manager", "the compliance lead")
_VERBS = ("postponed", "reviewed", "confirmed", "escalated", "archived", "reissued", "verified", "logged")
_OBJECTS = ("the tide tables", "the expense report", "the delivery schedule", "the safety checklist",
            "the vendor invoice", "the shift roster", "the incident log", "the maintenance order")
_TAILS = ("before the holiday", "after the audit", "ahead of schedule", "during the handover",
          "prior to closing", "following the inspection", "without further delay", "pending final review")
PROBE_PROMPT = "Below is a long log of routine office notes. Reply with only the word DONE.\n\n"


def payload_words_for(expect_tokens_per_pct: float) -> int:
    """Prose payload size so one prompt's total tokens (payload plus the fixed per-prompt
    overhead) cost about a twelfth of a tick, within the word range.

    Every prompt also pays FIXED_PROMPT_TOKENS regardless of payload size, so that
    overhead is subtracted from the per-prompt target before converting the remainder
    to words. If what's left after subtracting overhead would size below
    MIN_PAYLOAD_WORDS, MIN_PAYLOAD_WORDS is used instead (the clamp below already
    covers this, since a negative or tiny target rounds under the minimum).
    """
    target_tokens = expect_tokens_per_pct / PROMPTS_PER_TICK - FIXED_PROMPT_TOKENS
    words = round(target_tokens / TOKENS_PER_WORD)
    return max(MIN_PAYLOAD_WORDS, min(MAX_PAYLOAD_WORDS, words))


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


def output_prompt(salt: str, index: int, words: int = OUTPUT_REPLY_WORDS) -> str:
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
        f"Write approximately {words:,} words of original narrative prose about a day in the "
        f"life of {role} {place} {season}. Write it as continuous prose: no lists, no "
        f"headings, no bullet points, no section breaks, just plain paragraphs telling the "
        f"story in order. Invent whatever specific detail you need; none of it needs to be "
        f"factual. Keep writing until you reach roughly {words:,} words, then stop; do not "
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
    payload_words: int = PROBE_PAYLOAD_WORDS
    ticks: int = 3
    skip: int = 1
    settle_s: float = 60
    expect_tokens_per_pct: float | None = None
    early_tick: bool = False
    reset_start: bool = False
    # The whole run's five-hour meter reads: the very first read (before any prompt,
    # including any reset-wait) and the last read at exit -- so alignment and skip
    # spans count too, unlike tick_from/tick_to which mark only the measured span.
    # Mirrors seven_day_before/after, which measure the same whole-run span.
    five_hour_before: float | None = None
    five_hour_after: float | None = None
    # The account's own weekly reset boundary, so probe_weekly_windows can bucket
    # by the real weekly window instead of falling back to the ISO week. Taken
    # from the last reading (`cur`, at exit) rather than the first, since that
    # is the reading closest to the moment the run's d7 is attributed.
    seven_day_resets_at: str | None = None


def _same_window(a: Utilization, b: Utilization) -> bool:
    if not same_reset(a.five_hour_resets_at, b.five_hour_resets_at):
        return False
    return (b.five_hour or 0) >= (a.five_hour or 0)


def _sum_usage(usages: list[RunUsage]) -> dict:
    return {c: sum(getattr(u, c) for u in usages) for c in CLASSES}


def _seconds_until_reset(u: Utilization, now: datetime) -> float | None:
    """Seconds from `now` to the window's resets_at, or None when it is absent or unreadable."""
    if not u.five_hour_resets_at:
        return None
    try:
        return (datetime.fromisoformat(u.five_hour_resets_at) - now).total_seconds()
    except (ValueError, TypeError):
        return None


def run_tick_probe(model: str, effort: str, prompt: str, read: Callable[[], Utilization],
                   run: Callable[[int], RunUsage], sleep: Callable[[float], None],
                   now: Callable[[], datetime], max_prompts: int = 80, settle_s: float = 60,
                   deadline: datetime | None = None, usd_per_token: dict | None = None,
                   ticks: int = 3, payload: str = "prose", skip: int = 1,
                   expect_tokens_per_pct: float | None = None,
                   payload_words: int = PROBE_PAYLOAD_WORDS,
                   busy: Callable[[], str | None] | None = None) -> ProbeResult:
    """Send prompts until the 5-hour meter has advanced `ticks` percent past the first
    measured tick, and report tokens spent over that whole span.

    A single tick-to-tick span sees a variable lag of several prompts, so it is too noisy
    to publish alone; summing spend across `ticks` ticks and dividing by the total percent
    advanced averages that lag out. `deadline` is the one wall-clock budget for the whole
    run: every sleep is charged against it and the probe aborts once it passes.
    `usd_per_token` is the model's entry from data/prices.json (USD per million tokens);
    when given, a span our own prompts cannot pay for is rejected rather than published
    as a rate. `payload` and `payload_words` are recorded on the result only; the actual
    prompt text comes from `run`, not from this function.

    `skip` discards the first `skip` spans after alignment: spend only accumulates once
    tick (1+skip) has been seen, and that tick is the result's `tick_from`.

    With `expect_tokens_per_pct`, every span opens with a burst of concurrent prompts
    sized to `BURST_FRACTION` of the expected span, then singles until the tick; the
    reading after a burst carries `"burst": K`. A tick that lands inside a burst in a
    full span flags `early_tick` and shrinks later bursts to `EARLY_BURST_FRACTION`.
    After a burst of K the account-not-idle check allows a jump of up to K percent
    instead of one; a run whose final jump carries the meter past `ticks` is still valid
    (the extra percent is in the division). Without an expectation every prompt is single.

    If the window resets within `RESET_WAIT_S` of the start, the probe sleeps until just
    after the reset; a meter reading 0.0 there is taken as the first tick (`reset_start`),
    so no alignment span is spent. Any other reading falls back to normal alignment.

    `busy` is the account-not-idle guard, checked after every meter reading: a reason
    aborts the run with `account not idle: <reason>` and writes no row. It is what makes
    the relaxed pre-check safe: a session that goes busy mid-probe is caught even when
    its spend stays inside the burst size and so never trips the jump check (issue #19).
    `main` passes `SessionWatch.check`, which compares the session state at this reading
    with the state at the previous one, so a turn that both starts and finishes between
    two readings is caught as well as one still running when the meter is read.
    """
    from .publish import tokens_usd
    start = now()
    before = read()
    reset_start = False
    tick1: int | None = None
    tick_from: int | None = None  # the tick at which measurement started
    to_reset = _seconds_until_reset(before, now())
    if to_reset is not None and 0 <= to_reset <= RESET_WAIT_S:
        print(f"window resets in {to_reset:.0f}s: waiting for it", file=sys.stderr)
        sleep(to_reset + RESET_MARGIN_S)
        if deadline is not None and now() >= deadline:
            raise ProbeAbort("deadline")
        before = read()
        if before.five_hour == 0:
            reset_start = True
            tick1 = 0
            if skip <= 0:
                tick_from = 0
    last = before
    prompts = 0
    skipped = 0
    advanced = 0
    spent = {c: 0 for c in CLASSES}
    readings = []
    prompt_totals: list[int] = []  # per-prompt token totals, for the burst estimate
    early_tick = False
    fraction = BURST_FRACTION
    burst_pending = True  # every span, alignment included, opens with a burst
    while prompts < max_prompts:
        if deadline is not None and now() >= deadline:
            raise ProbeAbort("deadline")
        k = 1
        if burst_pending:
            burst_pending = False
            k = _burst_size(expect_tokens_per_pct, fraction, prompt_totals, payload_words,
                            max_prompts - prompts)
        if k > 1:
            with ThreadPoolExecutor(max_workers=k) as pool:
                usages = list(pool.map(run, range(prompts, prompts + k)))
        else:
            usages = [run(prompts)]
        prompts += len(usages)
        batch = _sum_usage(usages)
        prompt_totals.extend(u.total for u in usages)
        if tick_from is not None:
            for c in CLASSES:
                spent[c] += batch[c]
        sleep(settle_s)
        if deadline is not None and now() >= deadline:
            raise ProbeAbort("deadline")
        cur = read()
        reading = {"prompt": prompts, "five_hour": cur.five_hour, "tokens": batch}
        if k > 1:
            reading["burst"] = k
        readings.append(reading)
        print(f"prompt {prompts}{f' (burst of {k})' if k > 1 else ''}: five_hour={cur.five_hour} "
              f"resets_at={cur.five_hour_resets_at} spent=input={batch['input']} output={batch['output']} "
              f"cache_read={batch['cache_read']} cache_write={batch['cache_write']}", file=sys.stderr)
        if busy is not None:
            reason = busy()
            if reason is not None:
                raise ProbeAbort(f"account not idle: {reason}")
        if not _same_window(last, cur):
            raise ProbeAbort("window reset during probe")
        jump = (cur.five_hour or 0) - (last.five_hour or 0)
        if jump > k:
            raise ProbeAbort(f"utilization jumped {jump}% after {k} prompt{'s' if k > 1 else ''}; account not idle")
        if jump >= 1:
            burst_pending = True
            if k > 1 and not (tick1 is None and skip > 0):
                early_tick = True
                fraction = EARLY_BURST_FRACTION
            if tick1 is None:
                tick1 = int(cur.five_hour)
                if skip <= 0:
                    tick_from = tick1
            elif tick_from is None:
                skipped += jump
                if skipped >= skip:
                    tick_from = int(cur.five_hour)
            else:
                advanced += jump
                if advanced >= ticks:
                    if usd_per_token is not None and tokens_usd(spent, usd_per_token) < MIN_TICK_USD * advanced:
                        raise ProbeAbort("tick too early")
                    elapsed = (now() - start).total_seconds()
                    total = sum(spent.values())
                    return ProbeResult(start, model, effort, total / advanced, spent, prompts, tick_from,
                                       int(cur.five_hour), elapsed, before.seven_day, cur.seven_day, readings,
                                       payload, payload_words=payload_words, ticks=ticks, skip=skip,
                                       settle_s=settle_s, expect_tokens_per_pct=expect_tokens_per_pct,
                                       early_tick=early_tick, reset_start=reset_start,
                                       five_hour_before=before.five_hour, five_hour_after=cur.five_hour,
                                       seven_day_resets_at=cur.seven_day_resets_at)
        last = cur
    raise ProbeAbort(f"no second tick after {prompts} prompts")


def _burst_size(expect_tokens_per_pct: float | None, fraction: float, prompt_totals: list[int],
                payload_words: int, room: int) -> int:
    """How many prompts open the span concurrently: 1 means a single prompt.

    The burst is `fraction` of the expected span in prompts: `expect_tokens_per_pct` over
    the mean tokens per prompt seen so far, or over the payload's word estimate plus the
    fixed per-prompt overhead before any prompt has run. Without an expectation there is nothing to size it from, so no burst.
    `room` is the prompts left under `max_prompts`.
    """
    if expect_tokens_per_pct is None:
        return 1
    if prompt_totals:
        per_prompt = sum(prompt_totals) / len(prompt_totals)
    else:
        per_prompt = payload_words * TOKENS_PER_WORD + FIXED_PROMPT_TOKENS
    if per_prompt <= 0:
        return 1
    k = min(int(fraction * expect_tokens_per_pct // per_prompt), room)
    return k if k >= 2 else 1


CLAUDE_PROCESS_NAME = "claude"
CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"


def claude_processes(proc: Path = Path("/proc")) -> list[tuple[int, Path]]:
    """(pid, CLAUDE_CONFIG_DIR) for every live `claude` process on this host.

    The probe runs on the same host as the accounts it measures, so a `claude` process
    whose environment names an account's config dir is that account in use, whatever
    its meter says. Processes are found by walking `proc` (normally /proc): a numeric
    entry whose `comm` is `claude`, with the config dir taken from its `environ` or,
    failing that, a `CLAUDE_CONFIG_DIR=...` word on its `cmdline`. A `claude` running on
    the default config dir sets neither and is not listed. Entries that vanish or
    cannot be read mid-walk are skipped, and a host without /proc lists nothing.
    """
    found: list[tuple[int, Path]] = []
    try:
        entries = sorted(proc.iterdir())
    except OSError:
        return found
    prefix = f"{CONFIG_DIR_VAR}=".encode()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_bytes().strip() != CLAUDE_PROCESS_NAME.encode():
                continue
            words = (entry / "environ").read_bytes().split(b"\0")
            try:
                words += (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                pass
        except OSError:
            continue
        for word in words:
            if word.startswith(prefix):
                found.append((int(entry.name), Path(word[len(prefix):].decode(errors="replace"))))
                break
    return found


def busy_pids(cfg: Path, processes: list[tuple[int, Path]]) -> list[int]:
    """The pids among `processes` whose config dir is `cfg` (paths compared resolved)."""
    target = Path(cfg).expanduser().resolve()
    return [pid for pid, d in processes if Path(d).expanduser().resolve() == target]


IDLE_STATUS = "idle"


@dataclass(frozen=True)
class SessionState:
    """One interactive session's state as this host last recorded it.

    `updated_at` is Claude Code's `statusUpdatedAt` in epoch milliseconds. Either
    field is None when the file cannot supply it, and either being None counts as
    busy: a None `status` means the host cannot say what the session is doing, and a
    None `updated_at` means it cannot tell whether the session has done anything
    since the last look.
    """
    status: str | None = None
    updated_at: int | None = None


def read_session_status(cfg: Path, pid: int) -> SessionState:
    """Claude Code's own state for `pid`, from `<cfg>/sessions/<pid>.json`.

    Claude Code (2.1.267 and later) writes that file for every interactive session,
    with a `status` of `idle` at the prompt and `busy` during a turn (`shell` while it
    runs a command, and other values as the CLI gains them), plus a `statusUpdatedAt`
    stamp that moves with every transition. The file goes away when the process exits.

    Returns `SessionState(None, None)` when the file is missing, unreadable, not JSON
    or not an object, and leaves either field None when that field is absent or of the
    wrong type — `statusUpdatedAt` must be a plain integer, so a bool (an `int`
    subclass in Python) does not count.
    """
    try:
        data = json.loads((Path(cfg) / "sessions" / f"{pid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SessionState()
    if not isinstance(data, dict):
        return SessionState()
    status = data.get("status")
    stamp = data.get("statusUpdatedAt")
    return SessionState(status if isinstance(status, str) else None,
                        stamp if isinstance(stamp, int) and not isinstance(stamp, bool) else None)


def session_snapshot(cfg: Path, processes: list[tuple[int, Path]],
                     session_status: Callable[[Path, int], SessionState] = read_session_status,
                     ) -> dict[int, SessionState]:
    """The state of every `claude` session on `cfg`, keyed by pid, in lister order."""
    return {pid: session_status(cfg, pid) for pid in busy_pids(cfg, processes)}


def not_idle_pids(snapshot: dict[int, SessionState]) -> list[int]:
    """The pids in `snapshot` that are not sitting idle at their prompt.

    Presence is not activity: Jonathan keeps interactive sessions open on both probe
    accounts for days, and counting those as the account being in use meant the
    rotation never found an idle account (issue #21). A pid counts as busy unless its
    status is exactly `idle`, so a missing, unreadable or malformed status file — a
    headless run, or a Claude Code too old to write one — still blocks the account as
    it did before, and so does a status value this code has never heard of.

    An `idle` session also counts as busy when it has no `updated_at`. Without a
    timestamp the interval comparison in `SessionWatch.check` is blind for that pid --
    `None` on both sides of every interval reads as "nothing happened" no matter how
    many turns it ran -- so the host cannot vouch for the session and the check fails
    closed. Every real file carries the stamp; one that does not is a file this code
    cannot reason about.
    """
    return [pid for pid, st in snapshot.items()
            if st.status != IDLE_STATUS or st.updated_at is None]


def not_idle_reason(snapshot: dict[int, SessionState]) -> str | None:
    """`"pid 2355887, 2881098"` for the non-idle sessions in `snapshot`, or None when
    every session in it is at its prompt."""
    pids = not_idle_pids(snapshot)
    return "pid " + ", ".join(str(p) for p in pids) if pids else None


class SessionWatch:
    """The account's session state as of the last look, so two looks can be compared.

    A single status reading is not enough. An interactive session that starts and
    finishes a whole turn inside one prompt-plus-settle interval is back at `idle` by
    the time it is sampled, and if its spend moved the meter by no more than the burst
    size the jump check will not catch it either -- so the row is published with
    someone else's traffic in it. Comparing consecutive snapshots closes that: a turn
    that came and went still leaves `statusUpdatedAt` moved.

    `check()` therefore reports activity when any session on the config dir is not
    idle (`pid N`, as the pre-check has always said), when a session's
    `statusUpdatedAt` differs from the previous snapshot (`pid N changed status`), when
    a session is new since it (`pid N started`), or when one has gone (`pid N exited`).
    A session with no usable `statusUpdatedAt` is already not idle by `not_idle_pids`,
    so it is named outright rather than compared against a stamp that cannot move.

    A vanished session counts as activity on purpose. A session that ran a turn and
    then quit is indistinguishable, from the files left behind, from one that was
    closed while sitting at its prompt -- and the two cost very differently. A false
    abort costs one probe slot, which the next cron slot makes up; a missed turn
    pollutes a published rate, which calibration then carries forward. So the cheap
    error is the one to make.
    """

    def __init__(self, cfg: Path, processes: Callable[[], list[tuple[int, Path]]] = claude_processes,
                 session_status: Callable[[Path, int], SessionState] = read_session_status) -> None:
        self.cfg = cfg
        self.processes = processes
        self.session_status = session_status
        self.last: dict[int, SessionState] | None = None

    def _look(self) -> dict[int, SessionState]:
        return session_snapshot(self.cfg, self.processes(), self.session_status)

    def snapshot(self) -> str | None:
        """Record the current state as the baseline, and report anything already busy.

        The returned reason is the instant rule only (`pid N` for a session not at its
        prompt), so a caller can reject an account before spending a meter read.
        """
        self.last = self._look()
        return not_idle_reason(self.last)

    def check(self) -> str | None:
        """Take a fresh snapshot, compare it with the previous one, and report why the
        account is not idle -- or None. The fresh snapshot becomes the new baseline, so
        consecutive calls each cover the interval since the last. Called with no
        baseline yet, only the instant rule applies.
        """
        prev = self.last
        cur = self._look()
        self.last = cur
        clauses = []
        named = not_idle_pids(cur)
        if named:
            clauses.append("pid " + ", ".join(str(p) for p in named))
        if prev is not None:
            for pid, st in cur.items():
                if pid in named:
                    continue  # already named, and for the stronger reason
                if pid not in prev:
                    clauses.append(f"pid {pid} started")
                elif st.updated_at != prev[pid].updated_at:
                    clauses.append(f"pid {pid} changed status")
            clauses += [f"pid {pid} exited" for pid in prev if pid not in cur]
        return "; ".join(clauses) or None


def busy_reason(read: Callable[[], Utilization], sleep: Callable[[float], None], window_s: float = 120,
                cfg: Path | None = None,
                processes: Callable[[], list[tuple[int, Path]]] | None = None,
                session_status: Callable[[Path, int], SessionState] = read_session_status) -> str | None:
    """Why the account is busy, or None when it is idle.

    Two checks, cheapest first. With `cfg` and `processes`, a `claude` session on that
    config dir which is not idle per its `sessions/<pid>.json` is busy (`"pid 491402"`,
    listing only the pids judged busy) and no meter read is spent. Then the meter: two
    reads `window_s` apart must agree on the 5-hour percent within the same window
    (`"meter moved 7% -> 8%"`, `"window reset"`). The meter check alone is too weak on
    its own host: a busy account that is between prompts for two minutes passes it.

    The session state recorded before the window is compared with a fresh look once the
    meter check passes, by the `SessionWatch` rule: a session opened during the sleep
    has not necessarily moved the meter yet, and a session that ran a whole turn inside
    the window is back at `idle` by the end of it but left `statusUpdatedAt` moved.
    Either would otherwise be accepted just as it spends. Without `cfg` and `processes`
    only the meter is consulted. `processes` and `session_status` are the host lister
    and the status reader; tests inject both.
    """
    watch = (SessionWatch(cfg, processes, session_status)
             if cfg is not None and processes is not None else None)
    if watch is not None:
        reason = watch.snapshot()
        if reason is not None:
            return reason
    a = read()
    sleep(window_s)
    b = read()
    if not _same_window(a, b):
        return "window reset"
    if a.five_hour != b.five_hour:
        return f"meter moved {a.five_hour:g}% -> {b.five_hour:g}%"
    return watch.check() if watch is not None else None


def is_idle(read: Callable[[], Utilization], sleep: Callable[[float], None], window_s: float = 120,
            cfg: Path | None = None,
            processes: Callable[[], list[tuple[int, Path]]] | None = None,
            session_status: Callable[[Path, int], SessionState] = read_session_status) -> bool:
    """True when `busy_reason` finds nothing: no non-idle `claude` session on `cfg` (when
    given) and a flat meter across `window_s`."""
    return busy_reason(read, sleep, window_s, cfg, processes, session_status) is None


def _log_stderr(line: str) -> None:
    print(line, file=sys.stderr)


def choose_account(accounts: list[tuple[str, Path]], read_for: Callable[[str], Callable[[], Utilization]],
                   sleep: Callable[[float], None], max_wait_s: float = 4 * 3600, retry_s: float = 900,
                   now: Callable[[], datetime] | None = None, deadline: datetime | None = None,
                   processes: Callable[[], list[tuple[int, Path]]] = claude_processes,
                   session_status: Callable[[Path, int], SessionState] = read_session_status,
                   log: Callable[[str], None] = _log_stderr):
    """Return the first idle account, or None once the wait or the deadline runs out.

    Each rejection is logged as `<name> busy: <reason>` (`dave busy: pid 491402`,
    `dave busy: meter moved 7% -> 8%`) so the probe log explains the choice; a pid
    reason names only the sessions judged busy. `processes` is the host process lister
    (`claude_processes` by default) and `session_status` the per-session status reader
    (`read_session_status`); tests inject both.
    """
    waited = 0.0
    while True:
        if deadline is not None and now is not None and now() >= deadline:
            return None
        for name, cfg in accounts:
            reason = busy_reason(read_for(name), sleep, cfg=cfg, processes=processes,
                                 session_status=session_status)
            if reason is None:
                return (name, cfg)
            log(f"{name} busy: {reason}")
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
                    help="name=config_dir, in priority order; default jwork (~/.claude-javiswork) then dave (~/.claude-dave)")
    ap.add_argument("--max-wait", type=int, default=4 * 3600)
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--ticks", type=int, default=3, help="measured ticks after the skipped spans (default 3)")
    ap.add_argument("--payload", choices=("prose", "output"), default="prose",
                    help="prose: cache-write-heavy fixed payload with a one-word reply (default). "
                         "output: short prompt asking for ~4,000 words of reply, to measure output-token weight")
    ap.add_argument("--skip", type=int, default=1,
                    help="spans to discard after the first observed tick before measuring starts (default 1)")
    ap.add_argument("--expect-tokens-per-pct", type=float, required=True,
                    help="expected tokens per 1%% (the last published rate for the model); sizes the "
                         "prompt to a tenth of a tick and each span's opening burst to 80%% of the span")
    ap.add_argument("--settle", type=float, default=60,
                    help="seconds to wait after a prompt returns before reading the meter (default 60)")
    a = ap.parse_args(argv)
    if a.skip < 0 or a.settle < 0 or a.ticks < 1:
        ap.error("--skip and --settle must not be negative and --ticks must be at least 1")
    if a.expect_tokens_per_pct <= 0:
        ap.error("--expect-tokens-per-pct must be positive")
    if a.payload == "output":
        words = OUTPUT_REPLY_WORDS
        builder = output_prompt
    else:
        words = payload_words_for(a.expect_tokens_per_pct)
        per_prompt = words * TOKENS_PER_WORD + FIXED_PROMPT_TOKENS
        if a.expect_tokens_per_pct / per_prompt < MIN_PROMPTS_PER_SPAN:
            # The fixed CLI overhead alone caps how many prompts fit in one tick, so a
            # low expectation cannot be sized into a well-averaged span. Say so loudly
            # rather than publish a noisy rate.
            print(f"expectation {a.expect_tokens_per_pct:.0f} tokens per 1% fits fewer than "
                  f"{MIN_PROMPTS_PER_SPAN} prompts per tick at the minimum payload; "
                  f"quantisation would exceed the published tolerance", file=sys.stderr)
            return 4
        builder = lambda salt, i: probe_prompt(salt, i, words)  # noqa: E731
    home = Path.home()
    accounts = [tuple(x.split("=", 1)) for x in a.account] or [("jwork", home / ".claude-javiswork"), ("dave", home / ".claude-dave")]
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
        # No max_retries: a 429 is retried until deadline_sleep itself raises
        # ProbeAbort("deadline") once the run's wall-clock budget is gone.
        return _default_fetch(url, headers, sleep=deadline_sleep, max_retries=None)

    def read_for(name):
        return lambda: read_usage(account_cfgs[name], fetch=fetch)
    picked = choose_account(accounts, read_for, time.sleep, max_wait_s=a.max_wait,
                            now=clock, deadline=deadline)
    if picked is None:
        print("probe skipped: no idle account within max wait", file=sys.stderr)
        return 3
    name, cfg = picked
    # The same check the pre-check used, re-run after every meter reading and compared
    # with the state at the previous one, so a session that wakes up mid-probe aborts
    # the run instead of polluting it -- even if its whole turn fits between readings.
    # The baseline is taken here, as close to the first prompt as we can get it.
    watch = SessionWatch(cfg)
    watch.snapshot()
    try:
        salt = clock().isoformat()
        r = run_tick_probe(a.model, a.effort, PROBE_PROMPT, lambda: read_usage(cfg, fetch=fetch),
                           lambda i: run_prompt(builder(salt, i), a.model, a.effort, cfg),
                           time.sleep, clock, deadline=deadline, usd_per_token=usd_per_token, ticks=a.ticks,
                           payload=a.payload, skip=a.skip, expect_tokens_per_pct=a.expect_tokens_per_pct,
                           payload_words=words, settle_s=a.settle, busy=watch.check)
    except ProbeAbort as e:
        print(f"probe aborted on {name}: {e}", file=sys.stderr)
        return 4
    append_result(a.out, r, account=name)
    print(f"{name} {a.model} {a.effort}: {r.tokens_per_pct:.0f} tokens per 1% ({r.prompts} prompts, "
          f"ticks {r.tick_from}->{r.tick_to}, skip {r.skip}, {r.payload_words} words, settle {r.settle_s:g}s"
          f"{', early tick' if r.early_tick else ''}{', reset start' if r.reset_start else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
