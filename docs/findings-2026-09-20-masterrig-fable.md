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
are tried. Route A, the joint fit, is the one this revision leads with, because the
single-family anchor's 90% threshold was never going to find much: masterrig does not run Opus
alone. Route B, the original single-family anchor, is kept below as a paragraph.

## The capture_status gate is dropped from Route A

`accounts.masterrig.stretches[].capture_status` since the cut is `"unpriced"` for 58 rows and
`"surplus"` for 12; none is `"accepted"`. `"unpriced"` means the stretch contains
claude-opus-4-7 tokens, which `data/prices.json` has no rate for -- using that status to exclude
a row from a fit that is itself trying to price claude-opus-4-7 is circular, exactly as Codex's
review of this file's first pass said. `"surplus"` is a verdict computed under the old,
pre-Codex-review price model, not this one, so it is not a completeness signal for this fit
either. Gating Route A on `capture_status` therefore returned `n = 0` in the first pass, which
was an artefact of the gate, not a finding about masterrig.

This revision drops the `capture_status` filter from Route A entirely and gates only on
`delta_pct >= 10` (rows below that are dropped because their own meter movement is mostly
+-1-percentage-point rounding noise, not because of what produced the tokens). It also adds
claude-opus-4-7 as its own nonnegative column, separate from the rest of the Opus family
(claude-opus-4-8 and claude-opus-5, which `data/prices.json` does price), so the fit is not
forced to assume claude-opus-4-7 costs whatever the priced Opus models cost. The fit is reported
twice: once over all 70 gated rows, and once with the 12 `"surplus"`-status rows also dropped,
to show whether they move it.

## Route A: joint fit, gated on delta_pct >= 10 only

Nonnegative least squares of `delta_pct` on five columns -- claude-opus-4-7, the rest of the
Opus family, Sonnet and Fable input-equivalent tokens (input + cache_write + 5x output, an
assumption on the output multiplier) and total cache reads (unweighted as its own column,
summed across every model including claude-opus-4-7 and Haiku) -- fit jointly. An 80%
row-bootstrap interval (10th-90th percentile of the resampled Fable/priced-Opus ratio, 600
resamples, seed 20260920, matching `tools/model_rates.py`'s own convention) is reported.
`tools/rounding_feasibility.py`'s linear programme (same four input-equivalent columns, reads
free, i.e. weight 0) runs on the same rows to say whether any nonnegative stationary rate is
even consistent with whole-percent rounding.

**All 70 gated rows:**

| coefficient (pp per raw token) | value |
|---|---|
| claude-opus-4-7 | 5.722e-06 |
| priced Opus (4-8, 5) | 2.907e-06 |
| Sonnet | 2.498e-06 |
| Fable | 7.435e-06 |
| reads | 2.871e-08 |

Fable/priced-Opus coefficient ratio: **2.558**, 80% row-bootstrap interval **[2.073, 3.445]**
(600/600 draws). B5 = 1/coef(priced Opus) = **344,019** Opus-input-equivalent tokens per 1%,
range over +-windows rounding **[301,628, 400,273]**. Feasibility slack (reads free): **t =
6.949 percentage points**. `t > 0` means no nonnegative stationary rate on these four columns
puts every row inside its own whole-percent rounding bound -- masterrig's 70 gated rows do not
fit the stationary complete-capture model this file assumes, at any read weight in the range
tried, by a wide margin.

**58 rows, `"surplus"`-status dropped:**

| coefficient (pp per raw token) | value |
|---|---|
| claude-opus-4-7 | 7.227e-06 |
| priced Opus (4-8, 5) | 2.840e-06 |
| Sonnet | 2.871e-06 |
| Fable | 7.388e-06 |
| reads | 7.535e-09 |

Fable/priced-Opus coefficient ratio: **2.601**, 80% row-bootstrap interval **[1.959, 3.767]**
(600/600 draws). B5 = **352,057**, range **[300,090, 432,026]**. Feasibility slack: **t = 3.376
percentage points** -- smaller than the all-rows figure, so the `"surplus"` rows do make the fit
worse, but dropping them does not reach `t = 0` either. Both the coefficients and the ratio move
only modestly (2.558 to 2.601) between the two groups; the non-zero slack is not explained away
by the 12 dropped rows.

## Route B: the single-family >=90%-Opus anchor (kept as a paragraph)

Under a 90% Opus-share threshold (input + cache_write + 5x output basis, claude-opus-4-7
included in "Opus" here, all 70 post-cut stretches regardless of capture status), **n = 0**. The
highest Opus share any of the 70 reaches is 76.35% (2026-09-18T19:51:55+02:00), and only 32 of
the 70 clear even 50%. masterrig's usage since 6 September never runs Opus alone for long enough
to isolate an Opus-only window the way `tools/model_rates.py` does on jwork's pure-Opus cluster.
This route is not extended further -- Route A above is the route that uses every row.

## Step 2: the Fable solve, against Route A's full-group B5

17 of the 70 post-cut stretches are Fable-heavy enough (raw share >= 50%) and clear the same
`delta_pct >= 10` gate as Route A (every one of them already had `delta_pct >= 10`, so the gate
changes no row here, only makes the two steps consistent) to solve. Using the all-70-rows joint
fit's B5 (median 344,019; range 301,628-400,273):

