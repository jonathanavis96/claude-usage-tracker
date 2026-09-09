"""Contributed meter samples: fetch, keep, and aggregate into the `contributed` block.

Readers post their own meter through `contrib/sample.py` (see
docs/superpowers/specs/2026-09-06-contributed-meter-samples.md). The site
stores each validated body plus `received_at` and hands them all back from
`GET /api/contribute/export` as JSON lines behind the same bearer secret the
notify send endpoint uses. The daily job (bin/daily.sh) runs this module
before tracker.publish:

  fetch      every stored sample, following the export cursor
  merge      append the new ones to history/contributed.jsonl, never rewriting
             or dropping a line already there
  aggregate  the per-plan `contributed` block, written to data/contributed.json
             and carried into the public JSON by tracker.publish --contributed

Aggregation rules, per plan (pro, max5, max20):

  contributors, samples   distinct contributor ids and rows on that plan.
  tokens_per_pct[model]   median across contributors (each contributor first
                          reduced to the median of their own samples) of that
                          model's tokens since the five-hour reset over the
                          five-hour utilization, using only samples whose
                          utilization is at least MIN_UTILIZATION (5). Raw
                          counts, the same rule the personal page draws.
  usd_per_pct[model]      same, but the tokens valued in meter dollars exactly
                          as tracker/publish.py values a probe row (list price
                          x class_weight per class, x the model's meter_weight):
                          the cross-check on the probe's dollar invariant.
  spread                  on both: interquartile range across contributors over
                          the median, null with fewer than two contributors.
  weekly_windows          each contributor's consecutive samples inside one
                          five-hour and one seven-day window (tracker/weekly.py's
                          own pairing) give five-hour movement over seven-day
                          movement per week; a contributor's figure is the median
                          of their complete weeks, weighted by how many they have;
                          the plan figure is the weighted median after dropping
                          contributors more than MAX_DEVIATION (30%) from it;
                          `measured` only once two contributors each have one
                          complete week, else null with a `reason`.

A sample's five-hour percent covers every model used in that window, so a
mixed-model sample under-reads each model's own figure; the median across
many contributors is what the page shows, and the probe stays the controlled
reference (nothing here replaces a probe or passive figure).
"""
from __future__ import annotations
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, quantiles
from typing import Callable

from .alert import ENV_FILE, USER_AGENT, read_env_file
from .publish import meter_usd, write_json
from .weekly import parse_row, weekly_windows

EXPORT_URL = "https://alldonesites.com/api/contribute/export"
SECRET_KEY = "NOTIFY_SEND_SECRET"
PLANS = ("pro", "max5", "max20")
CLASSES = ("input", "output", "cache_read", "cache_write")
MIN_UTILIZATION = 5.0
MAX_DEVIATION = 0.30
MIN_CONTRIBUTORS = 2
FETCH_TIMEOUT_S = 20
CURSOR_LINE_KEY = "next_cursor"


# ---------------------------------------------------------------- fetch

def _page_url(endpoint: str, cursor: str | None) -> str:
    if not cursor:
        return endpoint
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}cursor={urllib.parse.quote(cursor, safe='')}"


def fetch(endpoint: str, secret: str, *, urlopen: Callable | None = None) -> list[dict]:
    """Every stored sample from the export endpoint, following `?cursor=` to the end.

    One request per page, FETCH_TIMEOUT_S each, no retries. On any HTTP or
    network error (or an unparseable page) the rows fetched so far are returned
    and one line goes to stderr, so a bad day at the edge costs nothing but
    freshness. The secret is sent as a bearer header and never printed.
    """
    urlopen = urlopen or urllib.request.urlopen
    rows: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        url = _page_url(endpoint, cursor)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret}",
                                                   "Accept": "application/x-ndjson",
                                                   "User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
                header_cursor = (r.headers.get("x-next-cursor") or "").strip() if getattr(r, "headers", None) else ""
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            with e:  # close its body deterministically (tracker/alert.py does the same)
                print(f"warning: contributed export {url} returned HTTP {e.code}; keeping {len(rows)} rows fetched so far",
                      file=sys.stderr)
            return rows
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"warning: contributed export {url} failed: {e}; keeping {len(rows)} rows fetched so far",
                  file=sys.stderr)
            return rows
        line_cursor = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                print(f"warning: contributed export {url} sent an unreadable line; keeping {len(rows)} rows fetched so far",
                      file=sys.stderr)
                return rows
            if not isinstance(d, dict):
                continue
            if set(d) == {CURSOR_LINE_KEY}:
                line_cursor = str(d[CURSOR_LINE_KEY] or "").strip()
                continue
            rows.append(d)
        cursor = header_cursor or line_cursor
        if not cursor or cursor in seen_cursors:
            return rows
        seen_cursors.add(cursor)


