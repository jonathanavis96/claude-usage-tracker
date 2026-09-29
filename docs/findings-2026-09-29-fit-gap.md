# Why the 22 September five-hour figure read +29.8% in #101 and +6.3% in production

Issue #129. Written 2026-09-29. It follows docs/findings-2026-09-29-rates-and-missing-work.md
(PR #101).

## The two figures

| | five-hour limit change | weekly limit change | known-date test | joint fit's r (Opus 5.5 / Opus 5) |
|---|---|---|---|---|
| PR #101, "rebuilt locally from the same regenerated stretch files" | +29.8% [-19.9, +65.1] | +29.6% [-13.3, +93.6] | +40.2% [27.1, 54.7], provisional | 0.795 |
| production, gs publisher at 2026-09-29T11:01Z | +6.3% [-20.9, +29.6] | +7.6% [-21.9, +48.1] | +10.8% [-0.3, 23.1], measuring | 0.635 |

Both ran the same code: the 11:01Z publish was the first after #101 merged (10:46Z), and the
code in #101's rebuild root differs from main's only in docstrings and one published method
string.

## The cause: masterrig's stretch record was still counted the pre-#99 way

The whole gap is one input file, `history/masterrig-passive.json` (Max account 1's
stretches).

- On main, that file is masterrig's own commit e5d2d51, "Passive history 2026-09-28", made at
  15:15Z on 28 September. PR #99 (merged 20:45Z the same day) changed `tracker/turns.py` to
  read each message's final streamed usage instead of its first line. Masterrig's record was
  made before that, so it still counts output tokens the old way.
- `bin/passive.sh` runs hourly but exits at once while its stamp is under 20 hours old. The
  stamp was written at 15:15Z on 28 September, so masterrig did not join again until the
  12:15Z tick on 29 September. Pulling main before the joins (3a094a7) made sure a run uses
  main's code, but did not make a run happen.
- gs's own record (`history/gs-passive.json`, Max accounts 2 to 4) is regenerated before
  every publish, so it took #99's count within half an hour. From 20:45Z on 28 September until
  masterrig's next run, every publish compared gs's stretches counted one way with
  masterrig's counted the other.
- #101's rebuild used a masterrig record regenerated locally on 28 September with #99's code
  (`until` 2026-09-28T17:00Z). That is the file its findings describe as "masterrig's
  regenerated on 2026-09-28 with the same code". It never reached `history/`.

The two masterrig records hold the same 261 stretches with the same boundaries. They differ
in the tokens of 182 of them:

| Max account 1 stretches | n | output tokens, main's record | #99's count | ratio | median credits ratio |
|---|---|---|---|---|---|
| 14 to 22 Sep (before side) | 45 | 8,576,912 | 13,246,486 | 1.54 | 1.075 |
| from 22 Sep (after side) | 10 | 2,613,288 | 4,453,470 | 1.70 | 1.141 |

The old count loses more output after 22 September, when Opus 5.5 does most of the work.
So on main's record the after side's credits per meter percent look about 6% lower than
they are, relative to the before side, and a lower after-side figure reads as a smaller
limit rise.

### Why one account's record moves every account's figure

Every account's figure moves, not only Max account 1's. The publisher fits Opus 5.5's rate
jointly with the limit change at its first use (`credits.absorb_new_family_rates`,
`joint_rate_fit`). That fit pools all the accounts. Its r then replaces Opus 5.5's row in the
rates that every later figure prices stretches at, including the known-date test. Max account 1's
undercounted after side pulls r from 0.795 to 0.635. At an Opus 5.5 share of about 0.9 on
Max account 2's after side, that alone is roughly the 25-point move in that account's
figure.

Per account, known-date test (`credits.announced_change`, change % [95% interval]) and joint
fit (`joint_fit.per_account`, change %):

| | stretches before / after | known-date, production | known-date, #101 | joint fit, production | joint fit, #101 |
|---|---|---|---|---|---|
| Max account 1 (masterrig) | 45 / 10 | +20.9 [3.6, 41.0] | +52.4 [33.9, 73.5] | +19.7 | +49.6 |
| Max account 2 | 41 / 15 | +8.2 [-8.4, 27.8] | +33.0 [11.7, 58.4] | +9.2 | +32.8 |
| Max account 3 | 52 / 5 | -18.7 [-41.5, 13.1] | -5.4 [-33.3, 34.2] | -19.2 | -6.2 |
| Max account 4 | 0 / 13 (12 in #101) | no before side | no before side | not combined | not combined |

The stretch counts either side are identical in the two builds. The only count that differs
is one more Max account 4 stretch after 22 September in production's newer gs record, and
that account has no before side. gs's two records agree on the tokens of every stretch they
share. The 03:30Z build's per-account figures (+17.3%, +9.7%, -18.4%) come from the same
masterrig record and show the same pattern.

## Reproducing both figures

Every build below is `python3 -m tracker.rebuild_offline --root <root> --now
2026-09-29T11:01:08.707311+00:00` on main's code (cae56bf). The root is a copy of main's
checkout with the files named in the row swapped in. "#101 masterrig" and "#101 gs" are the
records from #101's rebuild root. "Rates" is `history/model-rates.json`: "00:01Z" is main's (the
00:01Z fit, made on pre-#101 code), "#101" is the fit from #101's root, and "refit" is
`tools.model_rates` run on the row's own files with main's code.

| masterrig record | gs record | rates | five-hour | weekly | known-date | r |
|---|---|---|---|---|---|---|
| main's | main's | 00:01Z | **+6.3% [-20.9, +29.6]** | +7.6% | +10.8% | 0.635 |
| main's | #101 | 00:01Z | +6.3% [-20.9, +29.6] | +6.1% | +10.8% | 0.635 |
| main's | main's | #101 | +4.9% [-23.0, +28.2] | +6.1% | +9.1% | 0.614 |
| main's | main's | refit | +15.4% [-11.2, +40.3] | +16.8% | +17.9% | 0.711 |
| #101 | main's | 00:01Z | +29.3% [-17.1, +61.5] | +30.8% | +40.1% | 0.803 |
| #101 | #101 | 00:01Z | +29.3% [-17.1, +61.5] | +29.1% | +40.1% | 0.803 |
| #101 | #101 | #101 | **+29.8% [-19.9, +65.1]** | +29.6% | +40.2% | 0.795 |
| #101 | main's | refit | +35.1% [-4.7, +62.3] | +36.7% | +42.7% | 0.857 |

The first row matches the live 11:01Z JSON in every `credits`, `last_change` and `events`
field. The offline rebuild differs only in `rebuild`, `contributed` timestamps and the
`speed` block, which it does not recompute. The #101 row matches #101's own
`pub-after.json` at its own `now` (10:28Z) and at 11:01Z.

Swapping the gs record moves nothing. Swapping the masterrig record moves the figure by 23
points on either gs record and either rates file. The rates file moves it by 1 to 9
points, as the next section explains.

## The rates

- Production priced at the 00:01Z fit, made before #101 by the pre-split code (groups
  account x before/after the cut). There Opus 5.5 is 0.687x Opus, Sonnet 0.406 and Fable
  2.159. `bin/daily.sh` refits only when the file was not generated today, so #101's regime
  split (merged 10:46Z) had not reached it.
- #101's root was refitted with the split: Opus 5.5 0.524, Sonnet 0.369, Fable 2.136.
- Opus 5.5's row is replaced by the joint fit's r in both builds (above), so its
  `model-rates.json` value is not what the figures use. The file still prices every other
  family and the cache-read weight. Those move the joint fit by a few points, and more when
  a family crosses the 1.5x publication rule. Refitting on today's gs record puts Sonnet's
  80% interval at 1.52x wide, just over the rule, and moves the five-hour figure from +29.3%
  to +35.1%. That is well inside its own interval.

## Which figure is right

**#101's +29.8% is the right reading of the data it had. Production's +6.3% is wrong.** It
compares Max account 1's stretches, counted before #99's usage fix, with gs's stretches
counted after it. The undercount is uneven across the change (1.54x before and 1.70x after
in output tokens), so it biases the before-and-after comparison itself, not only a level.
Through the joint fit's r it biases every account.

Neither figure is a settled measurement. Both intervals include no change, and the state is
`measuring` in both. Moving 23 points on one input's token count is the width of the r-versus-limit
confound #101 described, seen from a different side.

No correction goes on the #101 findings. Its figure and its description of the inputs are
accurate. The inputs were never committed, so the page could not follow them until
masterrig's record caught up.

## What changed in this PR

1. **`bin/passive.sh` joins again when the join code changes on main.** The stamp now holds
   the git tree of `tracker/` the joins ran on. A fresh stamp fetches origin, still runs if
   origin's `tracker/` tree differs, and otherwise exits as before. A failed fetch, or
   an origin with no `tracker/`, keeps the plain 20-hour rule. A stamp from before this change is
   empty, so it runs once. Tests (`TestPassiveShGuard`): a `tracker/` commit on main runs
   despite a fresh stamp and records the new tree, and the next tick skips. Other commits
   on main do not run it. An empty stamp runs once. No origin tree keeps the age rule. Three of
   the new tests fail on the old script.
2. **`bin/daily.sh` refits the rates when their code or masterrig's record reaches main after
   the fit** (`rates_due`). The daily rule stays. A commit on main's first-parent line
   touching `tools/model_rates.py` or `history/masterrig-passive.json` after the fit's
   `generated_at` also triggers a refit. The first-parent line dates a merged branch by its
   merge, so a branch committed before the fit and merged after it still counts. Tests:
   `TestDailyRatesDue`.

Nothing is set by hand, and neither the page wording nor the website repository changes.

## What the page should show now

Masterrig joined again at the 12:15Z tick and pushed eacacf6, "Passive history 2026-09-29",
at 12:18Z, on main's code. That record agrees with #101's locally regenerated one on the
tokens of all 261 stretches they share, and adds one after 22 September. So the committed
state now holds the input #101 used, and the gap closes without anything being set by hand.

Rebuilt from eacacf6 at 2026-09-29T12:18:29Z:

| rates | five-hour | weekly | known-date | r | per account, joint fit (1 / 2 / 3) | stretches after (1 / 2 / 3 / 4) |
|---|---|---|---|---|---|---|
| main's 00:01Z fit (what the next publishes use on main's daily.sh) | +26.8% [-16.1, +57.7] | +27.3% [-10.0, +80.1] | +36.1% [24.0, 49.5], provisional | 0.749 | +47.4 / +24.3 / +1.7 | 11 / 15 / 7 / 13 |
| refitted on eacacf6 (what this PR's `rates_due` does at the next publish) | +33.7% [-6.1, +61.4] | +34.3% [-1.2, +82.4] | +41.4% [29.3, 54.7], provisional | 0.816 | +53.7 / +32.6 / +7.2 | 11 / 15 / 7 / 13 |

The stretches before are unchanged: 45, 41 and 52, and none for Max account 4. The refit
puts Opus 5.5's model-rates row at 0.552x Opus (joint fit r 0.816 replaces it), Fable at
2.134, and Sonnet just over the 1.5x publication rule, as above.

The page should show the refitted row: a five-hour rise of about +34% [-6, +61], state
`measuring`, because the interval still includes no change. The known-date test, which is
priced at that r, reads +41% [29, 55], provisional. Main's daily.sh will not refit until the
first publish after midnight, so until this PR merges the page will read about +27%. That
figure is on correctly counted stretches but on rates fitted before #101 and before
masterrig's record caught up. Both point estimates sit above the announced +20%, and each
lies inside the other's interval. Neither interval excludes no change.
