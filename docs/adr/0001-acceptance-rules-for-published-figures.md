# 0001. Acceptance rules for published figures

Status: accepted, 2026-09-23; rules 8 and 9 added 2026-10-04; rules 10 to 13 added 2026-10-05;
rule 15 added 2026-10-06; rule 17 added 2026-10-06

## Context

The tracker's public figures depend on a handful of rules that decide which evidence counts:
when a change is published, which stretches are read, and when a per-model rate is withheld.
Each rule is in the code with its reasoning in a comment, but they are spread over six
files, and a change to any of them moves published numbers without the change being obvious
from the diff alone. This record fixes them as they stand in the code on 2026-09-23 (main at
b0a8874), so a later change to one is visible as a change to a decision, not only to a
constant.

## Decision

These are the acceptance rules. Each names where it lives.

1. **Two accounts must agree on a published change in the credit series, within 3 days.**
   A step in credits per 1% is published only when at least two accounts each detect a step
   in the same direction with onsets no more than `DOLLAR_AGREEMENT_DAYS = timedelta(days=3)`
   apart. An account needs 2 x `MIN_STRETCH_PCT` (100) points of five-hour meter movement
   before it is testable at all; with fewer than two testable accounts nothing is published.
   Source: `tracker/publish.py:83` (`DOLLAR_AGREEMENT_DAYS`), `tracker/publish.py:316`
   (`_agreeing_credit_events`), `tracker/detect.py:287` (`MIN_STRETCH_PCT`).

2. **Provisional events stay out of the headline.** `last_change`, the figure the page
   states as its headline, is chosen only from events that are not provisional. Events on
   the credit (window) series are provisional because their readings carry no rounding
   bounds; they remain in `events` with `evidence_quality: "provisional"`. Weekly events are
   certified against both levels' rounding intervals and can be the headline.
   Source: `tracker/publish.py:1716` (`_latest_change_with_scope`, the filter at line 1734),
   `tracker/publish.py:1743` (`_provisional`). bin/daily.sh also refuses to announce a
   provisional or legacy-uncertain change.

3. **The capture check on gs stretches.** The check itself (`tracker/capture.py`) runs on
   every gs stretch and its verdict is recorded as `capture_status`. It gates in two
   different places, differently:
   - For the published credit series it is advisory: `CAPTURE_GATE = False`. Every priced
     stretch is published except one whose capture is under `COLLECTION_GAP = 0.10` (the
     meter moved with almost nothing on gs to explain it) and one with any unpriced token.
     Source: `tracker/gs_passive.py:108` (`CAPTURE_GATE`), `tracker/gs_passive.py:336`
     (`publishable`), `tracker/capture.py:87` (`COLLECTION_GAP`).
   - For the per-model rate fits it is a gate: a gs stretch is fitted only if its
     `capture_status` reads `accepted`. masterrig is exempt from this test, and its fit is
     not adopted anyway (rule 7).
     Source: `tracker/credits.py:448` (`clean_stretches`, `require="capture_status"`),
     `tools/model_rates.py:357` (`clean`, `exempt=("masterrig",)`).

4. **`min_n`.** A ratio with fewer than `MIN_N = 3` stretches on either side is reported as
   not measurable rather than as a number, and a family's pooled rate needs at least
   `MIN_N - 1 = 2` fits carrying it. Alongside it, a fit needs `MIN_FIT_N = 8` clean
   stretches and a family enters a fit only with tokens in `MIN_NONZERO = 3` of them.
   Source: `tools/model_rates.py:64` (`MIN_N`, with `MIN_FIT_N` and `MIN_NONZERO` on the
   next two lines), used at `tools/model_rates.py:609` (`adopt`).

5. **A family whose pooled interval reaches zero is withheld (`NOT_IDENTIFIED`).** When the
   union of the fits' bootstrap intervals for a family has a lower end of zero, neither the
   value nor the interval is published; the row carries the `NOT_IDENTIFIED` sentence
   instead. The interval is withheld too because tracker/gs_passive.py prices at an
   interval's midpoint where there is no value.
   Source: `tools/model_rates.py:653` (`NOT_IDENTIFIED`), applied at
   `tools/model_rates.py:685` (`identified = ...`).

