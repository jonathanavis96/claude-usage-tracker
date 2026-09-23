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
held Fable rate only where no pooled fit exists. `fable_interval` states the pooled fit's
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

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

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
    best, best_len = None, -1
    for name, row in credits["per_family"].items():
        needle = row.get("matches", name)
        if needle in lowered and len(needle) > best_len:
            best, best_len = name, len(needle)
    return best


def rates(fam: str, credits: dict) -> tuple[float, float] | None:
    """(input, output) credits per token for a family, or None when it has no single rate.

    Fable has none. Its rate is solved from the stretches at publish time
    (`fable_interval`) and published as an interval, because the data bound its input
    rate only loosely and do not separate its output ratio at all.
    """
    row = credits["per_family"][fam]
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
    detail = {k: row[k] for k in ("n_fits", "agree", "per_fit", "times_opus",
                                  "times_opus_interval", "why", "max_share_of_a_clean_stretch")
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
        if fam is None:
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
    accounts in `exempt` skip that test. `None` skips it everywhere.
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
    per_account = (across or {}).get("per_account") or {}
    qualifying = {label: row["change_pct"] for label, row in per_account.items()
                  if row.get("change_pct") is not None
                  and (row.get("n_with_capture") or 0) > 0
                  and (row.get("n_before") or 0) >= FIVE_HOUR_MIN_SIDE
                  and (row.get("n_after") or 0) >= FIVE_HOUR_MIN_SIDE}
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


def window_credits(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
                   fam: str = "opus", weight: float | None = None,
                   five_hour_pct: float | None = None) -> dict:
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
    chosen_name, factor, current_source = current_cluster_rule(
        len(sides["before"]), len(sides["after"]), five_hour_pct)
    chosen = sorted(sides[chosen_name])
    per_pct = median(chosen) * factor if chosen else None

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
        "interval": ([round(min(chosen) * 100 * factor), round(max(chosen) * 100 * factor)]
                     if chosen else None),
        "n": len(pooled),
        "cut_at": CUT_AT.isoformat(),
        "before": side_figure(sides["before"]),
        "after": side_figure(sides["after"]),
        "current_source": current_source if pooled else None,
        "five_hour_window_pct": five_hour_pct,
        "current_method": CURRENT_METHOD,
        "accounts": accounts,
        "pure_family": fam,
        "cache_read_weight": weight,
        "cache_read_weight_range": credits.get("cache_read_weight_range"),
        "method": method,
        "status": None if pooled else f"no capture-accepted pure-{fam} stretch in the history files",
        "derivation": "credits",
    }


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
    produced; "envelope" is a family whose fits disagree, so only an interval survives;
    "none" is a family there is nothing at all to measure a rate from.
    """
    if rate.anchor:
        return "anchor"
    if rate.input is not None:
        return "measured"
    if rate.input_interval:
        return "envelope"
    return "none"


def _conversion_sentence(anchor_fam: str, fam: str, source: str) -> str | None:
    """How one family's window tokens were got from the anchor's, or None where it was not."""
    if source == "anchor" or source == "none":
        return None
    over = (f"the measured {fam.capitalize()} input rate" if source == "measured"
            else f"the {fam.capitalize()} input-rate envelope")
    tail = ("" if source == "measured" else
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


def window_tokens(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
                  model_rates: dict | None = None, *, windows_per_week: float | None = None,
                  windows_per_week_interval: list | None = None,
                  windows_per_week_source: str = "weekly_windows current, max20, newest regime",
                  fam: str = "opus", weight: float | None = None,
                  five_hour_pct: float | None = None) -> dict:
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
    shape from (below), and each class states its own two sides beside it.

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
    chosen_name, factor, current_source = current_cluster_rule(
        n_side["before"], n_side["after"], five_hour_pct)

    def figure(side: str, cls: str | None = None, scale: float = 1.0) -> dict:
        """One side's readings as the published figure, optionally scaled to now."""
        return _figure_from({label: [v * scale for v in readings(account_rows, cls)]
                             for label, account_rows in sides[side].items()})

    def both_sides(cls: str | None = None) -> dict:
        """The `before` and `after` sub-figures one published figure carries."""
        return {side: dict(figure(side, cls), n=n_side[side]) for side in ("before", "after")}

    all_figure = dict(figure(chosen_name, scale=factor),
                      status=None if pooled_rows else
                      f"no capture-accepted pure-{fam} stretch in the history files")
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

    families = {}
    for name in credits["per_family"]:
        rate = family_rate(name, credits, model_rates)
        source = _rate_source(rate)
        families[name] = {
            "all": _family_window_tokens(all_figure, anchor_per_token, rate, shares, weight),
            "rate_source": source,
            "conversion": _conversion_sentence(fam, name, source),
        }

    windows_figure = {"value": windows_per_week,
                      "interval": list(windows_per_week_interval) if windows_per_week_interval else None,
                      "source": windows_per_week_source}
    per_week = {
        "windows_per_week": windows_figure,
        "all": _times_windows(all_figure, windows_per_week, windows_per_week_interval),
        "per_family": {name: {"all": _times_windows(row["all"], windows_per_week,
                                                    windows_per_week_interval)}
                       for name, row in families.items()},
        "status": None if windows_per_week is not None else
                  "no measured windows per week to multiply by",
    }
    method = (f"median over the pure-{fam} clean stretches of the tokens the stretch carried per 1% "
              f"of the five-hour meter, times 100, taken per token class and over all four classes "
              f"together -- `all` over the current side of the cut alone (`current_method`), "
              f"`per_class` over the whole cluster, and both sides published beside each. "
              f"The interval is the spread of the readings themselves -- per account its "
              f"own lowest and highest per-stretch value, pooled the union of the account intervals "
              f"-- and not a confidence interval. No credit rate and no class weight enters the "
              f"pure-{fam} figure: it is the stretch's own token counts over its own meter movement. "
              f"Every other family is a conversion of it at the two families' rates (`conversion`), "
              f"and a family whose rate is a status sentence publishes no number.")
    return {
        "derivation": "credits",
        "as_of": newest_end(pooled_rows),
        "method": method,
        "current_method": CURRENT_METHOD,
        "selection": selection_sentence(),
        "n": len(pooled_rows),
        "cut_at": CUT_AT.isoformat(),
        # The all-classes figure's own two sides; every `per_class` entry carries its own.
        **both_sides(),
        "current_source": current_source if pooled_rows else None,
        "five_hour_window_pct": five_hour_pct,
        "accounts": accounts,
        "per_class": per_class,
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


def across_cut(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
               weight: float | None = None, model_rates: dict | None = None) -> dict:
    """Each account's five-hour window in credits before and after the announced change.

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
    weight = cache_read_weight(credits) if weight is None else weight
    held = credits.get("across_cut_fable_rate") or {}
    fable_in, fable_out = _rate(held.get("input")), _rate(held.get("output"))
    out = {}
    for name, label in labels.items():
        sides: dict[str, list[float]] = {"before": [], "after": []}
        for st in clean.get(name, []):
            if fit is not None:
                total = _pooled_stretch_credits(st["tokens"], credits, fit)
                if total is None:
                    continue
            else:
                priced = price_tokens(st["tokens"], credits, weight)
                if not priced.priced:
                    continue
                if (priced.fable_input or priced.fable_output) and fable_out is None:
                    continue
                total = (priced.known + priced.fable_input * (fable_in or 0)
                         + priced.fable_output * (fable_out or 0))
            if not st.get("start"):
                continue  # unplaceable: no stamp to put it on one side of the change
            side = "before" if datetime.fromisoformat(st["start"]) < CUT_AT else "after"
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
        "rates_used": rates_used,
        "fable_rate_held": {"input": fable_in, "output": fable_out,
                            "why": held.get("why")} if fit is None and fable_out is not None else None,
        "method": method,
    }


def windows_per_week_ratio_note(weekly: dict) -> dict | None:
    """What the pooled windows-per-week ratio (five-hour movement over seven-day movement)
    actually identifies across the certified change, and nothing more.

    A fall in this ratio is one equation in two unknowns: the weekly cap and the
    five-hour window could each have moved, in any combination that reproduces the
    observed fall. This reports the fall itself, from the exact rows behind the last
    two certified regimes (`weekly['max20']['regimes']`), plus three of the many splits
    that reproduce it -- the weekly cap absorbing all of it, the five-hour window
    absorbing all of it, and the announced 17% weekly cut absorbing the rest as an
    implied five-hour rise. None of the three is claimed; they are worked examples of
    the same one-equation-two-unknowns fact, ported from the Codex review of commit
    447b926 (2026-09-20), which also retracted the cross-account-spread argument this
    replaces (see `ACROSS_CUT_UNRESOLVED`).
    """
    regimes = weekly["max20"]["regimes"]
    if len(regimes) < 2:
        return None
    by_window = weekly["max20"]["by_window"]

    def sums(regime: dict, exclude_at: datetime | None = None) -> tuple[int, float, float] | None:
        # `by_window` is pooled across accounts, and each account keeps its own window
        # cadence, so two different accounts' rows can share a `window_ending` instant.
        # Both bounds are inclusive so a row inside a regime's own span is never dropped,
        # but a row could sit exactly on the instant where one regime ends and the next
        # begins, and each regime's own inclusive bounds would then both claim it.
        # `exclude_at` names that one instant (the earlier regime's own `end`) so it is
        # dropped here, from the later regime only -- never a whole boundary side, which
        # would also drop the later regime's own first window whenever that window's own
        # timestamp happens to equal its own `start`.
        lo, hi = datetime.fromisoformat(regime["start"]), datetime.fromisoformat(regime["end"])
        rows = [r for r in by_window
                if lo <= datetime.fromisoformat(r["window_ending"]) <= hi
                and datetime.fromisoformat(r["window_ending"]) != exclude_at]
        d7 = sum(r["seven_day_pct"] for r in rows)
        return (len(rows), sum(r["five_hour_pct"] for r in rows), d7) if d7 else None

    before = sums(regimes[-2])
    after = sums(regimes[-1], exclude_at=datetime.fromisoformat(regimes[-2]["end"]))
    if not before or not after:
        return None
    n_before, d5_before, d7_before = before
    n_after, d5_after, d7_after = after
    ratio_before, ratio_after = d5_before / d7_before, d5_after / d7_after
    rho = ratio_after / ratio_before  # (weekly_after/weekly_before) / (five_hour_after/five_hour_before)
    fall_pct = round((1 - rho) * 100, 2)
    five_hour_only_pct = round((1 / rho - 1) * 100, 2)
    announced_weekly_pct = 17.0
    announced_five_hour_pct = round(((1 - announced_weekly_pct / 100) / rho - 1) * 100, 2)
    return {
        "before": {"n_windows": n_before, "sum_five_hour_pct": round(d5_before, 1),
                  "sum_seven_day_pct": round(d7_before, 1), "ratio": round(ratio_before, 4),
                  "start": regimes[-2]["start"], "end": regimes[-2]["end"]},
        "after": {"n_windows": n_after, "sum_five_hour_pct": round(d5_after, 1),
                 "sum_seven_day_pct": round(d7_after, 1), "ratio": round(ratio_after, 4),
                 "start": regimes[-1]["start"], "end": regimes[-1]["end"]},
        "ratio_fell_pct": fall_pct,
        "consistent_with": [
            {"description": "the weekly cap falls by the whole measured amount, the five-hour "
                            "window unchanged",
             "weekly_cap_change_pct": -fall_pct, "five_hour_window_change_pct": 0.0},
            {"description": "the five-hour window rises, the weekly cap unchanged",
             "weekly_cap_change_pct": 0.0, "five_hour_window_change_pct": five_hour_only_pct},
            {"description": "the announced 17% weekly cut, with the rest of the measured fall "
                            "an implied five-hour rise",
             "weekly_cap_change_pct": -announced_weekly_pct,
             "five_hour_window_change_pct": announced_five_hour_pct},
        ],
        "unit": "percent",
        "method": ("the pooled ratio of five-hour to seven-day meter movement over every window in "
                   "each of the last two certified regimes (tracker/detect.py weighted_regimes), "
                   "before divided by after. The fall is one equation in the ratio of the two "
                   "meters' own budget changes; `consistent_with` lists example splits that "
                   "reproduce it exactly, not measurements of which one moved."),
    }
