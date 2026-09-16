"""The capture-completeness check: withhold passive readings the transcripts cannot account for.

Why this is the point of passive measurement on gs (issue #39). A passive
reading divides what an account's transcripts spent by how far that same
account's meter moved. It is only a measurement while the transcripts hold
every token the meter counted. When the account is also used somewhere else
-- the web, the phone, another machine -- or transcript collection breaks,
tokens go missing from the numerator while the meter still moves, and the
rate reads low with nothing in it to say so. The same join on Jonathan's own
account is exactly that: a fifteen-fold day-to-day spread, $2.20 to $0.17 per
1%. The failure also runs the other way: a transcript directory shared with
another account (jwork's `projects/` is a symlink into `~/.claude/projects`,
which other config dirs write to) holds tokens this meter never counted, and
the rate reads high.

So every stretch (tracker/join.py) is judged against the account's own
recent accepted stretches before it can be published:

- The reference is the median of the account's last LOOKBACK accepted
  stretches. It starts from a bootstrap: the median of the first BOOTSTRAP
  priced stretches, each then judged against it. Until there are enough
  stretches for that, a stretch is `unjudged`, never accepted. The reference
  is the account's own and never the probe's: real sessions are nearly all
  cache reads and value very differently from the cache-write-heavy probe
  payload under the current class weights, so the two instruments' levels
  are not comparable; only their steps are.
- A stretch is withheld when even the most favourable reading of its
  whole-percent meter movement (Stretch.bounds, one point of rounding per
  window piece) lies outside TOLERANCE of the reference: `unaccounted` below
  (meter movement the transcripts do not explain), `surplus` above
  (transcripts the meter did not count). TOLERANCE is the tracker's standing
  15%, the same as the drift rule and change detection, rather than a width
  learned from the accepted readings: a band fitted to what it accepts widens
  with every noisy reading it lets in.
- A stretch with any token from a model the price table does not have is
  `unpriced`: it cannot be valued, so it is not judged. There is no tolerated
  raw-token share (audit 2026-09-16, finding 9): a sliver of unpriced output can
  be most of the meter dollars beside a large, nearly free cache-read bundle.

Only `accepted` stretches are ever published (`published`). A withheld one is
kept, with its capture (its rate over the reference), so it can be inspected.

A run of withheld stretches is a signal in its own right, and `runs` names
what it looks like:

- `collection_gap`: the meter moved while the transcripts hold almost
  nothing (median capture under COLLECTION_GAP). Collection has broken, or
  the account is being used entirely off gs. No limit change reads like this.
- `level_shift`: at least MIN_SHIFT withheld stretches that agree with each
  other -- every one would pass the check against the run's own median. That
  is what a genuine limit change looks like, and also what steady off-gs use
  looks like, so it stays withheld until something independent agrees.
- `unaccounted` / `surplus`: withheld stretches that do not agree with each
  other. Off-gs use comes and goes, so the deficit scatters.

`check` then weighs each run against independent evidence. A limit change is
on the plan, so the other gs account (both are Max 20x) shifts with it and a
probe steps with it; missing capture is one account's own. A run is
`corroborated_by` the other account when that account shows a level shift
the same way and about the same size over the same time, and by the probe
when probe readings after the run started step the same way and size from the
median before it. It is `contradicted_by` the other account when that
account's stretches were accepted through the run, and by the probe when the
probe did not move. Only a corroborated level shift becomes a change: the
reference restarts where the run began, so the new level is accepted from
there, and the change is reported. Nothing uncorroborated is ever published,
and no run of withheld stretches can quietly read as a change.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable
from .join import Stretch

TOLERANCE = 0.15
BOOTSTRAP = 5
MIN_REFERENCE = 4
LOOKBACK = 8
COLLECTION_GAP = 0.10
MIN_SHIFT = 3
MIN_PROBE_HISTORY = 3  # detect.py's MIN_HISTORY
PROBE_LOOKBACK = 4     # detect.py's LOOKBACK
SLACK = timedelta(days=1)

ACCEPTED, UNACCOUNTED, SURPLUS, UNPRICED, UNJUDGED = "accepted", "unaccounted", "surplus", "unpriced", "unjudged"
WITHHELD = (UNACCOUNTED, SURPLUS)


@dataclass
class Verdict:
    stretch: Stretch
    status: str
    reference: float | None = None

    @property
    def capture(self) -> float | None:
        """This stretch's rate over the reference: the share of expected meter dollars the transcripts hold."""
        return self.stretch.usd_per_pct / self.reference if self.reference else None


