"""Rotation, expectation, drift check and outlier flag for the scheduled probe.

`bin/probe.sh` is a thin caller of this module. Each subcommand answers one question
from `history/probes.jsonl` (the same rows the publisher reads) and `data/prices.json`:

  plan    dry run: the flags every model in the rotation would be probed with, and
          which one is next (the default subcommand).
  next    the next model in the rotation: the one after the last prose row's model,
          in the fixed order Sonnet, Opus, Fable (an empty history starts at Sonnet).
  flags   `--model M --expect-tokens-per-pct N` for the next model (or `--model`), one
          shell-splittable line. `--rerun` gives the flags for the confirmation run
          after a drift: the drifted row's model, `--ticks 2`, and the smaller of the
          earlier median and the drifted reading as the expectation, so the opening
          burst cannot overshoot whichever of the two turns out to be true.
  check   drift check of the newest row against the median of that model's last four
          usable prose rows, compared in meter dollars per 1% (tracker.publish.usd_per_pct)
          rather than raw tokens, since rows on the same model with different token-class
          splits can differ wildly in tokens/1% while agreeing in dollars/1%: exit 10 and
          `drift ...` when it is more than 15% away, exit 0 and `ok ...` otherwise
          (including when there is nothing to compare).
  decide  after the rerun: the newest row is the rerun, the previous prose row is the
          drifted one, both on the same model. Compared in meter dollars per 1%, same as
          check. A rerun within 15% of the earlier median makes the drifted row an
          outlier, flagged in place with `"outlier": true`; a rerun within 15% of the
          drifted reading instead is a change (two agreeing readings that both differ
          from the earlier median); anything else is inconclusive and flags nothing.
          Prints the verdict for the alert.

Expectation is the tokens per 1% the probe assumes before it starts, and it comes from
the dollar invariant in tracker.publish rather than from the target model's own rows:
the median meter-dollar value per 1% of the last four usable prose rows, any model,
divided by the target model's blended meter price per token for probe traffic (the class
split of that model's own latest prose row, or of the latest prose row when it has none)
and by its meter_weight. So Opus and Fable get an expectation before they have ever
been probed, and every model's expectation moves together when the limit moves.

Rows are usable when they are prose (no `payload` key, from before the flag existed, or
`"payload": "prose"`) and not flagged `outlier`; the rules live in tracker/rows.py and the
publisher applies the same ones. Output rows never enter a median and never move the
rotation; they are the weekly weight run (bin/output-probe.sh). An outlier row does not
enter a median but does count as its model's turn.
"""
from __future__ import annotations
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from .publish import blended_price_per_token, load_probes, usd_per_pct
from .rows import is_output, is_outlier

ROTATION = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5-1")
DRIFT_THRESHOLD = 0.15
MEDIAN_ROWS = 4  # the same lookback tracker.detect uses for its plateau median
RERUN_TICKS = 2
EXIT_DRIFT = 10
CLASSES = ("input", "output", "cache_read", "cache_write")


def _ts(row: dict) -> datetime:
    return datetime.fromisoformat(row["ts"])


def _by_ts(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_ts)


def is_prose(row: dict) -> bool:
    return not is_output(row)


def prose_rows(rows: list[dict]) -> list[dict]:
    """Prose rows in time order, flagged outliers included (tracker/rows.py rules)."""
    return [r for r in _by_ts(rows) if is_prose(r)]


def usable_rows(rows: list[dict]) -> list[dict]:
    """Prose rows that may enter a median: not flagged as outliers. Time order."""
    return [r for r in prose_rows(rows) if not is_outlier(r)]


def next_model(rows: list[dict]) -> str:
    """The model after the latest prose row's, in ROTATION order; Sonnet to start."""
    prose = prose_rows(rows)
    if not prose:
        return ROTATION[0]
    last = prose[-1]["model"]
    if last not in ROTATION:
        return ROTATION[0]
    return ROTATION[(ROTATION.index(last) + 1) % len(ROTATION)]


def _split(row: dict) -> dict:
    tokens = row["tokens"]
    total = sum(tokens[c] for c in CLASSES)
    return {c: tokens[c] / total for c in CLASSES}


