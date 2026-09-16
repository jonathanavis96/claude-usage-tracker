# Claude Usage Tracker

Measures how much work a Claude subscription buys per 5-hour window, from the account's own usage meter, and publishes the result as a public page.

## Language

### Measurement

**Meter**:
The subscription's 5-hour utilization percentage as reported by Anthropic's usage endpoint, in whole percent.
_Avoid_: usage bar, gauge, limit meter

**Window**:
The 5-hour period the meter counts against. It resets at a fixed time and the meter returns to 0.
_Avoid_: session, period, block

**Tick**:
The moment the meter advances by one whole percent.
_Avoid_: step, increment, bump

**Span**:
The prompts sent between two consecutive ticks.
_Avoid_: interval, gap, segment

**Probe**:
One run that sends prompts to a model until the meter has advanced a chosen number of ticks and records the tokens spent per 1%.
_Avoid_: test, calibration run, measurement run

**Row**:
The record a probe appends to the history file when it completes.
_Avoid_: entry, sample, reading

**Reading**:
One meter value observed after a prompt, inside a probe.
_Avoid_: sample, observation

**Alignment**:
The first tick a probe waits for so that measurement starts on a whole-percent boundary. The alignment span is discarded.
_Avoid_: warm-up, calibration, sync

**Skip**:
A discarded span after alignment, removed because the first span looks like meter catch-up rather than a true rate.

**Burst**:
Several prompts sent at the same time inside one span, sized to a fraction of the expected span so the tick should not arrive during them.
_Avoid_: batch, parallel run

**Early tick**:
A tick that arrives during a burst, meaning the span was shorter than expected and the limit has probably fallen.

**Reset start**:
A probe that waited for a window reset within 20 minutes of starting and took the meter's 0.0 as its first tick, spending no alignment span.

**Settle**:
The wait between a prompt returning and the meter being read.
_Avoid_: delay, cooldown

**Payload**:
The shape of the probe's prompt traffic: `prose` (large cache-write-heavy input, one-word reply) or `output` (small input, long generated reply).
_Avoid_: prompt type, mode

### Rates

**Tokens per percent**:
The tokens a probe spent for each 1% the meter advanced. The probe's raw result.
_Avoid_: rate, cost per tick

**Meter budget**:
The class/model-weighted dollar unit used to compare captured work with meter movement. It is distinct from API list value. Converting it to another model is a reference-mix scenario using the price table and assumed weights, not a direct measurement of that model's cap.
_Avoid_: API value, dollar invariant, price parity

**Class weight**:
How much harder the meter charges one token class (input, output, cache read, cache write) than its list price suggests, relative to cache write.
_Avoid_: multiplier, penalty

**Meter weight**:
How much harder the meter charges one model than another for the same list value. Currently 1.0 for every model.

**Reference mix**:
The frozen, versioned token-class mix (data/reference_mix.json) that converts a meter budget into tokens per window, and those tokens into API list value, for the page. It is a scenario, not a measurement of anyone's workload: it changes only with a new id, so figures stay comparable through time (audit 2026-09-16, finding 11). Replaces the passive split, which moved with every day's sessions.
_Avoid_: real-world mix, session profile, passive split

