# What the publish check did not check, 2026-09-20

The reproducibility check covered the `credits` block and nothing else, and said "All 911
figures reproduce" while 6,636 published figures -- the headline percent, the onset dates,
the windows per week, the rates, the history, the effort cells, both basis blocks and the
whole reference block -- were compared to nothing at all. This is the record of what the
check now covers, what a covered block means for each kind of block, what it still cannot
say, and the two inconsistencies and two missing figures the same review found in the
published file.

Reproduce everything with:

```
python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
    --gs-passive history/gs-passive.json --out /tmp/claude-usage-58.json
python3 tools/credits_report.py --publish-check /tmp/claude-usage-58.json
```

No traffic was sent for this and no account was driven. Every input is a file already in
the repository.

## 1. The check rebuilds the document, not a block of it

`--publish-check` used to rebuild `credits` by calling `tracker.publish._credits_block`
with inputs it assembled itself. It now calls `tracker.publish.rebuild_public_json`, which
is **the same function `tracker.publish.main` calls to write the file in the first place**.
That is the point of the change and not an implementation detail: a check that assembles
the document its own way proves its own arithmetic, and the two can agree on a figure that
is wrong in both. One code path, read off the same files, at the publish's own instant.

`main` is now three lines around that call plus the output-weight update it has always run
first. Nothing about the published document changed because of the refactor: against a
publish built at the parent commit, exactly two figures moved and 49 are new, all of them
items 4 to 6 below (section 7 lists them).

| Figures the check compares | Before | After |
|---|---|---|
| An offline publish (`--probes --passive --gs-passive`) | 911 of 7,547 | **7,596 of 7,596** |
| The daily publish (`bin/daily.sh`, which also passes `--contributed`) | 911 of 7,641 | **7,690 of 7,690** |

The file grew by 49 figures in this change (items 2 to 4 below), which is why 7,547 becomes
7,596 rather than staying put.

## 2. Which blocks are covered, and what "covered" means for each

The check prints this table before the figure-by-figure listing. Every block is compared
the same way -- leaf by leaf, numbers to 0.5%, strings exactly -- so the `kind` column is
what the comparison *means*, never a filter.

| Block | Kind | Figures | What it is compared against |
|---|---|---|---|
| `credits` | derived | 925 | the stretches, probe rows, effort-matrix runs, weekly points and rates, through `_credits_block` |
| `weekly_windows` | derived | 5,094 | `history/passive.json` and `history/gs-passive.json` window points, through `_weekly_block` |
| `history` | derived | 1,107 | the passive dollar series split into regimes and held flat -- a step function, **not** a pass-through of the raw daily rows |
| `rates` | derived | 141 | the current regime's held value, converted on the frozen reference mix |
| `reference` | derived | 74 | the table's constants, plus the new `shortfall` recomputed from the weekly regimes |
| `events` / `last_change` | derived | 59 / 57 | `detect_smoothed_changes` and `_weekly_block`'s own events, with the across-the-cut figures |
| `effort` / `effort_usd` | derived | 15 / 15 | `data/effort_matrix.json` `_meta.runs`, repriced at the current table (`calibrate.recompute`) |
| `model_plan_limits` | constant | 36 | the policy table keyed by the priced models |
| `weekly_window_ratios_basis` | constant | 13 | the reference constants, now including `as_of` |
| `plan_ratios_basis` | constant | 8 | the same |
| `availability`, `instrument`, `last_sample_at`, `meter_read_at`, `passive_account_count`, `probe_account_count` | derived | 8 total | the evidence status the readings produce |
| `plan_ratios`, `weekly_window_ratios`, `plan_measured`, `rate_basis`, `schema_version` | constant | 9 total | the constants the running checkout holds |
| `api_price_per_mtok` | pass-through | 30 | `data/prices.json`, compared as data |
| `session_tokens` | pass-through | 3 | `history/passive.json` `session_tokens`, compared as data |
| `passive_generated_at` | pass-through | 1 | `history/passive.json` `generated_at`, compared as data |
| `contributed` | pass-through | 94 | `data/contributed.json`, compared as data, and read **only when the published file carries the block** |
| `generated_at` | from the publish | 1 | nothing: it is the instant the rebuild is made at |

