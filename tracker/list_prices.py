"""Add a list-price row to data/prices.json for a model in use that has none (issue #130).

A model id the tracker has never met gets a credit family of its own with no manual step
(tracker/credits.py `auto_family`), and tools/model_rates.py fits it once stretches carry
it. What stayed manual was its dollar row: `passive_credit_points` drops every stretch
holding a model with no row, and `unpriced_credit_models` names those models. Claude
Sonnet 5.5 went into use on 2026-09-29 as the subagent model, before the pricing page
listed it, so its stretches were dropped until someone added the row by hand.

This fetches Anthropic's pricing page, reads the main model pricing table, and for each
model the stretch histories carry that is still unpriced adds the row the page lists. It
never changes or removes an existing row, and never adds one for a model not in use.

The page holds several tables with the same model names (batch, and at times fast-mode and
long-context rates). The main one is the table whose header row names all five token
classes -- base input, 5-minute cache write, 1-hour cache write, cache hits, output -- and
the column of each is read from that header row, never assumed. The batch table has no
cache columns and is never picked.

Advisory, like every step before the publish in bin/daily.sh: a failed fetch, a missing
table, or a value that is missing, zero or not a number is one warning line on stderr and
no change, and the exit status is 0 either way.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from . import credits as credit_model
from .gs_passive import unpriced_credit_models
from .turns import normalized_raw_model

PRICING_URL = "https://docs.anthropic.com/en/docs/about-claude/pricing"
TIMEOUT_S = 20
#: The token classes of a dollar row, in the order data/prices.json writes them.
PRICE_CLASSES = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
METER_WEIGHT_SOURCE = ("assumed, not measured: added automatically from the pricing page "
                       "by tracker/list_prices.py (issue #130)")
#: The fewest models the main table can name. It lists every current and older model (19
#: on 2026-09-29); a fast-mode table with the same five columns lists one or two, and must
#: never be read as the main table when the main one is missing from the page.
MIN_MAIN_MODELS = 5
#: "Claude Sonnet 5.5" -> claude-sonnet-5-5, the form data/prices.json keys take.
_DISPLAY_NAME = re.compile(r"^Claude ([A-Za-z]+) (\d+(?:\.\d+)*)$")
_DOLLARS = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


class PriceTableError(ValueError):
    """The page gave no usable main pricing table, or a value in it was unusable."""


class _Tables(HTMLParser):
    """Every <table> on the page as rows of cells, each cell the list of its text pieces.

    The pieces are kept apart rather than joined: a model's cell holds its name and then
    a one-line description ("Claude Opus 5.5" / "For long-running agentic coding..."), and
    the name is the first piece.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[list[str]]]] = []
        self._stack: list[list[list[list[str]]]] = []
        self._row: list[list[str]] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, _attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
            self._stack[-1].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._row.append(self._cell)

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = self._cell = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())
            self._row = self._cell = None

    def handle_data(self, data):
        text = " ".join(data.split())
        if self._cell is not None and text:
            self._cell.append(text)


def _column(label: str) -> str | None:
    """Which token class a header cell names, or None."""
    t = label.lower().replace("-", " ")
    if "write" in t and any(k in t for k in ("5m", "5 min")):
        return "cache_write"
    if "write" in t and any(k in t for k in ("1h", "1 hour", "1 hr")):
        return "cache_write_1h"
    if "hit" in t or "cache read" in t:
        return "cache_read"
    if "output" in t:
        return "output"
    if "input" in t and "cache" not in t:
        return "input"
    return None


def model_id(display: str) -> str | None:
    """`Claude Sonnet 5.5` -> `claude-sonnet-5-5`; None for anything else."""
    m = _DISPLAY_NAME.match(display.strip())
    if not m:
        return None
    return f"claude-{m.group(1).lower()}-{m.group(2).replace('.', '-')}"


def _dollars(cell: list[str]) -> float:
    m = _DOLLARS.search(" ".join(cell))
    if not m:
        raise PriceTableError(f"no dollar value in {' '.join(cell)!r}")
    value = float(m.group(1).replace(",", ""))
    if value <= 0:
        raise PriceTableError(f"zero price in {' '.join(cell)!r}")
    return value


def parse_price_table(html: str) -> dict[str, dict[str, float]]:
    """{model id: {class: USD per million tokens}} from the page's main pricing table.

    A table qualifies when a header row names all five token classes, and its columns are
    taken from that row. Where more than one qualifies (a fast-mode table can carry the
    same five columns for one or two models), the main table is the one listing the most
    models, the first on a tie, and it must name at least MIN_MAIN_MODELS. A model listed with a value that is missing, zero or not
    a number raises PriceTableError, so no half-read row is ever written.
    """
    parser = _Tables()
    parser.feed(html)
    candidates = [c for c in (_read_table(t) for t in parser.tables) if c is not None]
    if not candidates:
        raise PriceTableError("no table with input, output, 5m write, 1h write and cache hit columns")
    rows, bad, named = max(candidates, key=lambda c: c[2])
    if named < MIN_MAIN_MODELS:
        raise PriceTableError(f"the largest table with all five price columns names {named} models, "
                              f"under {MIN_MAIN_MODELS}: not the main pricing table")
    if bad:
        raise PriceTableError(bad)
    if not rows:
        raise PriceTableError("main pricing table has no model rows")
    return rows


