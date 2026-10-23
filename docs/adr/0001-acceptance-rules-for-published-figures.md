# 0001. Acceptance rules for published figures

Status: accepted, 2026-09-23; rules 8 and 9 added 2026-10-04; rules 10 to 13 added 2026-10-05;
rule 15 added 2026-10-06; rule 17 added 2026-10-06; rule 22 added 2026-10-23, superseding
rules 1, 2, 13, 15, 17, 20 and 21, rule 9 as a rule for changes, rule 11's certification and
rule 16's check 11

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
   Superseded 2026-10-23 by rule 22: every change the page states is a step all the voting
   Max accounts take at one instant. `DOLLAR_AGREEMENT_DAYS`, `_agreeing_credit_events`,
   `detect_credit_changes` and `MIN_STRETCH_PCT` are removed.

2. **Provisional events stay out of the headline.** `last_change`, the figure the page
   states as its headline, is chosen only from events that are not provisional. Events on
   the credit (window) series are provisional because their readings carry no rounding
   bounds; they remain in `events` with `evidence_quality: "provisional"`. Weekly events are
   certified against both levels' rounding intervals and can be the headline.
   Source: `tracker/publish.py:1716` (`_latest_change_with_scope`, the filter at line 1734),
   `tracker/publish.py:1743` (`_provisional`). bin/daily.sh also refuses to announce a
   provisional or legacy-uncertain change.
   Superseded 2026-10-23 by rule 22: `last_change` and `events` hold only the four-account
   detector's measured changes, and its candidates and provisional changes are published in
   `four_accounts` alone. `_latest_change_with_scope` and `_provisional` are removed.

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

   Rule 9 is superseded as a rule for changes on 2026-10-23 by rule 22, whose leave-one-out
   test (`four_accounts.plan_wide`) is this one, applied on every line the detector reads.
   `five_hour_on_meters`, its candidates, `five_hour_meter_events`,
   `five_hour_meter_boundaries`, `_candidate_plan_wide` and the check `no_unproven_step` are
   removed. `plan_wide_verdict` stays in `joint_rate_fit`'s leave-one-out, which rule 8's
   rate fits still read.

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
    Amended 2026-10-23 by rule 22: the three measurements stay as defined here, but no change
    is certified on them any more. The four-account detector finds, dates and sizes every
    change, and a regime opens only at its measured changes. `weekly_change`,
    `window_change`, `direct_change`, `five_hour_on_meters`, `known_date_changes`,
    `cut_direct_tests` and `carry_across_withheld_windows` are removed.
    `chain_certified_weeks` now steps each week at a measured change by the detector's
    seven-day size where the charts draw that line, and carries it elsewhere.

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
    Superseded 2026-10-23 by rule 22: no figure reads a before side of the 14 September
    change any more. The detector's sides are 14 days, cut at the neighbouring changes, for
    every account and every change. `cut_before_start` and `cut_direct_tests` are removed.
    `masterrig_excluded` and `MASTERRIG_PHANTOM` stay in the rate fits and the window
    cluster (rule 18).

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
    (Amended by rule 20: both factors are now read on the direct test's own seven-day steps,
    so nothing remains between the routes.)
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
    Superseded 2026-10-23 by rule 22: the detector measures the seven-day line, the ratio
    and the five-hour line each on its own and states each with its own interval, so no
    published figure is the product of the others. `cut_weekly_sides`, `cut_ratio_route`,
    `_tokens_per_week_change` and check 10 (`weekly_routes_agree`) are removed.

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
    Amended 2026-10-23 by rule 22: the unit stays, and the detector's ratio line is in it
    (each seven-day step's five-hour points over its own headless inflation, at the factor
    the detector fits at each run). Check 11 (`detected_windows_per_week_agree`) is removed
    with the windows-per-week detection it checked, and so are `paired_levels`,
    `windows_per_week_ratio_note` and `cut_ratio_route`. The windows-per-week regimes now
    open at the detector's measured changes and step by its ratio size.

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
    Superseded 2026-10-23 by rule 22. A change's instant is now the detector's own: the hour
    of the largest |z| near the instant that flagged, on the line that found it, published
    with the range of hours around it. Every event, marker and regime boundary carries that
    instant, and its figure is the detector's size on the line that found it. `event_instant`,
    `_rule_dated`, `_RuleDatedEvent`, `_paired_step`, `_chart_figure`, `_align_markers`,
    `_event_record` and `_build_events` are removed. `one_figure_per_change` now reads the
    detector's records (rule 22). On the repaired records of 2026-10-23 this rule's pooled
    split put an event at -17% on 5 September on `weekly_to_five_hour_ratio` that no chart
    drew, and the page was withheld. The split is gone with the rule.

