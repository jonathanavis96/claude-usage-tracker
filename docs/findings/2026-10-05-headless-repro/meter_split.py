"""Which meter carries the headless factor (section 7 of the post), from the tracker's own
seven-day steps in data/tracker_steps.json. Python 3 and numpy only.

    python3 meter_split.py                # the post's section 7 figures, about a minute
    python3 meter_split.py --json out.json  # also every fit, as one JSON file

Data: the tracker's one-point seven-day steps of Max accounts 2, 3 and 4 from 14 Sep 12:00Z, as
its committed history held them on 11 Oct (`history-2026-10-11`; the 12 Oct history holds the
same steps), valued at the model rates of 12 Oct 00:02Z. Two windows: to 4 Oct 19:53Z (the
last transcript message behind the 5 Oct docs) and to 12 Oct 00:00Z.

Three fits share the same rows and columns (intercept, regime, account, headless share):
  ratio  quasi-Poisson of five-hour points per step (the five-hour per seven-day factor)
  (a)    the same with offset log(credits): five-hour points per credit
  (b)    Gamma log-link of credits per one-point step: seven-day points per credit = exp(-coef)
Factors are headless (share 1) over interactive (share 0); intervals are exp(b +/- 1.96 se)
with each fit's own dispersion.
"""
import collections
import itertools
import json
import math
import random
import sys
from datetime import datetime, timezone

import numpy as np

import common as K

SNAP = "history-2026-10-11"
RATES = "rates-2026-10-12"
T_DOC = datetime(2026, 10, 4, 19, 53, tzinfo=timezone.utc)
T_EXT = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
WINDOWS = {"14Sep-4Oct": T_DOC, "14Sep-11Oct": T_EXT}
ACCOUNTS = ("Max account 2", "Max account 3", "Max account 4")
CLASSES = K.CLASSES
FAMS = ("opus", "opus-5-5", "sonnet", "sonnet-5-5", "haiku", "fable")
V = K.valuations()
FAMILY = V["families"]
S = K.tracker_steps(SNAP)
EDGES = [K.CUT_AT] + sorted(K.ts(e) for e in S["regime_edges"] if K.ts(e) > K.CUT_AT)


def family(m):
    return FAMILY.get(m)


# ----------------------------------------------------------------------------------- rows

def regime(t):
    if t < EDGES[0]:
        return None
    k = 0
    for i, e in enumerate(EDGES):
        if t >= e:
            k = i
    return k


def _sub(a, b):
    out = {}
    for m in set(a) | set(b):
        ta, tb = a.get(m) or {}, b.get(m) or {}
        out[m] = {c: ta.get(c, 0) - tb.get(c, 0) for c in CLASSES}
    return out


def rows(accounts=ACCOUNTS, until=T_EXT):
    """One row per seven-day step from 14 Sep 12:00Z to `until`, inside one regime. `tok` holds
    raw tokens by source: head (a claude -p run's own turns) and non (everything else)."""
    out = []
    for n in accounts:
        for st in S["accounts"][n]["steps"]:
            a, b = K.ts(st["start"]), K.ts(st["end"])
            if a < K.CUT_AT or b > until:
                continue
            k = regime(a)
            if k is None or k != regime(b - (b - a) / 1e6):
                continue
            tok = K.as_dict(st["tokens"])
            head = K.as_dict(st["headless_tokens"] or {})
            out.append({"account": n, "regime": k, "start": a, "end": b,
                        "day": b.astimezone(timezone.utc).date().isoformat(),
                        "d5": int(st["d5"]), "d7": int(st["d7"]), "clean": st["clean"],
                        "cloud": st["cloud_session"], "tok": {"head": head, "non": _sub(tok, head)},
                        "raw_tokens": tok, "headless_turns": st["headless_turns"], "turns": st["turns"]})
    return out


def rate_set(kind):
    return RATES if kind == "fitted" else f"{RATES} all_at_list"