6. **Inferred resets are used for no figure (`INFERRED_RESET_VERIFIED = False`).** A
   five-hour reset inferred from a reset-less sample is recorded as `reset_source:
   "inferred"` but does not make a stretch `reset_verified`, and the join, the weekly points
   and every rollup see the samples without it. The bar for changing this is zero wrong and
   zero spurious resets when recorded resets are stripped and re-inferred; jwork and
   masterrig missed it on 2026-09-23.
   Source: `tracker/gs_passive.py:400` (`INFERRED_RESET_VERIFIED`), applied at
   `tracker/gs_passive.py:424` and `tracker/gs_passive.py:536`.

7. **Only jwork and dave are fitted (`FIT_ACCOUNTS`).** The per-model rates are pooled from
   `FIT_ACCOUNTS = ("jwork", "dave")`. masterrig's fit is reported for completeness and
   never adopted: its median absolute relative residual is 0.455 against 0.055 to 0.065 on
   the two gs accounts.
   Source: `tools/model_rates.py:555` (`FIT_ACCOUNTS`).

8. **A joint fit separates only at a rate a list price can explain (rate check), added
   2026-10-04.** On top of the span test (`JOINT_SEPARABLE_SPAN = 1.5`) and the mixing test
   (`JOINT_MIN_MIXED` after-side stretches at a share in `JOINT_MIXED_SHARE`), a new
   family's fitted `rate_relative_to_base` counts only if its 95% interval overlaps
   `JOINT_RATE_PLAUSIBLE = (0.5, 2.0)` times its input list-price ratio to its base family
   in data/prices.json. Otherwise the fit is not separable: its g is not published, its rate
   is not absorbed, and `reason` names the rate check. A family with no list price skips the
   test, and its `rate_check.reason` says so.
   Source: `tracker/credits.py` (`joint_rate_check`, `JOINT_RATE_PLAUSIBLE`,
   `JOINT_RATE_PRICE_CLASS`, applied in `joint_rate_fit`).
   Findings: `docs/findings-2026-10-02-one-account-change.md`.

9. **A five-hour change must hold on every account (plan-wide), added 2026-10-04.** A plan
   limit change reaches every account at the same instant. A change candidate whose
   combined figure draws on two or more accounts is refitted with each of them left out in
   turn, by the estimator behind the figure (since rule 11, each of the two direct
   measurements: the window change and the weekly limit change, each an inverse-variance
   combination of the accounts' own changes; the windows-per-week ratio only where neither
   is measured). It is plan-wide only if
   every refit keeps the direction with a 95% interval that still excludes no change; with
   two accounts that is each one alone, and a change resting on one account is not
   plan-wide. Only a plan-wide candidate whose own interval excludes no change `applies`:
   enters `events`, can become `last_change`, reaches the subscriber email, and steps the
   measurement it was certified on (rule 11). The rest stay in
   `credits.five_hour_on_meters.candidates` with a
   `withheld_reason` that names each test it failed. A withheld candidate that can be
   measured still opens a regime boundary, which carries the previous window and does not
   split the window cluster.
   Source: `tracker/credits.py` (`plan_wide_verdict`, `_candidate_plan_wide`,
   `_withheld_reason`, `five_hour_meter_events`, `five_hour_meter_boundaries`, the
   leave-one-out block in `joint_rate_fit`).
   Findings: `docs/findings-2026-10-02-one-account-change.md`.

   Both rules are also checked on every publish by `tracker/invariants.py`
   (`no_unproven_step`), which alerts without blocking.

