# Closure of the Codex audit of 2026-09-16

The audit is preserved in the parent workspace repo at
`.agents/codex/claude-usage-tracker-audit-2026-09-16/` (commit 4a42692 there). It
was pinned to collector `8b42b65`. This file states each finding's status on main
at `eba1852` (2 October 2026).

**Summary.** PR #57 (`44471f4`) repaired all 16 findings in the collector. Two
days later Jonathan reviewed the live page and reversed part of that repair. PR
#59 (`abd8413`) reversed findings 11 and 12 and part of 6. PR #60 (`1f0a92a`)
reversed the contributor `usd_per_pct` and points gating from findings 7 and 14.
Those reversals are owner decisions, so they are recorded below as **reverted by
decision**. They are not reopened here. The remaining collector findings hold on
current main. This was checked by replaying the audit's counterexamples against
current code.

The audit's `audit_checks.py` cannot run as written. It imports a
`collector/` checkout and a `live-data.json` snapshot that were never preserved,
and it asserts the *old, wrong* outputs (for example `== 7.5`). Its
counterexamples were ported instead, inverted to assert the repaired behaviour:

- `tests/test_core_audit_repairs.py` (from #57) covers findings 1, 3, 4 (rounding), 5, 9, 10, 13 and 15.
- `tests/test_audit_2026_09_16_closure.py` (this branch) covers findings 2, 4 (cumulative step), 7, 8, 9 (sampler and contributor) and 16.
  At `8b42b65` the file fails at collection, because the repaired functions do not exist there.

Platform-side parts (`website/src/...` in all-done-sites-platform) are outside
this repo. They are marked "platform" and were not assessed.

| # | Finding | Status | Evidence | Fixing commit |
|---|---|---|---|---|
| 1 | "API value" is meter dollars | Fixed | `meter_budget_per_window` and `api_value_per_window`/`api_list_value_per_window` are separate fields. Test: `test_meter_budget_and_actual_api_list_value_use_distinct_units` | 7983d57 via #57 (44471f4) |
| 2 | Plan/model eligibility (Fable on Pro, 50% on Max) | Fixed (collector). The UI side is platform | `_model_plan_limits` in tracker/publish.py. Test: `Finding2ModelEligibility` | 7983d57, 043efc1 via #57 |
| 3 | Weekly pairing drops denominator-only movement | Fixed | `(0,0),(30,4),(30,6),(60,10)` gives 6.0. Test: `test_denominator_only_movement_is_retained` | 521cb29 via #57 |
| 4 | Detector misses cumulative steps, invents rounding steps, overstates date/scope | Fixed | Audit's 60/10 then 18/4 case now yields one −25% event with levels 6.0 and 4.5 (`Finding4CumulativeStep`). The (11,1)/(47,8) case yields no event (`test_independent_rounding_errors_do_not_invent_a_step`). `ChangeEvent` carries onset bounds, confirmation, evidence points and intervals. Window-scope events are provisional | 521cb29, 6099154 via #57; later #122/#126 |
| 5 | Smoother splits/misdates one cut | Fixed for onset. History blending was reverted by decision | Test: `test_persistent_segment_sets_the_onset_not_a_stray_low_day`. A sub-15% step stays one blended regime by Jonathan's decision (`test_below_threshold_history_holds_one_blended_regime`) | 521cb29 via #57; partly reversed by #59 |
| 6 | Competing weekly "current"; Pro borrows Max 5x; forced seam | Partly fixed; the rest reverted by decision | Max 20x current is a pooled, dated, stale-flagged estimate. Max 5x/Pro `current` (Pro `assumed: true`) and `weekly_window_ratios` were restored by #59 at Jonathan's request. The UI ignoring `assumed` is platform | 7983d57 via #57; reversed in part by abd8413 (#59) |
| 7 | Contributor windows/week from token quotients | Fixed | Points carry no `windows` (`Finding7ContributorWeeklyRatio`). #60 restored only the points and `usd_per_pct` gating, not the quotient | 6ed9015 via #57 |
| 8 | Mixed-model fallback shown as selected model | Fixed (collector). The UI fallback is platform | An unpriced model drops the by-model map (`Finding8MissingModelIsMissing`). The summaries share the 5-point floor | 6ed9015 via #57 |
| 9 | Unknown-model tokens vanish before pricing | Fixed | The passive path withholds any stretch with unpriced work (`test_any_positive_unpriced_work_withholds_a_monetary_stretch`). The sampler keeps raw ids. Contributor values are withheld (`Finding9SamplerKeepsUnknownModels`) | 80b8a70, 6ed9015 via #57 |
| 10 | Incomplete capture; invisible resets; hidden uncertainty | Fixed as the audit recommends | Reset-bearing meter logs; `reset_verified` and `reset_source` per stretch; reset-less evidence is `conditional` and fires no events (`test_archived_resetless_evidence_is_conditional_and_never_fires_events`). `CAPTURE_GATE` stays off on purpose: the audit itself says not to revive a narrow accept band | 80b8a70, 7983d57 via #57; 8a152f3 (#77) |
| 11 | Per-model tokens are extrapolations; sessions/window | Reverted by decision | `session_tokens` restored by #59 at Jonathan's request. The reference mix is still versioned (`data/reference_mix.json`) | 7983d57 via #57; reversed by abd8413 (#59) |
| 12 | Weekly token chart / held early history | Reverted by decision (collector). The chart product is platform | The step-function history with `source: "held"` days was restored by #59 | reversed by abd8413 (#59) |
| 13 | Missing passive input silently switches to probes | Fixed | Rates become `unavailable` with a reason. Probes publish separately (`test_no_eligible_passive_measurement_emits_nullable_rates`) | 7983d57 via #57 |
| 14 | Contributor aggregation: stale medians, identity, no promotion | Partly fixed; the rest reverted by decision | 30-day `EVIDENCE_DAYS` cutoff, `identity_basis: unverified_source_ids`, and no promotion claim. Per-contributor all-sample medians inside the window were restored by #60 (the audit's 10-day-old $1 vs current $0.50 case still publishes $1) | 6ed9015 via #57; reversed in part by 1f0a92a (#60) |
| 15 | Cache-write 1h duration discarded | Fixed | `cache_write_1h` is a priced subset, never double counted (`test_cache_write_one_hour_is_a_subset_but_gets_its_price_premium`, `test_turn_parser_retains_cache_duration_without_double_counting_total`) | 80b8a70 via #57 |
| 16 | Fresh timestamps conceal stale evidence | Fixed, with one residue | Weekly `stale` is judged against the publish clock (`Finding16WeeklyFreshness`). Per-rate `freshness`/`evidence`/`quality`, `meter_read_at`, `account_feeds`. Residue: `passive_account_count` still counts any account with a stretch anywhere; per-account freshness is in `account_feeds`. Left as is because tracker/publish.py is UT-1's claim tonight | 7983d57 via #57; b26559a (#85) |

## Decisions taken on this run

- Owner reversals (#59, #60) were not re-fixed. Jonathan rejected the #57 page
  explicitly ("the graphs are fucked now…"). Undoing his decision overnight would
  repeat that.
- Finding 16's `passive_account_count` residue was not changed: the publisher is
  claimed by UT-1, and the per-account `account_feeds` already carries the
  freshness detail.
