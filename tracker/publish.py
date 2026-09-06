"""Assemble the public JSON from probe rows, passive output, and static tables."""
from __future__ import annotations
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from .detect import detect_changes, latest_change

PLAN_RATIOS_BASE = {"pro": 0.05, "max5": 0.25, "max20": 1.0}
HISTORY_DAYS = 90
MAX_SAMPLE_AGE_DAYS = 10


def load_probes(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def tokens_usd(tokens: dict, price: dict) -> float:
    """API-list value of a token bundle. `price` is USD per million tokens, per class."""
    return sum(tokens[cls] * price[cls] / 1e6 for cls in ("input", "output", "cache_read", "cache_write"))


def usd_per_pct(row: dict, price: dict) -> float:
    return tokens_usd(row["tokens"], price) / (row["tick_to"] - row["tick_from"])


def blended_price_per_token(split: dict, price: dict) -> float:
    return sum(frac * price[cls] / 1e6 for cls, frac in split.items())


def _row_split(row: dict) -> dict:
    tokens = row["tokens"]
    total = sum(tokens[cls] for cls in ("input", "output", "cache_read", "cache_write"))
    return {cls: tokens[cls] / total for cls in ("input", "output", "cache_read", "cache_write")}


def build_public_json(probe_rows: list[dict], passive: dict, effort: dict, prices: dict, now: datetime) -> dict:
    """Derive every model's rate from one probed model's dollar value.

    Only one model is probed (see bin/probe.sh, weekly cadence). Its API-list
    dollar value per window is the invariant: every other model's
    tokens_per_window is that same dollar amount divided by the model's own
    blended price per token (blended_price_per_token returns USD per token,
    not per million, so no further scaling is needed). A model's `rates`
    entry is "probe" sourced only when it is the model the latest row
    actually probed; every other model in prices.json is "derived".
    """
    passive_split = passive.get("split", {})
    if not probe_rows:
        raise ValueError("no probe rates to publish")

    # One model-agnostic dollar series: what a full window (100%) of usage
    # would have cost at API list prices, regardless of which model probed it.
    dollar_readings = [(datetime.fromisoformat(r["ts"]), usd_per_pct(r, prices[r["model"]]) * 100) for r in probe_rows]
    events = detect_changes(dollar_readings)
    last = latest_change(events)

    by_day_value: dict[date, list[float]] = {}
    by_day_models: dict[date, set[str]] = {}
    for r in probe_rows:
        d = datetime.fromisoformat(r["ts"]).date()
        by_day_value.setdefault(d, []).append(usd_per_pct(r, prices[r["model"]]) * 100)
        by_day_models.setdefault(d, set()).add(r["model"])

    latest_row = max(probe_rows, key=lambda r: r["ts"])
    api_value_per_window = usd_per_pct(latest_row, prices[latest_row["model"]]) * 100
    # A missing/lagging passive.json is allowed (it arrives from masterrig), so fall back to
    # the latest probe row's own class split rather than blowing up on an empty split.
    passive_split = passive_split or _row_split(latest_row)

    # Published ratios only: the passive-observed 5x-to-20x ratio is too noisy to publish
    # (see docs/spike-2026-09.md); the page cites it in caveats instead.
    ratios = dict(PLAN_RATIOS_BASE)
    cutoff = now.date() - timedelta(days=HISTORY_DAYS)
    first_probe_day = min(by_day_value, default=None)
    rates, history = {}, {}
    for model, price in prices.items():
        blended = blended_price_per_token(passive_split, price)
        rates[model] = {"tokens_per_window": round(api_value_per_window / blended),
                        "source": "probe" if model == latest_row["model"] else "derived",
                        "probe_effort": latest_row["effort"],
                        "split": passive_split,
                        "api_value_per_window": round(api_value_per_window, 2)}
        hist = []
        for ds, v in sorted(passive.get("history", {}).items()):
            d = date.fromisoformat(ds)
            if d >= cutoff and (first_probe_day is None or d < first_probe_day):
                hist.append({"date": ds, "tokens_per_window": round(v["tokens_per_pct"] * 100), "source": "passive", "interpolated": bool(v.get("interpolated"))})
        for d in sorted(by_day_value):
            if d >= cutoff:
                day_value = median(by_day_value[d])
                hist.append({"date": d.isoformat(), "tokens_per_window": round(day_value / blended),
                            "source": "probe" if model in by_day_models[d] else "derived", "interpolated": False})
        history[model] = hist
    last_sample = max((r["ts"] for r in probe_rows), default=None)
    # The page freezes generated_at, so a stale publish would silently present old
    # numbers as current instead of letting the page's own stale warning fire.
    if not rates:
        raise ValueError("no probe rates to publish")
    if last_sample is None or datetime.fromisoformat(last_sample) < now - timedelta(days=MAX_SAMPLE_AGE_DAYS):
        raise ValueError(f"newest probe sample {last_sample} is older than {MAX_SAMPLE_AGE_DAYS} days")
    return {
        "generated_at": now.isoformat(),
        "last_sample_at": last_sample,
        "passive_generated_at": passive.get("generated_at"),
        "plan_measured": "max20",
        "plan_ratios": ratios,
        "rate_basis": "api_value",
        "rates": rates,
        "effort": effort,
        "api_price_per_mtok": prices,
        "history": history,
        "last_change": None if last is None else {"date": last.date.isoformat(), "direction": last.direction, "percent": last.percent, "model": last.model},
    }


def write_json(path: Path, obj: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic: a crash mid-write never truncates the previous file


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
    # A missing passive file is allowed (it arrives from masterrig and may lag); an unreadable one is not.
    try:
        passive = json.loads(a.passive.read_text()) if a.passive.exists() else {}
        effort_raw = json.loads(a.effort.read_text())
        if effort_raw.get("_status") == "placeholder":
            raise ValueError(f"{a.effort} is still a placeholder; calibrate it before publishing")
        effort = {k: v for k, v in effort_raw.items() if not k.startswith("_")}
        prices = {k: v for k, v in json.loads(a.prices.read_text()).items() if not k.startswith("_")}
        j = build_public_json(load_probes(a.probes), passive, effort, prices, datetime.now(timezone.utc))
    except (OSError, ValueError, KeyError) as e:
        print(f"publish failed, previous output left in place: {e}", file=sys.stderr)
        return 1
    write_json(a.out, j)
    print(f"wrote {a.out}: {len(j['rates'])} models, last_change={j['last_change']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
