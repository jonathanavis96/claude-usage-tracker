"""Credit arithmetic over the passive stretch records, shared by every consumer of it.

The meter does not work in dollars. It works in an internal unit -- credits --
charged per token at a small rational rate per model, and the five-hour window is a
credit budget. Nothing here carries a rate of its own, so a rate only ever changes in
one place, and there are two such places, for two different jobs:

- `history/model-rates.json` holds the rates **measured on the meter** by
  `tools/model_rates.py`, with a bootstrap interval each. Every per-model figure the
  page states divides by one of these (`family_rate`). A family the fit could not
  measure gets a status sentence and no number.
- `data/prices.json`'s `_credits` block is the January 2026 **reference** table
  (`role: reference`). Two things in it are still used: Opus's row, which is the unit
  anchor -- 10/15 credits per input token -- that every measured rate is expressed
  against and that nothing in our data tests; and the cache-read weight, which is
  measured elsewhere rather than taken from the article. The rest of it is drawn beside
  the measurement and divided by nowhere.

The stretch-level instruments below price a stretch's tokens directly. `window_credits`
is pure-Opus, so it touches only the anchor. `across_cut` prices mixed stretches at the
pooled fit's whole coefficient vector (`pooled_fit_prices`) -- every family, Haiku's
unpublished point estimate included, and the fitted cache-read weight -- because a
before-and-after ratio needs the fit's own model on both sides, and the fit was measured on
the same stretches (`rate_fit_stretches`). It falls back to the reference table with one
held Fable rate only where no pooled fit exists. Every other change figure -- the joint fit
(`absorb_new_family_rates`) and the known-date test (`announced_change`) -- prices at the
same point estimates through `comparison_value`, never at the published rates: a family's
interval crossing the publish limit is not a change in anything the meter did.
`fable_interval` states the pooled fit's
Fable rate and keeps the per-stretch solve against the reference table as working
(docs/findings-2026-09-23-pooled-rates.md).

Three callers import this module rather than each keeping their own copy of the
selection rule: `tracker/publish.py` (the published `credits` block),
`tools/reconcile_window.py` (the analysis the block's method comes from) and
`tools/credits_report.py` (`--publish-check`, which recomputes every published
figure from the same history files and refuses to agree by accident).

The selection rule, once, here. A stretch is evidence about ordinary use when:

- it moved the meter by at least `MIN_DELTA_PCT` (below that the whole-percent
  rounding is most of the reading),
- it carries tokens at all (an empty stretch says the meter moved and the host saw
  nothing of it),
- it does not overlap one of the tracker's own runs on its own account, as
  `history/harness-runs.jsonl` records them. There the tracker was driving the
  account, so the percent is not a reading of a session's tokens. The rule is keyed
  to the runs, never to a date: the 2026-09-09 jwork contamination was the effort
  matrix, not a probe, and a date rule would have taken the rest of that day with it.
  That file is the single source of the runs, and it is wider than the two files it
  replaced here: a probe writes a `history/probes.jsonl` row only when it finishes,
  and an aborted or crashed probe sent its prompts all the same. `tools/harness_runs.py`
  builds the file from `history/probes.jsonl`, `data/effort_matrix.json` and the two
  ops logs the probes write; nothing downstream reads those four again.
- and it passes whichever acceptance column the caller asks for (`require`).

Both callers gate on `capture_status`, not on `status`: 42 jwork stretches read
`status` "accepted" with `capture_status` "unaccounted", and letting those price the
window is the error the gate on PR #64 caught. `exempt` is the one place the callers
differ, deliberately. `tools/reconcile_window.py` exempts masterrig, which is the rule
its published findings were computed under. The publisher exempts nobody, which is
strictly tighter: masterrig's meter counts web, phone and every other machine while
only that host's transcripts are read, so its stretches divide real meter movement by
part of what moved it, and its capture column is not yet usable
(docs/findings-2026-09-20-masterrig-stretches.md). Its pure-Opus set reads 1,917 to
24,546 credits per 1% against jwork's 175,934 to 208,197; the reconciliation says in as
many words that a median of that is not a measurement. Gating masterrig too drops
exactly those and nothing else.
"""
from __future__ import annotations

import functools
import json
import math
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from .detect import ratio_interval

PRICES_PATH = Path(__file__).resolve().parent.parent / "data" / "prices.json"
#: The meter's own per-model rates, fitted from the passive stretches by
#: tools/model_rates.py and committed. `data/prices.json`'s `_credits` table is the January
#: 2026 reference the fit is read against; this file is the measurement, and it is what every
#: per-model figure the page states is divided by. Regenerate it with the command in its own
#: `_meta` (`python3 -m tools.model_rates history/masterrig-passive.json --json
#: history/model-rates.json`), which sends no traffic.
MODEL_RATES_PATH = Path(__file__).resolve().parent.parent / "history" / "model-rates.json"
#: Every run the tracker's own instruments made, one JSON object per line, written by
#: tools/harness_runs.py. Anchored to the checkout, not the working directory, because the
#: publisher runs from cron and the report tools run from anywhere.
HARNESS_RUNS_PATH = Path(__file__).resolve().parent.parent / "history" / "harness-runs.jsonl"

#: Below this much meter movement a stretch is mostly whole-percent rounding.
MIN_DELTA_PCT = 3.0
#: The four token classes, in the order the published blocks list them. `cache_write_1h`
#: is already inside `cache_write` (tracker/turns.py), so it is not a fifth class and
#: counting it again would count those tokens twice.
TOKEN_CLASSES = ("input", "cache_write", "cache_read", "output")
#: The announced date of the weekly change (see ANNOUNCEMENT), used to split an
#: account's stretches into before and after. Not a contamination rule: nothing is
#: excluded by date anywhere in this module.
CUT_AT = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
#: data/effort_matrix.json's `_meta` records `started` and `finished` but not which
#: account the matrix ran on. It ran on jwork (2026-09-20 review), so the account is
#: named here rather than read, and tools/harness_runs.py reads it from here when it
#: writes the matrix's row. If a later matrix runs elsewhere, this is the line.
EFFORT_MATRIX_ACCOUNT = "jwork"

#: What Anthropic announced, quoted, with where the quote was read. The page shows
#: this beside the measured quantity; neither is derived from the other.
ANNOUNCEMENT = {
    "date": "2026-09-14",
    "scope": "weekly",
    "announced_change_pct": -17,
    "quote": "Compared to today, this works out to a 17% reduction in weekly limits on Claude Code",
    "also_quoted": ("Starting September 14, we're permanently raising standard weekly limits "
                    "in Claude Code by 25% for Pro, Max, Team, and seat-based Enterprise plans"),
    "source": "Anthropic on X, quoted by BleepingComputer; recorded in "
              "docs/findings-2026-09-20-reconciliation.md",
}
#: Every recorded announcement, oldest first. An announcement annotates a change
#: candidate (`change_candidates`) whose date it matches; it never creates or gates one.
#: `ANNOUNCEMENT` above stays the weekly 14 September record the weekly event carries.
ANNOUNCEMENTS = [
    ANNOUNCEMENT,
    {
        "date": "2026-09-22",
        "scope": "five_hour",
        "announced_change_pct": 20,
        "quote": "We're also raising Pro and Max users' 5-hour limit by 20%.",
        "also_quoted": ("In addition to the price drop, we're increasing five-hour usage "
                        "limits on Pro, Max, Team, and seat-based Enterprise plans."),
        "source": ("Anthropic email to Max subscribers (the figure); "
                   "anthropic.com/news/claude-opus-5-5 (the increase, no figure); "
                   "MacRumors 2026-09-22 (the date)"),
    },
]
#: The announced weekly cap as a multiple of the January baseline, before and after
#: the 14 September change: a temporary +50% from May, made a permanent +25% on
#: 14 September. Policy, announced and cited -- never a measurement.
WEEKLY_CAP_MULTIPLIER = {"before": 1.5, "after": 1.25}
#: How many stretches an account needs on each side of the cut before its own five-hour
#: change is read as a measurement of that account's window (`five_hour_window_change`).
#: A side of two or three stretches is a median of two or three stretches.
FIVE_HOUR_MIN_SIDE = 10
#: How many readings the after cluster needs before it states the current five-hour window
#: on its own. Below it the before cluster carries the figure, scaled by the measured
#: five-hour change, and `current_source` says so.
MIN_AFTER_CLUSTER = 5


def load_credits(prices_raw: dict | None = None, *, default: dict | None = None) -> dict:
    """The `_credits` block of a price table, or of data/prices.json when none is given.

    A price table without one -- an archive from before the block existed, or a test
    fixture -- falls back to `default` when a caller supplies it, the same way
    tracker/rebuild_offline.py falls back to the running checkout's reference mix. With
    no default a missing block is an error, because nothing downstream can invent a rate.
    """
    table = prices_raw if prices_raw is not None else json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    block = table.get("_credits")
    if not isinstance(block, dict) or not isinstance(block.get("per_family"), dict):
        if default is not None:
            return default
        raise ValueError("price table carries no _credits block with a per_family table")
    return block


def _rate(value) -> float | None:
    """One rate as a float. `[numerator, denominator]` keeps the exact fifteenths in JSON."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        num, den = value
        return num / den
    return float(value)


def family(model: str, credits: dict) -> str | None:
    """The credit family a model id belongs to, matched on `matches` as a substring.

    The table is keyed by family rather than by model id, so Opus 5, 4.8 and 4.7 all
    price as Opus. Where more than one `matches` is a substring of the id, the longest
    wins: `claude-opus-5-5` holds both `opus` and `opus-5-5`, and Opus 5.5 is its own
    family, measured on its own rather than credited at Opus 5's rate. Taking the most
    specific match rather than the first makes the answer independent of the table's
    key order.
    """
    lowered = model.lower()
    auto = auto_family(lowered, credits)
    if auto is not None:
        return auto
    best, best_len = None, -1
    for name, row in credits["per_family"].items():
        needle = row.get("matches", name)
        if needle in lowered and len(needle) > best_len:
            best, best_len = name, len(needle)
    return best


#: A current-style model id: `claude-<name>-<major>[-<minor>...]`, with an optional
#: eight-digit date suffix. Older ids (`claude-3-5-sonnet-...`) and the tracker's own
#: non-model strings do not match and keep the substring rule.
_MODEL_ID = re.compile(r"^claude-([a-z]+(?:-\d{1,2})+)(?:-\d{8})?$")


def model_key(model: str) -> str | None:
    """A current-style model id without `claude-` and any date suffix, or None."""
    m = _MODEL_ID.match(model.lower())
    return m.group(1) if m else None


def auto_family(model: str, credits: dict) -> str | None:
    """The family of its own a model id gets when no table family lists it, or None.

    Every id already filed under a family is in that family's `members`. A current-style
    id that is in none of them is a model the table has not met -- Sonnet 5.5, Haiku 5.5 --
    and it becomes its own family, keyed by the id without `claude-` and any date suffix,
    rather than being credited at an older model's rate through a substring match (the
    reason Opus 5.5 was split from Opus on 2026-09-23). Nobody has to add it anywhere:
    tools/model_rates.py fits it like any family once stretches carry it, and until then
    detection values it at its list-price ratio to the Opus anchor (`list_price_ratio`).
    A table without `members` lists (a fixture, an archive) never auto-files anything.
    """
    key = model_key(model)
    if key is None:
        return None
    rows = credits.get("per_family") or {}
    if not any("members" in row for row in rows.values()):
        return None
    full = f"claude-{key}"
    if any(full in (row.get("members") or ()) for row in rows.values()):
        return None
    return key


def family_label(fam: str) -> str:
    """How a family is named in a sentence: `opus-5-5` reads "Opus 5.5"."""
    head, *version = fam.split("-")
    return head.capitalize() + (" " + ".".join(version) if version else "")


def list_price_model(fam: str, credits: dict) -> str:
    """The data/prices.json row a family lists at: the table's choice, else its own id."""
    return (credits.get("list_price_model") or {}).get(fam) or f"claude-{fam}"


def list_price_ratio(fam: str, credits: dict, prices: dict | None = None) -> float | None:
    """A family's API input list price over the Opus anchor's, from data/prices.json, or None.

    None when either row is missing from the dollar table: a model the price table does not
    list has no list price to infer a rate from, and is reported unpriced rather than guessed.
    """
    if prices is None:
        prices = _list_prices()
    num = prices.get(list_price_model(fam, credits)) or {}
    den = prices.get(list_price_model("opus", credits)) or {}
    if not num.get("input") or not den.get("input"):
        return None
    return num["input"] / den["input"]


@functools.lru_cache(maxsize=1)
def _list_prices() -> dict:
    """data/prices.json, read once per process: `list_price_ratio` runs per stretch."""
    return json.loads(PRICES_PATH.read_text(encoding="utf-8"))


def rates(fam: str, credits: dict) -> tuple[float, float] | None:
    """(input, output) credits per token for a family, or None when it has no single rate.

    Fable has none. Its rate is solved from the stretches at publish time
    (`fable_interval`) and published as an interval, because the data bound its input
    rate only loosely and do not separate its output ratio at all.
    """
    row = credits["per_family"].get(fam)
    if row is None:
        # A family of its own (`auto_family`): the table has never met it, so it has no rate.
        return None
    rate_in, rate_out = _rate(row.get("input")), _rate(row.get("output"))
    return None if rate_in is None or rate_out is None else (rate_in, rate_out)


def cache_read_weight(credits: dict) -> float:
    return float(credits.get("cache_read_weight", 0) or 0)


def load_model_rates(path: Path | None = None, *, raw: dict | None = None) -> dict:
    """The `measured_rates` block of `history/model-rates.json`, or `{}` when there is none.

    An empty block is not an error. A fixture, or an archive from before the file existed,
    has no measurement to publish, and the honest publish is then a status sentence on every
    per-model row rather than the reference table quietly standing in for a measurement. Only
    the Opus anchor survives a missing file, because the anchor is what a credit means here
    rather than a figure about Opus (`family_rate`).
    """
    if raw is None:
        p = Path(path) if path is not None else MODEL_RATES_PATH
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
    block = (raw or {}).get("measured_rates")
    if not isinstance(block, dict) or not isinstance(block.get("per_family"), dict):
        return {}
    return block


#: What a row says when there is no measured-rate source at all to read a rate from.
NO_MEASURED_SOURCE = ("no measured rate source: history/model-rates.json carries no "
                      "measured_rates block")


@dataclass(frozen=True)
class FamilyRate:
    """One family's credit rate as every published per-model figure divides by it.

    Exactly one of three shapes, never two:

    - a rate with a value (`input`/`output` set, `interval` beside it where the fit has one),
    - an interval and no value, with `status` saying the rate is not yet identified,
    - no value and no interval, with `status` saying it is not measurable at all.

    `rate_source` is "measured" or "reference". Only Opus reads "reference": it is the unit
    anchor -- 10/15 credits per input token, five times that per output token -- and every
    other family's rate is measured relative to it, so nothing here tests Opus itself.
    `reference_input`/`reference_output` carry the January table's figure for the family so a
    page can draw it beside the measurement. Nothing divides by them.
    """
    family: str
    input: float | None
    output: float | None
    input_interval: tuple[float, float] | None
    output_interval: tuple[float, float] | None
    status: str | None
    rate_source: str
    anchor: bool
    reference_input: float | None
    reference_output: float | None
    detail: dict

    @property
    def input_edges(self) -> tuple[float | None, float | None]:
        """(low, high) of the input rate: the value itself when the rate has no interval."""
        if self.input_interval:
            return tuple(self.input_interval)
        return (self.input, self.input)

    @property
    def output_edges(self) -> tuple[float | None, float | None]:
        if self.output_interval:
            return tuple(self.output_interval)
        return (self.output, self.output)


def family_rate(fam: str, credits: dict, model_rates: dict | None) -> FamilyRate:
    """The measured rate for one family, with the reference figure carried beside it.

    The output rate is the family's output multiplier times its input rate -- 5 for every
    family in the reference table, and the fit assumes the same 5 -- except where the table
    carries unresolved candidates (`output_ratio_candidates`, Fable's 3 and 5), in which case
    the output interval spans the cheapest candidate against the low edge and the dearest
    against the high one, which is the shape the Fable row has published since PR #65.
    """
    row = (model_rates or {}).get("per_family", {}).get(fam) or {}
    reference = rates(fam, credits)
    ref_in, ref_out = reference if reference else (None, None)
    candidates = credits["per_family"].get(fam, {}).get("output_ratio_candidates")
    anchor = bool(row.get("anchor"))
    if not row:
        # No measurement at all. The anchor is definitional and stands; everything else says so.
        anchor = fam == "opus"
        value_in, value_out = (ref_in, ref_out) if anchor else (None, None)
        return FamilyRate(fam, value_in, value_out, None, None,
                          None if anchor else NO_MEASURED_SOURCE,
                          "reference" if anchor else "measured", anchor, ref_in, ref_out,
                          {"source_file": str(MODEL_RATES_PATH.name)})
    mult = row.get("output_multiplier") or 5
    value_in = row.get("input")
    interval_in = tuple(row["interval"]) if row.get("interval") else None
    inferred = row.get("inferred") if value_in is None and not interval_in and not anchor else None
    if inferred and inferred.get("input") is not None:
        # The fit cannot measure this family yet, and the page shows it anyway at an inferred
        # rate, marked as such: the reference table's row, or the list-price ratio to Opus
        # where the table has none (tools/model_rates.py `inferred_rate`). No interval, no
        # status; what the fit did say stays in the detail under `measured_status`.
        mult = inferred.get("output_multiplier") or mult
        detail = {k: row[k] for k in ("n_fits", "per_fit", "why", "pooled",
                                      "max_share_of_a_clean_stretch") if k in row}
        detail.update({"measured_status": row.get("status"),
                       "inferred_from": inferred.get("inferred_from"),
                       "inferred_basis": inferred.get("basis"),
                       "times_opus": inferred.get("times_opus"),
                       "output_multiplier": mult})
        return FamilyRate(fam, inferred["input"], inferred["input"] * mult, None, None, None,
                          "inferred", False, ref_in, ref_out, detail)
    # The anchor's output rate is the table's own 50/15 rather than 5 x 10/15, so the exact
    # fifteenth the table stores is the one published.
    value_out = (ref_out if anchor and ref_out is not None else
                 value_in * mult if value_in is not None else None)
    if interval_in and candidates:
        interval_out = (interval_in[0] * min(candidates), interval_in[1] * max(candidates))
    elif interval_in:
        interval_out = (interval_in[0] * mult, interval_in[1] * mult)
    else:
        interval_out = None
    detail = {k: row[k] for k in ("n_fits", "agree", "per_fit", "times_opus", "provisional",
                                  "times_opus_interval", "why", "max_share_of_a_clean_stretch",
                                  "rate_in_use", "list_times_opus")
              if k in row}
    detail["output_multiplier"] = mult
    if candidates:
        detail["output_ratio_candidates"] = list(candidates)
    return FamilyRate(fam, value_in, value_out, interval_in, interval_out, row.get("status"),
                      row.get("rate_source", "measured"), anchor, ref_in, ref_out, detail)


def input_side(tok: dict, weight: float) -> float:
    """The input-rate tokens of one model's bundle.

    Cache writes join the input side at the plain input rate: on a subscription the
    meter does not charge the API's 1.25x write premium. Cache reads join it at
    `weight` of that rate, which the table publishes as 0 with an uncertainty range
    (see `cache_read_weight_range`). `cache_write_1h` is already inside `cache_write`
    (tracker/turns.py), and the credit model has no one-hour premium, so counting it
    again would charge those tokens twice.
    """
    return tok.get("input", 0) + tok.get("cache_write", 0) + tok.get("cache_read", 0) * weight


def raw_tokens(tok: dict) -> int:
    return sum(tok.get(c, 0) for c in ("input", "output", "cache_read", "cache_write"))


@dataclass(frozen=True)
class Priced:
    """One stretch's tokens split into what a published rate prices and what it does not.

    `priced` is False when a model in the stretch has no family at all, so the
    stretch cannot be priced and must be left out rather than under-counted.
    `fable_input`/`fable_output` are kept apart from `known` because Fable has no
    single rate to fold in -- they are what an interval or a solve is applied to.

    Two token totals, because "what share of this stretch was Fable" has two honest
    answers. `raw` counts every token of every class. `charged` counts only the tokens
    the meter charges for: the input side at the current cache-read weight, plus
    output. At a cache-read weight of 0 they differ by the whole cache-read column,
    which is 97% of a typical stretch, so a share taken against one is nothing like a
    share taken against the other.
    """
    priced: bool
    known: float
    fable_input: float
    fable_output: float
    raw: int
    charged: float


def price_tokens(tokens: dict, credits: dict, weight: float | None = None) -> Priced:
    """One stretch's tokens priced in credits.

    Cache writes are priced at the input rate and cache reads at zero on purpose. These
    are the meter's credit rates (docs/reference-2026-09-20-shellac-credits-model.md: a
    subscription charges a cache write as ordinary input and a cache read as nothing),
    not the API list prices in data/prices.json, where a cache write carries the 1.25x
    premium. Pricing writes at 1.25x here would understate nothing in the meter; it is
    what loaded the retracted 0.155 cache-read weight onto reads in the dollar model.
    """
    weight = cache_read_weight(credits) if weight is None else weight
    known = fable_in = fable_out = charged = 0.0
    raw = 0
    priced = True
    for model, tok in tokens.items():
        if not isinstance(tok, dict):
            continue
        at_in, at_out = input_side(tok, weight), tok.get("output", 0)
        raw += raw_tokens(tok)
        charged += at_in + at_out
        fam = family(model, credits)
        pair = rates(fam, credits) if fam else None
        if fam is None or fam not in credits["per_family"]:
            priced = False
        elif pair is None:
            fable_in += at_in
            fable_out += at_out
        else:
            known += at_in * pair[0] + at_out * pair[1]
    return Priced(priced, known, fable_in, fable_out, raw, charged)


@dataclass(frozen=True)
class HarnessRun:
    """A span in which the tracker was driving the account itself, not observing it."""
    account: str
    start: datetime
    end: datetime
    reason: str


def harness_runs(path: Path | None = None) -> list[HarnessRun]:
    """Every span the tracker's own instruments occupied, per account.

    One row per run in `history/harness-runs.jsonl`, written by `tools/harness_runs.py`:
    the completed probes (whose interval is the probes.jsonl row's `ts` to
    `ts + elapsed_s`), the effort-matrix run, and the probes that aborted or crashed
    part way and never wrote a row at all. Outlier and output probe rows count too:
    they moved the account's meter like any other run.

    A missing file yields no runs, which excludes nothing. That is the honest reading of
    an absent record rather than a guess at what it would have held, and it is what a
    price table from before the file existed, or a fixture, gets.
    """
    p = Path(path) if path is not None else HARNESS_RUNS_PATH
    out: list[HarnessRun] = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not row.get("account") or not row.get("start") or not row.get("end"):
            continue
        out.append(HarnessRun(row["account"], datetime.fromisoformat(row["start"]),
                              datetime.fromisoformat(row["end"]), run_reason(row)))
    return sorted(out, key=lambda r: (r.account, r.start))


def run_reason(row: dict) -> str:
    """The published one-line description of a harness run: what it was and how it ended."""
    model = "/".join(str(x) for x in (row.get("model"), row.get("effort")) if x)
    what = " ".join(x for x in (row.get("kind") or "run", model) if x)
    ended = row.get("outcome") or "ran"
    detail = f": {row['detail']}" if row.get("detail") else ""
    return f"{what} {ended}{detail} from {str(row['start'])[:19]}Z"


def overlapping_run(runs: list[HarnessRun], account: str, start: datetime,
                    end: datetime) -> HarnessRun | None:
    """The first run of this account whose span overlaps [start, end], or None.

    Half-open on both sides, so a stretch that only meets a run at an instant is kept.
    """
    for run in runs:
        if run.account == account and start < run.end and run.start < end:
            return run
    return None


def stretches_by_account(*reports: dict | None) -> dict[str, list[dict]]:
    """Every report's `accounts.<name>.stretches`, merged by account name.

    history/gs-passive.json carries two accounts, history/masterrig-passive.json one.
    A report may also be the bare `{"stretches": [...]}` shape an older masterrig run
    wrote; it is then filed under `masterrig`.
    """
    out: dict[str, list[dict]] = {}
    for report in reports:
        if not report:
            continue
        accounts = report.get("accounts")
        if not accounts and report.get("stretches") is not None:
            accounts = {"masterrig": {"stretches": report["stretches"]}}
        for name, body in (accounts or {}).items():
            out.setdefault(name, []).extend(body.get("stretches") or [])
    return out


def clean_stretches(by_account: dict[str, list[dict]], runs: list[HarnessRun], *,
                    require: str = "capture_status", exempt: tuple[str, ...] = (),
                    min_delta_pct: float = MIN_DELTA_PCT) -> dict[str, list[dict]]:
    """The stretches that are evidence about ordinary use, per account (see module docstring).

    `require` names the acceptance column a stretch must read "accepted" in;
    accounts in `exempt` skip that test. `None` skips it everywhere. A stretch overlapping
    a harness run, or marked `cloud_session`, is left out whatever its columns say.
    """
    kept: dict[str, list[dict]] = {}
    for account, stretches in by_account.items():
        rows = []
        for st in stretches:
            delta = st.get("delta_pct") or 0
            if delta < min_delta_pct or not st.get("tokens"):
                continue
            # A record with no span cannot be tested against a harness run. Every real
            # stretch carries both stamps; a fixture may not, and a missing stamp must
            # not be read as "overlaps nothing" in one place and crash in another.
            start, end = st.get("start"), st.get("end")
            if start and end and overlapping_run(runs, account, datetime.fromisoformat(start),
                                                 datetime.fromisoformat(end)) is not None:
                continue
            # Cloud-session work (tracker/cloud_sessions.py) is left out like a harness run:
            # its turns ran in Anthropic's cloud, teleported back or never seen here, so the
            # stretch is not a reading of this host's ordinary use (issue #133).
            if st.get("cloud_session"):
                continue
            if require and account not in exempt and st.get(require) != "accepted":
                continue
            rows.append(st)
        kept[account] = rows
    return kept


#: masterrig enters the rate fits, and the before-and-after comparison, from this instant
#: and not before. From 2 to 5 September the takeoff pipeline on gs ran on the personal
#: account, so its meter moved on work masterrig's transcripts never saw -- all 18 of its
#: zero-token stretches that moved the meter fall in those four days -- and from June to
#: August its transcripts had been cleaned up, so those stretches hold nothing. From 6
#: September the account is Claude Code on masterrig alone
#: (docs/findings-2026-09-23-masterrig-admitted.md).
MASTERRIG_FROM = datetime(2026, 9, 6, tzinfo=timezone.utc)


def spans_cut(st: dict) -> bool:
    """True when a stretch starts before CUT_AT and ends after it.

    Such a stretch belongs to neither side of the change: its meter movement is part one
    regime and part the other, so it is dropped from both rather than filed by its start.
    """
    start, end = st.get("start"), st.get("end")
    return bool(start and end and datetime.fromisoformat(start) < CUT_AT < datetime.fromisoformat(end))


def rate_fit_stretches(by_account: dict[str, list[dict]],
                       runs: list[HarnessRun]) -> dict[str, list[dict]]:
    """The selection the rate fits run over, and the before-and-after comparison with them.

    `clean_stretches` with the capture test on every account (masterrig included, now that
    its capture column is filled), masterrig only from MASTERRIG_FROM, and no stretch that
    spans the cut. tools/model_rates.py builds its fits from exactly this, and `across_cut`
    reads the same stretches, so the five-hour change across the cut is measured on the
    stretches the rates it prices with were measured on (docs/findings-2026-09-23-pooled-rates.md).
    """
    kept = clean_stretches(by_account, runs, require="capture_status")
    if "masterrig" in kept:
        kept["masterrig"] = [st for st in kept["masterrig"]
                             if datetime.fromisoformat(st["start"]) >= MASTERRIG_FROM]
    return {account: [st for st in rows if not spans_cut(st)] for account, rows in kept.items()}


