"""Take Opus fast-mode tokens out of the committed masterrig stretches, once (issue #106).

Opus fast mode is billed "via usage credits only and not included in the subscription rate
limits" (code.claude.com/docs/en/fast-mode), so its tokens are in the transcripts and never on
the meter. From this change tracker/join.py keeps them out of every stretch it builds, and
bin/passive.sh does that on masterrig from its own transcripts. The stretches already committed
in history/masterrig-passive.json were built before it, and masterrig's transcripts are only on
masterrig, so this tool corrects that file on gs, once, from:

- a text-free extract of masterrig's transcript lines (timestamps, message ids, model and
  usage, no text; each line's `file` names its transcript), never committed;
- masterrig's two meter logs, copied into a scratch home (`.moonlighter/usage_log.jsonl`
  and `.paperclip/ops/mis-usage-ceiling-systemd.log`), never committed.

Which requests ran fast is tracker/speed.py's `fast_mode`, over the whole extract. Their turns
are joined against the meter exactly as tracker/join.py `build_stretches` joins, fast set and
all, so a fast turn lands in the stretch, and the sample pair, the committed build put it in,
and a turn in a pair that straddled a gap or a reset counts for nothing, as there. The
stretches this rebuild closes must be the committed ones, start and end, or nothing is written.

Each committed stretch then loses its fast-mode tokens from `tokens` and its fast turns from
`turns`, gains `fast_mode_tokens` and `fast_mode_turns`, and is revalued at the price table.
Before anything is corrected, the committed file is rebuilt from its own records by
tracker/gs_passive.py `summarise` and must come back identical: that is the proof that the
records, prices and probe rows here are the ones it was written from. The corrected stretches
then go through the same `summarise`, which re-runs the capture check and every figure read
off it. A file that already carries `fast_mode_correction` is left alone, so a second run
changes nothing.

    python3 tools/fast_mode_correction.py --extract ~/wf-data/masterrig-timing.jsonl.gz \\
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
    """The turns of every fast-mode request in the extract, and counts for the record.

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
    fast = speed.fast_mode(reqs, every=True)
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
                   reset_verified=rec["reset_verified"],
                   fast_mode_tokens=copy.deepcopy(rec.get("fast_mode_tokens") or {}),
                   fast_mode_turns=rec.get("fast_mode_turns", 0))


def subtract(s: Stretch, fast: Stretch, prices: dict) -> None:
    """Move `fast`'s fast-mode tokens and turns out of `s`, and revalue `s`."""
    for model, by_class in fast.fast_mode_tokens.items():
        bucket = s.tokens if model in s.tokens else s.unpriced
        have = bucket.get(model)
        if have is None:
            raise ValueError(f"stretch {s.start.isoformat()}: fast-mode {model} tokens but none recorded")
        for c, n in by_class.items():
            if have.get(c, 0) < n:
                raise ValueError(f"stretch {s.start.isoformat()}: {model} {c} {have.get(c, 0)} < fast {n}")
            have[c] -= n
        if all(v == 0 for v in have.values()):
            del bucket[model]
        if bucket is s.unpriced:
            s.unpriced_tokens -= sum(by_class.get(c, 0) for c in CLASSES)
    s.turns -= fast.fast_mode_turns
    s.fast_mode_tokens = copy.deepcopy(fast.fast_mode_tokens)
    s.fast_mode_turns = fast.fast_mode_turns
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


def _without_fast_fields(body: dict) -> dict:
    body = copy.deepcopy(body)
    for rec in body["accounts"][ACCOUNT]["stretches"]:
        rec.pop("fast_mode_tokens", None)
        rec.pop("fast_mode_turns", None)
    return body


def correct(body: dict, fast: list[Turn], samples: list, prices: dict, probe_rows: list[dict],
            note: dict) -> dict | None:
    """The corrected stretch file, or None when `body` is already corrected."""
    if body.get("fast_mode_correction"):
        return None
    recs = body["accounts"][ACCOUNT]["stretches"]
    generated = datetime.fromisoformat(body["generated_at"])
    probe_rows = [r for r in probe_rows if datetime.fromisoformat(r["ts"]) <= generated]
    stretches = [to_stretch(rec, prices) for rec in recs]
    again = _without_fast_fields(_summarise(body, stretches, probe_rows, prices))
    if not same(json.loads(json.dumps(again)), body):
        raise ValueError("the committed file does not rebuild from its own records; not correcting it")
    ids = {t.id for t in fast}
    rebuilt = {s.start: s for s in build_stretches(samples, fast, prices, fast=ids)}
    for s in stretches:
        f = rebuilt.get(s.start)
        if f is None or f.end != s.end:
            raise ValueError(f"stretch {s.start.isoformat()} does not rebuild from the meter logs")
        subtract(s, f, prices)
    out = _summarise(body, stretches, probe_rows, prices)
    changed = sum(1 for s in stretches if s.fast_mode_turns)
    out["fast_mode_correction"] = {
        **note, "stretches_changed": changed,
        "fast_mode_turns": sum(s.fast_mode_turns for s in stretches),
        "rule": "tracker/speed.py fast_mode over the extract; tokens joined as tracker/join.py build_stretches",
        "tool": "tools/fast_mode_correction.py"}
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
    if body.get("fast_mode_correction"):
        print(f"{a.stretches} is already corrected ({body['fast_mode_correction']['applied_at']}); nothing to do")
        return 0
    prices = {k: v for k, v in json.loads(a.prices.read_text()).items() if not k.startswith("_")}
    rows = [json.loads(line) for line in a.probes.read_text(encoding="utf-8").splitlines() if line.strip()]
    turns, counts = fast_turns(a.extract)
    samples = load_samples(masterrig_account(a.meter_home), datetime.fromisoformat(body["generated_at"]))
    note = {"applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "extract": a.extract.name, **counts}
    out = correct(body, turns, samples, prices, rows, note)
    assert out is not None  # already-corrected files returned above
    tmp = a.stretches.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    tmp.replace(a.stretches)
    c = out["fast_mode_correction"]
    print(f"wrote {a.stretches}: {c['stretches_changed']} stretches lost {c['fast_mode_turns']} fast-mode turns "
          f"({c['fast_requests']} fast requests of {c['requests']} timed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
