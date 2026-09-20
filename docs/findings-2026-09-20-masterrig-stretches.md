# masterrig's stretches, and credits per 1% across all three accounts

Issue #52. Written 2026-09-20 against `history/gs-passive.json` generated the same day and
`history/masterrig-passive.json` generated on masterrig at 02:41Z. Read-only analysis: no
probe, no burst, nothing that spends allowance.

## What was added

`tracker.gs_passive --masterrig` joins Jonathan's own account in the same record shape the
gs accounts get, and `bin/passive.sh` writes it to `history/masterrig-passive.json` beside
the `history/passive.json` the publisher reads. Both paths run; neither replaces the other.

The point is retention. `tracker/passive.py` keeps one number per UTC day —
`history/passive.json` → `history[<date>].tokens_per_pct` — and throws the per-stretch,
per-model token counts away. masterrig has the tracker's longest meter record, 10,836
samples back to 2026-06-13 against gs's 2026-09-05, and that detail is now kept: 237
stretches, each with its own model breakdown, turns, bounds, capture verdict and reset
provenance.

`masterrig_account` is deliberately not part of `gs_accounts`. Its meter counts the whole
account — claude.ai in a browser, the phone, every other machine — while only that host's
`~/.claude/projects` is read, so a stretch's percent can include movement its tokens cannot
explain. The meter meta says so in `note`, every stretch carries its own `capture`, and the
capture check never withholds a stretch from the file.

## Two numbers worth knowing about that file

**155 of 237 stretches are `unpriced` under the dollar model.** `data/prices.json` covers
Sonnet 5, Opus 5 and Fable 5.1; masterrig's history also holds `claude-opus-4-7` (172M
tokens), `claude-opus-4-8` (55M), `claude-sonnet-4-6` (16M) and `claude-haiku-4-5` (8.5M).
Any stretch containing one of those is unpriced whole, correctly — audit finding 9 — so the
dollar path reads 55 stretches of 237. The credits model prices all four families and reads
210.

**Every masterrig stretch has `capture: null`, and all four runs are `unknown`.** The
capture check bootstraps its reference from the median of the account's first five priced
stretches. masterrig's earliest stretches are from 2026-07-16, by which time the transcripts
for them had been cleaned up, so they hold no tokens, their rate is 0, and the bootstrap
median is 0. A reference of zero has nothing to divide by, so no stretch gets a capture, and
the run classifier had no median to take — `statistics.StatisticsError: no median for empty
data`, which is the crash this branch fixes. The run is now `unknown` and the stretches are
written, but **the capture column on masterrig is not yet usable**, which is the one thing
the phantom-usage note in the meta is supposed to point a reader at.

A one-line change to `tracker/capture.py` would fix it: bootstrap the reference from the
batch's non-zero rates only, since a stretch whose transcripts hold nothing says nothing
about the account's level. Measured 2026-09-20 — with `--until 2026-09-19T00:00:00+00:00`
pinning the data, the gs report is byte-identical with and without it, and 82 masterrig
stretches gain a capture. It is left out of this branch because it changes the shared check
that the published gs series depends on, and that deserves its own change rather than a
ride-along.

## Credits per 1%: `tools/credits_report.py`

