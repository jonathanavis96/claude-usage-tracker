"""Assemble the public JSON from probe rows, passive output, and static tables."""
from __future__ import annotations
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from .detect import detect_changes
from .passive import PLAN_CHANGE
from .rows import usable_rows
from .weekly import probe_weekly_windows

PLAN_RATIOS_BASE = {"pro": 0.05, "max5": 0.25, "max20": 1.0}
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


def class_weight(price: dict, cls: str) -> float:
    """Meter weight of one token class relative to its list price.

    The 5-hour meter does not charge every token class at its list-price
    ratio: the 2026-09-06 output-heavy Fable probe cost $0.59 of list value
    per 1% against $1.08 for the cache-write-heavy payload, so output tokens
    weigh about 1.8x their list price (see docs/spike-2026-09.md). A missing
    class_weight, or a class missing from it, defaults to 1.0 (cache_write,
    the class the Sonnet invariant probe is made of).
    """
    return price.get("class_weight", {}).get(cls, 1.0)


def meter_usd(tokens: dict, price: dict) -> float:
    """Meter-dollar value of a token bundle: list value per class x class_weight."""
    return sum(tokens[cls] * price[cls] * class_weight(price, cls) / 1e6
               for cls in ("input", "output", "cache_read", "cache_write"))


def usd_per_pct(row: dict, price: dict) -> float:
    """Meter-dollar value of a token bundle for one pct of a window.

    Meter dollars = list dollars x class_weight per token class x the model's
    meter_weight: the 5-hour meter does not charge every model or every token
    class at its list-price ratio (see docs/spike-2026-09.md, 2026-09-06
    rulings), so a probe on any model must be scaled by both before it can
    serve as a model-agnostic invariant. A missing meter_weight defaults to
    1.0 (Sonnet 5's own weight).
    """
    weight = price.get("meter_weight", 1.0)
    return meter_usd(row["tokens"], price) * weight / (row["tick_to"] - row["tick_from"])


def blended_price_per_token(split: dict, price: dict) -> float:
    """Meter dollars per token for a class split (class_weight applied, meter_weight not)."""
    return sum(frac * price[cls] * class_weight(price, cls) / 1e6 for cls, frac in split.items())


def _row_split(row: dict) -> dict:
    tokens = row["tokens"]
    total = sum(tokens[cls] for cls in ("input", "output", "cache_read", "cache_write"))
    return {cls: tokens[cls] / total for cls in ("input", "output", "cache_read", "cache_write")}


