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
    # Published ratios only: the passive-observed 5x-to-20x ratio is too noisy to publish
    # (see docs/spike-2026-09.md); the page cites it in caveats instead.
    ratios = dict(PLAN_RATIOS_BASE)
    cutoff = now.date() - timedelta(days=HISTORY_DAYS)
    first_probe_day = min((min(s) for s in series.values()), default=None)
    rates, history = {}, {}
    for model, s in series.items():
        latest_day = max(s)
        rates[model] = {"tokens_per_window": round(s[latest_day]), "source": "probe",
                        "probe_effort": max((r for r in probe_rows if r["model"] == model), key=lambda r: r["ts"])["effort"],
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
        effort = {k: v for k, v in json.loads(a.effort.read_text()).items() if not k.startswith("_")}
        j = build_public_json(load_probes(a.probes), passive, effort, json.loads(a.prices.read_text()), datetime.now(timezone.utc))
    except (OSError, ValueError, KeyError) as e:
        print(f"publish failed, previous output left in place: {e}", file=sys.stderr)
        return 1
    write_json(a.out, j)
    print(f"wrote {a.out}: {len(j['rates'])} models, last_change={j['last_change']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