18. **A per-week row's window and week read one span of each account's history, added
    2026-10-06.** A `per_week_regimes` row's windows per week is its week over its window,
    with its interval the week's ends over the window's opposite ends. Both are therefore read
    over the same span of each account's history: a stretch enters the window cluster
    (`window_credits`, `window_tokens`, every window regime) only where the account's
    seven-day steps are read for the row's week (`weekly_meter.clean_steps`, the
    `masterrig_excluded` gate without `whole_history`), on top of the capture test it already
    passes. So Max account 1 enters the window from `MASTERRIG_FROM`, as it enters the rows'
    weeks and the rate fits. The rule names no account and no date of its own: it reuses the
    gate the weeks already apply, and moves with it.
    Before this rule the window cluster read Max account 1's whole capture-accepted history.
    One pure-Opus stretch from 2 September, inside the takeoff-pipeline days
    (`MASTERRIG_PHANTOM`), read 86,003 credits per 1% against 175,934 to 208,197 for the ten
    Max account 2 stretches beside it. As the before window's lowest reading (173.5M tokens
    against a median of 673.6M) it set the first row's windows-per-week interval at
    [3.99, 20.75] around 5.11, and the 14 September event's interval at [-82.8, +388.0]%.
    The row's week (interval within 6% of its value) and its readings were not the cause:
    none of them starts before `PLAN_CHANGE_AT`, and the per-week figure never reads the
    five-hour windows of 15-18 August.
    Source: `tracker/credits.py` (`window_stretches`, `selection_sentence`),
    `tracker/publish.py` (the `window_stretches` call).

19. **A window regime set by a measured change keeps that change through rounding, added
    2026-10-06.** A window regime whose value is the previous regime's times a measured
    change (the bridge rate of a run or a new family, `bridge_rate`; the before cluster
    scaled by the five-hour change, `before_cluster_scaled_by_five_hour_change`) states that
    change as its step by construction. The change is the meters' published one-decimal
    percent, so the step can sit exactly on a whole-percent half (+19.5%). Each regime value
    was rounded to a whole token on its own, so the step the chart draws from two rounded
    values fell either side of the half by chance (+19.50000002% or +19.49999999%), and the
    chart's label (+20% or +19%) with it, while the event states the change's own rounding.
    So such a regime is published at the whole value nearest the previous regime's published
    value times the change, moved one unit toward the change while the drawn step rounds
    (halves up, as the page rounds) to another whole percent than the change does. A regime
    carrying the previous value unchanged is published at the same value. The drawn step's
    label is then the change's, never across its rounding. Every other regime is rounded as
    before. Found while recomputing rule 18: the
    previous regime moved 2.6% and the 22 September step crossed the half.
    Source: `tracker/credits.py` (`window_regimes` `step`, `_rounded_regimes`).