def build_public_json(probe_rows: list[dict], passive: dict, effort: dict, prices: dict, now: datetime,
                      effort_usd: dict | None = None) -> dict:
    """Derive every model's rate from one probed model's dollar value.

    `effort` is the calibration matrix's median tokens per task (model -> effort) and
    `effort_usd` its median meter dollars per task, passed through as `effort_usd`.

    Only one model is probed (see bin/probe.sh, weekly cadence). Its
    meter-dollar value per window (list-dollar value x that model's own
    meter_weight, see usd_per_pct) is the invariant: every other model's
    tokens_per_window is that same meter-dollar amount divided by the
    model's own blended price per token times its own meter_weight
    (blended_price_per_token returns USD per token, not per million, so no
    further scaling is needed there). meter_weight corrects for the 5-hour
    meter charging models at a different rate than their list price implies
    (see docs/spike-2026-09.md, 2026-09-06 ruling); a model missing the key
    defaults to 1.0.

    The published `history` is a step function of the measured limit, not
    the raw daily series: Jonathan's own passive account is noisy 2x-5x day
    to day (the rolling meter decays), and the public chart must show only
    what change detection actually found, not that noise and not his plan
    history. detect_changes splits the model-agnostic dollar series into
    regimes; each regime is held flat at the median of the probe readings
    that fall inside it, and every model's tokens_per_window for that regime
    is that same median divided by the model's own blended price times its
    own meter_weight -- so every model steps on the same dates, just at
    different levels. Days before the first probe (when passive history
    reaches further back) are folded into the first regime and held flat at
    its value: the passive record certifies no step larger than the 30
    July-18 August plan change happened before probing started, so nothing
    in that gap can be measured, only held. A day's `source` is "probe" when
    this exact model was probed that day, "derived" when the day falls in a
    probed regime but another model was the one actually probed, and "held"
    for the pre-first-probe fill. `interpolated` stays false everywhere --
    it is a step function, not an interpolation, and the page should never
    draw a dot on a held or derived day.

    `rates[model]["source"]` is a separate, coarser signal: "probe" when this
    model has at least one usable prose row anywhere in the history (all
    three models are probed in rotation, one per 12 hours, so each has its
    own probe rows even on days it was not the one actually probed), else
    "derived". `rates[model]["probed_at"]` is that model's own latest usable
    prose row's timestamp, or null when it has never been probed.

    Top-level `probe_accounts` is the sorted list of distinct `account` tags
    (tracker/probe.py's --account names, e.g. "dave", "jono") carried on
    usable prose rows -- which accounts actually did the probing, not which
    were merely configured.

    Every history day also carries `api_value_per_window`: the regime's
    held dollar value itself, identical across models on any given day, so
    the page can show dollars per window with the same step treatment as
    tokens without re-deriving it from prices. `rates[model]` carries the
    current regime's value under the same key.
    """
    passive_split = passive.get("split", {})
    # Weekly windows measured from probe rows' own before/after meter reads,
    # independent of the passive log -- taken from every row regardless of the
    # outlier/class-weighting filter below, since that filter is about dollar
    # value, not the five-hour/seven-day deltas this measures.
    probe_weekly = probe_weekly_windows(probe_rows, now=now)

    # Output rows (the weekly Fable weight run, payload "output") measure the meter's
    # class weighting, not the limit, and a row flagged `outlier` by the rotation's
    # drift check was contradicted by its rerun: neither enters the dollar series,
    # the regime medians, change detection or the freshness check (tracker/rows.py).
    probe_rows = usable_rows(probe_rows)
    if not probe_rows:
        raise ValueError("no probe rates to publish")

    # One model-agnostic dollar series: what a full window (100%) of usage
    # would have cost at API list prices, regardless of which model probed it.
    dollar_readings = [(datetime.fromisoformat(r["ts"]), usd_per_pct(r, prices[r["model"]]) * 100) for r in probe_rows]
    events = detect_changes(dollar_readings)
    regime_starts = sorted({e.date for e in events})

    def regime_index(d: date) -> int:
        idx = 0
        for rd in regime_starts:
            if d >= rd:
                idx += 1
            else:
                break
        return idx

    by_day_value: dict[date, list[float]] = {}
    by_day_models: dict[date, set[str]] = {}
    regime_values: dict[int, list[float]] = {}
    for r in probe_rows:
        d = datetime.fromisoformat(r["ts"]).date()
        v = usd_per_pct(r, prices[r["model"]]) * 100
        by_day_value.setdefault(d, []).append(v)
        by_day_models.setdefault(d, set()).add(r["model"])
        regime_values.setdefault(regime_index(d), []).append(v)

    latest_row = max(probe_rows, key=lambda r: r["ts"])
    latest_row_per_model: dict[str, dict] = {}
    for r in probe_rows:
        m = r["model"]
        if m not in latest_row_per_model or r["ts"] > latest_row_per_model[m]["ts"]:
            latest_row_per_model[m] = r
    probe_accounts = sorted({r["account"] for r in probe_rows if r.get("account")})
    first_probe_day = min(by_day_value)
    # A missing/lagging passive.json is allowed (it arrives from masterrig), so fall back to
    # the latest probe row's own class split rather than blowing up on an empty split.
    passive_split = passive_split or _row_split(latest_row)

    passive_history = passive.get("history", {})
    first_day = min((date.fromisoformat(ds) for ds in passive_history), default=first_probe_day)
    first_day = min(first_day, first_probe_day)
    last_day = now.date()
    current_regime = regime_index(last_day)
    current_regime_value = median(regime_values[current_regime])
    api_value_per_window = current_regime_value

    # Published ratios only: the passive-observed 5x-to-20x ratio is too noisy to publish
    # (see docs/spike-2026-09.md); the page cites it in caveats instead.
    ratios = dict(PLAN_RATIOS_BASE)
    rates, history = {}, {}
    for model, price in prices.items():
        blended = blended_price_per_token(passive_split, price)
        weight = price.get("meter_weight", 1.0)
        model_probe = latest_row_per_model.get(model)
        rates[model] = {"tokens_per_window": round(current_regime_value / (blended * weight)),
                        "source": "probe" if model_probe is not None else "derived",
                        "probed_at": model_probe["ts"] if model_probe is not None else None,
                        "probe_effort": latest_row["effort"],
                        "split": passive_split,
                        "api_value_per_window": round(api_value_per_window, 2)}
        hist = []
        d = first_day
        while d <= last_day:
            idx = regime_index(d)
            regime_value = median(regime_values[idx])
            if d < first_probe_day:
                source = "held"
            elif model in by_day_models.get(d, set()):
                source = "probe"
            else:
                source = "derived"
            hist.append({"date": d.isoformat(), "tokens_per_window": round(regime_value / (blended * weight)),
                        "api_value_per_window": round(regime_value, 2),
                        "source": source, "interpolated": False})
            d += timedelta(days=1)
        history[model] = hist
    last_sample = max((r["ts"] for r in probe_rows), default=None)
    # The page freezes generated_at, so a stale publish would silently present old
    # numbers as current instead of letting the page's own stale warning fire.
    if not rates:
        raise ValueError("no probe rates to publish")
    if last_sample is None or datetime.fromisoformat(last_sample) < now - timedelta(days=MAX_SAMPLE_AGE_DAYS):
        raise ValueError(f"newest probe sample {last_sample} is older than {MAX_SAMPLE_AGE_DAYS} days")
    passive_weekly = passive.get("weekly_windows")
    weekly_windows = None
    weekly_events: list = []
    if passive_weekly is not None:
        # The count of five-hour windows a week's cap holds is per plan, not one
        # continuous series: Jonathan moved Max 5x -> Max 20x on PLAN_CHANGE, so
        # the passive history is split at that date rather than treated as one
        # series that happens to contain a plan-change artifact. The week whose
        # (week_ending-7, week_ending] span straddles PLAN_CHANGE is dropped from
        # both plans -- it mixes days from each plan and belongs to neither.
        # max5 is frozen (Jonathan is not going back to it); only max20, the live
        # plan, gets a probe series and change detection. Probe weeks (the Dave
        # account, which probes on max20) replace passive max20 weeks from the
        # first probe week on, same concatenation rule as before.
        passive_history = passive_weekly.get("history", [])
        # passive.json may lag: it can predate the flag, or carry a `partial` from
        # when its newest week was still open. Recompute the flag against this
        # publish's own time so every published weekly row honours the contract.
        for h in passive_history:
            h["partial"] = date.fromisoformat(h["week_ending"]) >= now.date()
        probe_history = probe_weekly["history"]
        max5_history = [h for h in passive_history if _plan_for_week(h["week_ending"]) == "max5"]
        passive_max20 = [h for h in passive_history if _plan_for_week(h["week_ending"]) == "max20"]
        if probe_history:
            first_probe_week = min(h["week_ending"] for h in probe_history)
            max20_history = [h for h in passive_max20 if h["week_ending"] < first_probe_week] + probe_history
        else:
            max20_history = list(passive_max20)
        weekly_readings = [(datetime.fromisoformat(h["week_ending"]), h["windows"]) for h in max20_history]
        weekly_events = detect_changes(weekly_readings)
        max5_current = _weekly_current(max5_history, now)
        weekly_windows = {
            "max20": {"current": _weekly_current(max20_history, now), "history": max20_history, "assumed": False},
            "max5": {"current": max5_current, "history": max5_history, "assumed": False},
            # Pro has no measurement of its own yet: publish max5's frozen figures
            # as a stand-in (same 5x-ratio era) flagged "assumed" so the page can
            # show Pro numbers while labelling them unmeasured.
            "pro": {"current": max5_current, "history": max5_history, "assumed": True},
            "passive": passive_weekly,
            "probe": probe_weekly,
        }
    last_change = _latest_change_with_scope(events, weekly_events)
    out = {
        "generated_at": now.isoformat(),
        "last_sample_at": last_sample,
        "passive_generated_at": passive.get("generated_at"),
        "plan_measured": "max20",
        "probe_accounts": probe_accounts,
        "plan_ratios": ratios,
        "rate_basis": "api_value",
        "rates": rates,
        "effort": effort,
        "effort_usd": effort_usd or {},
        "api_price_per_mtok": prices,
        "history": history,
        "last_change": last_change,
        "events": _build_events(events, weekly_events),
        "session_tokens": passive.get("session_tokens", {}),
    }
    if weekly_windows is not None:
        out["weekly_windows"] = weekly_windows
    return out


