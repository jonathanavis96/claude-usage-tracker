"""Put the fast-session tokens back into the committed masterrig stretches, once.

PR #88 took the tokens of Opus sessions running at about 2.5x the usual speed out of every
passive stretch, as Opus fast mode billed to usage credits and never metered. That was
wrong: Jonathan never used fast mode, the account is not eligible for usage credits, and
#88's own table shows the meter counted those tokens, at least in part
(docs/findings-2026-09-23-fast-mode-stretches.md, correction). tracker/join.py now counts
them like any other; this tool undoes #88's one-off subtraction on
history/masterrig-passive.json, which only masterrig's transcripts could rebuild. It reads
the same inputs #88's correction did, on gs:

- a text-free extract of masterrig's transcript lines (timestamps, message ids, model and
  usage, no text; each line's `file` names its transcript), never committed;
- masterrig's two meter logs, copied into a scratch home (`.moonlighter/usage_log.jsonl`
  and `.paperclip/ops/mis-usage-ceiling-systemd.log`), never committed.

1. The committed file is rebuilt from its own records by tracker/gs_passive.py `summarise`
   and must come back identical: the records, prices and probe rows here are the ones it
   was written from.
2. The fast-session turns (tracker/speed.py `fast_sessions`, over the whole extract) are
   joined against the meter as tracker/join.py `build_stretches` joins, and each stretch's
   rebuilt fast-session tokens and turns must equal the `fast_mode_tokens` and
   `fast_mode_turns` #88 recorded as subtracted: that is the proof the amounts added back
   are the amounts taken out.
3. Each stretch gets those tokens back in `tokens` and those turns back in `turns`, is
   revalued at the price table, and keeps them as `fast_session_tokens` and
   `fast_session_turns`, for diagnosis only.
4. The stretches go through the same `summarise`, which re-runs the capture check and
   every figure read off it.

#88's `fast_mode_correction` record is replaced by `fast_session_restore`, whose `undid` keeps
when #88 applied it and how many stretches and turns it changed. A file
that carries `fast_session_restore` is left alone, so a second run changes nothing.

    python3 tools/fast_session_restore.py --extract ~/wf-data/masterrig-timing.jsonl.gz \\
        --meter-home <scratch>/mr
"""
from __future__ import annotations

import argparse
import copy
import gzip
import json
import math
import sys
from collections.abc import Iterator
from datetime import datetime
from itertools import groupby
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import speed
from tracker.gs_passive import load_samples, masterrig_account, summarise
from tracker.join import CLASSES, Stretch, build_stretches, bundle_meter_usd
from tracker.turns import Turn, normalize_model, turns_in

ACCOUNT = "masterrig"
STRETCHES = Path("history/masterrig-passive.json")


