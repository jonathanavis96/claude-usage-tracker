"""How fast each model answers, read off Claude Code transcripts (issue #105).

One API response is written to a transcript as one `assistant` line per content block,
all sharing `message.id` and all carrying the response's `usage`. A request is timed
from the last `user` line (a prompt or a tool_result) written before the response's
first block, to the response's last block:

    output speed         = output_tokens / (last block - trigger)
    time to first block  = first block - trigger

Both include queueing and processing the input, and a block is written when it ends, so
"first block" is the end of the first block, not the first token. Kept: at least
MIN_OUTPUT output tokens, MIN_SECONDS < duration < MAX_SECONDS, and `usage.speed` of
"standard" (or absent, on versions that predate the field).

Fast sessions. Some Opus sessions run at about 2.5x the model's usual speed, on the same
account and settings, with nothing between: a session is fast or it is not. Their lines
say `"speed": "standard"` like the rest. Why they run faster is not known
(docs/findings-2026-09-23-model-speed.md, and its correction). On an Opus model, a
request whose session's running median speed (FAST_WINDOW requests around it) is at
least FAST_FACTOR times the model's median over the whole scan is a fast-session
request. They are real measured speeds, so the published figures include them, and a day
when answers came much faster shows as a jump in the ordinary line; each published row
counts them in `fast_session_requests`. A history row still bins them apart
(`fast_output_hist`, `fast_ttfb_hist`), and the publisher pools both. The window lets a
session that slows mid-way keep its normal stretch.

Rows. Each machine turns its own transcripts into daily rows, one per UTC day of the
response's end, model (tracker/turns.py `normalize_model`, the page's ids), account label
(a1..a4, tracker/publish.py ACCOUNT_LABELS) and entrypoint. A row keeps its speeds as a
histogram on a log scale (bins BIN_STEP apart, so any median read back is within 2%)
rather than as a median, so the publisher can pool rows from both machines, and any
split of them, and still take a real median.

History. Claude Code deletes transcripts after 30 days, so rows are appended to a
committed history file and old days are never recomputed: a run replaces only the rows
of days on or after `today - RECOMPUTE_DAYS` (a day is still being written until its last
session ends), and keeps every older row as stored. gs keeps its rows in
history/gs-passive.json's `speed` (tracker/gs_passive.py main, hourly); masterrig in
history/masterrig-speed.json (bin/passive.sh, daily):

    python3 -m tracker.speed --masterrig --history history/masterrig-speed.json
    python3 -m tracker.speed --lines EXTRACT.jsonl.gz --label a1 --history history/masterrig-speed.json

The second form reads a text-free extract of transcript lines (each with a `file` field
naming its transcript) instead of transcripts; it is
how masterrig's rows from before its transcripts were deleted were seeded.
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from .turns import _parse_ts, normalize_model

MIN_OUTPUT = 300
MIN_SECONDS = 1.0
MAX_SECONDS = 900.0
#: A published day needs at least this many requests of the model (per split, too).
MIN_REQUESTS = 30
FAST_FACTOR = 1.6
FAST_WINDOW = 9
#: Fast sessions have been seen only on Opus, so only Opus is classified.
FAST_MODEL_PREFIX = "claude-opus"
BIN_STEP = 1.02
RECOMPUTE_DAYS = 2
SCHEMA = 2


@dataclass(frozen=True)
class Request:
    id: str
    session: str
    model: str
    entrypoint: str
    sidechain: bool
    start: float
    first: float
    end: float
    output: int
    uncached_input: int
    speed: str | None

    @property
    def seconds(self) -> float:
        return self.end - self.start

    @property
    def output_rate(self) -> float:
        return self.output / self.seconds

    @property
    def ttfb(self) -> float:
        return self.first - self.start

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.end, timezone.utc).date().isoformat()


def _lines(path: Path) -> Iterator[dict]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                yield d


def requests_in(lines: Iterable[dict], session: str) -> list[Request]:
    """The timed requests of one transcript's lines, in file order.

    The trigger is tracked per `isSidechain` value, so a sidechain's lines written into
    the main file never start or end a main-chain request. A request with no trigger
    before its first block (a transcript that opens mid-response) is not timed. When a
    response's lines disagree on `output_tokens`, the largest is the final count.

    First and last block are the earliest and latest timestamps, not the first and last
    lines: Claude Code 2.1.278 and 2.1.280 write some responses' blocks a second time
    after the last one, with `output_tokens` 0 and the first block's timestamp, and
    taking the last line as the end made those responses read up to half again as fast.
    """
    trigger: dict[bool, float] = {}
    open_: dict[str, dict] = {}
    for d in lines:
        kind, ts = d.get("type"), d.get("timestamp")
        if kind not in ("user", "assistant") or not ts:
            continue
        t = _parse_ts(ts).timestamp()
        side = bool(d.get("isSidechain"))
        if kind == "user":
            trigger[side] = t
            continue
        m = d.get("message") or {}
        mid, u = m.get("id"), m.get("usage")
        if not mid or not isinstance(u, dict):
            continue
        out = int(u.get("output_tokens") or 0)
        q = open_.get(mid)
        if q is None:
            if side not in trigger:
                continue
            open_[mid] = {"start": trigger[side], "first": t, "end": t, "out": out, "model": m.get("model") or "",
                          "entrypoint": d.get("entrypoint") or "unknown", "side": side, "speed": u.get("speed"),
                          "uncached": int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)}
        else:
            q["first"] = min(q["first"], t)
            q["end"] = max(q["end"], t)
            q["out"] = max(q["out"], out)
    out_list = []
    for mid, q in open_.items():
        model = normalize_model(q["model"])
        if model is None:
            continue
        out_list.append(Request(mid, session, model, q["entrypoint"], q["side"], q["start"], q["first"], q["end"],
                                q["out"], q["uncached"], q["speed"]))
    return out_list


def requests_from_files(paths: Iterable[Path], seen: set[str] | None = None) -> list[Request]:
    """Every timed request in these transcripts, each message id once.

    `seen` is shared across calls so one scan over several accounts counts a response
    once: gs's config dirs share one projects tree, and a resumed session copies its
    history, ids and all, into a new file. The first file in the order given wins.
    """
    seen = set() if seen is None else seen
    out = []
    for p in paths:
        for r in requests_in(_lines(p), str(p)):
            if r.id not in seen:
                seen.add(r.id)
                out.append(r)
    return out


def requests_from_extract(path: Path, seen: set[str] | None = None) -> list[Request]:
    """Timed requests from a text-free extract whose lines carry their transcript as `file`."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for d in _lines(path):
        by_file[d.get("file") or ""].append(d)
    seen = set() if seen is None else seen
    out = []
    for f in sorted(by_file):
        for r in requests_in(by_file[f], f):
            if r.id not in seen:
                seen.add(r.id)
                out.append(r)
    return out


