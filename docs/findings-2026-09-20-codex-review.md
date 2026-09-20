# Codex's review of commit 447b926, and what it changed here

An external, read-only review by Codex ("What the passive records identify") checked commit
[447b9269cfc34bceec50d5211018ec74eeea9eb5](https://github.com/jonathanavis96/claude-usage-tracker/blob/447b9269cfc34bceec50d5211018ec74eeea9eb5)
against the repository's own history files, using no traffic and driving no account. It found
four defects, reproduced its own numbers exactly, and this document records all four, the
repair for each, and the consequences that follow. Reproduce the feasibility numbers with
`python3 -m tools.rounding_feasibility history/masterrig-passive.json`.

## 1. The rounding floor used 0.5, not the number of window pieces

`tools/model_rates.py` section4's quantisation floor reported `0.5 / delta_pct` per stretch, as
if every stretch were a single whole-percent reading pair. A stretch pooled from `windows`
separate window pieces carries two whole-percent endpoints per piece (`tracker/join.py`'s
`Stretch.bounds` already divides by `delta_pct +- windows` for exactly this reason), so the
correct floor is `windows / delta_pct`. Fixed in `tools/model_rates.py` (`prepare()` now carries
`windows` through to its record; section4 divides by it).

## 2. The joint fit's rate groups do not pass a rounding-only feasibility check

`tools/model_rates.py`'s joint fit always returns coefficients by non-negative least squares,
even when no stationary rate model actually explains the rows. Codex asked the sharper question
directly, by linear programming: do *any* non-negative coefficients on Opus/Sonnet/Fable
input-equivalent tokens (input + cache_write + 5x output) and on cache reads put every row's
predicted meter movement inside its rounding bound (`delta_pct +- windows`) at all? Where they
cannot, what is the smallest uniform extra percentage-point slack `t` that closes the gap?

`tools/rounding_feasibility.py` ports that check for the same three fit groups
`tools/model_rates.py`'s joint fit uses (jwork/pre, jwork/post, dave/post), at two read weights:
reads costing nothing, and a read weight fixed to 0.0118 of Opus (the jwork/pre joint fit's own
point estimate). Reproduced against `history/gs-passive.json` and
`history/masterrig-passive.json`:

| Group | n | Reads free (weight 0) | Reads at 0.0118 of Opus |
|---|---:|---:|---:|
| jwork/pre | 41 | 1.0191 pp | 0.2998 pp |
| jwork/post | 14 | 0.0000 pp | 0.0000 pp |
| dave/post | 14 | 0.7557 pp | 1.8952 pp |

These match Codex's table exactly. jwork/post is not excluded by rounding at either weight.
jwork/pre needs extra slack at both weights, less at the fixed weight than at zero -- a nonzero
cache-read charge is doing real work there. dave/post needs *more* extra slack at the fixed
weight than with reads free (1.8952pp against 0.7557pp): its own unconstrained read coefficient
is negative, so non-negativity pins it to zero and forcing a positive weight on it only makes
the fit worse, not better.

**Consequence (a): Dave's rows do not fit the stationary complete-capture model at any read
weight tried, so Dave's capture is unverified until its extra slack falls under the rounding
bound.** A stationary per-family rate plus a single cache-read weight cannot, at any
non-negative coefficients in the range tested, explain dave/post's fourteen clean stretches
within whole-percent rounding. This does not identify *why* -- omitted traffic, cross-account
misattribution, an unmodelled class interaction and a real price change would all produce the
same symptom -- but it does mean the joint fit's dave/post rates should be read as a fit to
inconsistent data, not a rate this repository has verified.

## 3. The "47% between accounts" argument was invalid

See `docs/findings-2026-09-20-publisher-credits.md` ("It does not resolve the attribution...")
for the full retraction. In short: `tracker/credits.py`'s `across_cut()` called the September
event's meter attribution unresolved because the accounts differed from each other by 46.8%
after the change, more than the largest per-account move (12.8%). That comparison is not valid:
a stable account-specific scale cancels out of a within-account before/after ratio regardless of
how far apart two different accounts sit, so the cross-account spread was never evidence either
way, and the 46.8% figure also mixed in masterrig's unusable capture column.

The retraction replaces it with the fact these rows do establish: the pooled windows-per-week
ratio (five-hour meter movement over seven-day meter movement) fell 23.65% at the commit
447b926 snapshot (before: 144 windows, sum five-hour-pct 3,365, sum seven-day-pct 519; after: 57,
1,094, 221). That is one equation in two unknowns, and is equally consistent with the weekly cap
falling 23.65% and the five-hour window unchanged, the five-hour window rising about 31% and the
weekly cap unchanged, or any split between -- including the announced 17% weekly cut paired with
an implied 8.7% five-hour rise. `credit_model.windows_per_week_ratio_note` computes this live
from the certified regimes and publishes it as `windows_per_week_ratio` on the weekly event.
`meter_attribution` stays "unresolved".

**Consequence (c): meter ratios identify rate/allowance only, so an account gap cannot be called
pricing versus allowance without an independent debit observation.** With complete capture,
these five-hour readings identify `rate / budget` for each account, and multiplying every rate
and both budgets by the same positive number leaves every reading unchanged. This is the same
algebraic fact that made the cross-account spread argument invalid, and it is general: no
combination of within-account ratios, across two accounts or across the cut on one account, can
by itself say whether a gap is a price difference or an allowance difference. That needs an
actual debit or cap observation in a stable accounting unit, or an independently established
equality of one model's rate across the accounts being compared. A plan label or another rounded
percentage is not that observation.

**Consequence (d): a Dave-before baseline does not exist and cannot be recreated.** Dave's
stretches begin after the 14 September change; there is no committed record of Dave's account
before it. Better future logging can resolve a *future* event on Dave's account; it cannot
recreate this one, because the September change has already happened and no passive record of
Dave's pre-change meter survives to be found. This is a dated exclusion, not a gap this
repository can close by collecting more data going forward.

## 4. The cache-read weight is not identified from current rows

Section 1's cache-read fits put the read/Opus-input coefficient at 0.0117889 on jwork/pre
(n=41), 0.0088094 on jwork/post (n=14), and clip to 0 on dave/post (n=14; unconstrained value
-0.0025438). `data/prices.json` publishes `cache_read_weight: 0` with the range 0 to 0.015 kept
beside it, and `tools/model_rates.py`'s own module docstring already says the 2026-09-20 hostile
review put the weight at 0.005 to 0.015 rather than at nothing.

Codex's rounding-feasibility numbers above sharpen this: jwork/post accommodates both 0 and
0.0189 (and everything between) within its rounding bound, so its interval is a design
sensitivity, not a resolved identification. jwork/pre and dave/post both need extra slack beyond
rounding at *any* read weight in the range tried, so neither fit is a clean estimate of the true
weight either -- they are the best fit to data a stationary rate does not fully explain. A
row-bootstrap subsample of fourteen *different* rows from jwork/pre lands at exactly zero only
3 times in 2,000 draws (seed 20260920; 10th/50th/90th percentiles 0.00816/0.01502/0.02593),
which shows dave/post's zero is not simply an artifact of having only fourteen rows -- but it is
a design-sensitivity check, not a p-value comparing accounts, since the two accounts differ in
token mix, dates and capture process as well as in sample size.

**Consequence (b): the cache-read weight is published as an interval 0 to 0.019 with 0.015 in
use, and is not identified from current rows.** No single value in that range is established by
the committed stretches; 0.015 is a working choice within the range the fits and the range check
both leave open, not a measurement. Passive observation that would resolve it: preserve
account-attributed usage with paired meter endpoints across natural contrasts that vary cache
reads substantially *after* accounting for input, cache writes, output and model mix -- more
rows of the same token proportions do not supply that contrast, and using the existing
price-derived capture gate as a completeness certificate would be circular.

## What did not change

Codex's finding on Fable (its section 4 -- the committed clean selection does not yield 22
validated Fable-heavy stretches under the described selection, and the 0.94/1.48 candidate
input rates it checked could not be reproduced from any selection it could reconstruct) is not
repaired by this PR. Fable's page row already publishes "rate not yet identified" and no value,
which is consistent with what Codex found; nothing here changes that status, and this PR's
masterrig Fable envelope work (`docs/findings-2026-09-20-masterrig-fable.md`) is exploratory and
does not touch any published figure.

## Source

- Codex's report: `analysis.md` and `reproduce.py`, from a read-only checkout of commit
  [447b9269cfc34bceec50d5211018ec74eeea9eb5](https://github.com/jonathanavis96/claude-usage-tracker/blob/447b9269cfc34bceec50d5211018ec74eeea9eb5).
- The repair: this branch, `tools/model_rates.py` (item 1), `tools/rounding_feasibility.py`
  (item 2), `tracker/credits.py` and `tracker/publish.py` (item 3),
  `docs/findings-2026-09-20-publisher-credits.md` (item 3's paragraph).