**n = 17, median f = 2.4446 Opus input tokens, 10th-90th percentile [2.2800, 3.3867].**

`other_chg` prices claude-opus-4-7's own tokens at its own fitted coefficient from the joint fit
above (5.722e-06 pp per raw token), not at the priced-Opus per-token weight the rest of
`other_chg` uses -- the two families are priced at different rates in the same fit, and every
row below carries claude-opus-4-7 tokens (that is what makes it `"unpriced"`), so charging them
at the priced-Opus rate would carry the wrong rate into every row of this table.

| start | end | delta | win | capture | F_in+wr | F_out | F_reads | other_chg | f@median |
|---|---|---:|---:|---|---:|---:|---:|---:|---:|
| 2026-09-06T02:06 | 2026-09-06T02:41 | 10.0 | 1 | unpriced | 360,401 | 21,649 | 17,024,211 | 880,676 | 5.4615 |
| 2026-09-06T14:05 | 2026-09-06T15:57 | 10.0 | 1 | unpriced | 403,991 | 108,936 | 20,200,889 | 1,214,866 | 2.3457 |
| 2026-09-07T14:49 | 2026-09-08T07:13 | 10.0 | 4 | unpriced | 449,962 | 88,229 | 18,608,624 | 978,298 | 2.7627 |
| 2026-09-08T07:13 | 2026-09-08T12:39 | 11.0 | 2 | unpriced | 478,188 | 151,710 | 28,241,937 | 760,887 | 2.4446 |
| 2026-09-08T15:51 | 2026-09-08T23:22 | 10.0 | 3 | surplus | 392,259 | 122,896 | 34,124,155 | 511,862 | 2.9087 |
| 2026-09-09T14:30 | 2026-09-09T18:41 | 10.0 | 2 | unpriced | 415,158 | 127,440 | 29,239,193 | 1,004,826 | 2.3142 |
| 2026-09-09T21:34 | 2026-09-10T01:33 | 10.0 | 2 | unpriced | 442,786 | 142,530 | 30,403,323 | 916,702 | 2.1840 |
| 2026-09-10T12:30 | 2026-09-10T13:57 | 10.0 | 2 | unpriced | 463,022 | 68,338 | 11,502,940 | 1,100,789 | 2.9071 |
| 2026-09-10T13:57 | 2026-09-10T15:33 | 10.0 | 1 | unpriced | 337,096 | 121,285 | 17,240,090 | 982,700 | 2.6046 |
| 2026-09-11T18:34 | 2026-09-12T16:46 | 10.0 | 2 | unpriced | 308,002 | 63,036 | 12,468,056 | 965,474 | 3.9711 |
| 2026-09-12T16:46 | 2026-09-12T17:32 | 10.0 | 1 | surplus | 494,760 | 115,796 | 12,172,702 | 376,878 | 2.8529 |
| 2026-09-12T17:32 | 2026-09-12T18:17 | 11.0 | 1 | surplus | 685,516 | 134,899 | 20,046,070 | 557,078 | 2.3729 |
| 2026-09-12T18:17 | 2026-09-12T20:19 | 10.0 | 2 | surplus | 382,166 | 129,731 | 19,561,922 | 1,019,942 | 2.3479 |
| 2026-09-12T20:19 | 2026-09-12T20:59 | 10.0 | 1 | surplus | 463,141 | 104,592 | 22,334,539 | 484,772 | 2.9971 |
| 2026-09-12T20:59 | 2026-09-12T22:51 | 10.0 | 1 | surplus | 445,032 | 151,014 | 35,191,414 | 686,301 | 2.2947 |
| 2026-09-19T13:21 | 2026-09-19T19:31 | 10.0 | 3 | unpriced | 582,271 | 121,132 | 12,941,060 | 543,971 | 2.4380 |
| 2026-09-19T19:31 | 2026-09-19T20:52 | 10.0 | 1 | unpriced | 304,775 | 131,832 | 21,821,400 | 1,263,630 | 2.2580 |

Stated assumptions, not re-derived: output multiplier 5x, cache-write multiplier 1x, cache-read
weight 0.015 relative to Opus input (used in Route B's per-row solve and here to weight
`other_chg`'s read term for every family except claude-opus-4-7; Route A's own joint fit gives
cache reads their own free-standing, unweighted column instead of this fixed relative weight).

## What this does and does not say

The joint fit now runs, unlike the first pass's `n = 0` (an artefact of the circular
`capture_status` gate). But it does not fit well: the feasibility slack is 6.949 percentage
points over all 70 rows and 3.376 over 58, both far from the `t = 0` that would mean a
stationary rate is even consistent with rounding. The Fable/priced-Opus ratio (2.558-2.601
across the two groups, bootstrap range roughly 2.0-3.8) is reported as what the fit gives, not
as a rate this repository adopts -- a group this far outside its own feasibility bound is not
one whose point coefficients should be trusted as a price. masterrig's post-cut record does not
support a stationary complete-capture model at any of the read weights tried, and Fable's rate
on this account remains exactly as unidentified as the published page already says.

## Source

`history/masterrig-passive.json` `accounts.masterrig.stretches`, read by
`tools/masterrig_fable.py`. No account was driven; this is read-only arithmetic over a file
already in the repository.
