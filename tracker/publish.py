"""Assemble the public JSON from probe rows, passive output, and static tables."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

from .capture import ACCEPTED
from .detect import (
    MIN_POOL_D7,
    ChangeEvent,
    current_regime_points,
    detect_smoothed_changes,
    detect_weighted_changes,
    pooled_interval,
    weighted_regimes,
)
from .gs_passive import passive_dollar_readings
from .join import bundle_meter_usd
from .passive import PLAN_CHANGE, PLAN_CHANGE_AT
from .rows import usable_rows
from .weekly import _iso_week_ending, probe_weekly_windows

# The credits table (she-llac.com/claude-limits, undated) gives each plan's credits per
# five-hour window and per week. One window is worth 1 : 6 : 20 between Pro, Max 5x and
# Max 20x; that says nothing about how many windows a week holds on each plan, which is
# WEEKLY_WINDOW_RATIOS below. A documented figure is a reference to compare a
# measurement against, never a substitute for one.
CREDITS_TABLE_URL = "https://she-llac.com/claude-limits"
CREDITS_PER_WINDOW = {"pro": 550_000, "max5": 3_300_000, "max20": 11_000_000}
CREDITS_PER_WEEK = {"pro": 5_000_000, "max5": 41_666_700, "max20": 83_333_300}
PLAN_RATIOS_BASE = {"pro": 0.05, "max5": 0.30, "max20": 1.0}
PLAN_RATIOS_BASIS = {"kind": "credits_table", "scope": "one five-hour window",
                     "credits_per_window": dict(CREDITS_PER_WINDOW),
                     "source_url": CREDITS_TABLE_URL, "dated": False}
# How many five-hour windows a week's cap holds, per plan, relative to max20. NOT
# PLAN_RATIOS_BASE: that one is what a single window is worth (the 1 : 6 : 20 above),
# this one is how many windows a week contains, and the two pull in opposite
# directions -- Max 5x holds more windows, Max 20x holds bigger ones. From the credits
# table: a week holds 9.09 / 12.63 / 7.58 windows on Pro / Max 5x / Max 20x, so relative
# to Max 20x that is 1.20 and 1.667. The tracker's own measurement confirms the Max 5x
# figure: 10.85 windows a week measured on Max 5x (Jun-Aug) over 6.54 on Max 20x (19 Aug
# to 11 Sep) is 1.66. Max 5x and Pro are not measured any more on any watched account,
# so their weekly `current` is the live Max 20x figure scaled by these, marked inferred.
DOCUMENTED_WINDOWS_PER_WEEK = {"pro": 9.09, "max5": 12.63, "max20": 7.58}
WEEKLY_WINDOW_RATIOS = {"pro": 1.20, "max5": 1.667, "max20": 1.0}
WEEKLY_WINDOW_RATIOS_BASIS = {
    "kind": "credits_table", "scope": "five-hour windows per week, relative to max20",
    "credits_per_week": dict(CREDITS_PER_WEEK),
    "documented_windows_per_week": dict(DOCUMENTED_WINDOWS_PER_WEEK),
    "source_url": CREDITS_TABLE_URL, "dated": False,
    "measured_confirmation": {"max5_over_max20": 1.66,
                              "spans": "Max 5x Jun-Aug over Max 20x 19 Aug-11 Sep"},
}
# The watched Max 20x accounts, in the fixed order their published labels follow. Names
# are never published: the JSON carries `a1`, `a2`, `a3` and counts. masterrig's points
# are history/passive.json's `weekly_windows.by_window` (its own meter log); the others'
# are history/gs-passive.json's `accounts.<name>.weekly_by_window`, the same point shape.
ACCOUNT_LABELS = (("masterrig", "a1"), ("jwork", "a2"), ("dave", "a3"))
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

    weekly_windows, weekly_events = _weekly_block(passive.get("weekly_windows"), probe_weekly, now,
                                                  gs_passive=gs_passive)
    # Accounts behind the passive evidence: those with an accepted stretch (the rates)
    # plus those with Max 20x window points (the weekly series), by name, never published.
    weekly_accounts = {name for name, label in ACCOUNT_LABELS
                       if weekly_windows["max20"]["by_account"][label]["n"]}
    passive_accounts = {st["_account"] for st in stretches} | weekly_accounts
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
        "passive_account_count": len(passive_accounts),
        "plan_ratios": dict(PLAN_RATIOS_BASE),
        "plan_ratios_basis": json.loads(json.dumps(PLAN_RATIOS_BASIS)),
        "weekly_window_ratios": dict(WEEKLY_WINDOW_RATIOS),
        "weekly_window_ratios_basis": json.loads(json.dumps(WEEKLY_WINDOW_RATIOS_BASIS)),
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


def _regimes(points: list[tuple]) -> list[dict]:
    """tracker/detect.py's regimes over a per-window series, tagged with their source."""
    return [dict(r, source="passive_paired_deltas", assumed=False)
            for r in weighted_regimes(points)]