def valuation(kind):
    """Credits by class at the tracker's valuation ('fitted') or with every family at its
    list-price ratio ('all_at_list')."""
    rs = V["rate_sets"][rate_set(kind)]
    pairs, crr = rs["pairs"], rs["cache_read_rate"]
    whole = K.comparison_value(rate_set(kind))

    def pair(m):
        fam = family(m)
        return pairs.get(fam) if fam else None

    def classes(tok, cw1_mult=1.0, cr_rate=None):
        o = {"inp": 0.0, "cw5": 0.0, "cw1": 0.0, "out": 0.0, "cr": 0.0}
        for m, t in tok.items():
            if not any(t.get(c, 0) for c in ("input", "output", "cache_read", "cache_write")):
                continue
            p = pair(m)
            if p is None:
                return None
            o["inp"] += t["input"] * p[0]
            o["cw5"] += (t["cache_write"] - t["cache_write_1h"]) * p[0]
            o["cw1"] += t["cache_write_1h"] * p[0] * cw1_mult
            o["out"] += t["output"] * p[1]
            o["cr"] += t["cache_read"] * (crr if cr_rate is None else cr_rate * p[0])
        return o

    return classes, whole


# ----------------------------------------------------------------------------------- fits

def design(rs, share, extra=(), by_account=True, by_family=False):
    regs = sorted({r["regime"] for r in rs})[1:]
    accts = sorted({r["account"] for r in rs})[1:] if by_account else []
    fams = sorted({r["family"] for r in rs})[1:] if by_family else []
    cols = [np.ones(len(rs))]
    cols += [np.array([r["regime"] == k for r in rs], float) for k in regs]
    cols += [np.array([r["account"] == a for r in rs], float) for a in accts]
    cols += [np.array([r["family"] == f for r in rs], float) for f in fams]
    cols += [np.asarray(v, float) for v in extra]
    kept = []
    for c in cols:  # drop dummies that duplicate earlier columns; the share is always kept
        M = np.column_stack(kept + [c])
        if np.linalg.matrix_rank(M) == M.shape[1]:
            kept.append(c)
    kept.append(np.asarray(share, float))
    return np.column_stack(kept)


def poisson(y, X, offset=None, iters=100):
    y = np.asarray(y, float)
    off = np.zeros(len(y)) if offset is None else np.asarray(offset, float)
    b = np.zeros(X.shape[1])
    b[0] = math.log(max(y.sum(), 1e-12) / np.exp(off).sum())
    for _ in range(iters):
        mu = np.exp(off + X @ b)
        z = X @ b + (y - mu) / mu
        nb = np.linalg.solve((X * mu[:, None]).T @ X, (X * mu[:, None]).T @ z)
        if np.max(np.abs(nb - b)) < 1e-11:
            b = nb
            break
        b = nb
    mu = np.exp(off + X @ b)
    n, p = X.shape
    disp = float(((y - mu) ** 2 / mu).sum() / (n - p))
    inv = np.linalg.inv((X * mu[:, None]).T @ X)
    meat = (X * ((y - mu) ** 2)[:, None]).T @ X
    dev = float(2 * np.sum(np.where(y > 0, y * np.log(np.where(y > 0, y, 1) / mu), 0) - (y - mu)))
    return {"b": b, "cov": inv * disp, "cov_hc": inv @ meat @ inv, "disp": disp, "dev": dev}


def gamma_log(c, X, iters=100):
    c = np.asarray(c, float)
    b = np.zeros(X.shape[1])
    b[0] = math.log(c.mean())
    for _ in range(iters):
        mu = np.exp(X @ b)
        z = X @ b + (c - mu) / mu
        nb = np.linalg.lstsq(X, z, rcond=None)[0]
        if np.max(np.abs(nb - b)) < 1e-11:
            b = nb
            break
        b = nb
    mu = np.exp(X @ b)
    n, p = X.shape
    r = c / mu - 1
    disp = float((r ** 2).sum() / (n - p))
    inv = np.linalg.inv(X.T @ X)
    meat = (X * (r ** 2)[:, None]).T @ X
    return {"b": b, "cov": inv * disp, "cov_hc": inv @ meat @ inv, "disp": disp}


def factor(fit, j=-1, sign=1.0, cov="cov"):
    b = sign * fit["b"][j]
    se = math.sqrt(fit[cov][j, j])
    return [round(math.exp(b), 3), round(math.exp(b - 1.96 * se), 3), round(math.exp(b + 1.96 * se), 3)]


