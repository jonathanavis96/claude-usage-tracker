"""Reconcile the three instruments for the Max 20x five-hour window in credits per 1%.

Read-only analysis over history/gs-passive.json and a masterrig stretch file in the
same record shape. Prints (1) the Fable-free window from pure-Opus stretches, (2) Fable's
credit rate solved per Fable-heavy stretch against that window, (3) the five-hour window
before and after 2026-09-14 on every account, (4) jwork against Dave across a range
of assumed Fable rates, and (5) what that same window buys in tokens per class, the block
`tracker/publish.py` publishes as `credits.window_tokens` -- built by the publisher's own
code so this document and the page cannot state two different figures.
Stretches that overlap a harness run on the same account are
excluded, as history/harness-runs.jsonl records them -- the completed probes, the
2026-09-09 effort-matrix run on jwork, and the probes that aborted or crashed without
writing a probes.jsonl row; gs stretches must also be capture-accepted.

The rates, the exclusion rule and the stretch selection live in tracker/credits.py, which
the publisher imports too: the figures this prints and the figures the public JSON carries
come from one implementation, not from two that agree today. This file keeps masterrig's
exemption from the capture gate, which is the selection its published findings were
computed under; the publisher applies the gate to every account, which is tighter.

    python3 tools/reconcile_window.py history/masterrig-passive.json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import credits_report as R
from tracker import credits as C

CUT = C.CUT_AT
P = datetime.fromisoformat


#: Opus input credits per token, the scale the solved Fable rates are quoted against.
OPUS_INPUT = (C.rates("opus", C.load_credits()) or (1.0, 1.0))[0]


def load(masterrig: Path) -> dict[str, list[dict]]:
    gs = json.loads(Path("history/gs-passive.json").read_text(encoding="utf-8"))
    return C.stretches_by_account(gs, json.loads(masterrig.read_text(encoding="utf-8")))


def main() -> int:
    credits = C.load_credits()
    weight = 0.0  # this report reads the article literally: cache reads free
    runs = C.harness_runs()
    Cl = C.clean_stretches(load(Path(sys.argv[1])), runs, require="capture_status",
                           exempt=("masterrig",))
    print("1. Pure-Opus stretches (no Fable, no fitted rate), credits per 1%, cache reads 0")
    W = None
    pure = C.pure_family_rows(Cl, credits, "opus", weight)
    for a in Cl:
        for era, keep in (("pre", lambda s: P(s["start"]) < CUT), ("post", lambda s: P(s["start"]) >= CUT)):
            v = sorted(r["credits_per_pct"] for r in pure.get(a, []) if keep(r))
            if v:
                print(f"   {a:9} {era:4} n={len(v):2} median={st.median(v):9,.0f} range={v[0]:,.0f}-{v[-1]:,.0f}")
                if a == "jwork" and era == "pre":
                    W = st.median(v)
    W = W or 197000
    print(f"\n2. Fable input credits per token solved per Fable-heavy stretch, W={W:,.0f}")
    for k in (3, 5):
        for a in Cl:
            v = []
            for s in Cl[a]:
                p = C.price_tokens(s["tokens"], credits, weight)
                if p.priced and p.charged and (p.fable_input + p.fable_output) / p.charged >= 0.5:
                    v.append((s["delta_pct"] * W - p.known) / (p.fable_input + k * p.fable_output))
            if v:
                v.sort()
                n = len(v)
                opus = OPUS_INPUT
                print(f"   output={k}x {a:9} n={n:3} median={st.median(v):5.2f} p25={v[n // 4]:5.2f} "
                      f"p75={v[3 * n // 4]:5.2f} ({st.median(v) / opus:.2f}x Opus)")
    print("\n3. All clean stretches, Fable 1.667/5.0, before and after 2026-09-14")
    for a in Cl:
        for era, keep in (("pre", lambda s: P(s["start"]) < CUT), ("post", lambda s: P(s["start"]) >= CUT)):
            v = []
            for s in Cl[a]:
                p = C.price_tokens(s["tokens"], credits, weight)
                if p.priced and keep(s):
                    v.append((p.known + p.fable_input * 1.667 + p.fable_output * 5) / s["delta_pct"])
            if v:
                print(f"   {a:9} {era:4} n={len(v):3} median={st.median(v):9,.0f}")
    print("\n4. jwork against Dave across assumed Fable rates (output 3x), cache reads 0")
    for fr in (0.667, 1.0, 1.333, 1.667, 2.0, 2.5):
        med = {}
        for a in ("jwork", "dave"):
            v = []
            for s in Cl[a]:
                p = C.price_tokens(s["tokens"], credits, weight)
                if p.priced:
                    v.append((p.known + p.fable_input * fr + p.fable_output * 3 * fr) / s["delta_pct"])
            med[a] = st.median(v)
        print(f"   fable={fr:5.3f} jwork={med['jwork']:9,.0f} dave={med['dave']:9,.0f} "
              f"ratio={med['jwork'] / med['dave']:.3f}")
    print("\n5. The same window in TOKENS, as tracker/publish.py writes it to credits.window_tokens.")
    print("   Built by the publisher's own code from these same files, so this document and the")
    print("   page state one figure. Its selection exempts nobody from the capture gate, where")
    print("   sections 1 to 4 above exempt masterrig: the nine phantom-inflated masterrig rows of")
    print("   section 1 are gated out of it, leaving the eleven jwork and Dave stretches.")
    args = argparse.Namespace(probes=R.PROBES, passive=R.PASSIVE, effort_matrix=R.EFFORT_MATRIX,
                              prices=R.PRICES, gs=R.REPORTS["gs"], masterrig=Path(sys.argv[1]),
                              model_rates=R.MODEL_RATES)
    print("\n".join(R.window_tokens_lines(R.window_tokens_block(args))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
