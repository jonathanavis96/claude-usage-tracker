# Pricing the publisher in credits, 2026-09-20

What the public JSON now publishes in credits, where each figure comes from, and the four
decisions taken along the way that a reader could reasonably have taken differently.
Companion to `docs/findings-2026-09-20-reconciliation.md`, which is the measurement this
work publishes; nothing here re-measures anything.

Reproduce every figure with:

    python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
      --gs-passive history/gs-passive.json --out /tmp/claude-usage.json
    python3 tools/credits_report.py --publish-check /tmp/claude-usage.json

## The unit

The five-hour meter does not charge dollars. It charges an internal unit -- credits --
at a small rational rate per token per model. `data/prices.json` gains a `_credits` block
holding those rates: Haiku 2/15 in and 10/15 out, Sonnet 6/15 and 30/15, Opus of any
version 10/15 and 50/15, cache writes at the plain input rate rather than the API's 1.25x
write premium, and cache reads at a `cache_read_weight` of 0 with the fitted 0 to 0.015
range kept beside it as `cache_read_weight_range`. Fable has no rate, only
`{"input_low": 2.0, "input_high": 2.9, "output_ratio": [3, 5], "status": "interval, not
yet separable"}`.

Rates are stored as `[numerator, denominator]`. A fifteenth does not survive a JSON round
trip as a decimal, and every figure on the page is a division by one of them.

The dollar table above it is untouched. The contributor path still reads it, the
dollar-derived `tokens_per_window` and `meter_budget_per_window` still publish, and they
now carry `derivation: "list-price dollars, legacy"` where the new figures carry
`derivation: "credits"`. Both routes are published so a reader can see that they agree:
the dollar route puts an Opus window at 589M tokens on the frozen reference mix, the
credit route puts it at 630M tokens on the accounts' own split.

## What the publisher computes, and from what

Everything below is computed at publish time from the history files. No figure in this
change is typed in. A figure that cannot be computed publishes `null` with a `status`
string saying why -- which is what a fixture with no pure-Opus stretch, or a missing
`history/masterrig-passive.json`, produces.

**`credits.window_credits`** -- the five-hour window. **19,543,887 credits**, interval
17,250,018 to 20,819,693, n=11 over two of the three watched accounts. It is the median
credits per 1% of the pure-Opus, capture-accepted, harness-clean stretches, times 100.
Pure-Opus is the point: every token in such a stretch is priced at a rate the reference
publishes, so the figure carries no fitted parameter -- no Fable rate, no cache-read
weight above zero, no class weight, no reference mix. The `interval` is the cluster's own
min to max, which is the spread of eleven readings of the same quantity and not a
confidence interval; there is no error model here and the field does not pretend there is.

**`credits.window_credits_from_weekly`** -- the same quantity anchored the other way, and
labelled `kind: "cross_check"`. The announced weekly cap (the January baseline of
83,333,300 times 1.5 before 14 September, 1.25 after) divided by this tracker's own
measured windows per week for the regime either side of the certified change:
**19,290,116** before and **21,001,336** after. It shares no input with the pure-Opus
cluster, which is the only reason it is worth publishing beside it. Three routes,
19.3M to 21.0M.

**`credits.per_model`** -- tokens per window and the API list value of exactly those
tokens, per family, with the window's interval carried through. Opus reads 29,315,830
input tokens per window (25,875,027 to 31,229,540) worth $146.58 of API list value;
Sonnet 48,859,718 ($97.72); Haiku 146,579,152 with a null API value and the status "no row
in the dollar table, so no list price to value the tokens at". Fable's row carries no
value at all: `tokens_per_window.input` is 5,948,282 to 10,409,846 with
`status: "rate not yet identified"`, and the interval widens at both ends at once --
cheapest rate against the top of the window's range, dearest against the bottom.

**`credits.sessions`** -- sessions per window and per week, per model, each publishing the
cache mix it assumes. At `history/passive.json`'s own split (97.0% cache read, 2.5% cache
write, 0.4% output) a window holds about 354 median Opus sessions and a week about 1,755;
Sonnet 1,818 and 9,015. Those numbers are only as meaningful as the split behind them --
cache reads are free in credits, so a cold-cache reading of the same budget would be far
smaller -- so `cache_normalised: true` and the split itself sit in the same object.
Sessions per week multiplies by the measured windows per week, not a documented one.

**`credits.effort_cache_mix`** -- each effort cell's cache-read share and how many of its
runs ran cold, from `data/effort_matrix.json`'s own `_meta.runs` token counts. This exists
to make one thing visible: the Sonnet row inverts, low reading dearer than medium, and
nothing on the page said why. Four of the seven Sonnet low runs wrote cache where the
medium runs read it, and a cold run pays for tokens a warm one gets at the cache-read
weight. The share is now beside the number instead of behind it.

**The event row** keeps the wording PR #62 set, computed from the event's own measured
quantity and nothing else: "Observed windows per week fell about 24% around 2026-09-11 to
2026-09-14". Two facts now sit beside it in the record, each labelled as what it is.
`announced` is Anthropic's own figure for 14 September, quoted: "Compared to today, this
works out to a 17% reduction in weekly limits on Claude Code". `five_hour_window_credits`
is this tracker's reading of the five-hour window in credits either side of that date,
per account: 169,038 before and 163,069 after on the account with a usable capture column
(n=64 and 25), which is flat within 4%. With the five-hour window flat, a fall in the
ratio is a fall in the weekly cap.