An unlisted block would be counted as `derived (unclassified)` and named in that column
rather than silently omitted; `tests/test_credits.py` asserts a publish leaves nothing
unclassified.

**`history` is derived, not pass-through.** Its rows read like a daily series carried
across, and they are not: the published `history` is the measured limit as a step function,
each regime held flat at the median of its own readings, with `source`
"passive"/"derived"/"held" per day. All 1,107 of its figures are recomputed.

**An empty object or array is now a figure of its own.** `_leaves` used to return nothing
for one, so a block that gained its first entry -- a plan's first weekly regime, a family's
first fit -- was indistinguishable from a path that had never been there. There are 21 such
containers in a current publish (`weekly_windows.pro.regimes`,
`weekly_windows.max20.history[*].reasons`, `credits.per_model.haiku.measured_rate.per_fit`
and the rest), and they now read `<empty object>` / `<empty array>` on both sides.

## 3. What the check still cannot say

- **`generated_at` is taken from the published file, so it is trivially equal.** It has to
  be: the weekly block's current regime, the history's last day and every freshness field
  are bounded by the publish time, so rebuilding at "now" would compare two different
  questions. One figure of 7,596.
- **It reproduces *a* publish from the files as they stand, not any past publish.** Several
  of the inputs are rewritten between publishes: `tracker/weight.py` rewrites
  `data/prices.json` when the weekly output run supplies a new output class weight,
  `tracker.contributed` rewrites `data/contributed.json` daily, and
  `history/passive.json` and `history/gs-passive.json` are replaced on every run. Checking
  last week's published file against today's history files is a real failure and the check
  reports it as one; that is correct behaviour and it is not a claim that the old publish
  was wrong.
- **Two inputs are read from the checkout rather than from a flag.**
  `data/reference_mix.json` (through `tracker.publish.REFERENCE_MIX`) and
  `history/harness-runs.jsonl` (through `tracker.credits.HARNESS_RUNS_PATH`) are anchored
  to the module's own directory, so `--publish-check` cannot be pointed at an archive's
  copies of them. That was already true of the credits-only check. The `--harness-runs`
  flag on `tools/credits_report.py` applies to the report, not to the check.
- **It is a reproducibility check, not a correctness check.** It proves the published
  figures are what the committed code and the committed files produce. It says nothing
  about whether that arithmetic is the right arithmetic; that is what the findings
  documents and the review rounds are for.
- **The measured rates are still read from `history/model-rates.json`, never from the
  published JSON.** A per-model row published at a rate the committed fit does not hold
  fails, as it did before.

## 4. One source, two dates -- fixed

`plan_ratios_basis` and `weekly_window_ratios_basis` published `dated: false` while
`reference.as_of` and `credits.window_credits_from_weekly.weekly_cap_baseline_source.as_of`
both published `2026-01-25` for the same URL. The page could therefore print one source
with two dates, which is the failure mode the dated reference exists to prevent.

`CREDITS_TABLE_AS_OF` is now declared beside `CREDITS_TABLE_URL`, above every block that
uses it, and all four read it. Both basis blocks gained `as_of` and read `dated: true`.
`tests/test_publish.py::ReferenceDateTests` asserts the four agree on the date and on the
URL, that neither basis block still claims to be undated, and that moving the constant
moves the blocks that read it at call time.

The date is the reconciliation's: the article was crossposted on 25 January 2026
(`braddelong.substack.com, "CROSSPOST: SHELLAC", dated January 25, 2026`), so its table
describes the plans as they were in January.

## 5. Goal 7 published: `reference.shortfall`

