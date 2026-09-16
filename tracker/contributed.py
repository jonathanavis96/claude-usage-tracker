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

Aggregation rules, per plan (pro, max5, max20). Only samples from the last
EVIDENCE_DAYS count (`evidence` says how many were set aside and why), and a
contributor is an unverified source id, not a proven account
(`identity_basis`). Nothing here feeds the tracker's own rates or weekly
figures; the block sits beside them.

Restored 2026-09-17 to the pre-2026-09-16-audit publishing rule, by decision:
every accepted current sample is its own point and its own vote in the
medians below, the same as before that audit's finding 7 required two
same-reset readings ("a span") before a source counted at all. The schema-2
additions the audit made alongside -- `quality`, `evidence`, `identity_basis`,
`missing_reasons`, `evidence_window_days`, model-id normalisation (`_price`),
`cache_write_1h` pricing, and the presence of `capture` metadata on a reading
-- stay; they describe the sample, they no longer gate it.

  contributors, samples   distinct source ids and current rows on that plan.
  usd_per_pct             one combined meter-dollar figure per accepted
                          sample: every model present valued as
                          tracker/publish.py values meter work (list price x
                          class_weight per class, x the model's meter_weight),
                          summed, over the sample's five-hour utilization. A
                          sample with any unpriced model is left out whole. A
                          contributor's figure is the median of their own
                          samples; the published figure is the median across
                          contributors, null with none.
  tokens_per_pct[model]   the model's sample tokens over its own slice of the
                          utilization, the slice being its dollar share of the
                          sample's value (u_m = u x value_m / total). Same
                          sample rules as usd_per_pct. A model with no figure
                          is absent, never given the combined total.
  spread                  on both: interquartile range across contributors
                          over the median, null with fewer than two
                          contributors.
  weekly_windows          each source's own newest paired week (tracker/weekly.py
                          pairing: both meters' movement over the same readings)
                          with its rounding interval, as `estimates`. `measured`
                          stays null: sources are neither pooled, weighted nor
                          dropped as outliers. (Unaffected by the finding-7
                          reversal above -- this was never gated on pairing.)
  points                  one entry per (thinned) accepted sample, for the
                          page's per-contributor chart: {"t", "usd_per_pct",
                          "tokens_per_pct", "tokens_per_pct_week", "c",
                          "coarse"}, plus `tokens_per_pct_by_model` and
                          `tokens_per_pct_week_by_model` when every model
                          present is priced. `tokens_per_pct` is the sample's
                          tokens since the five-hour reset over its five-hour
                          percent and `tokens_per_pct_week` the same for the
                          seven-day window; each is null when its own meter
                          moved less than MIN_UTILIZATION or no token was
                          counted. No windows-per-week quotient of the two is
                          published: it divides two token estimators, not paired
                          meter movement. A model whose slice of the meter is
                          under MIN_UTILIZATION is left out of the by-model
                          maps: a model used almost entirely for cache_read has
                          almost no priced value, and dividing its tokens by
                          that slice runs away. `c` is a stable opaque per-plan
                          pseudonym (a hash), never the source id. `coarse`
                          marks a sample under MIN_UTILIZATION (still included,
                          unlike the medians above, so the chart can grey it
                          out). Thinned to at most one point per source per UTC
                          clock hour (the latest sample in that hour), then to
                          the newest MAX_POINTS, sorted by `t` ascending.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median, quantiles

from .alert import ENV_FILE, USER_AGENT, read_env_file
from .publish import meter_usd, write_json
from .weekly import parse_row, weekly_windows

EXPORT_URL = "https://alldonesites.com/api/contribute/export"
SECRET_KEY = "NOTIFY_SEND_SECRET"
PLANS = ("pro", "max5", "max20")
CLASSES = ("input", "output", "cache_read", "cache_write")
MIN_UTILIZATION = 5.0
POINTS_DAYS = 30
EVIDENCE_DAYS = 30
MAX_POINTS = 2000
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


def _model_tokens(row: dict, field: str = "tokens_since_five_hour_reset") -> dict[str, dict]:
    t = row.get(field)
    if not isinstance(t, dict):
        return {}
    out = {}
    for model, counts in t.items():
        if not isinstance(counts, dict):
            continue
        try:
            out[model] = {c: int(counts.get(c) or 0) for c in CLASSES}
            if counts.get("cache_write_1h") is not None:
                out[model]["cache_write_1h"] = int(counts["cache_write_1h"])
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


