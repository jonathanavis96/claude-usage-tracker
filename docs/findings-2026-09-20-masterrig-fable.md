# An exploratory masterrig Fable-input-rate envelope, 2026-09-20

This is exploratory arithmetic, not a measurement this repository adopts. Fable's published
row stays "rate not yet identified" (`tracker/credits.py`'s `fable_interval`), and nothing here
changes any figure on the page. It exists to see what masterrig's own passive record gives, at
stated assumptions, without aiming at any prior figure (this document does not repeat or defend
the credits-model note's earlier 0.94-1.48 candidates). Reproduce with:

    python3 -m tools.masterrig_fable history/masterrig-passive.json --json /tmp/masterrig-fable.json

## Scope: stretches since 6 September

Only `accounts.masterrig.stretches` starting on or after 2026-09-06T00:00 UTC are used. The 2
to 5 September stretches are excluded: a pipeline ran against this account from another host
during that window. `history/harness-runs.jsonl` carries no masterrig entry for it, so this is
a **dated exclusion stated here**, not a file-backed one -- `tools/masterrig_fable.py`'s own
`CUT_REASON` constant says the same thing, so the code and this document cannot drift apart on
it. 70 of masterrig's stretches since the cut carry tokens.

## Method

1. **Opus anchor.** B5 = Opus input-equivalent tokens per 1% of the five-hour meter (input +
   cache_write + 5x output + 0.015x cache reads, all relative to Opus's own input), from every
   stretch where Opus is at least 90% of input + cache_write + 5x output summed across every
   family in the stretch. Output 5x, cache writes 1x, and cache reads at 0.015 relative to Opus
   input are assumptions -- the last is `data/prices.json`'s own published range's upper edge,
   not a value fitted here.
2. **Fable solve.** For every stretch where Fable is at least 50% of raw (unweighted) input +
   cache_write + output, solve
   `f = (delta_pct * B5 - other_models_charge - read_charge) / (Fable_input + Fable_write + 5 *
   Fable_output)`, in units of Opus input tokens. `other_models_charge` treats every non-Fable
   family's own input-equivalent tokens as worth the same as an equal count of Opus's (an
   assumption this document states rather than hides); `read_charge` is 0.015 times the
   stretch's total cache reads across every model. `f` is solved once at B5's median and once
   at each of its two rounding-implied edges (`tracker/join.py`'s `Stretch.bounds`,
   `delta_pct +- windows`).

## Result: no stretch since 6 September is Opus-dominant enough to anchor B5

Under the 90% threshold above, **n = 0**. The highest Opus share any of the 70 post-cut
stretches reaches is 76.35% (2026-09-18T19:51:55+02:00, input + cache_write + 5x output
basis), and only 32 of the 70 clear even 50%. masterrig's usage since 6 September never runs
Opus alone for long enough, at the volume this account produces, to isolate an Opus-only
window the way `tools/model_rates.py` does on jwork's pure-Opus cluster.

Because there is no B5 anchor, the Fable solve in step 2 cannot run either, even though 17 of
the 70 post-cut stretches are Fable-heavy enough (raw share >= 50%) to qualify for it: the
formula needs B5's median and its two rounding edges as an input, and none exist. **The
envelope this document set out to report is empty at the stated threshold, and this is reported
as the finding rather than relaxed to produce a number.** `tools/masterrig_fable.py --json`
still writes `opus_anchor: null` and `fable_envelope: {"n": 0, "rows": []}` so a caller sees the
same absence of data.

## What this does and does not say

It does not say masterrig cannot anchor an Opus rate at all -- a looser dominance threshold, or
a longer observation window that eventually accumulates an Opus-heavy stretch, might. It does
say that at the assumptions and threshold stated up front, masterrig's own committed record
since 6 September does not support this particular anchor-and-solve method, and that Fable's
rate on this account remains exactly as unidentified as the published page already says.

## Source

`history/masterrig-passive.json` `accounts.masterrig.stretches`, read by
`tools/masterrig_fable.py`. No account was driven; this is read-only arithmetic over a file
already in the repository.