20. **The 14 September weekly step compares each account with itself, on one set of
    readings, added 2026-10-08.** The weekly change is measured directly, credits per 1% of
    the seven-day meter, on the direct weekly test's clean seven-day steps and sides (rules
    11 and 13). Its two factors are read on those same steps and days: the window, as credits
    per five-hour point, with each step's five-hour points over its headless inflation (rule
    16), and windows per week, as five-hour over seven-day points, the derived quotient. So
    weekly = window x windows per week holds exactly for each account. The accounts are
    combined at one set of weights, the weekly figure's inverse variances, so the identity
    also holds for the combined figures (`weekly_meter.reconcile`, `credits.cut_ratio_route`).
    `tokens_per_week_change` states the direct combined weekly change, with its interval, and
    carries the other two as its factors. A weekly figure is never built from them. Each
    combined change is tested under rule 9, and its verdict is published even when it fails.
    The tokens-per-week chart's 14 September step (`per_week_regimes`) is bridged to that
    same combined change (`chain_certified_weeks`, `week_bridge`), whether or not rule 9
    certifies it. The boundary is the detector's certified step, and the chart draws a step
    there either way. An account with readings on one side only moves no step.
    `tracker/invariants.py` check 10 (`weekly_routes_agree`, advisory) also fails when that
    chart step is not the direct combined change to its one decimal.
    Written before any figure was recomputed with it: the readings, valuation, sides,
    quantities and bootstrap above. Five alternatives were declared in advance: Max account
    1's before side from 6 September only, raw five-hour points, the after side to the newest
    reading, meter windows for windows per week, and the before side from the detector's
    regime start.
    Before this rule, three figures for Max account 1 did not reconcile. Windows per week
    read -24.9% on meter windows from 15 August to 7 October. The weekly change read -9.1% on
    steps from 20 August to 22 September. The window read +3.4% on stretches from 6
    September. The chart stepped -10.75%: a level pooled over Max accounts 1 and 2 against
    one over Max accounts 1, 2 and 3, whose log starts after the change. The route stated
    -14.6%. On the common basis the figures are: Max account 1 weekly -9.1%, window +9.0%,
    windows per week -16.6%; Max account 2 -10.4%, +3.2%, -13.1%; combined -9.9% [-22.1,
    +4.1], +5.2%, -14.3% [-19.0, -9.4]. The weekly change fails both rule 9 tests. Windows per
    week on the same steps passes both.
    Source: `tracker/weekly_meter.py` (`QUANTITIES`, `account_change`, `reconcile`),
    `tracker/credits.py` (`cut_weekly_steps`, `cut_ratio_route`, `chain_certified_weeks`,
    `regime_figures`), `tracker/publish.py` (`_tokens_per_week_change`, `_align_markers`),
    `tracker/invariants.py` (`weekly_routes_agree`, `_week_step_at`).
    Findings: `docs/findings-2026-10-08-14sep-reconcile.md`.
    Superseded 2026-10-23 by rule 22: the weekly change at every change, 14 September
    included, is the detector's seven-day size, with each account's own step beside it.
    `weekly_meter.reconcile`, `account_change`, `weekly_change`, `QUANTITIES`,
    `cut_weekly_steps` and `cut_ratio_route` are removed. The tokens-per-week chart steps at
    a measured change by that size where the detector draws the seven-day line there, and
    carries its level where it does not.