def kept(r: Request) -> bool:
    return (r.output >= MIN_OUTPUT and MIN_SECONDS < r.seconds < MAX_SECONDS
            and r.speed in (None, "standard"))


def fast_sessions(reqs: Iterable[Request], every: bool = False) -> set[str]:
    """Ids of the kept Opus requests in a fast session, by their timing (module docstring).

    With `every`, also the Opus requests too short or too long to be kept whose session's
    running median, over the FAST_WINDOW kept requests nearest them in time, is fast: the
    same rule, read where the request sits in its session rather than from its own speed.
    Speed figures need only the kept ones; a stretch's `fast_session_tokens` takes all of
    them, a fast session's short tool-call requests included. A request in a session with
    no kept request of its model is never counted as fast.
    """
    reqs = list(reqs)
    timed = [r for r in reqs if kept(r)]
    ref = {m: median(v) for m, v in _group(timed, lambda r: r.model, lambda r: r.output_rate).items()}
    fast = set()
    sessions = _group([r for r in timed if r.model.startswith(FAST_MODEL_PREFIX)],
                      lambda r: (r.session, r.model), lambda r: r)
    half = FAST_WINDOW // 2

    def is_fast(rs: list[Request], i: int, model: str) -> bool:
        lo = max(0, min(i - half, len(rs) - FAST_WINDOW))
        return median(x.output_rate for x in rs[lo:lo + FAST_WINDOW]) >= FAST_FACTOR * ref[model]

    for (_, model), rs in sessions.items():
        rs.sort(key=lambda r: r.end)
        for i, r in enumerate(rs):
            if is_fast(rs, i, model):
                fast.add(r.id)
    if every:
        ends = {k: [r.end for r in rs] for k, rs in sessions.items()}
        for r in reqs:
            key = (r.session, r.model)
            if r.id in fast or key not in sessions or kept(r):
                continue
            if is_fast(sessions[key], bisect.bisect_left(ends[key], r.end), r.model):
                fast.add(r.id)
    return fast