# ---------------------------------------------------------------- merge

def _key(row: dict) -> tuple[str, str] | None:
    cid, ts = row.get("contributor_id"), row.get("ts")
    if not isinstance(cid, str) or not isinstance(ts, str) or not cid or not ts:
        return None
    return cid, ts


def load_history(path: Path) -> list[dict]:
    """Every parseable row in the history file; unreadable lines are skipped, never removed."""
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            rows.append(d)
    return rows


def merge(path: Path, rows: list[dict]) -> int:
    """Append rows not already in the file, keyed by (contributor_id, ts). Returns the count added.

    Append-only: an existing line is never rewritten or dropped, even one that
    does not parse. A row with no usable key is ignored.
    """
    p = Path(path)
    existing = {k for k in (_key(r) for r in load_history(p)) if k is not None}
    new_lines = []
    for r in rows:
        k = _key(r)
        if k is None or k in existing:
            continue
        existing.add(k)
        new_lines.append(json.dumps(r, sort_keys=True, separators=(",", ":")))
    if not new_lines:
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = p.exists() and p.stat().st_size > 0 and not p.read_bytes().endswith(b"\n")
    with open(p, "a", encoding="utf-8") as fh:
        if needs_newline:
            fh.write("\n")
        fh.write("".join(line + "\n" for line in new_lines))
    return len(new_lines)


# ---------------------------------------------------------------- aggregate

def _utilization(row: dict) -> float | None:
    u = (row.get("five_hour") or {}).get("utilization")
    try:
        return float(u) if u is not None else None
    except (TypeError, ValueError):
        return None


def _model_tokens(row: dict) -> dict[str, dict]:
    t = row.get("tokens_since_five_hour_reset")
    if not isinstance(t, dict):
        return {}
    out = {}
    for model, counts in t.items():
        if not isinstance(counts, dict):
            continue
        try:
            out[model] = {c: int(counts.get(c) or 0) for c in CLASSES}
        except (TypeError, ValueError):
            continue
    return out


def _spread(values: list[float]) -> float | None:
    """Interquartile range over the median, or None with fewer than two values or a zero median."""
    if len(values) < 2:
        return None
    m = median(values)
    if m == 0:
        return None
    q1, _, q3 = quantiles(values, n=4, method="inclusive")
    return round((q3 - q1) / m, 3)


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Median of values weighted by weight; equal weights reduce to statistics.median."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if acc > total / 2:
            return v
        if acc == total / 2:
            return (v + pairs[i + 1][0]) / 2 if i + 1 < len(pairs) else v
    return pairs[-1][0]


def _per_pct(rows_by_contributor: dict[str, list[dict]], prices: dict) -> tuple[dict, dict]:
    """(tokens_per_pct, usd_per_pct) per model: medians across contributors with the utilization floor."""
    tokens: dict[str, dict[str, list[float]]] = {}
    usd: dict[str, dict[str, list[float]]] = {}
    for cid, rows in rows_by_contributor.items():
        for r in rows:
            u = _utilization(r)
            if u is None or u < MIN_UTILIZATION:
                continue
            for model, counts in _model_tokens(r).items():
                total = sum(counts.values())
                if total <= 0:
                    continue
                tokens.setdefault(model, {}).setdefault(cid, []).append(total / u)
                price = prices.get(model)
                if price is None:
                    continue
                value = meter_usd(counts, price) * price.get("meter_weight", 1.0)
                usd.setdefault(model, {}).setdefault(cid, []).append(value / u)

    def block(per_model: dict, rounder) -> dict:
        out = {}
        for model in sorted(per_model):
            per_contributor = [median(v) for v in per_model[model].values()]
            out[model] = {
                "median": rounder(median(per_contributor)),
                "spread": _spread(per_contributor),
                "contributors": len(per_contributor),
                "samples": sum(len(v) for v in per_model[model].values()),
            }
        return out

    return block(tokens, lambda x: round(x)), block(usd, lambda x: round(x, 4))


def _contributor_weeks(rows: list[dict], now: datetime) -> list[float]:
    """The `windows` figure of each complete week one contributor's samples pair into."""
    ordered = sorted(rows, key=lambda r: r.get("ts") or "")
    parsed = [parse_row(r) for r in ordered]
    result = weekly_windows(parsed, now=now)
    return [h["windows"] for h in result["history"] if not h.get("partial")]