def pure_family_rows(clean: dict[str, list[dict]], credits: dict, fam: str,
                     weight: float | None = None) -> dict[str, list[dict]]:
    """Per account, the stretches whose every model belongs to `fam`, priced per 1%.

    A pure-Opus stretch is the one reading with no fitted parameter in it: every
    token in it is priced at a rate the reference publishes, so the credits a percent
    of the meter buys falls straight out of the division.
    """
    weight = cache_read_weight(credits) if weight is None else weight
    out: dict[str, list[dict]] = {}
    for account, stretches in clean.items():
        rows = []
        for st in stretches:
            tokens = st["tokens"]
            if not all(family(m, credits) == fam for m in tokens):
                continue
            priced = price_tokens(tokens, credits, weight)
            if not priced.priced or priced.known <= 0:
                continue
            rows.append({"account": account, "start": st.get("start"), "end": st.get("end"),
                         "delta_pct": st["delta_pct"], "credits": priced.known,
                         "credits_per_pct": priced.known / st["delta_pct"],
                         # The stretch's own token counts, carried so `window_tokens` can
                         # read the same cluster without a second selection rule.
                         "tokens": tokens})
        out[account] = sorted(rows, key=lambda r: r["credits_per_pct"])
    return out


def selection_sentence(min_delta_pct: float = MIN_DELTA_PCT) -> str:
    """The acceptance rule of the module docstring as one published sentence, in one place.

    `window_credits` and `window_tokens` read the same cluster, so they state the same
    rule; stating it twice is how the two sentences drift apart while the code does not.
    """
    return (f"A stretch counts when it moved the meter at least {min_delta_pct:g}%, carries "
            f"tokens, does not overlap one of the tracker's own runs on its own account (a "
            f"probe row or the effort-matrix run), and reads capture_status 'accepted'.")


def five_hour_window_changes(across: dict | None) -> dict[str, float]:
    """Each qualifying account's own measured change in the five-hour window, by label.

    The accounts `five_hour_window_change` reads (a capture reading at all, and at least
    FIVE_HOUR_MIN_SIDE stretches on each side of the cut), before it takes their median:
    the tokens-per-week change pairs each of them with the same account's own change in
    windows per week.
    """
    per_account = (across or {}).get("per_account") or {}
    return {label: row["change_pct"] for label, row in per_account.items()
            if row.get("change_pct") is not None
            and (row.get("n_with_capture") or 0) > 0
            and (row.get("n_before") or 0) >= FIVE_HOUR_MIN_SIDE
            and (row.get("n_after") or 0) >= FIVE_HOUR_MIN_SIDE}


def five_hour_window_change(across: dict | None) -> dict | None:
    """The measured change in the five-hour window across the cut, as one percent.

    `across_cut` reads every watched account's credits per 1% either side of 14 September,
    but not every account's two medians are a reading of that account's own window: one
    with no usable capture column (`n_with_capture` 0) divides meter movement this host
    never saw by transcripts it did see, and a side of two or three stretches is a median
    of two or three stretches. Only accounts with a capture reading at all and at least
    FIVE_HOUR_MIN_SIDE stretches on each side are read, and the figure is the median of
    their own `change_pct` -- on today's data one account, at +8.6%.

    None when no account qualifies, and the publisher then states no tokens-per-week
    change and scales no window by it, rather than compounding a number nobody measured.
    """
    qualifying = five_hour_window_changes(across)
    if not qualifying:
        return None
    return {"pct": round(median(list(qualifying.values())), 1), "accounts": sorted(qualifying)}


def cut_side(row: dict) -> str | None:
    """Which side of the announced change a cluster row's stretch started on.

    The stretch's own `start` against CUT_AT, the same instant and the same rule
    `across_cut` splits by. A row with no start stamp is placed on neither side: every
    real stretch carries one, and a fixture that does not must not be read as pre-cut.
    """
    start = row.get("start")
    return None if not start else ("before" if datetime.fromisoformat(start) < CUT_AT else "after")


def current_cluster_rule(n_before: int, n_after: int,
                         five_hour_pct: float | None) -> tuple[str, float, str]:
    """(which side states the window now, the factor it is scaled by, the published source).

    The pure-family cluster spans 14 September, and a median over the whole of it is
    mostly a pre-cut reading published as the current one: ten of today's twelve stretches
    are one account's from 9-10 September. So the after cluster states the window itself
    once it holds MIN_AFTER_CLUSTER readings; below that the before cluster carries it,
    scaled by the measured five-hour change across the cut (`five_hour_window_change`).
    With no measured change to scale by, the before cluster is published unscaled, and
    with no stretch placeable on either side the whole cluster is -- each case naming
    itself in `current_source` rather than publishing a number with no provenance.
    """
    if n_after >= MIN_AFTER_CLUSTER:
        return "after", 1.0, "after_cluster"
    if n_before:
        if five_hour_pct is not None:
            return "before", 1 + five_hour_pct / 100, "before_cluster_scaled_by_five_hour_change"
        return "before", 1.0, "before_cluster_unscaled"
    if n_after:
        return "after", 1.0, "after_cluster"
    return "whole", 1.0, "whole_cluster_unsplit"


CURRENT_METHOD = (
    "the pure-family cluster is split on cut_at by each stretch's own start. The after "
    f"cluster states the current window once it holds {MIN_AFTER_CLUSTER} readings; below "
    "that the before cluster states it, scaled by the median five-hour window change across "
    "the cut over the accounts whose capture column can carry one. `current_source` names "
    "which of the two the published value and interval came from; `n` counts the whole "
    "cluster, and each side carries its own count.")

#: `current_source` of a regime opened by a measured known-date change and stated by the
#: previous regime times that change, or by the regime's own cluster once it is thick enough.
KNOWN_DATE_SCALED_SOURCE = "previous_regime_scaled_by_known_date_change"
#: `current_source` of a regime opened by a change whose joint fit cannot yet separate the
#: new family's rate from the limit change: the previous regime's value, carried.
KNOWN_DATE_UNSCALED_SOURCE = "previous_regime_carried_fit_not_separable"
KNOWN_DATE_CLUSTER_SOURCE = "known_date_regime_cluster"
#: `current_source` of a regime in a run of regimes joined by boundaries the tracker could not
#: measure, stated by the whole run's cluster in the run's own family (`window_runs`).
RUN_CLUSTER_SOURCE = "unmeasured_boundary_run_cluster"

KNOWN_DATE_METHOD = (
    " After that, every change measured on the two meters (`five_hour_on_meters`, the "
    "windows-per-week ratio across a change candidate; `measurable`, dated after cut_at) opens "
    "a new regime in `regimes`, from the candidate's own instant, whether it is published "
    "(`applies`) or withheld. Each such regime is measured in the family "
    "actually in use: its regime family is the family with the most pure clean stretches "
    "starting in it (`regime_family`, `n_regime_family`). A boundary the tracker could not "
    "measure (a change whose joint fit cannot separate the new family's rate from the limit "
    "change, or a withheld one: ADR 0001 rules 8 and 9) does not split the window cluster: the regimes it joins form one run, from the "
    "last measured boundary (the cut, or a change whose fit separates), and `run_from` names "
    "its first instant. A run of two or more regimes whose pure clean stretches, starting "
    f"anywhere in it, hold {MIN_AFTER_CLUSTER} readings of its most common family states every "
    "regime in it with that family's direct median and spread "
    f"(`{RUN_CLUSTER_SOURCE}`); the 14 September regime's own rule above gives way to it. "
    "Where that family is not the anchor and the boundary opening the run has a five-hour "
    "change the meters measured (the change across the cut, or a separable change's g), the "
    "run's anchor-unit `value` and `interval`, and every earlier regime's figure in the run "
    "family's units, go through the bridge rate rather than the fitted one: the previous "
    "regime's anchor window times that change, over the run's direct window. The step at "
    "that boundary is then the meters' change. `bridge_rate`, `bridge_rate_interval` (the "
    "same over the run's highest and lowest reading) and `bridge_rate_source` are published "
    "on the run's first regime; the fitted rate stays published as the fitted figure, and "
    "other families still convert from the anchor at their own rates. "
    "Otherwise each regime is measured on its own: once its regime family holds "
    f"{MIN_AFTER_CLUSTER} readings in the regime, the regime is their median tokens per 1% "
    "times 100, read straight off their token counts with no rate in it, published as "
    "`measured_value` and `measured_interval`. `value` and `interval` give the same window in "
    "the anchor family's tokens, converted at the two families' point rates where the regime "
    "family is not the anchor. Below that many readings the regime carries the previous "
    "regime's figure in the same family's units, and `measured_family` names the family the "
    "figure was measured in "
    f"(`{KNOWN_DATE_UNSCALED_SOURCE}`), because the joint fit cannot yet separate the new "
    "family's rate from the five-hour limit change g or the change is withheld. Only where the "
    "joint fit does separate them and the change is published (`applies`) is the previous "
    "regime's figure scaled by g instead, each interval edge by g's same "
    "edge. The window interval is a spread of readings, not a standard error. The published "
    "value and interval are the newest regime's. Where the newest regime is measured in a "
    "family other than the anchor, that family's row in `per_family` is its direct reading, "
    "never a conversion.")


def known_date_changes(meters: dict | None) -> list[dict]:
    """The measured changes that open window regimes, oldest first, with their window ratio.

    The candidates of the `five_hour_on_meters` block that are `measurable` (a combined
    reading, dated after CUT_AT: the 14 September split already stands for everything before
    it; `five_hour_meter_boundaries`). Each carries its instant, its scope, and the five-hour
    window ratio and interval, from the published rounded percents so a reader can redo the
    arithmetic. The ratio is the direct window change (credits per 1% of the five-hour
    meter, `window_change`) at its point estimate, and only for a change that `applies` on
    that measurement (ADR 0001 rule 9: plan-wide, its interval excluding no change). A
    change certified on the weekly limit alone, one with no window change, or a withheld one
    has none, and
    `window_scaled` False tells `window_regimes` to carry the previous regime and not to
    split the run there. Nothing here names a date; the boundaries are the candidate records'
    own.
    """
    out = []
    for cand in five_hour_meter_boundaries(meters):
        at = datetime.fromisoformat(cand["at"])
        if at <= CUT_AT:
            continue
        # Only a published change may scale a window, and only by the measurement it was
        # certified on: the window change, certified on its own (ADR 0001 rule 9). A
        # change certified on the weekly limit alone steps the week, never the window.
        window = cand.get("window_change")
        scaled = (bool(cand.get("applies")) and cand.get("change_pct") is not None
                  and bool(cand.get("interval_pct"))
                  and (window is None or bool(window.get("certified"))))
        lo, hi = cand["interval_pct"] if scaled else (0.0, 0.0)
        pct = cand["change_pct"] if scaled else 0.0
        out.append({"at": at, "family": cand["family"], "change_pct": pct,
                    "scope": (cand.get("scope") or {}).get("state") or "undetermined",
                    "window_scaled": scaled, "ratio": 1 + pct / 100,
                    "ratio_interval": (1 + lo / 100, 1 + hi / 100)})
    return sorted(out, key=lambda c: c["at"])


def regime_index(row: dict, changes: list[dict]) -> int | None:
    """Which window regime a cluster row's stretch started in: 0 before CUT_AT, then one
    more for every known-date change at or before its start. None with no start stamp."""
    side = cut_side(row)
    if side is None:
        return None
    if side == "before":
        return 0
    start = datetime.fromisoformat(row["start"])
    return 1 + sum(1 for c in changes if c["at"] <= start)


def current_method(changes: list[dict]) -> str:
    """CURRENT_METHOD, plus the known-date regimes and, where one applied, which."""
    if not changes:
        return CURRENT_METHOD + KNOWN_DATE_METHOD
    applied = "; ".join(f"from {_utc(c['at'])} ({c['family']} first turn), "
                        + (f"{c['change_pct']:+g}%" if c.get("window_scaled", True)
                           else "window carried, joint fit not separable or change withheld, "
                                "unless the regime's own family states it")
                        for c in changes)
    return (CURRENT_METHOD + KNOWN_DATE_METHOD
            + f" Known-date changes applied: {applied}.")


def _scaled(fig: dict, ratio: float, r_lo: float, r_hi: float) -> dict:
    """{value, interval} times a ratio, each interval edge times the ratio interval's same edge."""
    return {"value": fig["value"] * ratio if fig["value"] is not None else None,
            "interval": ([fig["interval"][0] * r_lo, fig["interval"][1] * r_hi]
                         if fig["interval"] else None)}


def window_runs(changes: list[dict]) -> list[list[int]]:
    """The window regimes grouped into runs, oldest first, as lists of regime indices.

    A boundary the tracker measured splits two runs: the 14 September cut, and every change
    whose joint fit separates the new family's rate from the limit change (`window_scaled`).
    A change it could not measure does not: the regimes either side of it are one run, so
    one cluster. Regime 0, before the cut, is always a run of its own.
    """
    runs = [[0], [1]]
    for k, change in enumerate(changes, start=2):
        if change.get("window_scaled", True):
            runs.append([k])
        else:
            runs[-1].append(k)
    return runs


def window_regimes(counts: dict[int, int], own, five_hour_pct: float | None,
                   changes: list[dict], direct=None,
                   anchor_fam: str | None = None) -> tuple[list[dict], str]:
    """The window regimes oldest first, and the current one's `current_source`.

    `own(key, scale)` is one cluster as {value, interval} (unrounded is fine), where key is
    a regime index or "whole". Regime 0 is the before cluster as measured. Regime 1 is the
    14 September split exactly as `current_cluster_rule` states it, over the stretches
    that started before the first known-date change. Both are in `anchor_fam`.

    Every later regime is measured in the family actually in use. `direct(ks)`, where given,
    returns the cluster of the regimes `ks` (a tuple of indices) as {family, n, measured,
    anchor}: the family with the most pure clean stretches starting in them, how many, their
    direct figure in that family's tokens, and the same figure in anchor-family units.

    Regimes joined by boundaries the tracker could not measure form one run (`window_runs`).
    A run of two or more regimes whose cluster holds MIN_AFTER_CLUSTER readings states every
    regime in it with that one cluster (`RUN_CLUSTER_SOURCE`, `run_from` the run's first
    instant). Where the run's family is not the anchor and the boundary opening the run has a
    five-hour change the meters measured (`five_hour_pct` across the cut, a separable g after
    it), the run's anchor-unit figure goes through the bridge rate rather than the fitted one:
    the previous regime's anchor window times that change over the run's direct window, so
    the step there is the meters' change (`bridge_rate`, on the run's first regime). Any
    other regime follows the rules below. Without it the regime's
    cluster is the anchor family's (`own`). With MIN_AFTER_CLUSTER readings the regime is
    that cluster. Below that it is the previous regime times the change's g where the joint
    fit separates g from the new family's rate, and the previous regime carried, in the same
    family's units, where it does not. `measured_family`, `measured_value` and
    `measured_interval` publish the figure in the family it was measured in; `value` and
    `interval` stay in anchor-family units. `regime_family` and `n_regime_family` name the
    regime's own family and its count whether or not that count was enough to state it.
    """
    def row(fig: dict, measured: dict, fam: str | None, source: str, frm, until,
            regime_fam: str | None, n: int, run_from=None) -> dict:
        return {"from": frm, "until": until, "value": fig["value"], "interval": fig["interval"],
                "source": source, "measured_family": fam,
                "measured_value": measured["value"], "measured_interval": measured["interval"],
                "regime_family": regime_fam, "n_regime_family": n, "run_from": run_from}

    # The runs whose own cluster is thick enough to state them, by their first regime.
    run_clusters: dict[int, dict] = {}
    for run in window_runs(changes):
        cluster = direct(tuple(run)) if direct and len(run) > 1 else None
        if cluster and cluster["n"] >= MIN_AFTER_CLUSTER:
            for k in run:
                run_clusters[k] = cluster
    froms = [None, CUT_AT.isoformat(), *(_utc(c["at"]) for c in changes)]

    def opening_change(k: int) -> float | None:
        """The five-hour change the meters measured at the boundary opening regime k, as a
        ratio: the change across the cut for regime 1, a separable change's g after it."""
        if k == 1:
            return 1 + five_hour_pct / 100 if five_hour_pct is not None else None
        change = changes[k - 2]
        return change["ratio"] if change.get("window_scaled", True) else None

    bridges: dict[int, dict] = {}

    def run_row(k: int, until, prev: dict) -> dict:
        cluster = run_clusters[k]
        first = min(j for j in run_clusters if run_clusters[j] is cluster)
        anchor_fig, extra = cluster["anchor"], {}
        if k == first and cluster["family"] != anchor_fam:
            g = opening_change(k)
            measured = cluster["measured"]
            if g is not None and prev["value"] and measured["value"] and measured["interval"]:
                # The bridge rate: the run family's tokens priced so that the step at the
                # opening boundary equals the five-hour change the meters measured there.
                rate = prev["value"] * g / measured["value"]
                lo, hi = measured["interval"]
                bridges[first] = {
                    "bridge_rate": rate,
                    "bridge_rate_interval": [prev["value"] * g / hi, prev["value"] * g / lo],
                    "bridge_rate_source": (
                        f"derived: the rate that makes the step at {froms[k]} equal the "
                        f"five-hour change the meters measured there ({(g - 1) * 100:+.1f}%): "
                        f"the previous regime's {anchor_fam} window times that change, over "
                        f"the run's direct {cluster['family']} window; its interval is the same "
                        f"over the run's highest and lowest reading")}
                extra = bridges[first]
        if first in bridges:
            rate = bridges[first]["bridge_rate"]
            anchor_fig = _scaled(cluster["measured"], rate, rate, rate)
        return dict(row(anchor_fig, cluster["measured"], cluster["family"], RUN_CLUSTER_SOURCE,
                        froms[k], until, cluster["family"], cluster["n"], froms[first]), **extra)

    before = own(0, 1.0)
    regimes = [row(before, before, anchor_fam, "before_cluster", None, CUT_AT.isoformat(),
                   anchor_fam, counts.get(0, 0))]
    if 1 in run_clusters:
        regimes.append(run_row(1, None, regimes[0]))
    else:
        chosen, factor, source = current_cluster_rule(counts.get(0, 0), counts.get(1, 0),
                                                      five_hour_pct)
        fig = own({"before": 0, "after": 1, "whole": "whole"}[chosen], factor)
        regimes.append(row(fig, fig, anchor_fam, source, CUT_AT.isoformat(), None,
                           anchor_fam, counts.get(1, 0)))
    for k, change in enumerate(changes, start=2):
        prev = regimes[-1]
        prev["until"] = _utc(change["at"])
        if k in run_clusters:
            regimes.append(run_row(k, None, prev))
            continue
        cluster = direct((k,)) if direct else None
        if cluster is None:
            mine = own(k, 1.0)
            cluster = {"family": anchor_fam, "n": counts.get(k, 0), "measured": mine, "anchor": mine}
        unmeasured = prev["value"] is None and prev["measured_value"] is None
        if cluster["n"] >= MIN_AFTER_CLUSTER or (unmeasured and cluster["n"]):
            fig, measured, fam = cluster["anchor"], cluster["measured"], cluster["family"]
            source = KNOWN_DATE_CLUSTER_SOURCE
        elif change.get("window_scaled", True):
            r_lo, r_hi = change["ratio_interval"]
            fig = _scaled({"value": prev["value"], "interval": prev["interval"]},
                          change["ratio"], r_lo, r_hi)
            measured = _scaled({"value": prev["measured_value"], "interval": prev["measured_interval"]},
                               change["ratio"], r_lo, r_hi)
            fam, source = prev["measured_family"], KNOWN_DATE_SCALED_SOURCE
        else:
            # No separable fit, so no estimate of the limit change, and too few readings to
            # state the regime: the previous regime's figure carries, in its own family.
            fig = {"value": prev["value"], "interval": prev["interval"]}
            measured = {"value": prev["measured_value"], "interval": prev["measured_interval"]}
            fam, source = prev["measured_family"], KNOWN_DATE_UNSCALED_SOURCE
        regimes.append(row(fig, measured, fam, source, _utc(change["at"]), None,
                           cluster["family"], cluster["n"]))
    return regimes, regimes[-1]["source"]


def _rounded_regimes(regimes: list[dict], unit: float = 1.0) -> list[dict]:
    """The regimes as published: values and interval edges times `unit`, rounded."""
    def edges(iv):
        return [_round(x * unit) for x in iv] if iv else None
    return [dict(r, value=_round(r["value"] * unit if r["value"] is not None else None),
                 interval=edges(r["interval"]),
                 measured_value=_round(r["measured_value"] * unit
                                       if r.get("measured_value") is not None else None),
                 measured_interval=edges(r.get("measured_interval")),
                 **({"bridge_rate": round(r["bridge_rate"], 4),
                     "bridge_rate_interval": [round(x, 4) for x in r["bridge_rate_interval"]]}
                    if r.get("bridge_rate") is not None else {}))
            for r in regimes]


def window_credits(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
                   fam: str = "opus", weight: float | None = None,
                   five_hour_pct: float | None = None, meters: dict | None = None) -> dict:
    """The five-hour window in credits, from the pure-`fam` cluster, as it is NOW.

    `value` is a full window (100% of the meter), so it is the median credits per 1%
    times 100; `credits_per_pct` keeps the per-percent median the median was taken
    of. `interval` is the readings' own min to max, which is the spread of the
    readings and not a confidence interval -- there is no error model here, only
    a dozen readings of the same quantity.

    The cluster spans the 14 September change, and a median over the whole of it is
    mostly a pre-cut reading: ten of today's twelve stretches are one account's from
    9-10 September, two are another's from after the cut. So the cluster is split on
    `cut_at` by each stretch's own start and published as `before` and `after`, and
    `value`, `credits_per_pct` and `interval` are the CURRENT window -- the after
    cluster where it is thick enough to state one, else the before cluster scaled by
    the measured five-hour change (`current_cluster_rule`, `current_source`). `n`
    stays the whole cluster's count; each side carries its own.

    A five-hour change measured on the meters after the cut (`meters`, the
    `five_hour_on_meters` block; `known_date_changes`) opens a further regime, and the
    current figure is the newest regime's (`window_regimes`). `regimes` publishes every
    one, oldest first, in credits per full window.

    `accounts` reports every watched account by its published label, including the
    ones that contributed nothing, so a reader can see that a cluster came from two
    accounts of three and why the third is absent. Account names are never published.
    """
    weight = cache_read_weight(credits) if weight is None else weight
    per_account = pure_family_rows(clean, credits, fam, weight)
    pooled_rows = [r for rows in per_account.values() for r in rows]
    pooled = sorted(r["credits_per_pct"] for r in pooled_rows)
    sides: dict[str, list[float]] = {"before": [], "after": [], "whole": pooled}
    for row in pooled_rows:
        side = cut_side(row)
        if side:
            sides[side].append(row["credits_per_pct"])
    changes = known_date_changes(meters)
    by_regime: dict[int, list[float]] = {}
    for row in pooled_rows:
        idx = regime_index(row, changes)
        if idx is not None:
            by_regime.setdefault(idx, []).append(row["credits_per_pct"])

    def own(key, scale: float) -> dict:
        """One regime's cluster (or the whole one) in credits per 1%, scaled."""
        values = pooled if key == "whole" else sorted(by_regime.get(key, []))
        return {"value": median(values) * scale if values else None,
                "interval": [values[0] * scale, values[-1] * scale] if values else None}

    regimes, current_source = window_regimes(
        {k: len(v) for k, v in by_regime.items()}, own, five_hour_pct, changes, anchor_fam=fam)
    per_pct, per_pct_interval = regimes[-1]["value"], regimes[-1]["interval"]

    def side_figure(values: list[float]) -> dict:
        """One side of the cut as the published {value, interval, n}."""
        values = sorted(values)
        return {"value": round(median(values) * 100) if values else None,
                "interval": [round(values[0] * 100), round(values[-1] * 100)] if values else None,
                "n": len(values)}

    accounts = {}
    for name, label in labels.items():
        rows = per_account.get(name, [])
        values = [r["credits_per_pct"] for r in rows]
        accounts[label] = {
            "n": len(values),
            "value": round(median(values) * 100) if values else None,
            "interval": [round(values[0] * 100), round(values[-1] * 100)] if values else None,
        }
    method = (f"median credits per 1% of the five-hour meter over the current side of the pure-{fam} "
              f"cluster taken from every watched account's passive stretch file, times 100 "
              f"(`current_method` for which side that is). {selection_sentence()} Every token in a "
              f"pure-{fam} stretch is priced at a rate data/prices.json publishes, so the figure "
              f"carries no fitted parameter. Cache writes at the input rate, cache reads at "
              f"{weight:g} of it.")
    return {
        "value": round(per_pct * 100) if per_pct is not None else None,
        "credits_per_pct": round(per_pct) if per_pct is not None else None,
        "interval": ([round(per_pct_interval[0] * 100), round(per_pct_interval[1] * 100)]
                     if per_pct_interval else None),
        "n": len(pooled),
        "cut_at": CUT_AT.isoformat(),
        "before": side_figure(sides["before"]),
        "after": side_figure(sides["after"]),
        "regimes": _rounded_regimes(regimes, 100),
        "current_source": current_source if pooled else None,
        "five_hour_window_pct": five_hour_pct,
        "current_method": current_method(changes),
        "accounts": accounts,
        "pure_family": fam,
        "cache_read_weight": weight,
        "cache_read_weight_range": credits.get("cache_read_weight_range"),
        "method": method,
        "status": None if pooled else f"no capture-accepted pure-{fam} stretch in the history files",
        "derivation": "credits",
    }


def pure_family_of(tokens: dict, credits: dict) -> str | None:
    """The one family every model of a stretch belongs to, or None for a mixed stretch."""
    fams = {family(m, credits) for m in tokens}
    return next(iter(fams)) if len(fams) == 1 and None not in fams else None


def regime_family_rows(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
                       changes: list[dict]) -> dict[int, dict[str, dict[str, list[dict]]]]:
    """Every pure clean stretch with tokens, as {regime index: {family: {label: rows}}}.

    No rate enters: a stretch is placed by its own start (`regime_index`) and filed under the
    one family all its models belong to, priced or not, so a family the table has no rate for
    (Opus 5.5) still has its window read straight off its token counts.
    """
    out: dict[int, dict[str, dict[str, list[dict]]]] = {}
    for name, label in labels.items():
        for st in clean.get(name, []):
            fam = pure_family_of(st.get("tokens") or {}, credits)
            idx = regime_index(st, changes) if fam else None
            if idx is not None and st.get("delta_pct"):
                out.setdefault(idx, {}).setdefault(fam, {}).setdefault(label, []).append(st)
    return out


def regime_family(by_fam: dict[str, dict[str, list[dict]]], anchor_fam: str) -> str:
    """The regime family: the one with the most pure stretches in the regime, the anchor
    first and then by name on a tie."""
    return max(sorted(by_fam), key=lambda f: (sum(len(v) for v in by_fam[f].values()),
                                              f == anchor_fam))