def _account_window_dicts(passive_points: list[dict],
                          gs_passive: dict | None) -> dict[str, list[dict]]:
    """Each watched Max 20x account's own per-window points, keyed by published label.

    masterrig (`a1`) is history/passive.json's own `weekly_windows.by_window`;
    jwork (`a2`) and dave (`a3`) are history/gs-passive.json's
    `accounts.<name>.weekly_by_window`, the same point shape from the same pairing
    code. Account names are never published: the labels are fixed by
    ACCOUNT_LABELS and the JSON carries only those and counts.

    Every account is restricted to the Max 20x era, windows that START at or after
    PLAN_CHANGE_AT. masterrig ran Max 5x before that, and a window straddling or
    preceding the plan change reads at the Max 5x ratio (about 11 against 6.5),
    which as the first base of the max20 series would read Jonathan's own plan
    move as a change. The split is on PLAN_CHANGE_AT, the precise seam within
    PLAN_CHANGE day where the meter's ratio actually drops (the last Max 5x window
    ends 16:20 UTC), not on the whole day; a window straddling that seam is
    dropped from both plans, the mirror of _plan_for_week's rule for weeks. Only
    points paired after the finding-3 repair (_repaired) count.

    The gs accounts' points are NOT required to be reset-verified. masterrig's log
    names every window's reset, but jwork's names only the newest few (56 of its 62
    points carry no reset id), so requiring it would drop the second account
    altogether. Each published point keeps its own `reset_verified` flag.

    The probe runs' own per-window points (`weekly_windows.probe.by_window`) are
    published for the record but deliberately NOT pooled in here, even though the
    probe accounts are on the same plan: a 3-tick run moves the seven-day meter by
    one whole point or none, so each of its points is almost pure rounding, and
    they are another instrument.
    """
    era = [p for p in passive_points
           if _repaired(p) and p.get("reset_verified") is True
           and (datetime.fromisoformat(p["window_ending"]) - FIVE_HOURS) >= PLAN_CHANGE_AT]
    accounts = ((gs_passive or {}).get("accounts") or {})
    out = {"a1": era}
    for name, label in ACCOUNT_LABELS[1:]:
        points = (accounts.get(name) or {}).get("weekly_by_window") or []
        out[label] = [p for p in points
                      if _repaired(p) and (datetime.fromisoformat(p["window_ending"])
                                           - FIVE_HOURS) >= PLAN_CHANGE_AT]
    return {label: sorted(pts, key=lambda p: p["window_ending"]) for label, pts in out.items()}


def _point_tuples(points: list[dict], label: str) -> list[tuple]:
    """Window-point dicts as the (window_ending, d5, d7, pieces, account) tuples
    the detector takes."""
    return [(datetime.fromisoformat(p["window_ending"]), p["five_hour_pct"], p["seven_day_pct"],
             p.get("pieces", 1), label) for p in points]


