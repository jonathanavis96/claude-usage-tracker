# The credits model: Anthropic's internal subscription accounting

Source: [suspiciously precise floats, or, how I got Claude's real limits](https://she-llac.com/claude-limits),
fetched 2026-09-20. Everything in the "What the article establishes" section is quoted or
derived from that page. Everything in "What our data says" is computed by
`tools/credits_model.py` against `history/gs-passive.json`.

## Why this matters here

The tracker has always priced work in **US dollars at API list price** and then fitted
per-class weights to correct the gap between list price and meter movement. The article
shows the meter does not work in dollars at all. It works in an internal unit, and the
conversion from tokens to that unit is a published-shaped formula with small integer
rates. Pricing in that unit instead of dollars fits our own measurements better, and it
removes a fitted parameter we got wrong.

## What the article establishes

### The unit

> "They're the unit used internally to keep track of your plan usage. 'Credits' is my
> arbitrary name for it, these values don't appear directly in any API field."

```
credits_used = ceil(input_tokens × input_rate + output_tokens × output_rate)
```

| Model  | Input credits/token | Output credits/token |
|--------|---------------------|----------------------|
| Haiku  | 2/15                | 10/15                |
| Sonnet | 6/15                | 30/15                |
| Opus   | 10/15               | 50/15                |

Output is 5x input for every model in the table.

### Cache handling — the part that corrects us

> "**Cache reads. They're entirely free.** ... The API charges 10% for every read;
> subscriptions charge nothing."

> "Cache writes are also discounted, they cost 1.25x/2x the input price in the API, while
> **on the plan they're charged the regular input price**."

So on a subscription: cache reads contribute nothing, and cache writes contribute at the
plain input rate rather than the API's 1.25x premium.

### Published limits

| Tier    | Credits / 5h | Credits / week | Windows per week |
|---------|--------------|----------------|------------------|
| Pro     | 550,000      | 5,000,000      | 9.09             |
| Max 5x  | 3,300,000    | 41,666,700     | 12.63            |
| Max 20x | 11,000,000   | 83,333,300     | 7.58             |

Max 5x / Max 20x windows-per-week ratio = **1.667**.

### How the numbers were recovered

Not from the `/usage` endpoint, which is rounded:

> "a `/usage` endpoint returning a tiny JSON snippet with the numbers rounded to the
> nearest percentage point."

From unrounded doubles in the streaming generation responses:

> "On a Max 5x account, the SSE responses from the generation endpoint had usage values as
> unrounded doubles: `0.16327272727272726`."

Method, in four steps:

1. **Bucket.** A double represents an interval of rationals about 1e-17 wide. The true
   fraction `used/limit` lies inside it.
2. **Stern-Brocot search.** Binary search over all positive fractions for the
   simplest one inside that interval. `0.16327272727272726` resolves to `449/2750`.
3. **LCM.** Each sample yields a denominator that must divide the real limit, since the
   recovered fraction is in lowest terms. Take the LCM across samples; it can only grow
   toward the true limit, never overshoot it. `449/2750` and `11401/75000` both scale to
   a denominator of 3,300,000.
4. **Manual inference** for the per-model rate table.

We have independently confirmed step 0: our own live call to
`https://api.anthropic.com/api/oauth/usage` returns whole-number `utilization`, and all
1,829 stored samples in the raw meter logs are integers. The precision is not in the
endpoint the tracker reads.

## What our data says

Fitted against 131 stretches in `history/gs-passive.json` with per-model token counts and
an observed meter movement. Fit statistic is the coefficient of variation of
credits-per-percent: if the model is right, that ratio is a constant.

| Model                                          | CV        |
|------------------------------------------------|-----------|
| Tracker's dollar model (cache_read weight 0.0) | 0.360     |
| Credits model, cache reads free                | 0.333     |
| Credits model, fitted Fable rate               | **0.295** |

### Fable

Fable is not in the article's table. Fitted here, with every other rate fixed:

- input rate **25/15**, i.e. **2.5x Opus**. Bootstrap 80% interval [25/15, 30/15].
- output/input ratio **3**, not the 5 that Haiku, Sonnet and Opus all use. Bootstrap 80%
  interval [3, 3].

The output ratio is the surprise. If it is real, Fable output is comparatively cheaper
against the meter than any other model's output.

### Cache reads are approximately, not exactly, free

Scanning the cache-read weight inside the credits model:

```
w=0       CV=0.3334
w=0.015   CV=0.2953   <- best
w=0.155   CV=0.3923
w=1.0     CV=0.4469
```

Bootstrap (200 resamples) puts it at 0.015, 80% interval [0.015, 0.02], and never selects
zero. So cache reads appear to count at roughly **1.5% of the input rate** rather than at
nothing. That residual is small enough to be an artefact of whole-percent quantisation or
of tokens the transcripts did not capture, and we do not treat it as established.

### This retracts our own earlier finding

`docs/findings-2026-09-20-cache-read-weight.md` reported a fitted cache_read weight of
**0.155** and recommended changing `prices.json`. That number is an artefact. It was fitted
inside the dollar model, which prices cache writes at the API's 1.25x input premium. The
article says the plan charges cache writes at 1x. Over-pricing cache writes by 1.25x forces
the fit to load compensating weight onto cache reads. Correct the write price and the read
weight falls by a factor of ten.

The tracker's original 0.0 was closer to right than our correction to it.

## What does not reconcile

Our stretches imply a 5-hour allowance of 23.4M credits before 14 September and 26.1M
after, against the article's 11,000,000 for Max 20x — a factor of 2.1 to 2.4. Fable
dominates our token volume and is the one rate not taken from the article, so it is the
first suspect, but a rate error large enough to close that gap is outside its bootstrap
interval. Unexplained.

Separately, in credits terms the implied 5-hour allowance **rose 11.2%** after 14
September (234,440 to 260,620 credits per percent, n=89 and n=42). The published page
reports a fall. These are not contradictory — the page measures five-hour windows per
week, a ratio of two meters that never touches token counts — but a five-hour pot that
grew while the weekly pot did not is a different explanation of the September change than
the one we have published, and it is not yet tested.

## What is unchanged

Windows-per-week, the page's headline measure, is a ratio of the 5-hour and 7-day meters
read at the same moment. It contains no token counts and no prices. Nothing in this
document moves it. Our measured Max 5x / Max 20x ratio of 1.665 now has a third
independent confirmation: this article's published limits give 1.667.