10. **A new model is valued at its list price until its rate separates from a limit
    change, added 2026-10-05.** A family first used inside the measured record whose joint
    fit there is not separable (rules 8 and 9's fit) is valued at its input list-price ratio
    to Opus 5 from data/prices.json, with the fitted output and cache-read handling, in
    every figure that values a stretch; its fitted rate is published beside it with a
    sentence saying it is not used. The fitted rate takes over by itself once that fit
    separates, and a family with no list price, or first used before any account had a
    before side, keeps its fitted rate.
    Source: `tracker/credits.py` (`list_until_separable`, `LIST_UNTIL_SEPARABLE`, applied in
    `absorb_new_family_rates`), `tracker/publish.py` (`_rate_in_use`).

11. **The weekly limit is measured on the seven-day meter, the window on the five-hour
    meter, and windows per week is their quotient, added 2026-10-05.** The weekly limit is
    credits per 1% of the seven-day meter, read between exact one-point seven-day crossings
    and pooled over every account; the window is read on the five-hour meter as before; and
    windows per week is the weekly limit divided by the window, never a meter ratio
    multiplied back in (the ratio is published beside it for reference). A change is
    certified on those two direct measurements: each account compared with itself either
    side, the accounts combined by inverse variance, and rule 9 applied to each. A certified
    change steps the measurement it was certified on and nothing else, and only a certified
    change opens a regime.
    Source: `tracker/weekly_meter.py` (`seven_day_steps`, `level`, `weekly_change`),
    `tracker/credits.py` (`window_change`, `direct_change`, `five_hour_on_meters`,
    `known_date_changes`, `regime_figures`, `cut_direct_tests`), `tracker/gs_passive.py`
    (`weekly_steps` on each account's record).
    Findings: `docs/findings/2026-10-04-account-agreement.md` (sections 2c and 3, branch
    wf/139-account-agreement).
    Amended 2026-10-05 (wf-143): the step at a change certified on the weekly limit is the
    certified change, every earlier week scaled by one factor (`week_bridge`) as the window's
    history is bridged, the newest week its own direct level; and where a measured window
    change is withheld, windows per week is carried across the boundary rather than the week
    divided by a carried window. `tracker/invariants.py` checks 7-9 fail the publish when the
    page states two figures for one change.
    Source: `tracker/credits.py` (`chain_certified_weeks`, `carry_across_withheld_windows`).
    Findings: `docs/findings/2026-10-05-headless-five-hour.md`.

12. **The five-hour meter weights headless work at a measured factor, added 2026-10-05.**
    The five-hour meter moves about 1.5 times as far per credit for a headless `claude -p`
    run's own turns (entrypoint `sdk-cli`, sub-agents not counted) as for interactive work.
    The seven-day meter treats the two alike. No counting error explains it, and concurrency,
    burstiness, model mix, effort and time of day do not absorb it. So every five-hour figure
    (the window, the five-hour change tests, the account lines) counts headless tokens at that
    factor and is quoted in interactive-equivalent units. The seven-day meter is not weighted.
    The factor is refitted from the seven-day steps at every publish: a quasi-Poisson fit of
    five-hour points on regime, account and headless share. It is published with its interval
    and per account (`five_hour_meter`), and it is never a hand-set constant. While fewer than
    50 steps carry a headless split, nothing is weighted, and `five_hour_meter` says so.
    The per-family credit rates are fitted in the same unit: `tools/model_rates.py` fits the
    factor from the same histories and fits the rates on the weighted stretches, recording
    the factor as `five_hour_unit`; the publish shows it as `model_rates_fitted_at`. Rates
    fitted on metered tokens priced a headless-heavy family with the factor inside its rate.
    Source: `tracker/five_hour_weight.py`, `tools/model_rates.py` (`five_hour_unit`), `tracker/turns.py` (`Turn.headless`),
    `tracker/join.py` (`headless_tokens`), `tracker/publish.py` (`_five_hour_meter`).
    Findings: `docs/findings/2026-10-05-headless-five-hour.md`,
    `docs/findings/2026-10-05-headless-weight.md`.

13. **The 14 September weekly before side reaches back to the account's last certified
    boundary, added 2026-10-05.** On the seven-day meter, each account's before side for
    the 14 September change starts at its last certified boundary before the change. That
    boundary is the start of a regime that a certified step opened in the account's own
    windows-per-week series, before its own weekly step. Without one, it is `PLAN_CHANGE_AT`,
    where every account's Max 20x series starts. The start of an account's first regime is
    only where its points begin, and it bounds nothing. From that boundary the before side
    reads every clean step: an account with a longer clean history uses all of it. Max
    account 1's history before `MASTERRIG_FROM` therefore counts, except the 2-5 September
    takeoff phantom (`MASTERRIG_PHANTOM`). Its stretches from before its transcripts survive
    already fail the `status` gate. The window change across 14 September keeps the
    known-date selection (`MASTERRIG_FROM`): on Max account 1's five-hour meter, late August
    does not read the level of 6-13 September, while on its seven-day meter it does.
    `MASTERRIG_FROM` keeps its place in the rate fits and in every later candidate.
    Source: `tracker/credits.py` (`cut_before_start`, `masterrig_excluded`,
    `MASTERRIG_PHANTOM`, `cut_direct_tests`), `tracker/weekly_meter.py` (`clean_steps`,
    `whole_history`), `tracker/publish.py` (the `cut_direct_tests` call).
    Findings: `docs/findings/2026-10-05-a1-long-before.md`.

15. **Windows per week times the window is read on one set of readings, in one unit,
    added 2026-10-06.** Weekly credits per 1% of the seven-day meter equal credits per 1% of
    the five-hour meter times five-hour points per seven-day point, on the same readings.
    So a weekly change stated as windows per week times the window (the 14 September
    `tokens_per_week_change`, the ratio route) reads both factors on the direct weekly
    test's own sides (`cut_weekly_sides`: the rule 13 start, each account's own step, the
    first change candidate after the cut), with its selection (a five-hour meter window that
    meets a seven-day step the weekly selection leaves out is left out, and the stretches
    pass the same harness, cloud, status and takeoff-phantom gates), with its valuation, and
    by pooled sums. Both factors are in one unit. The meter ratio counts raw five-hour points,
    so the window factor values metered tokens. The headless weight of rule 12 belongs to
    five-hour figures published on their own, and is never multiplied into a raw meter
    ratio. (Amended by rule 16: the ratio is now itself interactive-equivalent, so the
    window factor values headless-weighted tokens; still one unit.) On identical readings the two routes are then one number. What remains between
    them is the two kinds of reading: five-hour windows against one-point seven-day steps.
    `tracker/invariants.py` check 10 (`weekly_routes_agree`, advisory) fails when, on a
    certified weekly event, the two differ by more than their combined interval.
    Before this rule the route compounded the detector's raw meter ratio, over the account's
    own regimes (Max account 1 from 15 August, both accounts to 5 October, across the
    certified 22 September change), with the headless-weighted median window on the
    known-date selection. That put 14 September at -28.0% against a direct -9.9%. The
    largest single cause was Max account 2's headless share, which fell from 46% to 3% of
    its credits across the cut.
    Source: `tracker/credits.py` (`cut_weekly_sides`, `cut_ratio_route`),
    `tracker/publish.py` (`_tokens_per_week_change`), `tracker/invariants.py`
    (`weekly_routes_agree`).
    Findings: `docs/findings-2026-10-06-14-sep-routes.md`.

16. **Windows per week is read in interactive-equivalent units, added 2026-10-06.** Rule 12
    puts the window in interactive-equivalent units and rule 11 makes windows per week the
    week over the window, but the windows per week the meters themselves show (the change
    detector's input, its regimes and the account regimes, `max20.by_window`, the 14
    September event's `percent` and `windows_per_week_ratio`, the windows-per-week candidates
    and the chart's meter levels) was the raw five-hour over seven-day meter ratio. Headless
    work moves the five-hour meter by the fitted factor per credit and the seven-day meter not
    at all, so a raw ratio moves when an account's headless share moves, with no limit
    change: Max account 2 went from 46% to 3% headless across 14 September. So each five-hour
    meter window's movement is divided by its headless inflation, 1 + (factor - 1) x the
    window's headless share of credits, before anything reads it. The factor is the one
    fitted at the same publish (`five_hour_meter.headless_factor`), never a constant. The
    share is read from the account's seven-day steps that overlap the window's five hours,
    each counted by the part of its span inside them and valued as the factor's own fit
    values them. A window no step overlaps reads the stretches the same way, and a window
    neither overlaps is counted as recorded, unweighted, as rule 12 counts an account with
    no split. A seven-day step's own five-hour points (`windows_per_week_meters`) are divided
    by the step's own inflation the same way. The rounding error stays the meter's, scaled
    into the new unit. The meter's
    own figures stay published beside them as `raw_*`, a diagnostic that no figure reads.
    The ratio route of rule 15 is then in this unit on both factors: windows per week over
    each window's inflation, the window on headless-weighted tokens. The inflation cancels
    in the product, so the identity with the direct weekly change still holds. The seven-day
    meter is not weighted. `tracker/invariants.py` check 11
    (`detected_windows_per_week_agree`, advisory) fails when a detected windows-per-week
    level and the week over the window it overlaps longest differ by more than their
    combined interval.
    Source: `tracker/five_hour_weight.py` (`interactive_windows`, `WINDOWS_METHOD`),
    `tracker/publish.py` (`_interactive_windows`, `_weekly_block`, `_point_tuples`),
    `tracker/detect.py` (`ratio_interval` `raw_d5`, `pooled_interval`),
    `tracker/weekly_meter.py` (`valued`, `level`), `tracker/credits.py`
    (`_side`, `paired_levels`, `windows_per_week_ratio_note`, `cut_ratio_route`),
    `tracker/invariants.py` (`detected_windows_per_week_agree`).

17. **A published change states its chart's pooled step, dated by one rule, added
    2026-10-06.** A change event's figure is the pooled figure of the metric the chart
    beside it draws, at the marker that chart draws for it: the windows-per-week event
    states the `per_week_regimes` windows-per-week step at its boundary (`change_pct`, the
    whole `percent`, `metric` `windows_per_week`), with that step's interval
    (`interval_pct`: the after row's interval ends over the before row's opposite ends).
    One account's detector step is never the figure; each certifying account's own step,
    the paired accounts' combined figure and the pooled detector's boundaries are published
    beside it under `evidence`. Without two `per_week_regimes` rows, or without windows per
    week on both, the event keeps the detector's figure and `metric`
    `weekly_to_five_hour_ratio`.
    Every event is dated by one rule, written here before any figure was recomputed with it.
    A certifying account is one with readings on both sides of its own certified detector
    step (`windows_per_week_ratio_note.per_account`). Its step interval is
    [e, l]: e is the end of its last window at the old level, l the end of its first window
    at the new level. A pooled detector boundary (between adjacent
    `regimes_pooled_all_accounts` rows) is the end of the last pooled window at the old
    level.
    - (a) With one or more certifying accounts, the event instant is the earliest pooled
      detector boundary lying inside every certifying account's interval, [max e, min l],
      if one does. A pooled boundary outside an account's interval contradicts that
      account's own step: one account's earlier step can be lost in the pool, and an account
      that joins the pool late moves its split.
    - (b) Otherwise it is the earliest instant inside every certifying account's interval:
      max e, when max e <= min l.
    - (c) When no instant is inside every interval (the accounts stepped at seven-day
      resets too far apart), it is min e: the change had reached the first account by then.
    - With exactly one certifying account, (a) to (c) reduce to that account's interval, and
      `evidence.single_account` says so.
    - With no certifying account, the instant is the pooled detector's own boundary for the
      step.
    - A change certified on the meters directly (rule 9, `five_hour_on_meters`) is dated at
      its candidate instant, a boundary found in the data (a family's first turn).
    - An announcement date never dates an event or a marker.
    The event's `at` is that instant and `date` its UTC day. The boundary the chart draws for
    the change is moved to that same instant in `window_tokens.regimes`,
    `per_week_regimes` and every `account_regimes` row, and in `window_tokens.cut_at`. So the
    event, the per-week and account boundaries and every chart marker carry one value. The
    readings either side are still split at each account's own step, or at `split_at`
    (`credits.CUT_AT`) for an account without one, and `split_at` is published.
    `tracker/invariants.py` check 9 (`one_figure_per_change`, blocking) reads the meter ratio
    on the windows-per-week chart. It fails when an event's whole percent is not its chart's
    step label at its marker, when that chart draws no step there, or when the event's
    instant is not the marker's.
    Before this rule the 14 September event said windows per week "fell about 25% around
    2026-09-13", Max account 1's own detector step alone (6.32 to 4.75). The chart beside it
    stepped -10% (5.11 to 4.60) at the announced 14 September 12:00Z.
    Source: `tracker/publish.py` (`event_instant`, `_rule_dated`, `_chart_figure`,
    `_align_markers`, `_event_record`), `tracker/invariants.py` (`one_figure_per_change`,
    `_event_off_its_marker`).

### Pending

14. **60% dominance for a family's fit (pending, not yet in the code).** A fit counts toward a
   family's rate only if 3 or more of its stretches are at least 60% that family. Another
   PR is adding this; when it merges, this entry takes its source location and moves up
   into the list above. Until then the code has no such rule (the only dominance constant
   today is `DOMINANCE = 0.95` at `tools/model_rates.py:63`, which selects single-model
   stretches for section 1 and does not gate the fits).

## The rule for changing these

Any change to one of these rules is made in its own PR, which changes nothing else and
records the published figures before and after the change: at least `last_change`, the
per-model rates and their status, and the tokens per window, taken from a rebuild of the
same committed inputs with the old and the new code (`python3 tools/verify_publish.py`
checks the "before" against the live file). The same PR updates this record.

## Consequences

A reviewer can check a PR that touches `tracker/publish.py`, `tracker/gs_passive.py`,
`tracker/credits.py`, `tracker/capture.py` or `tools/model_rates.py` against this list, and
a changed rule without its before-and-after figures is visible as a missing section of the
PR rather than as a quiet shift on the page. The line numbers above drift as the files
change; the names do not, and are the reference.