**`reference`** -- Shellac's table with its January 2026 date, and the three announced
changes since it, each with the sentence it was read from: the 6 May five-hour doubling,
the +50% weekly promotion that ran from May to 13 September, and the 14 September +25%.
The page draws the table as a dashed line; undated it reads as a figure for now, and the
gap between it and the measurement reads as an unexplained factor. That is exactly how
the credits-model note's "2.1x unexplained" came about. Dating the line turns the gap back
into arithmetic.

## The reproducibility check

`python3 tools/credits_report.py --publish-check <claude-usage.json>` rebuilds the whole
credits block from the history files -- the stretches, the probe rows, the effort-matrix
runs, the weekly window points, the rates -- and prints every figure beside its
recomputation. It fails, and exits 1, on any number more than 0.5% from the published one,
on any path present on one side and not the other, and on a `method` sentence that no
longer describes what was computed. The one thing it takes from the published JSON is
`generated_at`: the weekly block's current regime is bounded by the publish time, so
recomputing at "now" would compare two different questions. Against the publish above it
reproduces all 391 figures.

## Four decisions

**1. The window cluster requires `capture_status == "accepted"`, which
`tools/reconcile_window.py` does not.** The reconciliation's own tool asks for the
`status` column and exempts masterrig from it entirely. Applied to the pure-Opus cluster
that admits masterrig's six stretches, which read 1,917 to 24,546 credits per 1% against
the other account's 175,934 to 208,197 -- the phantom the reconciliation describes, where
the meter counts web, phone and every other machine while only that host's transcripts are
read. Pooling them would put the published median at 187,168 with an interval from 1,917,
and the reconciliation says in as many words that a median of that set is not a
measurement. Requiring the capture column drops exactly those stretches and nothing else,
and the account still appears in `accounts` with `n: 0`, so its absence is visible rather
than silent.

**2. The before-and-after comparison uses the looser selection, deliberately.** It asks a
different question -- did this account's own window move -- so it takes every priceable
stretch of the account, which is the selection `reconcile_window.py` section 3 made and
the one that produced the published 169,038 and 163,069. Each account's row carries
`n_with_capture`, which is 0 on the account with no usable capture column, so a reader can
see which of the three rows is a reading of this host's work.

**3. The credit rates match by family, not by exact model id, and that changed two of
`reconcile_window.py`'s numbers.** The reference says "Opus of any version"; the tool's
rate table listed `claude-opus-5` and `claude-opus-4-8` and stopped there, so
`claude-opus-4-7` and `claude-sonnet-4-6` were silently unpriced. That dropped 113 of
masterrig's stretches out of section 3 and left its pure-Opus cluster at six instead of
nine. Every jwork and dave row is unchanged by the fix, and masterrig's solved Fable rate
moves from 19.19 to 3.88 -- which is the figure this repository's own reconciliation
findings already quote, so the fix restores agreement rather than breaking it.

**4. The comparison holds one Fable rate that sits below the published interval.**
Section 3 prices Fable at 1.667 credits per input token and 5.0 per output token on both
sides of the change; the published interval starts at 2.0. Holding it is what makes the
two medians comparable with each other, and a rate that appears identically in both cannot
move their ratio much. It is recorded in `data/prices.json` as `across_cut_fable_rate`
with that reasoning attached, and it is never used for a level the page states -- the
level is `window_credits`, which contains no Fable tokens at all.

## What this change does not do

- **It does not re-measure anything.** Every input is a history file already in the
  repository; no probe was run and no account was driven.
- **It does not publish `passive.json`'s `current`.** The median of two complete weeks is
  not a measurement of anything the page states, and it stays out; `_regime_current` is
  the headline.
- **It does not name an account.** The JSON carries `a1`, `a2`, `a3` and counts, and the
  test suite asserts that no watched account's name appears anywhere in a published
  document, including inside a `method` or `source` sentence.
- **It does not settle Fable's rate.** That needs a stretch that separates the input rate
  from the output ratio, which the current history does not contain.

## Known gaps

- `docs/reference-2026-09-20-shellac-credits-model.md` on branch `step-vs-trend-finding`
  is cited by `tools/credits_report.py`, by the masterrig findings and by this work's
  brief, but it exists only in masterrig's checkout: the branch was never pushed and is
  not in this repository. The rates are restated in `data/prices.json`'s `_credits._source`
  and in the reconciliation findings so that nothing depends on a file no one here can
  read. The citation should stay until the branch is pushed, and then be checked.
- `CONTEXT.md` has no glossary entry for **credit**, the unit the page now leads with. It
  is outside this change's file list and is worth its own small change.
- The masterrig account still has no usable capture column
  (`docs/findings-2026-09-20-masterrig-stretches.md` names the one-line fix in
  `tracker/capture.py`). Until it does, the window cluster is two accounts wide.
