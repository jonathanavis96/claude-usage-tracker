"""Assemble the public JSON from probe rows, passive output, and static tables."""
from __future__ import annotations
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from .capture import ACCEPTED
from .detect import (MIN_POOL_D7, current_regime_points, detect_changes, detect_smoothed_changes, detect_weighted_changes,
                     pooled_windows, weighted_regimes)
from .gs_passive import passive_dollar_readings
from .passive import PLAN_CHANGE
from .rows import usable_rows
from .weekly import probe_weekly_windows

PLAN_RATIOS_BASE = {"pro": 0.05, "max5": 0.25, "max20": 1.0}
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
# Frozen is the right shape, not a shortcut: max5 is not coming back on this account
# (see _plan_for_week), so the ratio can never be re-measured here. Recomputing it live
# from two `current` fields is what issue #54 fixes -- max20's current tracks the newest
# regime while max5's is a frozen August calendar-week median, so their quotient carries
# whatever limit change has landed since (2.39 today, being 1.78 x the 14 Sep -29%) and
# fires a plan move as a limit move. A real Max 5x contributor replaces this with
# measurement; until then the gap-fill has one constant, not a drifting quotient.
WEEKLY_WINDOW_RATIOS = {"pro": 1.78, "max5": 1.78, "max20": 1.0}
MAX_SAMPLE_AGE_DAYS = 10
WEEKLY_CURRENT_DAYS = 14
FIVE_HOURS = timedelta(hours=5)


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
    return price.get("class_weight", {}).get(cls, 1.0)


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


def _gs_split(gs_passive: dict | None, *, allow_legacy_unverified: bool = False) -> dict:
    """Token-class shares over every gs account's published stretches, weighted by tokens."""
    tot = {c: 0.0 for c in ("input", "output", "cache_read", "cache_write")}
    for acct in (gs_passive or {}).get("accounts", {}).values():
        for st in acct.get("stretches", []):
            if st.get("status") != ACCEPTED:
                continue
            if st.get("reset_verified") is not True and not allow_legacy_unverified:
                continue
            if st.get("unpriced_tokens", 0) > 0:
                continue
            for by_class in st.get("tokens", {}).values():
                for c in tot:
                    tot[c] += by_class.get(c, 0)
    grand = sum(tot.values())
    if not grand:
        return {}
    out = {c: n / grand for c, n in tot.items()}
    one_hour = sum(by_class.get("cache_write_1h", 0)
                   for acct in (gs_passive or {}).get("accounts", {}).values()
                   for st in acct.get("stretches", [])
                   if st.get("status") == ACCEPTED
                   and (st.get("reset_verified") is True or allow_legacy_unverified)
                   and st.get("unpriced_tokens", 0) == 0
                   for by_class in st.get("tokens", {}).values())
    if one_hour:
        out["cache_write_1h"] = one_hour / grand
    return out


def _row_split(row: dict) -> dict:
    tokens = row["tokens"]
    total = sum(tokens[cls] for cls in ("input", "output", "cache_read", "cache_write"))
    out = {cls: tokens[cls] / total for cls in ("input", "output", "cache_read", "cache_write")}
    if tokens.get("cache_write_1h"):
        out["cache_write_1h"] = tokens["cache_write_1h"] / total
    return out