def _account_block(points: list[tuple], now: datetime) -> dict:
    """One account's own count, current, regimes and step, from its own points alone.

    The step is dated per account because the weekly cut lands on each account at
    its own seven-day reset, and pooling the accounts into one series dates the
    event at whichever of them stepped first. `percent` is signed (negative for a
    cut), `before`/`after` are the pooled levels of the regimes the step separates.
    """
    estimate = _regime_current(points, now) if points else None
    regimes = _regimes(points)
    events = detect_weighted_changes(points)
    step = None
    if events:
        e = events[-1]
        after = next((i for i, r in enumerate(regimes)
                      if r["start"][:10] == e.date.isoformat()), None)
        step = {"onset": e.date.isoformat(),
                "before": regimes[after - 1]["windows"] if after else None,
                "after": regimes[after]["windows"] if after is not None else None,
                "percent": -e.percent if e.direction == "decreased" else e.percent}
    return {"n": len(points), "current": estimate["value"] if estimate else None,
            "regimes": regimes, "step": step}


def _pooled_weeks(points: list[tuple], now: datetime) -> list[dict]:
    """The pooled per-window points bucketed into calendar weeks, oldest first.

    The same pooling tracker/weekly.py uses for its own `history` -- total
    five-hour movement over total seven-day movement, with the rounding interval
    of that pool -- over every watched account's points rather than one account's.
    A probe row's week key is its ISO week (weekly.py's `_iso_week_ending`) and so
    is this one: the three accounts do not share a seven-day reset, so no single
    meter boundary can bucket them. `n` is how many window points the week pools
    and `partial` marks the newest week while it is still open.
    """
    weeks: dict[str, list[tuple]] = {}
    for p in points:
        weeks.setdefault(_iso_week_ending(p[0].isoformat()), []).append(p)
    rows = []
    for week_ending in sorted(weeks):
        pool = weeks[week_ending]
        d5, d7 = sum(p[1] for p in pool), sum(p[2] for p in pool)
        lo, hi = pooled_interval(pool)
        rows.append({"week_ending": week_ending, "windows": round(d5 / d7, 2) if d7 > 0 else None,
                     "rounding_interval": [round(x, 4) if x is not None else None
                                           for x in (lo, hi)],
                     "n": len(pool), "five_hour_pct": round(d5, 1), "seven_day_pct": round(d7, 1),
                     "partial": date.fromisoformat(week_ending) >= now.date(),
                     "source": "passive_paired_deltas", "assumed": False})
    return rows


@dataclass(frozen=True)
class _AccountDatedEvent(ChangeEvent):
    """A pooled weekly event whose date and onset bounds come from the accounts' own steps.

    The detector's own bounds -- the last pooled window at the old level and the
    first at the new one -- are kept in `window_onset_*` and published under
    `onset.from_windows`, so re-dating never loses them.
    """
    window_onset_earliest: date | None = None
    window_onset_latest: date | None = None
    account_dated: bool = False


def _account_dated(events: list, by_account: dict) -> list:
    """The pooled weekly events with the newest one re-dated from the per-account onsets.

    Anthropic's cut reaches each account at that account's own seven-day reset, so
    the honest bounds on the pooled event are the earliest and the latest onset the
    watched accounts saw, and its date is the earliest of them. Only the newest
    pooled event is re-dated: an older one predates the accounts being watched
    together. With no per-account step at all the events are returned unchanged.
    """
    onsets = sorted(date.fromisoformat(a["step"]["onset"]) for a in by_account.values()
                    if a["step"] and a["step"]["onset"])
    if not events or not onsets:
        return events
    e = events[-1]
    dated = _AccountDatedEvent(
        date=onsets[0], direction=e.direction, percent=e.percent, model=e.model,
        onset_earliest=onsets[0], onset_latest=onsets[-1], confirmed_at=e.confirmed_at,
        evidence_points=e.evidence_points, denominator_pct=e.denominator_pct,
        before_interval=e.before_interval, after_interval=e.after_interval,
        window_onset_earliest=e.onset_earliest, window_onset_latest=e.onset_latest,
        account_dated=onsets[0] != e.date)
    return sorted([*events[:-1], dated], key=lambda ev: ev.date)