def class_totals(tokens: dict) -> dict[str, int]:
    """One stretch's tokens summed per class across every model in it."""
    out = {cls: 0 for cls in TOKEN_CLASSES}
    for tok in tokens.values():
        if isinstance(tok, dict):
            for cls in TOKEN_CLASSES:
                out[cls] += tok.get(cls, 0)
    return out


def _round(value: float | None, ndigits: int = 0):
    if value is None:
        return None
    return round(value) if ndigits == 0 else round(value, ndigits)


def _figure_from(per_account: dict[str, list[float]], ndigits: int = 0) -> dict:
    """The published shape of one measured quantity: a pooled median and an interval.

    `value` is the median of every reading, pooled across accounts. `interval` is the
    union of the per-account intervals -- each account's own lowest and highest reading
    -- which is the spread of the readings and not a confidence interval. Pooling the
    medians instead would average two accounts that differ by more than either's spread;
    taking a standard error would claim an error model these eleven readings do not have.
    """
    pooled = [v for values in per_account.values() for v in values]
    if not pooled:
        return {"value": None, "interval": None}
    lows = [min(values) for values in per_account.values() if values]
    highs = [max(values) for values in per_account.values() if values]
    return {"value": _round(median(pooled), ndigits),
            "interval": [_round(min(lows), ndigits), _round(max(highs), ndigits)]}


def _mix_credits_per_token(shares: dict[str, float] | None, rate_in: float | None,
                           rate_out: float | None, weight: float) -> float | None:
    """Credits one token of a stated class mix costs at one family's rates.

    `shares` are the four classes as fractions of the whole token count, so this is the
    credits a stretch of that mix spends per token of it. Cache writes ride the plain
    input rate and cache reads ride `weight` of it, the same rule `price_tokens` applies
    to a real bundle.
    """
    if shares is None or rate_in is None or rate_out is None:
        return None
    at_input = shares["input"] + shares["cache_write"] + shares["cache_read"] * weight
    return at_input * rate_in + shares["output"] * rate_out


def _rate_source(rate: FamilyRate) -> str:
    """Where a family's rate comes from, as one word, from the shape of the rate itself.

    "anchor" is the unit anchor and nothing tests it; "measured" is a rate the meter fit
    produced; "list" is a list-price ratio standing in for a fit that cannot yet be told apart
    from a limit change (`list_until_separable`); "envelope" is a family whose fits disagree, so only an interval survives;
    "none" is a family there is nothing at all to measure a rate from.
    """
    if rate.anchor:
        return "anchor"
    if rate.input is not None:
        if rate.rate_source == LIST_UNTIL_SEPARABLE:
            return "list"
        return "inferred" if rate.rate_source == "inferred" else "measured"
    if rate.input_interval:
        return "envelope"
    return "none"


def _conversion_sentence(anchor_fam: str, fam: str, source: str) -> str | None:
    """How one family's window tokens were got from the anchor's, or None where it was not."""
    if source == "anchor" or source == "none":
        return None
    over = (f"the {source} {fam.capitalize()} input rate" if source in ("measured", "inferred", "list")
            else f"the {fam.capitalize()} input-rate envelope")
    tail = ("" if source in ("measured", "inferred", "list") else
            " The envelope has no single rate, so there is no value, only the interval.")
    return (f"the {anchor_fam.capitalize()} window tokens times the {anchor_fam.capitalize()} input "
            f"rate over {over}, at the same token-class mix (`per_class` above, cache reads at the "
            f"published cache-read weight); the interval combines the window interval with the rate "
            f"interval.{tail}")


def _family_window_tokens(all_figure: dict, anchor_per_token: float | None, rate: FamilyRate,
                          shares: dict[str, float] | None, weight: float) -> dict:
    """One family's window tokens: the anchor's, rescaled by the two families' rates.

    The measurement is the anchor family's -- token counts divided by the meter movement
    they caused, with no rate in it at all. Another family's figure is that same window of
    credits spent at that family's rate instead, at the mix the measurement was taken at,
    which is a conversion and not a second measurement. A family with no rate at all
    publishes null and the rate's own status sentence; a family with only an interval
    publishes the interval and no value, never the middle of it.
    """
    if all_figure["value"] is None or anchor_per_token is None:
        return {"value": None, "interval": None,
                "status": rate.status or all_figure.get("status") or "no measured window"}
    in_lo, in_hi = rate.input_edges
    out_lo, out_hi = rate.output_edges
    cheapest = _mix_credits_per_token(shares, in_lo, out_lo, weight)
    dearest = _mix_credits_per_token(shares, in_hi, out_hi, weight)
    point = _mix_credits_per_token(shares, rate.input, rate.output, weight)
    if cheapest is None or dearest is None or cheapest <= 0 or dearest <= 0:
        return {"value": None, "interval": None,
                "status": rate.status or "no rate to convert the window tokens at"}
    lo, hi = all_figure["interval"]
    return {
        "value": round(all_figure["value"] * anchor_per_token / point) if point else None,
        # The cheapest rate buys the most tokens, so it is the top of the interval.
        "interval": [round(lo * anchor_per_token / dearest), round(hi * anchor_per_token / cheapest)],
        "status": None if point else rate.status,
    }


def _times_windows(figure: dict, windows: float | None,
                   windows_interval: list | None) -> dict:
    """One per-window figure multiplied out to a week, both edges at once.

    A row with an interval and no value -- a family whose rate the fits did not separate
    -- keeps that shape: the interval is multiplied out and the value stays null. The
    week never invents a number the window did not have.
    """
    if windows is None:
        return {"value": None, "interval": None,
                "status": "no measured windows per week to multiply by"}
    lo_w, hi_w = windows_interval or [windows, windows]
    interval = ([round(figure["interval"][0] * lo_w), round(figure["interval"][1] * hi_w)]
                if figure.get("interval") else None)
    value = round(figure["value"] * windows) if figure.get("value") is not None else None
    return {"value": value, "interval": interval,
            "status": None if value is not None else figure.get("status")}


def per_week_block(all_figure: dict, families: dict, windows_per_week: float | None,
                   windows_per_week_interval: list | None, source: str) -> dict:
    """`window_tokens.per_week`: the current window, and every family's, times one week."""
    return {
        "windows_per_week": {"value": windows_per_week,
                             "interval": (list(windows_per_week_interval)
                                          if windows_per_week_interval else None),
                             "source": source},
        "all": _times_windows(all_figure, windows_per_week, windows_per_week_interval),
        "per_family": {name: {"all": _times_windows(row["all"], windows_per_week,
                                                    windows_per_week_interval)}
                       for name, row in families.items()},
        "status": None if windows_per_week is not None else
                  "no measured windows per week to multiply by",
    }


def window_tokens(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
                  model_rates: dict | None = None, *, windows_per_week: float | None = None,
                  windows_per_week_interval: list | None = None,
                  windows_per_week_source: str = "weekly_windows current, max20, newest regime",
                  fam: str = "opus", weight: float | None = None,
                  five_hour_pct: float | None = None, meters: dict | None = None) -> dict:
    """What a five-hour window buys in tokens, measured on the meter rather than priced.

    The same cluster `window_credits` is the median of -- the capture-accepted,
    harness-clean, pure-`fam` stretches -- read for its token counts instead of its
    credits: tokens per 1% of the five-hour meter, times 100, per class and over all four
    classes together. No credit rate and no class weight enters the anchor family's
    figure, so it is a reading of the meter and not a scenario. That is the difference
    from the route the page states today, where `window_credits` divided by one model's
    rate for one class answers "how much fresh Sonnet input alone would fill a window" --
    a quantity no account's use looks like (docs/findings-2026-09-20-window-tokens.md).

    The cluster spans the 14 September change, so it is split on `cut_at` by each
    stretch's own start: `before` and `after` at the top level for the all-classes figure,
    and inside each `per_class` entry for that class. `all` is the CURRENT window -- the
    after cluster where it is thick enough to state one, else the before cluster scaled by
    the measured five-hour change (`current_cluster_rule`, `current_source`) -- because
    ten of today's twelve stretches are one account's from before the cut and a median
    over the whole cluster is a pre-cut figure published as the current one. `per_class`
    keeps its own median over the whole cluster: it is the mix the conversions hold their
    shape from (below), and each class states its own two sides beside it. A five-hour
    change measured on the meters after the cut opens a further regime exactly as in `window_credits`,
    `all` is the newest regime's, and `regimes` publishes every one in tokens per window.

    `per_class` is what makes it a measurement rather than a mix assumption: the cluster's
    own median is 444M cache reads, 15M cache writes, 2.8M output and 5.8k input per
    window, and a page that states one number without that shape is stating a scenario
    again. `cache_read_share` is the same fact as one fraction.

    The other families are a conversion of the anchor's figure at the two families' own
    rates, marked as such in each row's `conversion`, and a family whose rate is a status
    sentence publishes null (`_family_window_tokens`). `per_week` multiplies by the
    measured windows per week the page's headline already uses; with none measured it
    carries a status and nulls rather than a week nobody counted.
    """
    weight = cache_read_weight(credits) if weight is None else weight
    per_account = pure_family_rows(clean, credits, fam, weight)
    rows = {label: per_account.get(name, []) for name, label in labels.items()}
    pooled_rows = [row for name in labels for row in per_account.get(name, [])]

    def readings(account_rows: list[dict], cls: str | None = None) -> list[float]:
        """Each stretch's tokens per full window: its own counts over its own percent."""
        out = []
        for row in account_rows:
            totals = class_totals(row["tokens"])
            count = sum(totals.values()) if cls is None else totals[cls]
            out.append(count / row["delta_pct"] * 100)
        return out

    accounts = {}
    for label, account_rows in rows.items():
        if not account_rows:
            accounts[label] = {"n": 0, "per_class": None,
                               "all": {"value": None, "interval": None,
                                       "status": f"contributed no clean pure-{fam} stretch"}}
            continue
        accounts[label] = {
            "n": len(account_rows),
            "all": dict(_figure_from({label: readings(account_rows)}), status=None),
            "per_class": {cls: _figure_from({label: readings(account_rows, cls)})
                          for cls in TOKEN_CLASSES},
        }
    # The same rows split on the cut, per account, so every figure below can state its own
    # two sides and the current one can be taken from whichever side states the window now.
    sides = {"whole": rows,
             "before": {label: [r for r in account_rows if cut_side(r) == "before"]
                        for label, account_rows in rows.items()},
             "after": {label: [r for r in account_rows if cut_side(r) == "after"]
                       for label, account_rows in rows.items()}}
    n_side = {name: sum(len(account_rows) for account_rows in by_label.values())
              for name, by_label in sides.items()}
    changes = known_date_changes(meters)
    by_regime: dict[int, dict[str, list[dict]]] = {}
    for label, account_rows in rows.items():
        for r in account_rows:
            idx = regime_index(r, changes)
            if idx is not None:
                by_regime.setdefault(idx, {}).setdefault(label, []).append(r)

    def figure(side: str, cls: str | None = None, scale: float = 1.0) -> dict:
        """One side's readings as the published figure, optionally scaled to now."""
        return _figure_from({label: [v * scale for v in readings(account_rows, cls)]
                             for label, account_rows in sides[side].items()})

    def own(key, scale: float) -> dict:
        """One regime's cluster (or the whole one), all classes, scaled."""
        by_label = rows if key == "whole" else by_regime.get(key, {})
        return _figure_from({label: [v * scale for v in readings(account_rows)]
                             for label, account_rows in by_label.items()})

    def both_sides(cls: str | None = None) -> dict:
        """The `before` and `after` sub-figures one published figure carries."""
        return {side: dict(figure(side, cls), n=n_side[side]) for side in ("before", "after")}

    per_class = {cls: dict(_figure_from({label: readings(account_rows, cls)
                                         for label, account_rows in rows.items()}),
                           **both_sides(cls))
                 for cls in TOKEN_CLASSES}
    share = _figure_from({label: [r / t if t else 0.0 for r, t in
                                  zip(readings(account_rows, "cache_read"), readings(account_rows))]
                          for label, account_rows in rows.items()}, ndigits=4)

    # The mix the conversions hold: the published per-class medians themselves, so a
    # reader can redo the arithmetic from the numbers on the page. They are medians of
    # separate samples and so need not sum to `all.value`; what the conversion needs is
    # their shape, and the shares are taken of their own total.
    mix_total = sum(per_class[cls]["value"] or 0 for cls in TOKEN_CLASSES)
    shares = ({cls: (per_class[cls]["value"] or 0) / mix_total for cls in TOKEN_CLASSES}
              if mix_total else None)
    anchor = family_rate(fam, credits, model_rates)
    anchor_per_token = _mix_credits_per_token(shares, anchor.input, anchor.output, weight)

    # Every pure clean stretch of any family, by the regime it started in, so each regime
    # after a known-date change is measured in the family actually in use.
    pure_by_regime = regime_family_rows(clean, credits, labels, changes)

    def direct(ks: tuple[int, ...]) -> dict | None:
        """Regimes `ks`' cluster in its regime family, and that figure in anchor units."""
        by_fam: dict[str, dict[str, list[dict]]] = {}
        for k in ks:
            for f, by_label in (pure_by_regime.get(k) or {}).items():
                for label, account_rows in by_label.items():
                    by_fam.setdefault(f, {}).setdefault(label, []).extend(account_rows)
        if not by_fam:
            return None
        name = regime_family(by_fam, fam)
        measured = _figure_from({label: readings(account_rows)
                                 for label, account_rows in by_fam[name].items()})
        n = sum(len(v) for v in by_fam[name].values())
        if name == fam:
            return {"family": name, "n": n, "measured": measured, "anchor": measured}
        rate = family_rate(name, credits, model_rates)
        per_token = _mix_credits_per_token(shares, rate.input, rate.output, weight)
        # The anchor-unit value goes through the family's point rate at the published mix;
        # the regime family's own figure never does.
        ratio = per_token / anchor_per_token if per_token and anchor_per_token else None
        return {"family": name, "n": n, "measured": measured,
                "anchor": (_scaled(measured, ratio, ratio, ratio) if ratio is not None
                           else {"value": None, "interval": None})}

    regimes, current_source = window_regimes(
        {k: sum(len(v) for v in by_label.values()) for k, by_label in by_regime.items()},
        own, five_hour_pct, changes, direct=direct, anchor_fam=fam)
    regimes = _rounded_regimes(regimes)
    measured_family = regimes[-1]["measured_family"]

    all_figure = dict(value=regimes[-1]["value"], interval=regimes[-1]["interval"],
                      status=None if pooled_rows else
                      f"no capture-accepted pure-{fam} stretch in the history files")

    families = {}
    for name in credits["per_family"]:
        rate = family_rate(name, credits, model_rates)
        source = _rate_source(rate)
        families[name] = {
            "all": _family_window_tokens(all_figure, anchor_per_token, rate, shares, weight),
            "rate_source": source,
            "conversion": _conversion_sentence(fam, name, source),
            "measured_directly": name == measured_family,
        }
    if measured_family and measured_family != fam and measured_family in families:
        # The headline family's figure is the regime's own direct reading, never a round
        # trip through a rate: x r then / r at a different r is how a window is invented.
        families[measured_family].update(
            all={"value": regimes[-1]["measured_value"],
                 "interval": regimes[-1]["measured_interval"], "status": None},
            conversion=None)
    # Every regime in the newest regime's measured family: its own direct figure where it was
    # measured in that family, else its anchor-unit figure over the rate the history is
    # converted at -- the newest run's bridge rate where it has one, else the fitted rate.
    newest_run = regimes[-1].get("run_from")
    bridge = next((r for r in regimes if newest_run and r.get("run_from") == newest_run
                   and r.get("bridge_rate") is not None), None)
    history_rate, history_source = None, None
    if measured_family == fam:
        history_rate = 1.0
    elif bridge is not None:
        history_rate, history_source = bridge["bridge_rate"], bridge["bridge_rate_source"]
    elif measured_family:
        rate = family_rate(measured_family, credits, model_rates)
        per_token = _mix_credits_per_token(shares, rate.input, rate.output, weight)
        if per_token and anchor_per_token:
            history_rate, history_source = per_token / anchor_per_token, "fitted"
    for r in regimes:
        if r["measured_family"] == measured_family:
            r["family_value"], r["family_interval"] = r["measured_value"], r["measured_interval"]
        else:
            r["family_value"] = (_round(r["value"] / history_rate)
                                 if history_rate and r["value"] is not None else None)
            r["family_interval"] = ([_round(x / history_rate) for x in r["interval"]]
                                    if history_rate and r["interval"] else None)
    if measured_family and measured_family != fam and measured_family in families:
        families[measured_family]["history_rate"] = {
            "times_anchor": round(history_rate, 4) if history_rate else None,
            "interval": bridge["bridge_rate_interval"] if bridge is not None else None,
            "source": ("fitted: the family's measured rate at the published mix"
                       if history_source == "fitted" else history_source)}

    per_week = per_week_block(all_figure, families, windows_per_week, windows_per_week_interval,
                              windows_per_week_source)
    method = (f"median over the pure-{fam} clean stretches of the tokens the stretch carried per 1% "
              f"of the five-hour meter, times 100, taken per token class and over all four classes "
              f"together -- `all` over the current side of the cut alone (`current_method`), "
              f"`per_class` over the whole cluster, and both sides published beside each. "
              f"The interval is the spread of the readings themselves -- per account its "
              f"own lowest and highest per-stretch value, pooled the union of the account intervals "
              f"-- and not a confidence interval. No credit rate and no class weight enters the "
              f"pure-{fam} figure: it is the stretch's own token counts over its own meter movement. "
              f"Every other family is a conversion of it at the two families' rates (`conversion`), "
              f"and a family whose rate is a status sentence publishes no number. A regime after "
              f"a known-date change is measured the same way in its own regime family "
              f"(`measured_family`, `current_method`), and that family's row is its direct "
              f"reading.")
    return {
        "derivation": "credits",
        "as_of": newest_end(pooled_rows),
        "method": method,
        "current_method": current_method(changes),
        "selection": selection_sentence(),
        "n": len(pooled_rows),
        "cut_at": CUT_AT.isoformat(),
        # The all-classes figure's own two sides; every `per_class` entry carries its own.
        **both_sides(),
        "regimes": regimes,
        "current_source": current_source if pooled_rows else None,
        "five_hour_window_pct": five_hour_pct,
        "measured_family": measured_family,
        "family_values_method": (
            "`family_value` and `family_interval` on each regime are its window in "
            "`measured_family` tokens: the regime's own direct figure where it was measured in "
            "that family, else its anchor-unit figure over `per_family[measured_family]."
            "history_rate`."),
        "accounts": accounts,
        "per_class": per_class,
        # What converts a credit figure into this window's tokens: credits per token of the
        # published mix at the anchor's rates (`_mix_credits_per_token`). The direct weekly
        # figure goes from credits to tokens through it (`regime_figures`).
        "anchor_credits_per_token": anchor_per_token,
        "all": all_figure,
        "cache_read_share": share,
        "per_family": families,
        "per_week": per_week,
    }


def newest_end(stretches: list[dict]) -> str | None:
    """The newest `end` stamp of a set of stretches, as the record's own string.

    Ordered by the parsed instant, not by the string: the watched accounts do not all
    write UTC (one file carries `+02:00`), and a lexical maximum over mixed offsets
    would read the wrong stretch as the newest. The string is returned unchanged so
    the published date is the record's own and not a re-rendering of it.
    """
    dated = [(datetime.fromisoformat(st["end"]), st["end"]) for st in stretches if st.get("end")]
    return max(dated, key=lambda p: p[0])[1] if dated else None


def newest(*stamps: str | None) -> str | None:
    """The newest of some ISO stamps, comparing instants and returning the stamp itself."""
    return newest_end([{"end": s} for s in stamps if s])


def cluster_as_of(clean: dict[str, list[dict]], credits: dict, fam: str = "opus",
                  weight: float | None = None) -> str | None:
    """The newest stretch end in the pure-`fam` cluster `window_credits` is the median of.

    The date behind the five-hour window, and so behind every figure derived by dividing
    it. The page had no date for the credit figures at all and printed a neighbouring
    block's, which is what this answers.
    """
    rows = pure_family_rows(clean, credits, fam, weight)
    return newest_end([r for account_rows in rows.values() for r in account_rows])


def fits_as_of(priceable: dict[str, list[dict]], credits: dict,
               model_rates: dict | None) -> str | None:
    """The newest stretch end behind the pooled per-model fits in history/model-rates.json.

    `measured_rates.fits_pooled` names the fits the published rates were pooled from as
    `<account>/<era>`; the stretches behind them are that account's capture-accepted,
    harness-clean ones whose every model the credit table can price, which is the same
    selection `tools/model_rates.py` builds its design matrix from (its `ok` column is
    exactly `price_tokens(...).priced`). The account names stay inside this function --
    nothing published names an account -- and only the date comes out.
    """
    accounts = {key.split("/")[0] for key in (model_rates or {}).get("fits_pooled", [])}
    rows = [st for account in accounts for st in priceable.get(account, [])
            if price_tokens(st["tokens"], credits).priced]
    return newest_end(rows)


#: A stretch is Fable-heavy, and so worth solving a Fable rate from, when Fable holds
#: more than this share of the tokens the meter charges for. Below it the divisor is
#: small and the residual of everything else lands on the solved rate.
FABLE_HEAVY = 0.5
#: The two candidate output ratios. Every model in the reference table uses 5; the
#: credits-model note fits 3 for Fable, with a bootstrap interval of [3, 3]. The data
#: here do not separate them, so both are carried and neither is chosen.
OUTPUT_RATIO_CANDIDATES = (3, 5)


def solved_fable_input(clean: dict[str, list[dict]], credits: dict, window_per_pct: float,
                       ratio: float, weight: float | None = None) -> dict[str, list[float]]:
    """Per account, the Fable input rate each Fable-heavy stretch implies, sorted.

    The account moved `delta_pct` of its meter, so it spent `delta_pct x W` credits.
    Subtract the credits of every model a published rate covers and what is left is
    Fable's, so

        fable_input = (delta_pct x W - known) / (fable_in + ratio x fable_out)

    This is arithmetic on one stretch, not a fit. It inherits W's own accuracy, the
    published rates, the cache-read weight, and -- on an account whose meter counts
    machines the transcripts never see -- phantom movement, which inflates `delta_pct`
    and so inflates the rate solved from it.
    """
    weight = cache_read_weight(credits) if weight is None else weight
    out: dict[str, list[float]] = {}
    for account, stretches in clean.items():
        values = []
        for st in stretches:
            priced = price_tokens(st["tokens"], credits, weight)
            if not priced.priced or not priced.charged:
                continue
            if (priced.fable_input + priced.fable_output) / priced.charged < FABLE_HEAVY:
                continue
            divisor = priced.fable_input + ratio * priced.fable_output
            if divisor <= 0:
                continue
            values.append((st["delta_pct"] * window_per_pct - priced.known) / divisor)
        out[account] = sorted(values)
    return out