def _weekly(rows_by_contributor: dict[str, list[dict]], now: datetime) -> dict:
    per_contributor: list[tuple[float, float]] = []  # (median windows, weeks)
    for rows in rows_by_contributor.values():
        weeks = _contributor_weeks(rows, now)
        if weeks:
            per_contributor.append((median(weeks), float(len(weeks))))
    out = {"measured": None, "reason": None, "contributors": len(per_contributor),
           "dropped": 0, "weeks": int(sum(w for _, w in per_contributor))}
    if not rows_by_contributor:
        out["reason"] = "no samples"
        return out
    if len(per_contributor) < MIN_CONTRIBUTORS:
        out["reason"] = (f"{len(per_contributor)} contributor{'s' if len(per_contributor) != 1 else ''} with a "
                         f"complete week; {MIN_CONTRIBUTORS} needed")
        return out
    centre = _weighted_median(per_contributor)
    kept = [(v, w) for v, w in per_contributor if abs(v - centre) <= MAX_DEVIATION * centre]
    out["dropped"] = len(per_contributor) - len(kept)
    out["weeks"] = int(sum(w for _, w in kept))
    if len(kept) < MIN_CONTRIBUTORS:
        out["reason"] = f"contributors more than {int(MAX_DEVIATION * 100)}% apart; {MIN_CONTRIBUTORS} agreeing needed"
        return out
    out["measured"] = round(_weighted_median(kept), 2)
    return out


def aggregate(rows: list[dict], now: datetime, prices: dict | None = None) -> dict:
    """The `contributed` block: one entry per plan plus `updated_at`.

    `prices` is data/prices.json's model table (underscore keys already
    dropped); without it `usd_per_pct` is empty. Duplicate (contributor_id, ts)
    rows count once; rows on an unknown plan or without a usable meter are
    ignored.
    """
    prices = prices or {}
    by_plan: dict[str, dict[str, list[dict]]] = {p: {} for p in PLANS}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        k = _key(r)
        plan = r.get("plan")
        if k is None or k in seen or plan not in by_plan or parse_row(r) is None:
            continue
        seen.add(k)
        by_plan[plan].setdefault(k[0], []).append(r)

    out: dict = {"updated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                 "min_utilization": MIN_UTILIZATION, "max_deviation": MAX_DEVIATION,
                 "min_contributors": MIN_CONTRIBUTORS}
    for plan in PLANS:
        contributors = by_plan[plan]
        tokens_per_pct, usd_per_pct = _per_pct(contributors, prices)
        out[plan] = {
            "contributors": len(contributors),
            "samples": sum(len(v) for v in contributors.values()),
            "tokens_per_pct": tokens_per_pct,
            "usd_per_pct": usd_per_pct,
            "weekly_windows": _weekly(contributors, now),
        }
    return out


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None, *, urlopen: Callable | None = None, environ=None,
         now: datetime | None = None) -> int:
    import argparse
    import os
    ap = argparse.ArgumentParser(description="Fetch, keep and aggregate contributed meter samples")
    ap.add_argument("--env-file", type=Path, default=ENV_FILE, help=f"where {SECRET_KEY} lives (never printed)")
    ap.add_argument("--endpoint", default=EXPORT_URL)
    ap.add_argument("--history", type=Path, required=True, help="append-only history/contributed.jsonl")
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--out", type=Path, required=True, help="data/contributed.json")
    ap.add_argument("--offline", action="store_true", help="skip the fetch; aggregate what is on disk")
    a = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    environ = os.environ if environ is None else environ

    fetched = added = 0
    if not a.offline:
        secret = environ.get(SECRET_KEY) or read_env_file(a.env_file).get(SECRET_KEY, "")
        if not secret:
            print(f"warning: no {SECRET_KEY} in {a.env_file} or the environment; aggregating what is on disk",
                  file=sys.stderr)
        else:
            rows = fetch(a.endpoint, secret, urlopen=urlopen)
            fetched = len(rows)
            try:
                added = merge(a.history, rows)
            except OSError as e:
                print(f"contributed failed: could not append to {a.history}: {e}", file=sys.stderr)
                return 1

    try:
        prices = {k: v for k, v in json.loads(a.prices.read_text(encoding="utf-8")).items() if not k.startswith("_")}
        history = load_history(a.history)
        block = aggregate(history, now, prices)
        write_json(a.out, block)
    except (OSError, ValueError, KeyError) as e:
        print(f"contributed failed, previous output left in place: {e}", file=sys.stderr)
        return 1
    plans = ", ".join(f"{p}={block[p]['contributors']}" for p in PLANS)
    print(f"contributed: fetched {fetched}, added {added}, {len(history)} rows on disk, contributors {plans}; wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
