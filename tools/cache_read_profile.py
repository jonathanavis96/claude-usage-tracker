"""Profile the per-family rate fits over a fixed cache-read weight, to settle what the weight is.

tools/model_rates.py fits the cache-read weight jointly with the per-family rates, and the three
fits the publisher pools disagree about it: jwork before 14 September puts it at 0.0114, jwork
after at 0.0209, and dave after at 0.0 -- on the non-negativity bound. A value on the bound can
mean the data say zero, or that the data cannot see the weight at all and the constraint is
what put it there. This tool tells the two apart without sending any traffic:

1. For each fit, the rates are refitted with the weight FIXED at each point of a grid, and the
   fit's error is reported at each: a profile of the error over the weight.
2. Each fit's error increase between its own best weight and the other fits' weights is set
   beside the spread of the same fit's error under the bootstrap tools/model_rates.py uses
   (seed 20260920, 600 resamples), which says whether that fit can distinguish them.
3. One weight shared by the three fits, with the family rates still per fit, and a bootstrap
   interval on it.
4. What the published figures do at weight 0, at the pooled weight and at 0.02: the publisher
   is rebuilt to scratch files from a temporary copy of data/prices.json with the weight
   overridden. The committed data/prices.json is never written.

    python3 -m tools.cache_read_profile history/masterrig-passive.json
    python3 -m tools.cache_read_profile history/masterrig-passive.json --json out.json

The stretches, the exclusions, the family grouping and the output-5x input-equivalent tokens are
tools/model_rates.py's own (`prepare`, `clean`, `design`), so the fit at a fixed weight is the
joint fit of that tool with its cache-read coefficient held rather than fitted. In the joint fit
the cache-read column's coefficient c and the Opus coefficient b_opus give the weight
w = c x CACHE_READ_PER_MILLION / b_opus; holding w fixed makes c = w x b_opus / CACHE_READ_PER_MILLION,
so the cache-read tokens join the Opus column at w times their count and the fit keeps one column
per family. As in that tool, the weight is one number charged at the Opus input rate on every
family's cache reads.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import model_rates as M
from tracker import credits as C

#: The fits are derived from the data, as tools/model_rates.py derives its fit accounts: every
#: account and side of the cut with at least tools/model_rates.py MIN_FIT_N usable stretches,
#: at the output multiplier it adopts.
OUT_MULT = 5
GRID = (0.0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03)
#: The pooled weight is searched over this range: a step of STEP, then a golden-section
#: refinement inside the best step. The upper end sits above every fit's bootstrap interval in
#: history/model-rates.json (the widest reaches 0.047); a pooled value at it is reported as such.
SEARCH = (0.0, 0.06)
STEP = 0.0005
SEED = M.SEED
RESAMPLES = M.RESAMPLES
PRICES = Path("data/prices.json")


def groups(data: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """The usable stretches of each pooled fit, keyed `account/era`."""
    out = {}
    for account in M.fit_accounts(data):
        for era in ("pre", "post"):
            recs = [r for r in data[account] if r["era"] == era and r["ok"]]
            if len(recs) >= M.MIN_FIT_N:
                out[f"{account}/{era}"] = recs
    return out


def families(recs: list[dict]) -> tuple[str, ...]:
    """The families a fit carries a column for: tools/model_rates.py `fit`'s own rule."""
    return tuple(f for f in M.FAMILIES
                 if sum(1 for r in recs if r["raw"][f] > 0) >= M.MIN_NONZERO)


def matrices(recs: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[str, ...]]:
    """The family columns, the meter movement, and cache reads in the family columns' unit."""
    fams = families(recs)
    if len(recs) < M.MIN_FIT_N or "opus" not in fams:
        raise ValueError(f"{len(recs)} stretches and families {fams} do not support a fit")
    X, y = M.design(recs, OUT_MULT, fams)
    reads = np.array([r["total_reads"] / 1e6 for r in recs])
    return X, y, reads, fams