21. **The 14 September weekly step is frozen at -9.9%, added 2026-10-09.** The size of the
    14 September weekly change is the rule 20 figure, -9.9% [-22.1, +4.1] as recorded on
    2026-10-08 (the interval read [-22.1, +4.3] on the 2026-10-09 inputs). The size is frozen
    at -9.9%. It is reopened only for a counting error shown with evidence. It is never
    reopened because a different method gives a different number. Reopening it means changing
    this rule, and `FROZEN_14SEP_WEEKLY_PCT` with it, in their own PR.
    `tracker/invariants.py` check 12 (`fourteen_sep_weekly_frozen`, advisory) fails when the
    published step moves from -9.9% at one decimal: the direct weekly change, the
    `tokens_per_week_change` and the tokens-per-week chart's step at the event's marker.
    The live figure is still recomputed each publish, and its credit valuation moves with each
    refit of the rates, so the check is how a move shows. A test reads this rule and fails
    when its figure and the constant differ.
    The alternative tested first was the window headline's own reading moved to the seven-day
    meter: raw tokens per seven-day point on pure-Opus steps (Opus 5 only, Opus 5.5 being its
    own family), each account against itself on rule 20's sides. Its rules, the estimator, the
    interval, a thin-sample floor (3 UTC days and 20 points a side for Max accounts 1 and 2)
    and the choice rule were committed before any figure was computed. The sample was
    thin: Max account 1 had 8 pure-Opus steps before and none after, and Max account 2 had 15
    before (2 days) and 3 after (1 day). No account gave a figure, so the choice rule kept
    rule 20's. No publisher code changed.
    The announced 17% reduction is inside rule 20's interval. It was used as a test of the
    method only, never as an input.
    Source: `tracker/invariants.py` (`FROZEN_14SEP_WEEKLY_PCT`, `fourteen_sep_weekly_frozen`),
    `tests/test_invariants.py` (`FourteenSepFrozenTest`).
    Findings: `docs/findings-2026-10-09-14sep-pure-opus.md`.
    Superseded 2026-10-23 by rule 22. The freeze predates the capture repair of 2026-10-23
    (PR #137). That repair corrected counting the freeze never saw. The gs accounts'
    transcripts that the 15 October disk cleanup had archived are read again, so their
    history before 1 October is back, and every account's work is counted on whichever host
    it ran and against the account that did it. That is the counting-error evidence this rule
    asks for. On the repaired records this rule's figure read -10.30%, and check 12 failed.
    The 14 September weekly figure is now the detector's seven-day size at its 14 September
    change: -4.2% [-15.4, +8.5] on the records of 2026-10-23 (at 12:02Z), with Max account 1
    at +28.0% and Max account 2 at -10.1%. Its interval includes no change, so the
    tokens-per-week chart carries its level across 14 September. The page states that change
    on the line that found it: windows per week -17.4% [-25.7, -8.1]. `FROZEN_14SEP_WEEKLY_PCT`,
    `FROZEN_14SEP_REACH_DAYS`, check 12 and `FourteenSepFrozenTest` are removed with this
    entry.

22. **A limit change is a step every Max account takes at the same instant (the four-account
    detector), added 2026-10-23.** The four-account detector (`tracker/four_accounts.py`) is
    the only source of the limit changes the page states: `last_change`, `events`, every
    regime boundary on every chart, and the subscriber email. Each Max account is its own
    ruler. When every account gets the same percentage more or less work per 1% of a meter at
    the same moment, the limit changed.
    - The lines. Per account, from the passive records (history/gs-passive.json, and
      history/masterrig-passive.json for Max account 1): `seven_day`, credits per 1% of the
      seven-day meter on the one-point seven-day steps; `ratio`, five-hour points per
      seven-day point on the same steps, each step's five-hour points over its own headless
      inflation (rule 16's unit); and `five_hour`, credits per 1% of the five-hour meter on
      the stretches, headless work at the fitted factor. The first two are searched. The
      five-hour line is measured as a size at each change and never searched. Every reading
      counts except cloud-session work, harness runs and unpriced tokens, and, on the two
      credit lines, a collection gap (under `COLLECTION_GAP` of the account's own
      meter-weighted median work per point).
    - The test. At every six-hour instant, each account's 14 days after are compared with its
      14 days before, both sides cut at the neighbouring changes. Each side is a pooled sum, so
      a reading counts by the meter movement it carries, and is read in 24-hour units. The
      accounts' steps are averaged by inverse variance. The instant flags when all three of
      these hold. (a) Shuffling each account's days between the two sides 4,000 times makes a
      pooled step that large less than once in a thousand tries (permutation p < 0.001).
      The shuffles at an instant are seeded by the line, the instant and the account alone,
      never by the day the search runs, so the same readings give the same p on every run.
      Where p lies within three Monte Carlo standard errors of 0.001 the shuffles go on, five
      times as many each round, up to 100,000, and flagged instants are ranked at the first
      round's resolution. Before this, the seed was the run's day: at 9 October 18:00 p is
      0.00079 by 100,000 shuffles, 20 of 30 seeds passed it at 4,000, and on unchanged
      records the 9 October change was withdrawn on 24 October and back on 25 October. (b)
      Every voting account agrees: none is more than 2.5 standard errors from the others, none
      is significant the other way, and at least two vote. (c) The step holds with any one
      account left out, at |z| >= 1.96 for the rest: rule 9's test, on every line. The
      strongest flagged instant becomes a change, the history is split there, and the search
      repeats until nothing flags. A day's find is the hour of the largest |z| within 48
      hours of the flagged instant, with the range of hours within one z of that. A change's
      instant is the median of its hold's finds (the lower middle one), and its published
      range spans every find's range. A step
      one account takes alone is a counting problem on that account (`not_limit_changes`),
      never a limit change.
    - No date is an input. No cut, announcement, family's first use or account list enters
      the detector. Accounts and their labels come from the records. The headless factor is
      fitted at every run from the seven-day steps, with one level per account per ISO week in
      place of dated regimes. The credits are valued once across the whole history: at the
      rates in force in history/model-rates.json, as every change figure prices them
      (`credits.comparison_value`), except Opus 5.5, held at its list ratio (rule 23), the
      same rate per family on both sides of every instant.
      They are never valued at the rates absorbed at a family's first use (rules 8 and 10),
      which are fitted jointly with a limit change at that first-seen instant and would carry
      its date into the seven-day line. The ratio reads no rate except a step's headless share,
      so a refit moves the seven-day and five-hour sizes and never what the ratio finds. A held
      refit changes nothing here until it is accepted.
    - The hold. The detector runs once a day on the data known at midnight UTC (bin/daily.sh).
      A change is a `candidate` the day it is first found. It is `provisional` while it is
      found again on the same line, in the same direction, within one weekly cycle
      (`MATCH_HOURS`, 7 days) of the median of its hold's finds so far, and `measured` once it
      has been found on 7 runs in a row. Matching against the hold's median, not yesterday's
      find, keeps a find from walking a week a day; the median and the range of every find
      keep the date from being whichever instant the seventh run picked. From then on it is
      carried into every later run as a fixed change: never moved, never withdrawn, and
      re-tested inside its published range each day (`found_today`). A flagged instant inside
      that range (widened by 48 hours), on its line and in its direction, is that change found
      again; one further off is a change of its own. The match is one weekly cycle, not the 36
      hours of the design, because a weekly limit change reaches each account at its own
      seven-day reset. Replayed on the records of 2026-10-23 with 36 hours, the strongest
      instant of the 22 September step moved anywhere from 23 to 30 September from one run to
      the next, and it was never held 7 runs. Its seven finds were 22 September 15:00, 28
      September 14:00, 29 September 13:00 and 23 September 13:00 four times: measured at 23
      September 13:00, range 20 September 12:00 to 29 September 16:00.
    - What a measured change says moved is decided the day it is measured (`decided`: which
      lines moved, and the interval factors that said so) and never re-decided. Its sizes and
      intervals are re-measured every run, but what the charts draw, `scope` and the email do
      not flip with them: re-estimated daily, the 23 September five-hour interval read
      [+0.1, +38.6] on 21 October, [-0.4, +39.2] on 22 October and [+1.3, +37.0] on 23
      October, and the scope would have read "weekly" for one day.
    - Capture. A change found on the seven-day line also needs every voting account to pass
      the capture audit on both sides (`capture_audit`, spec 9.7): at least 90% of its
      five-hour meter movement in stretches with captured work (not a collection gap). One
      that fails stays provisional and names the account (`held_by_capture`). The ratio reads
      no transcript work and is not audited. The audit sees a meter that moved with nothing
      recorded. It does not see partial capture: on the records before PR #137, Max account 2
      had no collection-gap stretch on 17 October, while its stretches there read 8-61% of
      its own reference. The protection against a shared partial gap is the capture itself, and
      `CaptureDependenceTest` (R3) shows the detector still sees one: on those records the
      seven-day line steps by -43% on 17 October at p < 0.001, kept out only by the
      leave-one-out test.
    - Replays. A run with no state replays every day from the first possible one and marks
      each run `replayed`, each change it holds `hold_replayed`, and the state
      `replayed_through`: their dates were found after the fact, not seen live. A state file
      that is missing, unreadable, of another schema or without a newest run is no state: the
      daily command rebuilds it this way on its own and logs that it did, never exits with
      nothing written. (The first live run after PR #142, 2026-10-23T13:30Z, wrote nothing
      because bash was still reading the bin/daily.sh from before that run's pull, which had
      no detector step; a pull that changes bin/daily.sh now restarts the run on the new one.) On the records
      of 2026-10-23 a replay takes about two minutes; one day's run about 4 seconds. The state
      is history/four-accounts.json, committed by the daily run: one row per run of what it
      found and held, and the newest run in full.
    - Sizes. At each change the step on all three lines is measured with sides of 14 days, cut
      at the neighbouring changes. Each account's own step, and the pooled step with each
      account left out, are published beside it. Each line's 95% interval is widened by the
      spread of z over the history with the measured changes divided out (`coverage`), and
      never narrowed. The 2026-10-23 re-check found that the nominal intervals of the
      five-hour line and the ratio covered 59-74% of injected steps. On the records of
      2026-10-23 the factors are 1.79 (five-hour), 2.01 (ratio) and 1.0 (seven-day). The
      unmeasured 9 October step sits in that bed as noise; once it is measured and divided
      out the factors fall (1.16 and 1.10 on a run of 26 October on the same records).
    - Rates. At every change each family with a rate interval in history/model-rates.json
      (10th to 90th percentile), other than a family held at its list price (rule 23), has
      its rate moved to either end of it, the others as in force, and every size re-read
      (`rate_dependence`): the range the rates move the figure
      over, each family taken at its worse end and added, and the interval that would then be
      published. The figure, its interval and what the charts draw stay at the rates in
      force, the valuation every chart, window and week figure is in. The change record says
      `rate_limited` where the rates move the figure more than its own interval does, with
      `interval_with_rates_pct`. On the records of 2026-10-23 the 23 September weekly limit
      is +18.3% [+11.5, +25.6] at the rates in force and rate-limited: across Opus 5.5's
      interval [0.652, 0.938] x Opus it reads +5.0% to +34.6%, and with every family +0.6% to
      +39.3%, interval [-5.2, +47.8]. The five-hour line there reads +0.7% to +38.8%. The
      after side runs on a model the before side never used, so a credit line cannot tell
      that model's rate from a limit change. The ratio moves by under half a point across
      every family's interval. (Those were the figures before rule 23 held Opus 5.5 at its
      list ratio; its figures are under that rule.)
    - What the page states. Only measured changes reach `last_change` (the newest), `events`,
      the window and week regimes, and the email (bin/daily.sh mails a change whose state is
      `measured`; the site takes scope "window" or "weekly", so a five-hour or both-limits
      change is sent as "window", a weekly one as "weekly", and a windows-per-week or
      undetermined one with no scope). A change's figure is its size on the line that found
      it: the seven-day line states the weekly limit (`metric` `weekly_limit`), the ratio states windows per week
      (`windows_per_week`). Each direct limit's own size and interval ride beside it, and
      `scope` names the direct limits that moved: their interval excludes no change and their
      accounts agree. A chart steps at a change on the line that found it, and on another line
      only where that line moved (`four_accounts.drawn`). Elsewhere its level is carried, and
      the size stays a published figure with its interval. Every regime boundary is a measured
      change, and every measured change opens one (`four_accounts.regime_changes`,
      `credits.chain_regimes`, `credits.chain_certified_weeks`, `publish._segment_regimes`).
      Candidates and provisional changes are published in `four_accounts` alone, with what the
      chart needs: each account's daily level on every line, indexed to its own current level;
      the shared step levels; finds withdrawn before they were measured; and the rule, the
      valuation, the headless factor, the interval factors and the measured sensitivity.
      Accounts are named only "Max account N".
    - Invariants. `tracker/invariants.py` checks 4-8 block the publish (exit 2): the headline
      is the newest measured change (`headline_is_last_measured_change`); every published
      change is drawn on its line and every event is a measured change (`every_change_drawn`);
      one figure per change on every chart that draws its line (`one_figure_per_change`); no
      chart steps where no change was measured (`regimes_open_at_measured_changes`); accounts
      are named only by label (`accounts_named_by_label`). Checks 9
      (`measured_changes_still_found`) and 10 (`detector_ran`) alert without blocking. The
      checks on the old candidates and routes are removed: `no_unproven_step`,
      `steps_agree_with_meters`, `no_withheld_boundary`, `headline_matches_chart`,
      `windows_per_week_implied`, `weekly_routes_agree`, `detected_windows_per_week_agree` and
      `fourteen_sep_weekly_frozen`.
    - Sensitivity, measured on 2026-10-23 by injecting a step into every account's real
      readings (`four_accounts.SENSITIVITY`). With all four accounts voting, the smallest step
      caught 95% of the time is 30% on the seven-day line 14 days after it, and a 15% fall or a
      20% rise on the ratio. Seven days after it, the smallest is 30% on the ratio and a 40%
      fall on the seven-day line. Over every start from 12 September to 15 October (in
      September at most two accounts voted), only a 40% fall on the ratio at 14 days was caught
      that reliably. The five-hour line caught no step of up to 50% that reliably, which is
      why it is measured and not searched. Of 20 histories with every account's readings
      shifted in time by its own random offset, 2 flagged with the 9 October ratio step left
      in as noise and none with it divided out. The 7-day hold is what keeps such a flag from
      becoming measured.
    - On the records of 2026-10-23 (main at 689db7d), with no date given (a replay, so every
      hold below was found after the fact): 14 September 09:00Z on the ratio (range 14
      September 00:00 to 17 September 00:00), windows per week -17.4% [-25.7, -8.1], Max
      accounts 1 and 2 voting, measured since 25 September; 23 September 13:00Z on the
      seven-day line (range 20 September 12:00 to 29 September 16:00), weekly limit +18.3%
      [+11.5, +25.6], rate-limited, Max accounts 1 to 3 voting (Max account 4's readings
      start a few hours before it), with the five-hour limit +17.8% [+1.3, +37.0] beside it,
      measured since 11 October; and 9 October 16:00Z on the ratio, -9.2% [-16.2, -1.6]
      (p = 0.00046) with all four accounts agreeing, provisional since 20 October. Nothing
      at 17 October. Run forward on the same records, 9 October is held 5 and 6 days on 24
      and 25 October and measured on 26 October, at -9.2% [-13.1, -5.1].
    - Before and after, rebuilt from those same records with main's code and with this rule's
      code. Before: `last_change` 22 September 19:41Z, five-hour limit +20.1% [+11.3, +29.6];
      `events` 5 September `weekly_to_five_hour_ratio` -17%, 13 September windows per week -9%,
      22 September five-hour limit +20%; tokens per window 1,137,165,850 Opus 5.5; invariants
      exit 2 on `account_units`, `one_figure_per_change` and `fourteen_sep_weekly_frozen`, so
      the page stayed withheld. After: `last_change` 23 September 13:00Z, weekly limit +18.3%
      [+11.5, +25.6]; `events` 14 September windows per week -17% and 23 September weekly
      limit +18%; tokens per window 1,125,987,941 Opus 5.5; all ten invariants pass. The
      per-model rates are unchanged: history/model-rates.json is the input to both.
    - Also on 2026-10-23, for check 3 (`account_units`): a regime's direct `measured_window`
      needs `MIN_AFTER_CLUSTER` pure stretches of its family, as a regime's own direct window
      does, and is null below that. The capture repair restored Max account 3's history of
      14-22 September, which holds one pure-Opus stretch: 98.7% cache reads and almost all
      headless. Alone it read 906M tokens per window against the 624M median of the account's
      27 stretches there. That is one stretch's own token mix, not the account's level.
    Source: `tracker/four_accounts.py`, `tracker/four_accounts_page.py` (what the publisher
    and the invariants read of a run, stdlib only, so numpy stays out of the publisher),
    `tracker/publish.py` (`load_four_accounts`,
    `_segment_regimes`, `_chained_levels`), `tracker/credits.py` (`regime_index`,
    `chain_regimes`, `chain_certified_weeks`, `REGIME_METHOD`, `regime_figures`),
    `tracker/invariants.py` (checks 4-10), `bin/daily.sh` (the daily run and the state commit),
    `tests/test_four_accounts.py` (the rule, the hold, the null on shifted accounts, and
    recovery on the readings frozen on 2026-10-23 in `tests/fixtures/four_accounts`).

23. **Opus 5.5 is valued at Anthropic's list ratio, 0.8x Opus, in every comparison, added
    2026-10-23.** Every figure that compares credits across time (`credits.comparison_rate`:
    the four-account detector's lines and sizes, the window and week figures, the headless
    factor, the joint fits) values Opus 5.5's input, cache-write and output tokens at its
    input list-price ratio to Opus 5 in data/prices.json (4/5 = 0.8; output 20/25, cache
    writes 5/6.25 and 8/10 are the same ratio), not at its fitted rate. The detector moves it
    across no rate interval (`four_accounts.rate_intervals` leaves it out), and its state
    names it under `valuation.pinned_at_list`. Its fitted rate stays the per-model
    measurement the page publishes. Every other family keeps its fitted rate, and every
    cache read stays at the one fitted cache-read weight.
    - Why. The 23 September step's after side runs on Opus 5.5, which the before side never
      used, so on the credit lines its fitted rate and the limit change trade off: across
      that rate's interval the weekly step read +5.0% to +34.6% (rule 22). The meters accept
      the list ratio. On the records of 2026-10-23 the rate in force is 0.780x Opus, 80%
      interval [0.652, 0.938], and a refit on that day's records reads 0.789x [0.644,
      0.944]. Holding it at 0.8 and refitting everything else raises the pooled fit's
      residual sum of squares by 0.01 sigma^2 (delta SSE over the fit's residual variance),
      against the 3.84 a one-rate restriction may cost at 95%. With it held, the accounts
      still agree on the 23 September step.
    - What was tested and not adopted, on the same records with rules written before any
      figure was computed: Sonnet 5.5 at its list ratio 0.4x does not fit (delta SSE 21.1
      sigma^2), and Opus 5.5's cache reads at their list ratio (0.4x the pooled weight) do
      not fit either. So only Opus 5.5 is pinned, Sonnet 5.5 stays fitted, and every family
      shares the one fitted cache-read weight.
    - A pinned family with no list price in data/prices.json falls back to its fitted rate.
      The pin outranks a rate absorbed at a family's first use (rule 10), which would
      otherwise value Opus 5.5 at a joint fit's rate.
    - On the records of 2026-10-23, replayed from no state with the rates in force of
      2026-10-13 (no refit): the 23 September weekly limit reads +20.5% [+13.5, +27.9]
      (before: +18.3% [+11.5, +25.6]). Its rate range narrows from +0.6% to +39.3% to +15.1%
      to +24.6%, interval with rates [+8.4, +32.3] (before [-5.2, +47.8]), and it is no
      longer rate-limited. The five-hour limit beside it reads +19.7% [+3.1, +39.1] (before
      +17.8% [+1.3, +37.0]). The 14 September windows-per-week step (-17.4%) and the
      provisional 9 October step (-9.2%) do not move.
    Source: `tracker/credits.py` (`PINNED_AT_LIST`, `pinned_rate`, `comparison_rate`),
    `tracker/four_accounts.py` (`rate_intervals`, `valuation_block`, `VALUATION_RULE`),
    `tests/test_pinned_at_list.py`.

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