def build_public_json(probe_rows: list[dict], passive: dict, effort: dict, prices: dict, now: datetime,
                      effort_usd: dict | None = None, gs_passive: dict | None = None) -> dict:
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

    `gs_passive` (tracker/gs_passive.py's `report()`, issue #39) supplies the
    passive readings: `passive_dollar_readings(gs_passive, prices, by="day")`,
    one (time, meter dollars per full window) reading per gs account per UTC
    day, every priced stretch pooled. When there is at least one, the dollar
    series IS that passive series -- probe rows do not join it. The probe read
    the same meter on another scale (its payload was 98% cache_write by list
    value; real sessions are 59% cache_read, 24% cache_write, 17% output) and
    every attempt to scale one onto the other invented change events. The
    probe rows still supply `probed_at`/`probe_effort`, the weekly windows and
    the class-split fallback; with no passive readings at all the series is the
    probe's, exactly as before 2026-09-16. `instrument` (top level) says which:
    "passive" or "probe". Step detection on a passive series uses
    detect_smoothed_changes (a rolling week's median against the regime before
    it) because a passive day scatters 15-20% and detect_changes' two-readings
    rule fires on that; the probe series keeps detect_changes. The freshness
    guard reads the newest reading of the series, so a passive day 24 hours old
    keeps the publish fresh with no probe ever run again. History days before
    the first reading of the series are "held" at the first regime's value;
    the passive series starts 2026-09-05, so probe days before it are held,
    not shown at the probe's scale.

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

    # One model-agnostic dollar series: what a full window (100%) of usage would
    # have cost at API list prices, regardless of which model probed it, or
    # whether it was probed at all. `passive_dollar_readings` returns the same
    # (time, dollars-per-window) shape as a probe reading (see gs_passive.py),
    # so the two concatenate directly; only the *kind* of each reading is
    # tracked separately, for `instrument` and `rates[model]["source"]" below.
    probe_readings = [(datetime.fromisoformat(r["ts"]), usd_per_pct(r, prices[r["model"]]) * 100, "probe")
                      for r in probe_rows]
    # Probe and passive payloads are incompatible instruments.  A missing
    # passive report must not silently replace the public series with probe
    # values on a different scale.  Archived reset-less stretches are retained
    # as conditional references, but cannot produce a confident change event.
    passive_readings = ([(ts, v, "passive") for ts, v in
                         passive_dollar_readings(gs_passive, prices, by="day")]
                        if gs_passive else [])
    legacy_readings = ([(ts, v, "passive_legacy") for ts, v in
                        passive_dollar_readings(gs_passive, prices, by="day", allow_legacy_unverified=True)]
                       if gs_passive and not passive_readings else [])
    evidence_status = "measured" if passive_readings else ("conditional" if legacy_readings else "unavailable")
    combined = sorted(passive_readings or legacy_readings, key=lambda t: t[0])

    dollar_readings = [(ts, v) for ts, v, _ in combined]
    events = detect_smoothed_changes(dollar_readings) if evidence_status == "measured" else []
    regime_starts = sorted({e.date for e in events})

    def regime_index(d: date) -> int:
        idx = 0
        for rd in regime_starts:
            if d >= rd:
                idx += 1
            else:
                break
        return idx

    by_day_models: dict[date, set[str]] = {}
    regime_values: dict[int, list[float]] = {}
    # The newest reading in each regime, kind and all: what rates[model]["source"]
    # and ["measured_at"] key off, so a passive-only day in the current regime
    # reads "passive" for every model instead of silently keeping a stale "probe".
    regime_newest: dict[int, tuple[datetime, str]] = {}
    for ts, v, kind in combined:
        idx = regime_index(ts.date())
        regime_values.setdefault(idx, []).append(v)
        if idx not in regime_newest or ts > regime_newest[idx][0]:
            regime_newest[idx] = (ts, kind)
    passive_days = {ts.date() for ts, _, kind in combined if kind.startswith("passive")}

    latest_row = max(probe_rows, key=lambda r: r["ts"]) if probe_rows else None
    latest_row_per_model: dict[str, dict] = {}
    for r in probe_rows:
        m = r["model"]
        if m not in latest_row_per_model or r["ts"] > latest_row_per_model[m]["ts"]:
            latest_row_per_model[m] = r
    # Counts, never names. The published JSON is world-readable, and the account names are
    # the login names of real people's Claude accounts; how many accounts a figure rests on
    # is the part a reader needs.
    probe_account_count = len({r["account"] for r in probe_rows if r.get("account")})
    passive_account_count = sum(1 for acct in (gs_passive or {}).get("accounts", {}).values()
                                if any(s["status"] == ACCEPTED for s in acct.get("stretches", [])))
    earliest_reading_day = combined[0][0].date() if combined else now.date()
    first_probe_day = min(by_day_models, default=earliest_reading_day)
    # The class split converts meter dollars to tokens. In passive mode it is the gs
    # accounts' own mix (tracker/gs_passive.py `split`, token-weighted across accounts),
    # the sessions the series is measured from; else masterrig's passive.json split; a
    # missing/lagging passive.json is allowed (it arrives from masterrig), so fall back
    # to the latest probe row's own class split rather than blowing up on an empty one.
    gs_split = _gs_split(gs_passive, allow_legacy_unverified=evidence_status == "conditional")
    passive_split = gs_split or passive_split

    passive_history = passive.get("history", {})
    # No retrospective held rows: before the first compatible observation the
    # rate was not measured.  Consumers may present a separate current-mix
    # scenario, but history does not masquerade as contemporaneous evidence.
    first_day = earliest_reading_day
    last_day = now.date()
    current_regime = regime_index(last_day)
    current_regime_value = median(regime_values[current_regime]) if current_regime in regime_values else None
    measured_at, current_kind = regime_newest.get(current_regime, (None, "none"))
    instrument = "passive" if evidence_status in ("measured", "conditional") else "unavailable"

    # Published ratios only: the passive-observed 5x-to-20x ratio is too noisy to publish
    # (see docs/spike-2026-09.md); the page cites it in caveats instead.
    ratios = dict(PLAN_RATIOS_BASE)
    rates, history = {}, {}
    for model, price in prices.items():
        blended = blended_price_per_token(passive_split, price) if passive_split else 0.0
        api_blended = blended_api_price_per_token(passive_split, price) if passive_split else 0.0
        weight = price.get("meter_weight", 1.0)
        model_probe = latest_row_per_model.get(model)
        tokens_per_window = (round(current_regime_value / (blended * weight))
                             if current_regime_value is not None and blended > 0 else None)
        api_list_value = (round(tokens_per_window * api_blended, 2)
                          if tokens_per_window is not None else None)
        stale = (measured_at < now - timedelta(days=MAX_SAMPLE_AGE_DAYS)) if measured_at else None
        reasons = []
        if evidence_status == "conditional":
            reasons.extend(["legacy_reset_metadata_missing", "capture_completeness_not_certified"])
        elif evidence_status == "unavailable":
            reasons.append("no_eligible_passive_measurement")
        rates[model] = {"tokens_per_window": tokens_per_window,
                        "source": "derived_reference_mix" if tokens_per_window is not None else "unavailable",
                        "probed_at": model_probe["ts"] if model_probe is not None else None,
                        "measured_at": measured_at.isoformat() if measured_at else None,
                        "probe_effort": latest_row["effort"] if latest_row is not None else None,
                        "split": passive_split,
                        "meter_budget_per_window": round(current_regime_value, 2) if current_regime_value is not None else None,
                        "api_value_per_window": api_list_value,
                        "api_list_value_per_window": api_list_value,
                        "reference_mix": {"kind": "derived_scenario",
                            "source": "passive_token_mix" if evidence_status == "measured" else "legacy_passive_token_mix",
                            "as_of": measured_at.isoformat() if measured_at else None,
                            "cache_write_duration": ("observed" if passive_split.get("cache_write_1h") is not None
                                                     else "legacy_assumed_5m")},
                        "assumptions": {
                            "model_conversion": "meter budget converted with the shared reference mix and price-table weights",
                            "direct_model_cap_measurement": False,
                            "meter_weight": weight,
                            "class_weight": {c: class_weight(price, c)
                                             for c in ("input", "output", "cache_read", "cache_write", "cache_write_1h")},
                            "cache_read_meter_weight_zero": class_weight(price, "cache_read") == 0,
                            "cache_write_1h_meter_weight": "assumed_same_as_cache_write",
                        },
                        "evidence": {"source": "passive" if combined else "none",
                            "observed_from": combined[0][0].isoformat() if combined else None,
                            "observed_to": measured_at.isoformat() if measured_at else None,
                            "measured_at": measured_at.isoformat() if measured_at else None,
                            "account_count": passive_account_count,
                            "reset_verified": evidence_status == "measured"},
                        "quality": {"status": evidence_status, "reasons": reasons,
                            "capture_complete": True if evidence_status == "measured" else None,
                            "unpriced_work": False if evidence_status in ("measured", "conditional") else None,
                            "rounding_relative": None},
                        "freshness": {"as_of": measured_at.isoformat() if measured_at else None,
                            "stale_after": ((measured_at + timedelta(days=MAX_SAMPLE_AGE_DAYS)).isoformat()
                                            if measured_at else None), "stale": stale}}
        hist = []
        d = first_day
        while combined and d <= last_day:
            idx = regime_index(d)
            regime_value = median(regime_values[idx])
            day_tokens = round(regime_value / (blended * weight)) if blended > 0 else None
            hist.append({"date": d.isoformat(), "tokens_per_window": day_tokens,
                        "meter_budget_per_window": round(regime_value, 2),
                        "api_value_per_window": round(day_tokens * api_blended, 2) if day_tokens is not None else None,
                        "api_list_value_per_window": round(day_tokens * api_blended, 2) if day_tokens is not None else None,
                        "source": "passive" if d in passive_days else "derived_reference_mix",
                        "quality": evidence_status, "interpolated": False})
            d += timedelta(days=1)
        history[model] = hist
    last_sample = combined[-1][0].isoformat() if combined else None
    # The page freezes generated_at, so a stale publish would silently present old
    # numbers as current instead of letting the page's own stale warning fire. The
    # guard is on the newest reading of EITHER kind: passive keeps the figure
    # fresh on its own now that probes are not scheduled (issue #39).
    if not rates:
        raise ValueError("price table has no models")
    passive_weekly = passive.get("weekly_windows")
    weekly_windows = None
    weekly_window_ratios = {}
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
        #
        # The weekly rows are what the page charts. Detection and max20's
        # `current` do NOT run on them: a calendar week blends a mid-week step
        # into its average (the 2026-09-13 cut published as a -14.5% open week
        # and could not have fired before 2026-10-02, issue #25). They run on
        # the passive per-window series `by_window` instead, each point
        # weighted by its own seven-day movement (tracker/detect.py:
        # detect_weighted_changes, _max20_window_points for why the probe
        # runs' own points stay out). A passive.json from before that series
        # existed publishes its weekly rows as before, with no weekly
        # detection and the old two-complete-weeks median as current, until
        # masterrig's next passive run.
        passive_history = [dict(h) for h in passive_weekly.get("history", [])]
        # passive.json may lag: it can predate the flag, or carry a `partial` from
        # when its newest week was still open. Recompute the flag against this
        # publish's own time so every published weekly row honours the contract.
        for h in passive_history:
            h["partial"] = date.fromisoformat(h["week_ending"]) >= now.date()
        probe_history = [dict(h, source="probe_paired_deltas", quality="measured",
                              assumed=False) for h in probe_weekly["history"]]
        for h in passive_history:
            h.update({"source": h.get("source", "passive_paired_deltas"), "assumed": False,
                      "quality": ("measured" if h.get("reset_verified") is True else "legacy_uncertain"),
                      "reasons": ([] if h.get("reset_verified") is True
                                  else ["historical_row_missing_reset_provenance"])})
        max5_history = [h for h in passive_history if _plan_for_week(h["week_ending"]) == "max5"]
        max20_history = [h for h in passive_history if _plan_for_week(h["week_ending"]) == "max20"]
        max20_points = _max20_window_points(passive_weekly.get("by_window", []))
        weekly_events = detect_weighted_changes(max20_points)
        max20_current = _regime_current(max20_points)
        # Levels, not weekly points. Windows per week is a plan constant, so the honest
        # series is a step function and every weekly ratio is an estimate of it carrying
        # several percent of assembly error (weighted_regimes' docstring lists the three
        # sources). Published alongside the weekly rows rather than instead of them: the
        # rows are the evidence, the regimes are what the evidence says.
        max20_regimes = weighted_regimes(max20_points)
        # max5 is frozen, so the only step its own windows can contain is the plan move
        # itself, and the detector duly finds one -- dated 2026-08-15, three days before
        # PLAN_CHANGE (the 15-18 Aug windows pool to 6.42 against 10.83 for 1-14 Aug, so
        # the account was already on max20; see issue #56). Keep only the regimes before
        # that first step, which is max5's real level, rather than publishing the plan
        # move as a limit change on the frozen plan.
        max5_regimes = weighted_regimes(_max5_window_points(passive_weekly.get("by_window", [])))
        for r in max20_regimes + max5_regimes:
            r.update({"source": "passive_paired_deltas", "assumed": False, "quality": "measured", "reasons": []})
        current_estimate = ({"value": max20_current, "source": "passive_paired_deltas",
                             "as_of": max20_points[-1][0].isoformat(), "stale": max20_points[-1][0] < now - timedelta(days=14),
                             "assumed": False, "quality": "measured", "reasons": []}
                            if max20_current is not None and max20_points else None)
        weekly_windows = {
            "max20": {"current": max20_current, "current_estimate": current_estimate,
                      "history": max20_history, "assumed": False, "regimes": max20_regimes},
            "max5": {"current": None, "current_estimate": None, "history": max5_history,
                     "assumed": False, "regimes": max5_regimes,
                     "availability": {"status": "historical_only", "reason": "no_current_max5_measurement"}},
            "pro": {"current": None, "current_estimate": None, "history": [], "assumed": False,
                    "regimes": [], "availability": {"status": "unavailable", "reason": "no_pro_measurement"}},
            "passive": passive_weekly,
            "probe": {**probe_weekly, "history": probe_history},
        }
        weekly_window_ratios = {}
    last_change = _latest_change_with_scope(events, weekly_events)
    out = {
        "schema_version": 2,
        "generated_at": now.isoformat(),
        "last_sample_at": last_sample,
        "meter_read_at": _last_meter_read(gs_passive),
        "passive_generated_at": passive.get("generated_at"),
        "plan_measured": "max20",
        "instrument": instrument,
        "probe_account_count": probe_account_count,
        "passive_account_count": passive_account_count,
        "plan_ratios": ratios,
        "weekly_window_ratios": weekly_window_ratios,
        "rate_basis": "meter_budget",
        "model_plan_limits": _model_plan_limits(prices),
        "rates": rates,
        "effort": effort,
        "effort_usd": effort_usd or {},
        "api_price_per_mtok": prices,
        "history": history,
        "last_change": last_change,
        "events": _build_events(events, weekly_events),
        "availability": {"rates": evidence_status,
                         "reason": None if evidence_status == "measured" else
                         ("legacy_reset_metadata_missing" if evidence_status == "conditional"
                          else "no_eligible_passive_measurement")},
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
    """Median of the last two complete weeks: max5's figure, and max20's only when
    no per-window series has arrived yet (see _regime_current)."""
    complete = [h for h in history if date.fromisoformat(h["week_ending"]) < now.date()]
    return round(median(h["windows"] for h in complete[-2:]), 2) if complete else None


def _max20_window_points(passive_points: list[dict]) -> list[tuple[datetime, float, float]]:
    """The live plan's per-window series as (window_ending, d5, d7), oldest first.

    Passive windows are Jonathan's own account, so only those that START
    after PLAN_CHANGE are max20: a window straddling or preceding the plan
    change would read at the Max 5x ratio (about 11 against 6.5) and, as the
    first base of the max20 series, would fire Jonathan's own plan move as an
    Anthropic cut. The whole of PLAN_CHANGE day is excluded, not just windows
    before it, because the change is dated to a day and not an hour.

    The probe runs' own per-window points (`weekly_windows.probe.by_window`)
    are published for the record but deliberately NOT pooled in here, even
    though the probe accounts are on the same plan. A 3-tick run moves the
    seven-day meter by one whole point or none, so each of its points is
    pure rounding (d7 = 1 read against a true movement anywhere in 0.5-1.5),
    and a fortnight of runs adds a handful of points of d7 next to a passive
    window's eight or eleven: measured against the real 2026-09-13 cut,
    pooling them in moved the base enough to report -31% for what the
    passive windows alone put at -26%, without adding any information a
    detector could use. Their weekly rows still replace the passive weeks in
    the chart series once a probe week clears the floors (probe_weekly_windows).
    """
    points = [(datetime.fromisoformat(p["window_ending"]), p["five_hour_pct"], p["seven_day_pct"])
              for p in passive_points if (datetime.fromisoformat(p["window_ending"]) - FIVE_HOURS).date() > PLAN_CHANGE]
    return sorted(points, key=lambda p: p[0])


def _max5_window_points(passive_points: list[dict]) -> list[tuple[datetime, float, float]]:
    """The frozen plan's per-window series, the mirror of _max20_window_points.

    Only windows that END on or before PLAN_CHANGE: a window straddling the
    change mixes both plans' ratios and belongs to neither, the same rule
    _plan_for_week applies to weeks. Max 5x is never detected on in production
    (it is frozen, and the module docstring of tracker/detect.py uses it as the
    out-of-sample check that a flat stretch reports nothing), so this exists to
    give the chart a measured LEVEL for the months before the plan move rather
    than a level inferred backwards from max20.
    """
    points = [(datetime.fromisoformat(p["window_ending"]), p["five_hour_pct"], p["seven_day_pct"])
              for p in passive_points if datetime.fromisoformat(p["window_ending"]).date() <= PLAN_CHANGE]
    return sorted(points, key=lambda p: p[0])


def _regime_current(points: list[tuple[datetime, float, float]]) -> float | None:
    """max20's `current`: the pooled ratio of the current regime's newest
    WEEKLY_CURRENT_DAYS of per-window points, anchored on the newest point.

    Pooled (total d5 over total d7) rather than a median of weeks, and bounded
    by the regime rather than by calendar weeks, so that once a change is
    detected `current` is the post-change level from the day it fires instead
    of the median of two pre-change weeks for another fortnight (issue #25: the
    page's sessions-per-week and dollars-per-week derive from it). With no
    change in the series it is simply the trailing fortnight. None when that
    fortnight holds less than MIN_POOL_D7 of seven-day movement (a series that
    has only just started, or a passive log that has gone quiet): too little
    to state a level from, and the caller falls back to the weekly rows.
    """
    regime = current_regime_points(points)
    if not regime:
        return None
    since = regime[-1][0] - timedelta(days=WEEKLY_CURRENT_DAYS)
    recent = [p for p in regime if p[0] >= since]
    if sum(p[2] for p in recent) < MIN_POOL_D7:
        return None
    pooled = pooled_windows(recent)
    return round(pooled, 2) if pooled is not None else None


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
