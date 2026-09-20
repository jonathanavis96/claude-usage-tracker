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
range kept beside it as `cache_read_weight_range`.

Fable's row holds no number at all -- not a rate, and not an interval either. Its
endpoints are solved from the stretches at publish time (`tracker/credits.py`
`fable_interval`) and published only in the JSON, so the price table never freezes a
measurement into a table of reference rates. The row carries the rule instead, and the
two candidate output ratios (3 and 5) with the sentences they come from.

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

**`credits.fable_interval`** -- Fable's input rate, solved rather than stored.
**1.19 to 2.36 credits per token** (1.79 to 3.54 times Opus), output ratio 3 or 5, status
"interval, not yet separable". The rate is solved per Fable-heavy stretch against the
measured window, per account, at both candidate ratios; the interval runs from the lowest
per-account p25 at output 5x to the highest per-account p25 at output 3x. Both edges are a
p25 rather than a median because the solve is inflated wherever the meter counts machines
the transcripts never saw, and on the account with a usable capture column only three
stretches survive the gate, so its p25 is its lowest value. The rule names no account.

**`credits.per_model`** -- tokens per window and the API list value of exactly those
tokens, per family, with the window's interval carried through. Opus reads 29,315,830
input tokens per window (25,875,027 to 31,229,540) worth $146.58 of API list value;
Sonnet 48,859,718 ($97.72); Haiku 146,579,152 with a null API value and the status "no row
in the dollar table, so no list price to value the tokens at". Fable's row carries no
value at all: `tokens_per_window.input` is 7,299,741 to 17,444,234 with
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
works out to a 17% reduction in weekly limits on Claude Code".
`five_hour_window_credits` is this tracker's reading of the five-hour window in credits
either side of that date, per account: 187,168 before and 204,632 after on the account
with a usable capture column (n=41 and 14), a second account 158,809 after (n=14), the
third 123,599 and 139,377 (n=166 and 24, phantom usage included).

**It does not resolve the attribution, and the record says so.** The accounts differ from
each other by 46.8% after the change -- more than the largest move any one of them made
across it (12.8%) -- so the account-to-account spread is larger than the pre/post move and
neither direction can be read from it. `resolved` is false, `unresolved` carries that
sentence, `meter_attribution` stays "unresolved", and nothing published anywhere says the
five-hour window did not move. A fall in the ratio is consistent with a smaller weekly
cap, a bigger five-hour window, or both, and these stretches cannot separate them.

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

**1. The window cluster gates every account on `capture_status`, where
`tools/reconcile_window.py` exempts masterrig.** Both now gate on `capture_status` rather
than `status` -- that was PR #64's correction, and 42 stretches read `status` "accepted"
with `capture_status` "unaccounted". What is left is the exemption. Applied to the
pure-Opus cluster it admits masterrig's six stretches, which read 1,917 to 24,546 credits
per 1% against the other account's 175,934 to 208,197 -- the phantom the reconciliation
describes, where the meter counts web, phone and every other machine while only that
host's transcripts are read. The reconciliation says in as many words that a median of
that set is not a measurement. Gating masterrig too drops exactly those stretches and
nothing else, and the account still appears in `accounts` with `n: 0`, so its absence is
visible rather than silent.

**2. The before-and-after comparison and the Fable solve keep the exemption.** They ask
about each account's own meter rather than about a level the page states, and the inflated
account's p25 is the usable *edge* of the Fable interval rather than something to drop. So
both take `capture_status` with masterrig exempt, which is exactly
`reconcile_window.py`'s selection and the one that produced the published 187,168 and
204,632. Each account's row carries `n_with_capture`, which is 0 on the account with no
usable capture column, so a reader can see which of the three rows reads this host's work.

**3. The credit rates match by family, not by exact model id, and that changes three of
`reconcile_window.py`'s masterrig rows.** The reference table is per model family and the
brief says "Opus of any version"; the tool's rate table lists `claude-opus-5` and
`claude-opus-4-8` and stops there, so `claude-opus-4-7` and `claude-sonnet-4-6` are
silently unpriced and every stretch touching them is dropped whole. Every jwork and Dave
row is byte-identical to the merged tool's after the fix. Masterrig's solved Fable rate
moves from 19.19 to 3.85 -- which is the 3.88 the reconciliation findings themselves
quote, where the merged tool prints 19.19 -- so the fix reconciles the tool with the
document rather than breaking it.

**4. The comparison holds one Fable rate that sits below the solved interval.**
`reconcile_window.py` section 3 prices Fable at 1.667 credits per input token and 5.0 per
output token on both sides of the change; the solved interval starts at 1.19, so 1.667 is
inside it now, but the reason for holding it never depended on that. Holding one rate is
what makes the two medians comparable with each other, and a rate that appears identically
in both cannot move their ratio much. It is recorded in `data/prices.json` as
`across_cut_fable_rate` with that reasoning attached, and it is never used for a level the
page states -- the level is `window_credits`, which contains no Fable tokens at all.

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

- **The solved Fable interval is 1.19 to 2.36; the reconciliation document says 1.2 to
  2.7.** The low edge agrees exactly. The high edge is the phantom-carrying account's p25
  at output 3x, and the document's is taken over n=75 stretches where the current history
  yields n=49 with every Opus and Sonnet version priced (n=18 with the merged tool's
  exact-id table). No committed version of the tool reproduces n=75, at any point in its
  history, so the document's high edge cannot be recomputed from this repository. The
  publisher computes the edge rather than copying it, which is what the brief asked for;
  if the n=75 set can be recovered the edge will move with it and nothing needs editing.
- The second account reads 158,809 after the change over n=14, where the reconciliation
  document says 161,212 over n=13. The merged tool prints 158,809 and n=14 on this
  history, so the publisher agrees with the tool rather than with the document's figure.
- `CONTEXT.md` has no glossary entry for **credit**, the unit the page now leads with. It
  is outside this change's file list and is worth its own small change.
- The masterrig account still has no usable capture column
  (`docs/findings-2026-09-20-masterrig-stretches.md` names the one-line fix in
  `tracker/capture.py`). Until it does, the window cluster is two accounts wide.
