# Contributed meter samples

Status: plan only, not scheduled. Written 2026-09-06.

## Why

The tracker measures one Max 20x account. The weekly limit is a per-plan
number, and Pro currently assumes the Max 5x ratio. Other people's meters can
give a measured figure for every plan, plus a much larger sample of how the
five-hour window behaves under real mixed-model use, without spending any of
their allowance.

## What a contributor sends

A small script on their machine, cron every 30 minutes, reads their own usage
meter with their existing Claude Code login and posts one sample:

| Field | Source |
|---|---|
| contributor id | random UUID generated once at install, stored next to the script |
| plan | asked once at install (`pro`, `max5`, `max20`), sanity-checked later |
| ts | script clock, UTC |
| five_hour percent and resets_at | usage endpoint |
| seven_day percent and resets_at | usage endpoint |
| model token deltas since the last sample | optional; summed per model from the usage fields in their transcripts, same code as `tracker/turns.py` |

No prompt content, no file paths, no account identifiers. Token deltas are
counts only. The script prints exactly what it will send before the first run.

## Server

One Cloudflare Pages function on alldonesites, beside the notify functions,
writing rows to D1. Rules at the edge: percent within 0 to 100, reset times
within seven days of now, at most four samples per contributor per hour,
body under 2 KB. Rows older than 180 days are pruned.

## Aggregation

The daily job pulls rows per contributor and runs the pairing already in
`tracker/weekly.py`: consecutive samples inside the same five-hour and weekly
windows, sum both consumptions, ratio per week. Then per plan:

- weekly windows = median across contributors of their per-week ratio, weighted
  by weeks contributed, dropping contributors more than 30% from the plan median.
- A plan gets a measured figure once two contributors have one complete week
  each. Until then it keeps the current source (probe, passive, or assumed).
- With token deltas, the same pairing gives tokens per five-hour percent per
  model under real use, a cross-check on the probe's dollar invariant and the
  passive split.

Change detection runs on the aggregated per-plan weekly series with the same
15% threshold and gets a `contributed` source tag on the event.

## What does not change

The probe stays the source of tokens per percent. Contributed data cannot
control for model mix or cache traffic the way a probe does, so it corroborates
rather than replaces.

## Open questions

- Does the usage endpoint expose the plan? If it does, drop the install question.
- Does contributing need a page on the site with an install one-liner and a
  privacy note, or is a README in the tracker repo enough?

## Size

Two sessions: endpoint plus storage plus edge rules with tests, then the client
script plus aggregation plus the site note.