def _p25(values: list[float]) -> float | None:
    """The tool's own p25: the value at index n // 4 of the sorted sample.

    With three values that is the lowest one, which is what the reconciliation means
    when it says the tool's p25 and p75 are its lowest and highest values there.
    """
    return values[len(values) // 4] if values else None


def fable_interval(clean: dict[str, list[dict]], credits: dict, window_per_pct: float | None,
                   labels: dict[str, str], weight: float | None = None,
                   model_rates: dict | None = None) -> dict:
    """Fable's input rate as the page states it: the pooled fit's where it has one.

    Where `model_rates` publishes a measured Fable rate with an interval (the pooled fit,
    tools/model_rates.py), that is the figure: `input_low` and `input_high` are its bootstrap
    interval, `input` its point, and the per-stretch solve below is carried under `solved` as
    working, not stated. Otherwise the solve is the figure, as before (`_solved_fable_interval`).
    """
    solved = _solved_fable_interval(clean, credits, window_per_pct, labels, weight)
    row = ((model_rates or {}).get("per_family") or {}).get("fable") or {}
    if row.get("input") is None or not row.get("interval"):
        return solved
    lo, hi = row["interval"]
    opus_in = ((model_rates or {}).get("anchor") or {}).get("input")
    return {
        "input": round(row["input"], 4),
        "input_low": round(lo, 4),
        "input_high": round(hi, 4),
        "output_ratio": [row.get("output_multiplier") or 5],
        "status": "measured by one fit pooled across every account",
        "times_opus": ([round(lo / opus_in, 2), round(hi / opus_in, 2)] if opus_in else None),
        "method": ("one set of per-token rates shared by every account and side of 14 September, "
                   "with one credits-per-1% scale per account and side, fitted over the clean "
                   "stretches (history/model-rates.json measured_rates.pooled_fit); the interval "
                   "is its 80% bootstrap interval, resampling stretches within each group."),
        "unresolved": None,
        "solved": solved,
    }


def _solved_fable_interval(clean: dict[str, list[dict]], credits: dict,
                           window_per_pct: float | None, labels: dict[str, str],
                           weight: float | None = None) -> dict:
    """Fable's input rate as an interval, solved from the stretches, never typed in.

    Both edges are a p25 rather than a median, because the solve is inflated on any
    account carrying phantom meter movement and its p25 is the usable edge; on an
    account with three surviving stretches the p25 is simply its lowest value. The
    interval runs from the lowest p25 any account gives at the cheaper output ratio to
    the highest p25 any account gives at the dearer one, so it spans both the
    account-to-account spread and the unseparated output ratio at once.

    The rule names no account. The reconciliation's own edges happen to come from the
    account with the usable capture column at 5x and from the phantom-carrying one at
    3x, but nothing here depends on which account lands where.
    """
    low_ratio, high_ratio = max(OUTPUT_RATIO_CANDIDATES), min(OUTPUT_RATIO_CANDIDATES)
    per_account: dict[str, dict] = {label: {} for label in labels.values()}
    edges: dict[int, list[float]] = {}
    if window_per_pct:
        for ratio in OUTPUT_RATIO_CANDIDATES:
            solved = solved_fable_input(clean, credits, window_per_pct, ratio, weight)
            for account, label in labels.items():
                values = solved.get(account, [])
                p25 = _p25(values)
                per_account[label][f"output_{ratio}x"] = {
                    "n": len(values), "p25": round(p25, 4) if p25 is not None else None,
                    "median": round(median(values), 4) if values else None}
                if p25 is not None:
                    edges.setdefault(ratio, []).append(p25)
    low = min(edges[low_ratio]) if edges.get(low_ratio) else None
    high = max(edges[high_ratio]) if edges.get(high_ratio) else None
    opus = rates("opus", credits)
    return {
        "input_low": round(low, 4) if low is not None else None,
        "input_high": round(high, 4) if high is not None else None,
        "output_ratio": list(OUTPUT_RATIO_CANDIDATES),
        "status": "interval, not yet separable",
        "times_opus": ([round(low / opus[0], 2), round(high / opus[0], 2)]
                       if low is not None and high is not None and opus else None),
        "per_account": per_account,
        "window_credits_per_pct": window_per_pct,
        "fable_heavy_share": FABLE_HEAVY,
        "method": (f"solved per Fable-heavy stretch (Fable over {FABLE_HEAVY:.0%} of the tokens the "
                   f"meter charges for) against the measured window; the interval runs from the "
                   f"lowest per-account p25 at output {low_ratio}x input to the highest per-account "
                   f"p25 at output {high_ratio}x. Both edges are a p25 because the solve is inflated "
                   f"wherever the meter counts machines the transcripts never saw."),
        "unresolved": None if low is not None and high is not None else
                      "no Fable-heavy stretch to solve a rate from",
    }


#: Why `across_cut` never resolves the attribution. Not a comparison across accounts -- an
#: earlier version compared the accounts' post-change levels to each other and called that
#: comparison the reason; the Codex review of commit 447b926 (2026-09-20) retracted it, because
#: a stable account-specific scale cancels out of a within-account before/after ratio regardless
#: of how far apart two different accounts sit, so the spread was never evidence either way.
ACROSS_CUT_UNRESOLVED = (
    "a stable account-specific scale cancels out of any within-account before/after ratio, so "
    "these five-hour credit readings cannot separate a change in the five-hour window from a "
    "change in the weekly cap on their own -- that needs an independent debit or allowance "
    "observation, which no committed stretch carries. See the windows-per-week ratio note on "
    "the event for what these same accounts' meters do identify.")


def pooled_fit_prices(model_rates: dict | None) -> dict | None:
    """The pooled fit's whole coefficient vector, in credits, or None when there is none.

    `measured_rates.pooled_fit` in history/model-rates.json is one set of per-token rates
    shared by every account and side of the cut (tools/model_rates.py `pooled_fit`), with
    Opus as the unit. It carries a coefficient for every family the fit priced -- including
    one the page withholds as not measurable, whose point estimate is still the fit's --
    and the cache-read weight fitted with them. The published per-family rates are the
    measurable subset of this; `across_cut` prices with all of it, because a before-and-after
    ratio of the fit's own group scales needs the fit's own model on both sides.
    """
    pooled = (model_rates or {}).get("pooled_fit") or {}
    times_opus = pooled.get("times_opus")
    anchor = (model_rates or {}).get("anchor") or {}
    if not isinstance(times_opus, dict) or not times_opus or not anchor.get("input"):
        return None
    opus_in = float(anchor["input"])
    return {"input": {fam: float(v) * opus_in for fam, v in times_opus.items()},
            "output_multiplier": float(pooled.get("output_multiplier") or 5),
            "cache_read_rate": float(pooled.get("cache_read_weight") or 0.0) * opus_in,
            "times_opus": dict(times_opus),
            "cache_read_weight": float(pooled.get("cache_read_weight") or 0.0)}


def _pooled_stretch_credits(tokens: dict, credits: dict, fit: dict) -> float | None:
    """One stretch's credits at the pooled fit's rates, or None if a family has none there.

    Input-equivalent tokens exactly as the fit's design matrix counts them -- input plus
    cache write, plus `output_multiplier` times output, at the family's rate -- and every
    cache read at the one fitted cache-read rate, whichever family it came from.
    """
    total = 0.0
    for model, tok in tokens.items():
        if not isinstance(tok, dict):
            continue
        fam = family(model, credits)
        rate = fit["input"].get(fam) if fam else None
        if rate is None:
            return None
        total += (input_side(tok, 0.0) + fit["output_multiplier"] * tok.get("output", 0)) * rate
        total += tok.get("cache_read", 0) * fit["cache_read_rate"]
    return total


def across_cut_value(credits: dict, weight: float | None = None,
                     model_rates: dict | None = None):
    """The valuation `across_cut` prices a stretch's tokens with, as `value(tokens)`.

    Credits at the pooled fit's rates where `model_rates` carries one, else at the
    reference table with one Fable rate held (`across_cut_fable_rate`); None where a
    family has no rate on that table.
    """
    fit = pooled_fit_prices(model_rates)
    weight = cache_read_weight(credits) if weight is None else weight
    held = credits.get("across_cut_fable_rate") or {}
    fable_in, fable_out = _rate(held.get("input")), _rate(held.get("output"))

    def value(tokens: dict) -> float | None:
        if fit is not None:
            return _pooled_stretch_credits(tokens, credits, fit)
        priced = price_tokens(tokens, credits, weight)
        if not priced.priced:
            return None
        if (priced.fable_input or priced.fable_output) and fable_out is None:
            return None
        return (priced.known + priced.fable_input * (fable_in or 0)
                + priced.fable_output * (fable_out or 0))
    return value


def comparison_rate(fam: str, credits: dict, model_rates: dict | None = None,
                    prices: dict | None = None) -> tuple[float, float] | None:
    """(input, output) credits per token a family is priced at inside a comparison, or None.

    Never the published rate. A before-and-after or change figure needs the fit's own model
    on both sides, and whether a family's interval is narrow enough to publish is not part
    of that model: Sonnet's pooled interval crossing 1.5x on 2026-09-29 repriced every Sonnet
    token from the fit's 0.24 to the reference table's 0.4 through `family_rate`, and moved
    the joint fit's 22 September five-hour change from +27.7% to +43.4% with no new stretch
    across it. So, in order:

    - a family `absorb_new_family_rates` fitted at its first use (`joint_fit_at_first_use`):
      that fitted rate, which was fitted against this same valuation; or one it valued at
      its list price because that fit could not separate the rate from a limit change
      (`LIST_UNTIL_SEPARABLE`, ADR 0001 rule 10): the list rate;
    - the pooled fit's point estimate (`pooled_fit_prices`), published or not;
    - where there is no pooled fit or it has no coefficient for the family, the reference
      table, and for Fable, which has no row there, the one held rate `across_cut` holds on
      both sides (`across_cut_fable_rate`);
    - a family on none of those, the list-price ratio to the Opus anchor (`list_price_ratio`),
      as detection prices a family nobody has measured.
    """
    row = ((model_rates or {}).get("per_family") or {}).get(fam) or {}
    if row.get("rate_source") in ABSORBED_SOURCES and row.get("input") is not None:
        return float(row["input"]), float(row["input"]) * float(row.get("output_multiplier") or 5)
    fit = pooled_fit_prices(model_rates)
    if fit is not None and fam in fit["input"]:
        return fit["input"][fam], fit["input"][fam] * fit["output_multiplier"]
    pair = rates(fam, credits)
    if pair is not None:
        return pair
    held = credits.get("across_cut_fable_rate") or {}
    held_out = _rate(held.get("output"))
    if fam == "fable" and held_out is not None:
        return _rate(held.get("input")) or 0.0, held_out
    ratio = list_price_ratio(fam, credits, prices)
    anchor = rates("opus", credits)
    return (ratio * anchor[0], ratio * anchor[1]) if ratio is not None and anchor else None


def comparison_value(credits: dict, model_rates: dict | None = None,
                     weight: float | None = None, prices: dict | None = None):
    """The valuation every change figure prices a stretch's tokens with, as `value(tokens)`.

    Each family at `comparison_rate`, so a family's published or unpublished state moves no
    change figure. With a pooled fit every cache read is at its one fitted cache-read rate,
    the fit's own design (`_pooled_stretch_credits`); without one, at `weight` of the
    family's input rate (`price_tokens`). A bundle of no tokens is skipped, as
    `gs_passive.stretch_credits` skips Claude Code's `<synthetic>` rows; a model in no
    family, or a family with no rate at all, leaves the stretch unpriced (None).
    """
    fit = pooled_fit_prices(model_rates)
    weight = cache_read_weight(credits) if weight is None else weight
    cache = {}

    def value(tokens: dict) -> float | None:
        total = 0.0
        for model, tok in tokens.items():
            if not isinstance(tok, dict) or not raw_tokens(tok):
                continue
            fam = family(model, credits)
            if fam is None:
                return None
            if fam not in cache:
                cache[fam] = comparison_rate(fam, credits, model_rates, prices)
            pair = cache[fam]
            if pair is None:
                return None
            if fit is not None:
                total += (input_side(tok, 0.0) * pair[0] + tok.get("output", 0) * pair[1]
                          + tok.get("cache_read", 0) * fit["cache_read_rate"])
            else:
                total += input_side(tok, weight) * pair[0] + tok.get("output", 0) * pair[1]
        return total
    return value


def side_between(st: dict, at: datetime, bounds: list[datetime]) -> str | None:
    """"before" or "after" a change at `at` within its own two regimes, or None.

    `bounds` are every change instant known (the 14 September cut, each measured change in
    `known_date_changes`, any later one). The before side runs from the latest of them
    before `at`, the after side to the earliest after it, so a regime's figure never takes
    stretches from the next one or the one before; a stretch that crosses any of them is on
    no side.
    """
    start = datetime.fromisoformat(st["start"])
    end = datetime.fromisoformat(st["end"]) if st.get("end") else start
    lo = max((b for b in bounds if b < at), default=None)
    hi = min((b for b in bounds if b > at), default=None)
    if lo is not None and start < lo or hi is not None and end > hi:
        return None
    if end <= at:
        return "before"
    return "after" if start >= at else None


def across_cut(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
               weight: float | None = None, model_rates: dict | None = None,
               changes: list[datetime] | None = None) -> dict:
    """Each account's five-hour window in credits before and after the announced change.

    The after side ends at the first later change (`changes`, the `known_date_changes`
    instants), which opens a regime of its own: `side_between`.

    Every clean stretch is priced, not only the pure ones, so there is enough on both
    sides of 14 September to compare. Where `model_rates` carries the pooled fit
    (`pooled_fit_prices`), every family is priced at the fit's measured rate and cache reads
    at its fitted weight -- one table on both sides of the cut, and the one the rates were
    measured with, on the same selection (`rate_fit_stretches`). That fit found no change in
    Fable's rate at the cut (docs/findings-2026-09-23-pooled-rates.md), so holding it on both
    sides is a measurement rather than an assumption. Without a pooled fit (a fixture, an
    archive from before it existed) the reference table prices the known families and one
    Fable rate is held constant on both sides (`across_cut_fable_rate` in data/prices.json),
    which is what this block did before. The level itself is not a claim;
    `window_credits` is the claim.

    **This does not resolve whether the five-hour window moved, and the block says so.**
    An earlier version of this block compared the accounts' post-change levels to each
    other and called the attribution unresolved because they differed by more than any
    one of them moved (46.8% apart against a largest own-move of 12.8%). The Codex
    review of commit 447b926 (2026-09-20) retracted that argument: a stable
    account-specific scale cancels out of a within-account before/after ratio no matter
    how far apart two different accounts sit, so the cross-account spread was never
    evidence either way, and it is not published here any more. What actually blocks the
    attribution is algebraic, not a spread: with complete capture, these readings
    identify `rate / budget` for the five-hour meter, and multiplying every rate and
    both budgets by the same positive number leaves every reading unchanged. Separating
    a five-hour-window change from a weekly-cap change needs an independent debit or
    allowance observation in a stable unit; nothing committed here is that. `resolved`
    is always false for that reason, not a comparison across accounts. An earlier draft
    of the reconciliation called the window flat within 4%; that came from gating on
    `status` instead of `capture_status`, which let 42 unaccounted stretches into the
    comparison, and the gate on PR #64 caught it.
    """
    fit = pooled_fit_prices(model_rates)
    until = min((c for c in changes or [] if c > CUT_AT), default=None)
    held = credits.get("across_cut_fable_rate") or {}
    fable_in, fable_out = _rate(held.get("input")), _rate(held.get("output"))
    value = across_cut_value(credits, weight, model_rates)
    out = {}
    for name, label in labels.items():
        sides: dict[str, list[float]] = {"before": [], "after": []}
        for st in clean.get(name, []):
            total = value(st["tokens"])
            if total is None:
                continue
            if not st.get("start"):
                continue  # unplaceable: no stamp to put it on one side of the change
            side = side_between(st, CUT_AT, [CUT_AT, *(changes or [])])
            if side is not None:
                sides[side].append(total / st["delta_pct"])
        before = round(median(sides["before"])) if sides["before"] else None
        after = round(median(sides["after"])) if sides["after"] else None
        out[label] = {
            "before": before, "after": after,
            "n_before": len(sides["before"]), "n_after": len(sides["after"]),
            "change_pct": round((after - before) / before * 100, 1) if before and after else None,
            # How many of the account's clean stretches carry a capture reading at all.
            # Zero means the capture column is not usable on that account, so its two
            # medians divide the whole account's meter movement -- web, phone and every
            # other machine included -- by only what this host's transcripts saw.
            "n_with_capture": sum(1 for st in clean.get(name, []) if st.get("capture") is not None),
        }
    if fit is not None:
        rates_used = {
            "source": "pooled_fit",
            "times_opus": {fam: round(v, 4) for fam, v in sorted(fit["times_opus"].items())},
            "cache_read_weight": round(fit["cache_read_weight"], 6),
            "output_multiplier": fit["output_multiplier"],
            "why": ("every family at the pooled fit's own rate, Opus the unit, and every cache "
                    "read at its fitted weight: the same rates on both sides of the cut, "
                    "measured on the same stretches. The fit separates Fable's rate either "
                    "side of the cut and finds no change in it."),
        }
        method = ("median credits per 1% over the stretches the rate fits run over (capture test "
                  "on every account, the personal account from 6 September, no stretch that "
                  "spans the cut), split on the announced change, every stretch priced at the "
                  "pooled fit's rates so the two medians are comparable to each other. An "
                  "account whose n_with_capture is 0 has no usable capture column, so its meter "
                  "movement includes work this host never saw and its level reads low.")
    else:
        rates_used = None
        method = ("median credits per 1% over every clean stretch of the account, split on the "
                  "announced change; one Fable rate held constant on both sides so the two "
                  "medians are comparable to each other. An account whose n_with_capture is 0 "
                  "has no usable capture column, so its meter movement includes work this host "
                  "never saw and its level reads low; the comparison of its own two sides is "
                  "still its own.")
    return {
        "per_account": out,
        "resolved": False,
        "unresolved": ACROSS_CUT_UNRESOLVED,
        "unit": "credits per 1% of the five-hour meter",
        "cut_at": CUT_AT.isoformat(),
        "after_until": _utc(until) if until else None,
        "rates_used": rates_used,
        "fable_rate_held": {"input": fable_in, "output": fable_out,
                            "why": held.get("why")} if fit is None and fable_out is not None else None,
        "method": method,
    }


def _regime_rows(by_window: list[dict], regime: dict,
                 exclude_at: datetime | None = None) -> list[dict]:
    """The `by_window` rows inside one regime's own span, bounds inclusive.

    `by_window` is pooled across accounts, and each account keeps its own window
    cadence, so two different accounts' rows can share a `window_ending` instant. A row
    could sit exactly on the instant where one regime ends and the next begins, and each
    regime's own inclusive bounds would then both claim it. `exclude_at` names that one
    instant (the earlier regime's own `end`) so it is dropped from the later regime only
    -- never a whole boundary side, which would also drop the later regime's own first
    window whenever that window's own timestamp happens to equal its own `start`.
    """
    lo, hi = datetime.fromisoformat(regime["start"]), datetime.fromisoformat(regime["end"])
    return [r for r in by_window
            if lo <= datetime.fromisoformat(r["window_ending"]) <= hi
            and datetime.fromisoformat(r["window_ending"]) != exclude_at]


def _side(rows: list[dict], regime: dict) -> dict | None:
    """One side of a before/after comparison: the rows' pooled sums, ratio and rounding interval."""
    d5 = sum(r["five_hour_pct"] for r in rows)
    d7 = sum(r["seven_day_pct"] for r in rows)
    if not d7:
        return None
    lo, hi = ratio_interval(d5, d7, sum(r.get("pieces", 1) for r in rows))
    return {"n_windows": len(rows), "sum_five_hour_pct": round(d5, 1),
            "sum_seven_day_pct": round(d7, 1), "ratio": round(d5 / d7, 4),
            "rounding_interval": [round(x, 4) if x is not None else None for x in (lo, hi)],
            "accounts": sorted({r["account"] for r in rows if r.get("account")}),
            "start": regime["start"], "end": regime["end"]}


def _one_sided_reason(rows: list[dict], before: dict, after: dict) -> str:
    """Why an account with readings has no before/after change of its own."""
    endings = [datetime.fromisoformat(r["window_ending"]) for r in rows]
    if min(endings) >= datetime.fromisoformat(after["start"]):
        return "readings_only_after_the_change"
    if max(endings) <= datetime.fromisoformat(before["end"]):
        return "readings_only_before_the_change"
    return "no_certified_step_of_its_own"


def _paired_account(rows: list[dict], block: dict) -> dict | None:
    """One account's own change across its own last certified step, or None if unbounded.

    The two sides are the account's own last two regimes (`by_account.<label>.regimes`),
    not the pooled ones: the cut reaches each account at that account's own seven-day
    reset, so a pooled boundary would put one account's first post-cut windows on the
    before side. The change's interval takes the rounding interval of both sides at
    their far ends: the lowest after over the highest before, and the other way round.
    """
    regimes = block["regimes"]
    before = _side(_regime_rows(rows, regimes[-2]), regimes[-2])
    after = _side(_regime_rows(rows, regimes[-1],
                               exclude_at=datetime.fromisoformat(regimes[-2]["end"])), regimes[-1])
    if before is None or after is None:
        return None
    (b_lo, b_hi), (a_lo, a_hi) = before["rounding_interval"], after["rounding_interval"]
    if not (b_lo and b_hi and a_lo and a_hi):
        return None
    for side in (before, after):
        del side["accounts"]
    rho = after["ratio"] / before["ratio"]
    rho_lo, rho_hi = a_lo / b_hi, a_hi / b_lo
    return {"before": before, "after": after, "ratio_after_over_before": round(rho, 4),
            "ratio_interval": [round(rho_lo, 4), round(rho_hi, 4)],
            "change_pct": round((rho - 1) * 100, 2),
            "change_interval_pct": [round((rho_lo - 1) * 100, 2), round((rho_hi - 1) * 100, 2)]}


def combine_log_ratios(per_account: dict[str, dict]) -> dict:
    """The paired accounts' changes combined: a weighted mean of their own log ratios.

    Each account's weight is the inverse square of the half-width of its own ratio
    interval in log terms, normalised to sum to one, so an account whose own rounding
    pins its change tightly counts for more than one whose readings are thin. The
    weights depend on the intervals alone, never on the values, so with them fixed
    the combined figure can sit no lower than the same weighted mean of every account's
    lowest ratio and no higher than that of every highest: that is the published
    interval. It bounds rounding, not the accounts' disagreement with each other, which
    `per_account` shows directly. Worked from the published rounded values, so a reader
    can redo it.
    """
    raw = {label: (math.log(a["ratio_interval"][1]) - math.log(a["ratio_interval"][0])) / 2
           for label, a in per_account.items()}
    inv = {label: 1 / h ** 2 if h > 0 else 0.0 for label, h in raw.items()}
    if not any(inv.values()):  # every interval a point: weight the accounts equally
        inv = dict.fromkeys(inv, 1.0)
    total = sum(inv.values())
    weights = {label: round(v / total, 4) for label, v in inv.items()}
    norm = sum(weights.values())
    log_mid = sum(w * math.log(per_account[k]["ratio_after_over_before"]) for k, w in weights.items()) / norm
    log_lo = sum(w * math.log(per_account[k]["ratio_interval"][0]) for k, w in weights.items()) / norm
    log_hi = sum(w * math.log(per_account[k]["ratio_interval"][1]) for k, w in weights.items()) / norm
    return {"weights": weights, "ratio": math.exp(log_mid),
            "interval": (math.exp(log_lo), math.exp(log_hi))}


#: A change is `measured` once its 95% interval excludes no change and is no wider than this
#: many percentage points either side of its centre (`change_state`).
CHANGE_MEASURED_HALF_WIDTH_PCT = 10.0
#: How many stretches before a candidate an account needs before its own after side is
#: compared with it. The before side also supplies most of the scatter the interval uses.
ANNOUNCED_MIN_BEFORE = 5


def _t975(df: int) -> float:
    """The two-sided 95% Student-t quantile, from the Cornish-Fisher expansion in 1/df.

    Within 0.3% of the exact value from df 3 up; df 1 and 2 are taken from the table.
    """
    if df <= 2:
        return (12.706, 4.303)[max(df, 1) - 1]
    z = 1.959964
    return z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)


def family_first_seen(by_account: dict[str, list[dict]], credits: dict) -> dict[str, dict]:
    """Each credit family's earliest use across every account: when, where, and in which stretch.

    `start` is the earliest stretch that holds the family, which decides whether the family
    was on record from the first stretch. `first_turn` is the family's earliest turn: the
    stretch's own `first_turns` stamp for the model where the record carries one, else the
    stretch's start (`first_turn_source` says which). `end` and `account` are those of the
    stretch the first turn sits in. The earliest by instant, not by string: the files carry
    mixed UTC offsets. A model id in no family (`<synthetic>`) is not a family and is skipped.
    """
    seen: dict[str, dict] = {}
    for account in sorted(by_account):
        for st in by_account[account]:
            if not st.get("start") or not st.get("end"):
                continue
            start = datetime.fromisoformat(st["start"])
            stamps = st.get("first_turns") or {}
            for model in (st.get("tokens") or {}):
                fam = family(model, credits)
                if fam is None:
                    continue
                first = datetime.fromisoformat(stamps[model]) if stamps.get(model) else start
                row = seen.setdefault(fam, {"start": start, "first_turn": first,
                                            "first_turn_source": None, "end": None, "account": None})
                row["start"] = min(row["start"], start)
                if row["account"] is None or first < row["first_turn"]:
                    row.update(first_turn=first, end=datetime.fromisoformat(st["end"]),
                               account=account,
                               first_turn_source="first_turn" if stamps.get(model) else "stretch_start")
    return seen


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def change_candidates(by_account: dict[str, list[dict]], credits: dict,
                      announcements: list[dict] | None = None) -> list[dict]:
    """Candidate change points, generated from the data: every family's first use.

    A family present in the earliest stretch on record is not a candidate: nothing on any
    account precedes it. The candidate's instant is the family's first turn
    (`family_first_seen`): the turn's own timestamp where the stretch records one
    (`at_source` "first_turn"), else the start of the stretch that holds it
    ("stretch_start"). `first_seen_stretch_end` is the end of that stretch. A recorded
    five-hour announcement dated within a day of the candidate is attached to it as
    `announcement`; it annotates the candidate and never creates or gates one.
    """
    announcements = ANNOUNCEMENTS if announcements is None else announcements
    starts = [datetime.fromisoformat(st["start"]) for rows in by_account.values()
              for st in rows if st.get("start") and st.get("tokens")]
    if not starts:
        return []
    earliest = min(starts)
    out = []
    for fam, seen in family_first_seen(by_account, credits).items():
        if seen["start"] <= earliest:
            continue
        at = seen["first_turn"]
        day = at.astimezone(timezone.utc).date()
        note = next((dict(a) for a in announcements if a.get("scope") == "five_hour"
                     and abs((datetime.fromisoformat(a["date"]).date() - day).days) <= 1), None)
        out.append({"family": fam, "at": at, "at_source": seen["first_turn_source"],
                    "first_seen_account": seen["account"],
                    "first_seen_stretch_end": seen["end"], "announcement": note})
    return sorted(out, key=lambda c: (c["at"], c["family"]))


def rounding_variance(st: dict) -> float:
    """The variance whole-percent rounding alone puts on a stretch's log credits per 1%.

    Each window piece's movement is the difference of two whole-percent readings, each off
    by up to half a point either way, so a piece carries 1/6 of a point squared; the pieces
    add, and the log divides by the movement: windows / (6 x delta_pct squared). At the
    10% a stretch closes on that is 0.0017 for one piece, against a residual variance near
    0.1 (docs/findings-2026-09-28-scatter.md).
    """
    delta = st.get("delta_pct") or 0
    return (st.get("windows") or 1) / (6 * delta * delta) if delta > 0 else 0.0


def scatter_variance(ss: float, df: int, rounding: list[float]) -> float:
    """The stretch-to-stretch variance left once rounding is taken out, by moments.

    `ss` over `df` is the observed residual variance; each stretch's rounding share of it
    is known (`rounding_variance`), so their mean comes off. Never below zero.
    """
    total = ss / df if df > 0 else 0.0
    return max(total - (sum(rounding) / len(rounding) if rounding else 0.0), 0.0)


def _weights(rounding: list[float], s2: float) -> list[float]:
    """Inverse-variance weights, 1 / (scatter + the stretch's own rounding)."""
    return [1 / (s2 + v) if s2 + v > 0 else 1.0 for v in rounding]


def _wmean(xs: list[float], ws: list[float]) -> float:
    return sum(x * w for x, w in zip(xs, ws)) / sum(ws)


#: Huber's tuning constant: residuals within this many standard deviations of their level
#: count in full, beyond it their weight falls as 1 / |z|. 1.345 keeps 95% of the efficiency
#: of a plain mean on normal scatter (docs/findings-2026-09-29-rates-and-missing-work.md).
HUBER_K = 1.345


def _huber_weight(z: float) -> float:
    return 1.0 if abs(z) <= HUBER_K else HUBER_K / abs(z)


def _huber_rho(z: float) -> float:
    return z * z / 2 if abs(z) <= HUBER_K else HUBER_K * abs(z) - HUBER_K * HUBER_K / 2


def robust_variance(residuals: list[float]) -> float:
    """The squared normal-consistent median absolute deviation of residuals about their median.

    A stretch whose transcripts miss work the meter counted, or hold work it did not, lands
    far from its account's level on one side or the other; the median absolute deviation
    reads the scatter of the rest, so a handful of those does not set the width.
    """
    if len(residuals) < 2:
        return 0.0
    mid = median(residuals)
    return (1.4826 * median(abs(e - mid) for e in residuals)) ** 2


def huber_location(xs: list[float], variances: list[float], iters: int = 50,
                   start: float | None = None) -> tuple[float, list[float]]:
    """(Huber M-estimate of the level, each point's final weight), by reweighting.

    Each point's standardised residual is (x - level) / sqrt(its variance); the weight is
    1 / variance times `_huber_weight` of it. Starts from `start`, else the median.
    """
    m = median(xs) if start is None else start
    inv = [1 / v if v > 0 else 1.0 for v in variances]
    cut = [HUBER_K * math.sqrt(v) if v > 0 else HUBER_K for v in variances]
    ws = inv
    for _ in range(iters):
        ws = [w if abs(x - m) <= c else w * c / abs(x - m) for x, w, c in zip(xs, inv, cut)]
        total = sum(ws)
        new = sum(x * w for x, w in zip(xs, ws)) / total
        if abs(new - m) < 1e-9:
            return new, ws
        m = new
    return m, ws


def _huber_efficiency(z: list[float]) -> float:
    """E[psi squared] / E[psi'] squared over standardised residuals: the Huber estimate's
    variance as a multiple of a plain weighted mean's at the same scale."""
    psi2 = sum(min(abs(x), HUBER_K) ** 2 for x in z) / len(z)
    slope = sum(1 for x in z if abs(x) <= HUBER_K) / len(z)
    return psi2 / (slope * slope) if slope > 0 else float("inf")