def three(rs, work, share, extra=(), by_account=True, by_family=False):
    X = design(rs, share, extra, by_account, by_family)
    d5 = np.array([r["d5"] for r in rs], float)
    w = np.asarray(work, float)
    R, A, B = poisson(d5, X), poisson(d5, X, offset=np.log(w)), gamma_log(w, X)
    f5, f7, fr = math.exp(A["b"][-1]), math.exp(-B["b"][-1]), math.exp(R["b"][-1])
    return {"n": len(rs), "ratio": factor(R), "ratio_hc": factor(R, cov="cov_hc"),
            "five_hour_per_work": factor(A), "five_hour_per_work_hc": factor(A, cov="cov_hc"),
            "seven_day_per_work": factor(B, sign=-1.0),
            "seven_day_per_work_hc": factor(B, sign=-1.0, cov="cov_hc"),
            "product_check": {"five_hour_over_seven_day": round(f5 / f7, 3), "ratio_fit": round(fr, 3),
                              "gap_pct": round(100 * ((f5 / f7) / fr - 1), 2)},
            "dispersion": {"ratio": round(R["disp"], 3), "five_hour": round(A["disp"], 3),
                           "seven_day_gamma": round(B["disp"], 3)}}


def linear_mixture(rs, work_non, work_head, y_kind, by_account=True):
    grid = np.exp(np.linspace(math.log(0.3), math.log(4.0), 281))
    wn, wh = np.asarray(work_non, float), np.asarray(work_head, float)
    X = design(rs, np.zeros(len(rs)), (), by_account)[:, :-1]
    d5 = np.array([r["d5"] for r in rs], float)
    prof, best = [], None
    for rho in grid:
        if y_kind == "ratio":
            f = poisson(d5, X, offset=np.log((wn + rho * wh) / (wn + wh)))
        elif y_kind == "d5":
            f = poisson(d5, X, offset=np.log(wn + rho * wh))
        else:
            f = poisson(np.ones(len(rs)), X, offset=np.log(wn + rho * wh))
        prof.append((rho, f["dev"], f["disp"]))
        if best is None or f["dev"] < best[1]:
            best = (rho, f["dev"], f["disp"])
    rho0, dev0, disp0 = best
    inside = [r for r, d, _ in prof if d <= dev0 + 3.84 * disp0]
    return {"rho": round(float(rho0), 3), "interval": [round(float(min(inside)), 3), round(float(max(inside)), 3)]}


def day_bootstrap(rs, fn, draws=1000, seed=7):
    """Whole UTC days resampled within each account."""
    rng = random.Random(seed)
    blocks = {}
    for r in rs:
        blocks.setdefault(r["account"], {}).setdefault(r["day"], []).append(r)
    out = []
    for _ in range(draws):
        pick = []
        for days in blocks.values():
            ds = list(days.values())
            for _ in ds:
                pick.extend(rng.choice(ds))
        try:
            out.append(fn(pick))
        except (np.linalg.LinAlgError, ValueError, OverflowError, FloatingPointError):
            continue
    def q(xs):
        return [round(float(np.quantile(xs, 0.025)), 3), round(float(np.quantile(xs, 0.975)), 3)]
    return {k: q([o[k] for o in out]) for k in out[0]} | {"draws": len(out)}


# ------------------------------------------------------------------------ class weights

CLS = ("inp", "w5", "w1", "out", "cr")


def class_arrays(rs, rate_kind="fitted"):
    """Per step and source, tokens by class weighted by the family's fitted input rate
    ('fitted') or unweighted ('none', for single-model steps with family terms)."""
    pairs = V["rate_sets"][RATES]["pairs"]
    out = {s: {c: np.zeros(len(rs)) for c in CLS} for s in ("head", "non")}
    for i, r in enumerate(rs):
        for s in ("head", "non"):
            for m, t in r["tok"][s].items():
                if not any(t.get(c, 0) for c in ("input", "output", "cache_read", "cache_write")):
                    continue
                k = 1.0 if rate_kind == "none" else pairs[family(m)][0]
                out[s]["inp"][i] += t["input"] * k
                out[s]["w5"][i] += (t["cache_write"] - t["cache_write_1h"]) * k
                out[s]["w1"][i] += t["cache_write_1h"] * k
                out[s]["out"][i] += t["output"] * k
                out[s]["cr"][i] += t["cache_read"] * k
    return out