**Weekly windows**:
How many full five-hour windows the seven-day limit holds, measured rather than assumed, from two independent sources: the passive meter log (paired five-hour and seven-day deltas within a single window of each, bucketed by week, each week's total five-hour movement divided by its total seven-day movement) and each probe row's own whole-run before/after meter reads (same division, bucketed by the probe's own weekly reset date or, lacking that, its ISO calendar week). `weekly_windows` publishes both raw series (`passive`, `probe`) plus one `{current, history}` object per plan. Each raw series also carries the same pairs bucketed by five-hour window (`by_window`, one `Window point` per window); the weekly rows are what the page charts, the window points are what detection and the live plan's `current` run on.
_Avoid_: 28 (the calendar count of five-hour windows in a week; not the measured figure)

**Window point**:
One five-hour window's paired meter movement: `five_hour_pct` (d5) over `seven_day_pct` (d7), keyed by the window's reset time (`window_ending`), with `windows` = d5/d7 or null when the seven-day meter did not move. Every same-window pair in which neither meter fell counts, whichever of them moved: a seven-day tick with the five-hour meter still is denominator the ratio needs (audit 2026-09-16, finding 3). `pieces` is how many separately read stretches of the window it pools (one unless a gap broke the readings), `rounding_interval` the ratio's range under whole-percent rounding, and `reset_verified` whether the log named the window's reset.
_Avoid_: bucket, sample

**Pooled**:
Total d5 over total d7 across a set of window points, as opposed to a median of their individual ratios. Every level, confirmation and `current` on the weekly series is pooled, so a heavy window counts for more than a thin one. A pool of n pieces concedes min(n, 2 sqrt n) points of rounding to each total for its interval (tracker/detect.py).
_Avoid_: average, mean

_Plan_: The count is per plan, not one continuous series -- Jonathan's Max 5x -> Max 20x move (`PLAN_CHANGE`, tracker/passive.py) splits the passive history in two. A passive week whose span crosses `PLAN_CHANGE` belongs to neither plan and is dropped, and a passive window point counts as `max20` only when its window starts after `PLAN_CHANGE` day. `max5` is frozen passive-era history with no probe series and no change detection (Jonathan is not reverting to it), published as `historical_only` with its regimes and the plan change as the account owner's unverified record; `max20` is the live plan and the only one change detection runs on. The probe runs publish as their own `probe` series and never replace passive `max20` weeks (finding 13). `pro` has no measurement of its own and publishes none: nothing is copied from `max5` (finding 6). Every plan carries `"assumed": false` and an `availability` reason. Weekly rows paired before the finding-3 repair (no `pieces`) are published as `legacy_uncertain` and nothing is computed from them.

_Current_: `max20.current` is the pooled ratio of the current `Regime`'s repaired, reset-verified window points over the trailing fortnight (anchored on the newest point), so it follows a certified change from the publish that certifies it; with fewer than 10 points of d7 it is null. There is no fallback to a median of weekly rows. `max5.current` and `pro.current` are null without contemporaneous measurements; historical Max 5x evidence remains historical and is never copied into Pro. `current_estimate` carries the value's rounding interval, points, pieces, evidence dates, staleness (against the publish time) and quality.

**Stretch**:
One gs account's meter movement of at least 10%, pooled from consecutive same-window readings of that account's own meter log, with the meter dollars that account's own transcripts spent across it. The unit of passive measurement on gs (tracker/join.py `build_stretches`). Whole-percent rounding carries one point per separate window piece, so a 10% stretch reads to about ±10% and a busy day's pooled stretches to a few percent.
_Avoid_: interval (masterrig's 1% unit in the same module), span (a probe term)

**Capture**:
A stretch's meter dollars per 1% over its account's reference (the median of that account's recent accepted stretches): the share of the meter's movement that the account's transcripts on gs account for. 1.0 is complete capture.

**Unaccounted traffic**:
Meter movement a stretch's transcripts do not explain: the stretch reads more than 15% below its account's reference even allowing for rounding. It can come from off-machine use or broken transcript collection. Since 2026-09-16 it is recorded (`capture_status`) but published all the same (`CAPTURE_GATE` off in tracker/gs_passive.py): the 15% band withheld half of jwork's real stretches, and the half it withheld read low, so the published median was the median of the expensive half, and the audit of 2026-09-16 rules out a narrow band around the expected rate. Capture is therefore never verified, and every passive rate is published `conditional` with that reason. The one exception is a **collection gap**, a stretch whose capture is under a tenth of the reference -- the meter moving with nothing on gs to explain it -- which is still left out. Its mirror, **surplus**, is transcripts the meter did not count, from a transcript directory another account also writes to. A stretch with any unpriced-model work is never valued (finding 9).
_Avoid_: missing tokens, leakage

**Level shift**:
A run of withheld stretches that agree with each other at a new level. It is what a genuine limit change looks like, and also what steady off-gs use looks like, so it stays withheld until the other gs account or a probe steps the same way; only then is it a change and the reference rebased (tracker/capture.py).

**Session scenario** (not published):
Transcript token totals cannot be divided into the reference-mix window estimate. A sessions-per-window promise remains unavailable until session meter cost is measured under an explicit parent/subagent and effort policy.

**Derived rate**:
A model's tokens per window converted from the measured meter budget on the reference mix with that model's price-table weights, rather than measured on that model (`source: derived_reference_mix`).

**Passive calibration** (retired 2026-09-16):
The ratio a gs-passive reading was to be divided by before joining the probe's dollar series. Measured at 1.80 on 2026-09-16 (Jono Work's accepted passive days against the probe), it still invented change events when applied, because the probe payload (98% cache_write by list value) and real sessions (59% cache_read, 24% cache_write, 17% output) are different quantities under any single scale. The passive series is published on its own now; probe rows never join it. `tracker.gs_passive --calibrate` stays for the record and is not used.
_Avoid_: normalization, scale factor

**Output weight fit**:
`class_weight.output` in data/prices.json is measured from the passive stretches themselves: `tracker.gs_passive --fit-output-weight` solves meter movement = a x list dollars of (input + cache_write) + b x list dollars of output over the capture-accepted stretches, cache_read pinned at its measured 0.0, and reports b / a. On jwork's 60 accepted stretches 2026-09-05..15 it is 1.018, so the weight is 1.0 (was 1.8 from a single probe pair). The value is pasted into prices.json by hand so it stays fixed while real movement shows.

**Instrument**:
The compatible source behind a metric (`instrument`, `rates[model].evidence.source`). Measured passive readings require reset ids in the meter log and no unpriced work; capture completeness is diagnosed but not verified. Legacy reset-less passive evidence (jwork's ceiling log) remains a conditional reference with no change events. Missing passive evidence makes rates unavailable; probe payloads remain a separate series and never replace passive figures on an incompatible scale. History begins at the first compatible observation and is not backfilled.

**Regime**:
A detector segment over compatible observations. A published regime carries the raw pooled level, its rounding interval, evidence count and provenance. It is an account-scoped estimate, not an Anthropic policy statement.
_Avoid_: era, level, plateau

### Operation

**Rotation**:
The fixed order in which scheduled probes take turns across models: Sonnet, Opus, Fable.
_Avoid_: cycle, schedule, round-robin

**Expectation**:
The meter dollars per percent a probe assumes before it starts: the dollar invariant's median, handed to the probe as is. The probe sizes its prompt from it on the model's own prices, and the tokens per percent that size its bursts follow from that prompt, never from an earlier row's token-class split.
_Avoid_: estimate, prior, guess

**Drift**:
A reading more than 15% away from the median of that model's last four prose rows.
_Avoid_: anomaly, deviation

**Rerun**:
The immediate second probe on the same model that a drift triggers.
_Avoid_: retry, confirmation run

**Outlier**:
A drifted row whose rerun agreed with the earlier median. It stays in history but is flagged and ignored.
_Avoid_: bad row, glitch

**Change**:
A step detected in one published series: an observed change in this account's metric, not a proven change to Anthropic's limits. Published events and `last_change` carry a `scope`, `"window"` or `"weekly"`, naming which series the change was detected on, plus `metric`, `onset`, `confirmation` and `attribution: observed_account_metric_change`. Window-scope events (the smoothed passive detector) are `provisional`, and bin/daily.sh announces neither provisional nor legacy-uncertain ones.
_Avoid_: shift, event

**Weekly ratio change**:
An account-scoped change point in paired five-hour/seven-day movement. A split certifies only when both sides clear the d7 floors, their pooled levels differ by more than 15%, and their rounding intervals do not overlap. Detected on the window points, never on the calendar-week rows (a mid-week step blends into the week's average). Events publish onset bounds, confirmation evidence and the metric name; they do not claim that Anthropic changed a weekly cap or establish causation.
_Avoid_: plan change (that is Jonathan's own subscription move, not a measured step)

**Alert**:
One email to Jonathan, sent by the tracker through the site's send endpoint, for an outlier, a confirmed change or a refused weight.
_Avoid_: notification, ping, warning email

**Probe account**:
A subscription account used only for probing. Jono Work (`jwork`, `~/.claude-javiswork`) is tried first and Dave (`~/.claude-dave`) second; an account with a `claude` session on this host that is not idle (per its `sessions/<pid>.json`) is skipped before its meter is read. The probe compares each account's session state between readings rather than the status at the moment of sampling, so it aborts on a session that goes busy while it runs and on one whose whole turn fits between two readings; it also keeps the kernel's record of session files created or removed in that `sessions/` directory, so a session that starts and exits between two readings aborts it too (the probe's own `claude -p` prompts write session files as well, and are told apart by pid). An account whose weekly (seven-day) meter reads above 90% is passed over as well, and the log names that reason `weekly`, apart from `busy`.
_Avoid_: test account, alt
