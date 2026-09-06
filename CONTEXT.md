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

**Session**:
One transcript file's worth of turns (a subagent's own transcript counts as its own session). `session_tokens[model]` is the median cumulative tokens of a real session on that model over the last 30 days; the page's "about N sessions per window" unit.
_Avoid_: task, conversation, run

**Derived rate**:
A model's published rate computed from another model's probe through the dollar invariant, rather than probed directly.

**Regime**:
A stretch of history in which the measured limit is held flat, ended by a detected change.
_Avoid_: era, level, plateau

### Operation

**Rotation**:
The fixed order in which scheduled probes take turns across models: Sonnet, Opus, Fable.
_Avoid_: cycle, schedule, round-robin

**Expectation**:
The tokens per percent a probe assumes before it starts, used to size its bursts and prompts.
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
Two agreeing readings that both differ from the earlier median: the limit moved.
_Avoid_: shift, event

**Alert**:
One email to Jonathan, sent by the tracker through the site's send endpoint, for an outlier, a confirmed change or a refused weight.
_Avoid_: notification, ping, warning email

**Probe account**:
A subscription account used only for probing. Dave is primary; Jono Work is the fallback.
_Avoid_: test account, alt