def _extract_files(path: Path) -> Iterator[tuple[str, list[dict]]]:
    """(transcript, its lines) from the extract, one transcript in memory at a time.

    The extract is written a transcript at a time; a transcript seen twice would mean it
    is not, and the per-file trigger tracking of tracker/speed.py would be wrong, so that
    is an error rather than a silent merge.
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    done: set[str] = set()
    with opener(path, "rt", encoding="utf-8") as fh:
        rows = (json.loads(line) for line in fh if line.strip())
        for name, lines in groupby(rows, key=lambda d: d.get("file") or ""):
            if name in done:
                raise ValueError(f"{path}: lines of {name} are not contiguous")
            done.add(name)
            yield name, list(lines)


def fast_turns(extract: Path) -> tuple[list[Turn], dict]:
    """The turns of every fast-session request in the extract, and counts for the record.

    Read in the extract's own order, a transcript at a time. Order only decides which copy
    of a message id is kept when a resumed session copied it into a second transcript, and
    the copies carry the same usage.
    """
    reqs: list[speed.Request] = []
    turns: list[Turn] = []
    seen_req: set[str] = set()
    seen_turn: set[str] = set()
    files = 0
    for name, lines in _extract_files(extract):
        files += 1
        for r in speed.requests_in(lines, name):
            if r.id not in seen_req:
                seen_req.add(r.id)
                reqs.append(r)
        turns.extend(turns_in(lines, seen_turn))
    fast = speed.fast_sessions(reqs, every=True)
    out = [t for t in turns if t.id in fast]
    return out, {"files": files, "requests": len(reqs), "fast_requests": len(fast)}


def _priced(tokens: dict) -> dict:
    return {m: tok for m, tok in tokens.items() if normalize_model(m) is not None}


def _usd(tokens: dict, prices: dict) -> float:
    total = 0.0
    for m, tok in tokens.items():
        usd = bundle_meter_usd(m, tok, prices)
        if usd is None:
            raise ValueError(f"no price for {m}")
        total += usd
    return total


def to_stretch(rec: dict, prices: dict) -> Stretch:
    """A committed stretch record back as the Stretch it was written from."""
    tokens = _priced(rec["tokens"])
    usd = _usd(tokens, prices)
    return Stretch(datetime.fromisoformat(rec["start"]), datetime.fromisoformat(rec["end"]),
                   delta_pct=rec["delta_pct"], windows=rec["windows"], usd=usd,
                   tokens=copy.deepcopy(tokens), unpriced_tokens=rec["unpriced_tokens"],
                   unpriced=copy.deepcopy(rec["unpriced"]), turns=rec["turns"],
                   reset_verified=rec["reset_verified"])


def add_back(s: Stretch, taken: dict, turns: int, prices: dict) -> None:
    """Put `taken` (model -> class -> count) and `turns` back into `s`, record them as its
    fast-session tokens, and revalue it. A model goes where tracker/join.py would put it:
    `tokens` when it has a price, `unpriced` when not."""
    for model, by_class in taken.items():
        key = normalize_model(model)
        priced = key is not None and bundle_meter_usd(model, by_class, prices) is not None
        bucket = s.tokens if priced else s.unpriced
        have = bucket.setdefault(key if priced else model, {c: 0 for c in CLASSES})
        for c, n in by_class.items():
            have[c] = have.get(c, 0) + n
        if not priced:
            s.unpriced_tokens += sum(by_class.get(c, 0) for c in CLASSES)
    s.turns += turns
    s.fast_session_tokens = copy.deepcopy(taken)
    s.fast_session_turns = turns
    s.usd = _usd(s.tokens, prices)


def _summarise(body: dict, stretches: list[Stretch], probe_rows: list[dict], prices: dict) -> dict:
    a = body["accounts"][ACCOUNT]
    meta = {"meter": a["meter"], "transcripts": a["transcripts"]}
    sources = {ACCOUNT: {s.start: rec["reset_source"] for s, rec in zip(stretches, a["stretches"])}}
    until = datetime.fromisoformat(body["until"]) if body.get("until") else None
    return summarise({ACCOUNT: stretches}, sources, {ACCOUNT: meta}, {ACCOUNT: a["weekly_by_window"]},
                     probe_rows, prices, datetime.fromisoformat(body["generated_at"]), until)


def same(a, b) -> bool:
    """Equal, but for the last rounded digit of a figure: a stretch's `usd` is summed per turn
    when built and per model here, and the two can round a 4-decimal figure apart."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        return (isinstance(a, (int, float)) and isinstance(b, (int, float))
                and not isinstance(a, bool) and not isinstance(b, bool)
                and math.isclose(a, b, rel_tol=1e-6, abs_tol=1.01e-4))
    return a == b


def without_fast_fields(body: dict) -> dict:
    """`body` with no per-stretch fast-session or fast-mode fields and neither top-level record."""
    body = copy.deepcopy(body)
    body.pop("fast_mode_correction", None)
    body.pop("fast_session_restore", None)
    for rec in body["accounts"][ACCOUNT]["stretches"]:
        for k in ("fast_mode_tokens", "fast_mode_turns", "fast_session_tokens", "fast_session_turns"):
            rec.pop(k, None)
    return body


