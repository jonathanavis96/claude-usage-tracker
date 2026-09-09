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

## Revision 2026-09-09: two ways to contribute

Decided with Jonathan on 2026-09-09. People do not install random things,
and they need to be able to check for themselves what leaves their machine.
So there are two paths, both driven by the reader's own Claude Code, both
using one public script in this repo (`contrib/sample.py`, stdlib only) that
prints exactly what it would send before sending anything.

### What the script computes

From the reader's own machine, nothing else:

| Field | Source |
|---|---|
| plan | usage endpoint if it exposes it, else asked once |
| five_hour, seven_day, both resets_at | `https://api.anthropic.com/api/oauth/usage` with the reader's existing Claude Code login (same call the tracker makes in `tracker/usage_api.py`) |
| tokens by model and class since the current five-hour reset | summed from `~/.claude/projects/**/*.jsonl` usage fields with the pairing already in `tracker/turns.py`; counts only |
| tokens by model and class since the current seven-day reset | same |
| contributor id | random UUID, created on first run, stored beside the script |
| client version | script version string |

Never sent: prompt text, file paths, project names, session ids, account
ids, email. The script has `--print` (default on first run) that shows the
exact JSON body and asks before posting. The code is public in this repo so
the reader, or their Claude, can audit it.

Tokens since window start over the current meter percent gives tokens per 1%
under real mixed use for that plan and model mix in one sample. That is the
one-off measurement. Two or more samples from the same contributor add the
weekly ratio pairing from the original plan above.

### Path 1: one-off contribution

The page carries a prompt in a copy box:

> Run `python3 -c "$(curl -fsSL https://raw.githubusercontent.com/jonathanavis96/claude-usage-tracker/main/contrib/sample.py)" --print`, show me the JSON it prints, explain each field, and post it only if I say yes.

The reader pastes it into their own Claude Code. Claude fetches the script,
runs it in print mode, and the reader sees the payload before anything is
sent. The script prints two things: the readable JSON with a one-line
explanation per field, and the same body as one compact line, base64 of the
JSON, prefixed `CUT1:`. Two ways to send, the reader picks:

- Type `approve` (or yes) and Claude runs the script again with `--yes`,
  which posts it.
- Copy the compact line into the paste box on the page. The page decodes it,
  shows the fields again, and posts it. Nothing on their machine talks to the
  site at all in this case.

Both routes carry the same body and hit the same endpoint. The response
carries a personal link (`/claude-usage-tracker/me/<contributor id>`) that
draws that contributor's own tokens per 1% against the fleet median, which
is the reason to keep contributing. No install, no cron, nothing left
behind except the contributor id file.

### Path 2: continuous

Same script, installed on a 30-minute cron (or launchd on macOS). The page's
second copy box is a prompt that asks Claude to clone the repo, read
`contrib/README.md`, run the script once in print mode, wait for `approve`,
and only then install the schedule. Uninstall is one documented line. A
continuous contributor's personal link becomes a line rather than a point,
so "did my meter change, or everyone's?" is answerable from their own page.

### Why not measure with prompts

We cannot spend a contributor's allowance, so contributed rows never measure
tokens per 1% the way the probe does. They measure tokens per 1% under that
person's real mix, which is the number readers actually want anyway, and the
probe stays the controlled reference. Aggregation groups by plan and reports
the median and spread.

### Server additions to the original plan

- Accept the new token-since-reset fields; rows still under 2 KB.
- Rate limit per contributor id and per IP; Turnstile is not needed because
  the poster is a script, not a browser form.
- A `contributed` block in the published JSON: per plan, contributors,
  samples, median windows per week, median tokens per 1% by model, spread.

### Open questions

- Does the usage endpoint expose the plan? Check with one call from the
  tracker before the install question is written.
- macOS launchd wording for the continuous path.
