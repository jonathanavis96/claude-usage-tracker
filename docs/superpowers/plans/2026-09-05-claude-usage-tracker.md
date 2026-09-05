# Claude Usage Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily-updated public page at alldonesites.com/claude-usage-tracker that reports, from a tick-based probe on `ssh gs`, how many tokens a Claude Max 20x window buys and whether Anthropic has changed it.

**Architecture:** A private Python stdlib collector (`~/code/claude-usage-tracker`) has two inputs: a probe that loops a fixed small prompt on an idle Greenscape account until the 5-hour utilization ticks twice, and a passive join of Jonathan's utilization logs with his transcripts. A daily job on `gs` merges both into one JSON, commits it into the alldonesites repo, and Cloudflare Pages redeploys. The page is a React route that does arithmetic on that JSON.

**Tech Stack:** Python 3.10+ stdlib only, `unittest`; `claude -p --output-format json`; bash + cron on `gs`; React 18 + Vite + TypeScript in alldonesites with inline SVG for the chart.

**Spec:** `docs/superpowers/specs/2026-09-05-claude-usage-tracker-design.md`

## Global Constraints

- Collector: Python 3 standard library only. No pip dependencies. Tests use `unittest`, run with `python3 -m unittest discover -s tests -v`.
- Never read a whole transcript into memory; stream line by line. Dedup usage by `message.id`.
- Tests never touch the network or spend usage. Every network or CLI call goes through an injectable function.
- Usage endpoint: `GET https://api.anthropic.com/api/oauth/usage`, header `Authorization: Bearer <claudeAiOauth.accessToken>` from `<config_dir>/.credentials.json`, header `anthropic-beta: oauth-2025-04-20`. Response keys: `five_hour`, `seven_day`, `seven_day_sonnet`, each `{utilization, resets_at}`.
- Accounts on `gs`: Dave = `CLAUDE_CONFIG_DIR=$HOME/.claude-dave`, Jono Work = `CLAUDE_CONFIG_DIR=$HOME/.claude-javiswork`. Never `/logout` there. All three accounts are Max 20x.
- Passive sources: `~/.moonlighter/usage_log.jsonl` (30-minute samples since 2026-06-13, with `resets_at`), `~/.paperclip/ops/mis-usage-ceiling-systemd.log` (lines like `2026-08-18T17:07:19+02:00 5-hour 25% / 7-day 82% (...)`, no reset time), transcripts under `~/.claude/projects/**/*.jsonl`.
- Model ids: `claude-sonnet-5`, `claude-opus-5`, `claude-fable-5-1`. Effort levels: `low, medium, high, xhigh, max`.
- Change detection: rolling 3-day probe median vs preceding 7-day median, threshold 5%, must persist 2 consecutive days.
- alldonesites is a PUBLIC repo (`jonathanavis96/all-done-sites-platform`). Commits there carry no AI trailers and no handoff or spec files. Daily data commits use the fixed message `data: refresh claude usage`.
- Public JSON path: `website/public/data/claude-usage.json` in alldonesites. Shape is the one in the spec.
- Commits in this repo: normal prose messages. This repo is private.
- Long test suites are never run on masterrig; the collector suite is small and fast, run only it.

---

## File structure

```
claude-usage-tracker/
  tracker/__init__.py
  tracker/usage_api.py     read utilization from the OAuth endpoint (injectable fetch)
  tracker/samples.py       parse the two passive sample logs into Sample rows
  tracker/turns.py         stream transcripts into deduplicated Turn rows
  tracker/join.py          intervals, pooling, attribution, daily rates
  tracker/cli_run.py       run `claude -p` and parse its JSON usage (injectable runner)
  tracker/probe.py         tick loop, idle guard, account fallback, probes.jsonl writer
  tracker/detect.py        change detection over daily per-model series
  tracker/publish.py       assemble the public JSON
  tracker/calibrate.py     effort matrix run (one-off)
  data/effort_matrix.json  output of calibrate (committed)
  data/prices.json         static API prices per model per class
  bin/probe.sh             cron wrapper on gs
  bin/passive.sh           cron wrapper on masterrig: join + push history/passive.json
  bin/daily.sh             cron wrapper on gs: merge, detect, publish, commit to alldonesites
  tests/test_*.py          one per module, fixtures inline
  docs/spike-2026-09.md    spike findings
alldonesites/website/
  src/pages/ClaudeUsageTracker.tsx      the page
  src/lib/claudeUsage.ts                JSON type + arithmetic (pure, testable)
  src/styles/claude-usage.css           page styles built on home.css tokens
  src/App.tsx                            route
  src/entry-server.tsx                   prerender route
  public/data/claude-usage.json         data (written by daily.sh)
```

---

### Task 1: Repo scaffold and usage endpoint reader

**Files:**
- Create: `tracker/__init__.py`, `tracker/usage_api.py`, `tests/__init__.py`, `tests/test_usage_api.py`

**Interfaces:**
- Produces: `Utilization(ts: datetime, five_hour: float | None, seven_day: float | None, five_hour_resets_at: str | None)` dataclass; `read_usage(config_dir: Path, fetch=None, now=None) -> Utilization`; `parse_usage(body: dict, now: datetime) -> Utilization`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_api.py
import json, unittest, tempfile
from datetime import datetime, timezone
from pathlib import Path
from tracker.usage_api import parse_usage, read_usage

BODY = {"five_hour": {"utilization": 24.0, "resets_at": "2026-09-06T02:00:00.3Z"},
        "seven_day": {"utilization": 82.0, "resets_at": "2026-09-11T03:59:59Z"},
        "seven_day_sonnet": {"utilization": None, "resets_at": None}}

