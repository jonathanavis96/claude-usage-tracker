# Claude usage tracker — design

Date: 2026-09-05. Status: approved in conversation, pending spec review.

## Purpose

A public page on alldonesites.com that states, from measured data, how much a
Claude subscription actually buys per 5-hour window, whether Anthropic has
changed that amount, and by how much. It exists as advertising: All Done Sites
measures before it claims. The page must stay honest, so every number on it is
either measured on Jonathan's own account or derived from a measured number by
arithmetic the page explains.

The visual target is `docs/mockup.html` in this repo (approved 2026-09-05).

## The core measurement

### Primary: the fixed probe

A cron job on `ssh gs` measures **tokens per percent tick**. It runs as
`dclaude` (dave@greenscape.systems, credentials in that host's `.claude-dave`
directory) and falls back to `wclaude` (jono@greenscape.systems,
`.claude-javiswork`) when the Dave account is busy. Both are on Max 20x, the
same plan as Jonathan's own account, so probe and passive figures are directly
comparable. The prompt, model and effort never change, so the tokens per
prompt are constant within noise, and the only thing that can move the tick
size is Anthropic's limit. The measurement is independent of anyone's
workload and of whether masterrig is on.

- The endpoint reports whole percent, so a single delta has up to ±0.5% of
  rounding error. The probe therefore runs a small fixed prompt (about 0.2%
  of a window) in a loop, reading `GET https://api.anthropic.com/api/oauth/usage`
  after each run, until `five_hour.utilization` increments. It notes the token
  count at that tick, keeps going until the next tick, and reports the tokens
  consumed between the two ticks as one percent's worth. Precision is one
  small prompt, about a tenth of the signal. Cost is roughly 1 to 1.5% of a
  window per probe.
- A probe that sees `resets_at` change or utilization fall mid-run is
  discarded and re-run once.
- Cadence: twice a day, models rotated (Sonnet 5, Opus 5, Fable 5.1), so each
  model is probed at least every 36 hours. The daily figure per model is the
  median of its probes over the trailing 3 days.
- Each probe records: timestamp, model, effort, per-prompt tokens by class
  from the `--output-format json` usage block, utilization readings, tick
  times, and derived tokens per 1% of window.
- Other usage on the account between ticks would corrupt a probe, so the
  account must be idle while a probe runs. The probe reads utilization twice,
  2 minutes apart, on the Dave account; if it moved, it tries the Jono Work
  account the same way. If both are busy it sleeps 15 minutes and retries, up
  to 4 hours, then gives up for that slot and logs it. During the run it aborts
  if a tick arrives faster than its own prompts can explain.
- Each probe row records which account it ran on. Both are Max 20x, so the
  rate is pooled, but the account is kept for diagnosis.

### Secondary: the passive join

Two data streams already exist on masterrig:

- **Utilization samples.** `~/.moonlighter/usage_log.jsonl` holds
  `five_hour`, `seven_day` and `seven_day_sonnet` utilization with `resets_at`,
  sampled every 30 minutes since 2026-06-13. From 2026-08-18
  `~/.paperclip/ops/mis-usage-ceiling-systemd.log` adds samples every 60 to
  300 seconds. Both are read; the denser source wins where they overlap.
- **Token consumption.** `~/.claude/projects/**/*.jsonl` transcripts record
  every assistant turn with model, input, output, cache-read and cache-write
  token counts and a timestamp. On disk from at least 2026-07-08.

Joining them gives the **rate**: tokens consumed between two samples divided
by the utilization delta, in tokens per 1% of the 5-hour window. Multiplied by
100 that is the effective window size. Computed per model and per cache class.

The passive join supplies what the probe cannot: the real cache-class split of
a working session, the API-value figure, history back to July 2026 covering
the Max 5x period, and the measured 5x-to-20x plan ratio from the 2026-08-18
plan change. It does not feed change detection or the headline.

### Join rules

- Transcript parsing follows the traps documented in
  `~/code/claude-friction-audit/README.md`: stream, never load; dedup usage by
  `message.id` (one record per content block); ignore sidecar and automation
  noise where it carries no usage.
- A window resets when `resets_at` changes or utilization drops. Intervals that
  straddle a reset are discarded.
- Intervals with a utilization delta of 0 are pooled with their neighbours until
  the delta is at least 1, because the endpoint reports whole percent.
- Intervals are attributed to a model when at least 90% of their tokens came
  from one model. Mixed intervals feed the total rate but not the per-model
  rates.
- The rate is computed daily as the median of that day's qualifying intervals.
  Days with fewer than 5 qualifying intervals carry the previous value and are
  marked interpolated.

### Effort tiers

Effort does not change the rate. It changes how many tokens one prompt burns.
A one-off calibration run measures that: a fixed task run through `claude -p`
at each of 3 models × 5 efforts (low, medium, high, xhigh, max) × 3 repeats,
median tokens per run, with the usage endpoint read before and after each
batch so the utilization delta is measured directly as well. Output is an
effort matrix JSON committed to this repo. It is re-run only when a model is
added or Anthropic changes effort semantics. The page presents effort figures
as "about", because they are one task shape.

### Plans

All three accounts are Max 20x. Jonathan's passive history measured Max 5x
until 2026-08-18 and Max 20x after, so the 5x-to-20x ratio is observed. Pro is scaled by the published 1:5 ratio to Max 5x. The
table says which figures are measured and which are scaled.

### Change detection

Detection runs on probe data only. A change is declared when the rolling
3-day median of the probe rate differs from the preceding 7-day median by more
than 5%, above the tick method's error of about 2%, and the difference
persists for 2 consecutive days. The event records date, direction and
percentage. The headline shows the most recent event. Detection runs per
model, and an event is reported when any model changes; the headline percent
is that model's.