def _group(items, key, value) -> dict:
    out: dict = defaultdict(list)
    for it in items:
        out[key(it)].append(value(it))
    return out


def _bin(x: float) -> str:
    return str(math.floor(math.log(x) / math.log(BIN_STEP)))


def daily_rows(reqs: Iterable[Request], label: str, since_day: str | None = None) -> list[dict]:
    """One row per (day, model, entrypoint) of this account's kept requests.

    Fast-session requests are counted in `fast_session_requests` and binned in
    `fast_output_hist` and `fast_ttfb_hist`, apart from the rest (`n`), so a row still
    says which were which; the publisher pools the two. `since_day` drops requests that
    ended before it, so a run over recent transcripts never writes a partial row for a day
    the history already holds.
    """
    reqs = list(reqs)
    fast = fast_sessions(reqs)
    rows: dict[tuple, dict] = {}
    for r in reqs:
        if not kept(r) or (since_day is not None and r.day < since_day):
            continue
        row = rows.setdefault((r.day, r.model, r.entrypoint), {
            "day": r.day, "model": r.model, "account": label, "entrypoint": r.entrypoint,
            "n": 0, "fast_session_requests": 0, "output_hist": {}, "ttfb_hist": {},
            "fast_output_hist": {}, "fast_ttfb_hist": {}})
        if r.id in fast:
            row["fast_session_requests"] += 1
            out_hist, ttfb_hist = row["fast_output_hist"], row["fast_ttfb_hist"]
        else:
            row["n"] += 1
            out_hist, ttfb_hist = row["output_hist"], row["ttfb_hist"]
        b = _bin(r.output_rate)
        out_hist[b] = out_hist.get(b, 0) + 1
        if r.ttfb > 0:
            b = _bin(r.ttfb)
            ttfb_hist[b] = ttfb_hist.get(b, 0) + 1
    return sorted(rows.values(), key=_row_key)


def _row_key(row: dict) -> tuple:
    return (row["day"], row["model"], row["account"], row["entrypoint"])


def merge(stored: list[dict], fresh: list[dict], since_day: str) -> list[dict]:
    """Stored rows before `since_day` kept as they are; from `since_day` on, fresh rows
    replace them. A fresh scan with nothing for a day it covers leaves that day's stored
    rows alone, so a lost transcript never erases a day already recorded."""
    fresh_days = {r["day"] for r in fresh}
    keep = [r for r in stored if r["day"] < since_day or r["day"] not in fresh_days]
    return sorted(keep + [r for r in fresh if r["day"] >= since_day], key=_row_key)


def update(stored: dict | None, fresh: list[dict], since_day: str, now: datetime) -> dict:
    rows = merge((stored or {}).get("rows") or [], fresh, since_day)
    return {"schema": SCHEMA, "generated_at": now.isoformat(), "method": METHOD_ID, "rows": rows}


