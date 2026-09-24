"""Power of the known-date change test (`tracker.credits.announced_change`), offline.

For each account with a before side at the chosen candidate, the before side is the
account's own committed stretches (log credits per 1%), and an after side of `n_after`
stretches is drawn by resampling that before side's own residuals around its mean,
shifted by log(1 + step). The accounts are compared and combined exactly as the
publisher does (`log_ratio_side`, `combine_inverse_variance`). A trial counts when the
candidate reaches `measured` and the combined 95% interval excludes 1.0.

    python3 -m tools.announced_change_sim [--trials 200] [--step 0.20] [--n-after 10]

The committed history prices no Opus 5.5 stretch on `main` before PR A, which does not
matter here: every before-side stretch predates Opus 5.5.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from tracker import credits as C
from tracker.gs_passive import stretch_credits
from tracker.publish import ACCOUNT_LABELS, CREDITS

ROOT = Path(__file__).resolve().parent.parent


def before_sides(family: str = "opus-5-5", gs: Path | None = None,
                 masterrig: Path | None = None) -> dict[str, list[float]]:
    """Each account's before side at `family`'s candidate, from the committed history."""
    gs_raw = json.loads((gs or ROOT / "history" / "gs-passive.json").read_text())
    mr_raw = json.loads((masterrig or ROOT / "history" / "masterrig-passive.json").read_text())
    by_account = C.stretches_by_account(gs_raw, mr_raw)
    rates = C.load_model_rates()
    cands = C.change_candidates(by_account, CREDITS)
    at = next(c["at"] for c in cands if c["family"] == family)
    lo, _hi = C.candidate_bounds(at, cands)
    selected = C.announced_change_stretches(by_account, C.harness_runs())
    out = {}
    for name, rows in selected.items():
        split = C.split_at_candidate(rows, at, lo, None,
                                     lambda tok: stretch_credits(tok, CREDITS, rates)[0])
        if len(split["sides"]["before"]) >= C.ANNOUNCED_MIN_BEFORE:
            out[name] = split["sides"]["before"]
    return out


def trial(before: dict[str, list[float]], step: float, n_after: int, rng: random.Random) -> bool:
    """One seeded trial: True when the test reads measured with 1.0 outside the interval."""
    paired = {}
    for name in sorted(before):
        xs = before[name]
        mean = sum(xs) / len(xs)
        resid = [x - mean for x in xs]
        after = [mean + math.log(1 + step) + rng.choice(resid) for _ in range(n_after)]
        pair = C.log_ratio_side(xs, after)
        if pair is not None:
            paired[name] = pair
    combined = C.combine_inverse_variance(paired)
    if combined is None or C.announced_state([n_after] * len(paired)) != "measured":
        return False
    lo, hi = combined["interval"]
    return not lo <= 1 <= hi


def exclusion_rate(before: dict[str, list[float]], step: float, n_after: int = 10,
                   trials: int = 200, seed: int = 0) -> float:
    """The share of `trials` seeded trials whose combined interval excludes 1.0."""
    hits = sum(trial(before, step, n_after, random.Random(seed + i)) for i in range(trials))
    return hits / trials


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--n-after", type=int, default=10)
    ap.add_argument("--family", default="opus-5-5")
    a = ap.parse_args(argv)
    before = before_sides(a.family)
    labels = dict(ACCOUNT_LABELS)
    for name, xs in sorted(before.items(), key=lambda kv: labels.get(kv[0], kv[0])):
        mean = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
        print(f"{labels.get(name, '?')}: {len(xs)} stretches before, log sd {sd:.3f}")
    for step in (0.20, 0.0):
        combined = exclusion_rate(before, step, a.n_after, a.trials)
        print(f"step {step:+.0%}: combined interval excludes 1.0 in {combined:.1%} of {a.trials}")
        for name in sorted(before, key=lambda n: labels.get(n, n)):
            alone = exclusion_rate({name: before[name]}, step, a.n_after, a.trials)
            print(f"  {labels.get(name, '?')} alone: {alone:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
