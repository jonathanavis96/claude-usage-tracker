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
with a burst of concurrent prompts sized to the expected span so the reading
doesn't get stuck waiting on a single slow prompt. See `tracker/probe.py` for
the mechanics (burst sizing, alignment, reset handling).

**The dollar invariant.** Anthropic's meter tracks API list value, not raw
token count. That means one model's probe result, combined with
`data/prices.json`, is enough to derive every other model's rate — a probe
on Sonnet tells you Opus and Fable too, once each token class's price and
"class weight" (how much harder the meter charges that class relative to
list price) are applied. Only Sonnet 5 is probed on a schedule; the rest are
derived. Detail and the measurements behind the class weights: `docs/spike-2026-09.md`.

**Passive split.** The probe's own traffic is cache-write heavy, which isn't
what a real coding session looks like. A separate passive join, run against
Jonathan's own daily usage, supplies the real mix of input/output/cache-read/
cache-write tokens, and that mix is what converts a probe's dollar figure
into a "tokens per window" number that means something for actual use.

**Weekly windows.** How many 5-hour windows the 7-day limit actually holds is
measured, not assumed to be 28. It comes from two independent sources: the
passive log (pairing 5-hour and 7-day meter movement within the same window)
and each probe row's own before/after reads, both bucketed by week. See the
`weekly_windows` vocabulary in `CONTEXT.md` for how the two series are kept
separate per plan.

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
- Probing runs on two dedicated Max 20x accounts (Dave, primary; Jono Work,
  fallback), not Jonathan's own account.
- Weekly windows for the live plan come from one passive account's real
  usage, not from a controlled experiment.
- Pro and Max 5x figures are scaled from Max 20x using Anthropic's published
  plan ratios, not measured directly; see `docs/spike-2026-09.md` for how far
  the passive data could and couldn't pin down that ratio.
- The per-class weights in `data/prices.json` (how much harder the meter
  charges output vs. cache-write vs. input tokens) come from a single round
  of calibration; input and cache-read weights in particular are unmeasured
  and assumed equal to cache-write.

## Running it

Tests:

```
python3 -m pytest
```

Dry run of the rotation (which model is next, and what expectation it would
be given — no network calls, no spend):

```
python3 -m tracker.rotate plan
```

Running a probe directly spends real subscription allowance on whichever
account it uses — it is not a no-op. See `tracker/probe.py`'s `argparse`
setup for the full flag list; the required ones are `--model` and
`--expect-tokens-per-pct` (the assumed tokens-per-1% rate, used to size the
prompts and bursts):

```
python3 -m tracker.probe --model claude-sonnet-5 --expect-tokens-per-pct 500000
```

Publishing the public JSON from current history:

```
python3 -m tracker.publish --probes history/probes.jsonl --passive history/passive.json --out website/public/data/claude-usage.json
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