def log_ratio_side(before: list[float], after: list[float],
                   before_rounding: list[float] | None = None,
                   after_rounding: list[float] | None = None) -> dict | None:
    """One account's own change across a candidate, from log credits per 1% on each side.

    Each side's level is a Huber M-estimate (`huber_location`) of its log credits per 1%, and
    the ratio is the ratio of the two. Each stretch's variance is the scatter shared by all
    of them plus its own rounding (`rounding_variance`, zero when not given); the scatter is
    read robustly off both sides' residuals (`robust_variance`) less the mean rounding. A
    stretch within HUBER_K standard deviations of its side's level counts at 1 / variance; one
    further out, which is what a stretch whose transcripts missed work the meter counted (or
    hold work it did not) looks like, counts for less the further it lands. The standard
    error is the inverse-variance one scaled by the Huber efficiency of the standardised
    residuals pooled over both sides, so the after side borrows the pooled scatter and one or
    two stretches after still get an interval; `n_downweighted` counts the stretches beyond
    HUBER_K. None when the before side is under ANNOUNCED_MIN_BEFORE or the after side is
    empty.
    """
    nb, na = len(before), len(after)
    if nb < ANNOUNCED_MIN_BEFORE or na < 1:
        return None
    vb = list(before_rounding) if before_rounding is not None else [0.0] * nb
    va = list(after_rounding) if after_rounding is not None else [0.0] * na
    mb, ma = median(before), median(after)
    df = nb + na - 2
    plain_b, plain_a = sum(before) / nb, sum(after) / na
    ss = sum((x - plain_b) ** 2 for x in before) + sum((x - plain_a) ** 2 for x in after)
    # The scatter is read robustly (`robust_variance`) off both sides' residuals about their
    # medians, less the mean rounding; a zero spread falls back to the plain moments.
    resid = [x - mb for x in before] + [x - ma for x in after]
    s2 = max(robust_variance(resid) - sum(vb + va) / (nb + na), 0.0)
    if s2 <= 0:
        s2 = scatter_variance(ss, df, vb + va)
    varb, vara = [s2 + v for v in vb], [s2 + v for v in va]
    if s2 > 0:
        mb, wb = huber_location(before, varb)
        ma, wa = huber_location(after, vara)
        z = ([(x - mb) / math.sqrt(v) for x, v in zip(before, varb)]
             + [(x - ma) / math.sqrt(v) for x, v in zip(after, vara)])
        eff = _huber_efficiency(z)
        se = math.sqrt(eff * (1 / sum(_weights(vb, s2)) + 1 / sum(_weights(va, s2))))
        down = sum(1 for x in z if abs(x) > HUBER_K)
    else:
        wb, wa = _weights(vb, s2), _weights(va, s2)
        mb, ma = _wmean(before, wb), _wmean(after, wa)
        se, down = math.sqrt(1 / sum(wb) + 1 / sum(wa)), 0
    d, h = ma - mb, _t975(df) * se
    return {"log_ratio": d, "se": se, "df": df, "sd": math.sqrt(ss / df),
            "scatter_sd": math.sqrt(s2), "rounding_sd": math.sqrt(sum(vb + va) / (nb + na)),
            "n_downweighted": down,
            "ratio": math.exp(d), "interval": (math.exp(d - h), math.exp(d + h))}


def combine_inverse_variance(per_account: dict[str, dict]) -> dict | None:
    """The paired accounts' log ratios combined as a weighted mean, weights 1 / se squared.

    Each account is compared only with itself, so account-specific scale cancels out of
    every term. Unlike `combine_log_ratios`, whose interval bounds rounding and so stays
    as wide as its accounts', this interval bounds scatter, and independent accounts
    narrow it. It is a t interval on the combined standard error with the accounts'
    degrees of freedom summed. None when no account is paired.
    """
    if not per_account:
        return None
    inv = {k: 1 / a["se"] ** 2 if a["se"] > 0 else 1e12 for k, a in per_account.items()}
    total = sum(inv.values())
    d = sum(inv[k] * a["log_ratio"] for k, a in per_account.items()) / total
    se = 1 / math.sqrt(total)
    h = _t975(sum(a["df"] for a in per_account.values())) * se
    return {"weights": {k: v / total for k, v in inv.items()}, "ratio": math.exp(d),
            "interval": (math.exp(d - h), math.exp(d + h))}


def inverse_variance_plan_wide(paired: dict[str, dict], change_pct: float | None,
                               estimator: str) -> dict:
    """ADR 0001 rule 9 on an inverse-variance combination (`combine_inverse_variance`): the
    combined change refitted with each paired account left out in turn (`plan_wide_verdict`)."""
    without = {}
    for label in paired if len(paired) > 1 else ():
        rest = combine_inverse_variance({k: v for k, v in paired.items() if k != label})
        without[label] = {"change_pct": _pct_of(rest["ratio"]),
                          "interval_pct": [_pct_of(x) for x in rest["interval"]]}
    return plan_wide_verdict(change_pct, sorted(paired), without, estimator)


def direct_change(per_account: dict[str, dict], paired: dict[str, dict], estimator: str,
                  unit: str) -> dict:
    """One direct measurement's change across a candidate, combined, tested and certified.

    `paired[label]` carries each account's own `log_ratio`, `se` and `df`; `per_account` is
    every account's published row. The combination is `combine_inverse_variance`, and the
    change is `certified` (ADR 0001 rule 9) only when it holds with each account left out
    (`inverse_variance_plan_wide`) and its own 95% interval excludes no change.
    """
    combined = combine_inverse_variance(paired)
    pct = _pct_of(combined["ratio"]) if combined else None
    interval = [_pct_of(x) for x in combined["interval"]] if combined else None
    for label, w in (combined or {}).get("weights", {}).items():
        per_account[label]["weight"] = round(w, 4)
    plan_wide = inverse_variance_plan_wide(paired, pct, estimator)
    excludes = bool(interval and not interval[0] <= 0 <= interval[1])
    return {"unit": unit, "change_pct": pct, "interval_pct": interval,
            "interval_excludes_no_change": excludes, "accounts_combined": sorted(paired),
            "per_account": per_account, "plan_wide": plan_wide,
            "certified": plan_wide["state"] == "passed" and excludes}


def change_state(interval_pct: list[float] | tuple[float, float] | None) -> str:
    """measuring / provisional / measured, from a change's 95% interval in percent.

    `measuring` while there is no interval or it includes no change (0), `provisional` once
    it excludes 0, `measured` once it also has a half-width of CHANGE_MEASURED_HALF_WIDTH_PCT
    points or less. Nothing else sets it: not the number of stretches after the candidate,
    and not the notify step's 24 and 48 hour email rules, which time the email only.
    """
    if not interval_pct or interval_pct[0] is None or interval_pct[1] is None:
        return "measuring"
    lo, hi = interval_pct
    if lo <= 0 <= hi:
        return "measuring"
    return "measured" if (hi - lo) / 2 <= CHANGE_MEASURED_HALF_WIDTH_PCT else "provisional"


def announced_change_stretches(by_account: dict[str, list[dict]],
                               runs: list[HarnessRun]) -> dict[str, list[dict]]:
    """The selection the known-date test reads: status accepted, reset-verified or not.

    `clean_stretches` on the `status` column (harness runs out, at least MIN_DELTA_PCT of
    meter movement, some tokens), masterrig from MASTERRIG_FROM as in the rate fits. A
    reset-unverified stretch is admitted and counted per account.
    """
    kept = clean_stretches(by_account, runs, require="status")
    if "masterrig" in kept:
        kept["masterrig"] = [st for st in kept["masterrig"]
                             if datetime.fromisoformat(st["start"]) >= MASTERRIG_FROM]
    return kept


def _label_for(labels: dict[str, str], name: str, names: list[str]) -> str:
    """An account's published label; an account with none gets the next free `aN`."""
    if name in labels:
        return labels[name]
    extra = [n for n in sorted(set(names)) if n not in labels]
    return f"a{len(labels) + 1 + extra.index(name)}"


def candidate_bounds(at: datetime, cands: list[dict]) -> tuple[datetime | None, datetime | None]:
    """The boundaries either side of `at`: the weekly change (CUT_AT) and every candidate."""
    bounds = {CUT_AT, *(c["at"] for c in cands)}
    return (max((b for b in bounds if b < at), default=None),
            min((b for b in bounds if b > at), default=None))


def _side_of(st: dict, at: datetime, lo: datetime | None, hi: datetime | None) -> str | None:
    """Which side of `at` a stretch lies wholly on, within (lo, hi), or None if it spans one."""
    start = datetime.fromisoformat(st["start"])
    end = datetime.fromisoformat(st["end"])
    if (lo is None or start >= lo) and end <= at:
        return "before"
    if start >= at and (hi is None or end <= hi):
        return "after"
    return None


#: How finely, and with how many resamples, `unclaimed_shares` reads an account's share of the
#: unclaimed work. The share is bounded to [0, 1]: a meter can have carried none of it, or all.
UNCLAIMED_GRID = 50
UNCLAIMED_BOOTSTRAP = 100


def stretch_amount(st: dict, value, share: float = 0.0) -> float | None:
    """A stretch's credits: its own tokens, plus `share` of the work no login claimed in it.

    `unclaimed_tokens` (tracker/gs_passive.py) is the pooled projects root's work that no
    config dir's `session-env` claims, in the stretch's pairs. An unpriceable unclaimed
    bundle adds nothing rather than dropping the stretch, since it may not be this meter's.
    """
    amount = value(st["tokens"])
    if amount is None or not share or not st.get("unclaimed_tokens"):
        return amount
    extra = value(st["unclaimed_tokens"])
    return amount + share * extra if extra else amount


def _unclaimed_caps(rows: dict[str, list[tuple]]) -> list[frozenset[str]]:
    """The sets of accounts whose unclaimed work coincides at some instant.

    `rows` holds, per account, (own, unclaimed, meter %, start, end) for each stretch. Two
    accounts reading one pooled root see the same unclaimed turns over the span they share,
    so wherever several accounts' stretches carrying unclaimed work overlap, their shares
    of it must sum to at most 1.
    """
    # An end sorts before a start at the same instant: stretches that only meet do not overlap.
    # So a zero-length stretch overlaps nothing, and is skipped: its end would sort before its
    # own start.
    events = sorted((t, kind, name) for name, rs in rows.items() for r in rs if r[1] and r[3] < r[4]
                    for t, kind in ((r[3], 1), (r[4], 0)))
    active: dict[str, int] = {}
    caps: set[frozenset[str]] = set()
    for _, kind, name in events:
        if kind == 1:
            active[name] = active.get(name, 0) + 1
            if len(active) > 1:
                caps.add(frozenset(active))
        else:
            active[name] -= 1
            if not active[name]:
                del active[name]
    # Only the largest sets constrain: a subset's sum is at most its superset's.
    return [c for c in caps if not any(c < d for d in caps)]


#: The most accounts one group of overlapping caps is searched exactly for. One solve is a
#: depth-first search over the 51-point grid per account: measured 2026-09-30 on gs at 1.7 ms
#: for 2 accounts, 16 ms for 3 and 257 ms for 4, run 101 times per call (the fit and
#: UNCLAIMED_BOOTSTRAP resamples) and 8 calls per publish. Today's data has one group of 2
#: (Max accounts 2 and 4): 0.83 s of a 2.5 s credits pass. A group of 4 would add about
#: 3.5 minutes, so from 4 accounts `_one_budget` solves it instead.
CAPPED_EXACT_MAX = 3


def _one_budget(ss: dict[str, list[float]], members: list[str], n: int) -> dict[str, int]:
    """Grid indices for `members` minimising their summed scatter with all their shares summing
    to at most 1: a knapsack over the n grid steps, O(members x n^2). Exact when the group is one
    cap; for a chain of caps ({a, b} and {b, c}) it is stricter than required, since it also
    holds a + c to 1."""
    inf = float("inf")
    best = [0.0] + [inf] * n
    picks: list[list[int]] = []
    for m in members:
        nxt, pick = [inf] * (n + 1), [0] * (n + 1)
        for used, total in enumerate(best):
            if total == inf:
                continue
            for i in range(n + 1 - used):
                t = total + ss[m][i]
                if t < nxt[used + i]:
                    nxt[used + i], pick[used + i] = t, i
        best = nxt
        picks.append(pick)
    used = min(range(n + 1), key=lambda b: best[b])
    out = {}
    for m, pick in zip(reversed(members), reversed(picks)):
        out[m] = pick[used]
        used -= pick[used]
    return out


def _capped_best(ss: dict[str, list[float]], caps: list[frozenset[str]], grid: list[float]) -> dict[str, float]:
    """The grid shares minimising the summed scatter, every set in `caps` summing to at most 1.

    The scatter is separable by account, so an account in no cap is fitted on its own and each
    group of overlapping caps is solved together: searched exactly up to CAPPED_EXACT_MAX
    accounts, ties going to the smaller shares, and by `_one_budget` above that.
    """
    out: dict[str, float] = {}
    capped = set().union(*caps) if caps else set()
    for name, values in ss.items():
        if name not in capped:
            out[name] = grid[min(range(len(grid)), key=lambda i: values[i])]
    groups: list[set[str]] = []
    for cap in caps:
        joined = [g for g in groups if g & cap]
        merged = set(cap).union(*joined)
        groups = [g for g in groups if not g & cap] + [merged]
    for group in groups:
        members = sorted(group)
        if len(members) > CAPPED_EXACT_MAX:
            out.update({m: grid[i] for m, i in _one_budget(ss, members, len(grid) - 1).items()})
            continue
        group_caps = [c for c in caps if c <= group]
        best: tuple[float, tuple[int, ...]] | None = None

        def search(k: int, picked: tuple[int, ...], total: float) -> None:
            nonlocal best
            if best is not None and total >= best[0]:
                return
            if k == len(members):
                best = (total, picked)
                return
            chosen = dict(zip(members, picked))
            for i in range(len(grid)):
                trial = {**chosen, members[k]: i}
                if any(sum(grid[trial[m]] for m in cap if m in trial) > 1 + 1e-9 for cap in group_caps):
                    break
                search(k + 1, picked + (i,), total + ss[members[k]][i])

        search(0, (), 0.0)
        assert best is not None
        out.update({m: grid[i] for m, i in zip(members, best[1])})
    return out


def unclaimed_shares(selected: dict[str, list[dict]], value, at: datetime, lo: datetime | None,
                     hi: datetime | None, labels: dict[str, str], names: list[str]) -> dict[str, dict]:
    """How much of the unclaimed work each pooled account's meter carried, measured.

    The work no login claims on the pooled projects root (`stretch_amount`) is the headless
    `claude -p` runs no durable record attributes (tracker/unclaimed.py): since 21 September
    the auto-mail filing judge's seat is picked per run and not written down. Left out, it
    is meter movement with no tokens, and the stretch reads low; added whole to every pooled
    account, it is counted once per account. So the share is fitted: the s in [0, 1] per
    account that minimises the squared scatter of log((credits + s x unclaimed credits) /
    meter %) about each side's own mean, either side of `at` as the known-date test splits
    them, summed over the accounts. Accounts whose stretches carrying unclaimed work overlap
    in time read the same unclaimed turns there, so they are fitted together with their
    shares summing to at most 1 (`_unclaimed_caps`, listed as `capped_with`); an account that
    overlaps no other is fitted on its own. The 95% interval is a percentile bootstrap,
    stretches resampled within account and side, with a fixed seed. An account whose
    stretches carry no unclaimed work is not listed and adds nothing.
    """
    grid = [i / UNCLAIMED_GRID for i in range(UNCLAIMED_GRID + 1)]
    rows: dict[str, list[tuple]] = {}
    sides: dict[str, list[list[tuple]]] = {}
    for name in sorted(selected, key=lambda n: _label_for(labels, n, names)):
        by_side: dict[str, list[tuple]] = {"before": [], "after": []}
        for st in selected[name]:
            side = _side_of(st, at, lo, hi)
            own = value(st["tokens"]) if side else None
            if not own or own <= 0:
                continue
            extra = value(st["unclaimed_tokens"]) if st.get("unclaimed_tokens") else 0.0
            by_side[side].append((own, extra or 0.0, st["delta_pct"],
                                  datetime.fromisoformat(st["start"]), datetime.fromisoformat(st["end"])))
        account_rows = [r for rs in by_side.values() for r in rs]
        if not any(r[1] for r in account_rows):
            continue
        rows[name] = account_rows
        sides[name] = [g for g in by_side.values() if g]
    if not rows:
        return {}
    caps = _unclaimed_caps(rows)

    def scatter(groups: list[list[tuple]]) -> list[float]:
        out = []
        for s in grid:
            total = 0.0
            for g in groups:
                ys = [math.log((r[0] + s * r[1]) / r[2]) for r in g]
                m = sum(ys) / len(ys)
                total += sum((y - m) ** 2 for y in ys)
            out.append(total)
        return out

    shares = _capped_best({n: scatter(g) for n, g in sides.items()}, caps, grid)
    rng = random.Random(f"{JOINT_SEED}:unclaimed:{_utc(at)}")
    draws: dict[str, list[float]] = {n: [] for n in rows}
    for _ in range(UNCLAIMED_BOOTSTRAP):
        drawn = _capped_best({n: scatter([[rng.choice(g) for _ in g] for g in gs]) for n, gs in sides.items()},
                             caps, grid)
        for n, v in drawn.items():
            draws[n].append(v)
    out = {}
    for name in rows:
        d = sorted(draws[name])
        out[_label_for(labels, name, names)] = {
            "share": shares[name], "interval": [d[int(0.025 * len(d))], d[int(0.975 * len(d)) - 1]],
            "capped_with": sorted(_label_for(labels, m, names) for cap in caps if name in cap
                                  for m in cap if m != name),
            "n_with_unclaimed": sum(1 for r in rows[name] if r[1]), "n": len(rows[name])}
    return out


def split_at_candidate(stretches: list[dict], at: datetime, lo: datetime | None,
                       hi: datetime | None, value, unclaimed_share: float = 0.0) -> dict:
    """One account's stretches either side of `at`, as log credits per 1%, with counts.

    Before: start at or after `lo` and end at or before `at`. After: start at or after
    `at` and end at or before `hi`. A stretch spanning either boundary is on neither side.
    `rounding` holds each kept stretch's `rounding_variance`, in the same order as `sides`.
    A stretch's credits include `unclaimed_share` of its unclaimed work (`stretch_amount`).
    """
    sides: dict[str, list[float]] = {"before": [], "after": []}
    rounding: dict[str, list[float]] = {"before": [], "after": []}
    unverified = {"before": 0, "after": 0}
    unpriced = {"before": 0, "after": 0}
    for st in stretches:
        side = _side_of(st, at, lo, hi)
        if side is None:
            continue
        amount = stretch_amount(st, value, unclaimed_share)
        if amount is None or amount <= 0:
            unpriced[side] += 1
            continue
        sides[side].append(math.log(amount / st["delta_pct"]))
        rounding[side].append(rounding_variance(st))
        if st.get("reset_verified") is not True:
            unverified[side] += 1
    return {"sides": sides, "rounding": rounding, "unverified": unverified, "unpriced": unpriced}


def _known_date_rows(selected: dict[str, list[dict]], value, at: datetime,
                     lo: datetime | None, hi: datetime | None, labels: dict[str, str],
                     names: list[str]) -> tuple[dict, dict, dict]:
    """(per-account rows, paired log ratios, unclaimed shares) of the known-date test at `at`,
    its before side from `lo` and after side to `hi` (`announced_change`)."""
    per_account, paired = {}, {}
    shares = unclaimed_shares(selected, value, at, lo, hi, labels, names)
    for name in sorted(selected, key=lambda n: _label_for(labels, n, names)):
        label = _label_for(labels, name, names)
        split = split_at_candidate(selected[name], at, lo, hi, value,
                                   shares.get(label, {}).get("share", 0.0))
        sides = split["sides"]
        row = {"n_before": len(sides["before"]), "n_after": len(sides["after"]),
               "n_before_reset_unverified": split["unverified"]["before"],
               "n_after_reset_unverified": split["unverified"]["after"],
               "n_before_unpriced": split["unpriced"]["before"],
               "n_after_unpriced": split["unpriced"]["after"],
               "change_pct": None, "interval_pct": None, "combined": False}
        pair = log_ratio_side(sides["before"], sides["after"],
                              split["rounding"]["before"], split["rounding"]["after"])
        if pair is not None:
            paired[label] = pair
            row.update({"change_pct": round((pair["ratio"] - 1) * 100, 1),
                        "interval_pct": [round((x - 1) * 100, 1) for x in pair["interval"]],
                        "combined": True,
                        # What the leave-one-out refit of this window change reads
                        # (`five_hour_on_meters`, `direct_change`).
                        "log_ratio": pair["log_ratio"], "se": pair["se"], "df": pair["df"]})
        per_account[label] = row
    return per_account, paired, shares


def announced_change(by_account: dict[str, list[dict]], runs: list[HarnessRun], credits: dict,
                     value, labels: dict[str, str],
                     announcements: list[dict] | None = None,
                     joint_fits: dict | None = None) -> dict:
    """A known-date test of every change candidate, per account and combined.

    `value(tokens)` is a stretch's credits (the publisher passes `comparison_value`, so a
    family's published state moves no figure here), or None where it cannot be priced; such a stretch is
    counted and left out. Each candidate's before side runs from the previous boundary --
    the weekly change (CUT_AT) or an earlier candidate, whichever is later -- to the
    candidate; its after side from the candidate to the next boundary. Each account is
    compared with itself (`log_ratio_side`) and the accounts combined
    (`combine_inverse_variance`), pairing accounts the way the weekly event does. An
    account without both sides is listed with its counts and not combined. `joint_fits`
    (from `absorb_new_family_rates`, keyed by family and instant) is attached to each
    candidate as `joint_fit`; `five_hour_on_meters` decides the scope from it.
    """
    selected = announced_change_stretches(by_account, runs)
    cands = change_candidates(by_account, credits, announcements)
    names = list(by_account)
    out = []
    for cand in cands:
        at = cand["at"]
        lo, hi = candidate_bounds(at, cands)
        per_account, paired, shares = _known_date_rows(selected, value, at, lo, hi, labels, names)
        combined = combine_inverse_variance(paired)
        interval_pct = [round((x - 1) * 100, 1) for x in combined["interval"]] if combined else None
        state = change_state(interval_pct)
        excludes = bool(combined and not combined["interval"][0] <= 1 <= combined["interval"][1])
        for k, w in (combined or {}).get("weights", {}).items():
            per_account[k]["weight"] = round(w, 4)
        out.append({
            "family": cand["family"],
            "at": _utc(at),
            "at_source": cand["at_source"],
            "first_seen_account": _label_for(labels, cand["first_seen_account"], names),
            "first_seen_stretch_end": _utc(cand["first_seen_stretch_end"]),
            "before_from": _utc(lo) if lo else None,
            "after_until": _utc(hi) if hi else None,
            "state": state,
            "change_pct": round((combined["ratio"] - 1) * 100, 1) if combined else None,
            "interval_pct": interval_pct,
            "interval_excludes_no_change": excludes,
            "accounts_combined": sorted(paired),
            "per_account": per_account,
            "unclaimed_share": shares,
            "joint_fit": (joint_fits or {}).get((cand["family"], _utc(at))),
            "announcement": cand["announcement"],
        })
    return {
        "candidates": out,
        "unit": "credits per 1% of the five-hour meter",
        "thresholds": {"measured_half_width_pct": CHANGE_MEASURED_HALF_WIDTH_PCT,
                       "min_before": ANNOUNCED_MIN_BEFORE},
        "method": (
            "candidates are each model family's first use across every account; the candidate "
            "instant is the family's first turn where the stretch records it (`at_source` "
            "first_turn), else the start of the stretch that holds it (stretch_start). Per "
            "account, accepted stretches "
            "(reset-verified or not, counted) are valued in credits and split at the candidate; "
            "the before side starts at the previous boundary (the 14 September weekly change or "
            "an earlier candidate) and the after side ends at the next one, and a stretch that "
            "spans a boundary is on neither side. Each account's change is the ratio of the "
            "Huber M-estimates of its log credits per 1% on the two sides, each stretch's "
            "variance the robust scatter (median absolute deviation) plus its own whole-percent "
            "rounding variance, so a stretch with work the transcripts missed or the meter did "
            "not count weighs less the further it lands; a 95% t interval on the logs uses the "
            "scatter pooled over both sides. A stretch's credits "
            "include the account's fitted share (`unclaimed_share`, 0 to 1) of the work no login "
            "claims on the pooled projects root in its span. Accounts with at least "
            f"{ANNOUNCED_MIN_BEFORE} stretches before and 1 after are combined as a weighted "
            "mean of log ratios, weights 1 / se squared. The state is measuring while the "
            "interval includes no change, provisional once it excludes it, and measured once "
            f"it also has a half-width of {CHANGE_MEASURED_HALF_WIDTH_PCT:g} points or less "
            "(`change_state`). `joint_fit` is the candidate "
            "family's rate fitted jointly with the five-hour limit change "
            "(`joint_rate_fit`); `five_hour_on_meters` reads it to decide the change's scope. "
            "`announcement` is reference metadata and changes no figure."),
    }


#: The joint fit's search range for a new family's rate, relative to its base family's.
JOINT_RATE_BOUNDS = (0.05, 20.0)
#: Bootstrap draws behind the joint fit's intervals, and its fixed seed (a publish is
#: reproducible: the same history gives the same intervals).
JOINT_BOOTSTRAP = 200
JOINT_SEED = 20260928
#: The fit separates the rate from the limit change when the rate's 95% interval spans at
#: most this factor end to end and touches neither search bound. A mix too uniform to
#: separate them leaves the rate's interval spread across the range. At 3x, fits whose rate
#: ran from 0.64x to 1.47x its base family's passed, and their g swung with the rate.
JOINT_SEPARABLE_SPAN = 1.5
#: The rate is identified only by stretches that mix the new family with known-rate work:
#: the fit is separable only with at least JOINT_MIN_MIXED pooled after-side stretches whose
#: new-family share lies in JOINT_MIXED_SHARE. A stretch nearly all one family or the other
#: reads the level of one side, not the rate.
JOINT_MIXED_SHARE = (0.2, 0.8)
JOINT_MIN_MIXED = 3
#: The rate check (`joint_rate_check`, ADR 0001 rule 8): a fitted rate counts only if its 95%
#: interval reaches this band around the new family's list-price ratio to its base family. A
#: fit that can only explain the meter by pricing a new model at under half or over twice what
#: its list price says has not told the rate and the limit change apart
#: (docs/findings-2026-10-02-one-account-change.md).
JOINT_RATE_PLAUSIBLE = (0.5, 2.0)
#: The price class the rate check reads the list-price ratio in. The joint fit values a
#: family by its input rate: input and cache writes at that rate, output at a fixed multiple
#: of it (`comparison_value`), so the input price is the one its `rate_relative_to_base`
#: scales.
JOINT_RATE_PRICE_CLASS = "input"


def list_price_ratio_to_base(fam: str, base: str, credits: dict,
                             prices: dict | None = None) -> float | None:
    """A family's list price over its base family's, in JOINT_RATE_PRICE_CLASS, or None.

    From data/prices.json (`_list_prices`) unless `prices` is given. None when either row,
    or its price in that class, is missing: there is then no list price to check against.
    """
    if prices is None:
        prices = _list_prices()
    num = (prices.get(list_price_model(fam, credits)) or {}).get(JOINT_RATE_PRICE_CLASS)
    den = (prices.get(list_price_model(base, credits)) or {}).get(JOINT_RATE_PRICE_CLASS)
    return num / den if num and den else None


def joint_rate_check(fam: str, base: str, credits: dict, interval: list[float] | None,
                     prices: dict | None = None) -> dict:
    """ADR 0001 rule 8: whether a joint fit's rate interval is one a list price can explain.

    Passed when the fitted `rate_relative_to_base` interval overlaps JOINT_RATE_PLAUSIBLE
    times the list-price ratio of the new family to its base family, in
    JOINT_RATE_PRICE_CLASS. Failed when it misses that band: the fit then reads the meter's
    movement as a rate far from what the model lists at, which is a limit change, missing or
    extra work, or a mix of them, and not a rate. `not_applicable` without a list price, and
    its `reason` says the test was skipped.
    """
    ratio = list_price_ratio_to_base(fam, base, credits, prices)
    out = {"state": "not_applicable", "price_class": JOINT_RATE_PRICE_CLASS,
           "list_price_ratio": round(ratio, 4) if ratio is not None else None,
           "plausible_band": None, "rate_relative_interval": interval, "reason": None}
    if ratio is None:
        out["reason"] = (f"no {JOINT_RATE_PRICE_CLASS} list price for {family_label(fam)} or "
                         f"{family_label(base)} in data/prices.json, so the rate check was skipped")
        return out
    lo, hi = (ratio * f for f in JOINT_RATE_PLAUSIBLE)
    out["plausible_band"] = [round(lo, 4), round(hi, 4)]
    if not interval:
        out["reason"] = "no fitted rate interval, so the rate check was skipped"
        return out
    passed = interval[0] <= hi and interval[1] >= lo
    out["state"] = "passed" if passed else "failed"
    if not passed:
        out["reason"] = (f"the fitted rate's 95% interval, {interval[0]:.2f}x to {interval[1]:.2f}x "
                         f"{family_label(base)}, misses {lo:.2f}x to {hi:.2f}x, half to twice "
                         f"{family_label(fam)}'s {JOINT_RATE_PRICE_CLASS} list-price ratio of "
                         f"{ratio:.2f}x: the fit has not separated the rate from the limit change")
    return out


