"""Shared helpers for the reproduction scripts in this folder: data loading, the tracker's
credit valuation and the quasi-Poisson fit. Python 3 and numpy only."""
import csv
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CUT_AT = datetime.fromisoformat("2026-09-14T12:00:00+00:00")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def steps_csv():
    with open(DATA / "steps.csv", newline="") as f:
        return list(csv.DictReader(f))


def tracker_steps(snapshot: str) -> dict:
    """{"regime_edges": [...], "accounts": {"Max account N": {"steps": [...], "five_hour_windows": [...]}}}"""
    body = json.loads((DATA / "tracker_steps.json").read_text())
    return body["snapshots"][snapshot]


def valuations() -> dict:
    return json.loads((DATA / "valuations.json").read_text())


CLASSES = ("input", "output", "cache_read", "cache_write", "cache_write_1h")


def as_dict(tok: dict) -> dict:
    """[input, output, cache_read, cache_write, cache_write_1h] lists back to dicts."""
    return {m: dict(zip(CLASSES, v)) for m, v in (tok or {}).items()}


def comparison_value(rate_set: str):
    """The tracker's comparison valuation (tracker/credits.py `comparison_value`) at one
    resolved rate set: each model's family at its [input, output] credit rate, cache writes on
    the input side at 1x, every cache read at the pooled fit's one cache-read rate. A model with
    no family (or a family with no rate) leaves the bundle unpriced (None); a model with no
    tokens is skipped."""
    v = valuations()
    fams, rs = v["families"], v["rate_sets"][rate_set]
    pairs, crr, weight = rs["pairs"], rs["cache_read_rate"], rs["fallback_cache_read_weight"]

    def value(tokens: dict):
        total = 0.0
        for model, t in tokens.items():
            if not (t["input"] + t["output"] + t["cache_read"] + t["cache_write"]):
                continue
            fam = fams.get(model)
            pair = pairs.get(fam) if fam else None
            if pair is None:
                return None
            if crr is not None:
                total += (t["input"] + t["cache_write"]) * pair[0] + t["output"] * pair[1] + t["cache_read"] * crr
            else:
                total += (t["input"] + t["cache_write"] + t["cache_read"] * weight) * pair[0] + t["output"] * pair[1]
        return total
    return value


def quasi_poisson(y, X, offset=None, iters=100):
    """Poisson log-link by IRLS; standard errors scaled by the Pearson dispersion."""
    y = np.asarray(y, float)
    off = np.zeros(len(y)) if offset is None else np.asarray(offset, float)
    b = np.zeros(X.shape[1])
    b[0] = math.log(max(y.sum(), 1e-12) / np.exp(off).sum())
    for _ in range(iters):
        mu = np.exp(off + X @ b)
        z = X @ b + (y - mu) / mu
        nb = np.linalg.solve((X * mu[:, None]).T @ X, (X * mu[:, None]).T @ z)
        done = np.max(np.abs(nb - b)) < 1e-11
        b = nb
        if done:
            break
    mu = np.exp(off + X @ b)
    n, p = X.shape
    disp = float(((y - mu) ** 2 / mu).sum() / (n - p))
    cov = np.linalg.inv((X * mu[:, None]).T @ X) * disp
    return {"b": b, "se": np.sqrt(np.diag(cov)), "disp": disp, "mu": mu}


def factor(fit, j=-1, sign=1.0):
    b, se = sign * fit["b"][j], fit["se"][j]
    return math.exp(b), math.exp(b - 1.96 * se), math.exp(b + 1.96 * se)


def fmt(f, digits=2):
    return "%.*f [%.*f, %.*f]" % (digits, f[0], digits, f[1], digits, f[2])


def dummies(rows, key):
    levels = sorted({r[key] for r in rows})[1:]
    return [np.array([r[key] == k for r in rows], float) for k in levels]