def expectation(rows: list[dict], model: str, prices: dict) -> float | None:
    """Tokens per 1% the probe on `model` should expect, through the dollar invariant.

    None when no usable prose row on a priced model exists yet: the caller has no
    basis for a burst size and must say so rather than guess.
    """
    price = prices.get(model)
    if price is None:
        return None
    usable = [r for r in usable_rows(rows) if r["model"] in prices]
    if not usable:
        return None
    recent = usable[-MEDIAN_ROWS:]
    dollars = median(usd_per_pct(r, prices[r["model"]]) for r in recent)
    own = [r for r in usable if r["model"] == model]
    split = _split(own[-1] if own else usable[-1])
    per_token = blended_price_per_token(split, price) * price.get("meter_weight", 1.0)
    return dollars / per_token


@dataclass(frozen=True)
class Drift:
    model: str
    ts: str
    value: float
    median: float | None
    ratio: float | None
    drifted: bool


def _earlier_median(rows: list[dict], model: str, before: datetime, prices: dict) -> float | None:
    prior = [r for r in usable_rows(rows) if r["model"] == model and _ts(r) < before and r["model"] in prices]
    if not prior:
        return None
    return median(usd_per_pct(r, prices[r["model"]]) for r in prior[-MEDIAN_ROWS:])


def check_drift(rows: list[dict], prices: dict) -> Drift | None:
    """Compare the newest row with the median meter dollars/1% of its model's last four
    usable prose rows.

    None when there is no row, the newest row is not prose (an output run is never
    drift-checked), or the newest row's model has no price. A first row for a model
    has no median and cannot drift.
    """
    if not rows:
        return None
    last = _by_ts(rows)[-1]
    if not is_prose(last):
        return None
    price = prices.get(last["model"])
    if price is None:
        return None
    value = usd_per_pct(last, price)
    base = _earlier_median(rows, last["model"], _ts(last), prices)
    if base is None:
        return Drift(last["model"], last["ts"], value, None, None, False)
    ratio = value / base - 1
    return Drift(last["model"], last["ts"], value, base, ratio, abs(ratio) > DRIFT_THRESHOLD)


@dataclass(frozen=True)
class Verdict:
    verdict: str  # "outlier" | "change" | "inconclusive"
    model: str
    median: float
    first: float
    rerun: float
    first_ts: str
    rerun_ts: str
    ratio: float  # the pair's mean against the earlier median (meaningful for a change)
    direction: str  # "increased" | "decreased"
    outlier_ts: str | None  # the row to flag, for an outlier verdict


def _agrees(a: float, b: float) -> bool:
    return abs(a / b - 1) <= DRIFT_THRESHOLD


def decide(rows: list[dict], prices: dict) -> Verdict:
    """Judge a drifted row by its rerun. The newest prose row is the rerun; the prose
    row before it is the drifted one; both must be on the same model, and that model
    must have a price and an earlier usable median. Raises ValueError otherwise."""
    prose = prose_rows(rows)
    if len(prose) < 2:
        raise ValueError("decide needs a drifted row and its rerun")
    first, rerun = prose[-2], prose[-1]
    if first["model"] != rerun["model"]:
        raise ValueError(f"rerun row is on {rerun['model']} but the row before it is on {first['model']}")
    model = rerun["model"]
    price = prices.get(model)
    if price is None:
        raise ValueError(f"no price for {model}")
    base = _earlier_median(rows, model, _ts(first), prices)
    if base is None:
        raise ValueError(f"no earlier usable prose row on {model} to judge against")
    f, r = usd_per_pct(first, price), usd_per_pct(rerun, price)
    ratio = (f + r) / 2 / base - 1
    direction = "increased" if ratio > 0 else "decreased"
    if _agrees(r, base):
        return Verdict("outlier", model, base, f, r, first["ts"], rerun["ts"], ratio, direction, first["ts"])
    if _agrees(r, f):
        return Verdict("change", model, base, f, r, first["ts"], rerun["ts"], ratio, direction, None)
    return Verdict("inconclusive", model, base, f, r, first["ts"], rerun["ts"], ratio, direction, None)


