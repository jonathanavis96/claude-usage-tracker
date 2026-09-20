"""Price the measured stretches in credits per 1%, per account, per era, per dominant model.

Read-only arithmetic over history/gs-passive.json and history/masterrig-passive.json. It
spends no allowance, runs no probe and writes nothing but --json. There is no fitting and
no search here: every rate is either given on the command line or taken from the table
below, and every number printed is a count or an order statistic of
`credits / delta_pct`.

Where the rates come from. docs/reference-2026-09-20-shellac-credits-model.md (branch
`step-vs-trend-finding`) records them from
https://she-llac.com/claude-limits: the meter does not work in dollars but in an internal
unit, `ceil(input_tokens x input_rate + output_tokens x output_rate)`, with small integer
rates per model -- Haiku 2/15 and 10/15, Sonnet 6/15 and 30/15, Opus 10/15 and 50/15,
output five times input throughout. On a subscription cache reads are free and cache
writes are charged at the plain input rate, not the API's 1.25x premium. **That table is a
reference, not a source of truth**: it is one person's reconstruction from unrounded
doubles in streaming responses, it does not reconcile with our own stretches (they imply a
five-hour allowance two to two-and-a-half times the article's published Max 20x figure),
and nothing here depends on it being right. It is the hypothesis this report measures
against.

Two rates in it are ours, not the article's, and both are marked provisional wherever they
are printed:

- **Fable** is absent from the article. `--fable-input` (default 25/15, i.e. 2.5x Opus)
  and `--fable-output-ratio` (default 3, against the 5 every other model uses) come from
  the reference doc's own fit. The last section of this report solves the input rate
  afresh, per Fable-heavy stretch, from the account's pure-Opus level -- that is the one
  place a number here is derived rather than assumed.
- **Cache reads** are free in the article. `--cache-read-weight` (default 0.015) is the
  residual the reference doc measured, about 1.5% of the input rate, small enough to be an
  artefact of whole-percent quantisation or of tokens the transcripts never captured.
  `--cache-read-weight 0` reads the article literally.

What is left out, and why it is not a date rule. A stretch is excluded when it overlaps one
of the tracker's own runs on the same account: a probe row in history/probes.jsonl, spanning
[ts, ts + elapsed_s], or the effort-matrix run recorded in data/effort_matrix.json's `_meta`
(2026-09-09T11:28:37Z to 14:53:22Z, on jwork). There the tracker was driving the account, so
the meter moved on the instrument's own work and the stretch's percent is not a reading of a
session's tokens.

The six jwork stretches from 12:20 to 14:58 on 2026-09-09 that read 0.28 to 0.53 capture,
against about 1.0 either side, were first put down to a probe. They were not: probes.jsonl
holds two rows that day and both are Dave's, both finished before 00:30Z. The effort matrix
brackets them. Keying on the runs rather than on the date catches that, also catches three
long jwork stretches a probe cut through on 6-8 and 14-15 September, and leaves the rest of
9 September where it belongs -- in the series. `--keep-harness-runs` turns the exclusion off
so its effect can be seen; the excluded stretches are always printed with the run that
caused them.

Eras are the plan's, by the stretch's end in UTC: `5x` before 2026-08-14T12:00Z, `20x` to
2026-09-14T12:00Z, `20x-cut` after. (tracker/passive.py puts the August seam at 17:00Z,
where the meter's own weekly-to-window ratio moved; the difference covers one afternoon
and is noted rather than reconciled here.)

What a reader must not forget about masterrig: its meter counts the whole account -- web,
phone, every other machine -- while only that host's transcripts are read, so its rows can
divide real meter movement by tokens that are only part of what moved it. Its credits per
1% therefore read low where the phantom is large. Each stretch's own `capture` is in
history/masterrig-passive.json; `--min-capture` filters on it.

    python3 tools/credits_report.py
    python3 tools/credits_report.py --cache-read-weight 0
    python3 tools/credits_report.py --json /tmp/credit-rows.json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

#: Credits per token as (input, output), from the article's table. The key is matched as a
#: substring of the model id, so every Opus version is priced as Opus.
PUBLISHED_RATES = {"haiku": (2 / 15, 10 / 15), "sonnet": (6 / 15, 30 / 15), "opus": (10 / 15, 50 / 15)}
FABLE_INPUT = 25 / 15          # provisional: ours, not the article's
FABLE_OUTPUT_RATIO = 3         # provisional: ours; every published model uses 5
CACHE_READ_WEIGHT = 0.015      # provisional: the article says 0
OPUS_INPUT = PUBLISHED_RATES["opus"][0]

ERA_20X_AT = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
ERA_CUT_AT = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
#: A stretch is called one model's when that model holds more than this share of its credits.
DOMINANT = 0.90
#: A stretch is Fable-heavy when Fable holds more than this share of its raw tokens.
FABLE_HEAVY = 0.50
#: Token classes, and the one that is a subset of another. cache_write_1h is already inside
#: cache_write (tracker/turns.py Turn), and the credits model has no one-hour premium to
#: add, so counting it again would charge those tokens twice.
CLASSES = ("input", "output", "cache_read", "cache_write")

REPORTS = {"gs": Path("history/gs-passive.json"), "masterrig": Path("history/masterrig-passive.json")}
PROBES = Path("history/probes.jsonl")
EFFORT_MATRIX = Path("data/effort_matrix.json")
#: data/effort_matrix.json's `_meta` records `started` and `finished` but not which account
#: the matrix ran on. It ran on jwork (2026-09-20 review), so the account is named here
#: rather than read. If a later matrix runs elsewhere, this is the line to change.
EFFORT_MATRIX_ACCOUNT = "jwork"


@dataclass(frozen=True)
class HarnessRun:
    """A span in which the tracker was driving the account itself, not observing it."""
    account: str
    start: datetime
    end: datetime
    reason: str


def harness_runs(probes: Path = PROBES, effort_matrix: Path = EFFORT_MATRIX) -> list[HarnessRun]:
    """Every span the tracker's own instruments occupied, per account.

    A probe row's `ts` is the run's start (tracker/probe.py returns ProbeResult(start, ...))
    and `elapsed_s` its duration, so the row occupies [ts, ts + elapsed_s] on its own
    `account`. The effort matrix records its own `started` and `finished`.

    Why this and not a date rule. The six jwork stretches of 2026-09-09 that read 0.28 to
    0.53 capture against about 1.0 either side were first blamed on a probe; there is no
    jwork probe row that day (history/probes.jsonl has two, both Dave's, both before
    00:30Z). The effort-matrix run brackets them: 11:28:37Z to 14:53:22Z. Keying on the runs
    themselves catches that, catches the probe-overlapping stretches a date rule would miss,
    and leaves the rest of 9 September in the series -- which a blanket date rule would not.
    """
    out: list[HarnessRun] = []
    if probes.exists():
        for line in probes.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not row.get("account") or not row.get("ts"):
                continue
            start = datetime.fromisoformat(row["ts"])
            out.append(HarnessRun(row["account"], start,
                                  start + timedelta(seconds=row.get("elapsed_s") or 0),
                                  f"probe {row.get('model', '?')}/{row.get('effort', '?')} "
                                  f"from {row['ts'][:19]}Z"))
    if effort_matrix.exists():
        meta = json.loads(effort_matrix.read_text(encoding="utf-8")).get("_meta") or {}
        if meta.get("started") and meta.get("finished"):
            out.append(HarnessRun(EFFORT_MATRIX_ACCOUNT, datetime.fromisoformat(meta["started"]),
                                  datetime.fromisoformat(meta["finished"]),
                                  f"effort matrix {meta['started'][:19]}Z to {meta['finished'][:19]}Z"))
    return sorted(out, key=lambda r: (r.account, r.start))


def overlapping_run(runs: list[HarnessRun], account: str, start: datetime,
                    end: datetime) -> HarnessRun | None:
    """The first run of this account whose span overlaps [start, end], or None.

    Half-open on both sides, so a stretch that only meets a run at an instant is kept.
    """
    for run in runs:
        if run.account == account and start < run.end and run.start < end:
            return run
    return None


def family(model: str) -> str | None:
    """`haiku`, `sonnet`, `opus`, `fable`, or None for an id no rate covers."""
    m = model.lower()
    for key in PUBLISHED_RATES:
        if key in m:
            return key
    return "fable" if "fable" in m else None


def rates(fam: str, fable_input: float, fable_ratio: float) -> tuple[float, float]:
    if fam == "fable":
        return fable_input, fable_input * fable_ratio
    return PUBLISHED_RATES[fam]


def chargeable(tok: dict, cache_read_weight: float) -> tuple[float, float]:
    """(input-rate tokens, output-rate tokens) of one model's bundle.

    Cache writes join the input side at the plain input rate (the article's correction to
    the API's 1.25x premium); cache reads join it at `cache_read_weight` of that rate.
    """
    at_input = (tok.get("input", 0) + tok.get("cache_write", 0)
                + tok.get("cache_read", 0) * cache_read_weight)
    return at_input, tok.get("output", 0)


def raw_tokens(tok: dict) -> int:
    return sum(tok.get(c, 0) for c in CLASSES)


def era(end: datetime) -> str:
    if end < ERA_20X_AT:
        return "5x"
    return "20x" if end < ERA_CUT_AT else "20x-cut"


def short(model: str) -> str:
    return model.removeprefix("claude-")


def load_rows(paths: dict[str, Path], cache_read_weight: float, fable_input: float,
              fable_ratio: float, min_capture: float | None = None,
              runs: list[HarnessRun] | None = None) -> tuple[list[dict], dict, list[dict]]:
    """One row per priceable stretch, a count of what was left out and why, and the harness rows.

    A stretch is left out when it overlaps one of `runs` on its own account (the tracker was
    driving the account, so its percent is not a measurement of a session's tokens), when it
    has no tokens at all (the meter moved and the host saw nothing of it), when it has not
    moved, when the rates value its tokens at nothing, when a model carrying tokens has no
    rate, or when `min_capture` is given and the stretch's own capture is below it or absent.

    The harness-run test comes first and is reported stretch by stretch, because it is a
    statement about where the data came from rather than about whether it can be priced: a
    stretch cut by a probe or by the effort matrix is not evidence about ordinary use however
    cleanly it prices.
    """
    rows: list[dict] = []
    excluded: list[dict] = []
    runs = list(runs or [])
    skipped = {"harness_run": 0, "no_tokens": 0, "no_movement": 0, "no_credits": 0,
               "unknown_model": 0, "below_min_capture": 0}
    unknown: set[str] = set()
    for path in paths.values():
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, body in data.get("accounts", {}).items():
            for st in body.get("stretches", []):
                run = overlapping_run(runs, name, datetime.fromisoformat(st["start"]),
                                      datetime.fromisoformat(st["end"]))
                if run is not None:
                    skipped["harness_run"] += 1
                    excluded.append({"account": name, "start": st["start"], "end": st["end"],
                                     "delta_pct": st.get("delta_pct"), "capture": st.get("capture"),
                                     "status": st.get("status"), "reason": run.reason})
                    continue
                tokens = {m: t for m, t in st.get("tokens", {}).items() if raw_tokens(t)}
                if not tokens:
                    skipped["no_tokens"] += 1
                    continue
                if not st.get("delta_pct"):
                    skipped["no_movement"] += 1
                    continue
                bad = [m for m in tokens if family(m) is None]
                if bad:
                    skipped["unknown_model"] += 1
                    unknown.update(bad)
                    continue
                if min_capture is not None and (st.get("capture") is None or st["capture"] < min_capture):
                    skipped["below_min_capture"] += 1
                    continue
                per_model, per_family = {}, {}
                for model, tok in tokens.items():
                    fam = family(model)
                    at_in, at_out = chargeable(tok, cache_read_weight)
                    rate_in, rate_out = rates(fam, fable_input, fable_ratio)
                    c = at_in * rate_in + at_out * rate_out
                    per_model[model] = c
                    per_family[fam] = per_family.get(fam, 0.0) + c
                total = sum(per_model.values())
                if total <= 0:
                    # Tokens the rates value at nothing -- at --cache-read-weight 0, a
                    # stretch of nothing but cache reads. Its rate would be a flat zero,
                    # which is not a level to take a median with, so it is counted and
                    # left out rather than pooled. There are none in the real reports.
                    skipped["no_credits"] += 1
                    continue
                top = max(per_model, key=per_model.get)
                raw = sum(raw_tokens(t) for t in tokens.values())
                rows.append({
                    "account": name, "start": st["start"], "end": st["end"],
                    "era": era(datetime.fromisoformat(st["end"]).astimezone(timezone.utc)),
                    "delta_pct": st["delta_pct"], "credits": total,
                    "credits_per_pct": total / st["delta_pct"],
                    "dominant": short(top) if per_model[top] / total > DOMINANT else "mixed",
                    "families": {f: round(c / total, 6) for f, c in sorted(per_family.items())},
                    "pure": next(iter(per_family)) if len(per_family) == 1 else None,
                    "fable_token_share": sum(raw_tokens(t) for m, t in tokens.items()
                                             if family(m) == "fable") / raw,
                    "status": st.get("status"), "capture": st.get("capture"),
                    "reset_verified": st.get("reset_verified"),
                    "credits_by_model": {short(m): round(c, 1) for m, c in sorted(per_model.items())},
                    "tokens": tokens,
                })
    rows.sort(key=lambda r: (r["account"], r["end"]))
    excluded.sort(key=lambda r: (r["account"], r["end"]))
    skipped["unknown_models"] = sorted(unknown)
    return rows, skipped, excluded


def quartiles(values: list[float]) -> tuple[float, float, float]:
    """(p25, median, p75) by linear interpolation on the sorted sample; a single value is all three."""
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0], xs[0], xs[0]

    def q(p: float) -> float:
        h = (len(xs) - 1) * p
        lo = int(h)
        hi = min(lo + 1, len(xs) - 1)
        return xs[lo] + (h - lo) * (xs[hi] - xs[lo])

    return q(0.25), median(xs), q(0.75)


def summarise(rows: list[dict]) -> dict:
    p25, med, p75 = quartiles([r["credits_per_pct"] for r in rows])
    return {"n": len(rows), "median": med, "p25": p25, "p75": p75}


def _group(rows: list[dict]) -> list[tuple[str, str, str, dict]]:
    keyed: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        keyed.setdefault((r["account"], r["era"], r["dominant"]), []).append(r)
    order = {"5x": 0, "20x": 1, "20x-cut": 2}
    return [(a, e, g, summarise(rs))
            for (a, e, g), rs in sorted(keyed.items(), key=lambda kv: (kv[0][0], order[kv[0][1]], kv[0][2]))]


def pure_clusters(rows: list[dict], fam: str) -> dict[str, dict]:
    """Per account, the stretches whose credits come from `fam` alone."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r["pure"] == fam:
            by.setdefault(r["account"], []).append(r)
    return {a: summarise(rs) for a, rs in sorted(by.items())}


def solve_fable_input(rows: list[dict], opus: dict[str, dict], fable_ratio: float,
                      cache_read_weight: float) -> list[dict]:
    """The Fable input rate each Fable-heavy stretch implies, given the account's pure-Opus level.

    Take the account's pure-Opus median credits per 1% as `W`: the credits a percent of
    that account's meter buys, priced entirely at rates the article publishes. A stretch
    that moved `delta_pct` therefore spent `delta_pct x W` credits. Subtract the credits
    of every non-Fable model in it at those same published rates and what is left is
    Fable's, so

        fable_input = (delta_pct x W - known_credits) / (fable_in + ratio x fable_out)

    with `fable_in` Fable's input-rate tokens (input + cache_write + weighted cache_read)
    and `fable_out` its output tokens. Only stretches where Fable holds more than
    FABLE_HEAVY of the raw tokens are solved: below that the divisor is small and the
    residual of everything else lands on it.

    This is arithmetic on one stretch, not a fit. It inherits every assumption above --
    W's own accuracy, the published rates, the cache-read weight, and for masterrig the
    phantom usage the transcripts cannot see, which inflates delta_pct and so inflates
    the rate solved from it.
    """
    out = []
    for r in rows:
        if r["fable_token_share"] <= FABLE_HEAVY or r["account"] not in opus:
            continue
        w = opus[r["account"]]["median"]
        fable_in = fable_out = 0.0
        known = 0.0
        for model, tok in r["tokens"].items():
            at_in, at_out = chargeable(tok, cache_read_weight)
            if family(model) == "fable":
                fable_in += at_in
                fable_out += at_out
            else:
                rate_in, rate_out = PUBLISHED_RATES[family(model)]
                known += at_in * rate_in + at_out * rate_out
        divisor = fable_in + fable_ratio * fable_out
        if divisor <= 0:
            continue
        out.append({"account": r["account"], "era": r["era"], "end": r["end"],
                    "fable_token_share": round(r["fable_token_share"], 4),
                    "opus_median_credits_per_pct": w,
                    "solved_fable_input": (r["delta_pct"] * w - known) / divisor})
    return out


def _fmt(x: float | None, width: int = 11) -> str:
    return "-".rjust(width) if x is None else f"{x:>{width},.0f}"


def _table(header: tuple[str, ...], widths: tuple[int, ...], lines: list[tuple[str, ...]]) -> str:
    out = ["  ".join(h.ljust(w) if i < 2 else h.rjust(w) for i, (h, w) in enumerate(zip(header, widths)))]
    out.append("  ".join("-" * w for w in widths))
    for row in lines:
        out.append("  ".join(c.ljust(w) if i < 2 else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths))))
    return "\n".join(out)