def _price(model: str, prices: dict) -> dict | None:
    """The price row a model id is valued at. The old Fable id is priced as Fable 5.1
    (an alias for pricing only: the row keeps the id it was observed under)."""
    if model in prices:
        return prices[model]
    return prices.get("claude-fable-5-1") if model == "claude-fable-5" else None


def _sample_values(r: dict, prices: dict) -> tuple[dict[str, dict], dict[str, float]] | None:
    """This sample's per-model token counts and meter-dollar values, or None to skip it.

    A sample is skipped whenever any model it used has total tokens > 0 but no
    price: the dollar-share attribution below needs every present model's
    price to split the sample's utilization between them. Models with zero
    tokens are dropped first since they carry no attribution weight.
    """
    present = {m: c for m, c in _model_tokens(r).items() if sum(c.values()) > 0}
    if not present:
        return None
    resolved = {m: _price(m, prices) for m in present}
    if any(p is None for p in resolved.values()):
        return None
    values = {}
    for model, counts in present.items():
        price = resolved[model]
        values[model] = meter_usd(counts, price) * price.get("meter_weight", 1.0)
    return present, values


def _per_pct(rows_by_contributor: dict[str, list[dict]], prices: dict) -> tuple[dict, dict | None]:
    """(tokens_per_pct, usd_per_pct): usd_per_pct is one combined figure per sample
    (every priced model's value summed, over the whole-meter utilization);
    tokens_per_pct[model] recovers each model's own figure by giving it the
    slice of the utilization matching its dollar share of that sample."""
    tokens: dict[str, dict[str, list[float]]] = {}
    usd: dict[str, list[float]] = {}
    for cid, rows in rows_by_contributor.items():
        for r in rows:
            u = _utilization(r)
            if u is None or u < MIN_UTILIZATION:
                continue
            priced = _sample_values(r, prices)
            if priced is None:
                continue
            present, values = priced
            total = sum(values.values())
            if total <= 0:
                continue
            usd.setdefault(cid, []).append(total / u)
            for model, value in values.items():
                # The chart's own floor on a model's slice of the meter (_per_pct_both_windows):
                # one precision rule for points and summaries (audit finding 8).
                if value <= 0 or u * value / total < MIN_UTILIZATION:
                    continue
                model_total = sum(present[model].get(c, 0) for c in CLASSES)
                # u_m = u * value_m / total; tokens_per_pct_m = model_total / u_m.
                tokens.setdefault(model, {}).setdefault(cid, []).append(model_total * total / (value * u))

    def block(per_cid: dict, rounder):
        if not per_cid:
            return None
        per_contributor = [median(v) for v in per_cid.values()]
        return {
            "median": rounder(median(per_contributor)),
            "spread": _spread(per_contributor),
            "contributors": len(per_contributor),
            "samples": sum(len(v) for v in per_cid.values()),
        }

    tokens_out = {}
    for model in sorted(tokens):
        b = block(tokens[model], lambda x: round(x))
        if b is not None:
            tokens_out[model] = b

    return tokens_out, block(usd, lambda x: round(x, 4))


