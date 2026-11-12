"""Every figure of the post that comes from the per-step table (data/steps.csv), section by
section, plus the few that read the tracker's own steps (data/tracker_steps.json). Python 3
and numpy only; about half a minute.

    python3 steps_checks.py            # all sections
    python3 steps_checks.py 5d 6       # only those sections (1 2 3 4 5b 5d 5e 6)

Model, as tools/headless_hypotheses.py fits it: five-hour points per one-point seven-day step
~ quasi-Poisson(log link) with an intercept, regime and account dummies, any added variables,
and the step's headless share last. Factors are exp(coefficient); intervals exp(b +/- 1.96 se)
with se scaled by the Pearson dispersion. Continuous added variables are standardised (factor
per standard deviation); shares are not. The headless share leaves cache reads out
(`headless_share`) unless a line says it counts them (`headless_share_incl_cache_reads`, the
hypotheses run's share).
"""
import math
import random
import sys
from collections import defaultdict

import numpy as np

import common as K
import meter_split as M

ALL = K.steps_csv()
DOC = [r for r in ALL if r["original_window"] == "1"]
WANT = set(sys.argv[1:])
def F(r, c):
    return float(r[c])
H = "headless_share"
HI = "headless_share_incl_cache_reads"


def section(name, title):
    on = not WANT or name in WANT
    if on:
        print(f"\n== {name}. {title}")
    return on


def fit(rows, extra=(), share=H, regimes=True, accounts=True, standardise=True):
    """Returns the fit and its columns' names; `extra` is [(name, fn, continuous)]."""
    cols = [np.ones(len(rows))]
    if regimes:
        cols += K.dummies(rows, "regime")
    if accounts:
        cols += K.dummies(rows, "account")
    for _, fn, cont in extra:
        x = np.array([fn(r) for r in rows], float)
        cols.append((x - x.mean()) / x.std() if cont and standardise else x)
    if share:
        cols.append(np.array([F(r, share) for r in rows]))
    return K.quasi_poisson([F(r, "five_hour_points") for r in rows], np.column_stack(cols))


def headless(rows, extra=(), **kw):
    return K.fmt(K.factor(fit(rows, extra, **kw)))


def var(rows, extra, i=-2, **kw):
    """Factor of the added variable at column i (the share is last)."""
    return K.fmt(K.factor(fit(rows, extra, **kw), i))


def day_block(rows, extra=(), draws=2000, seed=20261023):
    """Whole UTC days (by step end, all accounts together) resampled with replacement."""
    days = defaultdict(list)
    for r in rows:
        days[r["end_utc"][:10]].append(r)
    keys = sorted(days)
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        f = fit([r for _ in keys for r in days[rng.choice(keys)]], extra)
        out.append([math.exp(v) for v in f["b"][-1 - len(extra):]])
    def q(xs):
        return (round(sorted(xs)[int(0.025 * draws)], 2), round(sorted(xs)[int(0.975 * draws) - 1], 2))
    return len(keys), [q([o[i] for o in out]) for i in range(1 + len(extra))]


# the ten covariates of the 5 Oct covariate run (peak concurrency is left out of "all together")
def LOG(c):
    return (lambda r: math.log(F(r, c)))
COVS = [
    ("concurrency, mean (log)", LOG("concurrency_mean"), True),
    ("concurrency, peak (log)", LOG("concurrency_peak"), True),
    ("credits per active minute (log)", LOG("credits_per_active_minute"), True),
    ("busiest minute's share", lambda r: F(r, "peak_minute_share"), True),
    ("Opus 5.5 share", lambda r: F(r, "share_opus_5_5"), False),
    ("Sonnet share", lambda r: F(r, "share_sonnet"), False),
    ("Fable share", lambda r: F(r, "share_fable"), False),
    ("effort (low 1 to max 5)", lambda r: F(r, "effort_score"), True),
    ("peak hours (weekday 12-18 UTC) share", lambda r: F(r, "us_morning_12_18z_share"), False),
    ("weekday 08-12 UTC share", lambda r: F(r, "uk_morning_08_12z_share"), False),
    ("weekend share", lambda r: F(r, "weekend_share"), False),
]
TEN = [c for c in COVS if not c[0].startswith("concurrency, peak")]
# section 5e's "everything at once" used a hand rebuild of those ten in which the Sonnet share
# also counts Sonnet 5.5; that is the rebuild the post compares with the tool's figure
TEN_5E = [c if c[0] != "Sonnet share" else
          ("Sonnet share", lambda r: F(r, "share_sonnet") + F(r, "share_sonnet_5_5"), False) for c in TEN]