def work(A, theta, wout=5.0):
    w5, w1, wcr = theta
    def f(s):
        return A[s]["inp"] + w5 * A[s]["w5"] + w1 * A[s]["w1"] + wout * A[s]["out"] + wcr * A[s]["cr"]
    return f("non"), f("head")


def profile(rs, A, grid):
    out = []
    d5 = np.array([r["d5"] for r in rs], float)
    for theta in grid:
        wn, wh = work(A, theta)
        W = wn + wh
        X = design(rs, wh / W, (), True, False)
        a = poisson(d5, X, offset=np.log(W))
        b = poisson(np.ones(len(rs)), X, offset=np.log(W))
        out.append({"theta": theta, "d5_dev": a["dev"], "d5_disp": a["disp"], "d7_dev": b["dev"],
                    "d7_disp": b["disp"], "f5": math.exp(a["b"][-1]), "f7": math.exp(b["b"][-1])})
    return out


def summarise(prof, which, df=3):
    """Best grid point and the region inside the chi-square(3) 95% bound (deviance difference
    over dispersion), with the range of (a) and (b) over that region."""
    crit = {1: 3.84, 2: 5.99, 3: 7.81}[df]
    if which == "joint":
        s5 = min(prof, key=lambda p: p["d5_dev"])["d5_disp"]
        s7 = min(prof, key=lambda p: p["d7_dev"])["d7_disp"]
        def key(p):
            return p["d5_dev"] / s5 + p["d7_dev"] / s7
        p0 = min(prof, key=key)
        inside = [p for p in prof if key(p) <= key(p0) + crit]
    else:
        k = f"{which}_dev"
        p0 = min(prof, key=lambda p: p[k])
        inside = [p for p in prof if p[k] <= p0[k] + crit * p0[f"{which}_disp"]]
    def rng(key):
        return [round(min(p[key] for p in inside), 3), round(max(p[key] for p in inside), 3)]
    return {"best": {"w5": p0["theta"][0], "w1": p0["theta"][1], "wcr": p0["theta"][2],
                     "f5": round(p0["f5"], 3), "f7": round(p0["f7"], 3)},
            "region_points": len(inside), "f5_range": rng("f5"), "f7_range": rng("f7")}


GRID = list(itertools.product([0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5],
                              [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0],
                              [0.0, 0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02, 0.03, 0.05, 0.1]))


# ------------------------------------------------------------------------------ blocks

def priced(rs, kind="fitted", cw1=1.0, cr=None, cw5=1.0):
    classes, whole = valuation(kind)
    out = []
    for r in rs:
        cn, ch = classes(r["tok"]["non"], cw1, cr), classes(r["tok"]["head"], cw1, cr)
        if cn is None or ch is None:
            continue
        cn["cw5"] *= cw5
        ch["cw5"] *= cw5
        tot = sum(cn.values()) + sum(ch.values())
        if tot <= 0:
            continue
        if cw1 == 1.0 and cr is None and cw5 == 1.0:
            w = whole(r["raw_tokens"])
            assert w is not None and abs(w - tot) <= 1e-6 * w
        out.append(dict(r, C=tot, Ch=sum(ch.values()), Cn=sum(cn.values()), cls_n=cn, cls_h=ch))
    return out


def main_fit(rs, extra=(), by_account=True):
    return three(rs, [r["C"] for r in rs], [r["Ch"] / r["C"] for r in rs], extra, by_account)


def fam_shares(rs):
    classes, _ = valuation("fitted")
    out = {f: [] for f in FAMS}
    for r in rs:
        by = collections.Counter()
        for s in ("head", "non"):
            for m, t in r["tok"][s].items():
                c = classes({m: t})
                if c:
                    by[family(m)] += sum(c.values())
        tot = sum(by.values())
        for f in FAMS:
            out[f].append(by[f] / tot)
    return out


def single_family(rs, thresh=0.999):
    out = []
    for r in rs:
        fams = collections.Counter()
        for s in ("head", "non"):
            for m, t in r["tok"][s].items():
                v = t["input"] + t["cache_write"] + 5 * t["output"]
                if v > 0:
                    fams[family(m)] += v
        tot = sum(fams.values())
        if not tot:
            continue
        f, v = fams.most_common(1)[0]
        if v / tot >= thresh:
            out.append(dict(r, family=f))
    return out


