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
it. All 70 of masterrig's stretches since the cut carry tokens.

Two routes for the Opus anchor B5 (Opus input-equivalent tokens per 1% of the five-hour meter)
are tried. Route A, the joint fit, is the one this revision adds and leads with, because the
single-family anchor's 90% threshold was never going to find much: masterrig does not run Opus
alone. Route B, the original single-family anchor, is kept below as a paragraph.

## Route A: joint fit over accepted-capture-status rows

Nonnegative least squares of `delta_pct` on four columns -- Opus, Sonnet and Fable
input-equivalent tokens (input + cache_write + 5x output, an assumption on the output
multiplier) and total cache reads, unweighted as its own column here rather than folded into
Opus at a fixed relative weight -- fit jointly over every stretch since the cut whose
`capture_status` is `"accepted"`. `tools/rounding_feasibility.py`'s linear programme (same three
input-equivalent columns, reads free, i.e. weight 0) runs on the same rows to say whether any
nonnegative stationary rate is even consistent with whole-percent rounding. An 80% row-bootstrap
interval (10th-90th percentile of the resampled ratio, 600 resamples, seed 20260920, matching
`tools/model_rates.py`'s own convention) is reported on the Fable/Opus coefficient ratio.

**Result: n = 0.** Of the 70 post-cut stretches, `capture_status` is `"unpriced"` for 58 and
`"surplus"` for 12; none is `"accepted"`. There is no row this joint fit can run on, so there
are no coefficients, no ratio, no bootstrap interval, and no feasibility slack to report --
`tools/masterrig_fable.py --json` writes `joint_fit: null`, `feasibility_reads_free: null`.
This is a sharper form of the same absence Route B already found: it is not only that no
90%-Opus stretch exists, but that no stretch on this account since 6 September carries the
capture status the joint fit needs at all. masterrig's post-cut capture is not "accepted";
whatever this account measures since 6 September, it is not yet a row this repository is
willing to fit a stationary rate against.

## Route B: the single-family >=90%-Opus anchor (kept as a paragraph)

Under a 90% Opus-share threshold (input + cache_write + 5x output basis, all 70 post-cut
stretches regardless of capture status), **n = 0** as well. The highest Opus share any of the 70
reaches is 76.35% (2026-09-18T19:51:55+02:00), and only 32 of the 70 clear even 50%.
masterrig's usage since 6 September never runs Opus alone for long enough, at the volume this
account produces, to isolate an Opus-only window the way `tools/model_rates.py` does on jwork's
pure-Opus cluster. This route is not extended further -- it was already a null result in the
first pass of this file, and Route A above is the one that uses every row (had any been
accepted).

## Consequence: the Fable solve does not run

Because neither route produces a B5, the Fable solve (`f = (delta_pct * B5 -
other_models_charge - read_charge) / (Fable_input + Fable_write + 5 * Fable_output)`, in units
of Opus input tokens) cannot run, even though 17 of the 70 post-cut stretches are Fable-heavy
enough (raw share >= 50%) to qualify for it. The formula needs B5's point estimate and its two
rounding edges as an input, and neither route supplies one. **The envelope this document set
out to report is empty, and this is reported as the finding rather than relaxed to produce a
number.** `tools/masterrig_fable.py --json` writes `joint_fit_b5: null` and
`fable_envelope: {"n": 0, "rows": []}` so a caller sees the same absence of data.

Stated assumptions, not re-derived: output multiplier 5x, cache-write multiplier 1x, cache-read
weight 0.015 relative to Opus input (used in the Route B per-row solve and in the Fable-solve
step; Route A's joint fit gives cache reads their own free-standing, unweighted column instead).

## What this does and does not say

It does not say masterrig cannot anchor an Opus rate at all -- a longer observation window that
eventually accumulates an accepted-capture-status stretch, or one with a genuinely Opus-heavy
mix, might change either route's result. It does say that at the assumptions and thresholds
stated up front, masterrig's own committed record since 6 September supports neither
anchor-and-solve route, that its post-cut rows do not even carry the capture status the joint
fit requires, and that Fable's rate on this account remains exactly as unidentified as the
published page already says.

## Source

`history/masterrig-passive.json` `accounts.masterrig.stretches`, read by
`tools/masterrig_fable.py`. No account was driven; this is read-only arithmetic over a file
already in the repository.