def _weekly_block(passive_weekly: dict | None, probe_weekly: dict, now: datetime,
                  gs_passive: dict | None = None) -> tuple[dict, list]:
    """(`weekly_windows`, weekly change events): per plan, each with its own evidence.

    `max20` is the live plan, and since 2026-09-20 it is measured over every
    watched Max 20x account rather than masterrig alone (_account_window_dicts):
    `by_window` is the three accounts' per-window points, each tagged `a1`/`a2`/
    `a3`, `weekly` pools them into calendar weeks for the chart, and `current`
    (_regime_current), `regimes` and the weekly events are computed over the
    pooled points. `by_account` carries each account's own count, current,
    regimes and step, because the weekly cut reaches each account at its own
    seven-day reset and the pooled series dates it at whichever stepped first;
    the pooled event is re-dated from those onsets (_account_dated).

    `max5` is measured history: its weekly rows and regimes are the account's own
    Jun-Aug Max 5x era, and its last regime begins before PLAN_CHANGE, a date
    taken from the account owner's record rather than from independent metadata,
    so the plan move itself cannot be told apart from a limit change there;
    `plan_change` says so. Neither max5 nor `pro` has a contemporaneous
    measurement any more, so each one's `current` is the live max20 figure scaled
    by WEEKLY_WINDOW_RATIOS, marked `assumed`, `inferred_from: max20`, and
    `availability.status: inferred`. Probe runs' weekly rows are published as
    their own series and never replace passive weeks (finding 13).
    """
    passive_weekly = dict(passive_weekly or {"current": None, "history": [], "by_window": []})
    # passive.json may lag: it can predate the flag, or carry a `partial` from when its
    # newest week was still open. Recompute it against this publish's own time.
    passive_weekly["history"] = [dict(h, partial=date.fromisoformat(h["week_ending"]) >= now.date())
                                 for h in passive_weekly.get("history", [])]
    # The median of the last two complete weeks is not a measurement of anything the
    # page states (audit finding 6), and it was the one figure here still published as
    # one. The raw series itself stays: `by_window` is what bin/daily.sh reads to tell
    # new weekly evidence from an hourly re-read of the same windows.
    passive_weekly["current"] = None
    passive_weekly["reason"] = "median_of_two_complete_weeks_is_not_a_measurement"
    rows = [dict(h) for h in passive_weekly["history"]]
    for h in rows:
        h.update({"source": "passive_paired_deltas", "assumed": False}
                 | ({"quality": "measured", "reasons": []} if _repaired(h)
                    else {"quality": "legacy_uncertain", "reasons": ["paired_before_denominator_repair"]}))
    points = passive_weekly.get("by_window", [])
    legacy_only = bool(points) and not any(_repaired(p) for p in points)
    account_dicts = _account_window_dicts(points, gs_passive)
    by_account = {label: _account_block(_point_tuples(account_dicts[label], label), now)
                  for _name, label in ACCOUNT_LABELS}
    max20_points = sorted((p for label in by_account
                           for p in _point_tuples(account_dicts[label], label)),
                          key=lambda p: p[0])
    max20_by_window = sorted((dict(p, account=label)
                              for label, pts in account_dicts.items() for p in pts),
                             key=lambda p: p["window_ending"])
    max5_points = _max5_window_points(points)
    events = _account_dated(detect_weighted_changes(max20_points), by_account)
    estimate = _regime_current(max20_points, now)

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
    max20_current = estimate["value"] if estimate else None

    def inferred(plan: str) -> dict:
        """One plan's `current` scaled off the live max20 figure by its weekly-window ratio."""
        ratio = WEEKLY_WINDOW_RATIOS[plan]
        value = round(max20_current * ratio, 2) if max20_current is not None else None
        if value is None:
            status = {"status": "unavailable", "reason": "no_max20_current_to_scale_from"}
        else:
            status = {"status": "inferred", "reason": "scaled_from_max20_by_weekly_window_ratio"}
        return {"current": value, "current_estimate": None, "assumed": True,
                "inferred_from": "max20", "weekly_window_ratio": ratio,
                "availability": status}

    block = {
        "max20": {"current": max20_current, "current_estimate": estimate,
                  "history": [h for h in rows if _plan_for_week(h["week_ending"]) == "max20"],
                  "weekly": _pooled_weeks(max20_points, now), "by_window": max20_by_window,
                  "by_account": by_account,
                  "regimes": _regimes(max20_points), "assumed": False,
                  "availability": max20_availability},
        "max5": {**inferred("max5"), "history": max5_history, "regimes": _regimes(max5_points),
                 "plan_change": {"date": PLAN_CHANGE.isoformat(), "source": "the meter's own step",
                                 "independently_verified": False}},
        "pro": {**inferred("pro"), "history": [], "regimes": []},
        "passive": passive_weekly,
        "probe": probe,
    }
    return block, events