def _plan_for_week(week_ending: str) -> str | None:
    """Which plan a passive weekly-window bucket belongs to, or None if it straddles PLAN_CHANGE.

    A week's bucket spans (week_ending - 7 days, week_ending]. It is max5 only if
    it ends on or before PLAN_CHANGE, max20 only if it starts on or after
    PLAN_CHANGE; a week whose span contains PLAN_CHANGE mixes days from both
    plans and is dropped from both.
    """
    we = date.fromisoformat(week_ending)
    start = we - timedelta(days=7)
    if we <= PLAN_CHANGE:
        return "max5"
    if start >= PLAN_CHANGE:
        return "max20"
    return None


def _weekly_current(history: list[dict], now: datetime) -> float | None:
    complete = [h for h in history if date.fromisoformat(h["week_ending"]) < now.date()]
    return round(median(h["windows"] for h in complete[-2:]), 2) if complete else None


def _latest_change_with_scope(window_events: list, weekly_events: list) -> dict | None:
    """The most recent change across both event series, tagged with which one it came from."""
    candidates = [(e, "window") for e in window_events] + [(e, "weekly") for e in weekly_events]
    if not candidates:
        return None
    e, scope = max(candidates, key=lambda c: c[0].date)
    return {"date": e.date.isoformat(), "direction": e.direction, "percent": e.percent, "model": e.model, "scope": scope}


