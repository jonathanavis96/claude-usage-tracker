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
MAX_SAMPLE_AGE_DAYS = 3


def load_probes(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def usd_per_pct(row: dict, price: dict) -> float:
    tokens = row["tokens"]
    total_usd = sum(tokens[cls] * price[cls] / 1e6 for cls in ("input", "output", "cache_read", "cache_write"))
    return total_usd / (row["tick_to"] - row["tick_from"])


def blended_price_per_token(split: dict, price: dict) -> float:
    return sum(frac * price[cls] / 1e6 for cls, frac in split.items())


def _row_split(row: dict) -> dict:
    tokens = row["tokens"]
    total = sum(tokens[cls] for cls in ("input", "output", "cache_read", "cache_write"))
    return {cls: tokens[cls] / total for cls in ("input", "output", "cache_read", "cache_write")}


def probe_daily_series(rows: list[dict], prices: dict, split: dict, trailing_days: int = 3) -> dict[str, dict[date, float]]:
    by_model: dict[str, dict[date, list[float]]] = {}
    for r in rows:
        d = datetime.fromisoformat(r["ts"]).date()
        price = prices[r["model"]]
        split_for_row = split if split else _row_split(r)
        value = usd_per_pct(r, price) * 100 / blended_price_per_token(split_for_row, price)
        by_model.setdefault(r["model"], {}).setdefault(d, []).append(value)
    out: dict[str, dict[date, float]] = {}
    for model, days in by_model.items():
        series: dict[date, float] = {}
        for d in sorted(days):
            window = [v for dd, vs in days.items() if d - timedelta(days=trailing_days - 1) <= dd <= d for v in vs]
            series[d] = median(window)
        out[model] = series
    return out


def build_public_json(probe_rows: list[dict], passive: dict, effort: dict, prices: dict, now: datetime) -> dict:
    passive_split = passive.get("split", {})
    series = probe_daily_series(probe_rows, prices, passive_split)
    events = detect_changes(series)
    last = latest_change(events)
    # Published ratios only: the passive-observed 5x-to-20x ratio is too noisy to publish
    # (see docs/spike-2026-09.md); the page cites it in caveats instead.
    ratios = dict(PLAN_RATIOS_BASE)
    cutoff = now.date() - timedelta(days=HISTORY_DAYS)
    first_probe_day = min((min(s) for s in series.values()), default=None)
    rates, history = {}, {}
    for model, s in series.items():
        latest_day = max(s)
        latest_row = max((r for r in probe_rows if r["model"] == model), key=lambda r: r["ts"])
        rates[model] = {"tokens_per_window": round(s[latest_day]), "source": "probe",
                        "probe_effort": latest_row["effort"],
                        "split": passive_split,
                        "api_value_per_window": round(usd_per_pct(latest_row, prices[model]) * 100, 2)}
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