def base_family(fam: str, credits: dict) -> str:
    """The family a new family's tokens are valued at before its own rate is known.

    Its head (`opus-5-5` -> `opus`, an auto-filed `sonnet-5-5` -> `sonnet`) where the table
    has that family, else the Opus anchor. The joint fit reports the new rate relative to it.
    """
    head = fam.split("-")[0]
    return head if head != fam and head in (credits.get("per_family") or {}) else "opus"


def _base_model(fam: str, credits: dict) -> str:
    members = (credits.get("per_family") or {}).get(fam, {}).get("members") or []
    return members[0] if members else f"claude-{fam}"


def _merge_bundles(bundles: list[dict]) -> dict:
    out: dict[str, float] = {}
    for tok in bundles:
        for k, v in tok.items():
            if isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + v
    return out


def _joint_rows(stretches: list[dict], fam: str, at: datetime, lo: datetime | None,
                hi: datetime | None, value, credits: dict,
                unclaimed_share: float = 0.0) -> tuple[list[tuple], list[tuple]]:
    """One account's before side as (log known credits per 1%, rounding variance) pairs, and
    its after side as (known credits, new-family credits at its base family's rate, meter %,
    rounding variance) rows. The rounding variance is `rounding_variance`; `unclaimed_share`
    of the stretch's unclaimed work (`stretch_amount`) counts as known-rate work."""
    base = _base_model(base_family(fam, credits), credits)
    before, after = [], []
    for st in stretches:
        if not st.get("delta_pct") or st["delta_pct"] <= 0:
            continue
        side = _side_of(st, at, lo, hi)
        if side is None:
            continue
        tokens = st.get("tokens") or {}
        own = [t for m, t in tokens.items() if family(m, credits) == fam and isinstance(t, dict)]
        rest = {m: t for m, t in tokens.items() if family(m, credits) != fam}
        known = value(rest) if rest else 0.0
        new = value({base: _merge_bundles(own)}) if own else 0.0
        if known is None or new is None:
            continue
        if unclaimed_share and st.get("unclaimed_tokens"):
            known += unclaimed_share * (value(st["unclaimed_tokens"]) or 0.0)
        if side == "before":
            if known > 0 and not new:
                before.append((math.log(known / st["delta_pct"]), rounding_variance(st)))
        elif known + new > 0:
            after.append((known, new, st["delta_pct"], rounding_variance(st)))
    return before, after


def _joint_solve(groups: list[tuple[list[tuple], list[tuple]]],
                 s2: float | None = None) -> tuple[float, float, float] | None:
    """(rate relative to base, log limit change, residual spread) minimising the squared log
    residuals of (known + rate x new) / meter % against each account's own before level.

    With `s2` (the scatter left once rounding is out, `_joint_scatter`) each stretch's
    variance is s2 plus its own rounding variance, and the fit is a Huber M-estimate: each
    account's before level is `huber_location`, the after side's log change m is the Huber
    location of the residuals, and r minimises the Huber loss (`_huber_rho`) of the
    standardised residuals, so a stretch with missing or extra work counts for less the
    further it lands. Without `s2` it is plain least squares, every stretch weighing the
    same. The spread returned is the unweighted standard deviation of the after side's
    residuals either way, so it stays comparable."""
    rows = []
    robust = s2 is not None
    for before, after in groups:
        if robust and all(s2 + v > 0 for _, v in before):
            level = huber_location([x for x, _ in before], [s2 + v for _, v in before])[0]
        else:
            level = sum(x for x, _ in before) / len(before)
        rows += [(k, u, d, level, s2 + v if s2 is not None and s2 + v > 0 else 1.0)
                 for k, u, d, v in after]
    if not rows:
        return None
    var = [x[4] for x in rows]
    last = {"m": None}

    def ss(r: float) -> tuple[float, float, list[float]]:
        e = [math.log((k + r * u) / d) - level for k, u, d, level, _ in rows]
        if robust:
            m = last["m"] = huber_location(e, var, start=last["m"])[0]
            return sum(_huber_rho((x - m) / math.sqrt(v)) for x, v in zip(e, var)), m, e
        m = sum(e) / len(e)
        return sum((x - m) ** 2 for x in e), m, e

    lo, hi = (math.log(b) for b in JOINT_RATE_BOUNDS)
    grid = [lo + (hi - lo) * i / 40 for i in range(41)]
    best = min(range(len(grid)), key=lambda i: ss(math.exp(grid[i]))[0])
    a, b = grid[max(best - 1, 0)], grid[min(best + 1, len(grid) - 1)]
    phi = (math.sqrt(5) - 1) / 2
    for _ in range(30):
        c, d = b - phi * (b - a), a + phi * (b - a)
        if ss(math.exp(c))[0] <= ss(math.exp(d))[0]:
            b = d
        else:
            a = c
    r = math.exp((a + b) / 2)
    _, m, e = ss(r)
    plain = sum(e) / len(e)
    return r, m, math.sqrt(sum((x - plain) ** 2 for x in e) / max(len(rows) - 1, 1))


def _joint_scatter(groups: list[tuple[list[tuple], list[tuple]]], r: float, m: float) -> float:
    """The joint fit's scatter with rounding taken out: the before sides' residuals about
    each account's median and the after sides' about the fit, pooled, read robustly
    (`robust_variance`) less the mean rounding variance of every stretch in them. Falls back
    to the plain moments (`scatter_variance`) when the robust spread is zero."""
    ss, n, rounding, resid = 0.0, 0, [], []
    for before, after in groups:
        level = sum(x for x, _ in before) / len(before)
        mid = median(x for x, _ in before)
        ss += sum((x - level) ** 2 for x, _ in before)
        ss += sum((math.log((k + r * u) / d) - level - m) ** 2 for k, u, d, _ in after)
        resid += [x - mid for x, _ in before]
        after_e = [math.log((k + r * u) / d) - mid for k, u, d, _ in after]
        amid = median(after_e)
        resid += [e - amid for e in after_e]
        n += len(before) + len(after)
        rounding += [v for _, v in before] + [v for *_, v in after]
    robust = robust_variance(resid) - sum(rounding) / len(rounding)
    return robust if robust > 0 else scatter_variance(ss, n - len(groups) - 2, rounding)


#: How `plan_wide` reads a change (ADR 0001 rule 9): a plan limit changes for every account
#: at the same instant, so a change that one account alone produces is not a plan change.
PLAN_WIDE_METHOD = (
    "A plan limit change applies to every account at the same instant. So a change whose "
    "combined estimate draws on two or more accounts is refitted with each of them left out in "
    "turn, by the same estimator that produced it (the joint fit's g, the weekly limit change "
    "g times the windows-per-week ratio where that moved more, or the combined windows-per-week "
    "ratio while the fit cannot separate g), and it is plan-wide only if every refit keeps its "
    "direction with a 95% interval that still excludes no change. With two accounts that is "
    "each account alone. A change resting on one account is not plan-wide.")


def plan_wide_verdict(change_pct: float | None, accounts: list[str], without: dict[str, dict],
                      estimator: str) -> dict:
    """Whether a combined change holds with each of its accounts left out in turn.

    `without[label]` is the same estimator refitted without that account, with its
    `change_pct` and `interval_pct`; each entry gains `holds`, True when the refit keeps the
    full change's direction and its interval excludes no change on that side. `passed` when
    every refit holds and there are at least two accounts; `failed` otherwise, with the
    reason; `untested` without a change to test. See PLAN_WIDE_METHOD.
    """
    out = {"state": "untested", "estimator": estimator, "accounts": list(accounts),
           "without": without, "reason": None}
    if change_pct is None:
        out["reason"] = "no combined change to test"
        return out
    if len(accounts) < 2:
        out.update(state="failed", reason=(
            f"the change rests on one account ({', '.join(accounts) or 'none'}); a plan limit "
            "change applies to every account at once"))
        return out
    failing = []
    for label, row in without.items():
        iv = row.get("interval_pct")
        row["holds"] = bool(row.get("change_pct") is not None and iv and change_pct != 0
                            and (iv[0] > 0 if change_pct > 0 else iv[1] < 0))
        if not row["holds"]:
            failing.append(label)
    if not failing:
        out["state"] = "passed"
        return out

    def reads(label: str) -> str:
        row = without[label]
        if row.get("change_pct") is None:
            return f"without {label} there is no estimate"
        lo, hi = row["interval_pct"] or (None, None)
        return f"without {label} it reads {row['change_pct']:+g}% [{lo:g}, {hi:g}]"
    out.update(state="failed", reason=(
        f"{'; '.join(reads(k) for k in failing)}: the {change_pct:+g}% does not hold with each "
        "account left out, so it is not plan-wide"))
    return out


def _pct_of(x: float) -> float:
    return round((x - 1) * 100, 1)


def joint_rate_fit(selected: dict[str, list[dict]], fam: str, at: datetime,
                   lo: datetime | None, hi: datetime | None, value, credits: dict,
                   labels: dict[str, str], names: list[str],
                   base_times_opus: float | None = None, prices: dict | None = None,
                   leave_one_out: bool = True) -> dict:
    """A new family's rate and the five-hour limit change at its first use, fitted jointly.

    Per stretch, meter % = (known + r x new) / (L x g): `known` is the credits of every
    model whose rate the candidate did not touch, `new` the candidate family's tokens valued
    at its base family's rate (`base_family`), L the account's own credits per 1% from its
    before side (known work only), and g the limit change. Mixed stretches vary the new
    family's share, and that variation separates r from g; the fit is `separable` only when
    r's interval spans at most JOINT_SEPARABLE_SPAN and JOINT_MIN_MIXED after stretches mix
    the families at a share in JOINT_MIXED_SHARE, and `reason` names the test that failed. The fit runs once by plain least squares to measure the
    scatter robustly (`_joint_scatter`), then again as a Huber M-estimate at that scatter
    plus each stretch's own rounding variance (`_joint_solve`), so a stretch with missing or
    extra work counts for less. Its interval is set mostly by how well the mix separates r
    from g, not by the scatter: held at its point estimate, r leaves g an interval about a
    third as wide (docs/findings-2026-09-29-rates-and-missing-work.md). Each pooled account's measured share of the work
    no login claims (`unclaimed_shares`) is added to its stretches' known work. Intervals are
    a percentile bootstrap, stretches resampled within each account and side, at that
    scatter. `value(tokens)` prices a bundle in credits.

    On top of the span and mixing tests, a fit counts as separable only if it passes the rate
    check (`joint_rate_check`, ADR 0001 rule 8, against `prices` or data/prices.json): a rate
    interval that misses half to twice the list-price ratio has not separated the rate from
    the limit change. `reason` names every test that failed.

    A separable fit is refitted with each combined account left out in turn
    (`leave_one_out`; the refits themselves are not), and `plan_wide` says whether its g
    holds on every one of them (`plan_wide_verdict`, ADR 0001 rule 9). None when the fit is
    not separable: its g is then not the published figure.
    """
    per_account, groups = {}, []
    unclaimed = unclaimed_shares(selected, value, at, lo, hi, labels, names)
    for name in sorted(selected, key=lambda n: _label_for(labels, n, names)):
        label = _label_for(labels, name, names)
        before, after = _joint_rows(selected[name], fam, at, lo, hi, value, credits,
                                    unclaimed.get(label, {}).get("share", 0.0))
        shares = sorted(u / (k + u) for k, u, *_ in after)
        per_account[label] = {"n_before": len(before), "n_after": len(after),
                              "new_family_share_median": round(median(shares), 3) if shares else None,
                              "combined": False}
        if len(before) >= ANNOUNCED_MIN_BEFORE and after:
            per_account[label]["combined"] = True
            groups.append((label, before, after))
    out = {"family": fam, "base_family": base_family(fam, credits), "state": "measuring",
           "separable": False, "rate_relative_to_base": None, "rate_relative_interval": None,
           "times_opus": None, "times_opus_interval": None,
           "five_hour_limit_change_pct": None, "five_hour_limit_change_interval_pct": None,
           "log_residual_sd": None, "scatter_sd": None, "rounding_sd": None,
           "n_mixed_after": None,
           "unclaimed_share": unclaimed, "accounts_combined": [g[0] for g in groups],
           "per_account": per_account, "bootstrap": JOINT_BOOTSTRAP, "rate_check": None,
           "plan_wide": None}
    fit = _joint_solve([(b, a) for _, b, a in groups])
    if fit is None or not any(u for _, _, a in groups for _, u, *_ in a):
        out["reason"] = "no stretch after the candidate on an account with a before side"
        return out
    s2 = _joint_scatter([(b, a) for _, b, a in groups], fit[0], fit[1])
    r, logg, sd = _joint_solve([(b, a) for _, b, a in groups], s2)
    rng = random.Random(f"{JOINT_SEED}:{fam}:{_utc(at)}")
    draws = []
    for _ in range(JOINT_BOOTSTRAP):
        sample = [([rng.choice(b) for _ in b], [rng.choice(a) for _ in a]) for _, b, a in groups]
        got = _joint_solve(sample, s2)
        if got is not None:
            draws.append(got[:2])
    rs = sorted(d[0] for d in draws)
    gs = sorted(math.exp(d[1]) for d in draws)

    def q(xs: list[float], p: float) -> float:
        return xs[min(int(p * len(xs)), len(xs) - 1)]

    r_lo, r_hi = q(rs, 0.025), q(rs, 0.975)
    g_lo, g_hi = q(gs, 0.025), q(gs, 0.975)
    lo_b, hi_b = JOINT_RATE_BOUNDS
    pinned = r_lo > lo_b * 1.05 and r_hi < hi_b / 1.05 and r_hi / r_lo <= JOINT_SEPARABLE_SPAN
    share_lo, share_hi = JOINT_MIXED_SHARE
    n_mixed = sum(1 for _, _, a in groups for k, u, *_ in a
                  if k + u > 0 and share_lo <= u / (k + u) <= share_hi)
    mixed = n_mixed >= JOINT_MIN_MIXED
    check = joint_rate_check(fam, out["base_family"], credits, [round(r_lo, 4), round(r_hi, 4)],
                             prices)
    separable = pinned and mixed and check["state"] != "failed"
    failed = []
    if not pinned:
        failed.append(f"span test: the new family's share of the work varies too little to separate its rate "
                      f"from the limit change: the rate's 95% interval runs {r_lo:.2f}x to "
                      f"{r_hi:.2f}x its base family's, wider than {JOINT_SEPARABLE_SPAN:g}x end "
                      f"to end or against a search bound")
    if not mixed:
        failed.append(f"mixing test: only {n_mixed} pooled stretch{'' if n_mixed == 1 else 'es'} after the "
                      f"candidate mixed the new family at a share of {share_lo:g} to {share_hi:g} "
                      f"of the work, and the rate is identified only by mixing; "
                      f"{JOINT_MIN_MIXED} are needed")
    if check["state"] == "failed":
        failed.append(f"rate check (ADR 0001 rule 8): {check['reason']}")
    for label, b, a in groups:
        level = huber_location([x for x, _ in b], [s2 + v or 1.0 for _, v in b])[0]
        e = [math.log((k + r * u) / d) - level for k, u, d, _ in a]
        per_account[label]["five_hour_limit_change_pct"] = _pct_of(math.exp(
            huber_location(e, [s2 + v or 1.0 for *_, v in a])[0]))
    rounding = [v for _, b, a in groups for v in [x[1] for x in b] + [x[3] for x in a]]
    out.update({
        "separable": separable,
        "rate_relative_to_base": round(r, 4),
        "rate_relative_interval": [round(r_lo, 4), round(r_hi, 4)],
        "times_opus": round(r * base_times_opus, 4) if base_times_opus else None,
        "times_opus_interval": ([round(r_lo * base_times_opus, 4), round(r_hi * base_times_opus, 4)]
                                if base_times_opus else None),
        "five_hour_limit_change_pct": _pct_of(math.exp(logg)),
        "five_hour_limit_change_interval_pct": [_pct_of(g_lo), _pct_of(g_hi)],
        "log_residual_sd": round(sd, 4),
        "scatter_sd": round(math.sqrt(s2), 4),
        "rounding_sd": round(math.sqrt(sum(rounding) / len(rounding)), 4),
        # An inseparable fit's g is not a reading of the limit, so it stays measuring.
        "state": change_state([_pct_of(g_lo), _pct_of(g_hi)]) if separable else "measuring",
        "n_mixed_after": n_mixed,
        "rate_check": check,
        "reason": None if separable else "; ".join(failed),
    })
    if separable and leave_one_out:
        without = {}
        for name in selected:
            label = _label_for(labels, name, names)
            if label not in out["accounts_combined"]:
                continue
            sub = joint_rate_fit({n: rows for n, rows in selected.items() if n != name}, fam, at,
                                 lo, hi, value, credits, labels, names, base_times_opus, prices,
                                 leave_one_out=False)
            without[label] = {"change_pct": sub["five_hour_limit_change_pct"],
                              "interval_pct": sub["five_hour_limit_change_interval_pct"],
                              "accounts_combined": sub["accounts_combined"],
                              "separable": sub["separable"],
                              "rate_relative_to_base": sub["rate_relative_to_base"],
                              "rate_relative_interval": sub["rate_relative_interval"]}
        out["plan_wide"] = plan_wide_verdict(out["five_hour_limit_change_pct"],
                                             out["accounts_combined"], without,
                                             "the joint fit's five-hour limit change g")
    return out


#: `rate_source` of a family valued at its list-price ratio because the joint fit at its first
#: use did not separate its rate from a limit change (`list_until_separable`).
LIST_UNTIL_SEPARABLE = "list_price_until_separable"
#: The `rate_source`s `absorb_new_family_rates` writes: the rate every valuation then uses.
ABSORBED_SOURCES = ("joint_fit_at_first_use", LIST_UNTIL_SEPARABLE)


def list_until_separable(fam: str, fit: dict, credits: dict, model_rates: dict,
                         anchor: tuple[float, float] | None) -> dict | None:
    """ADR 0001 rule 10: the rates row of a family valued at its list price, or None.

    A family first used inside the measured record -- some account has the
    ANNOUNCED_MIN_BEFORE stretches before it that the joint fit needs for a before side --
    whose joint fit there is not separable has a rate no fit can tell apart from a limit
    change at that instant: every stretch it appears in comes after the candidate, so a
    bigger window reads exactly as a cheaper model. Such a family is valued at its input
    list-price ratio to the Opus anchor (`list_price_ratio`), with output at the anchor's
    multiple and cache reads wherever the pooled fit puts them, until the fit separates.
    The row keeps the fitted `times_opus` and its interval beside the list rate, with a
    status sentence saying it is not used and why. None -- the caller keeps the fitted rate
    -- when the fit separates, when the family has no list price, when there is no anchor or
    no measured-rate source at all (the caller checks that), and when no account has a
    before side: a family first used before the measured record began has no limit change
    at its first use in the record to be confounded with.
    """
    if fit.get("separable") or anchor is None:
        return None
    if not any(row.get("n_before", 0) >= ANNOUNCED_MIN_BEFORE
               for row in (fit.get("per_account") or {}).values()):
        return None
    ratio = list_price_ratio(fam, credits)
    if ratio is None:
        return None
    old = (model_rates.get("per_family") or {}).get(fam) or {}
    pooled = model_rates.get("pooled_fit") or {}
    fitted = old.get("times_opus", (pooled.get("times_opus") or {}).get(fam))
    fitted_iv = old.get("times_opus_interval", (pooled.get("interval") or {}).get(fam))
    fitted_txt = (f"the fitted {fitted:.3g}x Opus" if fitted is not None
                  else "the fitted rate")
    # The fit's own detail (its fits, intervals, why) stays on the row beside the list rate.
    return {
        **old,
        "input": ratio * anchor[0], "interval": None, "output_multiplier": anchor[1],
        "status": (f"{family_label(fam)} is valued at its list price, {ratio:.3g}x Opus; "
                   f"{fitted_txt} is not used, because the joint fit at its first use "
                   f"({fit.get('reason') or 'not separable'}) cannot tell that rate apart from "
                   f"a five-hour limit change at that instant. The fitted rate takes over once "
                   f"that fit separates."),
        "rate_source": LIST_UNTIL_SEPARABLE, "anchor": False, "provisional": False,
        "rate_in_use": "list", "list_times_opus": ratio,
        "times_opus": fitted, "times_opus_interval": fitted_iv,
        "replaced": {k: old.get(k) for k in ("input", "interval", "rate_source", "times_opus")},
    }


def _base_input_rate(fam: str, credits: dict, model_rates: dict | None) -> tuple[float, float] | None:
    """(input rate, output multiplier) of a family as the joint fit values stretches at it:
    `comparison_rate`, so the rate a fit is reported against is the one it was fitted with."""
    fit = comparison_rate(fam, credits, model_rates)
    if fit is not None and fit[0]:
        return fit[0], fit[1] / fit[0]
    rate = family_rate(fam, credits, model_rates)
    if rate.input is not None and rate.output is not None:
        return rate.input, rate.output / rate.input
    if rate.input_interval and rate.output_interval:
        return sum(rate.input_interval) / 2, sum(rate.output_interval) / sum(rate.input_interval)
    pair = rates(fam, credits)
    return (pair[0], pair[1] / pair[0]) if pair else None


def absorb_new_family_rates(by_account: dict[str, list[dict]], runs: list[HarnessRun],
                            credits: dict, model_rates: dict | None, labels: dict[str, str],
                            value_for=None) -> tuple[dict, dict]:
    """Every candidate family's rate fitted at its first use, and the pricing with it absorbed.

    Candidates are taken oldest first, and each one the fit separates replaces that family's
    row in the measured rates before the next is fitted, so a later candidate's known work
    is valued at the earlier ones' fitted rates. One it does not separate is valued at its
    list price instead (`list_until_separable`, ADR 0001 rule 10), in its row and in the
    pooled fit's coefficients alike, so every valuation downstream reads the same rate. `value_for(rates)` is the stretch valuation
    under a rates block; by default, and as the publisher passes it, `comparison_value`: the
    fit is a before-and-after comparison, so every known family is priced at the pooled
    fit's point estimate whether the page publishes that family's rate or not. Returns (the
    rates block with the fitted rows, the fits keyed by family and instant). A fitted row
    keeps the one it replaced under `replaced`.
    """
    if value_for is None:
        def value_for(rates):
            return comparison_value(credits, rates)
    rates_now = dict(model_rates or {})
    rates_now["per_family"] = dict(rates_now.get("per_family") or {})
    selected = announced_change_stretches(by_account, runs)
    cands = change_candidates(by_account, credits, [])
    names = list(by_account)
    anchor = _base_input_rate("opus", credits, rates_now)
    fits = {}
    for cand in cands:
        fam, at = cand["family"], cand["at"]
        lo, hi = candidate_bounds(at, cands)
        base = _base_input_rate(base_family(fam, credits), credits, rates_now)
        fit = joint_rate_fit(selected, fam, at, lo, hi, value_for(rates_now), credits, labels,
                             names, base_times_opus=(base[0] / anchor[0] if base and anchor else None))
        fits[(fam, _utc(at))] = fit
        if not fit["separable"] or base is None:
            listed = list_until_separable(fam, fit, credits, rates_now, anchor) if model_rates else None
            if listed is not None:
                rates_now["per_family"][fam] = listed
                pooled = rates_now.get("pooled_fit")
                if isinstance(pooled, dict) and isinstance(pooled.get("times_opus"), dict):
                    rates_now["pooled_fit"] = {**pooled, "times_opus": {
                        **pooled["times_opus"], fam: listed["list_times_opus"]}}
            continue
        r, (r_lo, r_hi) = fit["rate_relative_to_base"], fit["rate_relative_interval"]
        old = rates_now["per_family"].get(fam) or {}
        rates_now["per_family"][fam] = {
            "input": base[0] * r, "interval": [base[0] * r_lo, base[0] * r_hi],
            "output_multiplier": base[1], "status": None,
            "rate_source": "joint_fit_at_first_use", "anchor": False, "provisional": False,
            "rate_in_use": "fitted",
            "list_times_opus": list_price_ratio(fam, credits),
            "times_opus": fit["times_opus"], "times_opus_interval": fit["times_opus_interval"],
            "joint_fit": {"at": _utc(at), "base_family": fit["base_family"],
                          "rate_relative_to_base": r, "rate_relative_interval": [r_lo, r_hi]},
            "replaced": {k: old.get(k) for k in ("input", "interval", "rate_source", "times_opus")},
        }
    # Nothing absorbed: the caller's block itself, so an empty one still reads as no source.
    changed = any(r.get("rate_source") in ABSORBED_SOURCES
                  for r in rates_now["per_family"].values())
    return (rates_now if changed else model_rates), fits


def paired_levels(note: dict | None, by_window: list[dict]) -> tuple[dict, dict] | None:
    """The before and after levels over the paired accounts only, each on its own sides.

    The chart draws these as the step across the change instead of the pooled regimes,
    which hold an account on one side only (a3 and a4 joined after the cut) and put
    an early-stepping account's post-step windows on the before side (a1 stepped on
    11 September, the pooled split is 14 September). Each level pools the paired
    accounts' windows from their own regime on that side (`note.per_account`): total
    five-hour movement over total seven-day movement, with that pool's rounding
    interval. The accounts step at their own seven-day resets, so the level's `start`
    and `end` are drawn at the earliest step -- the event's own date -- and
    `per_account` names each account's actual span. None when no account pairs.
    """
    per_account = (note or {}).get("per_account") or {}
    if not per_account:
        return None
    rows = {"before": [], "after": []}
    spans = {"before": {}, "after": {}}
    for label, a in per_account.items():
        mine = [r for r in by_window if r.get("account") == label]
        rows["before"] += _regime_rows(mine, a["before"])
        rows["after"] += _regime_rows(mine, a["after"],
                                      exclude_at=datetime.fromisoformat(a["before"]["end"]))
        for side in ("before", "after"):
            spans[side][label] = {"start": a[side]["start"], "end": a[side]["end"],
                                  "windows": a[side]["ratio"]}

    def level(side: str, start: str, end: str) -> dict:
        pool = rows[side]
        d5 = sum(r["five_hour_pct"] for r in pool)
        d7 = sum(r["seven_day_pct"] for r in pool)
        pieces = sum(r.get("pieces", 1) for r in pool)
        lo, hi = ratio_interval(d5, d7, pieces)
        return {"start": start, "end": end, "windows": round(d5 / d7, 2) if d7 else None,
                "seven_day_pct": round(d7, 1), "points": len(pool), "pieces": pieces,
                "rounding_interval": [round(x, 4) if x is not None else None for x in (lo, hi)],
                "quality": "bounded" if hi is not None else "insufficient_precision",
                "accounts": sorted(per_account), "per_account": spans[side],
                "source": "passive_paired_deltas_same_accounts", "assumed": False}

    before_starts = [s["start"] for s in spans["before"].values()]
    before_ends = [s["end"] for s in spans["before"].values()]
    after_starts = [s["start"] for s in spans["after"].values()]
    after_ends = [s["end"] for s in spans["after"].values()]
    return (level("before", min(before_starts, key=datetime.fromisoformat),
                  min(before_ends, key=datetime.fromisoformat)),
            level("after", min(after_starts, key=datetime.fromisoformat),
                  max(after_ends, key=datetime.fromisoformat)))