def mark_outlier(path: Path, ts: str) -> None:
    """Set `"outlier": true` on the row with this `ts`, rewriting only that line."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    hit = False
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("ts") == ts:
            row["outlier"] = True
            lines[i] = json.dumps(row)
            hit = True
            break
    if not hit:
        raise ValueError(f"no row with ts {ts} in {path}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    tmp.replace(path)


def _load(history: Path) -> list[dict]:
    return load_probes(history) if Path(history).exists() else []


def _prices(path: Path) -> dict:
    return {k: v for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items() if not k.startswith("_")}


def _pct(ratio: float) -> str:
    return f"{ratio * 100:+.0f}%"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Rotation, expectation and drift check for bin/probe.sh")
    ap.add_argument("command", nargs="?", default="plan", choices=("plan", "next", "flags", "check", "decide"))
    ap.add_argument("--history", type=Path, default=Path("history/probes.jsonl"))
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--model", default=None, help="flags: this model instead of the next in the rotation")
    ap.add_argument("--rerun", action="store_true", help="flags: the confirmation run for the newest (drifted) row")
    a = ap.parse_args(argv)
    try:
        rows = _load(a.history)
        prices = _prices(a.prices)
    except (OSError, ValueError) as e:
        print(f"{a.command}: {e}", file=sys.stderr)
        return 1

    if a.command == "next":
        print(next_model(rows))
        return 0

    if a.command == "plan":
        nxt = next_model(rows)
        for model in ROTATION:
            e = expectation(rows, model, prices)
            flags = (f"--model {model} --expect-tokens-per-pct {round(e)}" if e is not None
                     else f"--model {model} --expect-tokens-per-pct (none: no usable prose row)")
            print(f"{flags}{'  <- next' if model == nxt else ''}")
        return 0

    if a.command == "flags":
        if a.rerun:
            d = check_drift(rows, prices)
            if d is None or d.median is None:
                print("flags --rerun: newest row is not a prose row with an earlier median", file=sys.stderr)
                return 1
            # Convert the dollar median back to tokens through the drifted row's own
            # split, same as expectation does, so the burst size is still a token count.
            drifted = _by_ts(rows)[-1]
            price = prices[d.model]
            per_token = blended_price_per_token(_split(drifted), price) * price.get("meter_weight", 1.0)
            tokens = round(min(d.median, d.value) / per_token)
            print(f"--model {d.model} --expect-tokens-per-pct {tokens} --ticks {RERUN_TICKS}")
            return 0
        model = a.model or next_model(rows)
        e = expectation(rows, model, prices)
        if e is None:
            print(f"flags: no usable prose row to set an expectation for {model}", file=sys.stderr)
            return 1
        print(f"--model {model} --expect-tokens-per-pct {round(e)}")
        return 0

    if a.command == "check":
        d = check_drift(rows, prices)
        if d is None:
            print("ok: no prose row to check")
            return 0
        if d.median is None:
            print(f"ok {d.model} ${d.value:.3f}/1%: no earlier prose row to compare")
            return 0
        word = "drift" if d.drifted else "ok"
        print(f"{word} {d.model} ${d.value:.3f}/1% against median ${d.median:.3f}/1% ({_pct(d.ratio or 0.0)})")
        return EXIT_DRIFT if d.drifted else 0

    if a.command == "decide":
        try:
            v = decide(rows, prices)
            if v.outlier_ts:
                mark_outlier(a.history, v.outlier_ts)
        except (OSError, ValueError) as e:
            print(f"decide: {e}", file=sys.stderr)
            return 1
        detail = {"outlier": f"rerun agreed with the median; row {v.first_ts} flagged outlier",
                  "change": f"rerun agreed with the drifted reading; {v.direction} {_pct(v.ratio)} against the median",
                  "inconclusive": "rerun agreed with neither the median nor the drifted reading; nothing flagged"}
        print(f"{v.verdict} {v.model} first ${v.first:.3f}/1% rerun ${v.rerun:.3f}/1% median ${v.median:.3f}/1%: "
              f"{detail[v.verdict]}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