## Components

### 1. Collector (this repo, private, Python 3 stdlib)

- `tracker/probe.py` runs one tick probe and appends a row to `probes.jsonl`.
  Deployed to `ssh gs` with a cron entry; it selects the account per the idle
  rules above. The same file runs on masterrig for the spike.
- `tracker/samples.py` reads the utilization log into (timestamp, five_hour,
  seven_day, resets_at) rows. Goes on reading the existing systemd log; no
  second sampler.
- `tracker/turns.py` streams transcripts modified since a watermark and yields
  deduplicated (timestamp, model, input, output, cache_read, cache_write).
- `tracker/join.py` produces qualifying intervals and daily rates.
- `tracker/detect.py` runs change detection over the daily history.
- `tracker/publish.py` writes the public JSON and the private history.
- `tracker/calibrate.py` runs the effort calibration and writes the matrix.
- `bin/daily.sh` runs on `ssh gs`: pulls the probe rows, merges the latest
  passive-join output pushed from masterrig when available, runs detection,
  writes the public JSON, commits it into the alldonesites checkout and pushes.
  Cron, once a day. The passive output being stale only ages the cache split
  and the pre-probe history; the headline stays current.

Tests cover the join rules with synthetic fixtures. No network in tests.

### 2. Public JSON

Written to `website/public/data/claude-usage.json` in alldonesites. Shape:

```json
{
  "generated_at": "2026-09-05T20:15:00Z",
  "last_sample_at": "2026-09-05T20:13:07Z",
  "plan_measured": "max20",
  "plan_ratios": { "pro": 0.05, "max5": 0.25, "max20": 1.0 },
  "rates": {
    "sonnet-5":   { "tokens_per_window": 42000000, "source": "probe", "probe_effort": "high", "split": { "input": 0.062, "output": 0.021, "cache_read": 0.907, "cache_write": 0.010 } },
    "opus-5":     { "...": "..." },
    "fable-5-1":  { "...": "..." }
  },
  "effort": { "sonnet-5": { "low": 900000, "medium": 1400000, "high": 2520000, "xhigh": 3900000, "max": 5600000 }, "...": {} },
  "api_price_per_mtok": { "sonnet-5": { "input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75 }, "...": {} },
  "history": [ { "date": "2026-07-08", "tokens_per_window": 45100000, "interpolated": false }, "..." ],
  "last_change": { "date": "2026-09-02", "direction": "decreased", "percent": 14 }
}
```

`history` holds the last 90 days per model: probe-derived from the day probes
began, passive-join-derived before that and marked `"source": "passive"`,
expressed as tokens per window on the measured plan. The page scales it by the
selected plan ratio and model rate. Effort and price tables are static inputs
copied into the JSON so the page has one fetch.

### 3. Page (alldonesites, public repo)

- Route `/claude-usage-tracker` in `website/src/App.tsx`, component under
  `website/src/pages/`. Uses the site's existing chrome and CSS tokens from
  `src/styles/home.css`. No new dependencies; the chart is inline SVG.
- Fetches the JSON on load. Every figure is arithmetic on it:
  - tokens per window = rate[model] × plan_ratio[plan]
  - split figures = tokens per window × split fractions
  - tasks per window = tokens per window ÷ effort[model][effort]
  - tasks per week = tasks per window × 28
  - API value = Σ split tokens × price per class
- Three inline dropdown words: plan, model, effort. No slider.
- Headline template: "Anthropic last {increased|decreased} Claude's limits by
  {percent}% on {date}." Red for decreased, green for increased. If no change
  has ever been detected: "Anthropic hasn't changed Claude's limits since
  {first date measured}."
- Chart: selected plan and model only, y axis fitted to the data range so a
  change is visible, gradient fill to the axis, dates in brand blue, the last
  change marked with a dashed dark red line and a filled tag.
- Last sample rendered in the viewer's local time from the UTC stamp.
- "How we measure this" and "Caveats" are collapsed sections; caveat text
  states: one account, one workload mix, whole-percent rounding, effort figures
  from one task shape, Pro and Max 5x scaled.
- Guide-style metadata for SEO. No AI trailers or handoff files in that repo.

### 4. Publishing

The daily job commits the JSON to alldonesites `main` and pushes. The existing
Pages workflow deploys. The repo is public so the daily deploy is free. Commit
message is fixed text, no attribution trailers.

## Error handling

- Usage log unreadable or transcripts missing: the job exits non-zero, leaves
  the previous JSON in place, logs to `~/.paperclip/ops/claude-usage-tracker.log`.
  The page shows the previous data and its own `generated_at`, so staleness is
  visible.
- Fewer than 5 qualifying intervals in a day: previous rate carried forward,
  point marked interpolated, drawn hollow on the chart.
- JSON fetch fails on the page: the hero shows "Data temporarily unavailable"
  and the rest of the page still renders with the method text.

## Testing

- Collector: unit tests with synthetic samples and turns covering reset
  straddling, zero-delta pooling, model attribution threshold, interpolation,
  and change detection edge cases. Run only the touched files.
- Page: the site's existing typecheck plus a render test with a fixture JSON.
- Probe: a dry-run mode that replays a recorded sequence of usage readings
  and CLI JSON outputs, so tick detection, reset handling and the idle guard
  are tested without spending usage.
- End to end: the spike in step 1 of the plan is the acceptance check for the
  method: probe sizing, weekly cost, and agreement between probe rate and
  passive rate on the same day. Its result is recorded in `docs/spike-2026-09.md`.

## Out of scope

Other people's accounts beyond the two named, per-user login, historical data
before 2026-06-13, dark mode beyond what the site already does.
