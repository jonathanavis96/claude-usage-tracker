"""Credit arithmetic over the passive stretch records, shared by every consumer of it.

The meter does not work in dollars. It works in an internal unit -- credits --
charged per token at a small rational rate per model, and the five-hour window is a
credit budget. `data/prices.json`'s `_credits` block holds the rates; nothing here
carries a rate of its own, so a rate only ever changes in one place.

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
#: Every run the tracker's own instruments made, one JSON object per line, written by
#: tools/harness_runs.py. Anchored to the checkout, not the working directory, because the
#: publisher runs from cron and the report tools run from anywhere.
HARNESS_RUNS_PATH = Path(__file__).resolve().parent.parent / "history" / "harness-runs.jsonl"

#: Below this much meter movement a stretch is mostly whole-percent rounding.
MIN_DELTA_PCT = 3.0
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

    Every Opus version prices as Opus, which is why the table is keyed by family
    rather than by model id.
    """
    lowered = model.lower()
    for name, row in credits["per_family"].items():
        if row.get("matches", name) in lowered:
            return name
    return None


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
                         "credits_per_pct": priced.known / st["delta_pct"]})
        out[account] = sorted(rows, key=lambda r: r["credits_per_pct"])
    return out


def window_credits(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
                   fam: str = "opus", weight: float | None = None) -> dict:
    """The five-hour window in credits, from the pure-`fam` cluster pooled across accounts.

    `value` is a full window (100% of the meter), so it is the median credits per 1%
    times 100; `credits_per_pct` keeps the per-percent median the median was taken
    of. `interval` is the cluster's own min to max, which is the spread of the
    readings and not a confidence interval -- there is no error model here, only
    eleven readings of the same quantity.

    `accounts` reports every watched account by its published label, including the
    ones that contributed nothing, so a reader can see that a cluster came from two
    accounts of three and why the third is absent. Account names are never published.
    """
    weight = cache_read_weight(credits) if weight is None else weight
    per_account = pure_family_rows(clean, credits, fam, weight)
    pooled = sorted(r["credits_per_pct"] for rows in per_account.values() for r in rows)
    accounts = {}
    for name, label in labels.items():
        rows = per_account.get(name, [])
        values = [r["credits_per_pct"] for r in rows]
        accounts[label] = {
            "n": len(values),
            "value": round(median(values) * 100) if values else None,
            "interval": [round(values[0] * 100), round(values[-1] * 100)] if values else None,
        }
    method = (f"median credits per 1% of the five-hour meter over the pure-{fam} stretches of every "
              f"watched account's passive stretch file, times 100. A stretch "
              f"counts when it moved the meter at least {MIN_DELTA_PCT:g}%, carries tokens, does not "
              f"overlap one of the tracker's own runs on its own account (a probe row or the "
              f"effort-matrix run), and reads capture_status 'accepted'. Every token in a "
              f"pure-{fam} stretch is priced at a rate data/prices.json publishes, so the figure "
              f"carries no fitted parameter. Cache writes at the input rate, cache reads at "
              f"{weight:g} of it.")
    return {
        "value": round(median(pooled) * 100) if pooled else None,
        "credits_per_pct": round(median(pooled)) if pooled else None,
        "interval": [round(pooled[0] * 100), round(pooled[-1] * 100)] if pooled else None,
        "n": len(pooled),
        "accounts": accounts,
        "pure_family": fam,
        "cache_read_weight": weight,
        "cache_read_weight_range": credits.get("cache_read_weight_range"),
        "method": method,
        "status": None if pooled else f"no capture-accepted pure-{fam} stretch in the history files",
        "derivation": "credits",
    }


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
                   labels: dict[str, str], weight: float | None = None) -> dict:
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


def across_cut(clean: dict[str, list[dict]], credits: dict, labels: dict[str, str],
               weight: float | None = None) -> dict:
    """Each account's five-hour window in credits before and after the announced change.

    Every clean stretch is priced, not only the pure ones, so there is enough on both
    sides of 14 September to compare -- which means Fable's tokens need a rate, and
    Fable has only an interval. One rate is held constant on both sides
    (`across_cut_fable_rate` in data/prices.json): the comparison is of the two sides
    against each other, and a rate that is the same in both medians cannot move their
    ratio much. The level itself is not a claim; `window_credits` is the claim.

    **This does not resolve whether the five-hour window moved, and the block says so.**
    One account reads about 9% higher after the change, but two accounts on the same
    plan differ from each other by more than that after it, so the account-to-account
    spread is larger than the pre/post move and neither direction can be read from it.
    `spread_after_pct` publishes that comparison beside the per-account rows, and
    `resolved` is false. An earlier draft of the reconciliation called the window flat
    within 4%; that came from gating on `status` instead of `capture_status`, which let
    42 unaccounted stretches into the comparison, and the gate on PR #64 caught it.
    """
    weight = cache_read_weight(credits) if weight is None else weight
    held = credits.get("across_cut_fable_rate") or {}
    fable_in, fable_out = _rate(held.get("input")), _rate(held.get("output"))
    out = {}
    for name, label in labels.items():
        sides: dict[str, list[float]] = {"before": [], "after": []}
        for st in clean.get(name, []):
            priced = price_tokens(st["tokens"], credits, weight)
            if not priced.priced:
                continue
            if (priced.fable_input or priced.fable_output) and fable_out is None:
                continue
            total = priced.known + priced.fable_input * (fable_in or 0) + priced.fable_output * (fable_out or 0)
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
    afters = [row["after"] for row in out.values() if row["after"]]
    moves = [abs(row["change_pct"]) for row in out.values() if row["change_pct"] is not None]
    spread = round((max(afters) - min(afters)) / min(afters) * 100, 1) if len(afters) > 1 else None
    if spread is None or not moves:
        unresolved = "not enough accounts with readings on both sides to compare"
    elif spread >= max(moves):
        unresolved = (f"the accounts differ from each other by {spread:g}% after the change, more "
                      f"than the largest per-account move across it ({max(moves):g}%), so the "
                      f"five-hour and weekly meters cannot be separated from these stretches")
    else:
        unresolved = None
    return {
        "per_account": out,
        "resolved": unresolved is None,
        "unresolved": unresolved,
        "spread_after_pct": spread,
        "largest_move_pct": max(moves) if moves else None,
        "unit": "credits per 1% of the five-hour meter",
        "cut_at": CUT_AT.isoformat(),
        "fable_rate_held": {"input": fable_in, "output": fable_out,
                            "why": held.get("why")} if fable_out is not None else None,
        "method": ("median credits per 1% over every clean stretch of the account, split on the "
                   "announced change; one Fable rate held constant on both sides so the two "
                   "medians are comparable to each other. An account whose n_with_capture is 0 "
                   "has no usable capture column, so its meter movement includes work this host "
                   "never saw and its level reads low; the comparison of its own two sides is "
                   "still its own."),
    }