def PER_M(c):
    return (lambda r: F(r, c) / F(r, "credits") * 1e6)
RUNS = ("runs per million credits", PER_M("headless_runs"), True)
SUB = ("sub-agent share", lambda r: F(r, "subagent_share_incl_cache_reads"), False)
TURNS = ("turns per million credits (log)", lambda r: math.log(PER_M("turns")(r)), True)
SESS = ("sessions per million credits (log)", lambda r: math.log(PER_M("sessions")(r)), True)

# five-hour window resets inside each step, from the tracker's recorded five-hour windows
_snap = K.tracker_steps("history-2026-10-11")["accounts"]
_ends = {a: sorted(K.ts(w["window_ending"]) for w in body["five_hour_windows"]) for a, body in _snap.items()}
for r in ALL:
    a, b = K.ts(r["start_utc"]), K.ts(r["end_utc"])
    r["resets"] = sum(1 for t in _ends[r["account"]] if a < t <= b)
RESETS = ("five-hour window resets in the step", lambda r: r["resets"], True)

def heavy(rs, s=HI):
    return [r for r in rs if F(r, s) > 0.8]
def light(rs, s=HI):
    return [r for r in rs if F(r, s) < 0.2]

if section("1", "Data"):
    for a in sorted({r["account"] for r in DOC}):
        n = [r for r in DOC if r["account"] == a]
        print(f"  {a}: {len(n)} steps, {len(heavy(n))} over 80% headless")
    print(f"  all: {len(DOC)} steps, {len(heavy(DOC))} over 80% headless (share counting cache reads)")
    m4 = sorted((r["start_utc"], r["end_utc"]) for r in heavy(DOC) if r["account"] == "Max account 4")
    print(f"  Max account 4's headless-heavy steps run from {m4[0][0]} to {m4[-1][1]}")
    print("  highest recorded five-hour window, 14 Sep to 11 Oct (tracker's windows):")
    for a, body in sorted(_snap.items()):
        if a == "Max account 1":
            continue
        pk = [w["five_hour_pct"] for w in body["five_hour_windows"]
              if "2026-09-14T12:00" <= w["window_ending"] < "2026-10-12" and w["five_hour_pct"] is not None]
        print(f"    {a}: {max(pk):.0f}%")
    print("  plan check, five-hour points per seven-day point over every tracker step inside one period,"
          " 14 Sep 12:00Z to 12 Oct 00:00Z:")
    for a in sorted(_snap):
        st = M.rows(accounts=(a,), until=M.T_EXT)
        print(f"    {a}: {sum(s['d5'] for s in st) / len(st):.2f} ({len(st)} steps)")

if section("2", "Method: what 'interactive' holds"):
    for name, rs in (("steps under 0.2 headless", light(DOC)), ("steps over 0.8 headless", heavy(DOC))):
        print(f"  {name} ({len(rs)}): mean credit shares sub-agent "
              f"{np.mean([F(r, 'subagent_share_incl_cache_reads') for r in rs]):.2f}, interactive main "
              f"{np.mean([F(r, 'interactive_share_incl_cache_reads') for r in rs]):.2f}, headless "
              f"{np.mean([F(r, HI) for r in rs]):.2f}")