Rates from `docs/reference-2026-09-20-shellac-credits-model.md` on branch
`step-vs-trend-finding` (which is not on `main`, and was not pushed to origin as of
2026-09-20 — it exists only in masterrig's checkout), itself from
https://she-llac.com/claude-limits. Haiku 2/15 credits per input token and 10/15 per output
token, Sonnet 6/15 and 30/15, Opus of any version 10/15 and 50/15; cache writes at the plain
input rate rather than the API's 1.25x premium, cache reads free. **That table is a
reference, not a source of truth.** It is one person's reconstruction from unrounded doubles
in streaming responses, and it does not reconcile with our own stretches: they imply a
five-hour allowance two to two-and-a-half times the article's published 11,000,000 credits
for Max 20x.

Two rates are ours and are marked provisional wherever they print: Fable's, absent from the
article, at 25/15 with an output ratio of 3 against the 5 every published model uses; and
the cache-read weight of 0.015, the residual the reference doc measured where the article
says zero.

At `--cache-read-weight 0`, which reads the article literally:

```
account    era      dominant>90%     n   median cpp          p25          p75
dave       20x-cut     haiku-4-5     1       26,510       26,510       26,510
dave       20x-cut         mixed    19      150,757      127,562      161,937
dave       20x-cut        opus-5     3      171,794      166,503      172,147
jwork      20x         fable-5-1     5      160,166      132,837      161,744
jwork      20x             mixed    61      156,377      128,266      178,648
jwork      20x            opus-5    23      180,818      106,612      196,989
jwork      20x-cut     fable-5-1     3      124,514      117,893      137,141
jwork      20x-cut         mixed    34      166,234      136,327      205,008
jwork      20x-cut        opus-5     1      281,576      281,576      281,576
masterrig  20x         fable-5-1    20       24,018        6,945      139,818
masterrig  20x             mixed   145      129,149       68,985      162,302
masterrig  20x          opus-4-7     1       11,129       11,129       11,129
masterrig  20x            opus-5    11       24,546        7,012       98,590
masterrig  20x-cut     fable-5-1     1      169,513      169,513      169,513
masterrig  20x-cut         mixed    32      144,952      131,583      156,335
```

There are no `5x` rows in any account: the plan moved on 2026-08-14 and no transcript older
than that survives on any of the three hosts. On masterrig that is `cleanupPeriodDays`,
being fixed separately.

### What agrees and what does not

The gs numbers reproduce the 2026-09-20 scratch run exactly where the data has not moved
since: jwork 20x opus-5 n=23 at 180,818, dave 20x-cut opus-5 n=3 at 171,794, dave 20x-cut
median 150,757. The two rows whose counts grew (dave 20x-cut mixed 19 against 17, jwork
20x-cut mixed 34 against 33) grew because `history/gs-passive.json` is regenerated through
the day.

masterrig's opus-5 row does not reproduce: 11 stretches at 24,546 here against 20 at 108,689
in the scratch run. Grouping on the dominant share of **raw tokens** instead of credits gives
17 at 111,178, close to the scratch figure, which suggests the scratch script grouped on
tokens. This tool groups on credits, as the issue asks. The two bases differ on masterrig
and barely differ on gs because masterrig's stretches are the ones where nearly all the
tokens are free cache reads: a stretch can be 95% Opus by token count and mostly Sonnet by
credits.

### The gap between masterrig and gs is the phantom, and it is large

masterrig's pure-Opus median is 12,401 credits per 1% against jwork's 236,108 — a factor of
19, far more than any plan difference. The stretches show why:

| stretch end | delta_pct | Opus output tokens | Opus cache reads | credits/1% |
|---|---|---|---|---|
| 2026-08-24T18:01 | 10.0 | 236,380 | 77,685,170 | 169,867 |
| 2026-09-02T18:12 | 13.0 | 5,909 | 1,272,598 | 1,917 |
| 2026-09-02T23:26 | 12.0 | 20,317 | 3,529,792 | 6,972 |

Thirteen percent of a Max 20x five-hour window for 5,909 output tokens is not a measurement
of anything; it is the meter moving on work that host never saw. The low masterrig rows
cluster on 2026-09-02/03 and in ISO week 36, where the weekly median falls to 48,315 against
134,084–170,184 either side.

So masterrig's stretches are worth keeping as a token record and are **not** usable as a
rate without a working capture filter. That is the same conclusion `tracker/capture.py`'s
own docstring reached about this account in September, now with the per-stretch evidence
retained instead of discarded.

### Fable's input rate, solved rather than assumed

For each stretch where Fable holds over half the raw tokens, take the account's pure-Opus
median as `W` — the credits a percent of that account's meter buys, priced entirely at rates
the article publishes — subtract the credits of every non-Fable model, and what is left is
Fable's:

```
fable_input = (delta_pct x W - known_credits) / (fable_in + ratio x fable_out)
```

At the default cache-read weight of 0.015:

| account | n | median rate | as a multiple of Opus input |
|---|---|---|---|
| dave | 2 | 1.5634 | 2.35x |
| jwork | 26 | 1.8185 | 2.73x |
| masterrig | 82 | -0.0599 | -0.09x |

dave and jwork bracket the reference doc's fitted 2.5x from either side, on 28 stretches of
arithmetic with no fit in it. masterrig's is negative, which is what a `W` destroyed by
phantom usage produces: the known non-Fable credits already exceed `delta_pct x W`, so the
remainder is below zero. It is printed rather than suppressed, because a negative rate is
the clearest statement available that masterrig cannot anchor this calculation.

At `--cache-read-weight 0` the same solve gives dave 3.17x and jwork 4.13x, so the answer is
sensitive to a rate the article says is zero and our own data says is about 0.015. Neither
value is settled, and nothing in the tracker's published output depends on either.