class ParseTests(unittest.TestCase):
    def test_parse(self):
        now = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)
        u = parse_usage(BODY, now)
        self.assertEqual(u.five_hour, 24.0)
        self.assertEqual(u.seven_day, 82.0)
        self.assertEqual(u.five_hour_resets_at, "2026-09-06T02:00:00.3Z")
        self.assertEqual(u.ts, now)

    def test_read_uses_token_from_config_dir(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok123"}}))
            seen = {}
            def fetch(url, headers):
                seen["url"], seen["headers"] = url, headers
                return BODY
            u = read_usage(Path(d), fetch=fetch, now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
            self.assertEqual(seen["url"], "https://api.anthropic.com/api/oauth/usage")
            self.assertEqual(seen["headers"]["Authorization"], "Bearer tok123")
            self.assertEqual(seen["headers"]["anthropic-beta"], "oauth-2025-04-20")
            self.assertEqual(u.five_hour, 24.0)

    def test_missing_token_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                read_usage(Path(d), fetch=lambda u, h: BODY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/claude-usage-tracker && python3 -m unittest tests.test_usage_api -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tracker'`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/__init__.py
"""Claude usage tracker collector. Stdlib only."""
```

```python
# tracker/usage_api.py
"""Read subscription utilization from the Claude OAuth usage endpoint."""
from __future__ import annotations
import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


@dataclass(frozen=True)
class Utilization:
    ts: datetime
    five_hour: float | None
    seven_day: float | None
    five_hour_resets_at: str | None


def _bucket(body: dict, key: str) -> tuple[float | None, str | None]:
    b = body.get(key) or {}
    u = b.get("utilization")
    return (float(u) if u is not None else None, b.get("resets_at"))


def parse_usage(body: dict, now: datetime) -> Utilization:
    fh, fh_reset = _bucket(body, "five_hour")
    sd, _ = _bucket(body, "seven_day")
    return Utilization(ts=now, five_hour=fh, seven_day=sd, five_hour_resets_at=fh_reset)


def _default_fetch(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def read_usage(config_dir: Path, fetch: Callable[[str, dict], dict] | None = None,
               now: Callable[[], datetime] | None = None) -> Utilization:
    creds = Path(config_dir) / ".credentials.json"
    if not creds.exists():
        raise FileNotFoundError(f"no credentials at {creds}")
    token = json.loads(creds.read_text())["claudeAiOauth"]["accessToken"]
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
    body = (fetch or _default_fetch)(USAGE_URL, headers)
    return parse_usage(body, (now or (lambda: datetime.now(timezone.utc)))())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_usage_api -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tracker tests
git commit -m "Add usage endpoint reader with injectable fetch"
```

---

### Task 2: Passive sample parsers

**Files:**
- Create: `tracker/samples.py`, `tests/test_samples.py`

**Interfaces:**
- Produces: `Sample(ts: datetime, five_hour: float, seven_day: float | None, resets_at: str | None, source: str)`; `parse_moonlighter(lines) -> list[Sample]`; `parse_ceiling_log(lines) -> list[Sample]`; `merge_samples(*lists) -> list[Sample]` sorted by ts, deduplicated to the minute, denser source wins on ties.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_samples.py
import unittest
from datetime import datetime, timezone, timedelta
from tracker.samples import parse_moonlighter, parse_ceiling_log, merge_samples

ML = ['{"ts": "2026-06-13T00:51:22.743614+02:00", "seven_day": {"utilization": 23.0, "resets_at": "2026-06-19T04:00:00Z"}, "seven_day_sonnet": {"utilization": 5.0, "resets_at": null}, "five_hour": {"utilization": 40.0, "resets_at": "2026-06-13T01:30:00Z"}}',
      '{"ts": "2026-06-13T01:30:02+02:00", "seven_day": {"utilization": 25.0, "resets_at": null}, "seven_day_sonnet": {"utilization": null, "resets_at": null}, "five_hour": {"utilization": null, "resets_at": null}}',
      'not json']
CL = ['2026-08-18T17:05:10+02:00 usage ceiling armed for systemd (x): warn 60% / hard 80%',
      '2026-08-18T17:07:19+02:00 5-hour 25% / 7-day 82% (warn 60% / hard 80% / week 90%)',
      '2026-08-18T17:08:19+02:00 READ FAILURE (1 consecutive) -- usage endpoint unreadable']

class SampleTests(unittest.TestCase):
    def test_moonlighter_skips_null_and_bad_lines(self):
        s = parse_moonlighter(ML)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0].five_hour, 40.0)
        self.assertEqual(s[0].resets_at, "2026-06-13T01:30:00Z")
        self.assertEqual(s[0].ts.tzinfo.utcoffset(None), timedelta(hours=2))
        self.assertEqual(s[0].source, "moonlighter")

    def test_ceiling_log_only_reading_lines(self):
        s = parse_ceiling_log(CL)
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0].five_hour, s[0].seven_day, s[0].resets_at, s[0].source), (25.0, 82.0, None, "ceiling"))

    def test_merge_sorts_and_dedups_to_minute(self):
        a = parse_moonlighter(['{"ts": "2026-08-18T17:07:40+02:00", "five_hour": {"utilization": 26.0, "resets_at": null}, "seven_day": {"utilization": 1.0}}'])
        b = parse_ceiling_log(CL)
        m = merge_samples(a, b)
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].source, "ceiling")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_samples -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tracker.samples'`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/samples.py
"""Parse the two passive utilization logs on masterrig into Sample rows."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

_CEIL = re.compile(r"^(\S+) 5-hour (\d+)% / 7-day (\d+)%")
_PRIORITY = {"ceiling": 0, "moonlighter": 1}  # lower wins


@dataclass(frozen=True)
class Sample:
    ts: datetime
    five_hour: float
    seven_day: float | None
    resets_at: str | None
    source: str


def parse_moonlighter(lines: Iterable[str]) -> list[Sample]:
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        fh = (d.get("five_hour") or {})
        if fh.get("utilization") is None or not d.get("ts"):
            continue
        sd = (d.get("seven_day") or {}).get("utilization")
        out.append(Sample(datetime.fromisoformat(d["ts"]), float(fh["utilization"]),
                          float(sd) if sd is not None else None, fh.get("resets_at"), "moonlighter"))
    return out


def parse_ceiling_log(lines: Iterable[str]) -> list[Sample]:
    out = []
    for line in lines:
        m = _CEIL.match(line)
        if not m:
            continue
        out.append(Sample(datetime.fromisoformat(m.group(1)), float(m.group(2)), float(m.group(3)), None, "ceiling"))
    return out


def merge_samples(*lists: list[Sample]) -> list[Sample]:
    by_minute: dict[datetime, Sample] = {}
    for s in sorted((s for l in lists for s in l), key=lambda s: s.ts):
        key = s.ts.replace(second=0, microsecond=0)
        cur = by_minute.get(key)
        if cur is None or _PRIORITY[s.source] < _PRIORITY[cur.source]:
            by_minute[key] = s
    return [by_minute[k] for k in sorted(by_minute)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_samples -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tracker/samples.py tests/test_samples.py
git commit -m "Parse Moonlighter and ceiling utilization logs into samples"
```

---

### Task 3: Transcript turn streamer

**Files:**
- Create: `tracker/turns.py`, `tests/test_turns.py`

**Interfaces:**
- Produces: `Turn(ts: datetime, model: str, input: int, output: int, cache_read: int, cache_write: int)` with `.total`; `iter_turns(paths: Iterable[Path]) -> Iterator[Turn]` streaming, deduplicated by `message.id` across all files; `transcript_paths(root: Path, since: datetime | None) -> list[Path]` returning `*.jsonl` under root with mtime ≥ since.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_turns.py
import json, os, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path
from tracker.turns import iter_turns, transcript_paths

def rec(mid, ts="2026-09-05T20:20:48.817Z", model="claude-fable-5-1", **usage):
    u = {"input_tokens": 2, "cache_creation_input_tokens": 41398, "cache_read_input_tokens": 24483, "output_tokens": 96}
    u.update(usage)
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": model, "usage": u}})

class TurnTests(unittest.TestCase):
    def test_dedups_same_message_id_across_blocks_and_files(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d, "a.jsonl"); b = Path(d, "b.jsonl")
            a.write_text("\n".join([rec("m1"), rec("m1"), json.dumps({"type": "user", "timestamp": "x"}), "garbage", rec("m2", model="claude-sonnet-5")]) + "\n")
            b.write_text(rec("m1") + "\n")
            turns = list(iter_turns([a, b]))
        self.assertEqual([t.model for t in turns], ["claude-fable-5-1", "claude-sonnet-5"])
        t = turns[0]
        self.assertEqual((t.input, t.output, t.cache_read, t.cache_write), (2, 96, 24483, 41398))
        self.assertEqual(t.total, 2 + 96 + 24483 + 41398)
        self.assertEqual(t.ts, datetime(2026, 9, 5, 20, 20, 48, 817000, tzinfo=timezone.utc))

    def test_paths_filtered_by_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d, "sub", "old.jsonl"); old.parent.mkdir(); old.write_text("")
            new = Path(d, "new.jsonl"); new.write_text("")
            os.utime(old, (0, 0))
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.assertEqual(transcript_paths(Path(d), since), [new])
            self.assertEqual(set(transcript_paths(Path(d), None)), {old, new})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_turns -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/turns.py
"""Stream Claude Code transcripts into deduplicated per-turn token usage."""
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Turn:
    ts: datetime
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iter_turns(paths: Iterable[Path]) -> Iterator[Turn]:
    seen: set[str] = set()
    for p in paths:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                mid = m.get("id")
                if not isinstance(u, dict) or not mid or mid in seen or not d.get("timestamp"):
                    continue
                seen.add(mid)
                yield Turn(_parse_ts(d["timestamp"]), m.get("model") or "unknown",
                           int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                           int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0))


def transcript_paths(root: Path, since: datetime | None) -> list[Path]:
    out = []
    for p in Path(root).rglob("*.jsonl"):
        if since is None or datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= since:
            out.append(p)
    return sorted(out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_turns -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tracker/turns.py tests/test_turns.py
git commit -m "Stream transcripts into deduplicated turns"
```

---

### Task 4: Passive join

**Files:**
- Create: `tracker/join.py`, `tests/test_join.py`

**Interfaces:**
- Consumes: `Sample` from Task 2, `Turn` from Task 3.
- Produces: `Interval(start, end, delta_pct: float, tokens: dict[str,int] by class, by_model: dict[str,int], model: str | None)`; `build_intervals(samples, turns) -> list[Interval]` (reset-straddling dropped, zero-delta pooled, attribution ≥90%); `daily_rates(intervals) -> dict[date, DailyRate]` where `DailyRate(tokens_per_pct: float, n: int, interpolated: bool, per_model: dict[str, float], split: dict[str, float])`; `MIN_INTERVALS_PER_DAY = 5`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_join.py
import unittest
from datetime import datetime, timezone, timedelta, date
from tracker.samples import Sample
from tracker.turns import Turn
from tracker.join import build_intervals, daily_rates

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
def S(mins, fh, reset="r1"): return Sample(T0 + timedelta(minutes=mins), fh, None, reset, "ceiling")
def T(mins, model="claude-sonnet-5", cr=100_000, inp=1000, out=500, cw=0):
    return Turn(T0 + timedelta(minutes=mins), model, inp, out, cr, cw)

class IntervalTests(unittest.TestCase):
    def test_basic_interval_and_attribution(self):
        iv = build_intervals([S(0, 10), S(5, 12)], [T(1), T(2), T(3)])
        self.assertEqual(len(iv), 1)
        self.assertEqual(iv[0].delta_pct, 2.0)
        self.assertEqual(iv[0].tokens["cache_read"], 300_000)
        self.assertEqual(iv[0].model, "claude-sonnet-5")

    def test_mixed_interval_has_no_model(self):
        iv = build_intervals([S(0, 10), S(5, 12)], [T(1), T(2, model="claude-opus-5")])
        self.assertIsNone(iv[0].model)

    def test_zero_delta_pooled_into_next(self):
        iv = build_intervals([S(0, 10), S(5, 10), S(10, 11)], [T(1), T(7)])
        self.assertEqual(len(iv), 1)
        self.assertEqual((iv[0].start, iv[0].end, iv[0].delta_pct), (S(0, 10).ts, S(10, 11).ts, 1.0))
        self.assertEqual(iv[0].tokens["cache_read"], 200_000)

    def test_reset_straddle_dropped(self):
        iv = build_intervals([S(0, 90), S(5, 3, reset="r2"), S(10, 5, reset="r2")], [T(1), T(7)])
        self.assertEqual(len(iv), 1)
        self.assertEqual(iv[0].start, S(5, 3).ts)

    def test_utilization_drop_without_reset_field_is_a_reset(self):
        iv = build_intervals([S(0, 90, reset=None), S(5, 3, reset=None)], [T(1)])
        self.assertEqual(iv, [])

class DailyTests(unittest.TestCase):
    def test_median_and_interpolation(self):
        samples = [S(i * 5, 10 + i) for i in range(7)]           # 6 intervals of 1% on day 1
        turns = [T(i * 5 + 1, cr=100_000 * (i + 1)) for i in range(6)]
        day2 = [Sample(T0 + timedelta(days=1, minutes=m), 10 + m // 5, None, "r9", "ceiling") for m in (0, 5)]
        iv = build_intervals(samples + day2, turns + [Turn(T0 + timedelta(days=1, minutes=1), "claude-sonnet-5", 0, 0, 5, 0)])
        rates = daily_rates(iv)
        d1 = rates[date(2026, 9, 1)]
        self.assertEqual(d1.n, 6)
        self.assertFalse(d1.interpolated)
        # tokens per interval: 101.5k, 201.5k, ... 601.5k -> median of 6 = (301500+401500)/2
        self.assertAlmostEqual(d1.tokens_per_pct, 351_500)
        self.assertIn("claude-sonnet-5", d1.per_model)
        self.assertAlmostEqual(sum(d1.split.values()), 1.0)
        d2 = rates[date(2026, 9, 2)]
        self.assertTrue(d2.interpolated)
        self.assertAlmostEqual(d2.tokens_per_pct, d1.tokens_per_pct)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_join -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/join.py
"""Join utilization samples with transcript turns into tokens-per-percent rates."""
from __future__ import annotations
import bisect
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median
from .samples import Sample
from .turns import Turn

CLASSES = ("input", "output", "cache_read", "cache_write")
ATTRIBUTION = 0.90
MIN_INTERVALS_PER_DAY = 5


@dataclass
class Interval:
    start: datetime
    end: datetime
    delta_pct: float
    tokens: dict = field(default_factory=lambda: {c: 0 for c in CLASSES})
    by_model: dict = field(default_factory=dict)
    model: str | None = None

    @property
    def total(self) -> int:
        return sum(self.tokens.values())


@dataclass
class DailyRate:
    tokens_per_pct: float
    n: int
    interpolated: bool
    per_model: dict
    split: dict


def _is_reset(a: Sample, b: Sample) -> bool:
    if a.resets_at and b.resets_at and a.resets_at != b.resets_at:
        return True
    return b.five_hour < a.five_hour


def build_intervals(samples: list[Sample], turns: list[Turn]) -> list[Interval]:
    samples = sorted(samples, key=lambda s: s.ts)
    turns = sorted(turns, key=lambda t: t.ts)
    keys = [t.ts for t in turns]
    out: list[Interval] = []
    cur: Interval | None = None
    for a, b in zip(samples, samples[1:]):
        if _is_reset(a, b):
            cur = None
            continue
        if cur is None:
            cur = Interval(a.ts, b.ts, 0.0)
        cur.end = b.ts
        cur.delta_pct += b.five_hour - a.five_hour
        for t in turns[bisect.bisect_left(keys, a.ts):bisect.bisect_left(keys, b.ts)]:
            for c in CLASSES:
                cur.tokens[c] += getattr(t, c)
            cur.by_model[t.model] = cur.by_model.get(t.model, 0) + t.total
        if cur.delta_pct >= 1:
            if cur.total > 0:
                top = max(cur.by_model, key=cur.by_model.get)
                if cur.by_model[top] / cur.total >= ATTRIBUTION:
                    cur.model = top
                out.append(cur)
            cur = None
    return out


def daily_rates(intervals: list[Interval]) -> dict[date, DailyRate]:
    by_day: dict[date, list[Interval]] = {}
    for iv in intervals:
        by_day.setdefault(iv.end.date(), []).append(iv)
    if not by_day:
        return {}
    out: dict[date, DailyRate] = {}
    prev: DailyRate | None = None
    d = min(by_day)
    while d <= max(by_day):
        ivs = by_day.get(d, [])
        if len(ivs) >= MIN_INTERVALS_PER_DAY:
            rates = [iv.total / iv.delta_pct for iv in ivs]
            per_model: dict[str, list[float]] = {}
            for iv in ivs:
                if iv.model:
                    per_model.setdefault(iv.model, []).append(iv.total / iv.delta_pct)
            tot = {c: sum(iv.tokens[c] for iv in ivs) for c in CLASSES}
            grand = sum(tot.values()) or 1
            prev = DailyRate(median(rates), len(ivs), False,
                             {m: median(v) for m, v in per_model.items()},
                             {c: tot[c] / grand for c in CLASSES})
            out[d] = prev
        elif prev is not None:
            out[d] = DailyRate(prev.tokens_per_pct, len(ivs), True, dict(prev.per_model), dict(prev.split))
        d = date.fromordinal(d.toordinal() + 1)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_join -v`
Expected: 6 tests PASS. If `test_median_and_interpolation` fails on the median value, print the interval totals and adjust the fixture arithmetic, not the pooling logic.

- [ ] **Step 5: Commit**

```bash
git add tracker/join.py tests/test_join.py
git commit -m "Join samples with turns into daily tokens-per-percent rates"
```

---

### Task 5: `claude -p` runner and JSON usage parser

**Files:**
- Create: `tracker/cli_run.py`, `tests/test_cli_run.py`

**Interfaces:**
- Produces: `RunUsage(model: str, input: int, output: int, cache_read: int, cache_write: int, cost_usd: float | None, duration_s: float)` with `.total`; `parse_result(stdout: str, model_hint: str) -> RunUsage`; `run_prompt(prompt: str, model: str, effort: str, config_dir: Path | None, runner=None) -> RunUsage`; `build_argv(model, effort) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_run.py
import json, unittest
from pathlib import Path
from tracker.cli_run import parse_result, run_prompt, build_argv

RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 4321,
    "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 5, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 12000, "output_tokens": 80},
    "modelUsage": {"claude-sonnet-5": {"inputTokens": 5, "outputTokens": 80, "cacheReadInputTokens": 12000, "cacheCreationInputTokens": 300, "costUSD": 0.0123}}})

class CliTests(unittest.TestCase):
    def test_parse_prefers_model_usage_block(self):
        u = parse_result(RESULT, "claude-sonnet-5")
        self.assertEqual((u.input, u.output, u.cache_read, u.cache_write), (5, 80, 12000, 300))
        self.assertEqual(u.model, "claude-sonnet-5")
        self.assertAlmostEqual(u.cost_usd, 0.0123)
        self.assertAlmostEqual(u.duration_s, 4.321)
        self.assertEqual(u.total, 12385)

    def test_parse_falls_back_to_usage_block(self):
        d = json.loads(RESULT); del d["modelUsage"]
        u = parse_result(json.dumps(d), "claude-sonnet-5")
        self.assertEqual(u.cache_read, 12000)

    def test_error_result_raises(self):
        d = json.loads(RESULT); d["is_error"] = True
        with self.assertRaises(RuntimeError):
            parse_result(json.dumps(d), "claude-sonnet-5")

    def test_argv_and_env(self):
        self.assertEqual(build_argv("claude-opus-5", "low"),
                         ["claude", "-p", "--model", "claude-opus-5", "--effort", "low", "--output-format", "json", "--restricted"])
        seen = {}
        def runner(argv, prompt, env):
            seen.update(argv=argv, prompt=prompt, env=env); return RESULT
        u = run_prompt("say hi", "claude-sonnet-5", "low", Path("/x/.claude-dave"), runner=runner)
        self.assertEqual(seen["env"]["CLAUDE_CONFIG_DIR"], "/x/.claude-dave")
        self.assertEqual(seen["prompt"], "say hi")
        self.assertEqual(u.output, 80)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cli_run -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/cli_run.py
"""Run one `claude -p` prompt and return its token usage."""
from __future__ import annotations
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class RunUsage:
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    cost_usd: float | None
    duration_s: float

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def build_argv(model: str, effort: str) -> list[str]:
    return ["claude", "-p", "--model", model, "--effort", effort, "--output-format", "json", "--restricted"]


def parse_result(stdout: str, model_hint: str) -> RunUsage:
    d = json.loads(stdout)
    if d.get("is_error"):
        raise RuntimeError(f"claude -p returned an error result: {d.get('result') or d.get('subtype')}")
    mu = d.get("modelUsage") or {}
    if mu:
        model, m = next(iter(mu.items()))
        return RunUsage(model, int(m.get("inputTokens") or 0), int(m.get("outputTokens") or 0),
                        int(m.get("cacheReadInputTokens") or 0), int(m.get("cacheCreationInputTokens") or 0),
                        m.get("costUSD", d.get("total_cost_usd")), (d.get("duration_ms") or 0) / 1000)
    u = d.get("usage") or {}
    return RunUsage(model_hint, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0),
                    d.get("total_cost_usd"), (d.get("duration_ms") or 0) / 1000)


def _default_runner(argv: list[str], prompt: str, env: dict) -> str:
    p = subprocess.run(argv, input=prompt, capture_output=True, text=True, env=env, timeout=900)
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {p.stderr[-500:]}")
    return p.stdout


def run_prompt(prompt: str, model: str, effort: str, config_dir: Path | None,
               runner: Callable[[list[str], str, dict], str] | None = None) -> RunUsage:
    env = dict(os.environ)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env.pop("ANTHROPIC_API_KEY", None)  # must bill the subscription, never an API key
    out = (runner or _default_runner)(build_argv(model, effort), prompt, env)
    return parse_result(out, model)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cli_run -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tracker/cli_run.py tests/test_cli_run.py
git commit -m "Run claude -p and parse its JSON usage"
```

---

### Task 6: Tick probe

**Files:**
- Create: `tracker/probe.py`, `tests/test_probe.py`

**Interfaces:**
- Consumes: `Utilization` (Task 1), `RunUsage` (Task 5).
- Produces: `ProbeResult(ts, account, model, effort, tokens_per_pct: float, tokens: dict by class, prompts: int, tick_from: float, tick_to: float, elapsed_s: float)`; `run_tick_probe(model, effort, prompt, read, run, sleep, now, max_prompts=60) -> ProbeResult` where `read() -> Utilization` and `run() -> RunUsage`; `is_idle(read, sleep, window_s=120) -> bool`; `choose_account(accounts: list[tuple[str, Path]], read_for, sleep, max_wait_s=4*3600, retry_s=900) -> tuple[str, Path] | None`; `append_result(path, result)`; `PROBE_PROMPT` constant; `class ProbeAbort(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probe.py
import json, tempfile, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tracker.usage_api import Utilization
from tracker.cli_run import RunUsage
from tracker.probe import run_tick_probe, is_idle, choose_account, append_result, ProbeAbort

T0 = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)

def util_seq(values, reset="r1"):
    it = iter(values)
    def read():
        v = next(it)
        return Utilization(T0, v, 30.0, reset if v is not None else None)
    return read

def runner(tokens=20_000):
    def run():
        return RunUsage("claude-sonnet-5", 100, 400, tokens - 500, 0, 0.001, 3.0)
    return run

class TickTests(unittest.TestCase):
    def test_two_ticks_give_tokens_per_pct(self):
        # readings after each prompt: 10,10,11(tick1),11,11,11,12(tick2)
        read = util_seq([10, 10, 10, 11, 11, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0)
        self.assertEqual(r.prompts, 7)
        self.assertEqual((r.tick_from, r.tick_to), (11, 12))
        self.assertEqual(r.tokens_per_pct, 80_000)  # 4 prompts between ticks
        self.assertEqual(r.tokens["cache_read"], 4 * 19_500)

    def test_reset_mid_probe_aborts(self):
        vals = [90, 90, 91, 91, 2]
        it = iter(vals)
        def read():
            v = next(it)
            return Utilization(T0, v, 30.0, "r1" if v > 5 else "r2")
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0)

    def test_too_many_prompts_aborts(self):
        read = util_seq([10] * 100)
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0, max_prompts=5)

    def test_tick_faster_than_prompts_explain_aborts(self):
        # a jump of 3% after one 20k prompt cannot be ours
        read = util_seq([10, 10, 11, 14])
        with self.assertRaises(ProbeAbort):
            run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0)

class IdleTests(unittest.TestCase):
    def test_idle_when_two_readings_match(self):
        slept = []
        self.assertTrue(is_idle(util_seq([7, 7]), slept.append))
        self.assertEqual(slept, [120])

    def test_busy_when_moved(self):
        self.assertFalse(is_idle(util_seq([7, 8]), lambda s: None))

    def test_choose_account_falls_back_then_waits(self):
        reads = {"dave": util_seq([7, 8, 8, 8]), "jono": util_seq([3, 4, 4, 4])}
        slept = []
        acc = choose_account([("dave", Path("/d")), ("jono", Path("/j"))], lambda name: reads[name], slept.append, max_wait_s=3600, retry_s=900)
        self.assertEqual(acc, ("dave", Path("/d")))
        self.assertIn(900, slept)

    def test_choose_account_gives_up(self):
        reads = {"dave": util_seq([1, 2] * 50)}
        acc = choose_account([("dave", Path("/d"))], lambda n: reads[n], lambda s: None, max_wait_s=1800, retry_s=900)
        self.assertIsNone(acc)

class AppendTests(unittest.TestCase):
    def test_append_writes_jsonl(self):
        read = util_seq([10, 11, 11, 12])
        r = run_tick_probe("claude-sonnet-5", "low", "p", read, runner(), sleep=lambda s: None, now=lambda: T0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "probes.jsonl")
            append_result(p, r, account="dave")
            row = json.loads(p.read_text().splitlines()[0])
        self.assertEqual(row["account"], "dave")
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertEqual(row["tokens_per_pct"], 40_000)
        self.assertEqual(row["ts"], "2026-09-06T08:00:00+00:00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_probe -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/probe.py
"""Tick probe: loop a fixed small prompt until 5-hour utilization ticks twice."""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable
from .usage_api import Utilization
from .cli_run import RunUsage

CLASSES = ("input", "output", "cache_read", "cache_write")

PROBE_PROMPT = (
    "You are a probe. Reply with exactly the 26 lowercase letters of the English "
    "alphabet separated by spaces, then the numbers 1 to 40 separated by spaces, "
    "then the word DONE. No other text."
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


def _same_window(a: Utilization, b: Utilization) -> bool:
    if a.five_hour_resets_at and b.five_hour_resets_at and a.five_hour_resets_at != b.five_hour_resets_at:
        return False
    return (b.five_hour or 0) >= (a.five_hour or 0)


def run_tick_probe(model: str, effort: str, prompt: str, read: Callable[[], Utilization],
                   run: Callable[[], RunUsage], sleep: Callable[[float], None],
                   now: Callable[[], datetime], max_prompts: int = 60, settle_s: float = 5) -> ProbeResult:
    start = now()
    before = read()
    last = before
    prompts = 0
    tick1: int | None = None
    spent = {c: 0 for c in CLASSES}
    largest = 0
    while prompts < max_prompts:
        u = run()
        prompts += 1
        largest = max(largest, u.total)
        if tick1 is not None:
            for c in CLASSES:
                spent[c] += getattr(u, c)
        sleep(settle_s)
        cur = read()
        if not _same_window(last, cur):
            raise ProbeAbort("window reset during probe")
        jump = (cur.five_hour or 0) - (last.five_hour or 0)
        if jump >= 2 or (jump >= 1 and tick1 is not None and sum(spent.values()) < largest):
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
    return a.five_hour == b.five_hour and a.five_hour_resets_at == b.five_hour_resets_at


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_probe -v`
Expected: 9 tests PASS. `test_choose_account_falls_back_then_waits`: dave reads 7 then 8 (busy), jono reads 3 then 4 (busy), sleep 900, dave reads 8 then 8 (idle) → dave. Confirm the `util_seq` lengths support that order; extend the sequences if `StopIteration` appears.

- [ ] **Step 5: Add the CLI entry point**

Append to `tracker/probe.py`:

```python
def main(argv: list[str] | None = None) -> int:
    import argparse, os, sys, time
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
    read_for = lambda name: (lambda: read_usage(dict(accounts)[name]))
    picked = choose_account(accounts, read_for, time.sleep, max_wait_s=a.max_wait)
    if picked is None:
        print("probe skipped: no idle account within max wait", file=sys.stderr)
        return 3
    name, cfg = picked
    try:
        r = run_tick_probe(a.model, a.effort, PROBE_PROMPT, lambda: read_usage(cfg),
                           lambda: run_prompt(PROBE_PROMPT, a.model, a.effort, cfg),
                           time.sleep, lambda: datetime.now(timezone.utc))
    except ProbeAbort as e:
        print(f"probe aborted on {name}: {e}", file=sys.stderr)
        return 4
    append_result(a.out, r, account=name)
    print(f"{name} {a.model} {a.effort}: {r.tokens_per_pct:.0f} tokens per 1% ({r.prompts} prompts, ticks {r.tick_from}->{r.tick_to})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `python3 -m tracker.probe --help`
Expected: usage text printed, exit 0.

- [ ] **Step 6: Commit**

```bash
git add tracker/probe.py tests/test_probe.py
git commit -m "Tick probe with idle guard and account fallback"
```

---

### Task 7: Spike — validate the method on real data

This task spends real usage (one probe, roughly 1 to 2% of a 5-hour window on masterrig's account) and reads real logs. It is the acceptance test for the method. Output is a findings document, not code.

**Files:**
- Create: `docs/spike-2026-09.md`, `bin/passive.sh` (first draft, refined in Task 10)

- [ ] **Step 1: Passive join over the whole history**

Run from the repo root:

```bash
python3 - <<'EOF'
from pathlib import Path
from datetime import datetime, timezone
from tracker.samples import parse_moonlighter, parse_ceiling_log, merge_samples
from tracker.turns import iter_turns, transcript_paths
from tracker.join import build_intervals, daily_rates
home = Path.home()
ml = parse_moonlighter(open(home/".moonlighter/usage_log.jsonl"))
cl = parse_ceiling_log(open(home/".paperclip/ops/mis-usage-ceiling-systemd.log"))
samples = merge_samples(ml, cl)
turns = list(iter_turns(transcript_paths(home/".claude/projects", None)))
iv = build_intervals(samples, turns)
rates = daily_rates(iv)
print("samples", len(samples), "turns", len(turns), "intervals", len(iv), "days", len(rates))
for d, r in sorted(rates.items()):
    print(d, f"{r.tokens_per_pct:,.0f}/pct", r.n, "interp" if r.interpolated else "", {m: f"{v:,.0f}" for m, v in r.per_model.items()}, {c: f"{v:.2f}" for c, v in r.split.items()})
EOF
```

Expected: a daily table from mid June to today. Record in the findings doc: the median rate for the Max 5x period (before 2026-08-18) and the Max 20x period (after), the ratio between them, the cache split, and whether any step is visible around 2026-09-02. Runtime should be under a minute; if it is not, note it.

- [ ] **Step 2: Cross-check against Moonlighter's June calibration**

`~/.moonlighter/calibration.jsonl` holds 17 rows of `tokens_spent` per `util_delta` measured in June and July on the seven-day bucket. Compute their median tokens per 1% and note whether it agrees with the passive join for the same weeks within 20%. Disagreement is a finding, not a failure.

- [ ] **Step 3: Size the probe prompt**

Run one prompt with the probe text and low effort and read the token count:

```bash
python3 - <<'EOF'
from tracker.cli_run import run_prompt
from tracker.probe import PROBE_PROMPT
u = run_prompt(PROBE_PROMPT, "claude-sonnet-5", "low", None)
print(u)
EOF
```

Expected: a `RunUsage` with a few thousand tokens, mostly cache read and system prompt. Divide the passive Max 20x rate per percent by this total: that is prompts per tick. Target is 5 to 15 prompts per tick. If it is above 15, make `PROBE_PROMPT` ask for more output (for example the numbers 1 to 200); if below 5, ask for less. Record the chosen prompt and its token count.

- [ ] **Step 4: Run one real tick probe on masterrig**

```bash
PROBE_OUT=out/spike-probes.jsonl python3 -m tracker.probe --model claude-sonnet-5 --effort low --account main=$HOME/.claude --max-wait 600
```

Do not run this while a Claude session is active on this account; the idle guard will refuse and that is correct. Expected: one line printed with tokens per 1%, and `out/spike-probes.jsonl` gaining a row. Record: tokens per percent, prompts used, elapsed seconds, and the actual 5-hour utilization cost (tick_to minus reading before the probe).

- [ ] **Step 5: Compare probe and passive for today**

Passive rate for today from Step 1 versus the probe's tokens per percent. They measure different things (a working session's mix vs a tiny fixed prompt) so they need not match, but they must be the same order of magnitude and the probe must be the more stable one across two runs if time allows a second. Record both.

- [ ] **Step 6: Weekly cost of the cadence**

Two probes a day, each about the utilization cost from Step 4. Express as percent of the 7-day bucket per week using the ratio between the 5-hour and 7-day movements seen in the Moonlighter log (a 1% 5-hour tick moves the 7-day bucket by some fraction; measure it from consecutive samples). Record it. If it exceeds 5% of the weekly budget, drop to once a day and record that decision.

- [ ] **Step 7: Write the findings and commit**

`docs/spike-2026-09.md` sections: Passive history (table summary, ratio 5x to 20x, cache split, any step), Moonlighter cross-check, Probe sizing (prompt text, tokens per prompt, prompts per tick), Probe result, Probe vs passive, Weekly cost and cadence decision, Decisions carried into later tasks. Then:

```bash
git add docs/spike-2026-09.md tracker/probe.py
git commit -m "Spike: validate passive join and tick probe on real data"
```

If `PROBE_PROMPT` changed, the commit includes it. If any spec assumption failed (for example the passive join cannot attribute per model because sessions mix models constantly), stop and report before continuing; the plan's later tasks assume the spec holds.

---

### Task 8: Change detection

**Files:**
- Create: `tracker/detect.py`, `tests/test_detect.py`

**Interfaces:**
- Produces: `ChangeEvent(date: date, direction: str, percent: int, model: str)`; `detect_changes(series: dict[str, dict[date, float]], threshold=0.05, recent_days=3, base_days=7, persist_days=2) -> list[ChangeEvent]`; `latest_change(events) -> ChangeEvent | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_detect.py
import unittest
from datetime import date, timedelta
from tracker.detect import detect_changes, latest_change

def series(values, start=date(2026, 8, 20)):
    return {start + timedelta(days=i): v for i, v in enumerate(values)}

class DetectTests(unittest.TestCase):
    def test_flat_series_no_event(self):
        s = {"claude-sonnet-5": series([100] * 20)}
        self.assertEqual(detect_changes(s), [])

    def test_step_down_detected_once_with_percent(self):
        s = {"claude-sonnet-5": series([100] * 10 + [86] * 10)}
        ev = detect_changes(s)
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0].direction, ev[0].percent, ev[0].model), ("decreased", 14, "claude-sonnet-5"))
        self.assertEqual(ev[0].date, date(2026, 8, 30))

    def test_one_day_blip_ignored(self):
        s = {"claude-sonnet-5": series([100] * 10 + [70] + [100] * 9)}
        self.assertEqual(detect_changes(s), [])

    def test_small_drift_ignored(self):
        s = {"claude-sonnet-5": series([100] * 10 + [97] * 10)}
        self.assertEqual(detect_changes(s), [])

    def test_step_up(self):
        s = {"claude-opus-5": series([50] * 10 + [60] * 10)}
        ev = detect_changes(s)
        self.assertEqual((ev[0].direction, ev[0].percent), ("increased", 20))

    def test_latest_change_picks_newest(self):
        s = {"claude-sonnet-5": series([100] * 10 + [80] * 10 + [100] * 10)}
        ev = detect_changes(s)
        self.assertEqual(len(ev), 2)
        self.assertEqual(latest_change(ev).direction, "increased")
        self.assertIsNone(latest_change([]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_detect -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/detect.py
"""Detect persistent step changes in daily tokens-per-percent series."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median


@dataclass(frozen=True)
class ChangeEvent:
    date: date
    direction: str
    percent: int
    model: str


def _ratio_on(days: list[date], vals: dict[date, float], i: int, recent_days: int, base_days: int) -> float | None:
    if i < base_days + recent_days - 1:
        return None
    recent = [vals[d] for d in days[i - recent_days + 1:i + 1]]
    base = [vals[d] for d in days[i - recent_days - base_days + 1:i - recent_days + 1]]
    b = median(base)
    return (median(recent) - b) / b if b else None


def detect_changes(series: dict[str, dict[date, float]], threshold: float = 0.05, recent_days: int = 3,
                   base_days: int = 7, persist_days: int = 2) -> list[ChangeEvent]:
    events: list[ChangeEvent] = []
    for model, vals in series.items():
        days = sorted(vals)
        run = 0
        armed = True
        for i in range(len(days)):
            r = _ratio_on(days, vals, i, recent_days, base_days)
            if r is None:
                continue
            if abs(r) > threshold:
                run += 1
                if run == persist_days and armed:
                    first = days[i - persist_days + 1]
                    # date the change to the first day the recent window stepped
                    step_day = days[i - recent_days - persist_days + 2]
                    events.append(ChangeEvent(step_day, "increased" if r > 0 else "decreased", round(abs(r) * 100), model))
                    armed = False
            else:
                run = 0
                armed = True
    return sorted(events, key=lambda e: e.date)


def latest_change(events: list[ChangeEvent]) -> ChangeEvent | None:
    return max(events, key=lambda e: e.date) if events else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_detect -v`
Expected: 6 tests PASS. If the dated day in `test_step_down_detected_once_with_percent` is off by one, adjust `step_day` so the event carries the first day of the new level (2026-08-30 in the fixture), and keep the test as written.

- [ ] **Step 5: Commit**

```bash
git add tracker/detect.py tests/test_detect.py
git commit -m "Detect persistent step changes in daily rates"
```

---

### Task 9: Publisher — assemble the public JSON

**Files:**
- Create: `tracker/publish.py`, `tests/test_publish.py`, `data/prices.json`, `data/effort_matrix.json` (placeholder values until Task 11 replaces them)

**Interfaces:**
- Consumes: `ProbeResult` rows from `probes.jsonl`; `DailyRate` dict from Task 4 serialised as `history/passive.json`; `detect_changes` from Task 8.
- Produces: `load_probes(path) -> list[dict]`; `probe_daily_series(rows, trailing_days=3) -> dict[model, dict[date, float]]` (tokens per window = tokens_per_pct × 100); `build_public_json(probe_rows, passive, effort, prices, now) -> dict`; `write_json(path, obj)`.

- [ ] **Step 1: Create the static inputs**

```json
// data/prices.json  (USD per million tokens; verify against ~/.claude/references/claude-models-quickref.md, never from memory)
{
  "claude-sonnet-5":  {"input": 3,  "output": 15, "cache_read": 0.3, "cache_write": 3.75},
  "claude-opus-5":    {"input": 15, "output": 75, "cache_read": 1.5, "cache_write": 18.75},
  "claude-fable-5-1": {"input": 15, "output": 75, "cache_read": 1.5, "cache_write": 18.75}
}
```

Open `~/.claude/references/claude-models-quickref.md` and copy its rates over these placeholders before committing. Do not load the `claude-api` skill for this.

```json
// data/effort_matrix.json  (tokens per calibration task; PLACEHOLDER until Task 11)
{
  "_status": "placeholder",
  "claude-sonnet-5":  {"low": 900000, "medium": 1400000, "high": 2520000, "xhigh": 3900000, "max": 5600000},
  "claude-opus-5":    {"low": 900000, "medium": 1400000, "high": 2520000, "xhigh": 3900000, "max": 5600000},
  "claude-fable-5-1": {"low": 900000, "medium": 1400000, "high": 2520000, "xhigh": 3900000, "max": 5600000}
}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_publish.py
import json, unittest
from datetime import date, datetime, timezone, timedelta
from tracker.publish import probe_daily_series, build_public_json

def probe(day, model, tpp, account="dave"):
    return {"ts": f"2026-09-{day:02d}T08:00:00+00:00", "model": model, "effort": "low", "tokens_per_pct": tpp,
            "tokens": {"input": 100, "output": 400, "cache_read": tpp - 500, "cache_write": 0},
            "prompts": 8, "tick_from": 10, "tick_to": 11, "elapsed_s": 60, "account": account}

PASSIVE = {"generated_at": "2026-09-05T20:00:00+00:00", "plan_ratio_5x_to_20x": 0.25,
           "split": {"input": 0.062, "output": 0.021, "cache_read": 0.907, "cache_write": 0.010},
           "history": {"2026-08-01": {"tokens_per_pct": 100000, "interpolated": False}}}
EFFORT = {"claude-sonnet-5": {"low": 900000, "high": 2520000}}
PRICES = {"claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}}

class SeriesTests(unittest.TestCase):
    def test_trailing_median_per_model(self):
        rows = [probe(1, "claude-sonnet-5", 400000), probe(2, "claude-sonnet-5", 420000), probe(3, "claude-sonnet-5", 410000)]
        s = probe_daily_series(rows)
        self.assertEqual(s["claude-sonnet-5"][date(2026, 9, 3)], 41_000_000)
        self.assertEqual(s["claude-sonnet-5"][date(2026, 9, 1)], 40_000_000)

class BuildTests(unittest.TestCase):
    def test_shape(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 6)]
        now = datetime(2026, 9, 5, 20, 15, tzinfo=timezone.utc)
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, now)
        self.assertEqual(j["generated_at"], "2026-09-05T20:15:00+00:00")
        self.assertEqual(j["last_sample_at"], "2026-09-05T08:00:00+00:00")
        self.assertEqual(j["plan_measured"], "max20")
        self.assertEqual(j["plan_ratios"], {"pro": 0.05, "max5": 0.25, "max20": 1.0})
        r = j["rates"]["claude-sonnet-5"]
        self.assertEqual(r["tokens_per_window"], 42_000_000)
        self.assertEqual(r["source"], "probe")
        self.assertEqual(r["split"], PASSIVE["split"])
        self.assertEqual(j["effort"], EFFORT)
        self.assertEqual(j["api_price_per_mtok"], PRICES)
        self.assertIsNone(j["last_change"])
        hist = j["history"]["claude-sonnet-5"]
        self.assertEqual(hist[0], {"date": "2026-08-01", "tokens_per_window": 10_000_000, "source": "passive", "interpolated": False})
        self.assertEqual(hist[-1]["source"], "probe")

    def test_change_event_surfaces(self):
        rows = [probe(d, "claude-sonnet-5", 420000) for d in range(1, 11)] + [probe(d, "claude-sonnet-5", 360000) for d in range(11, 16)]
        j = build_public_json(rows, PASSIVE, EFFORT, PRICES, datetime(2026, 9, 15, tzinfo=timezone.utc))
        self.assertEqual(j["last_change"]["direction"], "decreased")
        self.assertEqual(j["last_change"]["percent"], 14)
        self.assertEqual(j["last_change"]["model"], "claude-sonnet-5")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m unittest tests.test_publish -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Write minimal implementation**

```python
# tracker/publish.py
"""Assemble the public JSON from probe rows, passive output, and static tables."""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from .detect import detect_changes, latest_change

PLAN_RATIOS_BASE = {"pro": 0.05, "max5": 0.25, "max20": 1.0}
HISTORY_DAYS = 90


def load_probes(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def probe_daily_series(rows: list[dict], trailing_days: int = 3) -> dict[str, dict[date, float]]:
    by_model: dict[str, dict[date, list[float]]] = {}
    for r in rows:
        d = datetime.fromisoformat(r["ts"]).date()
        by_model.setdefault(r["model"], {}).setdefault(d, []).append(r["tokens_per_pct"] * 100)
    out: dict[str, dict[date, float]] = {}
    for model, days in by_model.items():
        series: dict[date, float] = {}
        for d in sorted(days):
            window = [v for dd, vs in days.items() if d - timedelta(days=trailing_days - 1) <= dd <= d for v in vs]
            series[d] = median(window)
        out[model] = series
    return out


def build_public_json(probe_rows: list[dict], passive: dict, effort: dict, prices: dict, now: datetime) -> dict:
    series = probe_daily_series(probe_rows)
    events = detect_changes(series)
    last = latest_change(events)
    ratios = dict(PLAN_RATIOS_BASE)
    if passive.get("plan_ratio_5x_to_20x"):
        ratios["max5"] = passive["plan_ratio_5x_to_20x"]
        ratios["pro"] = ratios["max5"] / 5
    cutoff = now.date() - timedelta(days=HISTORY_DAYS)
    first_probe_day = min((min(s) for s in series.values()), default=None)
    rates, history = {}, {}
    for model, s in series.items():
        latest_day = max(s)
        rates[model] = {"tokens_per_window": round(s[latest_day]), "source": "probe",
                        "probe_effort": next(r["effort"] for r in probe_rows if r["model"] == model),
                        "split": passive.get("split", {})}
        hist = []
        for ds, v in sorted(passive.get("history", {}).items()):
            d = date.fromisoformat(ds)
            if d >= cutoff and (first_probe_day is None or d < first_probe_day):
                hist.append({"date": ds, "tokens_per_window": round(v["tokens_per_pct"] * 100), "source": "passive", "interpolated": bool(v.get("interpolated"))})
        for d in sorted(s):
            if d >= cutoff:
                hist.append({"date": d.isoformat(), "tokens_per_window": round(s[d]), "source": "probe", "interpolated": False})
        history[model] = hist
    last_sample = max((r["ts"] for r in probe_rows), default=None)
    return {
        "generated_at": now.isoformat(),
        "last_sample_at": last_sample,
        "plan_measured": "max20",
        "plan_ratios": ratios,
        "rates": rates,
        "effort": effort,
        "api_price_per_mtok": prices,
        "history": history,
        "last_change": None if last is None else {"date": last.date.isoformat(), "direction": last.direction, "percent": last.percent, "model": last.model},
    }


def write_json(path: Path, obj: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest tests.test_publish -v`
Expected: 3 tests PASS

- [ ] **Step 6: Add the CLI entry point**

Append to `tracker/publish.py`:

```python
def main(argv: list[str] | None = None) -> int:
    import argparse
    from datetime import timezone
    ap = argparse.ArgumentParser(description="Write the public claude-usage.json")
    ap.add_argument("--probes", type=Path, required=True)
    ap.add_argument("--passive", type=Path, required=True, help="history/passive.json from bin/passive.sh")
    ap.add_argument("--effort", type=Path, default=Path("data/effort_matrix.json"))
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    passive = json.loads(a.passive.read_text()) if a.passive.exists() else {}
    effort = {k: v for k, v in json.loads(a.effort.read_text()).items() if not k.startswith("_")}
    j = build_public_json(load_probes(a.probes), passive, effort, json.loads(a.prices.read_text()), datetime.now(timezone.utc))
    write_json(a.out, j)
    print(f"wrote {a.out}: {len(j['rates'])} models, last_change={j['last_change']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `python3 -m tracker.publish --help`
Expected: usage text, exit 0.

- [ ] **Step 7: Commit**

```bash
git add tracker/publish.py tests/test_publish.py data/prices.json data/effort_matrix.json
git commit -m "Assemble the public usage JSON from probes and passive history"
```

---

### Task 10: Passive job on masterrig and its output file

**Files:**
- Create: `tracker/passive.py`, `tests/test_passive.py`, `bin/passive.sh`

**Interfaces:**
- Consumes: Tasks 2, 3, 4.
- Produces: `passive_summary(rates: dict[date, DailyRate], plan_change: date) -> dict` with keys `generated_at`, `plan_ratio_5x_to_20x`, `split`, `history`; `bin/passive.sh` writes `history/passive.json` and pushes it to the private GitHub remote.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_passive.py
import unittest
from datetime import date, timedelta
from tracker.join import DailyRate
from tracker.passive import passive_summary

def dr(v, split=None, interp=False):
    return DailyRate(v, 6, interp, {}, split or {"input": .1, "output": .1, "cache_read": .7, "cache_write": .1})

class PassiveTests(unittest.TestCase):
    def test_ratio_and_split_from_last_14_days(self):
        change = date(2026, 8, 18)
        rates = {change - timedelta(days=i): dr(100000) for i in range(1, 15)}
        rates.update({change + timedelta(days=i): dr(400000) for i in range(0, 15)})
        s = passive_summary(rates, change)
        self.assertAlmostEqual(s["plan_ratio_5x_to_20x"], 0.25)
        self.assertEqual(s["split"]["cache_read"], .7)
        self.assertEqual(len(s["history"]), 29)
        self.assertEqual(s["history"]["2026-08-18"], {"tokens_per_pct": 400000, "interpolated": False})
        self.assertIn("generated_at", s)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_passive -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/passive.py
"""Run the passive join on masterrig and summarise it for the publisher."""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from .join import DailyRate, build_intervals, daily_rates
from .samples import merge_samples, parse_ceiling_log, parse_moonlighter
from .turns import iter_turns, transcript_paths

PLAN_CHANGE = date(2026, 8, 18)


def passive_summary(rates: dict[date, DailyRate], plan_change: date = PLAN_CHANGE) -> dict:
    before = [r.tokens_per_pct for d, r in rates.items() if plan_change - timedelta(days=14) <= d < plan_change and not r.interpolated]
    after = [r.tokens_per_pct for d, r in rates.items() if plan_change <= d < plan_change + timedelta(days=14) and not r.interpolated]
    ratio = (median(before) / median(after)) if before and after else None
    recent = [r for d, r in sorted(rates.items())[-14:] if not r.interpolated]
    split = {}
    if recent:
        for c in recent[-1].split:
            split[c] = round(median(r.split[c] for r in recent), 4)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_ratio_5x_to_20x": None if ratio is None else round(ratio, 4),
        "split": split,
        "history": {d.isoformat(): {"tokens_per_pct": round(r.tokens_per_pct), "interpolated": r.interpolated} for d, r in sorted(rates.items())},
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Passive join over masterrig logs")
    ap.add_argument("--out", type=Path, default=Path("history/passive.json"))
    a = ap.parse_args(argv)
    home = Path.home()
    with open(home / ".moonlighter/usage_log.jsonl", encoding="utf-8") as f:
        ml = parse_moonlighter(f)
    cl_path = home / ".paperclip/ops/mis-usage-ceiling-systemd.log"
    cl = parse_ceiling_log(open(cl_path, encoding="utf-8")) if cl_path.exists() else []
    samples = merge_samples(ml, cl)
    turns = list(iter_turns(transcript_paths(home / ".claude/projects", None)))
    rates = daily_rates(build_intervals(samples, turns))
    summary = passive_summary(rates)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {a.out}: {len(rates)} days, ratio {summary['plan_ratio_5x_to_20x']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_passive -v`
Expected: 1 test PASS

- [ ] **Step 5: Create the private remote and the cron wrapper**

```bash
cd ~/code/claude-usage-tracker
gh repo create jonathanavis96/claude-usage-tracker --private --source=. --remote=origin --push
```

`history/` is gitignored in Task 1's `.gitignore`; remove that line so `history/passive.json` is tracked (it is small and private).

```bash
# bin/passive.sh
#!/usr/bin/env bash
# Daily on masterrig: passive join, commit history/passive.json, push. Safe to run when gs is down.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m tracker.passive --out history/passive.json
git add history/passive.json
git -c user.name=tracker -c user.email=tracker@local commit -q -m "Passive history $(date -u +%F)" || exit 0
git push -q origin main
```

```bash
chmod +x bin/passive.sh && sed -i '/^history\/$/d' .gitignore && ./bin/passive.sh
```

Expected: `wrote history/passive.json: N days, ratio 0.2x`, a commit, a push. Then add the cron line on masterrig with `crontab -e`:

```
15 3 * * * /home/grafe/code/claude-usage-tracker/bin/passive.sh >> /home/grafe/.paperclip/ops/claude-usage-passive.log 2>&1
```

- [ ] **Step 6: Commit**

```bash
git add tracker/passive.py tests/test_passive.py bin/passive.sh .gitignore
git commit -m "Passive join job with daily summary and push"
git push
```

---

### Task 11: Effort calibration (one-off run)

**Files:**
- Create: `tracker/calibrate.py`, `tests/test_calibrate.py`
- Modify: `data/effort_matrix.json` (replace placeholder)

**Interfaces:**
- Consumes: `run_prompt` (Task 5), `read_usage` (Task 1).
- Produces: `calibrate(models, efforts, repeats, run, read, sleep) -> dict` matrix `{model: {effort: median_tokens}}` plus `_meta`; `CALIBRATION_PROMPT`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calibrate.py
import unittest
from tracker.cli_run import RunUsage
from tracker.usage_api import Utilization
from tracker.calibrate import calibrate
from datetime import datetime, timezone

class CalTests(unittest.TestCase):
    def test_matrix_is_median_of_repeats(self):
        calls = []
        def run(prompt, model, effort):
            calls.append((model, effort))
            n = {"low": 1000, "high": 3000}[effort] + (len(calls) % 3) * 10
            return RunUsage(model, 100, n - 100, 0, 0, 0.0, 1.0)
        util = iter([Utilization(datetime.now(timezone.utc), 10.0, 5.0, "r")] * 100)
        m = calibrate(["claude-sonnet-5"], ["low", "high"], 3, run, lambda: next(util), lambda s: None)
        self.assertEqual(len(calls), 6)
        self.assertEqual(m["claude-sonnet-5"]["low"], 1010)
        self.assertEqual(m["claude-sonnet-5"]["high"], 3010)
        self.assertEqual(m["_meta"]["repeats"], 3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_calibrate -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# tracker/calibrate.py
"""One-off effort calibration: tokens one representative task burns per model and effort."""
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


def calibrate(models: list[str], efforts: list[str], repeats: int, run: Callable, read: Callable, sleep: Callable) -> dict:
    matrix: dict = {"_meta": {"repeats": repeats, "started": datetime.now(timezone.utc).isoformat(), "usage_deltas": {}}}
    for model in models:
        matrix[model] = {}
        for effort in efforts:
            before = read()
            totals = []
            for _ in range(repeats):
                totals.append(run(CALIBRATION_PROMPT, model, effort).total)
                sleep(3)
            after = read()
            matrix[model][effort] = round(median(totals))
            matrix["_meta"]["usage_deltas"][f"{model}/{effort}"] = (after.five_hour or 0) - (before.five_hour or 0)
    matrix["_meta"]["finished"] = datetime.now(timezone.utc).isoformat()
    return matrix


def main(argv: list[str] | None = None) -> int:
    import argparse, time
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
    m = calibrate(a.models, a.efforts, a.repeats,
                  lambda p, mo, ef: run_prompt(p, mo, ef, a.config_dir), lambda: read_usage(cfg), time.sleep)
    a.out.write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in m.items() if not k.startswith("_")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_calibrate -v`
Expected: 1 test PASS

- [ ] **Step 5: Run the real calibration once**

This spends real usage: 45 prompts, expect 10 to 20% of a 5-hour window and a wall clock of 20 to 40 minutes. Run on `gs` on the Jono Work account so masterrig's window is untouched, when that account is idle:

```bash
ssh gs 'cd ~/claude-usage-tracker && CLAUDE_CONFIG_DIR=$HOME/.claude-javiswork python3 -m tracker.calibrate --config-dir $HOME/.claude-javiswork --out data/effort_matrix.json'
```

(The repo is cloned to `gs` in Task 12; if running this before Task 12, clone it first with `gh repo clone jonathanavis96/claude-usage-tracker ~/claude-usage-tracker`.) Expected: a printed matrix where tokens rise with effort for each model. Copy the file back and drop the `_status` placeholder key:

```bash
scp gs:~/claude-usage-tracker/data/effort_matrix.json ~/code/claude-usage-tracker/data/effort_matrix.json
```

- [ ] **Step 6: Commit**

```bash
git add tracker/calibrate.py tests/test_calibrate.py data/effort_matrix.json
git commit -m "Effort calibration run and matrix"
git push
```

---

### Task 12: Deploy the probe and the daily publisher to gs

**Files:**
- Create: `bin/probe.sh`, `bin/daily.sh`, `docs/deploy-gs.md`

- [ ] **Step 1: Clone on gs and confirm the CLI is on the cron PATH**

```bash
ssh gs 'gh repo clone jonathanavis96/claude-usage-tracker ~/claude-usage-tracker 2>/dev/null || git -C ~/claude-usage-tracker pull -q; which claude; python3 --version; ls ~/.claude-dave/.credentials.json ~/.claude-javiswork/.credentials.json'
```

Expected: both credential files exist, `claude` path printed. Note the `claude` path for the cron wrapper, cron has a minimal PATH.

- [ ] **Step 2: Write the probe wrapper**

```bash
# bin/probe.sh
#!/usr/bin/env bash
# Twice daily on gs: one tick probe, model rotated by slot. Appends to probes.jsonl and pushes.
set -uo pipefail
export PATH="$HOME/.local/bin:$HOME/.nvm/versions/node/current/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.."
git pull -q --rebase origin main || true
MODELS=(claude-sonnet-5 claude-opus-5 claude-fable-5-1)
SLOT=$(( ( $(date -u +%j) * 2 + ( $(date -u +%H) >= 12 ) ) % 3 ))
MODEL="${MODELS[$SLOT]}"
python3 -m tracker.probe --model "$MODEL" --effort low --out probes.jsonl
rc=$?
if [ $rc -eq 0 ]; then
  git add probes.jsonl
  git -c user.name=probe -c user.email=probe@gs commit -q -m "Probe $(date -u +%FT%H:%MZ) $MODEL" && git push -q origin main
fi
exit $rc
```

Replace the node path segment with the real directory containing `claude` from Step 1.

- [ ] **Step 3: Write the daily publisher wrapper**

```bash
# bin/daily.sh
# Daily on gs: merge probes + passive, publish JSON into the alldonesites clone, push.
#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.."
git pull -q --rebase origin main
SITE="$HOME/all-done-sites-platform"
[ -d "$SITE" ] || git clone -q git@github.com:jonathanavis96/all-done-sites-platform.git "$SITE"
git -C "$SITE" pull -q --rebase origin main
python3 -m tracker.publish --probes probes.jsonl --passive history/passive.json --out "$SITE/website/public/data/claude-usage.json"
cd "$SITE"
git add website/public/data/claude-usage.json
git -c user.name="Jonathan Avis" -c user.email="jonathanavis96@gmail.com" commit -q -m "data: refresh claude usage" || { echo "no change"; exit 0; }
git push -q origin main
```

- [ ] **Step 4: Give gs push access to the site repo with a deploy key**

On gs, generate a key used only for this and add it as a write deploy key. This is the only credential gs holds for the public repo.

```bash
ssh gs 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/alldonesites_deploy -C claude-usage-tracker@gs <<< y >/dev/null; cat ~/.ssh/alldonesites_deploy.pub'
```

Then from masterrig, where `gh` is logged in as the repo owner:

```bash
gh repo deploy-key add /dev/stdin --repo jonathanavis96/all-done-sites-platform --title "gs claude-usage-tracker" --allow-write <<< "$(ssh gs cat ~/.ssh/alldonesites_deploy.pub)"
```

And on gs, pin that key to the host in `~/.ssh/config`:

```
Host github.com-alldonesites
  HostName github.com
  IdentityFile ~/.ssh/alldonesites_deploy
  IdentitiesOnly yes
```

Change the clone URL in `bin/daily.sh` to `git@github.com-alldonesites:jonathanavis96/all-done-sites-platform.git`.

- [ ] **Step 5: Install cron on gs and run each once by hand**

```bash
ssh gs 'chmod +x ~/claude-usage-tracker/bin/*.sh; (crontab -l 2>/dev/null; echo "5 4,16 * * * \$HOME/claude-usage-tracker/bin/probe.sh >> \$HOME/claude-usage-tracker/probe.log 2>&1"; echo "30 5 * * * \$HOME/claude-usage-tracker/bin/daily.sh >> \$HOME/claude-usage-tracker/daily.log 2>&1") | crontab -'
ssh gs '~/claude-usage-tracker/bin/probe.sh'
ssh gs '~/claude-usage-tracker/bin/daily.sh'
```

Expected: the probe prints one result line and pushes a row; the daily job writes the JSON and pushes a `data: refresh claude usage` commit to the site repo. Confirm with `git -C ~/code/alldonesites pull && git log -1 --format=%s` showing that message.

Times are UTC on gs; 04:05 and 16:05 UTC are 06:05 and 18:05 SAST, chosen for low chance of the Greenscape accounts being in use. Adjust if the spike found otherwise.

- [ ] **Step 6: Write the deploy note and commit**

`docs/deploy-gs.md`: where the clone lives on gs, the two cron lines, the deploy key name, the log files, and how to re-run by hand. Then:

```bash
git add bin/probe.sh bin/daily.sh docs/deploy-gs.md
git commit -m "Deploy probe and daily publisher to gs"
git push
```

---

### Task 13: Page arithmetic module in alldonesites

**Files:**
- Create: `website/src/lib/claudeUsage.ts`, `website/src/lib/claudeUsage.test.ts`

The site has no test runner. Add the smallest one: `vitest` as a dev dependency with a `"test": "vitest run"` script. It stays scoped to this file.

**Interfaces:**
- Produces: `type UsageJson`, `type Plan = "pro"|"max5"|"max20"`, `type Effort = "low"|"medium"|"high"|"xhigh"|"max"`; `MODEL_LABELS`; `compute(json, plan, model, effort) -> { tokensPerWindow, split: Record<class, number>, tasksPerWindow, tasksPerWeek, apiValueUsd }`; `headline(json) -> { text: string, tone: "up"|"down"|"flat" }`; `fmtTokens(n) -> "42M" | "2.6M" | "900k"`; `seriesFor(json, plan, model) -> {date, value, interpolated}[]`.

- [ ] **Step 1: Add vitest**

```bash
cd ~/code/alldonesites/website && npm i -D vitest@^3 && node -e "const p=require('./package.json');p.scripts.test='vitest run';require('fs').writeFileSync('package.json',JSON.stringify(p,null,2)+'\n')"
```

- [ ] **Step 2: Write the failing test**

```ts
// website/src/lib/claudeUsage.test.ts
import { describe, it, expect } from "vitest";
import { compute, headline, fmtTokens, seriesFor, type UsageJson } from "./claudeUsage";

const J: UsageJson = {
  generated_at: "2026-09-05T20:15:00+00:00",
  last_sample_at: "2026-09-05T08:00:00+00:00",
  plan_measured: "max20",
  plan_ratios: { pro: 0.05, max5: 0.25, max20: 1 },
  rates: { "claude-sonnet-5": { tokens_per_window: 42_000_000, source: "probe", probe_effort: "low",
           split: { input: 0.062, output: 0.021, cache_read: 0.907, cache_write: 0.01 } } },
  effort: { "claude-sonnet-5": { low: 900_000, medium: 1_400_000, high: 2_520_000, xhigh: 3_900_000, max: 5_600_000 } },
  api_price_per_mtok: { "claude-sonnet-5": { input: 3, output: 15, cache_read: 0.3, cache_write: 3.75 } },
  history: { "claude-sonnet-5": [
    { date: "2026-08-01", tokens_per_window: 40_000_000, source: "passive", interpolated: false },
    { date: "2026-09-05", tokens_per_window: 42_000_000, source: "probe", interpolated: false } ] },
  last_change: { date: "2026-09-02", direction: "decreased", percent: 14, model: "claude-sonnet-5" },
};

describe("compute", () => {
  it("scales by plan and derives tasks and value", () => {
    const r = compute(J, "max20", "claude-sonnet-5", "high");
    expect(r.tokensPerWindow).toBe(42_000_000);
    expect(r.split.cache_read).toBeCloseTo(38_094_000, -3);
    expect(r.tasksPerWindow).toBeCloseTo(16.67, 1);
    expect(r.tasksPerWeek).toBeCloseTo(466.7, 0);
    // value = 2.604M*3 + 0.882M*15 + 38.094M*0.3 + 0.42M*3.75  (per Mtok)
    expect(r.apiValueUsd).toBeCloseTo(7.812 + 13.23 + 11.428 + 1.575, 1);
  });
  it("pro is 5% of max20", () => {
    expect(compute(J, "pro", "claude-sonnet-5", "low").tokensPerWindow).toBe(2_100_000);
  });
});

describe("headline", () => {
  it("states the last change", () => {
    expect(headline(J)).toEqual({ text: "Anthropic last decreased Claude's limits by 14% on 2 Sep 2026.", tone: "down" });
  });
  it("states no change when none", () => {
    const h = headline({ ...J, last_change: null });
    expect(h.tone).toBe("flat");
    expect(h.text).toBe("Anthropic hasn't changed Claude's limits since 1 Aug 2026.");
  });
});

describe("fmtTokens", () => {
  it("formats", () => {
    expect(fmtTokens(42_000_000)).toBe("42M");
    expect(fmtTokens(2_604_000)).toBe("2.6M");
    expect(fmtTokens(420_000)).toBe("420k");
  });
});

describe("seriesFor", () => {
  it("scales history by plan", () => {
    const s = seriesFor(J, "max5", "claude-sonnet-5");
    expect(s[1]).toEqual({ date: "2026-09-05", value: 10_500_000, interpolated: false });
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ~/code/alldonesites/website && npx vitest run src/lib/claudeUsage.test.ts`
Expected: FAIL, cannot resolve `./claudeUsage`

- [ ] **Step 4: Write minimal implementation**

```ts
// website/src/lib/claudeUsage.ts
export type Plan = "pro" | "max5" | "max20";
export type Effort = "low" | "medium" | "high" | "xhigh" | "max";
export type TokenClass = "input" | "output" | "cache_read" | "cache_write";

export interface UsageJson {
  generated_at: string;
  last_sample_at: string | null;
  plan_measured: Plan;
  plan_ratios: Record<Plan, number>;
  rates: Record<string, { tokens_per_window: number; source: string; probe_effort: string; split: Record<TokenClass, number> }>;
  effort: Record<string, Record<Effort, number>>;
  api_price_per_mtok: Record<string, Record<TokenClass, number>>;
  history: Record<string, { date: string; tokens_per_window: number; source: string; interpolated: boolean }[]>;
  last_change: { date: string; direction: "increased" | "decreased"; percent: number; model: string } | null;
}

export const MODEL_LABELS: Record<string, string> = {
  "claude-sonnet-5": "Sonnet 5",
  "claude-opus-5": "Opus 5",
  "claude-fable-5-1": "Fable 5.1",
};
export const PLAN_LABELS: Record<Plan, string> = { pro: "Pro", max5: "Max 5x", max20: "Max 20x" };
export const EFFORTS: Effort[] = ["low", "medium", "high", "xhigh", "max"];
export const CLASSES: TokenClass[] = ["input", "output", "cache_read", "cache_write"];
const WINDOWS_PER_WEEK = 28;

export function compute(j: UsageJson, plan: Plan, model: string, effort: Effort) {
  const rate = j.rates[model];
  const tokensPerWindow = rate.tokens_per_window * j.plan_ratios[plan];
  const split = Object.fromEntries(CLASSES.map((c) => [c, tokensPerWindow * (rate.split[c] ?? 0)])) as Record<TokenClass, number>;
  const perTask = j.effort[model]?.[effort] ?? NaN;
  const tasksPerWindow = tokensPerWindow / perTask;
  const prices = j.api_price_per_mtok[model];
  const apiValueUsd = CLASSES.reduce((s, c) => s + (split[c] / 1e6) * (prices?.[c] ?? 0), 0);
  return { tokensPerWindow, split, tasksPerWindow, tasksPerWeek: tasksPerWindow * WINDOWS_PER_WEEK, apiValueUsd };
}

export function fmtDate(iso: string): string {
  const d = new Date(iso + (iso.length === 10 ? "T00:00:00Z" : ""));
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function headline(j: UsageJson): { text: string; tone: "up" | "down" | "flat" } {
  const c = j.last_change;
  if (!c) {
    const first = Object.values(j.history).flat().map((h) => h.date).sort()[0];
    return { text: `Anthropic hasn't changed Claude's limits since ${fmtDate(first)}.`, tone: "flat" };
  }
  return { text: `Anthropic last ${c.direction} Claude's limits by ${c.percent}% on ${fmtDate(c.date)}.`, tone: c.direction === "increased" ? "up" : "down" };
}

export function fmtTokens(n: number): string {
  if (n >= 1e6) { const m = n / 1e6; return (m >= 10 ? m.toFixed(0) : m.toFixed(1).replace(/\.0$/, "")) + "M"; }
  if (n >= 1e3) return Math.round(n / 1e3) + "k";
  return String(Math.round(n));
}

export function seriesFor(j: UsageJson, plan: Plan, model: string) {
  const ratio = j.plan_ratios[plan];
  return (j.history[model] ?? []).map((h) => ({ date: h.date, value: h.tokens_per_window * ratio, interpolated: h.interpolated }));
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `npx vitest run src/lib/claudeUsage.test.ts`
Expected: 6 tests PASS. Also `npx tsc --noEmit -p tsconfig.app.json` reports no new errors.

- [ ] **Step 6: Commit (no AI trailer, public repo)**

```bash
cd ~/code/alldonesites && git checkout -b claude-usage-tracker
git add website/package.json website/package-lock.json website/src/lib/claudeUsage.ts website/src/lib/claudeUsage.test.ts
git commit -m "Add Claude usage arithmetic module with tests"
```

---

### Task 14: The page component, route and styles

**Files:**
- Create: `website/src/pages/ClaudeUsageTracker.tsx`, `website/src/styles/claude-usage.css`, `website/public/data/claude-usage.json` (a fixture copy from Task 12's first publish, or the test JSON from Task 13 if the daily job has not run yet)
- Modify: `website/src/App.tsx` (add route beside the `/guides` routes at lines 53-55), `website/src/entry-server.tsx:46-50` (add `"/claude-usage-tracker"` to `prerenderRoutes`)

**Interfaces:**
- Consumes: everything from `lib/claudeUsage.ts`; `PageShell` from `components/redesign/RedesignChrome` (props `eyebrow`, `title`, `sub`, `children`); `Seo` from `components/Seo` (props `title`, `description`, `canonical`, `image`, `jsonLd`); design in `~/code/claude-usage-tracker/docs/mockup.html`.

- [ ] **Step 1: Styles**

Translate `docs/mockup.html` styles into `website/src/styles/claude-usage.css`, scoped under `.cut` and using the `--ads-*` tokens from `home.css`:

```css
/* website/src/styles/claude-usage.css — Claude usage tracker page. Tokens from home.css. */
.cut { --claude: #D97757; --red: #B42318; --green: #059669; }
.cut .hero { text-align: center; padding: 64px 0 56px; }
.cut .lockup { display: inline-flex; align-items: center; gap: 9px; font-family: var(--ads-disp); font-weight: 600; font-size: 15px; padding: 7px 14px 7px 10px; border: 1px solid var(--ads-line); border-radius: 999px; background: var(--ads-surface); margin-bottom: 22px; }
.cut .lockup svg { width: 20px; height: 20px; fill: var(--claude); }
.cut .lockup b { color: var(--claude); }
.cut .lockup small { color: var(--ads-mut); font-family: var(--ads-body); font-weight: 500; font-size: 13px; }
.cut h1 { font-family: var(--ads-disp); font-size: 36px; line-height: 1.2; letter-spacing: -.02em; margin: 0 0 16px; }
.cut h1 .down { color: var(--red); } .cut h1 .up { color: var(--green); }
.cut .pill { display: inline-flex; align-items: center; gap: 8px; background: rgba(5,150,105,.10); color: #047857; font-size: 13px; font-weight: 600; padding: 6px 14px; border-radius: 999px; margin-bottom: 48px; }
.cut .pill i { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
.cut .sentence { font-size: 18px; margin-bottom: 22px; }
.cut .sel { position: relative; display: inline-block; }
.cut .sel select { appearance: none; -webkit-appearance: none; background: none; border: 0; border-bottom: 1.5px solid rgba(14,165,233,.5); color: var(--ads-acd); font: inherit; font-weight: 600; padding: 0 16px 1px 0; cursor: pointer; }
.cut .sel::after { content: ""; position: absolute; right: 3px; top: 40%; width: 7px; height: 7px; border-right: 1.5px solid var(--ads-acd); border-bottom: 1.5px solid var(--ads-acd); transform: rotate(45deg); pointer-events: none; }
.cut .big { font-family: var(--ads-disp); font-size: 72px; font-weight: 800; letter-spacing: -.04em; line-height: 1; color: var(--ads-ac); }
.cut .big span { font-size: 30px; font-weight: 600; letter-spacing: -.02em; color: var(--ads-tx); margin-left: 10px; }
.cut .split { font-family: var(--ads-mono); font-size: 14px; margin-top: 26px; }
.cut .split b { font-weight: 500; } .cut .split em, .cut .quiet em { font-style: normal; color: var(--ads-mut); margin: 0 8px; }
.cut .quiet { font-size: 15px; color: var(--ads-mut); margin-top: 12px; }
.cut section { padding: 48px 0; border-top: 1px solid var(--ads-line); }
.cut h2 { font-family: var(--ads-disp); font-size: 20px; margin: 0 0 6px; }
.cut .sub { font-size: 14px; color: var(--ads-mut); margin-bottom: 24px; }
.cut svg.chart text { font-family: var(--ads-body); font-size: 11px; fill: var(--ads-mut); }
.cut table { width: 100%; border-collapse: collapse; font-size: 14px; }
.cut th, .cut td { text-align: left; padding: 14px 12px; border-bottom: 1px solid var(--ads-line); }
.cut th { color: var(--ads-mut); font-weight: 500; font-size: 13px; }
.cut th:not(:first-child), .cut td:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
.cut .hl { color: var(--ads-acd); font-weight: 600; }
.cut details { border-top: 1px solid var(--ads-line); }
.cut summary { padding: 18px 0; font-weight: 500; font-size: 15px; cursor: pointer; list-style: none; display: flex; justify-content: space-between; }
.cut summary::after { content: "expand ⌄"; color: var(--ads-mut); font-weight: 400; font-size: 13px; }
.cut details[open] summary::after { content: "collapse ⌃"; }
.cut details p { color: var(--ads-bd); font-size: 14px; line-height: 1.6; margin: 0 0 14px; }
.cut .stale { color: var(--red); font-size: 13px; margin-top: 8px; }
@media (max-width: 640px) {
  .cut .hero { padding: 40px 0 36px; } .cut h1 { font-size: 24px; }
  .cut .pill { flex-direction: column; gap: 2px; padding: 9px 16px; margin-bottom: 32px; } .cut .pill i, .cut .pill .dot { display: none; }
  .cut .sentence { font-size: 16px; line-height: 1.7; }
  .cut .big { font-size: 52px; } .cut .big span { display: block; margin: 10px 0 0; font-size: 22px; }
  .cut .split { font-size: 12px; line-height: 1.8; } .cut .split span, .cut .quiet span { display: block; } .cut .brk { display: none; }
  .cut th, .cut td { padding: 12px 6px; font-size: 13px; }
}
```

- [ ] **Step 2: The component**

```tsx
// website/src/pages/ClaudeUsageTracker.tsx
import { useEffect, useMemo, useState } from "react";
import Seo from "@/components/Seo";
import { PageShell } from "@/components/redesign/RedesignChrome";
import { CLASSES, EFFORTS, MODEL_LABELS, PLAN_LABELS, compute, fmtDate, fmtTokens, headline, seriesFor,
         type Effort, type Plan, type UsageJson } from "@/lib/claudeUsage";
import "@/styles/home.css";
import "@/styles/claude-usage.css";

const SITE = "https://alldonesites.com";
const CLASS_LABEL = { input: "input", output: "output", cache_read: "cache read", cache_write: "cache write" } as const;

function Chart({ points, change }: { points: { date: string; value: number; interpolated: boolean }[]; change: { date: string; direction: string; percent: number } | null }) {
  if (points.length < 2) return <p className="sub">Not enough history yet.</p>;
  const W = 840, H = 260, L = 44, R = 820, T = 20, B = 200;
  const vals = points.map((p) => p.value);
  const lo = Math.min(...vals) * 0.9, hi = Math.max(...vals) * 1.05;
  const x = (i: number) => L + (i / (points.length - 1)) * (R - L);
  const y = (v: number) => B - ((v - lo) / (hi - lo)) * (B - T);
  const path = points.map((p, i) => `${x(i)},${y(p.value)}`).join(" ");
  const ticks = [0, 1, 2, 3].map((k) => lo + ((hi - lo) * k) / 3);
  const ci = change ? points.findIndex((p) => p.date >= change.date) : -1;
  const labelEvery = Math.max(1, Math.floor(points.length / 5));
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Effective window size over time">
      <defs><linearGradient id="cutfill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor="#0EA5E9" stopOpacity=".28" /><stop offset="1" stopColor="#0EA5E9" stopOpacity=".02" /></linearGradient></defs>
      {ticks.map((t) => (<g key={t}><line x1={L} x2={R} y1={y(t)} y2={y(t)} stroke="#E6E9EE" /><text x={0} y={y(t) + 4}>{fmtTokens(t)}</text></g>))}
      <polygon fill="url(#cutfill)" points={`${L},${B} ${path} ${R},${B}`} />
      <polyline fill="none" stroke="#0EA5E9" strokeWidth="2.5" strokeLinejoin="round" points={path} />
      {points.map((p, i) => p.interpolated && <circle key={p.date} cx={x(i)} cy={y(p.value)} r="3" fill="#fff" stroke="#0EA5E9" />)}
      {ci >= 0 && (<g><line x1={x(ci)} x2={x(ci)} y1={T} y2={B} stroke="#B42318" strokeWidth="1.5" strokeDasharray="5 4" />
        <rect x={Math.min(x(ci) + 7, R - 120)} y={T + 4} width="112" height="22" rx="6" fill="#B42318" />
        <text x={Math.min(x(ci) + 15, R - 112)} y={T + 19} style={{ fill: "#fff", fontWeight: 600 }}>{fmtDate(change!.date).slice(0, 6)} · {change!.direction === "decreased" ? "down" : "up"} {change!.percent}%</text></g>)}
      <circle cx={R} cy={y(points[points.length - 1].value)} r="4.5" fill="#0EA5E9" stroke="#fff" strokeWidth="2" />
      <g style={{ fill: "#0277B5", fontWeight: 500 }}>{points.map((p, i) => i % labelEvery === 0 && <text key={p.date} x={x(i)} y={B + 36}>{fmtDate(p.date).slice(0, 6)}</text>)}</g>
    </svg>
  );
}

export default function ClaudeUsageTracker() {
  const [data, setData] = useState<UsageJson | null>(null);
  const [failed, setFailed] = useState(false);
  const [plan, setPlan] = useState<Plan>("max20");
  const [model, setModel] = useState("claude-sonnet-5");
  const [effort, setEffort] = useState<Effort>("high");

  useEffect(() => {
    fetch("/data/claude-usage.json").then((r) => (r.ok ? r.json() : Promise.reject())).then(setData).catch(() => setFailed(true));
  }, []);

  const r = useMemo(() => (data && data.rates[model] ? compute(data, plan, model, effort) : null), [data, plan, model, effort]);
  const h = data ? headline(data) : null;
  const stale = data ? Date.now() - new Date(data.generated_at).getTime() > 3 * 86400e3 : false;
  const localTime = data?.last_sample_at ? new Date(data.last_sample_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;

  return (
    <PageShell>
      <Seo title="Claude Usage Tracker: what a Max plan actually buys | All Done Sites"
           description="Measured daily from a real account: how many tokens a Claude Max 20x plan buys per 5-hour window, and whether Anthropic has changed the limit."
           canonical={`${SITE}/claude-usage-tracker/`} image={`${SITE}/og1200x630_v2.jpg`} />
      <div className="cut">
        <div className="hero">
          <div className="lockup"><svg viewBox="0 0 24 24"><path d="M12 1.5l1.6 6.2 4.6-4.4-3 5.6 6.3-.4-5.8 2.5 5.8 2.5-6.3-.4 3 5.6-4.6-4.4L12 22.5l-1.6-6.2-4.6 4.4 3-5.6-6.3.4 5.8-2.5-5.8-2.5 6.3.4-3-5.6 4.6 4.4z" /></svg><b>Claude</b> <small>usage tracker</small></div>
          {failed && <h1>Data temporarily unavailable.</h1>}
          {h && <h1 dangerouslySetInnerHTML={{ __html: h.text.replace(/(increased|decreased)/, `<span class="${h.tone}">$1</span>`).replace(/(\d+%)/, `<span class="${h.tone}">$1</span>`) }} />}
          {data && (<div className="pill"><i /><span>Measured daily from a real account</span><em className="dot">·</em><span>last sample {localTime} local</span></div>)}
          {data && r && (<>
            <div className="sentence">On <span className="sel"><select value={plan} onChange={(e) => setPlan(e.target.value as Plan)}>{(Object.keys(PLAN_LABELS) as Plan[]).map((p) => <option key={p} value={p}>{PLAN_LABELS[p]}</option>)}</select></span>, running <span className="sel"><select value={model} onChange={(e) => setModel(e.target.value)}>{Object.keys(data.rates).map((m) => <option key={m} value={m}>{MODEL_LABELS[m] ?? m}</option>)}</select></span> at <span className="sel"><select value={effort} onChange={(e) => setEffort(e.target.value as Effort)}>{EFFORTS.map((e) => <option key={e} value={e}>{e}</option>)}</select></span> effort, you get</div>
            <div className="big">{fmtTokens(r.tokensPerWindow)}<span>tokens per 5-hour window</span></div>
            <div className="split"><span><b>{fmtTokens(r.split.input)}</b> input<em>·</em><b>{fmtTokens(r.split.output)}</b> output</span><em className="brk">·</em><span><b>{fmtTokens(r.split.cache_read)}</b> cache read<em>·</em><b>{fmtTokens(r.split.cache_write)}</b> cache write</span></div>
            <div className="quiet"><span>about {Math.round(r.tasksPerWindow)} tasks<em>·</em>{Math.round(r.tasksPerWeek)} per week</span><em className="brk">·</em><span>roughly ${Math.round(r.apiValueUsd)} of API value</span></div>
            {stale && <div className="stale">Last updated {fmtDate(data.generated_at.slice(0, 10))}. The daily job has not run since.</div>}
          </>)}
        </div>
        {data && (<section><h2>Effective window size, last 90 days</h2><div className="sub">{PLAN_LABELS[plan]} · {MODEL_LABELS[model] ?? model} tokens per 5-hour window</div><Chart points={seriesFor(data, plan, model)} change={data.last_change} /></section>)}
        {data && r && (<section><h2>Plan comparison</h2><div className="sub">{MODEL_LABELS[model] ?? model} at {effort} effort. Max 20x is measured; Max 5x uses the ratio observed at the August plan change; Pro is scaled 1:5 from Max 5x.</div>
          <table><thead><tr><th></th>{(Object.keys(PLAN_LABELS) as Plan[]).map((p) => <th key={p} className={p === plan ? "hl" : ""}>{PLAN_LABELS[p]}</th>)}</tr></thead><tbody>
            {[["Tokens per 5-hour window", (c: ReturnType<typeof compute>) => fmtTokens(c.tokensPerWindow)],
              ["Tasks per 5-hour window", (c: ReturnType<typeof compute>) => c.tasksPerWindow < 1 ? "< 1" : String(Math.round(c.tasksPerWindow))],
              ["Tasks per week", (c: ReturnType<typeof compute>) => String(Math.round(c.tasksPerWeek))],
              ["API value per week", (c: ReturnType<typeof compute>) => "~$" + Math.round(c.apiValueUsd * 28)]].map(([label, f]) => (
              <tr key={label as string}><td>{label as string}</td>{(Object.keys(PLAN_LABELS) as Plan[]).map((p) => <td key={p} className={p === plan ? "hl" : ""}>{(f as (c: ReturnType<typeof compute>) => string)(compute(data, p, model, effort))}</td>)}</tr>))}
          </tbody></table>
          <div style={{ height: 32 }} />
          <details><summary>How we measure this</summary>
            <p>A small fixed prompt is run repeatedly on an otherwise idle Max 20x account until Anthropic's own usage meter ticks from one whole percent to the next, twice. The tokens spent between the two ticks are one percent of the 5-hour window. That runs twice a day, rotating across Sonnet 5, Opus 5 and Fable 5.1.</p>
            <p>The split between input, output and cache tokens comes from real working sessions on a second Max 20x account, joined against the same usage meter. Effort figures come from one calibration task run at every effort level on every model.</p></details>
          <details><summary>Caveats</summary>
            <p>Two accounts, one workload mix. The meter reports whole percent, so each probe carries about one small prompt of rounding error. Effort figures describe one task shape; yours will differ. Pro and Max 5x are scaled, not measured, from the day of the plan change.</p></details>
        </section>)}
        <p className="sub" style={{ textAlign: "center", padding: "24px 0 8px" }}>All Done Sites measures before it claims. Want a site that does the same? <a href="/#contact">Get in touch</a></p>
      </div>
    </PageShell>
  );
}
```

- [ ] **Step 3: Route and prerender**

In `website/src/App.tsx` next to the guides routes:

```tsx
import ClaudeUsageTracker from "./pages/ClaudeUsageTracker";
// ...
<Route path="/claude-usage-tracker" element={<ClaudeUsageTracker />} />
```

In `website/src/entry-server.tsx` `prerenderRoutes`, add `"/claude-usage-tracker",` as the first entry. Prerendering renders the fetch-less state (the lockup and "Data temporarily unavailable" never shows because `failed` starts false; the hero renders only the lockup), which is fine: the client hydrates and fetches.

- [ ] **Step 4: Fixture data and local check**

Copy the first real JSON if Task 12 has run, otherwise write the test JSON from Task 13 to `website/public/data/claude-usage.json`. Then:

```bash
cd ~/code/alldonesites/website && npx tsc --noEmit -p tsconfig.app.json && npm run lint && npm run build
```

Expected: no type errors, lint clean, build finishes and `dist/claude-usage-tracker/index.html` exists. Then `npm run preview` and open `http://localhost:4173/claude-usage-tracker` in a browser; compare against `docs/mockup.html`. Check the three dropdowns recalculate every figure, the chart y axis fits the data, and the mobile width (390px in devtools) matches the mockup's stacking. Close the browser when done.

- [ ] **Step 5: Commit**

```bash
cd ~/code/alldonesites
git add website/src/pages/ClaudeUsageTracker.tsx website/src/styles/claude-usage.css website/src/App.tsx website/src/entry-server.tsx website/public/data/claude-usage.json
git commit -m "Add Claude usage tracker page"
```

---

### Task 15: Ship the page and confirm the daily loop end to end

**Files:**
- None new. This is the release check.

- [ ] **Step 1: Ship the alldonesites branch**

Invoke the `ship-to-main` skill for `~/code/alldonesites` on branch `claude-usage-tracker`. It handles the PR, the review gate and the merge. Do not merge by hand.

- [ ] **Step 2: Confirm the deploy**

After the Pages deploy finishes:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://alldonesites.com/claude-usage-tracker/
curl -s https://alldonesites.com/data/claude-usage.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['generated_at'], list(d['rates']))"
```

Expected: 200 and the JSON's timestamp and model list. Read-only checks only.

- [ ] **Step 3: Confirm one unattended daily cycle**

The next morning after the 05:30 UTC job on gs: `git -C ~/code/alldonesites pull` and check `git log -1 --format="%s %ci"` shows `data: refresh claude usage` from that morning, and the live JSON's `generated_at` moved. If the probe log on gs shows `probe skipped` for both slots, the accounts were busy; note it and adjust the cron times.

- [ ] **Step 4: Record the milestone**

```bash
worklog add -p "Claude usage tracker" -c personal "Public usage tracker live" "alldonesites.com/claude-usage-tracker fed by a tick probe on gs twice daily"
```

Update the vault per `~/notes/vault/_Agent_System/VAULT_AGENT_RULES.md`: a project hub note for the tracker with current state and links to the spec, tasks note with anything left open, changelog entry for the launch.

---

## Self-review

**Spec coverage:** Fixed probe with tick method, idle guard and account fallback: Task 6, deployed Task 12. Passive join with both logs, reset handling, pooling, attribution, interpolation: Tasks 2, 3, 4, 10. Effort calibration: Task 11. Plan ratios observed at 18 Aug: Task 10 `passive_summary`, consumed in Task 9. Change detection on probe data, per model, 5%, 2-day persistence: Task 8, wired in Task 9. Public JSON shape: Task 9. Page with three dropdowns, headline template, relative chart, local time, collapsed method and caveats, stale notice, fetch failure state: Task 14. Publishing via daily commit with fixed message, no trailers: Task 12. Tests without network: every task. Spike as acceptance: Task 7.

**Gaps found and fixed inline:** the spec's history is per model; `build_public_json` now emits `history` keyed by model and the page reads `history[model]`. The spec says the daily figure is a 3-day trailing median of probes; `probe_daily_series` does that. The spec's "abort if a tick arrives faster than its own prompts can explain" is the `jump >= 2` rule plus the sub-largest-prompt rule in `run_tick_probe`.

**Type consistency:** `Utilization` fields (`five_hour`, `five_hour_resets_at`) match between Tasks 1, 4's `Sample` (own type, `resets_at`), and 6. `RunUsage.total` and `Turn.total` both sum the four classes. `ProbeResult` rows written by `append_result` carry exactly the keys `probe_daily_series` and the Task 9 test fixture read (`ts`, `model`, `effort`, `tokens_per_pct`, `tokens`, `account`). `DailyRate` fields used in `passive_summary` (`tokens_per_pct`, `interpolated`, `split`) exist in Task 4. The TS `UsageJson` mirrors the Task 9 output including `history` keyed by model and `last_change.model`.