def _against(stretch: Stretch, reference: float) -> str:
    lo, hi = stretch.bounds
    if hi < reference * (1 - TOLERANCE):
        return UNACCOUNTED
    if lo > reference * (1 + TOLERANCE):
        return SURPLUS
    return ACCEPTED


def judge(stretches: Iterable[Stretch], restarts: Iterable[datetime] = ()) -> list[Verdict]:
    """One verdict per stretch, in time order.

    `restarts` are the starts of confirmed changes: from each, the reference
    is bootstrapped afresh instead of carried over from the old level.
    """
    ordered = sorted(stretches, key=lambda s: s.end)
    cuts = sorted(restarts)
    segments: list[list[int]] = [[]]
    ci = 0
    for i, s in enumerate(ordered):
        while ci < len(cuts) and s.start >= cuts[ci]:
            segments.append([])
            ci += 1
        segments[-1].append(i)
    out: list[Verdict | None] = [None] * len(ordered)
    for segment in segments:
        priced = []
        for i in segment:
            # Raw-token share cannot bound monetary error: a tiny amount of an
            # unknown output class can dominate a large free-cache-read bundle.
            if ordered[i].unpriced_tokens > 0:
                out[i] = Verdict(ordered[i], UNPRICED)
            else:
                priced.append(i)
        pool: list[float] = []
        k = 0
        while k < len(priced):
            if len(pool) < MIN_REFERENCE:
                batch = priced[k:k + BOOTSTRAP]
                if len(batch) < BOOTSTRAP:
                    for i in batch:
                        out[i] = Verdict(ordered[i], UNJUDGED)
                    break
                reference = median(ordered[i].usd_per_pct for i in batch)
                k += BOOTSTRAP
            else:
                batch = [priced[k]]
                reference = median(pool[-LOOKBACK:])
                k += 1
            for i in batch:
                out[i] = Verdict(ordered[i], _against(ordered[i], reference), reference)
                if out[i].status == ACCEPTED:
                    pool.append(ordered[i].usd_per_pct)
    return [v for v in out if v is not None]


def published(verdicts: Iterable[Verdict]) -> list[Verdict]:
    """The only verdicts that may ever reach a published series."""
    return [v for v in verdicts if v.status == ACCEPTED]


@dataclass
class Run:
    verdicts: list[Verdict]
    account: str | None = None
    kind: str = ""
    corroborated_by: list[str] = field(default_factory=list)
    contradicted_by: list[str] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.verdicts[0].stretch.start

    @property
    def end(self) -> datetime:
        return self.verdicts[-1].stretch.end

    @property
    def direction(self) -> str:
        return "low" if self.verdicts[0].status == UNACCOUNTED else "high"

    @property
    def rate(self) -> float:
        return median(v.stretch.usd_per_pct for v in self.verdicts)

    @property
    def step(self) -> float:
        """The run's level relative to the reference it was judged against."""
        return self.rate / median(v.reference for v in self.verdicts if v.reference is not None) - 1

    @property
    def capture(self) -> float:
        return median(v.capture for v in self.verdicts if v.capture is not None)


def _consistent(verdicts: list[Verdict]) -> bool:
    level = median(v.stretch.usd_per_pct for v in verdicts)
    return all(_against(v.stretch, level) == ACCEPTED for v in verdicts)


