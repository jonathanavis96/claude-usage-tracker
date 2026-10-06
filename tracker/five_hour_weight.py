"""Headless work on the five-hour meter: the factor, measured at every publish, and the weight.

The five-hour meter moves about 1.5 times as far per credit for a headless `claude -p` run's
own turns as for interactive work, and the seven-day meter treats the two alike
(docs/findings/2026-10-04-account-agreement.md section 4e). No counting error explains it,
and nothing that travels with headless work absorbs it: concurrency, burstiness, model mix,
effort and time of day each leave it at 1.5x (docs/findings/2026-10-05-headless-five-hour.md).
So it is the meter's own behaviour. Jonathan's ruling (2026-10-05) is to use it: every
five-hour figure counts headless work at the measured factor, so an account's
headless-heavy period compares like for like with its interactive one. The seven-day meter
stays unweighted.

`headless_factor` fits the factor from the seven-day steps at every publish and never takes
it from a constant (ADR 0001 rule 12). It is a quasi-Poisson regression of the five-hour
points in each one-point seven-day step on regime, account and the step's headless share of
credits. The same fit runs per account. `interactive_equivalent` then rewrites each
stretch's tokens as interactive-equivalent tokens, adding (factor - 1) times its
`headless_tokens`. Every five-hour figure reads stretches, so each is quoted in that unit.
The seven-day steps are left as they are.
"""
from __future__ import annotations

import copy
import math
from bisect import bisect_right
from datetime import datetime, timedelta

from .weekly import _ratio_interval

#: Fewer clean steps than this, or a headless share that hardly varies, and an account's own
#: factor is not fitted (the pooled one still is).
MIN_STEPS = 20
MIN_SHARE_SD = 0.05
#: The pooled fit needs at least this many steps with a headless split recorded.
MIN_POOLED_STEPS = 50

