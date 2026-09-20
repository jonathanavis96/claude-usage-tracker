"""Reconcile the three instruments for the Max 20x five-hour window in credits per 1%.

Read-only analysis over history/gs-passive.json and a masterrig stretch file in the
same record shape. Prints (1) the Fable-free window from pure-Opus stretches, (2) Fable's
credit rate solved per Fable-heavy stretch against that window, (3) the five-hour window
before and after 2026-09-14 on every account, and (4) jwork against Dave across a range
of assumed Fable rates. Stretches that overlap a harness run on the same account (a probe
row in history/probes.jsonl, or the 2026-09-09 effort-matrix run on jwork recorded in
data/effort_matrix.json _meta) are excluded; gs stretches must also be capture-accepted.

    python3 tools/reconcile_window.py history/masterrig-passive.json
"""
from __future__ import annotations

import json
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path

RATES = {"claude-opus-5": (10 / 15, 50 / 15), "claude-opus-4-8": (10 / 15, 50 / 15),
         "claude-sonnet-5": (6 / 15, 30 / 15), "claude-haiku-4-5": (2 / 15, 10 / 15)}
CUT = datetime.fromisoformat("2026-09-14T12:00:00+00:00")
P = datetime.fromisoformat


def harness_runs() -> list[tuple[str, datetime, datetime]]:
    meta = json.load(open("data/effort_matrix.json"))["_meta"]
    runs = [("jwork", P(meta["started"]), P(meta["finished"]))]
    for line in open("history/probes.jsonl"):
        if line.strip():
            r = json.loads(line)
            runs.append((r["account"], P(r["ts"]), P(r["ts"]) + timedelta(seconds=r.get("elapsed_s", 3600))))
    return runs


def load(masterrig: Path) -> dict[str, list[dict]]:
    gs = json.load(open("history/gs-passive.json"))["accounts"]
    out = {name: body["stretches"] for name, body in gs.items()}
    body = json.load(open(masterrig))
    out["masterrig"] = body["stretches"] if "stretches" in body else body["accounts"]["masterrig"]["stretches"]
    return out


def clean(stretches: list[dict], account: str, runs) -> list[tuple[dict, float, dict]]:
    keep = []
    for s in stretches:
        d = s.get("delta_pct") or 0
        t = s.get("tokens") or {}
        if d < 3 or not t:
            continue
        s0, s1 = P(s["start"]), P(s["end"])
        if any(a == account and not (s1 < h0 or s0 > h1) for a, h0, h1 in runs):
            continue
        if account != "masterrig" and s.get("status") != "accepted":
            continue
        keep.append((s, d, t))
    return keep


def split(tokens: dict):
    """(priced ok, credits of models with a published rate, fable input, fable output, raw tokens); cache reads at 0."""
    known = fi = fo = raw = 0
    ok = True
    for m, tok in tokens.items():
        if not isinstance(tok, dict):
            continue
        i = tok.get("input", 0) + tok.get("cache_write", 0)
        o = tok.get("output", 0)
        raw += i + o
        if "fable" in m:
            fi += i
            fo += o
        elif m in RATES:
            known += i * RATES[m][0] + o * RATES[m][1]
        else:
            ok = False
    return ok, known, fi, fo, raw


def main() -> int:
    S = load(Path(sys.argv[1]))
    runs = harness_runs()
    C = {a: clean(S[a], a, runs) for a in S}
    print("1. Pure-Opus stretches (no Fable, no fitted rate), credits per 1%, cache reads 0")
    W = None
    for a in C:
        for era, f in (("pre", lambda s: P(s["start"]) < CUT), ("post", lambda s: P(s["start"]) >= CUT)):
            v = []
            for s, d, t in C[a]:
                ok, known, fi, fo, raw = split(t)
                if f(s) and ok and fi + fo == 0 and all(m.startswith("claude-opus") for m in t):
                    v.append(known / d)
            if v:
                v.sort()
                print(f"   {a:9} {era:4} n={len(v):2} median={st.median(v):9,.0f} range={v[0]:,.0f}-{v[-1]:,.0f}")
                if a == "jwork" and era == "pre":
                    W = st.median(v)
    W = W or 197000
    print(f"\n2. Fable input credits per token solved per Fable-heavy stretch, W={W:,.0f}")
    for k in (3, 5):
        for a in C:
            v = []
            for s, d, t in C[a]:
                ok, known, fi, fo, raw = split(t)
                if ok and raw and (fi + fo) / raw >= 0.5:
                    v.append((d * W - known) / (fi + k * fo))
            if v:
                v.sort()
                n = len(v)
                print(f"   output={k}x {a:9} n={n:3} median={st.median(v):5.2f} p25={v[n // 4]:5.2f} p75={v[3 * n // 4]:5.2f} ({st.median(v) / (10 / 15):.2f}x Opus)")
    print("\n3. All clean stretches, Fable 1.667/5.0, before and after 2026-09-14")
    for a in C:
        for era, f in (("pre", lambda s: P(s["start"]) < CUT), ("post", lambda s: P(s["start"]) >= CUT)):
            v = [(known + fi * 1.667 + fo * 5) / d for s, d, t in C[a] if f(s)
                 for ok, known, fi, fo, raw in [split(t)] if ok]
            if v:
                print(f"   {a:9} {era:4} n={len(v):3} median={st.median(v):9,.0f}")
    print("\n4. jwork against Dave across assumed Fable rates (output 3x), cache reads 0")
    for fr in (0.667, 1.0, 1.333, 1.667, 2.0, 2.5):
        med = {}
        for a in ("jwork", "dave"):
            v = [(known + fi * fr + fo * 3 * fr) / d for s, d, t in C[a] for ok, known, fi, fo, raw in [split(t)] if ok]
            med[a] = st.median(v)
        print(f"   fable={fr:5.3f} jwork={med['jwork']:9,.0f} dave={med['dave']:9,.0f} ratio={med['jwork'] / med['dave']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