def _classify(run: Run) -> str:
    if run.capture < COLLECTION_GAP:
        return "collection_gap"
    if len(run.verdicts) >= MIN_SHIFT and _consistent(run.verdicts):
        return "level_shift"
    return "unaccounted" if run.direction == "low" else "surplus"


def runs(verdicts: Iterable[Verdict], account: str | None = None) -> list[Run]:
    """Consecutive withheld verdicts of one direction; an accepted one ends a run.

    Unpriced and unjudged stretches neither extend nor end a run: they say
    nothing about capture either way.
    """
    out: list[Run] = []
    cur: Run | None = None
    for v in verdicts:
        if v.status in WITHHELD:
            if cur is not None and cur.verdicts[-1].status == v.status:
                cur.verdicts.append(v)
            else:
                cur = Run([v], account)
                out.append(cur)
        elif v.status == ACCEPTED:
            cur = None
    for r in out:
        r.kind = _classify(r)
    return out


def _probe_step(readings: list[tuple[datetime, float]], run: Run) -> float | None:
    before = [v for t, v in readings if t < run.start][-PROBE_LOOKBACK:]
    after = [v for t, v in readings if run.start <= t <= run.end + SLACK]
    if len(before) < MIN_PROBE_HISTORY or not after:
        return None
    return median(after) / median(before) - 1


def _agrees(step: float, run: Run) -> bool:
    return (step < 0) == (run.step < 0) and abs(step) > TOLERANCE and abs(step - run.step) <= TOLERANCE


def _weigh(verdicts: dict[str, list[Verdict]], runs_by: dict[str, list[Run]],
           probe_readings: list[tuple[datetime, float]]) -> None:
    for account, account_runs in runs_by.items():
        for run in account_runs:
            for other, other_runs in runs_by.items():
                if other == account:
                    continue
                if run.kind == "level_shift" and any(
                        q.kind == "level_shift" and _agrees(q.step, run)
                        and q.start <= run.end + SLACK and q.end >= run.start - SLACK for q in other_runs):
                    run.corroborated_by.append(other)
                elif sum(1 for v in verdicts[other] if v.status == ACCEPTED and v.stretch.start >= run.start
                         and v.stretch.end <= run.end + SLACK) >= MIN_SHIFT:
                    run.contradicted_by.append(other)
            step = _probe_step(probe_readings, run)
            if step is None:
                continue
            if run.kind == "level_shift" and _agrees(step, run):
                run.corroborated_by.append("probe")
            elif abs(step) <= TOLERANCE:
                run.contradicted_by.append("probe")


@dataclass
class Checked:
    verdicts: dict[str, list[Verdict]]
    runs: dict[str, list[Run]]
    changes: list[dict]


def check(stretches: dict[str, list[Stretch]], probe_readings: Iterable[tuple[datetime, float]] = ()) -> Checked:
    """Judge every account, weigh its withheld runs, and rebase only on a corroborated change.

    `probe_readings` are (time, meter dollars per 1%) from probe rows; only
    their steps are compared, never their level.
    """
    readings = sorted(probe_readings)
    first = {a: judge(s) for a, s in stretches.items()}
    first_runs = {a: runs(v, a) for a, v in first.items()}
    _weigh(first, first_runs, readings)
    confirmed = {a: [r for r in rs if r.kind == "level_shift" and r.corroborated_by] for a, rs in first_runs.items()}
    changes = [{"account": a, "at": r.start.isoformat(), "step": round(r.step, 4), "corroborated_by": list(r.corroborated_by)}
               for a, rs in confirmed.items() for r in rs]
    verdicts = {a: judge(stretches[a], [r.start for r in confirmed[a]]) if confirmed[a] else first[a] for a in stretches}
    final_runs = {a: runs(v, a) for a, v in verdicts.items()}
    _weigh(verdicts, final_runs, readings)
    return Checked(verdicts, final_runs, sorted(changes, key=lambda c: (c["at"], c["account"])))
