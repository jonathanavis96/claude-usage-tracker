# Claude usage tracker

## What this measures

This tracks how much work a Claude subscription actually buys per 5-hour
window, by watching the account's own usage meter instead of guessing from
token counts. The public page is at
[alldonesites.com/claude-usage-tracker](https://alldonesites.com/claude-usage-tracker).

## Method

**Probe.** A probe sends fixed-size prose prompts to a model on an otherwise
idle Max 20x account until the 5-hour meter has ticked forward a chosen
number of whole percent, then reports tokens spent per 1%. Prompts are unique
per run so nothing gets served from cache by accident, and each span opens
with a burst of concurrent prompts sized to most of the expected span so a
probe finishes in minutes rather than an hour. See `tracker/probe.py` for
the mechanics (burst sizing, alignment, reset handling).

**Meter budget and API value.** Captured work is valued in weighted meter
dollars, using the assumptions in `data/prices.json`. The publisher keeps
that unit separate from the actual API list value of the declared token
bundle. Per-model token figures are derived reference-mix scenarios; they
are not direct measurements of each model's cap. The JSON exposes the
weights and assumptions used for the conversion.

**Reference mix.** The meter budget itself is measured passively, on gs
accounts' own transcripts against their own meter logs
(`tracker/gs_passive.py`). Converting it to tokens per window needs a token
mix, and the one used is frozen and versioned in `data/reference_mix.json`, so
a token figure only moves when the budget does. Probe rows never stand in for
the passive series: without an eligible passive reading the rates are
published as unavailable, with the reason.

**Weekly windows.** How many 5-hour windows the 7-day limit actually holds is
measured, not assumed to be 28. It comes from two independent sources: the
passive log (pairing 5-hour and 7-day meter movement within the same window)
and each probe row's own before/after reads, both bucketed by week for the
chart and by 5-hour window for change detection. Both numerator-only and
denominator-only movement is retained. Change candidates aggregate paired
movement and certify only when both levels' rounding intervals separate;
published events describe an observed account metric and do not attribute a policy
change to Anthropic. See the `weekly_windows` vocabulary in
`CONTEXT.md` for how the two series are kept separate per plan and
`tracker/detect.py` for the detection rules.

**Rotation.** Scheduled probes rotate Sonnet, Opus, Fable in a fixed order
(historically every 12 hours; see `docs/deploy-gs.md` for the current
cadence). A new reading more than 15% from the median of that model's last
four usable rows is drift: the wrapper reruns once, and the pair either
confirms a change, flags the first row an outlier, or is inconclusive.
Mechanics: `tracker/rotate.py`.

**Weekly output-weight run.** Once a week a dedicated probe measures how
hard the meter charges output tokens specifically, since that differs from
the cache-write-heavy traffic the main probe uses. See `docs/deploy-gs.md`.

## Data

**`history/probes.jsonl`** — one JSON line per probe row. Fields include:
`ts`, `model`, `tokens` (per class), `tokens_per_pct`, `ticks`, `skip`,
`payload` (`"prose"` or `"output"`), `early_tick`, `outlier`, `account`, plus
the raw per-reading `readings` list and window before/after meter values. See
`tracker/rows.py` for which rows count toward which series (output rows and
flagged outliers are excluded from medians and change detection).

**`history/passive.json`** — the passive join's daily token-per-percent
history and current class split, produced by `tracker/passive.py`.

**Published JSON** — `tracker.publish` assembles the above into the file the
site serves at `/data/claude-usage.json`, including per-model rates, weekly
windows per plan, and change history.

## Caveats

- The meter only reports whole percent, so every rate is quantised to that
  resolution — a single probe's tick-to-tick timing carries real noise, which
  is why probes average over several ticks and rotation runs get compared
  against a rolling median rather than trusted individually.
- Probing runs on two dedicated Max 20x accounts (Jono Work first, Dave
  second, skipping either while it has a live `claude` session on the host),
  not Jonathan's own account.
- Weekly windows for the live plan come from one passive account's real
  usage, not from a controlled experiment.
- Current Pro and Max 5x weekly figures remain unavailable until they have
  contemporaneous evidence. Historical Max 5x evidence is kept with its
  dates and is not copied into Pro or forced to meet the Max 20x series.
- The per-class weights in `data/prices.json` (how much harder the meter
  charges output vs. cache-write vs. input tokens) are assumptions transferred
  to the reference mix. Their provenance is published; a zero cache-read
  weight and the one-hour cache-write meter weight are premises rather than
  independently measured per-model caps.

## Checking the published figures

The headline change, the fall in five-hour windows per week (`last_change.percent`, scope
`weekly`), is the five-hour meter's movement divided by the seven-day meter's movement over
the same windows, before and after the change. The event's `windows_per_week_ratio`
(`tracker/credits.py` `windows_per_week_ratio_note`) measures it on each account against
itself, only for accounts with readings on both sides, and combines them with an interval.
No token count and no per-model rate enters it, so a change to the per-model rates cannot
move it. The tokens-per-week figure published beside it
(`last_change.tokens_per_week_change`) is different: it compounds that ratio with the
five-hour window's change in credits, which prices tokens per model.

To rebuild the published JSON from the files committed here and compare it field by field
with the live one:

```
python3 tools/verify_publish.py
```

It runs the same publisher command as the hourly cron (`bin/daily.sh`), at the live file's
own `generated_at`, and prints any difference (exit 1) or says the two are identical (exit 0).
Only `generated_at` itself is ignored. The rule for changing what counts as evidence is in
`docs/adr/0001-acceptance-rules-for-published-figures.md`; an out-of-sample test of the
per-model rates is in `docs/findings-2026-09-23-holdout.md`.

## Running it

Tests (one test reads a usage log that exists only on the machine that
records it, and is skipped elsewhere):

```
python3 -m unittest discover -s tests
```

Dry run of the rotation (which model is next, and what expectation it would
be given — no network calls, no spend):

```
python3 -m tracker.rotate plan
```

Running a probe directly spends real subscription allowance on whichever
account it uses — it is not a no-op. See `tracker/probe.py`'s `argparse`
setup for the full flag list; the required ones are `--model` and
`--expect-usd-per-pct` (the assumed meter dollars per 1%, which sizes the
prompt on the model's own prices and, through it, the bursts; an output run
takes `--expect-tokens-per-pct` instead, since its reply size is fixed).
Without `--out history/probes.jsonl` the row lands in a separate
`probes.jsonl` in the working directory and never enters the rotation or the
published page:

```
python3 -m tracker.probe --model claude-sonnet-5 --expect-usd-per-pct 0.96 --out history/probes.jsonl
```

Publishing the public JSON from current history. Publishing also recomputes
the output class weight and writes it back to the prices file, so point
`--prices` at a copy unless you mean to change `data/prices.json`:

```
cp data/prices.json /tmp/prices.json
python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json --prices /tmp/prices.json --out /tmp/claude-usage.json
```

In production this all runs unattended on a schedule (probes, the weekly
output-weight run, the daily publish and push to the site). Layout and
cron lines: `docs/deploy-gs.md`.

## Contributing

Bug reports and ideas go through
[GitHub Issues](https://github.com/jonathanavis96/claude-usage-tracker/issues).
One planned direction is letting other subscribers contribute their own
meter samples — see `docs/superpowers/specs/2026-09-06-contributed-meter-samples.md`
for the (not yet built) design.

## License

MIT — see `LICENSE`.