def _read_table(table: list[list[list[str]]]) -> tuple[dict[str, dict[str, float]], str | None, int] | None:
    """(rows read, the first problem or None, models named), or None when no header names all five classes.

    The problem is carried rather than raised so that a malformed side table cannot stop
    the main one being chosen, and a malformed main table is refused whole. Tables are
    compared by the models they name, read or not, so a bad value early in the main
    table cannot hand the choice to a smaller one.
    """
    for i, row in enumerate(table):
        cols = {}
        for j, cell in enumerate(row):
            cls = _column(" ".join(cell))
            if cls is not None and cls not in cols:
                cols[cls] = j
        if set(cols) != set(PRICE_CLASSES):
            continue
        body = [d for d in table[i + 1:] if d and d[0] and model_id(d[0][0]) is not None]
        named = len({model_id(d[0][0]) for d in body})
        out: dict[str, dict[str, float]] = {}
        problem = None
        for data in body:
            mid = model_id(data[0][0])
            if mid in out:
                continue
            if len(data) <= max(cols.values()):
                problem = f"row for {data[0][0]} has {len(data)} cells, the header {len(row)}"
                break
            try:
                out[mid] = {cls: _dollars(data[j]) for cls, j in cols.items()}
            except PriceTableError as exc:
                problem = f"{data[0][0]}: {exc}"
                break
        return out, problem, named
    return None


def fetch(url: str = PRICING_URL, timeout: float = TIMEOUT_S) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "claude-usage-tracker (list prices)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def models_in_use_unpriced(reports: list[dict], prices: dict, credits: dict) -> list[str]:
    """Model ids the stretch histories carry that have no row in `prices`.

    `unpriced_credit_models` is the list of what the credit series drops; of those, only a
    current-style id with no dollar row at all is one the page could price. A model that
    has a row and still cannot be valued is a credit-rate question, not a missing price.
    """
    out = set()
    for rpt in reports:
        for model in unpriced_credit_models(rpt, prices, credits):
            mid = normalized_raw_model(model)
            if credit_model.model_key(mid) is not None and mid not in prices:
                out.add(mid)
    return sorted(out)


def _class_weight(prices: dict) -> dict:
    """The class weights every existing row carries (the publisher writes them to all)."""
    seen = Counter(json.dumps(row["class_weight"], sort_keys=False)
                   for key, row in prices.items()
                   if not key.startswith("_") and isinstance(row, dict) and isinstance(row.get("class_weight"), dict))
    if not seen:
        raise PriceTableError("no existing row carries a class_weight to copy")
    return json.loads(seen.most_common(1)[0][0])


def _number(value: float) -> float | int:
    return int(value) if value == int(value) else value


def new_row(listed: dict[str, float], prices: dict, url: str, fetched: str) -> dict:
    row = {"input": _number(listed["input"]), "output": _number(listed["output"]),
           "cache_read": listed["cache_read"], "cache_write": listed["cache_write"],
           "cache_write_1h": listed["cache_write_1h"],
           "meter_weight": 1.0, "meter_weight_source": METER_WEIGHT_SOURCE,
           "class_weight": _class_weight(prices),
           "list_price_source": {"url": url, "fetched": fetched}}
    return row


def add_rows(prices: dict, listed: dict[str, dict[str, float]], wanted: list[str],
             url: str, fetched: str) -> tuple[dict, list[str]]:
    """`prices` with a row added for each `wanted` model the page lists and `prices` lacks.

    Existing keys keep their values and order; new rows go after the last model row, ahead
    of the trailing `_output_weight` block.
    """
    added = [m for m in wanted if m not in prices and m in listed]
    if not added:
        return prices, []
    rows = {m: new_row(listed[m], prices, url, fetched) for m in added}
    keys = list(prices)
    tail = len(keys)
    while tail > 0 and keys[tail - 1].startswith("_"):
        tail -= 1
    out = {k: prices[k] for k in keys[:tail]}
    out.update(rows)
    out.update({k: prices[k] for k in keys[tail:]})
    return out, added


def _warn(msg: str) -> None:
    print(f"warning: tracker.list_prices: {msg}", file=sys.stderr)


def run(prices_path: Path, stretch_paths: list[Path], *, fetcher=fetch, url: str = PRICING_URL,
        now: datetime | None = None) -> list[str]:
    """Add what rows are due and write data/prices.json; the models added. Never raises."""
    try:
        text = prices_path.read_text(encoding="utf-8")
        prices = json.loads(text)
        credits = credit_model.load_credits(prices)
    except (OSError, ValueError) as exc:
        _warn(f"could not read {prices_path}: {exc}")
        return []
    reports = []
    for p in stretch_paths:
        try:
            reports.append(json.loads(p.read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            _warn(f"could not read {p}: {exc}")
    try:
        wanted = models_in_use_unpriced(reports, prices, credits)
    except Exception as exc:  # noqa: BLE001 -- advisory step, never fails the publish
        _warn(f"could not list unpriced models: {exc}")
        return []
    if not wanted:
        return []
    try:
        listed = parse_price_table(fetcher(url))
    except PriceTableError as exc:
        _warn(f"{exc}; no rows added for {', '.join(wanted)}")
        return []
    except Exception as exc:  # noqa: BLE001 -- a fetch can fail many ways; all are a no-op
        _warn(f"could not fetch {url}: {exc}; no rows added for {', '.join(wanted)}")
        return []
    fetched = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
    try:
        updated, added = add_rows(prices, listed, wanted, url, fetched)
    except PriceTableError as exc:
        _warn(f"{exc}; no rows added")
        return []
    if not added:
        return []
    tmp = prices_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        tmp.replace(prices_path)
    except OSError as exc:
        _warn(f"could not write {prices_path}: {exc}")
        return []
    print(f"tracker.list_prices: added list-price rows for {', '.join(added)}")
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Add pricing-page rows for models in use that data/prices.json lacks")
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--stretches", type=Path, nargs="+",
                    default=[Path("history/gs-passive.json"), Path("history/masterrig-passive.json")],
                    help="stretch histories whose models count as in use")
    ap.add_argument("--url", default=PRICING_URL)
    a = ap.parse_args(argv)
    run(a.prices, a.stretches, url=a.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
