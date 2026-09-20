# Reconciling the three instruments, 2026-09-20

What the Max 20x five-hour window is in credits, why the three ways of measuring it disagreed, what the 14 September change was, and why both plans sat at 0.86 of Shellac's windows per week. Reproduce with `python3 tools/reconcile_window.py history/masterrig-passive.json` (the masterrig file is written by the passive run from PR #63 onward).

## The dates that explain the numbers

Shellac's article was crossposted on 25 January 2026 (braddelong.substack.com, "CROSSPOST: SHELLAC", dated January 25, 2026), so its table describes the plans as they were in January. Three changes since then:

- **6 May 2026**: Claude Code five-hour limits permanently doubled for Pro, Max, Team and seat-based Enterprise; peak-hour throttling removed (explainx.ai timeline: "On May 6, 2026, Anthropic announced it had permanently doubled Claude Code's 5-hour rate limits").
- **May 2026 to 13 September 2026**: a temporary +50% on Claude Code weekly limits, extended four times (devops.com: "the fourth deadline Anthropic has set for this promotion since introducing it in May"; apidog: "the 50% higher weekly limits now run through August 31, 2026").
- **14 September 2026**: weekly limits set to a permanent +25% over the January baseline. Anthropic on X, quoted by BleepingComputer: "Starting September 14, we're permanently raising standard weekly limits in Claude Code by 25% for Pro, Max, Team, and seat-based Enterprise plans" and "Compared to today, this works out to a 17% reduction in weekly limits on Claude Code."

Applied to Shellac's Max 20x row (11.0M credits per five hours, 83.33M per week):

| Quantity | January (Shellac) | May to 13 Sep | From 14 Sep |
|---|---|---|---|
| Five-hour window | 11.0M | 22.0M | 22.0M |
| Weekly cap | 83.33M | 125.0M | 104.2M |
| Windows per week | 7.58 | 5.68 | 4.73 |

## What we measure

**Five-hour window.** Ten jwork stretches on the evening of 9 to 10 September are pure Opus, so Shellac's Opus rate prices them with no fitted parameter: median **196,989 credits per 1%**, range 175,934 to 208,197, so **19.7M credits per window**. Dave's one pure-Opus stretch reads 172,500. Anchoring the other way, the official weekly caps divided by our measured windows per week give 125.0M / 6.54 = 19.1M before the cut and 104.2M / 5.10 = 20.4M after. Three routes, 19.1M to 20.4M, with the per-account all-stretch medians spanning 16.1M (Dave) to 20.5M (jwork). The window is about 20M credits, 0.9 of the 22M the May doubling implies, and 1.8 times Shellac's January figure. The "2.1x unexplained" in the credits-model note was a comparison against a January number.

**Did the five-hour window move on 14 September?** Not settled. Capture-accepted stretches clear of harness runs, priced at one Fable rate (1.667 in, 5.0 out), cache reads 0: jwork 187,168 credits per 1% before and 204,632 after (n=41, 14); Dave 161,212 after (n=13); masterrig 110,193 and 112,854 (n=177, 32, phantom usage included). jwork moved +9% across the change, but jwork and Dave differ by 27% after it on the same plan, so the account-to-account spread is larger than the pre/post move and neither direction can be read from it. An earlier draft of this document said the window was flat within 4%; that came from a filter on `status` instead of `capture_status`, which let 42 unaccounted jwork stretches into the comparison (caught by the gate on PR #64).

**The weekly cut.** Windows per week fell from 6.54 to 5.10, −22%, against an announced 17% reduction in the weekly cap. The ratio falls the same way whether the weekly cap shrank, the five-hour window grew, or both, and the stretches above cannot separate those. The post-cut week's rounding interval is 4.48 to 6.07, which contains 5.45 (the −17% value), so the measured ratio and the announced figure are not in conflict at this resolution. The page states the measured ratio, the announced figure, and that the split between the two meters is unresolved.

**Why both plans sat at 0.86 of Shellac's windows per week before the cut.** Shellac's 7.58 is January's 83.33M / 11.0M. With the five-hour window doubled in May and the weekly cap at +50%, the expected ratio was 125.0M / 22.0M = 5.68, and we measured 6.54. Against the January figure that reads as 0.86; against the May figure it is 1.15, and the 15% is the five-hour window being 20M rather than 22M. The shortfall was never a shortfall. It was two changes Shellac's table predates.

## Why the three instruments disagreed

- **Probes** (46k to 94k credits per 1% on Opus): ten of fifteen rows carry `early_tick` or `outlier`, and the probe's own tokens are a fraction of what moved the meter while other work ran on the same account. Contaminated by design on a shared account; not usable for the absolute figure.
- **Masterrig** (median 109k): the meter also counts web, phone and other machines. The pure-Opus set is bimodal, 1,917 to 127,672, with a phantom cluster near 16k. A median of that is not a measurement; its closeness to Shellac's 110k was coincidence.
- **gs** (jwork 197k, Dave 172k): with the effort-matrix run and the probe windows excluded and only capture-accepted stretches kept, jwork still reads 17% to 27% above Dave across every assumed Fable rate (ratio 1.17 to 1.27, section 4 of the tool). That gap is real and unexplained; it is published as the interval on the window, not averaged away. Sub-agent double counting was tested directly and refuted: parent and sub-agent message ids overlap zero times, and `iter_turns` deduplicates on message id across every file of an account. Dave's directory is not pooled at all and still reads high against Shellac's January figure, which the May doubling explains.

## Fable's rate

Solved per Fable-heavy stretch (Fable over half the raw tokens) against W = 197k, cache reads at 0, capture-accepted and harness-clean only:

| Account | Output at 3x | Output at 5x |
|---|---|---|
| jwork (n=3) | 1.70 credits per input token, p25 1.42, p75 2.65 | 1.31, p25 1.20, p75 1.92 |
| masterrig (n=75) | 3.88, p25 2.67 | 3.10, p25 1.99 |

Only three jwork stretches survive the capture gate, and masterrig's solve is inflated by phantom meter movement, so its p25 is the usable edge. **Fable input is somewhere between 1.2 and 2.7 credits per token (jwork p25 at output 5x to masterrig p25 at output 3x), 1.8 to 4.0 times Opus; the output ratio (3x or 5x) is not separable.** The credits-model note's 25/15 (2.5x Opus) sits inside the interval. Fable's rate goes on the page as an interval with its status, not a number; it narrows as clean Fable-heavy stretches accrue from ordinary use.

## Cache reads

With reads at 0.015 of the input rate the pure-Opus window reads 236k per 1%, 23.6M per window, above the 22M the May doubling implies; at 0 it reads 19.7M, between the official-anchored 19.1M and 20.4M. The data prefer a read weight at or near zero. The 0.005 to 0.015 range from the fits is kept as the uncertainty on every credit figure.

## What this changes on the page

1. The event row: "windows per week fell 22% around 12 to 14 September; Anthropic announced a 17% weekly reduction for 14 September; whether the five-hour window also moved is unresolved."
2. A reference row for Shellac dated January 2026, with the May doubling and the May and September weekly changes listed, so the dashed line means something.
3. The five-hour window published as about 20M credits from the pure-Opus stretches (19.7M, cluster 17.6M to 20.8M), with the per-account medians 16.1M to 20.5M and the official-anchored 19.1M to 20.4M as the interval; tokens per window per model derived from it.
4. Fable as an interval.
5. The contamination rule keyed to harness runs, never to dates.
