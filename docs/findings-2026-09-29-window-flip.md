# Why the 14 September window marker flipped from -5% to +6% (2026-09-29)

Issue #127, item 1. The page's per-regime window levels show the Max 20x five-hour window
rising 6.4% at the 14 September weekly cut (450,593,800 to 479,431,803 reference-mix tokens,
`credits.window_tokens.regimes`, live file 2026-09-28T23:00:57Z). Before PR #99 (merge
9b5b6ba) the same marker read about -5%.

## Where the marker comes from

The second window regime is `before_cluster_scaled_by_five_hour_change`: the before cluster
times 1 + `window_tokens.five_hour_window_pct` / 100 (`credits.current_cluster_rule`). That
percent is `credits.five_hour_window_change`, the plain median of each qualifying account's
`change_pct` in `credits.five_hour_window_across_cut`. Each account's `change_pct` is its
median credits per 1% of the five-hour meter on stretches starting after `CUT_AT`, over the
same median before it. The stretches are the rate fits' selection (`rate_fit_stretches`),
priced at the pooled fit's rates from `history/model-rates.json`. Two accounts qualify: a1
(masterrig) and a2 (jwork). a3 and a4 have no stretches before the cut.

## Reproduction

The inputs are the committed files either side of the merge:

- old counting: `history/gs-passive.json` at 2c5c98b (20:30Z publish, pre-#99 code);
- new counting: `history/gs-passive.json` at c73bf1e (23:00Z publish).

`history/masterrig-passive.json` and `history/model-rates.json` are byte-identical at the
two commits. The published figure is reproduced exactly:

| gs file | a1 before | a1 after | a1 | a2 before | a2 after | a2 | median |
|---|---|---|---|---|---|---|---|
| old (2c5c98b) | 166,168 (34) | 155,810 (28) | -6.2% | 192,663 (50) | 186,070 (36) | -3.4% | **-4.8%** |
| new (c73bf1e) | 166,168 (34) | 155,810 (28) | -6.2% | 195,878 (54) | 233,170 (41) | +19.0% | **+6.4%** |

Credits per 1%, with the stretch count in brackets. The brief's per-account figures (-8% and
+15%) are `window_tokens.account_regimes`: a1 383.1M to 353.2M, a2 451.6M to 519.6M. These
come from the same medians bounded to the 14 to 22 September regime.

## Which input moved it

**a1 did not move.** masterrig's stretch file has not been regenerated since #99: its last
push (e5d2d51, 15:15Z) ran pre-#99 code. a1 is still counted the old way.

**Unclaimed gs work had no effect.** `across_cut` reads each stretch's `tokens`, and #99
keeps the pooled root's headless work beside the stretch, in `unclaimed_tokens`. On every a2
stretch the input, cache-write and cache-read totals are identical in the two files. Only
the known-date test and the joint fit add an unclaimed share.

**Weighting had no effect.** The marker is an unweighted median of per-stretch ratios, and a
median of two accounts.

**The final-usage fix moved a2, and it moved the two sides unevenly.** On the 81 a2 stretches
selected under both countings, the new/old credit ratio per stretch has a median of:

| side | stretches | median new/old credits |
|---|---|---|
| before 14 Sep | 47 | 1.021 |
| 14 to 22 Sep | 25 | 1.195 |
| after 22 Sep | 8 | 1.293 |

The old reader undercounted output only on responses whose first transcript line carried
the stream's opening usage. That depends on the work:

- Fable output is unchanged on every side (x1.00).
- Before the cut, Opus 5 output rose x1.02.
- From 14 to 22 September, Opus 5 output rose x2.22, and Sonnet output x3.30 (x6.55 before
  the cut, but on only 0.25M tokens).

a2's pre-cut work was Fable-heavy. Its post-cut work was Opus 5 and Sonnet. So the recount
added about 2% before the cut and about 20% after it.

**Which stretches are in each side moved it by only a point or two.** New capture readings
moved 15 stretches into a2's selection and 6 out. On the stretches selected both times, a2's
14 to 22 September change is +13.0% with the new tokens. On the full new selection it is
+13.5%.

## Real measurement or artefact

The sign flip is a correction, not an artefact. The old -3.4% for a2 came from undercounting
its after side. The new counting agrees with Claude Code's own per-session cost accounting
(docs/findings-2026-09-28-scatter.md), and it applies the same rule on both sides of the cut.

The published +6.4% still carries three artefacts, all at the time of writing:

1. **Rates fitted on the old counts.** The committed `history/model-rates.json` was fitted at
   2026-09-28T00:31Z, before the fix. I refitted it offline with `tools/model_rates.py`
   (same command as `bin/daily.sh`, JSON to a scratch file, nothing committed):

   | fit | Sonnet | Fable | Opus 5.5 (x Opus) |
   |---|---|---|---|
   | committed | 0.526 | 2.102 | 0.776 |
   | old counts | 0.535 | 2.111 | 0.754 |
   | new counts | 0.406 | 2.159 | 0.687 |

   At the new-count rates, a2 reads +14.1% and a1 -4.9%, so the marker is **+4.6%**. The
   daily refit in `bin/daily.sh` fixes this by itself on the first publish of 2026-09-29.

2. **a1 is still on the old counting.** The median mixes one account counted before #99 with
   one counted after it. `bin/passive.sh` ran its joins on the code of the previous run's
   pull and pulled only before committing, so masterrig's record trailed main by one run.
   The first masterrig run after #99 would still have counted the old way.

   Fixed in this branch: `bin/passive.sh` now pulls before the joins, and
   `tests/test_deploy_scripts.py` has a test for it. After this merges, masterrig's next
   daily run counts a1 the new way. Masterrig's rebuild in #99's findings raised its output
   by 22%, so a1's after side will probably rise too. How far cannot be measured from gs:
   masterrig's transcripts are not on this host.

3. **The after side is not bounded at the next regime.** `across_cut` places every stretch
   that starts after `CUT_AT` on the after side. That includes stretches after the 22
   September five-hour change, which open their own regime (a2: 11 of 41). This value scales
   the 14 to 22 September regime only.

   | rates | a2, after side unbounded | a2, bounded at 22 Sep | marker, unbounded | marker, bounded |
   |---|---|---|---|---|
   | committed | +19.0% | +13.5% | +6.4% | +2.9% |
   | refit on new counts | +14.1% | +14.1% | +4.6% | +4.6% |

   On consistent rates the bound changes nothing today. It is still the wrong population, and
   it would matter whenever the rates and a later change are confounded. The fix belongs in
   `tracker/credits.py`, which another seat owns. It is written up in the PR.

Once these settle, what is left is a real disagreement between the two accounts: a1 about -5%
(old counts), a2 about +14% (new counts). With two accounts the median is their mean, so the
marker is not a stable estimate of the window. It should be read as unresolved until a1 has
been recounted.

Nothing here is hand-set. No date is pinned and no figure is overridden.