def windows_per_week_ratio_note(weekly: dict) -> dict | None:
    """What the windows-per-week ratio (five-hour movement over seven-day movement)
    identifies across the certified change, measured on the same accounts either side.

    The watched accounts differ in five-hour window size, and so in windows per week,
    and they joined the pool at different times: a3's meter log starts 15 September and
    a4's on 23 September, both after the cut. Pooling every account's windows either side
    of the pooled regimes therefore changes the figure through which accounts are in the
    pool, not only through the limit. So the change is measured account by account, each
    against itself over its own last certified step (`_paired_account`), only for the
    accounts with readings on both sides, and then combined (`combine_log_ratios`). An
    account with readings on one side only is named under `excluded` with the reason; it
    still counts towards every current-level figure elsewhere.

    `ratio_fell_pct` is the combined fall, `ratio_fell_interval_pct` its rounding
    interval, and `consistent_with` reworks the one-equation-two-unknowns algebra from
    the combined figure: a fall in this ratio can come from the weekly cap, the
    five-hour window, or both, and none of the splits is claimed. They are worked
    examples ported from the Codex review of commit 447b926 (2026-09-20), which also
    retracted the cross-account-spread argument this replaces (see
    `ACROSS_CUT_UNRESOLVED`). `pooled_all_accounts` keeps the old figure -- every
    account's windows in the last two pooled regimes -- for the record, since the charts
    still draw those regimes (docs/findings-2026-09-23-pooled-rates.md, "Same accounts
    either side").
    """
    max20 = weekly["max20"]
    # The chart's own `regimes` are the paired levels (`paired_levels`) once there are any;
    # the pooled record keeps the detector's regimes over every account.
    regimes = max20.get("regimes_pooled_all_accounts") or max20["regimes"]
    if len(regimes) < 2:
        return None
    by_window = max20["by_window"]
    pooled_before = _side(_regime_rows(by_window, regimes[-2]), regimes[-2])
    pooled_after = _side(_regime_rows(by_window, regimes[-1],
                                      exclude_at=datetime.fromisoformat(regimes[-2]["end"])),
                         regimes[-1])
    if not pooled_before or not pooled_after:
        return None

    per_account: dict[str, dict] = {}
    excluded: dict[str, str] = {}
    for label, block in sorted((max20.get("by_account") or {}).items()):
        rows = [r for r in by_window if r.get("account") == label]
        if not rows:
            continue
        if not block.get("step") or len(block.get("regimes") or []) < 2:
            excluded[label] = _one_sided_reason(rows, regimes[-2], regimes[-1])
            continue
        paired = _paired_account(rows, block)
        if paired is None:
            reason = _one_sided_reason(rows, regimes[-2], regimes[-1])
            excluded[label] = ("rounding_interval_unbounded"
                               if reason == "no_certified_step_of_its_own" else reason)
            continue
        per_account[label] = paired
    pooled = {
        "before": pooled_before, "after": pooled_after,
        "ratio_fell_pct": round((1 - pooled_after["ratio"] / pooled_before["ratio"]) * 100, 2),
        "why_not_used": ("pools every account's windows in each of the last two pooled "
                         "regimes, so an account that only has readings after the change "
                         "moves the after side through account mix; kept for the record"),
    }
    if not per_account:
        # No account has readings on both sides of its own step: nothing is measured
        # against itself, so no change is stated -- not the pooled one in its place.
        return {"accounts": [], "excluded": excluded, "per_account": {},
                "ratio_after_over_before": None, "ratio_interval": None,
                "ratio_fell_pct": None, "ratio_fell_interval_pct": None,
                "pooled_all_accounts": pooled, "consistent_with": [], "unit": "percent",
                "method": "no account has readings on both sides of its own certified step"}
    combined = combine_log_ratios(per_account)
    for label, w in combined["weights"].items():
        per_account[label]["weight"] = w
    rho = combined["ratio"]
    rho_lo, rho_hi = combined["interval"]
    fall_pct = round((1 - rho) * 100, 2)

    def pct(x: float) -> float:
        return round(x * 100, 2)

    return {
        "accounts": sorted(per_account),
        "excluded": excluded,
        "per_account": per_account,
        "ratio_after_over_before": round(rho, 4),
        "ratio_interval": [round(rho_lo, 4), round(rho_hi, 4)],
        "ratio_fell_pct": fall_pct,
        "ratio_fell_interval_pct": [pct(1 - rho_hi), pct(1 - rho_lo)],
        "pooled_all_accounts": pooled,
        "consistent_with": [
            {"description": "the weekly cap falls by the whole measured amount, the five-hour "
                            "window unchanged",
             "weekly_cap_change_pct": -fall_pct, "five_hour_window_change_pct": 0.0,
             "weekly_cap_change_interval_pct": [pct(rho_lo - 1), pct(rho_hi - 1)]},
            {"description": "the five-hour window rises, the weekly cap unchanged",
             "weekly_cap_change_pct": 0.0, "five_hour_window_change_pct": pct(1 / rho - 1),
             "five_hour_window_change_interval_pct": [pct(1 / rho_hi - 1), pct(1 / rho_lo - 1)]},
        ],
        "unit": "percent",
        "method": ("each account with readings on both sides against itself: the pooled ratio of "
                   "five-hour to seven-day meter movement over its own last two certified regimes "
                   "(tracker/detect.py weighted_regimes), after over before, with an interval from "
                   "both sides' rounding intervals at their far ends; then a weighted mean of the "
                   "accounts' log ratios, each weighted by the inverse square of its own log "
                   "interval's half-width, and the same weighted mean of their interval ends as "
                   "the combined interval. The fall is one equation in the ratio of the two meters' "
                   "own budget changes; `consistent_with` lists example splits that reproduce it "
                   "exactly, not measurements of which one moved."),
    }


#: A five-hour window lasts this long, so a window whose ending falls this soon after a
#: change instant began before it: it straddles the change and is on neither side.
FIVE_HOUR_SPAN = timedelta(hours=5)

WINDOWS_PER_WEEK_METHOD = (
    "Every change candidate (each model family's first use, `announced_change`) is measured; "
    "no announcement decides whether or how. Per account, the windows per week (five-hour "
    "meter movement over seven-day meter movement, pooled over the account's own "
    "`weekly_windows.max20.by_window` readings, the same pooling and rounding interval as the "
    "14 September paired measurement) is compared either side of the candidate instant. The "
    "before side runs from the previous boundary (the 14 September weekly change or an earlier "
    "candidate), or from the account's own certified weekly step where that is later, to the "
    "instant; the after side from five hours after the instant (a window ending sooner began "
    "before it) to the next boundary, or to the end of the account's own pre-step regime where "
    "the candidate precedes that step. An account needs "
    f"{ANNOUNCED_MIN_BEFORE} readings before and 1 after, and bounded rounding intervals on both "
    "sides. The accounts' ratios (after over before) are combined as a weighted mean of log "
    "ratios, weights the inverse square of each account's own log interval half-width, and the "
    "combined interval is the same weighted mean of the interval ends. No model rate enters. "
    "`windows_per_week_change_pct` is that ratio as a change, and it is scope-free: a fall can "
    "come from a larger five-hour window or a smaller weekly cap. `readings` works out both: "
    "the five-hour change 1 / ratio - 1 with the weekly cap unchanged, and the weekly cap "
    "change ratio - 1 with the window unchanged. `scope` decides between them from data. The "
    "joint fit (`joint_fit`, `joint_rate_fit`) solves, over mixed stretches either side of "
    "the candidate, for the new family's rate and the five-hour limit change g together: "
    "per stretch, meter % = (credits of models whose rate is known + rate x the new family's "
    "tokens at its base family's rate) / (the account's own before level x g). The weekly "
    "limit's change is g times the windows-per-week ratio, its interval from g's bootstrap "
    "interval and the ratio's (`windows_per_week_ratio_bootstrap_interval`) as independent "
    "log-normal ones. The fit separates the rate from g only when the rate is pinned: its 95% "
    f"interval spans at most {JOINT_SEPARABLE_SPAN:g}x end to end and touches neither search "
    f"bound, and at least {JOINT_MIN_MIXED} pooled stretches after the candidate mix the new "
    f"family at a share of {JOINT_MIXED_SHARE[0]:g} to {JOINT_MIXED_SHARE[1]:g} of the work, "
    "because only mixing identifies the rate, and its rate interval reaches half to twice the "
    "new family's input list-price ratio to its base family (`joint_fit.rate_check`, ADR 0001 "
    "rule 8; a family with no list price skips that test and says so); `scope.reason` names "
    "the test that failed. "
    "`scope` describes which of the fit's intervals exclude no change: `five_hour` g's "
    "alone, `weekly` g times the ratio's alone, `both` both, `undetermined` neither; it is "
    "reference and certifies nothing. "
    "A candidate is certified on the two direct measurements (ADR 0001 rules 9 and 11), "
    "never on the meter ratio or the joint fit. `window_change` is the known-date test's "
    "change in credits per 1% of the five-hour meter (`announced_change`, each account "
    "against itself); `weekly_change` the same shape on the seven-day meter: each account's "
    "credits over its seven-day points in its one-point steps either side (`weekly_meter."
    "weekly_change`, the same bounds as the windows per week above, its standard error from "
    "resampled whole UTC days, at least "
    f"{ANNOUNCED_MIN_BEFORE} steps before and two days each side). Each combines the accounts "
    "by inverse variance and is refitted with each account left out (`plan_wide`); it is "
    "`certified` when that holds and its own 95% interval excludes no change. "
    "`certified_on` names the certified ones, `headline` is a certified one (the larger "
    "where both are), else the larger measured one, else the windows-per-week change, and "
    "`state` reads its interval (`change_state`): measuring while it includes no change, "
    "provisional once it excludes it, measured once it also has a half-width of "
    f"{CHANGE_MEASURED_HALF_WIDTH_PCT:g} points or less; the notify step's 24 and 48 hour "
    "rules time the email and never set it. `change_pct` and `interval_pct` are the window "
    "change. A candidate after the weekly change that can be measured at all (`measurable`) "
    "opens a window regime, which carries the previous window unless the candidate applies "
    "on its window change. It `applies` -- enters `events`, can become `last_change` and "
    "reach the email -- once one of the two is certified, and steps that measurement only: "
    "a certified window change scales a thin regime's window by itself, a certified weekly "
    "change opens a per-week regime, measured directly. Every publish recomputes it. Every "
    "other candidate stays here with its figures, state and `withheld_reason`, which names "
    "each measurement and the test it failed. `announcement` is reference metadata and "
    "changes no figure, state or scope.")


def own_weekly_step_start(block: dict | None) -> datetime | None:
    """The start of an account's own last certified weekly regime, or None without a step.

    The step stands for the 14 September weekly change reaching the account at its own
    seven-day reset, so it needs a regime before it that began before the change: an account
    first watched after CUT_AT has no before side (a3's meter log starts on 15 September,
    and its own step on 21 September is a move in its meter ratio, not the weekly change).
    """
    regimes = (block or {}).get("regimes") or []
    if not (block or {}).get("step") or len(regimes) < 2:
        return None
    if datetime.fromisoformat(regimes[-2]["start"]) >= CUT_AT:
        return None
    return datetime.fromisoformat(regimes[-1]["start"])


def own_weekly_step_end(block: dict | None) -> datetime | None:
    """The end of the regime before an account's own certified weekly step, or None."""
    if own_weekly_step_start(block) is None:
        return None
    return datetime.fromisoformat(block["regimes"][-2]["end"])


def _five_hour_from_ratio(rho: float, rho_lo: float, rho_hi: float) -> tuple[float, list[float]]:
    """The five-hour change in percent, and its interval, from a windows-per-week ratio."""
    return (round((1 / rho - 1) * 100, 1),
            [round((1 / rho_hi - 1) * 100, 1), round((1 / rho_lo - 1) * 100, 1)])


def _pct_interval(lo: float, hi: float) -> list[float]:
    return [round((lo - 1) * 100, 1), round((hi - 1) * 100, 1)]


def scope_readings(rho: float, rho_lo: float, rho_hi: float) -> dict:
    """The windows-per-week ratio read under each scope: which budget moved, and by how much."""
    five, five_iv = _five_hour_from_ratio(rho, rho_lo, rho_hi)
    return {"five_hour_scope": {"five_hour_window_change_pct": five,
                                "five_hour_window_change_interval_pct": five_iv,
                                "weekly_cap_change_pct": 0.0},
            "weekly_scope": {"five_hour_window_change_pct": 0.0,
                             "weekly_cap_change_pct": round((rho - 1) * 100, 1),
                             "weekly_cap_change_interval_pct": _pct_interval(rho_lo, rho_hi)}}


def _log_normal_product(x: float, x_iv: list[float], y: float, y_iv: list[float]) -> tuple:
    """x times y, with a 95% interval from both intervals read as independent log-normal ones."""
    se = math.sqrt(sum(((math.log(hi) - math.log(lo)) / (2 * 1.96)) ** 2
                       for lo, hi in (x_iv, y_iv)))
    v = x * y
    return v, (v * math.exp(-1.96 * se), v * math.exp(1.96 * se))


def meter_scope(joint: dict | None, rho: float, rho_iv: list[float] | None) -> dict:
    """Which budget a candidate moved, from its own data (see WINDOWS_PER_WEEK_METHOD).

    `joint` is the candidate's `joint_rate_fit`: g, the five-hour limit change, fitted on
    mixed work jointly with the new family's rate. The weekly limit's change is g times the
    windows-per-week ratio (credits per 1% of the weekly meter are credits per 1% of the
    five-hour meter times five-hour over seven-day movement), its interval from g's and the
    ratio's bootstrap intervals. Undetermined until the fit separates the rate from g.
    """
    fit = joint or {}
    base = {"joint_fit_state": fit.get("state"), "separable": bool(fit.get("separable")),
            "five_hour_limit_change_pct": None, "five_hour_limit_change_interval_pct": None,
            "weekly_limit_change_pct": None, "weekly_limit_change_interval_pct": None}
    if not fit.get("separable") or fit.get("five_hour_limit_change_pct") is None or not rho_iv:
        return {"state": "undetermined", **base,
                "reason": fit.get("reason") or "no joint fit of the new family's rate and the limit"}
    g = 1 + fit["five_hour_limit_change_pct"] / 100
    g_iv = [1 + x / 100 for x in fit["five_hour_limit_change_interval_pct"]]
    w, (w_lo, w_hi) = _log_normal_product(g, g_iv, rho, rho_iv)
    five_moved, weekly_moved = not g_iv[0] <= 1 <= g_iv[1], not w_lo <= 1 <= w_hi
    state = {(True, False): "five_hour", (False, True): "weekly",
             (True, True): "both"}.get((five_moved, weekly_moved), "undetermined")
    base.update(five_hour_limit_change_pct=fit["five_hour_limit_change_pct"],
                five_hour_limit_change_interval_pct=fit["five_hour_limit_change_interval_pct"],
                weekly_limit_change_pct=_pct_of(w),
                weekly_limit_change_interval_pct=[_pct_of(w_lo), _pct_of(w_hi)])
    return {"state": state, **base,
            "reason": ("neither limit's change excludes no change yet" if state == "undetermined"
                       else "decided by which limit's change excludes no change")}


def _bootstrap_ratio(sides: dict[str, tuple[list[dict], list[dict]]], weights: dict[str, float],
                     seed: str) -> list[float] | None:
    """A 95% percentile interval on the combined windows-per-week ratio: each paired account's
    windows resampled within side, its pooled ratio recomputed, and the accounts' log ratios
    combined at their fixed weights."""
    rng = random.Random(seed)
    norm = sum(weights.values())
    draws = []
    for _ in range(JOINT_BOOTSTRAP):
        total = 0.0
        for label, (before, after) in sides.items():
            b = [rng.choice(before) for _ in before]
            a = [rng.choice(after) for _ in after]
            b7, a7 = sum(r["seven_day_pct"] for r in b), sum(r["seven_day_pct"] for r in a)
            b5, a5 = sum(r["five_hour_pct"] for r in b), sum(r["five_hour_pct"] for r in a)
            if not (b7 and a7 and b5 and a5):
                break
            total += weights[label] * math.log((a5 / a7) / (b5 / b7))
        else:
            draws.append(math.exp(total / norm))
    if len(draws) < JOINT_BOOTSTRAP // 2:
        return None
    draws.sort()
    return [round(draws[int(0.025 * len(draws))], 4),
            round(draws[min(int(0.975 * len(draws)), len(draws) - 1)], 4)]


def _candidate_plan_wide(cand: dict, headline: dict, paired: dict[str, dict],
                         sides: dict[str, tuple[list[dict], list[dict]]],
                         rho_boot: list[float] | None) -> dict:
    """The plan-wide test (`plan_wide_verdict`, ADR 0001 rule 9) of the figure a candidate
    would publish.

    That figure is `headline`: the joint fit's five-hour limit change g, or the weekly limit
    change g times the windows-per-week ratio where that moved more (the event reads the same
    one), or the combined windows-per-week change while the fit is not separable. g's refits
    are the joint fit's own (`joint_rate_fit`, `plan_wide.without`). The weekly change without
    an account is that account's g refit times the windows-per-week ratio recombined without
    it, with its bootstrap interval resampled the same way (`_bootstrap_ratio`).
    """
    metric = headline["metric"]
    joint_pw = (cand.get("joint_fit") or {}).get("plan_wide")
    if metric in ("five_hour_limit", "weekly_limit") and not joint_pw:
        return dict(plan_wide_verdict(None, [], {}, "the joint fit's five-hour limit change g"),
                    reason="the joint fit carries no leave-one-out refits")
    if metric == "five_hour_limit":
        return joint_pw
    if metric == "windows_per_week":
        without = {}
        for label in paired if len(paired) > 1 else ():
            rest = combine_log_ratios({k: v for k, v in paired.items() if k != label})
            without[label] = {"change_pct": _pct_of(rest["ratio"]),
                              "interval_pct": _pct_interval(*rest["interval"])}
        return plan_wide_verdict(headline["change_pct"], sorted(paired), without,
                                 "the combined windows-per-week ratio")
    if metric is None:
        return plan_wide_verdict(None, [], {}, "none")
    fit = cand["joint_fit"]
    accounts = sorted(set(joint_pw["accounts"]) | set(paired))
    without = {}
    for label in accounts if len(accounts) > 1 else ():
        g_row = joint_pw["without"].get(label) or {
            "change_pct": fit["five_hour_limit_change_pct"],
            "interval_pct": fit["five_hour_limit_change_interval_pct"]}
        rest = {k: v for k, v in paired.items() if k != label}
        if not rest or g_row.get("change_pct") is None:
            without[label] = {"change_pct": None, "interval_pct": None}
            continue
        if label in paired:
            combined = combine_log_ratios(rest)
            rho = combined["ratio"]
            boot = _bootstrap_ratio({k: sides[k] for k in rest}, combined["weights"],
                                    f"{JOINT_SEED}:{cand['at']}:without:{label}")
        else:
            rho, boot = combine_log_ratios(paired)["ratio"], rho_boot
        if not boot:
            without[label] = {"change_pct": None, "interval_pct": None}
            continue
        w, (w_lo, w_hi) = _log_normal_product(1 + g_row["change_pct"] / 100,
                                              [1 + x / 100 for x in g_row["interval_pct"]],
                                              rho, boot)
        without[label] = {"change_pct": _pct_of(w), "interval_pct": [_pct_of(w_lo), _pct_of(w_hi)]}
    return plan_wide_verdict(headline["change_pct"], accounts, without,
                             "the weekly limit change, g times the windows-per-week ratio")


WINDOW_CHANGE_ESTIMATOR = ("the combined change in credits per 1% of the five-hour meter, "
                           "each account against itself, weights 1 / se squared")
WINDOW_CHANGE_UNIT = "credits per 1% of the five-hour meter"


def window_change(cand: dict) -> dict:
    """The known-date test's window change (`announced_change`), combined, tested and certified.

    Each account's row carries its own log ratio, standard error and degrees of freedom, so
    the combined change can be refitted with each account left out (`direct_change`, ADR
    0001 rule 9). A row written without them is listed and not combined.
    """
    per_account, paired = {}, {}
    for label, row in sorted((cand.get("per_account") or {}).items()):
        out = {k: row.get(k) for k in ("n_before", "n_after", "change_pct", "interval_pct")}
        ok = bool(row.get("combined") and row.get("log_ratio") is not None and row.get("se"))
        if ok:
            paired[label] = {"log_ratio": row["log_ratio"], "se": row["se"], "df": row["df"]}
        per_account[label] = dict(out, combined=ok)
    return direct_change(per_account, paired, WINDOW_CHANGE_ESTIMATOR, WINDOW_CHANGE_UNIT)


#: The two direct measurements a change is certified on, by the headline metric each gives.
DIRECT_MEASUREMENTS = (("five_hour_limit", "window_change", "the five-hour window change "
                        "(credits per 1% of the five-hour meter)"),
                       ("weekly_limit", "weekly_change", "the weekly limit change "
                        "(credits per 1% of the seven-day meter)"))


def _direct_withheld_reason(at: datetime, measurable: bool, tests: dict[str, dict]) -> str | None:
    """Why a candidate is withheld, or None when one of its direct measurements is certified.

    ADR 0001 rule 9 on each direct measurement: a candidate `applies` once the five-hour
    window change or the weekly limit change holds with each account left out and its 95%
    interval excludes no change. Each part of the reason names the measurement and the test
    it failed.
    """
    if not measurable:
        return "not measurable: no account has readings on both sides of it yet"
    if at <= CUT_AT:
        return ("not measurable: dated before the 14 September weekly change, which stands for "
                "everything before it")
    if any(t["certified"] for t in tests.values()):
        return None
    why = []
    for _metric, key, name in DIRECT_MEASUREMENTS:
        t = tests[key]
        if t["change_pct"] is None:
            why.append(f"{name}: not measured, no account has both sides")
            continue
        parts = []
        if t["plan_wide"]["state"] != "passed":
            parts.append(f"plan-wide test (ADR 0001 rule 9) failed: {t['plan_wide']['reason']}")
        if not t["interval_excludes_no_change"]:
            lo, hi = t["interval_pct"]
            parts.append(f"interval test (ADR 0001 rule 9) failed: its 95% interval "
                         f"[{lo:g}, {hi:g}] includes no change")
        why.append(f"{name} {t['change_pct']:+g}%: {'; '.join(parts)}")
    return "; ".join(why)


def five_hour_on_meters(announced: dict | None, max20: dict | None,
                        weekly_rows: dict[str, list[dict]] | None = None) -> dict:
    """Every change candidate measured on the two meters, per account and combined.

    `announced` is the `announced_change` block, read for its candidates (instant, bounds,
    the window change per account, and the `joint_fit` kept for reference); `max20` is
    `weekly_windows.max20` (its `by_window` readings and `by_account` steps); `weekly_rows`
    each account's valued seven-day steps by label (`weekly_meter.valued`). A candidate is
    certified on the direct measurements (ADR 0001 rule 9): the five-hour window change
    (`window_change`) and the weekly limit change (`weekly_change`). See
    WINDOWS_PER_WEEK_METHOD. No announcement is read: a candidate's `announcement` is
    carried through as reference metadata only.
    """
    from .weekly_meter import weekly_change

    by_window = (max20 or {}).get("by_window") or []
    by_account = (max20 or {}).get("by_account") or {}
    weekly_rows = weekly_rows or {}
    labels = sorted({r["account"] for r in by_window if r.get("account")})
    out = []
    for cand in (announced or {}).get("candidates", []):
        at = datetime.fromisoformat(cand["at"])
        lo = datetime.fromisoformat(cand["before_from"]) if cand.get("before_from") else None
        hi = datetime.fromisoformat(cand["after_until"]) if cand.get("after_until") else None

        def bounds(label: str) -> tuple[datetime | None, datetime | None]:
            own = own_weekly_step_start(by_account.get(label))
            start = max([b for b in (lo, own) if b is not None and b < at], default=None)
            # A candidate before the account's own weekly step: the step bounds the after side.
            own_end = own_weekly_step_end(by_account.get(label))
            until = min([b for b in (hi, own_end) if b is not None and b > at], default=None)
            return start, until

        per_account, paired, sides = {}, {}, {}
        for label in labels:
            rows = [r for r in by_window if r.get("account") == label]
            start, until = bounds(label)
            before, after, straddling = [], [], 0
            for r in rows:
                t = datetime.fromisoformat(r["window_ending"])
                if (start is None or t >= start) and t <= at:
                    before.append(r)
                elif at < t <= at + FIVE_HOUR_SPAN:
                    straddling += 1
                elif t > at and (until is None or t <= until):
                    after.append(r)
            row = {"n_before": len(before), "n_after": len(after), "n_straddling": straddling,
                   "before_from": _utc(start) if start else None,
                   "after_until": _utc(until) if until else None,
                   "windows_per_week_before": None, "windows_per_week_after": None,
                   "ratio_after_over_before": None, "ratio_interval": None,
                   "windows_per_week_change_pct": None, "windows_per_week_change_interval_pct": None,
                   "combined": False}
            span = {"start": "", "end": ""}
            b = _side(before, span) if before else None
            a = _side(after, span) if after else None
            if b:
                row["windows_per_week_before"] = b["ratio"]
            if a:
                row["windows_per_week_after"] = a["ratio"]
            if (len(before) >= ANNOUNCED_MIN_BEFORE and b and a
                    and all(b["rounding_interval"]) and all(a["rounding_interval"])):
                (b_lo, b_hi), (a_lo, a_hi) = b["rounding_interval"], a["rounding_interval"]
                rho, rho_lo, rho_hi = a["ratio"] / b["ratio"], a_lo / b_hi, a_hi / b_lo
                paired[label] = {"ratio_after_over_before": round(rho, 4),
                                 "ratio_interval": [round(rho_lo, 4), round(rho_hi, 4)]}
                sides[label] = (before, after)
                row.update(paired[label], combined=True,
                           windows_per_week_change_pct=round((rho - 1) * 100, 1),
                           windows_per_week_change_interval_pct=_pct_interval(rho_lo, rho_hi),
                           readings=scope_readings(rho, rho_lo, rho_hi))
            per_account[label] = row
        combined = combine_log_ratios(paired) if paired else None
        for k, w in (combined or {}).get("weights", {}).items():
            per_account[k]["weight"] = w
        readings = scope = rho_boot = None
        wpw_pct = wpw_interval = None
        if combined:
            rho, (rho_lo, rho_hi) = combined["ratio"], combined["interval"]
            readings = scope_readings(rho, rho_lo, rho_hi)
            rho_boot = _bootstrap_ratio(sides, combined["weights"], f"{JOINT_SEED}:{cand['at']}")
            scope = meter_scope(cand.get("joint_fit"), rho, rho_boot)
            wpw_pct, wpw_interval = round((rho - 1) * 100, 1), _pct_interval(rho_lo, rho_hi)

        # The direct measurements the candidate is certified on: the window on the five-hour
        # meter, the weekly limit on the seven-day meter, each account against itself.
        steps = {}
        for label in sorted(weekly_rows):
            start, until = bounds(label)
            rows = weekly_rows[label]
            steps[label] = ([r for r in rows if (start is None or r["start"] >= start)
                             and r["end"] <= at],
                            [r for r in rows if r["start"] >= at
                             and (until is None or r["end"] <= until)])
        tests = {"window_change": window_change(cand),
                 "weekly_change": weekly_change(steps, f"candidate:{cand['at']}")}
        certified = [metric for metric, key, _ in DIRECT_MEASUREMENTS if tests[key]["certified"]]
        measured = [(metric, tests[key]) for metric, key, _ in DIRECT_MEASUREMENTS
                    if tests[key]["change_pct"] is not None]
        # The headline is a certified measurement where there is one (the larger, if both
        # are), else the larger measured one, else the windows-per-week change.
        pool = [(m, t) for m, t in measured if m in certified] or measured
        if pool:
            metric, t = max(pool, key=lambda mt: abs(mt[1]["change_pct"]))
            headline = {"metric": metric, "change_pct": t["change_pct"],
                        "interval_pct": t["interval_pct"]}
            plan_wide = t["plan_wide"]
        elif combined:
            headline = {"metric": "windows_per_week", "change_pct": wpw_pct,
                        "interval_pct": wpw_interval}
            plan_wide = _candidate_plan_wide(cand, headline, paired, sides, rho_boot)
        else:
            headline = {"metric": None, "change_pct": None, "interval_pct": None}
            plan_wide = plan_wide_verdict(None, [], {}, "none")
        measurable = bool(combined or any(t["accounts_combined"] for t in tests.values()))
        withheld = _direct_withheld_reason(at, measurable, tests)
        window = tests["window_change"]
        out.append({
            "family": cand["family"], "at": cand["at"], "at_source": cand.get("at_source"),
            "first_seen_account": cand.get("first_seen_account"),
            "first_seen_stretch_end": cand.get("first_seen_stretch_end"),
            "before_from": cand.get("before_from"), "after_until": cand.get("after_until"),
            "state": change_state(headline["interval_pct"]),
            "scope": scope,
            "plan_wide": plan_wide,
            "window_change": window,
            "weekly_change": tests["weekly_change"],
            "certified_on": certified,
            "windows_per_week_change_pct": wpw_pct,
            "windows_per_week_change_interval_pct": wpw_interval,
            "windows_per_week_change_excludes_no_change": bool(
                wpw_interval and not wpw_interval[0] <= 0 <= wpw_interval[1]),
            "readings": readings,
            # The five-hour window change, measured directly; it steps a window regime only
            # once it is certified (`window_change.certified`) and the candidate applies.
            "change_pct": window["change_pct"], "interval_pct": window["interval_pct"],
            "interval_excludes_no_change": window["interval_excludes_no_change"],
            "windows_per_week_ratio": round(combined["ratio"], 4) if combined else None,
            "windows_per_week_ratio_interval": ([round(x, 4) for x in combined["interval"]]
                                                if combined else None),
            "windows_per_week_ratio_bootstrap_interval": rho_boot,
            "joint_fit": cand.get("joint_fit"),
            "headline": headline,
            "measurable": measurable and at > CUT_AT,
            "applies": withheld is None,
            "withheld_reason": withheld,
            "accounts_combined": sorted(paired),
            "per_account": per_account,
            "announcement": cand.get("announcement"),
            "method": "direct_on_both_meters",
        })
    return {"candidates": out, "unit": "percent",
            "thresholds": {"measured_half_width_pct": CHANGE_MEASURED_HALF_WIDTH_PCT,
                           "min_before": ANNOUNCED_MIN_BEFORE,
                           "rate_plausible_times_list_ratio": list(JOINT_RATE_PLAUSIBLE)},
            "method": WINDOWS_PER_WEEK_METHOD, "plan_wide_method": PLAN_WIDE_METHOD}