def _window_points(passive_points: list[dict], keep) -> list[tuple[datetime, float, float, int]]:
    points = [(datetime.fromisoformat(p["window_ending"]), p["five_hour_pct"], p["seven_day_pct"], p["pieces"])
              for p in passive_points
              if _repaired(p) and p.get("reset_verified") is True and keep(datetime.fromisoformat(p["window_ending"]))]
    return sorted(points, key=lambda p: p[0])


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
    """The frozen plan's per-window series, the mirror of the max20 era rule.

    Only windows that END at or before PLAN_CHANGE_AT, the precise seam within
    PLAN_CHANGE day (see _account_window_dicts): a window straddling the change
    mixes both plans' ratios and belongs to neither, the same rule
    _plan_for_week applies to weeks.
    """
    return _window_points(passive_points, lambda ending: ending <= PLAN_CHANGE_AT)


def _regime_current(points: list[tuple], now: datetime) -> dict | None:
    """max20's current estimate: the current regime's newest WEEKLY_CURRENT_DAYS of window points, pooled.

    Pooled (total d5 over total d7) and bounded by the regime, so once a change is
    certified `current` is the post-change level instead of a blend of both.
    None when that fortnight holds less than MIN_POOL_D7 of seven-day movement.
    `stale` is judged against the publish time, not against the newest point, so
    a weekly log that has stopped arriving is visible as such (audit finding 16).

    Since the series became three accounts wide the pool can hold points whose
    meter log did not name the window's reset (_account_window_dicts): masterrig's
    names every one, the gs accounts' name only the newest few. `quality` stays
    "measured" and each point publishes its own `reset_verified` flag under
    `max20.by_window`, so a consumer can see how much of the pool carries one.
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
    """The most recent change across both event series, as its full event record.

    Recency is judged on the evidence's own newest window, not on the published
    date: a pooled weekly event re-dated from the accounts' own onsets
    (_account_dated) carries a date earlier than the windows it was certified on,
    and a staggered cut can certify more than one pooled split inside that span.
    Without this the account-dated event lost `last_change` to an older split of
    the same transition.
    """
    candidates = [(e, "window") for e in window_events] + [(e, "weekly") for e in weekly_events]
    if not candidates:
        return None
    e, scope = max(candidates,
                   key=lambda c: getattr(c[0], "window_onset_latest", None) or c[0].date)
    return _event_record(e, scope)


def _event_record(e, scope: str) -> dict:
    """One published change: an observed change in this account's metric, not a dated policy change.

    `onset` bounds when it happened (the last reading at the old level and the
    first at the new one), `confirmation` says by when the evidence after it
    alone was enough and how much there was. Weekly events are certified against
    both levels' rounding intervals (tracker/detect.py); window events have no
    uncertainty model behind them, so they are `provisional`, and bin/daily.sh
    announces neither a provisional nor a legacy-uncertain change.

    `meter_attribution` on a weekly event is `unresolved`: the measured quantity
    is the ratio of five-hour to seven-day movement, and a fall in it can come
    from a smaller weekly cap, a bigger five-hour window, or both. It is not
    resolved here and the record never claims which meter moved (see
    _weekly_label for the evidence that both did).
    """
    provisional = scope == "window"
    # A pooled weekly event re-dated from the accounts' own onsets (_account_dated)
    # says so in `attribution`, and keeps the detector's own window bounds under
    # `onset.from_windows` so the re-dating never loses them.
    window_onset = (getattr(e, "window_onset_earliest", None),
                    getattr(e, "window_onset_latest", None))
    onset = {"earliest": e.onset_earliest.isoformat() if e.onset_earliest else None,
             "latest": (e.onset_latest or e.date).isoformat()}
    if any(window_onset):
        onset["from_windows"] = {
            "earliest": window_onset[0].isoformat() if window_onset[0] else None,
            "latest": window_onset[1].isoformat() if window_onset[1] else None}
    return {
        "date": e.date.isoformat(), "direction": e.direction, "percent": e.percent, "model": e.model,
        "scope": scope, "metric": "meter_budget_per_window" if scope == "window" else "weekly_to_five_hour_ratio",
        "observation_scope": "account",
        "attribution": ("observed_account_metric_change_dated_from_per_account_onsets"
                        if getattr(e, "account_dated", False)
                        else "observed_account_metric_change"),
        "onset": onset,
        "confirmation": {"at": e.confirmed_at.isoformat() if e.confirmed_at else None,
                         "evidence_points": e.evidence_points, "seven_day_pct": e.denominator_pct},
        "rounding_interval_before": _rounded(e.before_interval),
        "rounding_interval_after": _rounded(e.after_interval),
        "evidence_quality": "provisional" if provisional else "certified",
        "provisional": provisional, "legacy_uncertain": False,
        **({} if provisional else {"meter_attribution": "unresolved"}),
    }


def _rounded(interval: tuple | None) -> list | None:
    return [round(x, 4) if x is not None else None for x in interval] if interval else None


def _weekly_label(e) -> str:
    """One weekly event's row, stating the measured quantity and nothing else.

    What is measured is how many five-hour windows a week's cap holds: five-hour
    meter movement over seven-day meter movement. A fall in it does NOT say the
    weekly cap fell by that much, and the row must not read as though it does --
    a smaller five-hour window moves the same ratio. The split between the two is
    unresolved here: jwork's accepted stretches read the five-hour window about 9%
    larger in credits across the change, which puts the weekly cap's own fall
    somewhere between 6% and 15% of a 22-24% fall in the ratio. `meter_attribution`
    carries that as a field (_event_record).

    The percent and the dates are the event's own measured ones, so the row can
    never say something the rest of the record does not.
    """
    moved = "rose" if e.direction == "increased" else "fell"
    earliest = (e.onset_earliest or e.date).isoformat()
    latest = (e.onset_latest or e.date).isoformat()
    span = earliest if earliest == latest else f"{earliest} to {latest}"
    return f"Observed windows per week {moved} about {e.percent}% around {span}"


def _build_events(window_events: list, weekly_events: list) -> list[dict]:
    events = [{**_event_record(e, "window"), "kind": "change",
               "label": f"Observed window budget changed {'+' if e.direction == 'increased' else '-'}{e.percent}%"}
              for e in window_events]
    events += [{**_event_record(e, "weekly"), "kind": "change", "label": _weekly_label(e)}
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