def _undid(correction: dict) -> dict:
    """What #88's record said it did, in this file's terms."""
    return {"pr": 88, "applied_at": correction.get("applied_at"),
            "stretches_changed": correction.get("stretches_changed"),
            "turns": correction.get("fast_mode_turns")}


def restore(body: dict, fast: list[Turn], samples: list, prices: dict, probe_rows: list[dict],
            note: dict) -> dict | None:
    """The restored stretch file, or None when `body` is already restored."""
    if body.get("fast_session_restore"):
        return None
    if not body.get("fast_mode_correction"):
        raise ValueError("the file carries no fast_mode_correction; there is nothing to restore")
    recs = body["accounts"][ACCOUNT]["stretches"]
    generated = datetime.fromisoformat(body["generated_at"])
    probe_rows = [r for r in probe_rows if datetime.fromisoformat(r["ts"]) <= generated]
    stretches = [to_stretch(rec, prices) for rec in recs]
    again = _summarise(body, stretches, probe_rows, prices)
    if not same(json.loads(json.dumps(without_fast_fields(again))), without_fast_fields(body)):
        raise ValueError("the committed file does not rebuild from its own records; not restoring it")
    ids = {t.id for t in fast}
    rebuilt = {s.start: s for s in build_stretches(samples, fast, prices, fast=ids)}
    for s, rec in zip(stretches, recs):
        taken, turns = rec.get("fast_mode_tokens") or {}, rec.get("fast_mode_turns", 0)
        f = rebuilt.get(s.start)
        if f is None or f.end != s.end:
            if taken or turns:
                raise ValueError(f"stretch {s.start.isoformat()} does not rebuild from the meter logs")
            f = Stretch(s.start, s.end)
        if (f.fast_session_tokens, f.fast_session_turns) != (taken, turns):
            raise ValueError(f"stretch {s.start.isoformat()}: the extract's fast-session tokens are not "
                             "the ones #88 recorded as taken out")
        add_back(s, taken, turns, prices)
    out = _summarise(body, stretches, probe_rows, prices)
    out["fast_session_restore"] = {
        **note, "stretches_changed": sum(1 for s in stretches if s.fast_session_turns),
        "fast_session_turns": sum(s.fast_session_turns for s in stretches),
        "rule": "tracker/speed.py fast_sessions over the extract; tokens joined as tracker/join.py build_stretches",
        "tool": "tools/fast_session_restore.py", "undid": _undid(body["fast_mode_correction"])}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--extract", type=Path, required=True, help="text-free extract of masterrig's transcript lines")
    ap.add_argument("--meter-home", type=Path, required=True,
                    help="a directory holding copies of masterrig's two meter logs at their home-relative paths")
    ap.add_argument("--stretches", type=Path, default=STRETCHES)
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--probes", type=Path, default=Path("history/probes.jsonl"))
    a = ap.parse_args(argv)
    body = json.loads(a.stretches.read_text(encoding="utf-8"))
    if body.get("fast_session_restore"):
        print(f"{a.stretches} is already restored ({body['fast_session_restore']['applied_at']}); nothing to do")
        return 0
    prices = {k: v for k, v in json.loads(a.prices.read_text()).items() if not k.startswith("_")}
    rows = [json.loads(line) for line in a.probes.read_text(encoding="utf-8").splitlines() if line.strip()]
    turns, counts = fast_turns(a.extract)
    samples = load_samples(masterrig_account(a.meter_home), datetime.fromisoformat(body["generated_at"]))
    note = {"applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "extract": a.extract.name, **counts}
    out = restore(body, turns, samples, prices, rows, note)
    assert out is not None  # already-restored files returned above
    tmp = a.stretches.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    tmp.replace(a.stretches)
    c = out["fast_session_restore"]
    print(f"wrote {a.stretches}: {c['stretches_changed']} stretches got back {c['fast_session_turns']} "
          f"fast-session turns ({c['fast_requests']} fast requests of {c['requests']} timed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