def _build_events(window_events: list, weekly_events: list) -> list[dict]:
    events = [
        {"date": e.date.isoformat(), "kind": "change", "scope": "window",
         "label": f"Window changed {'+' if e.direction == 'increased' else '-'}{e.percent}%"}
        for e in window_events
    ] + [
        {"date": e.date.isoformat(), "kind": "change", "scope": "weekly",
         "label": f"Weekly limit changed {'+' if e.direction == 'increased' else '-'}{e.percent}%"}
        for e in weekly_events
    ]
    return sorted(events, key=lambda ev: ev["date"])


def load_contributed(path: Path | None) -> dict | None:
    """data/contributed.json (tracker/contributed.py) as-is, or None when no path is
    given or the file is absent. An unreadable file is a warning and None: the
    contributed block is a courtesy on top of the publish, never a reason to fail it."""
    if path is None or not Path(path).exists():
        return None
    try:
        block = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"warning: contributed block not published, {path} unreadable: {e}", file=sys.stderr)
        return None
    if not isinstance(block, dict):
        print(f"warning: contributed block not published, {path} is not a JSON object", file=sys.stderr)
        return None
    return block


def write_json(path: Path, obj: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic: a crash mid-write never truncates the previous file


def main(argv: list[str] | None = None, *, post=None, environ=None, now: datetime | None = None) -> int:
    import argparse
    from datetime import timezone
    from .alert import ENV_FILE, _default_post
    from .weight import update_output_weight
    ap = argparse.ArgumentParser(description="Write the public claude-usage.json")
    ap.add_argument("--probes", type=Path, required=True)
    ap.add_argument("--passive", type=Path, required=True, help="history/passive.json from bin/passive.sh")
    ap.add_argument("--effort", type=Path, default=Path("data/effort_matrix.json"))
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"),
                    help="read for every model's price; rewritten when the weekly output run supplies a new output class weight")
    ap.add_argument("--alert-env-file", type=Path, default=ENV_FILE, help="where the refused-weight alert finds its address")
    ap.add_argument("--contributed", type=Path, default=None,
                    help="data/contributed.json from tracker.contributed; carried through as the `contributed` block when present")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    # A missing passive file is allowed (it arrives from masterrig and may lag); an unreadable one is not.
    try:
        probe_rows = load_probes(a.probes)
        # The weekly output run's weight goes into prices.json first, so this publish uses
        # it. Advisory: a bad pair of rows is a warning and the publish carries on with
        # the weight prices.json already holds.
        try:
            update_output_weight(probe_rows, a.prices, post=post or _default_post, environ=environ,
                                 env_file=a.alert_env_file, now=now)
        except (OSError, ValueError, KeyError) as e:
            print(f"warning: output weight not recomputed: {e}", file=sys.stderr)
        passive = json.loads(a.passive.read_text()) if a.passive.exists() else {}
        effort_raw = json.loads(a.effort.read_text())
        if effort_raw.get("_status") == "placeholder":
            raise ValueError(f"{a.effort} is still a placeholder; calibrate it before publishing")
        prices = {k: v for k, v in json.loads(a.prices.read_text()).items() if not k.startswith("_")}
        # The matrix's cells and usd are derived from its stored runs at the prices
        # in force right now, after update_output_weight above may have changed
        # class_weight.output: a committed usd block would be stale the moment the
        # weight moved, and the committed file need not carry one at all. The file
        # itself is never rewritten here; --recompute does that for human readers.
        # Only a matrix with no runs (fixtures) uses its stored cells as-is.
        if effort_raw.get("_meta", {}).get("runs"):
            from .calibrate import recompute
            effort_raw = recompute(effort_raw, prices)
        effort = {k: v for k, v in effort_raw.items() if not k.startswith("_") and k != "usd"}
        effort_usd = {k: v for k, v in effort_raw.get("usd", {}).items() if not k.startswith("_")}
        j = build_public_json(probe_rows, passive, effort, prices, now, effort_usd=effort_usd)
    except (OSError, ValueError, KeyError) as e:
        print(f"publish failed, previous output left in place: {e}", file=sys.stderr)
        return 1
    # Contributed figures sit beside the probe and passive ones; nothing above reads
    # them, so no existing field changes whether or not the block is present.
    contributed = load_contributed(a.contributed)
    if contributed is not None:
        j["contributed"] = contributed
    write_json(a.out, j)
    print(f"wrote {a.out}: {len(j['rates'])} models, last_change={j['last_change']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