The 0.86 pre-cut shortfall of both plans against the January table was explained only in
`docs/findings-2026-09-20-reconciliation.md`. It is now a published block, computed from
the weekly regimes at publish time and recomputed by the check like everything else.

| Plan | Measured windows per week | Documented (January) | Ratio | Expected after the announced changes | Measured over expected |
|---|---|---|---|---|---|
| max20 | 6.48 | 7.58 | **0.8549** | 5.6818 | 1.1405 |
| max5 | 10.86 | 12.63 | **0.8599** | 9.4697 | 1.1468 |
| pro | -- | 9.09 | -- | 6.8182 | -- |

`status` is **`explained`**. The reconciliation's explanation is that the table predates the
6 May five-hour doubling and the May and 14 September weekly changes, so its windows per
week are not comparable to the measured levels; every change it rests on was announced for
"Pro, Max, Team and seat-based Enterprise", so it reaches every plan the page draws.
`SHORTFALL_EXPLAINED_PLANS` names those plans and the status is computed against the plans
that actually carry a measured ratio, so a plan the explanation does not reach would leave
the block `open` rather than silently inheriting "explained".
`tests/test_publish.py::ShortfallTests` covers both branches.

Three things worth stating plainly about the numbers:

- **The measured figure is the plan's own last regime ending before the cut**, not a
  weekly median and not the current value. For max20 that is the 2026-08-15 to 2026-09-14
  regime at 6.48; for max5 the 2026-06-13 to 2026-08-14 regime at 10.86. A regime still
  running across the cut is not a pre-cut reading, and `_pre_cut_regime` tests the
  regime's own end stamp rather than its place in the list.
- **The 2026-09-20 review quoted 10.91 for max5; this history yields 10.86.** 10.91 appears nowhere
  in the current history files -- the only occurrence in the repository is a fixture row in
  `tests/test_publish.py`. The pooled regime reads 10.86 and the median of the nine weekly
  buckets reads 10.89. The publisher computes the figure rather than copying one, so the
  block states 10.86; max20's 6.48 matches the review's figure exactly, which is what
  identifies the regime's `windows` as the quantity meant.
- **Pro publishes nulls and a status, not a scaled figure.** Its weekly windows are
  inferred from max20 by `WEEKLY_WINDOW_RATIOS`, which come from this same table; dividing
  that back by the table would publish the max20 ratio a second time and call it a
  measurement.

`ratio_to_expected` is the number the reconciliation calls "the five-hour window being 20M
rather than 22M": both plans read about 1.14 against the expected level, and they agree
with each other to within 0.006, which the single-plan arithmetic in the document could not
show.

## 6. A date for the credits figures: `credits.as_of`

`credits.per_model.*` carried no date at all, so the page printed a neighbouring block's
under them. Two readings stand behind those figures and they are not the same date:

| Field | Value on this publish | What it is |
|---|---|---|
| `credits.as_of_source.window_cluster` | 2026-09-18T22:14:09+00:00 | newest stretch end in the pure-Opus cluster `window_credits` is the median of |
| `credits.as_of_source.measured_rate_fits` | 2026-09-20T00:21:09+00:00 | newest stretch behind the pooled per-model fits |
| `credits.as_of` | **2026-09-20T00:21:09+00:00** | the later of the two |

Each per-model row carries the date its own figures rest on:

| Row | `rate_source` | `as_of` | Why |
|---|---|---|---|
| opus | reference | 2026-09-18T22:14:09+00:00 | the anchor has no measurement date; the row is the window divided by a constant |
| sonnet | measured | 2026-09-20T00:21:09+00:00 | the window, and the fit that produced the rate |
| fable | measured | 2026-09-20T00:21:09+00:00 | the same; the row publishes an interval and no value |
| haiku | measured | `null` | the row publishes no figure at all, so it publishes no date either |