def within_account_regime(rs):
    """Steps of one account in one regime, share under 0.2 against over 0.8: no model."""
    out = {}
    g = collections.defaultdict(list)
    for r in rs:
        h = r["Ch"] / r["C"]
        g[(r["account"], r["regime"], "lo" if h < 0.2 else ("hi" if h > 0.8 else "mid"))].append(r)
    for (a, k, b) in sorted(g):
        if b != "lo" or (a, k, "hi") not in g:
            continue
        def f(v):
            return {"steps": len(v), "days": sorted({r["day"] for r in v}),
                               "k_credits_per_7d_point": round(sum(r["C"] for r in v) / len(v) / 1e3),
                               "k_credits_per_5h_point": round(sum(r["C"] for r in v) / sum(r["d5"] for r in v) / 1e3),
                               "five_hour_points_per_7d_point": round(sum(r["d5"] for r in v) / len(v), 2)}
        L, H = f(g[(a, k, "lo")]), f(g[(a, k, "hi")])
        out[f"{a}, regime {k}"] = {
            "interactive (share<0.2)": L, "headless (share>0.8)": H,
            "seven_day_factor": round(L["k_credits_per_7d_point"] / H["k_credits_per_7d_point"], 3),
            "five_hour_factor": round(L["k_credits_per_5h_point"] / H["k_credits_per_5h_point"], 3),
            "ratio": round(H["five_hour_points_per_7d_point"] / L["five_hour_points_per_7d_point"], 3)}
    return out


def doc_buckets():
    """The 5 Oct five-hour doc's crude buckets, with that day's rates and the tool's valuation
    (every family at list ratio, cache reads at the fitted weight of each family's input),
    pooled and split at the 22 September edge. All 306 steps to 4 Oct."""
    rs_ = V["rate_sets"]["rates-2026-10-05 all_at_list"]
    pairs, crw = rs_["pairs"], rs_["cache_read_weight_of_opus_input"]

    def credits(tok):
        c = 0.0
        for m, t in tok.items():
            p = pairs.get(family(m)) if family(m) else None
            if p:
                c += (t["input"] + t["cache_write"]) * p[0] + t["output"] * p[1] + t["cache_read"] * p[0] * crw
        return c
    rs = []
    for r in rows(until=T_DOC):
        ch, cn = credits(r["tok"]["head"]), credits(r["tok"]["non"])
        if ch + cn > 0:
            rs.append((r, ch + cn, ch / (ch + cn)))
    block = {"steps": len(rs)}
    for scope, sel in (("all regimes", lambda r: True), ("regime 0 (14-22 Sep)", lambda r: r["regime"] == 0),
                       ("regimes 1-2 (from 22 Sep)", lambda r: r["regime"] > 0)):
        lo = [(r, c) for r, c, h in rs if h < 0.2 and sel(r)]
        hi = [(r, c) for r, c, h in rs if h > 0.8 and sel(r)]
        def f(g):
            return {"steps": len(g), "k_credits_per_7d_point": round(sum(c for _, c in g) / len(g) / 1e3),
                               "k_credits_per_5h_point": round(sum(c for _, c in g) / sum(r["d5"] for r, _ in g) / 1e3)}
        L, H = f(lo), f(hi)
        block[scope] = {"share<0.2": L, "share>0.8": H,
                        "seven_day_factor": round(L["k_credits_per_7d_point"] / H["k_credits_per_7d_point"], 3),
                        "five_hour_factor": round(L["k_credits_per_5h_point"] / H["k_credits_per_5h_point"], 3)}
    return block


