# The meter's per-model credit rates, 2026-09-20

What the meter charges for a Sonnet, Haiku or Fable token relative to an Opus token, measured
from the passive stretches already in the repository. No traffic was sent for this; every
figure below comes from `history/gs-passive.json`, `history/masterrig-passive.json`,
`history/harness-runs.jsonl`, `history/probes.jsonl` and `data/effort_matrix.json`.

Reproduce everything with:

```
python3 -m tools.harness_runs                                    # rebuild history/harness-runs.jsonl
python3 -m tools.model_rates history/masterrig-passive.json      # sections 0 to 5 below
python3 -m tools.model_rates history/masterrig-passive.json --json out/model-rates.json
python3 -m tools.model_rates history/masterrig-passive.json --exclusions probes-only
```

The tool groups token counts by model family, because Shellac's rows are unversioned: every
`claude-opus-*` prices as Opus and so on. Input-equivalent tokens are
`input + cache_write + 5 x output`, the 5x being Shellac's output ratio for every model in its
table; the same figures at `3 x output` are printed beside them, because the credits-model note
fits 3 for Fable. Cache reads count nothing. Intervals are 80% bootstrap intervals over
resampled stretches, seeded from the run seed and the row's own name, so a figure quoted here
does not move when a different row gains or loses a stretch.

## The exclusion list, first