CUT_DIRECT_METHOD = (
    "The 14 September weekly change, found by the weekly detector on the windows-per-week "
    "ratio, tested on the two direct measurements exactly as every candidate is "
    "(`five_hour_on_meters`): the five-hour window change, the known-date test at cut_at with "
    "its before side open and its after side to the first candidate after it; and the weekly "
    "limit change on the seven-day steps, each account split at its own certified weekly "
    "step where it has one (the change reaches each account at its own seven-day reset), "
    "else at cut_at. Each is combined by inverse variance and certified under ADR 0001 rule 9 "
    "(plan-wide, interval excluding no change). These are published with the event; the "
    "14 September boundary itself is the weekly detector's certified step and is not "
    "re-decided by them.")


def cut_direct_tests(by_account: dict[str, list[dict]], runs: list[HarnessRun], value,
                     labels: dict[str, str], weekly_rows: dict[str, list[dict]],
                     max20: dict | None, next_at: datetime | None) -> dict:
    """The 14 September weekly change on the direct measurements (CUT_DIRECT_METHOD).

    `value` values stretches and was used for `weekly_rows` (each account's valued seven-day
    steps by label); `next_at` is the first change candidate after CUT_AT, which ends both
    after sides.
    """
    from .weekly_meter import weekly_change

    selected = {name: [st for st in rows if st.get("start") and st.get("end")]
                for name, rows in announced_change_stretches(by_account, runs).items()}
    per_account, _paired, _shares = _known_date_rows(selected, value, CUT_AT, None, next_at,
                                                     labels, list(by_account))
    # An account with no stretch on either side is no part of the test, so a history that
    # lists an account with no readings publishes what a history without it does.
    per_account = {k: v for k, v in per_account.items() if v["n_before"] or v["n_after"]}
    by_acc = (max20 or {}).get("by_account") or {}
    steps = {}
    for label, rows in sorted(weekly_rows.items()):
        if not rows:
            continue
        own_start = own_weekly_step_start(by_acc.get(label))
        before_end = own_weekly_step_end(by_acc.get(label)) or CUT_AT
        after_start = own_start or CUT_AT
        steps[label] = ([r for r in rows if r["end"] <= before_end],
                        [r for r in rows if r["start"] >= after_start
                         and (next_at is None or r["end"] <= next_at)])
    tests = {"window_change": window_change({"per_account": per_account}),
             "weekly_change": weekly_change(steps, f"cut:{_utc(CUT_AT)}")}
    return {"at": _utc(CUT_AT), **tests,
            "certified_on": [m for m, key, _ in DIRECT_MEASUREMENTS if tests[key]["certified"]],
            "method": CUT_DIRECT_METHOD}


def five_hour_meter_events(block: dict | None) -> list[dict]:
    """The meter-measured five-hour candidates that are published: those that `apply`
    (ADR 0001 rule 9: plan-wide, with a headline interval that excludes no change). Only
    these enter `events`, can become `last_change` and reach the email."""
    return [c for c in (block or {}).get("candidates", []) if c.get("applies")]


def five_hour_meter_boundaries(block: dict | None) -> list[dict]:
    """The meter-measured five-hour candidates that open a window regime: every one that is
    `measurable` (a combined reading, dated after the weekly change), published or withheld.
    A withheld one opens a regime that carries the previous window, never one scaled by its
    g (`known_date_changes`). A record without `measurable` reads `applies` for it."""
    return [c for c in (block or {}).get("candidates", [])
            if c.get("measurable", c.get("applies"))]


def _weekly_regime_index(t: datetime, own_end: datetime | None, regimes: list[dict]) -> int:
    """Which window regime a weekly reading ending at `t` belongs to.

    The weekly change reaches each account at its own seven-day reset, so an account with a
    certified step of its own is before that change up to the end of its own earlier regime
    (`own_end`) and after it from then on; one without is split at CUT_AT. Every later
    regime opens at a five-hour change instant, and a reading belongs to it once it ends
    after that instant.
    """
    if (t <= own_end) if own_end is not None else (t < CUT_AT):
        return 0
    return 1 + sum(1 for r in regimes[2:] if t > datetime.fromisoformat(r["from"]))


def _stretch_regime_index(st: dict, regimes: list[dict]) -> int | None:
    """The window regime a stretch lies wholly inside, or None if it spans a boundary."""
    start, end = datetime.fromisoformat(st["start"]), datetime.fromisoformat(st["end"])
    for k, r in enumerate(regimes):
        lo = datetime.fromisoformat(r["from"]) if r["from"] else None
        hi = datetime.fromisoformat(r["until"]) if r["until"] else None
        if (lo is None or start >= lo) and (hi is None or end <= hi):
            return k
    return None


def _pooled_windows_per_week(rows: list[dict]) -> dict:
    """Pooled five-hour over seven-day movement with its rounding interval, or nulls."""
    d7 = sum(r["seven_day_pct"] for r in rows)
    if not rows or not d7:
        return {"value": None, "interval": None, "n": len(rows)}
    side = _side(rows, {"start": "", "end": ""})
    lo, hi = side["rounding_interval"]
    return {"value": side["ratio"], "interval": [lo, hi] if lo and hi else None, "n": len(rows)}


PER_REGIME_METHOD = (
    "The regimes are the window's own (`regimes`): the 14 September weekly change and every "
    "change measured on the meters, so every weekly and every five-hour boundary. "
    "`per_week_regimes` states each regime's week directly on the seven-day meter: the "
    "credits per 1% of the seven-day meter (`weekly_credits_per_pct`), from every one-point "
    "seven-day step of every account lying wholly in the regime "
    "(`weekly_meter.seven_day_steps`, tiled between exact crossings; harness runs, cloud "
    "sessions and steps over stretches the capture check withheld out, the personal account "
    "from 6 September), each step's own tokens valued as `across_cut` "
    "values a stretch, pooled meter-weighted (every account's credits over every account's "
    "seven-day points), times 100, in the window's tokens through the anchor's credits per "
    "token at the published mix (`anchor_credits_per_token`). Its interval resamples each "
    "account's whole UTC days. No fitted coefficient enters it. A step belongs to the regime "
    "it lies in, the weekly change reaching each account at its own certified step. "
    "`windows_per_week` is derived, never measured: the week over the regime's window "
    "(`windows_per_week_source` `weekly_over_window`), its interval the week's ends over the "
    "window's opposite ends. The meter ratio is published beside it for reference only: "
    "`windows_per_week_meters`, five-hour over seven-day points in the same steps, and "
    "`windows_per_week_pooled`, five-hour over seven-day movement pooled over every account's "
    "`weekly_windows.max20.by_window` readings in the regime, with its rounding interval. "
    "A change the tracker withholds (ADR 0001 rule 9 on both direct measurements: `applies` "
    "false) opens no regime here or in `account_regimes`: the regime it would have opened is "
    "pooled with the one before, one row from the earlier regime's start to the later one's "
    "end, its week over every step in both, and its window the newest of the pooled regimes' "
    "windows (they carry one value while nothing measured steps between them). A change "
    "certified on the weekly limit alone opens a row here and leaves the window carried, and "
    "with it the windows per week (`windows_per_week_source` `carried_across_withheld_window`): "
    "a carried window is not measured unchanged, so the week over it would draw the whole "
    "weekly change as a windows-per-week step. Each row's week is a level pooled over the "
    "accounts with steps in it, while a change is certified on each account against itself, "
    "so every row before a change certified on the weekly limit is scaled to make the step "
    "there the certified change (`week_bridge`: the factor and the row's direct value), as the "
    "window's history is bridged; the newest row is its own direct level. "
    "`per_week` is the newest regime's figure, and every family's week moves from its window "
    "the way the anchor's does. "
    "`account_regimes` gives each account's own figures in each regime and never another "
    "account's: its week from its own steps the same way (`per_week`), its window, and "
    "`windows_per_week` its week over its window (carried across a withheld window change as "
    "the pooled row's is). Its window, in anchor units, is the "
    "regime's window times the account's median credits per 1% of the five-hour meter "
    "over its own clean stretches lying wholly in the regime, any family, valued as "
    "`across_cut` values them, over the same median pooled across every account's such "
    "stretches (`n_window` the account's count), so the rates only weigh sessions against "
    "each other inside one regime and the pool's median account sits at the regime's window. "
    "`measured_window` is the account's direct reading beside it: the median tokens per 1% "
    "times 100 of its own pure stretches of the regime's `regime_family` (named on the row "
    "as `measured_family`, counted in `n_measured_window`), with no rate in it. For regimes "
    "stated by one run's cluster, both medians are over the stretches lying wholly in the "
    "run, and every regime of the run carries the same readings. `windows_per_week_meters` "
    "is its own five-hour over seven-day readings, for reference. A figure with no reading "
    "behind it is null, and so is anything derived from it.")


def _week_tokens(level: dict, anchor: float | None) -> tuple[float | None, list[float] | None]:
    """A seven-day level (`weekly_meter.level`) as tokens per week in the window's units."""
    cpp = level.get("credits_per_pct")
    if cpp is None or not anchor:
        return None, None
    iv = level.get("interval")
    return cpp * 100 / anchor, ([x * 100 / anchor for x in iv] if iv else None)


def _over(week: float | None, week_iv: list | None, window: float | None,
          window_iv: list | None) -> tuple[float | None, list[float] | None]:
    """Windows per week derived: the week over the window, the interval the week's ends over
    the window's opposite ends."""
    if week is None or not window:
        return None, None
    iv = ([round(week_iv[0] / window_iv[1], 4), round(week_iv[1] / window_iv[0], 4)]
          if week_iv and window_iv and window_iv[0] and window_iv[1] else None)
    return round(week / window, 4), iv


def _scale_week_row(row: dict, factor: float) -> None:
    """Scale one `per_week_regimes` row's week, and the windows per week derived from it."""
    if row["value"] is not None:
        row["value"] = round(row["value"] * factor)
    if row["interval"]:
        row["interval"] = [round(x * factor) for x in row["interval"]]
    if row["windows_per_week"] is not None:
        row["windows_per_week"] = round(row["windows_per_week"] * factor, 4)
    if row["windows_per_week_interval"]:
        row["windows_per_week_interval"] = [round(x * factor, 4)
                                            for x in row["windows_per_week_interval"]]


def chain_certified_weeks(per_week: list[dict], meters: dict | None) -> None:
    """Make each published step of `per_week_regimes` the figure its change was certified at.

    Each row's week is a pooled level over whichever accounts had steps in it, while a
    change is certified on each account compared with itself (`weekly_change`, ADR 0001
    rule 11). The two differ when the accounts differ either side (a4 starts on
    23 September), and the page drew the level ratio (+32%) beside a headline of the
    certified change (+30%). So, as the window's history is bridged to the meters' change
    (`bridge_rate`), every row before a boundary certified on the weekly limit is scaled so
    the step there is the certified change; the newest row stays its own direct level.
    `week_bridge` records the factor and the row's direct `value`.

    A boundary whose five-hour window change was measured and withheld carries the window
    across it, so the window there is not known to be unchanged: dividing the new week by
    the carried window would draw the whole weekly change as a windows-per-week step. The
    row after such a boundary carries the windows per week of the row before
    (`windows_per_week_source` `carried_across_withheld_window`) instead.

    Edits `per_week` in place, newest boundary first.
    """
    by_at = {datetime.fromisoformat(c["at"]): c for c in (meters or {}).get("candidates", [])
             if c.get("at") and c.get("applies")}
    for i in range(len(per_week) - 1, 0, -1):
        row, prev = per_week[i], per_week[i - 1]
        cand = by_at.get(datetime.fromisoformat(row["from"])) if row["from"] else None
        weekly = (cand or {}).get("weekly_change") or {}
        if (weekly.get("certified") and weekly.get("change_pct") is not None
                and row["value"] and prev["value"]):
            factor = row["value"] / (1 + weekly["change_pct"] / 100) / prev["value"]
            for earlier in per_week[:i]:
                earlier.setdefault("week_bridge", {"direct_value": earlier["value"], "factor": 1.0,
                                                   "at": []})
                earlier["week_bridge"]["factor"] = round(earlier["week_bridge"]["factor"] * factor, 6)
                earlier["week_bridge"]["at"].append(row["from"])
                _scale_week_row(earlier, factor)
    carry_across_withheld_windows(per_week, meters)


def carry_across_withheld_windows(rows: list[dict], meters: dict | None) -> None:
    """Hold windows per week across each boundary whose window change was withheld.

    `rows` are `per_week_regimes` rows or one account's `account_regimes` rows. A row
    opening at a change that applies while the window change it records (`window_change`)
    is not certified takes the windows per week (and interval, where it has one) of the row
    before it, since its window is carried rather than measured (`chain_certified_weeks`).
    A row whose predecessor has no windows per week keeps its own: no step is drawn there.
    """
    by_at = {datetime.fromisoformat(c["at"]): c for c in (meters or {}).get("candidates", [])
             if c.get("at") and c.get("applies")}
    for prev, row in zip(rows, rows[1:]):
        cand = by_at.get(datetime.fromisoformat(row["from"])) if row.get("from") else None
        if cand and cand.get("measurable", True) and prev.get("windows_per_week") is not None \
                and cand.get("window_change") is not None \
                and not cand["window_change"].get("certified"):
            row["windows_per_week"] = prev["windows_per_week"]
            if "windows_per_week_interval" in row:
                row["windows_per_week_interval"] = prev.get("windows_per_week_interval")
            row["windows_per_week_source"] = "carried_across_withheld_window"


def regime_figures(window_credits: dict, window_tokens: dict, max20: dict | None,
                   stretches: dict[str, list[dict]], labels: dict[str, str], value,
                   meters: dict | None = None, credits: dict | None = None,
                   steps: dict[str, list[dict]] | None = None) -> dict:
    """`per_week_regimes`, `account_regimes` and the matching `per_week` for `window_tokens`.

    `stretches` is the across-the-cut selection by account name (`rate_fit_stretches`),
    `value(tokens)` its valuation (`across_cut_value`), which the account windows compare
    accounts with inside one regime and the seven-day steps are valued at (`window_credits`
    is kept in the signature though no figure uses it now), `meters` the
    `five_hour_on_meters` block whose published changes open regimes, and `steps` each
    account's clean seven-day steps by name (`weekly_meter.clean_steps`). Each regime's week
    is measured directly on the seven-day meter and windows per week is derived from it.
    `per_week` replaces the block's own with the newest regime's week. See PER_REGIME_METHOD.
    `credits` is the rate table whose families the stretches are filed under
    (data/prices.json when None).
    """
    from .weekly_meter import level, valued

    credits = load_credits() if credits is None else credits
    # A change with no published figure (ADR 0001 rule 9 on both direct measurements) is no
    # boundary here.
    withheld = {datetime.fromisoformat(c["at"]) for c in (meters or {}).get("candidates", [])
                if c.get("at") and not c.get("applies")}
    regimes = window_tokens["regimes"]
    anchor = window_tokens.get("anchor_credits_per_token")

    def merged(k: int) -> bool:
        return k >= 2 and bool(regimes[k]["from"]) \
            and datetime.fromisoformat(regimes[k]["from"]) in withheld

    # The per-week regimes: the window's, a withheld boundary joining its regime to the one
    # before. `week_of` maps a window regime to its per-week regime.
    weeks: list[list[int]] = []
    for k in range(len(regimes)):
        if weeks and merged(k):
            weeks[-1].append(k)
        else:
            weeks.append([k])
    week_of = {k: i for i, g in enumerate(weeks) for k in g}
    by_window = (max20 or {}).get("by_window") or []
    by_account = (max20 or {}).get("by_account") or {}

    def own_end(label: str) -> datetime | None:
        rs = (by_account.get(label) or {}).get("regimes") or []
        return (datetime.fromisoformat(rs[-2]["end"])
                if own_weekly_step_start(by_account.get(label)) else None)

    rows_in: dict[str, dict[int, list[dict]]] = {}
    for r in by_window:
        label = r.get("account")
        k = _weekly_regime_index(datetime.fromisoformat(r["window_ending"]), own_end(label), regimes)
        rows_in.setdefault(label, {}).setdefault(week_of[k], []).append(r)

    # Every clean seven-day step lying wholly in one per-week regime, valued, by account label.
    steps_in: dict[int, dict[str, list[dict]]] = {}
    for name, label in labels.items():
        for row in valued((steps or {}).get(name, []), value):
            k0 = _weekly_regime_index(row["start"], own_end(label), regimes)
            k1 = _weekly_regime_index(row["end"], own_end(label), regimes)
            if week_of[k0] == week_of[k1]:
                steps_in.setdefault(week_of[k0], {}).setdefault(label, []).append(row)

    per_week, levels = [], []
    for i, group in enumerate(weeks):
        # The group's window is its newest regime's: a withheld boundary carries the window
        # across it, so every regime in a group carries one value.
        k, reg = group[0], regimes[group[-1]]
        start = regimes[k]["from"]
        lvl = level(steps_in.get(i, {}), f"week:{start}")
        levels.append(lvl)
        week, week_iv = _week_tokens(lvl, anchor)
        window = reg["value"]
        wpw, wpw_iv = _over(week, week_iv, window, reg.get("interval"))
        pooled = _pooled_windows_per_week([r for by_i in rows_in.values() for r in by_i.get(i, [])])
        cpp_iv = lvl.get("interval")
        per_week.append({
            "from": start, "until": reg["until"],
            "value": round(week) if week is not None else None,
            "interval": [round(x) for x in week_iv] if week_iv else None,
            "window": window, "per_week_factor": None,
            "per_week_source": "seven_day_meter_direct" if week is not None else None,
            "weekly_credits_per_pct": round(lvl["credits_per_pct"]) if lvl["credits_per_pct"] else None,
            "weekly_credits_per_pct_interval": [round(x) for x in cpp_iv] if cpp_iv else None,
            "seven_day_points": lvl["seven_day_points"], "n_weekly_days": lvl["days"],
            "weekly_accounts": lvl["accounts"],
            "windows_per_week": wpw, "windows_per_week_interval": wpw_iv,
            "windows_per_week_source": "weekly_over_window" if wpw is not None else None,
            "windows_per_week_meters": (round(lvl["windows_per_week_meters"], 4)
                                        if lvl["windows_per_week_meters"] else None),
            "windows_per_week_pooled": pooled["value"],
            "windows_per_week_pooled_interval": pooled["interval"],
            "n_windows_per_week": pooled["n"]})
    chain_certified_weeks(per_week, meters)

    # A run of regimes stated by one cluster (`RUN_CLUSTER_SOURCE`) is one span for the
    # account windows: each account's readings are pooled over the whole run. So is a
    # regime pooled with the one before at a withheld boundary, so a span never splits a
    # per-week regime.
    groups: list[list[int]] = []
    for k, reg in enumerate(regimes):
        if groups and (merged(k) or (reg.get("run_from")
                                     and reg["run_from"] == regimes[groups[-1][0]].get("run_from"))):
            groups[-1].append(k)
        else:
            groups.append([k])
    spans = [{"from": regimes[g[0]]["from"], "until": regimes[g[-1]]["until"]} for g in groups]
    group_of = {k: i for i, g in enumerate(groups) for k in g}

    # Every clean stretch lying wholly in a regime (its run, for a run), any family, as
    # credits per 1% of the five-hour meter: per account, and pooled over the accounts.
    rates: dict[str, dict[int, list[float]]] = {}
    for name in labels:
        for st in stretches.get(name, []):
            if not st.get("start") or not st.get("end") or not st.get("delta_pct"):
                continue
            i = _stretch_regime_index(st, spans)
            credit = value(st.get("tokens") or {}) if i is not None else None
            if credit is not None:
                rates.setdefault(name, {}).setdefault(i, []).append(credit / st["delta_pct"])
    pooled_rates = {i: median([x for by_i in rates.values() for x in by_i.get(i, [])])
                    for i in range(len(groups))
                    if any(by_i.get(i) for by_i in rates.values())}

    accounts = {}
    for name, label in labels.items():
        # The account's own pure regime-family readings: tokens per full window straight off
        # its own stretches, no rate and no pooled figure in them.
        own_windows: dict[int, list[float]] = {}
        for st in stretches.get(name, []):
            if not st.get("start") or not st.get("end") or not st.get("delta_pct"):
                continue
            i = _stretch_regime_index(st, spans)
            if i is None:
                continue
            reg = regimes[groups[i][0]]
            fam = reg.get("regime_family") or reg.get("measured_family") or "opus"
            if pure_family_of(st.get("tokens") or {}, credits) == fam:
                total = sum(class_totals(st["tokens"]).values())
                own_windows.setdefault(i, []).append(total / st["delta_pct"] * 100)
        rows = []
        for i, week in enumerate(weeks):
            k, reg = week[0], regimes[week[-1]]
            readings = own_windows.get(group_of[k], [])
            own = rates.get(name, {}).get(group_of[k], [])
            pool = pooled_rates.get(group_of[k])
            # The account's level relative to the pool, in anchor units: the regime's window
            # times its median credits per 1% over the pooled median.
            window = (round(reg["value"] * median(own) / pool)
                      if own and pool and reg["value"] is not None else None)
            mine = (levels[i].get("per_account") or {}).get(label) or {}
            acct_week, acct_week_iv = _week_tokens(mine, anchor)
            wpw, _ = _over(acct_week, None, window, None)
            meters_wpw = _pooled_windows_per_week(rows_in.get(label, {}).get(i, []))
            cpp_iv = mine.get("interval")
            rows.append({"from": regimes[k]["from"], "until": reg["until"], "window": window,
                         "measured_family": (reg.get("regime_family") or reg.get("measured_family")
                                             or "opus"),
                         "measured_window": round(median(readings)) if readings else None,
                         "per_week": round(acct_week) if acct_week is not None else None,
                         "per_week_interval": ([round(x) for x in acct_week_iv]
                                               if acct_week_iv else None),
                         "weekly_credits_per_pct": (round(mine["credits_per_pct"])
                                                    if mine.get("credits_per_pct") else None),
                         "weekly_credits_per_pct_interval": ([round(x) for x in cpp_iv]
                                                             if cpp_iv else None),
                         "seven_day_points": mine.get("seven_day_points", 0),
                         "n_weekly_days": mine.get("days", 0),
                         "windows_per_week": wpw,
                         "windows_per_week_source": "weekly_over_window" if wpw is not None else None,
                         "windows_per_week_meters": meters_wpw["value"],
                         "n_window": len(own), "n_measured_window": len(readings),
                         "n_wpw": meters_wpw["n"]})
        carry_across_withheld_windows(rows, meters)
        accounts[label] = rows
    newest = per_week[-1]
    all_fig = window_tokens["all"]
    week = per_week_block(all_fig, window_tokens["per_family"], newest["windows_per_week"],
                          newest["windows_per_week_interval"],
                          f"per_week_regimes, newest regime ({newest['windows_per_week_source']})")
    if newest["value"] is not None and all_fig.get("value"):
        # The week is the newest regime's own direct figure, so every family moves from its
        # window the way the anchor's week moved from its own: value by value, each interval
        # end by the same end.
        lo_w, hi_w = all_fig.get("interval") or (None, None)

        def scaled(fig: dict) -> dict:
            if fig.get("value") is None and not fig.get("interval"):
                return fig
            value = (round(fig["value"] / all_fig["value"] * newest["value"])
                     if fig.get("value") is not None else None)
            interval = ([round(fig["interval"][0] / lo_w * newest["interval"][0]),
                         round(fig["interval"][1] / hi_w * newest["interval"][1])]
                        if fig.get("interval") and newest["interval"] and lo_w and hi_w else None)
            return dict(fig, value=value, interval=interval)
        week["all"] = dict(week["all"], value=newest["value"], interval=newest["interval"],
                           status=None)
        week["status"] = None
        week["per_family"] = {name: {"all": scaled(row["all"])}
                              for name, row in window_tokens["per_family"].items()}
    return {"per_week_regimes": per_week, "account_regimes": accounts, "per_week": week,
            "per_regime_method": PER_REGIME_METHOD}