def window(until):
    base = rows(until=until)
    W = {"steps_before_pricing": len(base)}
    for kind in ("fitted", "all_at_list"):
        P = priced(base, kind)
        for sel in ("all", "clean"):
            rs = [r for r in P if sel == "all" or r["clean"]]
            W[f"{kind}/{sel}"] = {"steps": len(rs), **{k: v for k, v in main_fit(rs).items() if k != "n"}}
    P = [r for r in priced(base, "fitted") if r["clean"]]
    W["within_account_and_regime (fitted/clean)"] = within_account_regime(P)
    sens = {}
    for cw1, cw5, cr, label in ((1, 1, None, "collector (writes 1x, reads at fitted rate)"),
                                (1, 1, 0.0, "reads 0"), (1, 1, 0.01, "reads 0.01 of input"),
                                (1, 1, 0.03, "reads 0.03 of input"), (1, 1, 0.1, "reads 0.1 (API)"),
                                (1.6, 1, None, "1h writes 1.6x"), (2, 1, None, "1h writes 2x"),
                                (2, 1.25, 0.1, "API ratios (5m 1.25x, 1h 2x, reads 0.1)")):
        for kind in ("fitted", "all_at_list"):
            rs = [r for r in priced(base, kind, cw1=cw1, cr=cr, cw5=cw5) if r["clean"]]
            fit = main_fit(rs)
            sens[f"{kind}: {label}"] = {"ratio": fit["ratio"], "five_hour_per_credit": fit["five_hour_per_work"],
                                        "seven_day_per_credit": fit["seven_day_per_work"]}
    W["fixed_weight_sensitivity (clean)"] = sens
    cs = {k: [(r["cls_n"][k] + r["cls_h"][k]) / r["C"] for r in P] for k in ("cw5", "cw1", "cr")}
    clsx = [cs[k] for k in ("cw5", "cw1", "cr")]
    fs = fam_shares(P)
    famx = [fs[f] for f in FAMS[1:]]
    cov = {}
    for name, extra in (("family credit shares (Opus 5 base)", famx), ("class credit shares (output base)", clsx),
                        ("family + class shares", famx + clsx)):
        fit = main_fit(P, extra)
        cov[name] = {"ratio": fit["ratio"], "five_hour_per_credit": fit["five_hour_per_work"],
                     "seven_day_per_credit": fit["seven_day_per_work"]}
    d5 = np.array([r["d5"] for r in P], float)
    R = poisson(d5, design(P, [r["Ch"] / r["C"] for r in P], clsx))
    cov["ratio with class shares: class coefficients (per full share)"] = {
        k: factor(R, j=-4 + i) for i, k in enumerate(("cw5", "cw1", "cr"))} | {"headless": factor(R)}
    W["share_covariates (fitted/clean)"] = cov
    prof = profile(P, class_arrays(P, "fitted"), GRID)
    W["free_class_weights_profile (fitted family rates, clean)"] = {
        "five_hour_alone": summarise(prof, "d5"), "seven_day_alone": summarise(prof, "d7"),
        "joint": summarise(prof, "joint")}
    sm = {}
    for thresh in (0.999, 0.95):
        rs = single_family([r for r in base if r["clean"]], thresh)
        sm[f"family_share>={thresh}"] = {
            "steps": len(rs), "families": dict(collections.Counter(r["family"] for r in rs)),
            "accounts": dict(collections.Counter(r["account"] for r in rs))}
    W["single_model_steps (clean)"] = sm
    W["linear_mixture_form (fitted/clean)"] = {
        k: linear_mixture(P, [r["Cn"] for r in P], [r["Ch"] for r in P], y)
        for k, y in (("ratio", "ratio"), ("five_hour_per_credit", "d5"), ("seven_day_per_credit", "d7"))}

    def boot(pick):
        res = main_fit(pick)
        f5, f7, fr = res["five_hour_per_work"][0], res["seven_day_per_work"][0], res["ratio"][0]
        return {"ratio": fr, "five_hour_per_credit": f5, "seven_day_per_credit": f7,
                "product_over_ratio": (f5 / f7) / fr}
    W["day_block_bootstrap_check (fitted/clean)"] = day_bootstrap(P, boot)
    loo = {}
    for drop in ACCOUNTS:
        fit = main_fit([r for r in P if r["account"] != drop])
        loo[f"without {drop}"] = {"steps": fit["n"], "ratio": fit["ratio"],
                                  "five_hour_per_credit": fit["five_hour_per_work"],
                                  "seven_day_per_credit": fit["seven_day_per_work"]}
    W["leave_one_account_out (fitted/clean)"] = loo
    return W