METHOD = (
    "The five-hour meter's factor for headless `claude -p` work, measured at every publish "
    "from the one-point seven-day steps (`weekly_meter.seven_day_steps`, cleaned as the "
    "weekly limit cleans them): a quasi-Poisson regression of the five-hour points crossed in "
    "each step on regime (the 14 September cut and every family's first use), account and the "
    "step's headless share of credits, where headless means a `claude -p` run's own turns "
    "(entrypoint `sdk-cli`, sub-agents not counted). `value` is exp of the share's "
    "coefficient, the five-hour meter's movement per credit of headless work over interactive "
    "work; its interval is 1.96 standard errors with the fit's own dispersion. `per_account` "
    "is the same fit on each account alone. Every five-hour figure (the window, the five-hour "
    "change tests, the account lines) counts a stretch's headless tokens at this factor, so it "
    "is quoted in interactive-equivalent tokens and credits; the seven-day meter, which "
    "treats both kinds of work alike, is not weighted. An account whose collector recorded no "
    "headless split is counted as recorded, unweighted.")


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """a x = b by Gauss-Jordan with partial pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            raise ValueError("singular design")
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def _inverse(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    cols = [_solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def quasi_poisson(y: list[float], x: list[list[float]], iters: int = 50) -> dict:
    """Quasi-Poisson regression by IRLS: coefficients, standard errors (dispersion-scaled),
    the dispersion and the deviance."""
    n, p = len(y), len(x[0])
    mean = max(sum(y) / n, 1e-9)
    b = [math.log(mean)] + [0.0] * (p - 1)
    for _ in range(iters):
        eta = [sum(bi * xi for bi, xi in zip(b, row)) for row in x]
        mu = [math.exp(e) for e in eta]
        z = [e + (yi - m) / m for e, yi, m in zip(eta, y, mu)]
        xtwx = [[sum(mu[k] * x[k][i] * x[k][j] for k in range(n)) for j in range(p)] for i in range(p)]
        xtwz = [sum(mu[k] * x[k][i] * z[k] for k in range(n)) for i in range(p)]
        new = _solve(xtwx, xtwz)
        done = max(abs(u - v) for u, v in zip(new, b)) < 1e-10
        b = new
        if done:
            break
    mu = [math.exp(sum(bi * xi for bi, xi in zip(b, row))) for row in x]
    disp = sum((yi - m) ** 2 / m for yi, m in zip(y, mu)) / max(n - p, 1)
    xtwx = [[sum(mu[k] * x[k][i] * x[k][j] for k in range(n)) for j in range(p)] for i in range(p)]
    cov = _inverse(xtwx)
    se = [math.sqrt(max(cov[i][i] * disp, 0.0)) for i in range(p)]
    dev = 2 * sum((yi * math.log(yi / m) if yi > 0 else 0.0) - (yi - m) for yi, m in zip(y, mu))
    return {"coef": b, "se": se, "dispersion": disp, "deviance": dev}


def _rows(steps: dict[str, list[dict]], value, labels: dict[str, str],
          boundaries: list[datetime]) -> list[dict]:
    """One row per clean step with a headless split and credits: label, regime, d5, share."""
    edges = sorted(boundaries)
    out = []
    for name, rows in steps.items():
        for st in rows:
            if "headless_tokens" not in st:
                continue
            total = value(st.get("tokens") or {})
            head = value(st["headless_tokens"]) if st["headless_tokens"] else 0.0
            if not total or head is None:
                continue
            start, end = datetime.fromisoformat(st["start"]), datetime.fromisoformat(st["end"])
            k = bisect_right(edges, start)
            if k != bisect_right(edges, end - (end - start) / 1e6):
                continue  # spans a regime boundary
            out.append({"label": labels.get(name, name), "regime": k, "d5": float(st.get("d5", 0)),
                        "share": min(max(head / total, 0.0), 1.0)})
    return out


def _fit(rows: list[dict], by_account: bool) -> dict | None:
    regimes = sorted({r["regime"] for r in rows})[1:]
    accounts = sorted({r["label"] for r in rows})[1:] if by_account else []
    x = [[1.0] + [float(r["regime"] == k) for k in regimes]
         + [float(r["label"] == a) for a in accounts] + [r["share"]] for r in rows]
    try:
        fit = quasi_poisson([r["d5"] for r in rows], x)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    b, se = fit["coef"][-1], fit["se"][-1]
    return {"value": round(math.exp(b), 3),
            "interval": [round(math.exp(b - 1.96 * se), 3), round(math.exp(b + 1.96 * se), 3)],
            "dispersion": round(fit["dispersion"], 3), "deviance": round(fit["deviance"], 1)}


def _sd(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((v - m) ** 2 for v in xs) / len(xs))


def headless_factor(steps: dict[str, list[dict]], value, labels: dict[str, str],
                    boundaries: list[datetime]) -> dict:
    """The published `five_hour_meter` block: the pooled factor and each account's own.

    `steps` are clean seven-day steps by account name (`weekly_meter.clean_steps`), `value`
    the credit valuation the change tests use, `boundaries` the regime edges. `value` is null,
    with `status` saying why, when too few steps carry a headless split to fit it.
    """
    rows = _rows(steps, value, labels, boundaries)
    recorded = sorted({r["label"] for r in rows})
    missing = sorted(labels.get(n, n) for n, rs in steps.items()
                     if rs and labels.get(n, n) not in recorded)
    block = {"headless_factor": {"value": None, "interval": None, "status": None},
             "n_steps": len(rows), "accounts": recorded, "accounts_without_split": missing,
             "per_account": {}, "basis": "interactive_equivalent", "method": METHOD}
    pooled = _fit(rows, by_account=True) if len(rows) >= MIN_POOLED_STEPS else None
    if pooled is None:
        block["headless_factor"]["status"] = (
            f"not measured: {len(rows)} seven-day steps carry a headless split, fewer than "
            f"{MIN_POOLED_STEPS}, so no five-hour figure is weighted")
    else:
        block["headless_factor"].update(value=pooled["value"], interval=pooled["interval"],
                                        dispersion=pooled["dispersion"])
    for label in recorded:
        own = [r for r in rows if r["label"] == label]
        shares = [r["share"] for r in own]
        row = {"n_steps": len(own), "mean_headless_share": round(sum(shares) / len(shares), 3),
               "value": None, "interval": None}
        if len(own) >= MIN_STEPS and _sd(shares) >= MIN_SHARE_SD:
            fit = _fit(own, by_account=False)
            if fit:
                row.update(value=fit["value"], interval=fit["interval"])
        if row["value"] is None:
            row["status"] = (f"not fitted alone: {len(own)} steps, headless share sd "
                             f"{_sd(shares):.2f}")
        block["per_account"][label] = row
    return block


def factor_of(block: dict | None) -> float | None:
    return ((block or {}).get("headless_factor") or {}).get("value")


def _add_scaled(tokens: dict, extra: dict, scale: float) -> dict:
    out = copy.deepcopy(tokens)
    for model, by_class in extra.items():
        dest = out.setdefault(model, {})
        for cls, n in by_class.items():
            dest[cls] = round(dest.get(cls, 0) + n * scale)
    return out


def interactive_equivalent(report: dict | None, factor: float | None) -> dict | None:
    """A copy of a passive report whose stretches count headless tokens at `factor`.

    Each stretch's `tokens` becomes its tokens plus (factor - 1) times its `headless_tokens`.
    The stretch keeps its metered tokens as `metered_tokens` and the factor as
    `headless_weight`. Seven-day steps are not touched. With no factor, or a stretch with no
    split, the stretch is unchanged.
    """
    if not report or not factor:
        return report
    out = copy.copy(report)
    accounts = report.get("accounts")
    if not accounts:
        return report
    out["accounts"] = {}
    for name, body in accounts.items():
        body = dict(body)
        stretches = []
        for st in body.get("stretches") or []:
            if st.get("headless_tokens"):
                st = dict(st, metered_tokens=st.get("tokens"), headless_weight=factor,
                          tokens=_add_scaled(st.get("tokens") or {}, st["headless_tokens"],
                                             factor - 1))
            stretches.append(st)
        if "stretches" in body:
            body["stretches"] = stretches
        out["accounts"][name] = body
    return out


#: A five-hour meter window: a `by_window` point covers the five hours before its `window_ending`.
WINDOW_SPAN = timedelta(hours=5)

WINDOWS_METHOD = (
    "Windows per week in interactive-equivalent units (ADR 0001 rule 16): each five-hour meter "
    "window's movement (`raw_five_hour_pct`) over its headless inflation, 1 + (factor - 1) x the "
    "window's headless share of credits, with the factor fitted at this publish "
    "(`five_hour_meter.headless_factor`). The share is read from the account's seven-day steps "
    "that overlap the window's five hours, each counted by the part of its span inside them, "
    "valued as the factor's own fit values them; a window no step overlaps reads the stretches "
    "the same way; a window neither overlaps (`headless_share` null) is counted as recorded, "
    "unweighted. `five_hour_pct`, `windows` and `rounding_interval` are in that unit; the "
    "meter's own figures are kept as `raw_five_hour_pct`, `raw_windows` and "
    "`raw_rounding_interval`, a diagnostic. The seven-day meter is not weighted.")


def _spans(items: list[dict], value) -> list[tuple[datetime, datetime, float, float]]:
    """(start, end, credits, headless credits) of every item with a headless split and credits."""
    out = []
    for it in items or []:
        if "headless_tokens" not in it or not it.get("start") or not it.get("end"):
            continue
        total = value(it.get("tokens") or {})
        head = value(it["headless_tokens"]) if it["headless_tokens"] else 0.0
        if not total or head is None:
            continue
        out.append((datetime.fromisoformat(it["start"]), datetime.fromisoformat(it["end"]),
                    total, head))
    return sorted(out, key=lambda r: r[0])


def _share(spans: list[tuple], start: datetime, end: datetime) -> float | None:
    """Headless credits over credits in the spans overlapping (start, end], each counted by
    the fraction of its own span inside it; None when none overlaps."""
    total = head = 0.0
    for a, b, c, h in spans:
        if a >= end:
            break
        if b <= start:
            continue
        span = (b - a).total_seconds()
        f = 1.0 if span <= 0 else (min(b, end) - max(a, start)).total_seconds() / span
        total += f * c
        head += f * h
    return min(max(head / total, 0.0), 1.0) if total > 0 else None


def interactive_windows(points: list[dict], steps: list[dict], stretches: list[dict], value,
                        factor: float | None) -> list[dict]:
    """One account's `by_window` points in interactive-equivalent units (WINDOWS_METHOD).

    `steps` and `stretches` are the account's own seven-day steps and stretches with their
    metered `tokens` and `headless_tokens`; `value` is the credit valuation the factor was
    fitted with. Each point is copied with its five-hour movement, ratio and rounding interval
    divided by its headless inflation, the meter's own kept under `raw_*`, and its
    `headless_share` (null where nothing records one), `headless_share_source` and
    `headless_inflation`. With no factor nothing is weighted, and the fields are still there.
    """
    by_step, by_stretch = _spans(steps, value), _spans(stretches, value)
    out = []
    for p in points:
        end = datetime.fromisoformat(p["window_ending"])
        share, source = _share(by_step, end - WINDOW_SPAN, end), "seven_day_steps"
        if share is None:
            share, source = _share(by_stretch, end - WINDOW_SPAN, end), "stretches"
        inflation = 1 + (factor - 1) * share if factor and share is not None else 1.0
        d5, d7 = p["five_hour_pct"], p["seven_day_pct"]
        raw_iv = p.get("rounding_interval") or _ratio_interval(d5, d7, p.get("pieces", 1))
        out.append(dict(
            p, five_hour_pct=round(d5 / inflation, 3),
            windows=round(d5 / inflation / d7, 2) if d7 > 0 else None,
            rounding_interval=[round(x / inflation, 4) if x is not None else None for x in raw_iv],
            raw_five_hour_pct=d5, raw_windows=p.get("windows"), raw_rounding_interval=raw_iv,
            headless_share=round(share, 4) if share is not None else None,
            headless_share_source=source if share is not None else None,
            headless_inflation=round(inflation, 4)))
    return out
