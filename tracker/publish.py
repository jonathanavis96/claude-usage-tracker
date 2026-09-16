"""Assemble the public JSON from probe rows, passive output, and static tables."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

from .capture import ACCEPTED
from .detect import (
    MIN_POOL_D7,
    current_regime_points,
    detect_smoothed_changes,
    detect_weighted_changes,
    pooled_interval,
    weighted_regimes,
)
from .gs_passive import passive_dollar_readings
from .join import bundle_meter_usd
from .passive import PLAN_CHANGE
from .rows import usable_rows
from .weekly import probe_weekly_windows

PLAN_RATIOS_BASE = {"pro": 0.05, "max5": 0.25, "max20": 1.0}
# The published 1:5:20 scaling of ONE five-hour window between plans (Max 5x and Max 20x
# are five and twenty times Pro's per-session usage). It says nothing about how many
# windows a week holds on each plan: that is measured per plan in `weekly_windows`.
PLAN_RATIOS_SOURCE_URL = "https://support.claude.com/en/articles/11049741-what-is-the-max-plan"
# How many five-hour windows a week's cap holds, per plan, relative to max20. NOT
# PLAN_RATIOS_BASE: that one is what a single window is worth in tokens (the published
# 1:5:20), this one is how many windows a week contains, and the two pull in opposite
# directions -- Max 5x holds more windows, Max 20x holds bigger ones.
#
# Frozen, and measured rather than published by Anthropic: it is one meter divided by
# another over the same traffic, so whoever generated the usage cancels out. Three
# independent routes over Jonathan's own 5x-to-20x move agree -- calendar weeks
# 11.02/6.18 = 1.78, the weeks either side of PLAN_CHANGE 11.00/6.58 = 1.67, and the
# per-window medians with the seven-day rounding guard applied 9.86/5.67 = 1.74 (80
# windows against 20). 1.78 is the figure of record.
#
# Restored 2026-09-17 by Jonathan's decision (reverses part of audit finding 6): the
# 2026-09-16 audit deleted this and left `weekly_windows.max5.current`/`.pro.current`
# null, which made the live page's graphs and top figures look wrong against what
# Jonathan had before. max5 is not coming back on this account (see _plan_for_week), so
# the ratio can never be re-measured here; it stays frozen, published beside a live seam
# ratio derived from the two plans' own regimes where both exist (_weekly_block).
WEEKLY_WINDOW_RATIOS = {"pro": 1.78, "max5": 1.78, "max20": 1.0}
MAX_SAMPLE_AGE_DAYS = 10
WEEKLY_CURRENT_DAYS = 14
FIVE_HOURS = timedelta(hours=5)
PLAN_SOURCE_URL = "https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan"
REFERENCE_MIX = json.loads((Path(__file__).resolve().parent.parent / "data" / "reference_mix.json").read_text())


def _model_plan_limits(prices: dict) -> dict:
    """Which plans include each model's usage, and what share of the weekly limit it may use.

    Published beside the rates rather than folded into them, because it is policy,
    not measurement (audit finding 2): Fable is not included in Pro's subscription
    usage, and on Max it may use at most half of the weekly allowance. Every other
    priced model is included on every plan at the whole weekly allowance. A
    consumer multiplying a window figure out to a week applies `weekly_fraction`;
    an `included: false` model has no subscription capacity to show.
    """
    out = {}
    for model in prices:
        fable = model.startswith("claude-fable-")
        out[model] = {}
        for plan in ("pro", "max5", "max20"):
            included = not (fable and plan == "pro")
            out[model][plan] = {
                "included": included,
                "weekly_fraction": (0.5 if fable else 1.0) if included else 0.0,
                "source_url": PLAN_SOURCE_URL,
                "as_of": "2026-09-16",
            }
    return out


def load_probes(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def tokens_usd(tokens: dict, price: dict) -> float:
    """API-list value of a token bundle. `price` is USD per million tokens, per class."""
    value = sum(tokens.get(cls, 0) * price[cls] / 1e6
                for cls in ("input", "output", "cache_read", "cache_write"))
    one_hour = tokens.get("cache_write_1h", 0)
    return value + one_hour * (price.get("cache_write_1h", 2 * price["input"])
                               - price["cache_write"]) / 1e6


def class_weight(price: dict, cls: str) -> float:
    """Meter weight of one token class relative to its list price.

    The 5-hour meter does not charge every token class at its list-price
    ratio: the 2026-09-06 output-heavy Fable probe cost $0.59 of list value
    per 1% against $1.08 for the cache-write-heavy payload, so output tokens
    weigh about 1.8x their list price (see docs/spike-2026-09.md). A missing
    class_weight, or a class missing from it, defaults to 1.0 (cache_write,
    the class the Sonnet invariant probe is made of).
    """
    weights = price.get("class_weight", {})
    if cls == "cache_write_1h":
        return weights.get(cls, weights.get("cache_write", 1.0))
    return weights.get(cls, 1.0)


def meter_usd(tokens: dict, price: dict) -> float:
    """Meter-dollar value of a token bundle: list value per class x class_weight."""
    value = sum(tokens.get(cls, 0) * price[cls] * class_weight(price, cls) / 1e6
                for cls in ("input", "output", "cache_read", "cache_write"))
    one_hour = tokens.get("cache_write_1h", 0)
    return value + one_hour * (
        price.get("cache_write_1h", 2 * price["input"])
        * class_weight(price, "cache_write_1h")
        - price["cache_write"] * class_weight(price, "cache_write")
    ) / 1e6


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
    value = sum(split.get(cls, 0) * price[cls] * class_weight(price, cls) / 1e6
                for cls in ("input", "output", "cache_read", "cache_write"))
    one_hour = split.get("cache_write_1h", 0)
    return value + one_hour * (
        price.get("cache_write_1h", 2 * price["input"]) * class_weight(price, "cache_write_1h")
        - price["cache_write"] * class_weight(price, "cache_write")
    ) / 1e6


def blended_api_price_per_token(split: dict, price: dict) -> float:
    """API list dollars per token for a declared reference mix."""
    value = sum(split.get(cls, 0) * price[cls] / 1e6
                for cls in ("input", "output", "cache_read", "cache_write"))
    return value + split.get("cache_write_1h", 0) * (
        price.get("cache_write_1h", 2 * price["input"]) - price["cache_write"]
    ) / 1e6


def _eligible_stretches(gs_passive: dict | None, prices: dict, *, allow_legacy_unverified: bool) -> list[dict]:
    """The stretch records passive_dollar_readings reads, for the evidence published beside a rate."""
    out = []
    for acct in (gs_passive or {}).get("accounts", {}).values():
        for st in acct.get("stretches", []):
            if st.get("status") != ACCEPTED or st.get("unpriced_tokens", 0) > 0:
                continue
            if st.get("reset_verified") is not True and not allow_legacy_unverified:
                continue
            if any(bundle_meter_usd(m, tok, prices) is None for m, tok in st.get("tokens", {}).items()):
                continue
            out.append({**st, "_account": acct.get("account")})
    return out


def _reference_tokens(budget: float | None, meter_usd_per_token: float) -> int | None:
    """Tokens of the reference mix a meter budget buys; None when there is no budget or no price."""
    return round(budget / meter_usd_per_token) if budget is not None and meter_usd_per_token > 0 else None


def build_public_json(probe_rows: list[dict], passive: dict, effort: dict, prices: dict, now: datetime,
                      effort_usd: dict | None = None, gs_passive: dict | None = None,
                      reference_mix: dict | None = None) -> dict:
    """The public JSON, schema_version 2 (the 2026-09-16 audit's implementation contract).

    Rates. The measured quantity is the meter budget: the meter dollars (list
    price x class_weight x meter_weight, tracker/join.py bundle_meter_usd) one
    full five-hour window holds, read from the gs accounts' passive stretches
    (tracker/gs_passive.py) and pooled per account per UTC day. It is published
    as `meter_budget_per_window`. Everything per model is derived from it on a
    frozen, versioned reference token mix (data/reference_mix.json): for mix s,
    `tokens_per_window` = budget / sum(s[c] x price[c] x class_weight[c]) /
    meter_weight, and `api_value_per_window` (also `api_list_value_per_window`)
    is the API list value of exactly those tokens, cache reads included. Until
    2026-09-16 `api_value_per_window` held the meter budget itself, which is a
    different unit whenever a class weight is not 1 (audit finding 1). The mix is
    frozen so that a change in how the watched accounts happen to work does not
    restate every token figure (finding 11); per-model numbers are conversions
    under it, not measurements of each model's cap, and `assumptions` says so.
    `reference_mix` defaults to this checkout's data/reference_mix.json; an
    offline rebuild passes its archive's own (tracker/rebuild_offline.py).

    Instruments. Only passive readings are published, and only from stretches
    whose meter log carried reset ids (`reset_verified`); those are "measured"
    and the only ones change detection runs on. With none, stretches from a
    reset-less log are used as a "conditional" reference with no change events
    (finding 10). With neither, every rate field is null and `availability`
    says why. Probe rows never stand in for missing passive readings (finding
    13): they read the same meter on another scale. Capture completeness cannot
    be proven from one host's transcripts, so every passive rate is `quality.status`
    "conditional" with its reasons, and `evidence` carries what the current value
    rests on: its readings, stretches, meter movement, window pieces and how many
    stretches the capture check diagnosed (published anyway, CAPTURE_GATE off).

    History and `meter_budget_per_window` (restored 2026-09-17, reversing findings 11
    and 12 by Jonathan's decision: the raw daily series he saw on the live page was
    "so much up and down instead of accurate" against what he had before). The public
    `history` is a step function of the measured limit, not the raw daily series:
    Jonathan's own passive account is noisy 2x-5x day to day (the rolling meter
    decays), and the public chart must show only what change detection actually
    found, not that noise. detect_smoothed_changes splits the whole dollar series
    into regimes; each regime is held flat at the median of the readings inside it,
    and every model's tokens_per_window for that regime is that same median divided
    by the model's own blended price times its own meter_weight -- so every model
    steps on the same dates, just at different levels. Days before the first
    reading (when passive.json's own legacy `history` reaches further back) are
    held flat at the first regime's value with `source: "held"`: that legacy record
    certifies no step larger than what it already shows happened before the gs
    passive readings started, so nothing in that gap can be measured, only held.
    A day's `source` is "passive" when the series has a reading that day, "derived"
    when the day falls in a regime but has no reading of its own, and "held" for
    the pre-first-reading fill; a day's `quality` is its own reading's
    ("measured"/"legacy_reset_unverified") where it has one, else the regime's
    (whether any reading feeding that regime was verified). `interpolated` stays
    false everywhere -- it is a step function, not an interpolation.

    `rates[model]["meter_budget_per_window"]`/`tokens_per_window`/dollar figures
    are the CURRENT regime's held value, the same figure the history's newest row
    for that model carries (the hero shows `rates`, the chart shows `history`; they
    must agree) -- not a rolling trailing-week median as finding 5 introduced.
    `evidence`/`quality`/`freshness` keep finding 16's shape and are unaffected.
    Freshness is per metric (`freshness.stale` once the newest reading is older
    than MAX_SAMPLE_AGE_DAYS), not a refusal to publish (finding 16).

    Weekly windows, events and plan limits: see _weekly_block, _event_record and
    _model_plan_limits.
    """
    if not prices:
        raise ValueError("price table has no models")
    probe_weekly = probe_weekly_windows(probe_rows, now=now)
    # Output rows and outliers still stay out of everything the probe rows supply
    # (probed_at, probe_effort, account counts; tracker/rows.py).
    probe_rows = usable_rows(probe_rows)

    all_readings = passive_dollar_readings(gs_passive, prices, by="day", allow_legacy_unverified=True) if gs_passive else []
    verified_readings = passive_dollar_readings(gs_passive, prices, by="day") if gs_passive else []
    evidence_status = "measured" if verified_readings else ("conditional" if all_readings else "unavailable")
    series = sorted(verified_readings if verified_readings else all_readings)
    events = detect_smoothed_changes(series) if evidence_status == "measured" else []
    regime_start = max((e.date for e in events), default=None)

    measured_at = series[-1][0] if series else None
    current = [(ts, v) for ts, v in series
               if (regime_start is None or ts.date() >= regime_start) and ts >= measured_at - timedelta(days=7)]
    stale = measured_at < now - timedelta(days=MAX_SAMPLE_AGE_DAYS) if measured_at else None

    stretches = _eligible_stretches(gs_passive, prices, allow_legacy_unverified=evidence_status == "conditional")
    if current:
        since = current[0][0].date().isoformat()
        stretches = [st for st in stretches if st["end"][:10] >= since]
    else:
        stretches = []
    movement = sum(float(st.get("delta_pct", 0)) for st in stretches)
    pieces = sum(int(st.get("windows", 1)) for st in stretches)
    reasons = {"measured": ["capture_completeness_not_verified"],
               "conditional": ["legacy_reset_metadata_missing", "capture_completeness_not_verified"],
               "unavailable": ["no_eligible_passive_measurement"]}[evidence_status]
    if stale:
        reasons = [*reasons, "evidence_stale"]
    evidence = {"source": "passive" if series else None,
                "reset_verified": evidence_status == "measured",
                "observed_from": current[0][0].isoformat() if current else None,
                "observed_to": measured_at.isoformat() if measured_at else None,
                "readings": len(current),
                "account_count": len({st["_account"] for st in stretches}),
                "stretches": len(stretches),
                "meter_movement_pct": round(movement, 1),
                "window_pieces": pieces,
                "rounding_relative": round(pieces / movement, 4) if movement else None,
                "capture_diagnosed": sum(1 for st in stretches if st.get("capture_status") not in (None, ACCEPTED))}
    quality = {"status": "unavailable" if evidence_status == "unavailable" else "conditional",
               "reasons": reasons, "capture_complete": None,
               "unpriced_work": False if series else None}
    freshness = {"as_of": measured_at.isoformat() if measured_at else None,
                 "stale_after": (measured_at + timedelta(days=MAX_SAMPLE_AGE_DAYS)).isoformat() if measured_at else None,
                 "stale": stale}

    latest_row = max(probe_rows, key=lambda r: r["ts"]) if probe_rows else None
    latest_row_per_model: dict[str, dict] = {}
    for r in probe_rows:
        m = r["model"]
        if m not in latest_row_per_model or r["ts"] > latest_row_per_model[m]["ts"]:
            latest_row_per_model[m] = r

    reference_mix = reference_mix or REFERENCE_MIX
    mix = dict(reference_mix["split"])
    verified_days = {ts.date() for ts, _ in verified_readings}
    # A day with verified readings publishes those alone; any other day publishes its
    # reset-less readings, labelled. The verified readings are taken from
    # verified_readings itself: all_readings pools an account's verified and
    # reset-less stretches of one day into a single value, so it cannot supply them.
    if evidence_status == "measured":
        day_readings = verified_readings + [(ts, v) for ts, v in all_readings if ts.date() not in verified_days]
    else:
        day_readings = series
    by_day: dict[date, list[float]] = {}
    for ts, v in day_readings:
        by_day.setdefault(ts.date(), []).append(v)

    # The step function (restored 2026-09-17, findings 11/12): every event
    # detect_smoothed_changes found over the whole series, not just the newest one,
    # bounds a regime. `day_readings` (broader than `series`: it fills a measured
    # day's gap with a legacy reading, see above) is bucketed into those regimes so
    # a regime's held value pools everything published for its span.
    regime_starts = sorted({e.date for e in events})

    def regime_index(d: date) -> int:
        idx = 0
        for rd in regime_starts:
            if d >= rd:
                idx += 1
            else:
                break
        return idx

    regime_values: dict[int, list[float]] = {}
    regime_dates: dict[int, list[date]] = {}
    for ts, v in day_readings:
        idx = regime_index(ts.date())
        regime_values.setdefault(idx, []).append(v)
        regime_dates.setdefault(idx, []).append(ts.date())
    regime_quality = {idx: ("measured" if any(dd in verified_days for dd in dates) else "legacy_reset_unverified")
                      for idx, dates in regime_dates.items()}

    last_day = now.date()
    # Days before the first reading are held at the first regime's value: passive.json's
    # own legacy `history` (tracker/passive.py, masterrig's transcript-derived daily
    # rates, predating gs_passive) can reach further back than the first gs_passive
    # reading and is the only other source of an earlier boundary -- never probe rows
    # (finding 13 stays).
    first_reading_day = min((ts.date() for ts, _ in day_readings), default=None)
    first_day = first_reading_day
    if first_reading_day is not None:
        # Days before the first reading are held at the first regime's value (above);
        # a legacy `history` date earlier than that pushes the held fill back further.
        # With no reading at all there is no regime to hold at, so the legacy date is
        # never used on its own -- history stays empty, as it already does today.
        legacy_history_days = [date.fromisoformat(ds) for ds in passive.get("history", {})]
        if legacy_history_days:
            first_day = min(first_reading_day, min(legacy_history_days))

    current_regime_idx = None
    if day_readings:
        # `now` may fall before the newest reading's own day (a test fixture, or a
        # publish run against stale readings): hold at the newest regime that
        # actually has evidence, which is what the history's own newest row shows.
        current_regime_idx = _regime_with_evidence(regime_index(last_day), regime_values)
    regime_current_value = median(regime_values[current_regime_idx]) if current_regime_idx is not None else None

    rates, history = {}, {}
    for model, price in prices.items():
        per_token = blended_price_per_token(mix, price) * price.get("meter_weight", 1.0)
        api_per_token = blended_api_price_per_token(mix, price)

        window_tokens = _reference_tokens(regime_current_value, per_token)
        model_probe = latest_row_per_model.get(model)
        rates[model] = {
            "meter_budget_per_window": round(regime_current_value, 2) if regime_current_value is not None else None,
            "tokens_per_window": window_tokens,
            "api_value_per_window": round(window_tokens * api_per_token, 2) if window_tokens is not None else None,
            "api_list_value_per_window": round(window_tokens * api_per_token, 2) if window_tokens is not None else None,
            "source": "derived_reference_mix" if window_tokens is not None else "unavailable",
            "split": mix,
            "reference_mix": {k: reference_mix[k] for k in ("id", "kind", "source", "as_of", "cache_write_duration")},
            "assumptions": {
                "direct_model_cap_measurement": False,
                "model_conversion": "meter budget converted on the reference mix with this model's price-table weights",
                "meter_weight": price.get("meter_weight", 1.0),
                "class_weight": {c: class_weight(price, c)
                                 for c in ("input", "output", "cache_read", "cache_write", "cache_write_1h")},
                "cache_write_1h_price_default": "2x input when the price table has no cache_write_1h",
                "cache_write_1h_meter_weight": "assumed equal to cache_write's",
            },
            "evidence": evidence,
            "quality": quality,
            "freshness": freshness,
            "measured_at": measured_at.isoformat() if measured_at else None,
            "probed_at": model_probe["ts"] if model_probe is not None else None,
            "probe_effort": latest_row["effort"] if latest_row is not None else None,
        }
        hist = []
        if first_day is not None:
            d = first_day
            while d <= last_day:
                idx = _regime_with_evidence(regime_index(d), regime_values)
                budget = median(regime_values[idx])
                day_tokens = _reference_tokens(budget, per_token)
                day_api = round(day_tokens * api_per_token, 2) if day_tokens is not None else None
                if d in by_day:
                    day_source, day_quality = "passive", ("measured" if d in verified_days else "legacy_reset_unverified")
                elif first_reading_day is not None and d < first_reading_day:
                    day_source, day_quality = "held", regime_quality.get(idx, "legacy_reset_unverified")
                else:
                    day_source, day_quality = "derived", regime_quality.get(idx, "legacy_reset_unverified")
                hist.append({"date": d.isoformat(), "meter_budget_per_window": round(budget, 2),
                             "tokens_per_window": day_tokens, "api_value_per_window": day_api,
                             "api_list_value_per_window": day_api, "source": day_source,
                             "quality": day_quality, "readings": len(by_day.get(d, [])), "interpolated": False})
                d += timedelta(days=1)
        history[model] = hist

    weekly_windows, weekly_events, weekly_window_ratios = _weekly_block(passive.get("weekly_windows"), probe_weekly, now)
    return {
        "schema_version": 2,
        "generated_at": now.isoformat(),
        "last_sample_at": measured_at.isoformat() if measured_at else None,
        "meter_read_at": _last_meter_read(gs_passive),
        "passive_generated_at": passive.get("generated_at"),
        "plan_measured": "max20",
        "instrument": "passive" if series else "unavailable",
        "availability": {"rates": quality["status"], "evidence": evidence_status,
                         "reason": {"measured": None, "conditional": "legacy_reset_metadata_missing",
                                    "unavailable": "no_eligible_passive_measurement"}[evidence_status]},
        "probe_account_count": len({r["account"] for r in probe_rows if r.get("account")}),
        "passive_account_count": evidence["account_count"],
        "plan_ratios": dict(PLAN_RATIOS_BASE),
        "plan_ratios_basis": {"kind": "published_plan_scaling", "scope": "one five-hour window",
                              "source_url": PLAN_RATIOS_SOURCE_URL},
        "weekly_window_ratios": weekly_window_ratios,
        "rate_basis": "meter_budget",
        "model_plan_limits": _model_plan_limits(prices),
        "rates": rates,
        "effort": effort,
        "effort_usd": effort_usd or {},
        "api_price_per_mtok": prices,
        "history": history,
        "weekly_windows": weekly_windows,
        "last_change": _latest_change_with_scope(events, weekly_events),
        "events": _build_events(events, weekly_events),
        # Restored 2026-09-17 (reverses finding 11 by Jonathan's decision): the median
        # session token total per model, from passive.py's own transcript-derived
        # daily_rates (tracker/turns.py session_tokens_by_model), unrelated to the
        # frozen reference mix above -- it feeds the page's "about N sessions" line,
        # not a token-figure conversion.
        "session_tokens": passive.get("session_tokens", {}),
    }


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


def _repaired(row: dict) -> bool:
    """Whether a passive.json weekly row or window point was paired after the finding-3 repair.

    tracker/weekly.py adds `pieces` from that repair on. A row without it came
    from pairing that dropped seven-day movement whenever the five-hour meter
    held still, and dropped every pair across the weekly reset's jitter: it is
    kept as legacy evidence, but nothing is computed from it.
    """
    return "pieces" in row


def _weekly_current(history: list[dict], now: datetime) -> float | None:
    """Median of the last two complete weeks' `windows` figure.

    Restored 2026-09-17 by Jonathan's decision (reverses part of audit finding 6):
    `weekly_windows.max5.current` and `.pro.current` (max5's own figure, `assumed`)
    carry this again, computed from each plan's own weekly rows rather than left
    null. max20 does not fall back to this any more; it has its own certified
    `_regime_current` from the per-window series.
    """
    complete = [h for h in history if date.fromisoformat(h["week_ending"]) < now.date()]
    return round(median(h["windows"] for h in complete[-2:]), 2) if complete else None


def _weekly_block(passive_weekly: dict | None, probe_weekly: dict, now: datetime) -> tuple[dict, list, dict]:
    """(`weekly_windows`, weekly change events, `weekly_window_ratios`): per plan, each
    with its own evidence.

    `max20` is the live plan. Its `current` is the pooled ratio of the current
    regime's trailing fortnight of reset-verified, repaired passive window points
    (_regime_current), with its rounding interval, evidence and staleness in
    `current_estimate`; its `regimes` and the weekly events come from
    tracker/detect.py on the same points. `max5` is history only for regimes and
    events -- its last regime begins before PLAN_CHANGE, a date taken from the
    account owner's record rather than from independent metadata, so the plan move
    itself cannot be told apart from a limit change there; `plan_change` says so.
    Its `current` (restored 2026-09-17, reverses part of finding 6 by Jonathan's
    decision) is `_weekly_current` on its own weekly rows -- the account is not
    going back to Max 5x, so this can never be re-measured, but the page needs a
    figure and the old median is what it showed. `pro` has no measurement of its
    own; its `current` is max5's own figure, `assumed: true`, the same gap-fill
    pre-2026-09-16 used. `weekly_window_ratios` is `WEEKLY_WINDOW_RATIOS` (frozen),
    overridden with the live seam between max5's best-supported regime (most points) and max20's first
    where both exist -- the ratio is windows-per-week, a different quantity from
    `plan_ratios` (one window's worth), and the page must never confuse the two.
    Probe runs' weekly rows are published as their own series and never replace
    passive weeks (finding 13).
    """
    passive_weekly = dict(passive_weekly or {"current": None, "history": [], "by_window": []})
    # passive.json may lag: it can predate the flag, or carry a `partial` from when its
    # newest week was still open. Recompute it against this publish's own time.
    passive_weekly["history"] = [dict(h, partial=date.fromisoformat(h["week_ending"]) >= now.date())
                                 for h in passive_weekly.get("history", [])]
    rows = [dict(h) for h in passive_weekly["history"]]
    for h in rows:
        h.update({"source": "passive_paired_deltas", "assumed": False}
                 | ({"quality": "measured", "reasons": []} if _repaired(h)
                    else {"quality": "legacy_uncertain", "reasons": ["paired_before_denominator_repair"]}))
    points = passive_weekly.get("by_window", [])
    legacy_only = bool(points) and not any(_repaired(p) for p in points)
    max20_points = _max20_window_points(points)
    max5_points = _max5_window_points(points)
    events = detect_weighted_changes(max20_points)
    estimate = _regime_current(max20_points, now)

    def regimes(pts: list) -> list[dict]:
        return [dict(r, source="passive_paired_deltas", assumed=False) for r in weighted_regimes(pts)]

    if estimate is not None:
        max20_availability = {"status": "measured", "reason": "evidence_stale" if estimate["stale"] else None}
    elif legacy_only:
        max20_availability = {"status": "unavailable", "reason": "passive_weekly_windows_predate_paired_delta_repair"}
    elif not max20_points:
        max20_availability = {"status": "unavailable", "reason": "no_paired_meter_data"}
    else:
        max20_availability = {"status": "unavailable", "reason": "insufficient_seven_day_movement"}
    probe = dict(probe_weekly, history=[dict(h, source="probe_paired_deltas", assumed=False)
                                        for h in probe_weekly["history"]])
    max5_history = [h for h in rows if _plan_for_week(h["week_ending"]) == "max5"]
    max20_regimes, max5_regimes = regimes(max20_points), regimes(max5_points)
    max5_current = _weekly_current(max5_history, now)
    ratios = dict(WEEKLY_WINDOW_RATIOS)
    if max5_regimes and max20_regimes and max20_regimes[0]["windows"]:
        # Max 5x's level is its best-supported regime, not whichever comes last: the
        # detector splits a four-day 6.6 tail off the plan move (14-18 Aug, the
        # PLAN_CHANGE date question), and a seam taken from that tail reads 1.02
        # where the account's Max 5x era ran 10.9 against Max 20x's 6.5.
        max5_level = max(max5_regimes, key=lambda r: r.get("points", 0))["windows"]
        seam = max5_level / max20_regimes[0]["windows"]
        ratios["max5"] = ratios["pro"] = round(seam, 3)
    block = {
        "max20": {"current": estimate["value"] if estimate else None, "current_estimate": estimate,
                  "history": [h for h in rows if _plan_for_week(h["week_ending"]) == "max20"],
                  "regimes": max20_regimes, "assumed": False, "availability": max20_availability},
        "max5": {"current": max5_current, "current_estimate": None,
                 "history": max5_history, "regimes": max5_regimes, "assumed": False,
                 "availability": {"status": "historical_only", "reason": "no_current_max5_measurement"},
                 "plan_change": {"date": PLAN_CHANGE.isoformat(), "source": "account owner's record",
                                 "independently_verified": False}},
        "pro": {"current": max5_current, "current_estimate": None, "history": [], "regimes": [], "assumed": True,
                "availability": {"status": "unavailable", "reason": "no_pro_measurement"}},
        "passive": passive_weekly,
        "probe": probe,
    }
    return block, events, ratios


def _window_points(passive_points: list[dict], keep) -> list[tuple[datetime, float, float, int]]:
    points = [(datetime.fromisoformat(p["window_ending"]), p["five_hour_pct"], p["seven_day_pct"], p["pieces"])
              for p in passive_points
              if _repaired(p) and p.get("reset_verified") is True and keep(datetime.fromisoformat(p["window_ending"]))]
    return sorted(points, key=lambda p: p[0])


def _max20_window_points(passive_points: list[dict]) -> list[tuple[datetime, float, float, int]]:
    """The live plan's per-window series as (window_ending, d5, d7, pieces), oldest first.

    Passive windows are Jonathan's own account, so only those that START
    after PLAN_CHANGE are max20: a window straddling or preceding the plan
    change would read at the Max 5x ratio (about 11 against 6.5) and, as the
    first base of the max20 series, would read Jonathan's own plan move as a
    change. The whole of PLAN_CHANGE day is excluded, not just windows before
    it, because the change is dated to a day and not an hour. Only points
    paired after the finding-3 repair (_repaired) with recorded reset ids count.

    The probe runs' own per-window points (`weekly_windows.probe.by_window`)
    are published for the record but deliberately NOT pooled in here, even
    though the probe accounts are on the same plan: a 3-tick run moves the
    seven-day meter by one whole point or none, so each of its points is
    almost pure rounding, and they are another instrument.
    """
    return _window_points(passive_points, lambda ending: (ending - FIVE_HOURS).date() > PLAN_CHANGE)


def _regime_with_evidence(idx: int, regime_values: dict) -> int:
    """The regime index to hold a day at: `idx` itself when readings landed in it,
    else the newest earlier regime with readings, else the oldest one there is.

    A day can index a regime with no readings when the publish time precedes
    the newest reading's day, or when held days before the first reading fall
    in a bucket the detector opened on that same day. Holding at the nearest
    evidenced regime keeps the history a step function of measured levels.
    """
    if idx in regime_values:
        return idx
    earlier = [k for k in regime_values if k < idx]
    return max(earlier) if earlier else min(regime_values)


def _max5_window_points(passive_points: list[dict]) -> list[tuple[datetime, float, float, int]]:
    """The frozen plan's per-window series, the mirror of _max20_window_points.

    Only windows that END on or before PLAN_CHANGE: a window straddling the
    change mixes both plans' ratios and belongs to neither, the same rule
    _plan_for_week applies to weeks.
    """
    return _window_points(passive_points, lambda ending: ending.date() <= PLAN_CHANGE)


def _regime_current(points: list[tuple], now: datetime) -> dict | None:
    """max20's current estimate: the current regime's newest WEEKLY_CURRENT_DAYS of window points, pooled.

    Pooled (total d5 over total d7) and bounded by the regime, so once a change is
    certified `current` is the post-change level instead of a blend of both.
    None when that fortnight holds less than MIN_POOL_D7 of seven-day movement.
    `stale` is judged against the publish time, not against the newest point, so
    a weekly log that has stopped arriving is visible as such (audit finding 16).
    """
    regime = current_regime_points(points)
    if not regime:
        return None
    since = regime[-1][0] - timedelta(days=WEEKLY_CURRENT_DAYS)
    recent = [p for p in regime if p[0] >= since]
    d5, d7 = sum(p[1] for p in recent), sum(p[2] for p in recent)
    if d7 < MIN_POOL_D7:
        return None
    lo, hi = pooled_interval(recent)
    newest = recent[-1][0]
    stale = newest < now - timedelta(days=WEEKLY_CURRENT_DAYS)
    return {"value": round(d5 / d7, 2), "rounding_interval": [round(x, 4) if x is not None else None for x in (lo, hi)],
            "five_hour_pct": round(d5, 1), "seven_day_pct": round(d7, 1), "points": len(recent),
            "pieces": sum(p[3] for p in recent), "from": recent[0][0].isoformat(), "as_of": newest.isoformat(),
            "stale": stale, "source": "passive_paired_deltas", "assumed": False,
            "quality": "measured", "reasons": ["evidence_stale"] if stale else []}


def _last_meter_read(gs_passive: dict | None) -> str | None:
    """When a watched account's meter was last read, newest across accounts.

    This is not `last_sample_at`, which is the end of the newest completed
    measurement: a stretch closes at an idle bound or a window end, so the meter
    is read every few minutes while the figure derived from it can be many hours
    older. The page labels a pill "Last sample", which a reader takes to mean the
    reading, not the derivation, and neither the old field nor
    `passive_generated_at` (when a file was rebuilt, on another host) answers that.
    """
    stamps = [
        (a.get("meter") or {}).get("last")
        for a in ((gs_passive or {}).get("accounts") or {}).values()
    ]
    usable = [t for t in stamps if isinstance(t, str) and t]
    return max(usable) if usable else None


def _latest_change_with_scope(window_events: list, weekly_events: list) -> dict | None:
    """The most recent change across both event series, as its full event record."""
    candidates = [(e, "window") for e in window_events] + [(e, "weekly") for e in weekly_events]
    if not candidates:
        return None
    e, scope = max(candidates, key=lambda c: c[0].date)
    return _event_record(e, scope)


def _event_record(e, scope: str) -> dict:
    """One published change: an observed change in this account's metric, not a dated policy change.

    `onset` bounds when it happened (the last reading at the old level and the
    first at the new one), `confirmation` says by when the evidence after it
    alone was enough and how much there was. Weekly events are certified against
    both levels' rounding intervals (tracker/detect.py); window events have no
    uncertainty model behind them, so they are `provisional`, and bin/daily.sh
    announces neither a provisional nor a legacy-uncertain change.
    """
    provisional = scope == "window"
    return {
        "date": e.date.isoformat(), "direction": e.direction, "percent": e.percent, "model": e.model,
        "scope": scope, "metric": "meter_budget_per_window" if scope == "window" else "weekly_to_five_hour_ratio",
        "observation_scope": "account", "attribution": "observed_account_metric_change",
        "onset": {"earliest": e.onset_earliest.isoformat() if e.onset_earliest else None,
                  "latest": (e.onset_latest or e.date).isoformat()},
        "confirmation": {"at": e.confirmed_at.isoformat() if e.confirmed_at else None,
                         "evidence_points": e.evidence_points, "seven_day_pct": e.denominator_pct},
        "rounding_interval_before": _rounded(e.before_interval),
        "rounding_interval_after": _rounded(e.after_interval),
        "evidence_quality": "provisional" if provisional else "certified",
        "provisional": provisional, "legacy_uncertain": False,
    }


def _rounded(interval: tuple | None) -> list | None:
    return [round(x, 4) if x is not None else None for x in interval] if interval else None


def _build_events(window_events: list, weekly_events: list) -> list[dict]:
    events = [{**_event_record(e, "window"), "kind": "change",
               "label": f"Observed window budget changed {'+' if e.direction == 'increased' else '-'}{e.percent}%"}
              for e in window_events]
    events += [{**_event_record(e, "weekly"), "kind": "change",
                "label": f"Observed weekly/window ratio changed {'+' if e.direction == 'increased' else '-'}{e.percent}%"}
               for e in weekly_events]
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


def load_gs_passive(path: Path | None) -> dict | None:
    """tracker.gs_passive's report() JSON (bin/daily.sh's history/gs-passive.json), or None
    when no path is given or the file is absent. An unreadable file is a warning and None,
    like --contributed: a bad or missing gs-passive run must never fail the publish, since
    the probe series (or a previous gs-passive.json) can still carry the day."""
    if path is None or not Path(path).exists():
        return None
    try:
        block = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"warning: gs-passive block not published, {path} unreadable: {e}", file=sys.stderr)
        return None
    if not isinstance(block, dict):
        print(f"warning: gs-passive block not published, {path} is not a JSON object", file=sys.stderr)
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
    ap.add_argument("--gs-passive", type=Path, default=None, dest="gs_passive",
                    help="history/gs-passive.json from tracker.gs_passive (issue #39); with any priced reading it IS "
                         "the published dollar series (probe rows only fill in). Missing or unreadable is a warning, not a failure")
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
        prices_raw = json.loads(a.prices.read_text())
        prices = {k: v for k, v in prices_raw.items() if not k.startswith("_")}
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
        gs_passive = load_gs_passive(a.gs_passive)
        j = build_public_json(probe_rows, passive, effort, prices, now, effort_usd=effort_usd, gs_passive=gs_passive)
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