def main():
    res = {w: window(u) for w, u in WINDOWS.items()}
    res["doc_crude_buckets (5 Oct rates, tool valuation)"] = doc_buckets()
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as f:
            json.dump(res, f, indent=1, default=str)
    def f3(v):
        return "%.3f [%.3f, %.3f]" % tuple(v)
    print("Section 7 main table (tracker valuation, clean steps, regime and account terms):")
    for w in WINDOWS:
        m = res[w]["fitted/clean"]
        pc = m["product_check"]
        print(f"  {w}, {m['steps']} steps: ratio {f3(m['ratio'])}; (a) five-hour per credit "
              f"{f3(m['five_hour_per_work'])}; (b) seven-day per credit {f3(m['seven_day_per_work'])}; "
              f"(a)/(b) {pc['five_hour_over_seven_day']:.3f} ({pc['gap_pct']:+.1f}%)")
    d = res["14Sep-4Oct"]
    print("Ratio on every step to 4 Oct (not only clean): %s, %d steps"
          % (f3(d["fitted/all"]["ratio"]), d["fitted/all"]["steps"]))
    m3 = d["within_account_and_regime (fitted/clean)"]["Max account 3, regime 0"]
    L, H = m3["interactive (share<0.2)"], m3["headless (share>0.8)"]
    print("Max account 3, %s to %s, no model:" % (min(L["days"] + H["days"]), max(L["days"] + H["days"])))
    for name, g in (("interactive (share<0.2)", L), ("headless (share>0.8)", H)):
        print("  %-24s %d steps, %sk credits per 7d point, %sk per 5h point, %.2f 5h per 7d point"
              % (name, g["steps"], format(g["k_credits_per_7d_point"], ","), g["k_credits_per_5h_point"],
                 g["five_hour_points_per_7d_point"]))
    print("  per credit, headless moved the seven-day meter %.2fx and the five-hour meter %.2fx as far"
          % (m3["seven_day_factor"], m3["five_hour_factor"]))
    b = res["doc_crude_buckets (5 Oct rates, tool valuation)"]
    a, r0, r12 = b["all regimes"], b["regime 0 (14-22 Sep)"], b["regimes 1-2 (from 22 Sep)"]
    print("Doc's crude buckets (5 Oct rates): seven-day factor %.3f pooled; %d of %d headless-heavy steps "
          "after 22 Sep; %d of %d interactive steps before it; within 14-22 Sep the seven-day factor is %.2f"
          % (a["seven_day_factor"], r12["share>0.8"]["steps"], a["share>0.8"]["steps"],
             r0["share<0.2"]["steps"], a["share<0.2"]["steps"], r0["seven_day_factor"]))
    print("Token weights (to 4 Oct, clean): (a) five-hour / (b) seven-day")
    sens = d["fixed_weight_sensitivity (clean)"]
    for label, key in (("cache reads at 0", "reads 0"), ("cache reads at 0.01", "reads 0.01 of input"),
                       ("cache reads at 0.03", "reads 0.03 of input"), ("cache reads at 0.1", "reads 0.1 (API)"),
                       ("one-hour writes at 2x", "1h writes 2x"),
                       ("API ratios", "API ratios (5m 1.25x, 1h 2x, reads 0.1)")):
        s = sens["fitted: " + key]
        print("  %-22s %.3f / %.3f   (ratio %.3f)" % (label, s["five_hour_per_credit"][0],
                                                      s["seven_day_per_credit"][0], s["ratio"][0]))
    ratios = [v["ratio"][0] for w in WINDOWS for k, v in res[w]["fixed_weight_sensitivity (clean)"].items()]
    print("  the ratio across every valuation, both windows: %.2f to %.2f" % (min(ratios), max(ratios)))
    print("Weights the meters' own data prefer (best joint grid point): (a) / (b)")
    for w in WINDOWS:
        j = res[w]["free_class_weights_profile (fitted family rates, clean)"]["joint"]
        print("  %s: %.2f / %.2f; over the joint 95%% region (a) [%.2f, %.2f], (b) [%.2f, %.2f]"
              % (w, j["best"]["f5"], j["best"]["f7"], *j["f5_range"], *j["f7_range"]))
    cc = d["share_covariates (fitted/clean)"]["ratio with class shares: class coefficients (per full share)"]
    print("Token-class shares in the ratio fit (to 4 Oct): headless %s; 5-minute writes %s, 1-hour writes %s, "
          "cache reads %s" % (f3(cc["headless"]), f3(cc["cw5"]), f3(cc["cw1"]), f3(cc["cr"])))
    print("Single-model steps (one family at least 99.9%% of the work), to 4 Oct: %d"
          % d["single_model_steps (clean)"]["family_share>=0.999"]["steps"])


if __name__ == "__main__":
    main()