The tracker excludes stretches that overlap one of its own runs. When this work started that
rule knew about 16 of them: 15 completed probe rows in `history/probes.jsonl` and the
effort-matrix run in `data/effort_matrix.json`'s `_meta`. A probe only writes a probes.jsonl
row when it finishes. `tools/harness_runs.py` parses `~/.paperclip/ops/claude-usage-probe.log`
and `~/.paperclip/ops/claude-usage-output-probe.log` as well, and finds **10 more runs that
sent traffic and left no row**: five aborted probes (dave "tick too early", dave "window reset
during probe", jwork "no second tick after 80 prompts", jwork "tick too early", jwork "account
not idle"), four that crashed mid-run on an HTTP 429, 401 or a missing `claude` binary, and one
aborted output probe on dave. Probe traffic leaves no transcript, so each of these moved a
meter with nothing in the transcripts to attribute the movement to. `history/harness-runs.jsonl`
holds all 26 runs, with `account`, `start`, `end`, `kind`, `outcome`, `precision` and the source
line each came from.

Four of those runs name no account in the log. They are attributed by the `resets_at` their
prompt lines carry, which identifies the account's five-hour window: a run is jwork's when
jwork's own meter shows a reset five hours before that `resets_at`, and dave's otherwise. That
puts one crashed run on jwork (2026-09-10, bracketed by jwork's meter to 00:02:18 to 00:57:14)
and three on dave. Dave's stretch record starts on 2026-09-15, so the dave runs remove nothing.

What it changes (section 0 of the tool):

```
   16 runs from probes.jsonl and the effort matrix, 26 from history/harness-runs.jsonl (dave 16, jwork 10)
   jwork     clean stretches  56 ->  55   removed beyond the current rule: 2026-09-09T23:13:14 (pre)
   dave      clean stretches  14 ->  14   removed beyond the current rule: none
   masterrig clean stretches 211 -> 211   removed beyond the current rule: none
   jwork pure-Opus pre-14-Sep window, probes-only  n=10 median=196,989 credits per 1% range=175,934-208,197
   jwork pure-Opus pre-14-Sep window, harness-runs n=10 median=196,989 credits per 1% range=175,934-208,197
```

One clean stretch goes, a jwork stretch of 2026-09-09T23:13:14 to 2026-09-10T01:02:44 that
contains the crashed run. It is not one of the pure-Opus stretches, so the reconciliation's
window is unchanged at 196,989 credits per 1% (n=10, range 175,934 to 208,197), which is also
the check that this tool's use of `clean()` matches the reconciliation's. Running
`--exclusions probes-only` moves only the jwork pre-14-September figures, and by less than half
a percent: the fitted window 189,282 against 189,609 credits per 1%, the Sonnet rate 0.615
against 0.617, Fable 1.166 against 1.170. Nothing in sections 1, 2, 4 or 5 moves except jwork's
own single-model count (11 against 10).

## 1. Tokens per 1% from stretches one model carries

A stretch prices one model alone when that family carries at least 95% of its raw tokens
(`input + cache_write + output`, cache reads at 0). Four such groups exist:

| Account and regime | Model | n | Median tokens per 1% (output 5x) | IQR | Output 3x |
|---|---|---|---|---|---|
| jwork, before 14 Sep | Opus | 10 | 295,484 | 290,802 to 302,805 | 240,639 |
| dave, after 14 Sep | Opus | 2 | not measurable | | |
| masterrig, before 14 Sep | Opus | 11 | 16,693 | 10,458 to 129,005 | 14,706 |
| masterrig, before 14 Sep | Fable | 12 | 7,103 | 3,875 to 13,658 | 5,007 |

jwork's Opus row is the reconciliation's window restated in tokens: 295,484 input-equivalent
tokens per 1% at 10/15 credits per token is 196,989 credits per 1%. masterrig's two rows are
not usable as absolutes — its meter counts web and phone use its transcripts never see, and the
interquartile ranges above, an order of magnitude wide, are that phantom cluster
(`docs/findings-2026-09-20-masterrig-stretches.md`).

**No Sonnet or Haiku row exists on any account.** The highest share one model reaches, per
account and regime, is the tool's first line of section 1:

```
   jwork/pre        highest share one model reaches: opus=1.000 sonnet=0.772 haiku=0.000 fable=0.778
   jwork/post       highest share one model reaches: opus=0.923 sonnet=0.504 haiku=0.000 fable=0.921
   dave/post        highest share one model reaches: opus=1.000 sonnet=0.718 haiku=0.000 fable=0.462
   masterrig/pre    highest share one model reaches: opus=1.000 sonnet=0.841 haiku=0.111 fable=1.000
   masterrig/post   highest share one model reaches: opus=0.745 sonnet=0.703 haiku=0.076 fable=0.834
```

Sonnet never exceeds 0.841 of a stretch's tokens anywhere, and Haiku never exceeds 0.111. That
is a measurement about how these accounts are used, and it is why section 2 below reports
almost nothing: the session mix that produces Sonnet tokens produces Opus tokens alongside them.

## 2. The meter's ratios from single-model stretches

| Account and regime | Ratio | Result |
|---|---|---|
| jwork before, jwork after, dave after, masterrig before, masterrig after | Opus rate ÷ Sonnet rate | **not measurable** — 0 Sonnet stretches on every account |
| all five | Sonnet rate ÷ Haiku rate | **not measurable** — 0 Sonnet and 0 Haiku stretches |
| jwork before and after, dave after, masterrig after | Fable rate ÷ Opus rate | **not measurable** — 0 Fable stretches on jwork and dave |
| masterrig before | Fable rate ÷ Opus rate | 2.350, interval 1.489 to 11.855 (n=12 Fable, 11 Opus) |

The one number this method produces is masterrig's Fable:Opus ratio of 2.350. Its interval spans
a factor of 8.0, it contains Shellac's Fable point value of 2.500 and the published Fable
interval of 1.8 to 4.0 times Opus, and it comes from the account that prices nothing absolute.
It constrains nothing: adopting it would move the page's Fable tokens-per-window row by +6%,
which is well inside its own interval. Verdict for it: agrees with the existing interval, and
adds no information to it.

The headline of this section is the Opus:Sonnet row. **The meter's Opus:Sonnet ratio cannot be
measured from single-model stretches with the data we have**, on any account, in either regime,
because no stretch anywhere is 95% Sonnet. It gets a number in section 3 and only there.

## 3. The same rates fitted across every clean stretch

Fitting `delta_pct = sum over families of (input-equivalent tokens x rate)` by non-negative
least squares, with the Opus rate scaled afterwards to 10/15 so the fit reads in credits. The
three gs fits at output 5x:

| Fit | n | Window (credits per 1%) | Sonnet rate | Fable rate | Residual \|rel\| median |
|---|---|---|---|---|---|
| jwork, before 14 Sep | 41 | 189,609 [185,050, 193,757] | **0.617 [0.521, 0.752]** | 1.170 [1.079, 1.285] | 0.055 |
| jwork, after 14 Sep | 14 | 270,028 [244,017, 293,986] | 0.525 [0.345, 0.831] | 2.532 [2.216, 2.883] | 0.065 |
| dave, after 14 Sep | 14 | 165,647 [157,477, 175,991] | 0.518 [0.359, 0.672] | 1.284 [1.165, 1.895] | 0.060 |

and the ratios those imply, against Shellac's 1.667 and the API list ratio of 2.5 from
`data/prices.json` (Opus input $5 per million against Sonnet's $2):

| Fit | Opus ÷ Sonnet | Verdict against Shellac's 1.667 | Sonnet row moves | Fable ÷ Opus |
|---|---|---|---|---|
| jwork, before | 1.081 [0.886, 1.280] | **disagrees** | −35% | 1.754 [1.619, 1.927] |
| jwork, after | 1.271 [0.803, 1.933] | agrees | −24% | 3.798 [3.324, 4.324] |
| dave, after | 1.288 [0.988, 1.815] | agrees | −23% | 1.927 [1.748, 2.842] |

Read together: every fit puts Sonnet's rate above Shellac's 0.400 and the Opus:Sonnet ratio
below 1.667, and all three are far below the API list ratio of 2.5, which no interval reaches.
The largest fit, jwork's 41 pre-14-September stretches, excludes both 0.400 and 1.667 from its
interval. The two 14-stretch fits contain 1.667 and cannot discriminate. So the meter's
Opus:Sonnet ratio is between about 1.08 and 1.29 by point estimate, against Shellac's 1.667:
Sonnet costs more against the meter, relative to Opus, than the January table says. At output
3x the pattern holds with the ratio nearer Shellac's (1.342, 1.309, 1.452), so part of the gap
is the output multiplier rather than the input rate; the data here cannot separate the two.

Haiku appears in no gs fit at all: no gs stretch carries enough Haiku for the fit to include it.
It appears only in masterrig's, where the fitted rate is 0.000 [0.000, 5.601] before the cut and
0.372 [0.000, 1.591] after, both useless. **Haiku's rate is not measurable from our data.**

Fable's fitted rate disagrees with itself across jwork's two regimes: 1.754 [1.619, 1.927] times
Opus before 14 September and 3.798 [3.324, 4.324] after, intervals that do not overlap, with
dave's 1.927 [1.748, 2.842] beside them. Either Fable's rate changed, or the five-hour window
changed and the fit is absorbing it into the rate with the fewest stretches. The window fitted
on the same jwork stretches moves the same way (189,609 to 270,028 credits per 1%), so these two
cannot be separated here either.

Comparison with section 2: there is nothing on gs to compare, since section 2 measured nothing
there. On masterrig the two methods agree, for what that is worth: section 2's Fable:Opus of
2.350 [1.489, 11.855] contains the masterrig fit's 4.111 [2.625, 5.893].

masterrig's fit is reported for completeness and should not be used: its residual |rel| median
is 0.455 before the cut against 0.055 to 0.065 on gs, which is the phantom meter movement
showing up as fit error.

## 4. The jwork/Dave gap

Fourteen clean stretches on each account after 14 September. Their median raw-token shares:

```
      jwork  opus=0.650 sonnet=0.144 haiku=0.000 fable=0.067
      dave   opus=0.783 sonnet=0.138 haiku=0.000 fable=0.065
```

Priced at four different rate tables, the median credits per 1% on each account:

| Rate table | jwork | dave | jwork ÷ dave | Verdict |
|---|---|---|---|---|
| Shellac | 219,334 | 172,104 | 1.274 [1.224, 1.408] | mix does not explain the gap |
| fitted jwork before | 220,468 | 166,108 | 1.327 [1.187, 1.401] | does not explain |
| fitted jwork after | 267,748 | 191,398 | 1.399 [1.295, 1.560] | does not explain |
| fitted dave after | 219,032 | 165,923 | 1.320 [1.203, 1.369] | does not explain |

**Model mix does not explain the gap.** The two accounts' mixes are close — the difference is
13 points of Opus share against Sonnet and Fable — and no rate table tested brings the ratio
below 1.27 or its interval near 1.0. Re-rating with the measured rates makes the gap wider, not
narrower, because jwork carries more of the models whose measured rate is above Shellac's.

**`reset_verified` does not explain it.** Only jwork after the cut has both groups: 11 verified
stretches at a median of 218,081 credits per 1% against 3 unverified at 220,588, a ratio of
0.989 [0.725, 1.044]. Dave's 14 post-cut stretches are all verified and jwork's pre-cut 41 are
all unverified, so the flag is not measurable on those. Restricting both accounts to verified
stretches only leaves the gap at 1.267 [1.185, 1.415] on 11 and 14 stretches.

**Stretch length does not explain it.** Every clean post-cut stretch on both accounts moved the
meter by 10 to 12%, so the whole-percent quantisation is ±5% on each of them and there is no
short/long contrast to test: jwork's post-cut split is 12 stretches against 2, and restricting
both accounts to stretches above 10% is not measurable (2 and 5). Dave's own split reads
1.005 [0.940, 1.187]. A length effect does exist in jwork's larger pre-cut set — long stretches
read 0.930 [0.890, 0.963] of short ones, 7% — but both accounts' post-cut stretches sit in the
same 10 to 12% band, so an effect of that size between 10% and 12% cannot produce a 27%
difference between the accounts.

The gap stands where the reconciliation left it: jwork reads 27% to 40% above dave after
14 September depending on the rate table, on the same plan, and none of the three cheap
explanations available in these files accounts for it.

## 5. The five-hour window per model, in tokens

At the measured window of 196,989 credits per 1%, 19,698,917 credits per five-hour window,
input-equivalent tokens (`input + cache_write + 5 x output`):

| Model | Shellac rate | Tokens per window at Shellac's rate | Measured rate | Tokens per window at the measured rate |
|---|---|---|---|---|
| Opus | 0.667 | 29,548,375 | 0.667 (the scale) | 29,548,375 |
| Sonnet | 0.400 | 49,247,292 | 0.525 [0.345, 0.831] | 37,550,650 [23,716,834, 57,118,664] |
| Haiku | 0.133 | 147,741,875 | not measurable | — |
| Fable | not in the table | — | 1.284 [1.079, 2.883] | 15,336,190 [6,833,899, 18,255,534] |

The window itself carries the reconciliation's own interval (19.1M to 20.4M credits), which
multiplies through every row; the table holds it at the pure-Opus median so the effect of the
rate is visible on its own. The measured rates are the median and the union of the intervals of
the three gs fits at output 5x (Sonnet 0.518, 0.525, 0.617; Fable 1.170, 1.284, 2.532).

## What the publisher should adopt

| Family | Rate, credits per input token | Source | Interval | Status |
|---|---|---|---|---|
| Opus | 10/15 = 0.667 | Shellac | — | the scale every other rate is measured against; not itself tested here |
| Sonnet | 0.525 | measured, three gs fits | [0.345, 0.831] | measured; Shellac's 0.400 is inside the interval but below all three point estimates |
| Haiku | 2/15 = 0.133 | Shellac | — | unverified: no clean stretch on any account carries enough Haiku to fit it |
| Fable | 1.284 (1.93x Opus) | measured, three gs fits | [1.079, 2.883] = 1.62x to 4.32x Opus | measured, and the two jwork regimes disagree with each other |

Output rates stay at 5x input for Opus, Sonnet and Haiku (Shellac's table). For Fable the
output ratio is still not separable, so both candidates are carried, as
`data/prices.json` already does.

The change is to the `_credits.per_family` block that PR #65 adds to `data/prices.json`; nothing
in `tracker/credits.py` needs new logic beyond returning a measured rate and its interval where
one exists. PR #65 owns those files and is in its review gate, so this is written out rather
than applied:

```json
"sonnet": {
  "matches": "sonnet",
  "input": [6, 15],
  "output": [30, 15],
  "measured_input": 0.525,
  "measured_input_interval": [0.345, 0.831],
  "measured_input_source": "docs/findings-2026-09-20-model-rates.md section 3: the median and the union of the 80% intervals of the three gs fits at output 5x (jwork before 0.617 [0.521, 0.752], jwork after 0.525 [0.345, 0.831], dave after 0.518 [0.359, 0.672]). Reproduce with python3 -m tools.model_rates history/masterrig-passive.json. Shellac's 6/15 = 0.400 is inside the union interval and below all three point estimates; the Opus:Sonnet ratio measures 1.08 to 1.29 against Shellac's 1.667 and the API list ratio of 2.5.",
  "status": "measured"
},
"haiku": {
  "matches": "haiku",
  "input": [2, 15],
  "output": [10, 15],
  "status": "unverified",
  "status_source": "docs/findings-2026-09-20-model-rates.md section 3: no clean stretch on any watched account carries enough Haiku to fit a rate (the highest Haiku share of any stretch is 0.111, on masterrig). Shellac's row stands as a reference, untested by our data."
},
"fable": {
  "matches": "fable",
  "input": null,
  "output": null,
  "output_ratio_candidates": [3, 5],
  "measured_input_interval": [1.079, 2.883],
  "measured_input_source": "docs/findings-2026-09-20-model-rates.md section 3: the union of the three gs fits at output 5x, 1.62x to 4.32x Opus. It extends the 1.8 to 4.0 in docs/findings-2026-09-20-reconciliation.md at the low end. jwork's two regimes fit 1.754 [1.619, 1.927] and 3.798 [3.324, 4.324] times Opus with no overlap, so a single Fable rate is not supported and the interval is the claim."
}
```

Three things follow for the published page, if these are adopted:

1. The Sonnet tokens-per-window row falls from 49.2M to 37.6M input-equivalent tokens, −24%,
   and gains an interval of 23.7M to 57.1M.
2. The Haiku row keeps its number and gains the status "unverified"; it is the one row on the
   page that no measurement of ours touches.
3. The Fable interval widens at the low end, from 1.8x Opus to 1.62x, and the page should say
   that the fitted Fable rate differs between the two sides of 14 September by more than its
   own interval, which is either a rate change or the five-hour window moving.

The Opus row cannot be checked by this work at all. Every rate here is measured relative to
Opus, and Opus is fixed at Shellac's 10/15 to set the scale; if that one row is wrong, every
credit figure on the page scales with it and nothing in our data would show it.

## After PR #65: the run file is now the exclusion, and what that moved

PR #65 merged, bringing `tracker/credits.py` -- the selection rule, the rates and the Fable
solve in one module, imported by the publisher, `tools/reconcile_window.py` and
`tools/credits_report.py`. Its exclusion list was built from `history/probes.jsonl` and
`data/effort_matrix.json`, so it knew the 16 completed runs and none of the 10 that aborted or
crashed. `history/harness-runs.jsonl` is now the single source: `tracker.credits.harness_runs()`
reads that file and nothing else, `clean_stretches` takes what it yields, and the two
original files are inputs to `tools/harness_runs.py` alone. Reproduce with:

```
python3 -m tools.harness_runs
python3 -m tools.model_rates history/masterrig-passive.json
python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
    --gs-passive history/gs-passive.json --out /tmp/claude-usage-55.json
python3 tools/credits_report.py --publish-check /tmp/claude-usage-55.json
```

**Clean stretches removed, per account.** One, on jwork.

| Account | Old rule (16 runs) | New rule (26 runs) | Removed |
|---|---|---|---|
| jwork | 56 | 55 | 2026-09-09T23:13:14 to 2026-09-10T01:02:44 |
| dave | 14 | 14 | none |
| masterrig | 211 | 211 | none |

The removed stretch contains the probe that crashed on 2026-09-10 between 00:02:18 and
00:57:14, which wrote no `history/probes.jsonl` row. The other nine unrecorded runs fall
outside every clean stretch: three of the four crashed runs and three of the six aborted ones
are dave's before 2026-09-15, and dave's stretch record starts on 2026-09-15. The counts above
are this document's selection (capture-accepted, masterrig exempt from the capture gate). The
publisher's own selection, which exempts nobody, moves the same way: jwork 56 to 55, dave 14,
masterrig 1 (its capture column is not yet usable).

**Every figure in the published `credits` block that moves.** 86 of its 496 leaves differ, and
82 of those are the `harness_runs_excluded` list itself, which grows from 16 rows to 26 and
carries a run's outcome in its reason ("probe claude-opus-5/low completed from ...",
"probe aborted: tick too early from ..."). The four that are figures:

| Figure | Old | New |
|---|---|---|
| `five_hour_window_across_cut.per_account.a2.before` | 187,168 | 187,797 |
| `five_hour_window_across_cut.per_account.a2.change_pct` | 9.3 | 9.0 |
| `five_hour_window_across_cut.per_account.a2.n_before` | 41 | 40 |
| `five_hour_window_across_cut.per_account.a2.n_with_capture` | 56 | 55 |

`a2` is the published label of the account this document calls jwork. Everything else holds:
`window_credits` (the 19.7M-credit five-hour window and its interval), `per_model`, `sessions`,
`fable_interval`, `window_credits_from_weekly` and every rate. The publish check agrees:
`All 496 figures reproduce from the history files`, exit 0.

The publisher's `n_before` of 41 and 40 is one below this document's 42 and 41 for the same
account and era. The difference is not the exclusion: one jwork stretch of 2026-09-05 carries a
`<synthetic>` model key with all four token counts at zero, which `tracker.credits.price_tokens`
treats as a model no family covers and so unpriceable, while `tools/model_rates.py` drops a
family only when it carries tokens. Neither reading changes a figure here; the convention is
noted so the two counts can be reconciled.

**Does the measured Opus:Sonnet ratio confirm the 1.667 in `data/prices.json` `_credits`? No.**
The largest fit, jwork's 41 clean stretches before 14 September, measures 1.081 with an 80%
interval of [0.886, 1.280], which **excludes 1.667**. The two 14-stretch fits after the cut
measure 1.271 [0.803, 1.933] on jwork and 1.288 [0.988, 1.815] on dave; both contain 1.667 and
neither can discriminate at that n. All three point estimates are below it, and all three are
far below the API list ratio of 2.5.

**No rate in `data/prices.json` was changed.** Sonnet's 6/15 stands exactly as PR #65 published
it. Adopting the measured rate would move a published row (Sonnet's tokens per window falls
24%, from 49.2M to 37.6M), and that is Jonathan's decision, not this branch's. The rate table
this document recommends is still written out above as the change to make, not applied.

## Follow-ups, not done here

1. **Keep the run file current.** `tools/harness_runs.py` reads two machine-local ops logs; the
   committed JSONL is the record for anyone without them. It should be rebuilt whenever a probe
   runs, which is the same moment `history/probes.jsonl` gains a row.
2. **Adopt the rate table** into `data/prices.json`'s `_credits` block, as written above. It
   moves a published row, so it is Jonathan's call rather than this branch's.
3. **Decide what the aborted runs mean for the probe itself.** Six probes aborted and four
   crashed in eleven days, all of them after sending prompts. The allowance they spent is
   gone either way; what the exclusion buys back is only the stretches they contaminated.

## Limits

- Every rate is relative to Opus. Nothing here tests the absolute credit values.
- The fits assume one input rate and one output rate per family, with the output rate a fixed
  multiple of the input rate (5, and 3 shown beside it). A model whose output ratio differs
  loads that difference onto its input rate.
- Cache reads are priced at zero, as `tracker.credits.price_tokens` is called here with a
  weight of 0. The reconciliation's 0 to 0.015 uncertainty on that weight is carried untested.
- The meter reads whole percent, so every stretch carries ±5% of quantisation at these
  delta_pct values; that is the floor on every per-stretch figure above.
- gs has no ruff. `ruff` runs locally before the gate.

## Verification

```
$ python3 -m unittest tests.test_model_rates tests.test_harness_runs tests.test_credits \
      tests.test_credits_report tests.test_publish tests.test_rebuild_offline
Ran 243 tests in 0.346s
OK
```

That is every test file touching code this branch changed. `tests/test_harness_runs.py` covers
the log parser -- run splitting at each terminator, the account attribution rule, the date
resolution for the log's `HH:MM:SSZ` lines, the meter bracketing of an undated run -- and that
what it writes is what `tracker/credits.py` reads. `tests/test_credits.py` covers the new
exclusion source: every row becomes a run on its own account, an aborted probe excludes a
stretch no `history/probes.jsonl` row could have excluded, a row without a span is not a run, a
missing file excludes nothing, and the committed file holds every probe row's span and more.