def _excluded_table(excluded: list[dict]) -> list[str]:
    """The harness-run exclusions, one line each, with the run that caused them."""
    out = ["\nExcluded: the stretch overlaps one of the tracker's own runs on that account --",
           "a probe row in history/probes.jsonl, or the effort-matrix run in data/effort_matrix.json.",
           "The instrument was driving the account there, so the percent is not a reading of a session."]
    if not excluded:
        return [*out, "  (none)"]
    head = ("account", "stretch start", "end", "delta", "capture", "caused by")
    widths = (9, 19, 19, 5, 7, 52)
    out.append("  ".join(h.ljust(w) for h, w in zip(head, widths)).rstrip())
    out.append("  ".join("-" * w for w in widths))
    for e in excluded:
        cap = "-" if e["capture"] is None else f"{e['capture']:.3f}"
        delta = "-" if e["delta_pct"] is None else f"{e['delta_pct']:g}"
        out.append("  ".join([e["account"].ljust(widths[0]), e["start"][:19].ljust(widths[1]),
                              e["end"][:19].ljust(widths[2]), delta.rjust(widths[3]),
                              cap.rjust(widths[4]), e["reason"]]).rstrip())
    return out


def render(rows: list[dict], skipped: dict, excluded: list[dict], args: argparse.Namespace) -> str:
    out: list[str] = []
    out.append("Credits per 1% of the five-hour meter, from history/*-passive.json.")
    out.append("Rates: docs/reference-2026-09-20-shellac-credits-model.md (branch step-vs-trend-finding),")
    out.append("from https://she-llac.com/claude-limits -- a reference, not a source of truth.")
    out.append("Haiku 2/15 in 10/15 out, Sonnet 6/15 30/15, Opus (any version) 10/15 50/15.")
    out.append(f"Cache writes at the input rate, cache reads at {args.cache_read_weight:g} of it.")
    out.append(f"PROVISIONAL, ours not the article's: Fable input {args.fable_input:.6g} credits/token "
               f"({args.fable_input / OPUS_INPUT:.2f}x Opus), output ratio {args.fable_output_ratio:g}.")
    out.append(f"{len(rows)} stretches priced; left out: "
               + ", ".join(f"{k} {v}" for k, v in skipped.items() if k != "unknown_models" and v)
               + (f" (unknown models: {', '.join(skipped['unknown_models'])})" if skipped["unknown_models"] else ""))
    if any(r["account"] == "masterrig" for r in rows):
        out.append("masterrig's meter also counts web, phone and other machines, so its rows divide real")
        out.append("meter movement by only the tokens this host saw and read low. See each stretch's capture.")
    out.extend(_excluded_table(excluded))

    out.append("\nBy account, era and dominant model (over 90% of the stretch's credits, else mixed)")
    header = ("account", "era", "dominant>90%", "n", "median cpp", "p25", "p75")
    widths = (9, 7, 13, 4, 11, 11, 11)
    lines = [(a, e, g, str(s["n"]), _fmt(s["median"]), _fmt(s["p25"]), _fmt(s["p75"]))
             for a, e, g, s in _group(rows)]
    out.append(_table(header, widths, lines))

    out.append("\nPure clusters: every credit of the stretch from one model family")
    pure_header = ("account", "cluster", "n", "median cpp", "p25", "p75")
    pure_widths = (9, 12, 4, 11, 11, 11)
    lines, empty = [], []
    clusters = {fam: pure_clusters(rows, fam) for fam in ("opus", "sonnet")}
    for fam, by_account in clusters.items():
        if not by_account:
            empty.append(f"pure-{fam}")
        for account, s in by_account.items():
            lines.append((account, f"pure-{fam}", str(s["n"]), _fmt(s["median"]), _fmt(s["p25"]), _fmt(s["p75"])))
    out.append(_table(pure_header, pure_widths, lines) if lines else "  (none)")
    if empty:
        out.append(f"  nothing is {' or '.join(empty)} in any account: every stretch mixes a second")
        out.append("  model in, so that family has no single-model level to read.")

    out.append(f"\nFable input rate solved per Fable-heavy stretch (Fable over {FABLE_HEAVY:.0%} of raw tokens),")
    out.append("from the account's pure-Opus median as the credits a percent buys. PROVISIONAL.")
    solved = solve_fable_input(rows, clusters["opus"], args.fable_output_ratio, args.cache_read_weight)
    if not solved:
        out.append("  (none: no account has both a pure-Opus cluster and a Fable-heavy stretch)")
    else:
        by_account: dict[str, list[float]] = {}
        for s in solved:
            by_account.setdefault(s["account"], []).append(s["solved_fable_input"])
        out.append(_table(("account", "credits/token", "n", "median rate", "p25", "p75"), (9, 12, 4, 11, 11, 11),
                          [(a, "", str(len(v)), f"{quartiles(v)[1]:.4f}", f"{quartiles(v)[0]:.4f}",
                            f"{quartiles(v)[2]:.4f}") for a, v in sorted(by_account.items())]))
        out.append("  as a multiple of Opus input (10/15): "
                   + ", ".join(f"{a} {quartiles(v)[1] / OPUS_INPUT:.2f}x"
                               for a, v in sorted(by_account.items())))
        out.append(f"  assumed for the tables above: {args.fable_input:.4f} "
                   f"({args.fable_input / OPUS_INPUT:.2f}x Opus)")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gs", type=Path, default=REPORTS["gs"], help="history/gs-passive.json")
    ap.add_argument("--masterrig", type=Path, default=REPORTS["masterrig"], help="history/masterrig-passive.json")
    ap.add_argument("--cache-read-weight", type=float, default=CACHE_READ_WEIGHT,
                    help="cache reads count at this multiple of the input rate (the article says 0)")
    ap.add_argument("--fable-input", type=float, default=FABLE_INPUT,
                    help="provisional Fable input rate in credits per token")
    ap.add_argument("--fable-output-ratio", type=float, default=FABLE_OUTPUT_RATIO,
                    help="provisional Fable output rate as a multiple of its input rate")
    ap.add_argument("--probes", type=Path, default=PROBES,
                    help="probe rows whose spans are excluded from their own account")
    ap.add_argument("--effort-matrix", type=Path, default=EFFORT_MATRIX,
                    help=f"effort-matrix run whose span is excluded from {EFFORT_MATRIX_ACCOUNT}")
    ap.add_argument("--keep-harness-runs", action="store_true",
                    help="do not exclude stretches overlapping a probe or the effort-matrix run")
    ap.add_argument("--min-capture", type=float,
                    help="keep only stretches whose own capture is at least this "
                         "(for masterrig, where the meter counts more than this host)")
    ap.add_argument("--json", type=Path, help="also dump the per-stretch rows here")
    a = ap.parse_args(argv)
    runs = [] if a.keep_harness_runs else harness_runs(a.probes, a.effort_matrix)
    rows, skipped, excluded = load_rows({"gs": a.gs, "masterrig": a.masterrig}, a.cache_read_weight,
                                        a.fable_input, a.fable_output_ratio, a.min_capture, runs)
    if not rows:
        print(f"no priceable stretches in {a.gs} or {a.masterrig}")
        return 1
    print(render(rows, skipped, excluded, a), end="")
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "rates": {"published": {k: list(v) for k, v in PUBLISHED_RATES.items()},
                      "fable_input": a.fable_input, "fable_output_ratio": a.fable_output_ratio,
                      "cache_read_weight": a.cache_read_weight,
                      "provisional": ["fable_input", "fable_output_ratio", "cache_read_weight"],
                      "source": "docs/reference-2026-09-20-shellac-credits-model.md "
                                "(branch step-vs-trend-finding); a reference, not a source of truth"},
            "eras": {"5x": f"end < {ERA_20X_AT.isoformat()}",
                     "20x": f"{ERA_20X_AT.isoformat()} <= end < {ERA_CUT_AT.isoformat()}",
                     "20x-cut": f"end >= {ERA_CUT_AT.isoformat()}"},
            "harness_runs": [{"account": r.account, "start": r.start.isoformat(),
                              "end": r.end.isoformat(), "reason": r.reason} for r in runs],
            "skipped": skipped, "excluded": excluded, "rows": rows,
        }, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {a.json} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