def recompute_from(stored: dict | None, now: datetime) -> tuple[str | None, datetime | None]:
    """(first day to recompute, transcript mtime cutoff); (None, None) means everything,
    which is what a history with no rows yet gets."""
    if not (stored or {}).get("rows"):
        return None, None
    day = (now - timedelta(days=RECOMPUTE_DAYS)).date()
    return day.isoformat(), datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)


#: Bumped when the method changes what a row means; recorded in every history file.
METHOD_ID = "trigger-to-last-block/v1"


# ---- publishing -----------------------------------------------------------------------


def _quantile(hist: dict[str, int], q: float) -> float:
    """The q-quantile of a log-binned histogram, interpolated geometrically inside its bin."""
    n = sum(hist.values())
    rank = q * n
    seen = 0
    for b in sorted(hist, key=int):
        c = hist[b]
        if seen + c >= rank:
            return BIN_STEP ** (int(b) + (rank - seen) / c)
        seen += c
    return BIN_STEP ** (int(max(hist, key=int)) + 1)


def _pool(rows: list[dict], *fields: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        for field in fields:
            for b, c in (r.get(field) or {}).items():
                out[b] = out.get(b, 0) + c
    return out


def _stat(hist: dict[str, int], digits: int) -> dict:
    return {"median": round(_quantile(hist, 0.5), digits), "q1": round(_quantile(hist, 0.25), digits),
            "q3": round(_quantile(hist, 0.75), digits)}


def _days(rows: list[dict]) -> list[dict]:
    """Per-day figures over every kept request, fast-session requests included.

    A row bins its fast-session requests apart from the rest, so both histograms are
    pooled here. `n` counts the binned requests: a row from before schema 2 counted its
    fast requests without binning them, and they cannot be read back.
    """
    out = []
    for day, rs in sorted(_group(rows, lambda r: r["day"], lambda r: r).items()):
        speed = _pool(rs, "output_hist", "fast_output_hist")
        n = sum(speed.values())
        if n < MIN_REQUESTS:
            continue
        ttfb = _pool(rs, "ttfb_hist", "fast_ttfb_hist")
        out.append({"day": day, "n": n, "fast_session_requests": sum(_fast_count(r) for r in rs),
                    "output_tokens_per_s": _stat(speed, 1),
                    "time_to_first_block_s": _stat(ttfb, 2) if ttfb else None})
    return out


def _fast_count(row: dict) -> int:
    """A row's fast-session requests; rows written before schema 2 call them `fast_excluded`."""
    return row.get("fast_session_requests", row.get("fast_excluded", 0))


METHOD = (
    "Each request is timed from Claude Code's transcripts: from the last user line (a prompt "
    "or a tool result) written before the response, to the response's last content block. "
    "Output speed is the response's output tokens over that time, so it includes waiting in "
    "the queue and reading the input; it is a speed as a Claude Code user sees it, not the "
    f"model's raw decoding speed. Only responses of at least {MIN_OUTPUT} output tokens taking "
    f"between {MIN_SECONDS:g} and {MAX_SECONDS:g} seconds are counted, and a day is shown only "
    f"with at least {MIN_REQUESTS} of them. Figures are the median and interquartile range "
    "across requests, per UTC day, pooled from speeds binned 2% apart. Input speed is "
    "shown as the median time from that user line to the end of the response's first block, not "
    "as a rate. A robust fit of each request's duration against its uncached input and output "
    "tokens, per model and day, gave an input rate whose 80% interval was under 1.5 times end "
    "to end on only 6 of 87 model-days with 300 or more requests (13 had no upper bound, the "
    "median was 2.7 times), and the six tight ones disagreed with each other several-fold. "
    "Claude Code sends little uncached input per request (a median of about 1,200 tokens), so "
    "the time spent reading it is lost in queueing and output time."
)

CAVEATS = [
    ("Some Opus sessions run at about 2.5 times the model's usual speed, with nothing in between. "
     f"A request in a session whose running median speed is {FAST_FACTOR:g} times the model's "
     "median or more is a fast-session request. They are real measured speeds and are included "
     "in every figure, so a day with many of them shows as a jump in the line; "
     "fast_session_requests counts them. Why these sessions run faster is not known."),
    ("a1 is one machine's transcripts; a2 to a4 are another machine's. Compare accounts and "
     "entrypoints (cli is interactive Claude Code, sdk-cli is claude -p, sdk-ts and sdk-py are "
     "the Agent SDK) with like, using by_account_entrypoint."),
    ("Time to first block includes writing the whole first block, which may be a long thinking "
     "or tool block, so it is not a time to first token."),
    ("Older days come from a stored history: Claude Code deletes transcripts after 30 days, so "
     "a day is computed while its transcripts exist and then kept as it was."),
]


def speed_block(*histories: dict | None) -> dict:
    """The public `speed` block from any number of history files' rows."""
    rows = [r for h in histories for r in ((h or {}).get("rows") or [])]
    models: dict[str, dict] = {}
    for model, rs in sorted(_group(rows, lambda r: r["model"], lambda r: r).items()):
        daily = _days(rs)
        if not daily:
            continue
        splits = {}
        for name, key in (("by_account", lambda r: r["account"]), ("by_entrypoint", lambda r: r["entrypoint"]),
                          ("by_account_entrypoint", lambda r: f"{r['account']}:{r['entrypoint']}")):
            part = {k: _days(v) for k, v in sorted(_group(rs, key, lambda r: r).items())}
            splits[name] = {k: v for k, v in part.items() if v}
        models[model] = {"daily": daily, **splits}
    through = {}
    for label, rs in sorted(_group(rows, lambda r: r["account"], lambda r: r["day"]).items()):
        through[label] = {"first_day": min(rs), "last_day": max(rs)}
    return {"method": METHOD, "caveats": CAVEATS, "min_requests": MIN_REQUESTS,
            "input_measure": "time_to_first_block_s", "models": models, "accounts": through}


# ---- command line ---------------------------------------------------------------------


def load_history(path: Path | None) -> dict | None:
    if path is None or not Path(path).exists():
        return None
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"warning: speed history {path} unreadable: {e}", file=sys.stderr)
        return None
    return body if isinstance(body, dict) else None


