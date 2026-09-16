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

**Dollar invariant**:
The rule that the meter tracks API list value, not token count, so one model's probe gives every other model's rate through the price table.
_Avoid_: cost equivalence, price parity

**Class weight**:
How much harder the meter charges one token class (input, output, cache read, cache write) than its list price suggests, relative to cache write.
_Avoid_: multiplier, penalty

**Meter weight**:
How much harder the meter charges one model than another for the same list value. Currently 1.0 for every model.

**Passive split**:
The mix of token classes in Jonathan's own real sessions, used to convert dollars per window into tokens per window for the page.
_Avoid_: real-world mix, session profile

**Weekly windows**:
How many full five-hour windows the seven-day limit holds, measured rather than assumed, from two independent sources: the passive meter log (paired five-hour and seven-day deltas within a single window of each, bucketed by week, each week's total five-hour movement divided by its total seven-day movement) and each probe row's own whole-run before/after meter reads (same division, bucketed by the probe's own weekly reset date or, lacking that, its ISO calendar week). `weekly_windows` publishes both raw series (`passive`, `probe`) plus one `{current, history}` object per plan. Each raw series also carries the same pairs bucketed by five-hour window (`by_window`, one `Window point` per window); the weekly rows are what the page charts, the window points are what detection and the live plan's `current` run on.
_Avoid_: 28 (the calendar count of five-hour windows in a week; not the measured figure)

**Window point**:
One five-hour window's paired meter movement: `five_hour_pct` (d5) over `seven_day_pct` (d7), keyed by the window's reset time (`window_ending`), with `windows` = d5/d7 or null when the seven-day meter did not move. Its weight is its d7: a point under 7 points of d7 is too noisy to vote on its own (rounding alone can move a d7=6 ratio by 17%, and on the real log a lower floor fired three false changes), but its movement still counts in every pooled figure.
_Avoid_: bucket, sample

**Pooled**:
Total d5 over total d7 across a set of window points, as opposed to a median of their individual ratios. Every base, confirmation and `current` on the weekly series is pooled, so a heavy window counts for more than a thin one.
_Avoid_: average, mean

_Plan_: The count is per plan, not one continuous series -- Jonathan's Max 5x -> Max 20x move (`PLAN_CHANGE`, tracker/passive.py) splits the passive history in two. A passive week whose span crosses `PLAN_CHANGE` belongs to neither plan and is dropped, and a passive window point counts as `max20` only when its window starts after `PLAN_CHANGE` day. `max5` is frozen passive-era history with no probe series and no change detection (Jonathan is not reverting to it); `max20` is the live plan -- probe weeks replace passive `max20` weeks in its chart rows from the first probe week on, and it is the only plan change detection runs on. `pro` has no measurement of its own, so it publishes `max5`'s figures again (same 5x-ratio era) with `"assumed": true`; `max20` and `max5` carry `"assumed": false`.

_Current_: `max20.current` is the pooled ratio of the current `Regime`'s window points over the trailing fortnight (anchored on the newest point), so it follows a detected step from the day it fires. It falls back to the median of the last two complete weekly rows only when no window points have arrived (a passive.json from before they existed) or the fortnight holds under 10 points of d7. `max5.current` is always that median.

**Stretch**:
One gs account's meter movement of at least 10%, pooled from consecutive same-window readings of that account's own meter log, with the meter dollars that account's own transcripts spent across it. The unit of passive measurement on gs (tracker/join.py `build_stretches`). Whole-percent rounding carries one point per separate window piece, so a 10% stretch reads to about ±10% and a busy day's pooled stretches to a few percent.
_Avoid_: interval (masterrig's 1% unit in the same module), span (a probe term)

**Capture**:
A stretch's meter dollars per 1% over its account's reference (the median of that account's recent accepted stretches): the share of the meter's movement that the account's transcripts on gs account for. 1.0 is complete capture.

**Unaccounted traffic**:
Meter movement a stretch's transcripts do not explain: the stretch reads more than 15% below its account's reference even allowing for rounding. It comes from using the account off gs or from broken transcript collection, is withheld, is kept for inspection, and is never published as a rate. Its mirror, **surplus**, is transcripts the meter did not count, from a transcript directory another account also writes to.
_Avoid_: missing tokens, leakage

**Level shift**:
A run of withheld stretches that agree with each other at a new level. It is what a genuine limit change looks like, and also what steady off-gs use looks like, so it stays withheld until the other gs account or a probe steps the same way; only then is it a change and the reference rebased (tracker/capture.py).

**Session**:
One transcript file's worth of turns (a subagent's own transcript counts as its own session). `session_tokens[model]` is the median cumulative tokens of a real session on that model over the last 30 days; the page's "about N sessions per window" unit.
_Avoid_: task, conversation, run

**Derived rate**:
A model's published rate computed from another model's probe through the dollar invariant, rather than probed directly.

**Passive calibration**:
The frozen ratio (`data/prices.json._passive_calibration.ratio`) a gs-passive reading is divided by before it can join the probe's dollar series: the two instruments read the same meter on different scales (measured on gs, Jono Work's accepted passive days ran $1.26-$1.81 per window against the probe's $0.97), and concatenating them raw invented change events the live page never had. `tracker.gs_passive --calibrate` computes it (median of accepted passive daily readings over median of usable probe readings, in a stated window) but never writes it -- Jonathan pastes the value in by hand, so it stays fixed while real movement in either series still shows. Missing entirely, passive readings are left out of the join altogether rather than joined unscaled.
_Avoid_: normalization, scale factor

**Instrument**:
Which kind of reading measured the currently published figure: `probe` (a scheduled run) or `passive` (a gs account's own transcripts against its own meter, tracker/gs_passive.py). Probes are no longer scheduled as of 2026-09-16 (issue #39); passive is the everyday instrument now, and a probe run by hand still enters the same dollar series and can still be the newest reading. Top-level `instrument` is the newest reading of either kind; a model's own `rates[model].source` is `passive` when the newest reading in its current `Regime` is passive, `probe`/`derived` otherwise, alongside `measured_at` (that reading's own time) and the older `probed_at` (this model's own latest probe row, unaffected by a passive reading).
_Avoid_: source (already means something narrower, per-model), method

**Regime**:
A stretch of history in which the measured limit is held flat, ended by a detected change. On the weekly series a regime starts at a `Weekly change`'s first window, and every later base is pooled from that regime only.
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
Two agreeing readings that both differ from the earlier median: the limit moved. Published events and `last_change` carry a `scope`, `"window"` or `"weekly"`, naming which series the change was detected on.
_Avoid_: shift, event

**Weekly change**:
A step of more than 15% in weekly windows on the live plan's passive window points (`max20`), dated by the first window at the new level -- a day, not a week ending. A window point with at least 7 points of d7 whose ratio lies beyond threshold from the `Pooled` base (the regime's previous fortnight, at least 10 points of d7) even after conceding half a point of d7 rounding is a candidate; it fires once the points from it onward pool to at least 10 points of d7 over at least two windows and that pool still lies beyond threshold the same way. Never detected on the calendar-week rows: a mid-week step blends into the week's average (the 2026-09-13 cut published as a -14.5% week), and a week-then-confirming-week rule could not have surfaced it for nineteen days. `max5` is frozen and never runs detection, so the Max 5x -> Max 20x plan change itself is never reported as a weekly change. Published as an event with `"scope": "weekly"`. Mechanics and the rounding reasoning: tracker/detect.py.
_Avoid_: plan change (that is Jonathan's own subscription move, not a measured step)

**Alert**:
One email to Jonathan, sent by the tracker through the site's send endpoint, for an outlier, a confirmed change or a refused weight.
_Avoid_: notification, ping, warning email

**Probe account**:
A subscription account used only for probing. Jono Work (`jwork`, `~/.claude-javiswork`) is tried first and Dave (`~/.claude-dave`) second; an account with a `claude` session on this host that is not idle (per its `sessions/<pid>.json`) is skipped before its meter is read. The probe compares each account's session state between readings rather than the status at the moment of sampling, so it aborts on a session that goes busy while it runs and on one whose whole turn fits between two readings; it also keeps the kernel's record of session files created or removed in that `sessions/` directory, so a session that starts and exits between two readings aborts it too (the probe's own `claude -p` prompts write session files as well, and are told apart by pid). An account whose weekly (seven-day) meter reads above 90% is passed over as well, and the log names that reason `weekly`, apart from `busy`.
_Avoid_: test account, alt