The fits' date is recomputed rather than read: `history/model-rates.json` records
`fits_pooled` (`jwork/pre`, `jwork/post`, `dave/post`) and each fit's `n`, but no spans.
`tracker.credits.fits_as_of` takes the accounts those keys name and returns the newest end
over their capture-accepted, harness-clean stretches whose every model the credit table can
price -- which is exactly the selection `tools/model_rates.py` builds its design matrix
from (its `ok` column is `price_tokens(...).priced`). On this history that selection is 55
jwork stretches and 14 Dave ones, matching the fits' own n of 41 + 14 and 14 exactly, so
nothing the fits used is outside it and nothing inside it was dropped. The account names
stay inside the function; only the date comes out, and the no-account-names test covers the
whole document as before.

`newest_end` orders by the parsed instant and not by the string. One watched account writes
`+02:00`, and a lexical maximum over mixed offsets reads the wrong stretch as the newest.

## 7. Every published figure that moved

Two moved, 49 are new, none were removed.

| Figure | Before | After |
|---|---|---|
| `plan_ratios_basis.dated` | false | **true** |
| `weekly_window_ratios_basis.dated` | false | **true** |

New: `plan_ratios_basis.as_of` and `weekly_window_ratios_basis.as_of` (2);
`credits.as_of` and `credits.as_of_source.*` (4); `credits.per_model.<family>.as_of` and
`.as_of_source` (8); `reference.shortfall.*` (35).

## 8. Verification

```
$ python3 -m unittest tests.test_credits tests.test_publish
Ran 205 tests in 0.734s
OK

$ python3 -m unittest discover -s tests -t .          # the whole suite
Ran 846 tests in 8.960s
OK (skipped=1)

$ python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json \
      --gs-passive history/gs-passive.json --out /tmp/claude-usage-58.json
$ python3 tools/credits_report.py --publish-check /tmp/claude-usage-58.json
All 7596 figures reproduce from the history files.
$ echo $?
0
```

The check was also run against a published file with six figures moved by hand, one per
newly-covered block, and reported all six and exited 1:

```
6 of 7596 figures do not reproduce:
  credits.as_of: published 2020-01-01T00:00:00+00:00, recomputed 2026-09-20T00:21:09+00:00
  history.claude-opus-5[3].api_value_per_window: published 0, recomputed 430.03, off by 43003.00%
  last_change.percent: published 99, recomputed 23, off by 76.77%
  rates.claude-opus-5.tokens_per_window: published 1, recomputed 588,854,697, off by 58885469600.00%
  reference.shortfall.per_plan.max5.ratio: published 0.5, recomputed 0.8599, off by 71.98%
  weekly_windows.max20.current: published 1, recomputed 4.96, off by 396.00%
```

The daily publish's shape (with `--contributed data/contributed.json`) reproduces too, at
7,690 figures.

`ruff` is not installed on the host this was written on, so it has not been run here; it
runs locally before the gate.

## 9. Limits

- **The offline rebuild still reads the running checkout's measured rates and reference
  mix.** `tracker/rebuild_offline.py` has no `model_rates` argument yet
  (`docs/findings-2026-09-20-measured-rates.md` section 6 already records this), and the
  check inherits it: pointed at an archive it would divide by today's rates.
- **`CONTEXT.md` still has no glossary entry for `credit`**, and now none for `shortfall`
  either. Outside this change's file list, worth its own small change.
- **The `shortfall` block's `status` is a judgement about a document, held as a constant
  with a computed test around it.** `SHORTFALL_EXPLAINED_PLANS` says which plans the
  reconciliation's explanation reaches, and the publisher computes the status from the
  plans that carry a measured ratio. Nothing in the data can check that the constant names
  the right plans; the test that guards it is the one asserting `PRE_CUT_MULTIPLIERS` match
  the entries in `PLAN_CHANGES_SINCE_REFERENCE`.
- **The masterrig account still has no usable capture column**, so the pure-Opus cluster
  behind `credits.as_of_source.window_cluster` is two accounts wide, as before.