def scan_accounts(accounts: Iterable[tuple[str, list[Path]]], since_day: str | None) -> list[dict]:
    """Daily rows for each (label, transcript paths), deduplicating message ids across them."""
    seen: set[str] = set()
    rows = []
    for label, paths in accounts:
        rows += daily_rows(requests_from_files(paths, seen), label, since_day)
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .gs_passive import masterrig_account, transcript_files
    from .publish import ACCOUNT_LABELS
    ap = argparse.ArgumentParser(description="Append masterrig's daily model speed rows to its history file")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--masterrig", action="store_true", help="read masterrig's own ~/.claude/projects")
    src.add_argument("--lines", type=Path, help="a text-free extract of transcript lines, each with `file`")
    ap.add_argument("--label", default=dict(ACCOUNT_LABELS)["masterrig"], help="account label for --lines")
    ap.add_argument("--home", type=Path, default=Path.home())
    ap.add_argument("--history", type=Path, required=True)
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    stored = load_history(a.history)
    since_day, mtime_since = recompute_from(stored, now)
    if a.lines:
        rows = daily_rows(requests_from_extract(a.lines), a.label, since_day)
    else:
        paths, _ = transcript_files(masterrig_account(a.home), mtime_since)
        rows = scan_accounts([(dict(ACCOUNT_LABELS)["masterrig"], paths)], since_day)
    body = update(stored, rows, since_day or date.min.isoformat(), now)
    a.history.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.history.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(a.history)
    print(f"wrote {a.history}: {len(rows)} fresh rows, {len(body['rows'])} in all", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