def _iso_z(ts) -> str | None:
    """`ts` normalised to "YYYY-MM-DDTHH:MM:SSZ" (UTC, seconds), or None if unparseable."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _per_pct_both_windows(row: dict, prices: dict) -> tuple[dict | None, dict | None]:
    """Per-window tokens per 1%, for the whole sample and for each model in it.

    Each window divides its own token count by its own percent, so neither is polluted
    by traffic the other window did not see. A window returns None when its meter is
    under MIN_UTILIZATION or its token count is zero -- the meters step in whole
    percents, so a low reading makes the quotient swing hard.

    Per model, the sample's utilization is split by each model's dollar share and the
    model's tokens divided by its own slice, the same attribution `_per_pct` uses. A
    model with no price leaves the split unsound, so `by_model` is omitted whole rather
    than published with one model's share silently folded into another's.
    """
    def side(meter: str, field: str) -> dict | None:
        u = (row.get(meter) or {}).get("utilization")
        try:
            u = float(u) if u is not None else None
        except (TypeError, ValueError):
            return None
        if u is None or u < MIN_UTILIZATION:
            return None
        present = {m: c for m, c in _model_tokens(row, field).items() if sum(c.values()) > 0}
        total_tokens = sum(sum(c.get(k, 0) for k in CLASSES) for c in present.values())
        if total_tokens <= 0:
            return None
        out: dict = {"all": total_tokens / u}
        resolved = {m: _price(m, prices) for m in present}
        if present and all(p is not None for p in resolved.values()):
            values = {m: meter_usd(c, resolved[m]) * resolved[m].get("meter_weight", 1.0)
                      for m, c in present.items()}
            total_value = sum(values.values())
            if total_value > 0:
                by_model = {}
                for model, value in values.items():
                    if value <= 0:
                        continue
                    # The share of the meter this model is judged to have moved.
                    u_m = u * value / total_value
                    # The same floor the whole meter has to clear, applied to the slice.
                    # Dividing a model's tokens by a slice near zero is what produced a
                    # Sonnet week reading 187M tokens per 1% -- three times the plan's
                    # own figure -- off a 0.46-point slice of a 7% meter. A model that
                    # is nearly all cache_read has nearly no priced value, so its slice
                    # collapses and the quotient runs away.
                    if u_m < MIN_UTILIZATION:
                        continue
                    by_model[model] = sum(present[model].get(c, 0) for c in CLASSES) / u_m
                if by_model:
                    out["by_model"] = by_model
        return out

    return side("five_hour", "tokens_since_five_hour_reset"), side("seven_day", "tokens_since_seven_day_reset")


def _points(rows_by_contributor: dict[str, list[dict]], prices: dict, now: datetime) -> list[dict]:
    """Anonymised per-sample points for the page's contributor chart, see the
    module docstring's `points` entry for the shape and thinning rules."""
    cutoff = now - timedelta(days=POINTS_DAYS)
    per_contributor: dict[str, list[tuple[datetime, str, dict]]] = {}
    for cid, rows in rows_by_contributor.items():
        entries = []
        for r in rows:
            ts_norm = _iso_z(r.get("ts"))
            if ts_norm is None:
                continue
            dt = datetime.strptime(ts_norm, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt < cutoff or dt > now:
                continue
            entries.append((dt, ts_norm, r))
        if entries:
            entries.sort(key=lambda e: e[0])
            per_contributor[cid] = entries

    # Stable opaque per-plan pseudonym: identifiers cannot shift just because a
    # source enters/leaves the rolling period, and the submitted UUID is not
    # exposed.  The caller invokes this separately for each plan.
    ordinals = {cid: int.from_bytes(hashlib.sha256(("contrib:" + cid).encode()).digest()[:4], "big")
                for cid in per_contributor}

    points: list[tuple[datetime, dict]] = []
    for cid, entries in per_contributor.items():
        c = ordinals[cid]
        by_hour: dict[tuple, tuple] = {}
        for dt, ts_norm, r in entries:
            bucket = (dt.year, dt.month, dt.day, dt.hour)
            if bucket not in by_hour or dt > by_hour[bucket][0]:
                by_hour[bucket] = (dt, ts_norm, r)
        for dt, ts_norm, r in by_hour.values():
            u = _utilization(r)
            coarse = u is None or u < MIN_UTILIZATION
            usd_per_pct = None
            if u is not None and u > 0:
                priced = _sample_values(r, prices)
                if priced is not None:
                    _, values = priced
                    usd_per_pct = round(sum(values.values()) / u, 4)
            # Never derive windows/week from two cumulative raw-token maps.
            # Their model and cache mixes differ, so the work does not cancel.
            five, seven = _per_pct_both_windows(r, prices)
            point = {"t": ts_norm, "usd_per_pct": usd_per_pct,
                     "tokens_per_pct": round(five["all"]) if five else None,
                     "tokens_per_pct_week": round(seven["all"]) if seven else None,
                     "c": c, "coarse": coarse}
            for key, side in (("tokens_per_pct_by_model", five), ("tokens_per_pct_week_by_model", seven)):
                if side and "by_model" in side:
                    point[key] = {m: round(v) for m, v in sorted(side["by_model"].items())}
            points.append((dt, point))

    points.sort(key=lambda p: p[0])
    if len(points) > MAX_POINTS:
        points = points[-MAX_POINTS:]
    return [p for _, p in points]


def _contributor_weeks(rows: list[dict], now: datetime) -> list[float]:
    """The `windows` figure of each complete week one contributor's samples pair into."""
    ordered = sorted(rows, key=lambda r: r.get("ts") or "")
    parsed = [parse_row(r) for r in ordered]
    result = weekly_windows(parsed, now=now)
    return [h["windows"] for h in result["history"] if not h.get("partial")]


def _weekly(rows_by_contributor: dict[str, list[dict]], now: datetime) -> dict:
    """Each source's own paired weekly estimate; never pooled, weighted or outlier-rejected.

    Every source's rows are paired as tracker/weekly.py pairs the tracker's own log
    (both meters' movement over the same readings, finding 3), and the source's
    newest week is its estimate, with that week's rounding interval. Nothing
    becomes a plan figure (`measured` stays null): the sources are unverified
    identities with unverified capture, and a source far from the others is kept
    and shown, not dropped (audit 2026-09-16, finding 14).
    """
    estimates = []
    complete = weeks = 0
    for rows in rows_by_contributor.values():
        ordered = sorted(rows, key=lambda r: r.get("ts") or "")
        history = weekly_windows([parse_row(r) for r in ordered], now=now).get("history", [])
        closed = [h for h in history if not h.get("partial")]
        weeks += len(closed)
        complete += bool(closed)
        if history:
            h = history[-1]
            estimates.append({"value": h["windows"], "interval": h.get("rounding_interval"),
                              "five_hour_pct": h["five_hour_pct"], "seven_day_pct": h["seven_day_pct"],
                              "pieces": h.get("pieces"), "through": h["week_ending"], "partial": h.get("partial", True)})
    n = len(rows_by_contributor)
    if not n:
        reason = "no samples"
    else:
        have = f"{complete} with a complete week" if complete else "none with a complete week yet"
        reason = f"{n} contributor{'s' if n != 1 else ''}, {have}; per-source estimates only, never pooled into a plan figure"
    return {"measured": None, "reason": reason, "contributors": n, "with_complete_week": complete,
            "dropped": 0, "weeks": weeks, "estimates": sorted(estimates, key=lambda e: (e["through"], e["value"])),
            "quality": "conditional_paired_meter_deltas"}


def _current_rows(rows: list[dict], now: datetime) -> tuple[dict[str, list[dict]], dict]:
    """Return the current evidence cohort and enough freshness detail to audit it."""
    cutoff = now - timedelta(days=EVIDENCE_DAYS)
    current: dict[str, list[dict]] = {}
    old = bad_time = 0
    newest: datetime | None = None
    for r in rows:
        ts = _iso_z(r.get("ts"))
        if ts is None:
            bad_time += 1
            continue
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if dt < cutoff or dt > now:
            old += 1
            continue
        current.setdefault(str(r.get("contributor_id")), []).append(r)
        if newest is None or dt > newest:
            newest = dt
    return current, {
        "as_of": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "current_since": cutoff.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "freshest_sample_at": newest.isoformat(timespec="seconds") if newest else None,
        "stale": newest is None or newest < now - timedelta(days=7),
        "reason": "no current samples" if newest is None else None,
        "ignored_old_samples": old,
        "ignored_invalid_time_samples": bad_time,
    }


def aggregate(rows: list[dict], now: datetime, prices: dict | None = None) -> dict:
    """The `contributed` block: one entry per plan plus `updated_at`.

    `prices` is data/prices.json's model table (underscore keys already
    dropped); without it `usd_per_pct` is null and `tokens_per_pct` is empty,
    since both need every present model priced. Duplicate (contributor_id, ts)
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
                 "min_utilization": MIN_UTILIZATION, "evidence_window_days": EVIDENCE_DAYS}
    for plan in PLANS:
        all_rows = [r for source_rows in by_plan[plan].values() for r in source_rows]
        contributors, evidence = _current_rows(all_rows, now)
        # Restored pre-2026-09-16-audit rule (finding 7 reversed by decision): every
        # accepted current sample is its own point and its own vote in the medians,
        # not just a source's newest paired span.
        tokens_per_pct, usd_per_pct = _per_pct(contributors, prices)
        points = _points(contributors, prices, now)
        out[plan] = {
            "contributors": len(contributors),
            "samples": sum(len(v) for v in contributors.values()),
            "tokens_per_pct": tokens_per_pct,
            "usd_per_pct": usd_per_pct,
            "weekly_windows": _weekly(contributors, now),
            "points": points,
            "quality": "conditional_local_transcripts",
            "identity_basis": "unverified_source_ids",
            "missing_reasons": [] if points else
                ["no current sample cleared the utilization floor with priced tokens"],
            "evidence": dict(evidence, samples_without_capture=sum(1 for rs in contributors.values() for r in rs if not r.get("capture"))),
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