if section("3", "Main result"):
    print(f"  headless factor: {headless(DOC)}, {len(DOC)} steps")
    print(f"  same, share counting cache reads: {headless(DOC, share=HI)}")
    rng = np.random.default_rng(20261023)
    s = np.array([F(r, H) for r in DOC])
    y = np.array([F(r, "five_hour_points") for r in DOC])
    means = []
    for name, m in (("0 to 0.15", s <= 0.15), ("0.15 to 0.5", (s > 0.15) & (s <= 0.5)),
                    ("0.5 to 0.9", (s > 0.5) & (s <= 0.9)), ("0.9 to 1", s > 0.9)):
        yy = y[m]
        boots = [yy[rng.integers(0, len(yy), len(yy))].mean() for _ in range(4000)]
        means.append(yy.mean())
        print(f"  share {name:11s} {m.sum():3d} steps: {yy.mean():.2f} [{np.percentile(boots, 2.5):.2f}, "
              f"{np.percentile(boots, 97.5):.2f}]  (bootstrap within the bin, 4,000 draws, seed 20261023)")
    print(f"  raw end-to-end ratio {means[-1]:.2f} / {means[0]:.2f} = {means[-1] / means[0]:.2f}")
    print(f"  plain means to 4 Oct: {len(heavy(DOC, H))} steps over 0.8 headless average "
          f"{np.mean([F(r, 'five_hour_points') for r in heavy(DOC, H)]):.2f}, {len(light(DOC, H))} under 0.2 "
          f"average {np.mean([F(r, 'five_hour_points') for r in light(DOC, H)]):.2f}")

if section("4", "Per account (share counting cache reads, as the hypotheses run)"):
    for a in sorted({r["account"] for r in DOC}):
        n = [r for r in DOC if r["account"] == a]
        print(f"  {a}: {headless(n, share=HI, accounts=False)}, {len(n)} steps, mean headless share "
              f"{np.mean([F(r, HI) for r in n]):.2f}, {len(heavy(n))} steps over 80% headless")

if section("5b", "Per-run startup overhead"):
    hv = heavy(DOC)
    runs = sum(F(r, "headless_runs") for r in hv)
    print(f"  {len(hv)} headless-heavy steps hold {runs / len(hv):.1f} runs each, at "
          f"{sum(F(r, 'credits') for r in hv) / runs / 1e3:.1f}k credits per run")
    sd = np.std([PER_M("headless_runs")(r) for r in DOC])
    print(f"  runs per million credits, per SD ({sd:.1f}): {var(DOC, [RUNS])}; headless beside it {headless(DOC, [RUNS])}")

if section("5d", "Everything else that travels with headless work"):
    print(f"  {'added variable':38s} {'that variable':18s} {'headless beside it':18s} that variable alone")
    for c in COVS:
        print(f"  {c[0]:38s} {var(DOC, [c]):18s} {headless(DOC, [c]):18s} {var(DOC, [c], i=-1, share=None)}")
    print(f"  {'all together (peak concurrency out)':38s} {'':18s} {headless(DOC, TEN)}")
    j = [c[0] for c in TEN].index(COVS[8][0])
    print(f"  peak hours with all ten in: {var(DOC, TEN, i=-1 - len(TEN) + j)}")
    for name, rs in (("to 4 Oct", DOC), ("to 11 Oct", ALL)):
        n, (us, h) = day_block(rs, [COVS[8]])
        print(f"  peak hours beside headless, {name}, day-block bootstrap over {n} days: "
              f"{K.factor(fit(rs, [COVS[8]]), -2)[0]:.2f} [{us[0]:.2f}, {us[1]:.2f}]; headless beside it "
              f"{K.factor(fit(rs, [COVS[8]]))[0]:.2f}")
    print(f"  extended to 11 Oct, all together: {headless(ALL, TEN)}")
    for col, lab in (("concurrency_mean", "mean"), ("concurrency_peak", "peak")):
        print(f"  median step concurrency ({lab}): over 0.8 headless {np.median([F(r, col) for r in heavy(DOC, H)]):.1f}, "
              f"under 0.2 {np.median([F(r, col) for r in light(DOC, H)]):.1f}")