def fit_at(X: np.ndarray, y: np.ndarray, reads: np.ndarray, w: float) -> tuple[np.ndarray, float]:
    """The non-negative fit with the cache-read weight held at `w`: coefficients and RSS.

    Opus is column 0 (FAMILIES order), so the reads join it at w times their count.
    """
    Xw = X.copy()
    Xw[:, 0] += w * reads
    b = M.nnls(Xw, y)
    r = Xw @ b - y
    return b, float(r @ r)


def summary(X, y, reads, fams, w: float) -> dict:
    """One grid point: the fit's error at weight `w` and the rates it lands on."""
    b, rss = fit_at(X, y, reads, w)
    Xw = X.copy()
    Xw[:, 0] += w * reads
    pred = Xw @ b
    scale = M.SHELLAC["opus"] / b[0] if b[0] > 0 else float("nan")
    return {"weight": w, "rss": rss,
            "residual_median_abs_rel": float(np.median(np.abs(pred - y) / y)),
            "rates": {f: float(v * scale) for f, v in zip(fams, b)}}


def best_weight(parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
                lo: float = SEARCH[0], hi: float = SEARCH[1], step: float = STEP,
                scales: list[float] | None = None) -> float:
    """The weight that minimises the summed RSS of every part, each part fitted separately.

    One part is one fit's own profile minimum; several are the pooled fit (one weight, rates per
    fit), where `scales` divides each part's RSS by its own. A grid, then golden section inside
    the best cell, so a minimum on a bound is found exactly on it.
    """
    div = scales or [1.0] * len(parts)

    def total(w):
        return sum(fit_at(X, y, rd, w)[1] / s for (X, y, rd), s in zip(parts, div))
    grid = np.arange(lo, hi + step / 2, step)
    vals = [total(w) for w in grid]
    i = int(np.argmin(vals))
    a, b = grid[max(0, i - 1)], grid[min(len(grid) - 1, i + 1)]
    g = (5 ** 0.5 - 1) / 2
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = total(c), total(d)
    for _ in range(40):
        if fc <= fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = total(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = total(d)
    w, fw = (c, fc) if fc <= fd else (d, fd)
    # The grid's own best point wins a tie with the refinement, so a bound stays on the bound.
    return float(grid[i]) if vals[i] <= fw else float(w)


def resample(n: int, rng: random.Random) -> list[int]:
    """One bootstrap draw of stretch indices, drawn the way tools/model_rates.py draws them."""
    return [rng.randrange(n) for _ in range(n)]


def profile(G: dict[str, list[dict]]) -> dict:
    """Section 1: each fit's error and rates at every grid weight, and its own best weight."""
    out = {}
    for label, recs in G.items():
        X, y, reads, fams = matrices(recs)
        w0 = best_weight([(X, y, reads)])
        out[label] = {"n": len(y), "families": list(fams),
                      "grid": [summary(X, y, reads, fams, w) for w in GRID],
                      "best": summary(X, y, reads, fams, w0)}
    return out


def consistency(G: dict[str, list[dict]], prof: dict, seed: int, resamples: int) -> dict:
    """Section 2: can each fit tell its own best weight from the other fits' weights?

    For fit F with best weight w_F and another fit's weight w, the error increase
    RSS_F(w) - RSS_F(w_F) is set against two things from the same bootstrap draws:

    - the spread of F's own error: the 80% interval of RSS_F(w_F) over the resamples, the
      noise a single fit's error carries. An increase well inside that width is one the fit
      cannot see.
    - a paired test: the share of resamples in which w fits F's resampled stretches at least
      as well as w_F does. Under the 80% interval rule the tool uses everywhere, a share of
      10% or more means w is inside what F's data allow, and a smaller one means F's data
      exclude it.

    The verdict is the paired test's, because it compares the two weights on the same draws;
    the spread is published beside it so the size of the difference can be read too.
    """
    out = {}
    for label, recs in G.items():
        X, y, reads, _ = matrices(recs)
        w_own = prof[label]["best"]["weight"]
        rss_own = prof[label]["best"]["rss"]
        others = {other: prof[other]["best"]["weight"] for other in G if other != label}
        rng = random.Random(f"{seed}/cache-read-consistency/{label}")
        draws_own, better = [], {o: 0 for o in others}
        n = len(y)
        for _ in range(resamples):
            idx = resample(n, rng)
            Xb, yb, rb = X[idx], y[idx], reads[idx]
            own = fit_at(Xb, yb, rb, w_own)[1]
            draws_own.append(own)
            for o, w in others.items():
                if fit_at(Xb, yb, rb, w)[1] <= own:
                    better[o] += 1
        lo, hi = M.interval(draws_own)
        spread = hi - lo
        rows = {}
        for o, w in others.items():
            increase = fit_at(X, y, reads, w)[1] - rss_own
            share = better[o] / resamples
            rows[o] = {"weight": w, "rss_increase": increase,
                       "increase_over_spread": increase / spread if spread else None,
                       "share_of_resamples_fitting_as_well": share,
                       "verdict": ("cannot distinguish" if share >= M.INTERVAL[0] / 100
                                   else "contradicts")}
        out[label] = {"own_weight": w_own, "rss": rss_own,
                      "rss_bootstrap_interval": [lo, hi], "rss_bootstrap_spread": spread,
                      "against": rows}
    return out


def pooled(G: dict[str, list[dict]], prof: dict, seed: int, resamples: int) -> dict:
    """Section 3: one cache-read weight across the fits, the family rates still per fit.

    The weight minimises the RSS summed over the fits, each refitted at it. The interval
    resamples each fit's stretches within that fit (so every draw still has the same three
    fits) and takes the 80% interval of the draws' pooled weights.

    Plain summed RSS lets the noisiest fit carry the most weight: a fit whose stretches
    scatter twice as far puts four times the squared error on the scale. `variance_weighted`
    is the same fit with each fit's RSS divided by its own residual variance at its own best
    weight (RSS / (n - columns - 1)), fixed from the full data, on the same bootstrap draws.
    Both are reported; neither is a model of why the fits disagree.
    """
    mats = {label: matrices(recs) for label, recs in G.items()}
    parts = [(X, y, rd) for X, y, rd, _ in mats.values()]
    var = [prof[label]["best"]["rss"] / (len(y) - X.shape[1] - 1)
           for label, (X, y, rd, _) in mats.items()]
    w = best_weight(parts)
    wv = best_weight(parts, scales=var)
    rng = random.Random(f"{seed}/cache-read-pooled")
    draws, draws_v = [], []
    for _ in range(resamples):
        drawn = []
        for X, y, rd in parts:
            idx = resample(len(y), rng)
            drawn.append((X[idx], y[idx], rd[idx]))
        draws.append(best_weight(drawn))
        draws_v.append(best_weight(drawn, scales=var))

    def block(value, ds):
        per_fit = {label: summary(X, y, rd, fams, value) for label, (X, y, rd, fams) in mats.items()}
        return {"weight": value, "interval": list(M.interval(ds)), "resamples": resamples,
                "draws_at_zero": sum(1 for d in ds if d == 0.0),
                "draws_at_upper_bound": sum(1 for d in ds if d >= SEARCH[1]),
                "rss": sum(r["rss"] for r in per_fit.values()), "per_fit": per_fit}
    out = block(w, draws)
    out["variance_weighted"] = {**block(wv, draws_v),
                                "residual_variance": dict(zip(mats, var))}
    return out


def published_figures(j: dict) -> dict:
    """The figures section 4 tabulates, read off one published document."""
    c = j["credits"]
    wt = c["window_tokens"]
    lo, hi = wt["all"]["interval"] or (None, None)
    out = {"window_credits": c["window_credits"]["value"],
           "window_credits.before": c["window_credits"]["before"]["value"],
           "window_credits.after": c["window_credits"]["after"]["value"],
           "window_credits_from_weekly.before (cross-check)":
               c["window_credits_from_weekly"]["before"]["value"],
           "window_credits_from_weekly.after (cross-check)":
               c["window_credits_from_weekly"]["after"]["value"],
           "headline window_tokens.all": wt["all"]["value"],
           "headline window_tokens.all interval low": lo,
           "headline window_tokens.all interval high": hi,
           "window_tokens.per_week.all": (wt.get("per_week") or {}).get("all", {}).get("value")}
    for f, v in sorted((wt.get("per_family") or {}).items()):
        out[f"window_tokens.per_family.{f}"] = v["all"]["value"]
    for f, v in sorted(c["per_model"].items()):
        for side in ("input", "output"):
            out[f"per_model.{f}.tokens_per_window.{side}"] = v["tokens_per_window"][side]["value"]
    return out


def sensitivity(weights: dict[str, float], now: datetime, outdir: Path) -> dict:
    """Section 4: the publisher's output at each weight, from a temporary copy of the price table.

    `tracker.publish.rebuild_public_json` is the one code path `tracker.publish` main and the
    publish check share; `main` itself is not called because it rewrites --prices when the
    weekly output run has supplied a weight, and the table here is a scratch copy. Only
    `_credits.cache_read_weight` is overridden: the per-family rates stay the ones
    history/model-rates.json publishes, so this is the weight's own effect on the figures.
    """
    from tracker.publish import rebuild_public_json, write_json
    base = json.loads(PRICES.read_text(encoding="utf-8"))
    runs = {}
    with tempfile.TemporaryDirectory(prefix="cache-read-profile-", dir="/tmp") as tmp:
        for label, w in weights.items():
            table = copy.deepcopy(base)
            table["_credits"]["cache_read_weight"] = w
            prices = Path(tmp) / f"prices-{label}.json"
            prices.write_text(json.dumps(table, indent=1), encoding="utf-8")
            j = rebuild_public_json(now, probes=Path("history/probes.jsonl"),
                                    passive=Path("history/passive.json"),
                                    effort=Path("data/effort_matrix.json"), prices=prices,
                                    gs_passive=Path("history/gs-passive.json"),
                                    masterrig_passive=Path("history/masterrig-passive.json"))
            out = outdir / f"claude-usage-cache-read-{label}.json"
            write_json(out, j)
            runs[label] = {"weight": w, "file": str(out), "figures": published_figures(j)}
    first = next(iter(runs))
    for label, r in runs.items():
        r["change_from_" + first] = {
            k: (v / runs[first]["figures"][k] - 1
                if v is not None and runs[first]["figures"].get(k) else None)
            for k, v in r["figures"].items()}
    return runs


def show(prof: dict, cons: dict, pool: dict, sens: dict | None) -> None:
    print("1. Fit error over a fixed cache-read weight (output 5x, Opus fixed at 10/15)")
    for label, p in prof.items():
        b = p["best"]
        print(f"   {label:10} n={p['n']:3} own best weight={b['weight']:.4f} rss={b['rss']:.2f} "
              f"median |rel|={b['residual_median_abs_rel']:.4f}")
        for g in p["grid"]:
            rates = " ".join(f"{f}={v:.3f}" for f, v in g["rates"].items())
            print(f"      w={g['weight']:.3f} rss={g['rss']:8.2f} (+{g['rss'] - b['rss']:6.2f}) "
                  f"median |rel|={g['residual_median_abs_rel']:.4f}  {rates}")
    print("\n2. Can each fit tell its own weight from the others'?")
    for label, c in cons.items():
        lo, hi = c["rss_bootstrap_interval"]
        print(f"   {label:10} own weight {c['own_weight']:.4f}, rss {c['rss']:.2f}, bootstrap rss "
              f"80% interval [{lo:.2f}, {hi:.2f}] (spread {c['rss_bootstrap_spread']:.2f})")
        for o, r in c["against"].items():
            print(f"      at {o}'s {r['weight']:.4f}: rss +{r['rss_increase']:.2f} "
                  f"({r['increase_over_spread']:.2f} of the spread), fits as well in "
                  f"{r['share_of_resamples_fitting_as_well']:.1%} of resamples: {r['verdict'].upper()}")
    print(f"\n3. Pooled: one weight across {', '.join(pool['per_fit'])}, rates per fit")
    print(f"   weight={pool['weight']:.4f} [{pool['interval'][0]:.4f}, {pool['interval'][1]:.4f}] "
          f"({pool['resamples']} resamples; {pool['draws_at_zero']} at 0, "
          f"{pool['draws_at_upper_bound']} at the search bound {SEARCH[1]})")
    for label, r in pool["per_fit"].items():
        rates = " ".join(f"{f}={v:.3f}" for f, v in r["rates"].items())
        print(f"      {label:10} rss={r['rss']:.2f} median |rel|={r['residual_median_abs_rel']:.4f}  {rates}")
    v = pool["variance_weighted"]
    print("   variance-weighted (each fit's RSS over its own residual variance "
          + ", ".join(f"{k} {s2:.3f}" for k, s2 in v["residual_variance"].items()) + "):")
    print(f"   weight={v['weight']:.4f} [{v['interval'][0]:.4f}, {v['interval'][1]:.4f}] "
          f"({v['draws_at_zero']} at 0, {v['draws_at_upper_bound']} at the search bound)")
    for label, r in v["per_fit"].items():
        rates = " ".join(f"{f}={x:.3f}" for f, x in r["rates"].items())
        print(f"      {label:10} rss={r['rss']:.2f} median |rel|={r['residual_median_abs_rel']:.4f}  {rates}")
    if not sens:
        return
    labels = list(sens)
    print("\n4. Published figures at each weight (change from " + labels[0] + ")")
    print("   " + " " * 50 + "".join(f"{lab:>26}" for lab in labels))
    for k in sens[labels[0]]["figures"]:
        cells = []
        for lab in labels:
            v = sens[lab]["figures"][k]
            ch = sens[lab]["change_from_" + labels[0]][k]
            cells.append(f"{'null' if v is None else f'{v:,.0f}':>15} "
                         f"{'' if ch is None else f'{ch:+.1%}':>10}")
        print(f"   {k:50}" + "".join(cells))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("masterrig", type=Path, help="a masterrig stretch file, as tools/model_rates.py takes")
    ap.add_argument("--json", type=Path, help="write the same figures as JSON")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--resamples", type=int, default=RESAMPLES)
    ap.add_argument("--no-publish", action="store_true", help="skip section 4")
    ap.add_argument("--publish-dir", type=Path, default=None,
                    help="where section 4 writes its published documents (default: a directory in /tmp)")
    ap.add_argument("--extra-weight", type=float, action="append", default=[],
                    help="another weight for section 4, beside 0, the pooled weight and 0.02; repeatable")
    ap.add_argument("--now", default=None,
                    help="the publish time for section 4, ISO 8601 (default: now); one time for every weight")
    a = ap.parse_args(argv)
    S = C.stretches_by_account(json.loads(Path("history/gs-passive.json").read_text(encoding="utf-8")),
                               json.loads(a.masterrig.read_text(encoding="utf-8")))
    kept = M.clean(S, C.harness_runs())
    data = {acct: M.prepare(acct, kept[acct]) for acct in S}
    G = groups(data)
    prof = profile(G)
    cons = consistency(G, prof, a.seed, a.resamples)
    pool = pooled(G, prof, a.seed, a.resamples)
    sens = None
    if not a.no_publish:
        now = datetime.fromisoformat(a.now) if a.now else datetime.now(timezone.utc)
        outdir = a.publish_dir or Path(tempfile.mkdtemp(prefix="cache-read-publish-", dir="/tmp"))
        weights = {"w=0": 0.0, f"pooled {pool['weight']:.4f}": round(pool["weight"], 4),
                   f"variance-weighted {pool['variance_weighted']['weight']:.4f}":
                       round(pool["variance_weighted"]["weight"], 4),
                   "w=0.02": 0.02}
        weights.update({f"w={w:g}": w for w in a.extra_weight})
        # Ascending, so w=0 stays first and every change is read from it.
        sens = sensitivity(dict(sorted(weights.items(), key=lambda kv: kv[1])), now, outdir)
    show(prof, cons, pool, sens)
    if a.json:
        a.json.write_text(json.dumps({
            "_meta": {"command": " ".join(["python3 -m tools.cache_read_profile", *(argv or sys.argv[1:])]),
                      "seed": a.seed, "resamples": a.resamples, "grid": GRID, "search": SEARCH,
                      "no_traffic": "read-only arithmetic over committed files; no account was driven"},
            "profile": prof, "consistency": cons, "pooled": pool, "sensitivity": sens},
            indent=1, default=float) + "\n")
        print(f"\nJSON -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