if section("5e", "Request counts, sub-agents and window resets"):
    for c in (TURNS, SESS):
        print(f"  {c[0]:36s} {var(DOC, [c])}  headless beside it {headless(DOC, [c])}")
    print(f"  {'sub-agent share (interactive main base)':36s} {var(DOC, [SUB], share=HI)}  headless beside it "
          f"{headless(DOC, [SUB], share=HI)}")
    print(f"  {RESETS[0]:36s} {var(DOC, [RESETS])}  headless beside it {headless(DOC, [RESETS])}")
    print(f"  all of 5d plus runs, sub-agent share and turns: {headless(DOC, TEN_5E + [RUNS, SUB, TURNS])}")
    print(f"  the ten-variable fit with the Sonnet share counting Sonnet 5.5: {headless(DOC, TEN_5E)}")
    for lab, f in (("turns per million credits", PER_M("turns")),
                   ("credits per turn (k)", lambda r: F(r, "credits") / F(r, "turns") / 1e3),
                   ("sessions per million credits", PER_M("sessions"))):
        print(f"  median {lab}: headless-heavy {np.median([f(r) for r in heavy(DOC, H)]):.3g}, "
              f"interactive {np.median([f(r) for r in light(DOC, H)]):.3g}")
    mi = [r for r in DOC if F(r, "subagent_share_incl_cache_reads") < 0.2 and F(r, HI) < 0.2]
    sa = [r for r in DOC if F(r, "subagent_share_incl_cache_reads") > 0.8]
    for lab, rs in (("mostly interactive main session", mi), ("mostly sub-agent", sa), ("mostly headless", heavy(DOC))):
        print(f"  {lab}: {len(rs)} steps, {np.mean([F(r, 'five_hour_points') for r in rs]):.2f} five-hour points per seven-day point")
    print(f"  resets per step: headless-heavy {np.mean([r['resets'] for r in heavy(DOC, H)]):.3f}, "
          f"interactive {np.mean([r['resets'] for r in light(DOC, H)]):.3f}")
    nr = [r for r in DOC if r["resets"] == 0]
    print(f"  steps with no reset: {headless(nr)}, {len(nr)} steps")

if section("6", "Robustness"):
    n, (h,) = day_block(DOC)
    print(f"  day-block bootstrap ({n} UTC days, 2,000 resamples): {K.factor(fit(DOC))[0]:.2f} [{h[0]:.2f}, {h[1]:.2f}]")
    print(f"  no period terms: {headless(DOC, regimes=False)}")
    r0 = [r for r in DOC if r["regime"] == "0"]
    print(f"  14 to 22 Sep only, one period: {headless(r0)}, {len(r0)} steps")
    for a in sorted({r["account"] for r in DOC}):
        rest = [r for r in DOC if r["account"] != a]
        print(f"  leave out {a}: {headless(rest)}, {len(rest)} steps")
    print(f"  interactive main sessions as the baseline: {headless(DOC, [SUB], share=HI)}")
    print(f"  headless run count added: {headless(DOC, [RUNS])}")
    print(f"  everything at once (5e): {headless(DOC, TEN_5E + [RUNS, SUB, TURNS])}")
    print(f"  extended to 11 Oct: {headless(ALL)}, {len(ALL)} steps")
    n, (h,) = day_block(ALL)
    print(f"  extended, day-block bootstrap ({n} days): {K.factor(fit(ALL))[0]:.2f} [{h[0]:.2f}, {h[1]:.2f}]")
    print(f"  extended, all covariates: {headless(ALL, TEN)}")
    print(f"  extended, interactive main sessions as the baseline: {headless(ALL, [SUB], share=HI)}")
    print(f"  extended, everything at once: {headless(ALL, TEN_5E + [RUNS, SUB, TURNS])}")
    for a in sorted({r["account"] for r in ALL}):
        n_ = [r for r in ALL if r["account"] == a]
        print(f"  extended, {a}: {headless(n_, share=HI, accounts=False)}, {len(n_)} steps")
    new4 = [r for r in ALL if r["account"] == "Max account 4" and r["original_window"] == "0"]
    print(f"  Max account 4's {len(new4)} new steps: highest headless share {max(F(r, HI) for r in new4):.3f}")
    oct7 = [r for r in M.priced(M.rows(accounts=("Max account 2",)))
            if r["start"].date().isoformat() == "2026-10-07" and r["C"] < 50000]
    print("  Max account 2 steps on 7 Oct that moved the seven-day meter a point on under 50k credits "
          "(tracker valuation): " + ", ".join(f"{r['C'] / 1e3:.1f}k" for r in oct7))
