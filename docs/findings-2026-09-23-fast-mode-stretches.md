# Opus fast-mode requests taken out of the passive stretches, 2026-09-23

> **Correction, 2026-09-23.** This document takes the fast sessions' tokens out of the
> passive stretches on the grounds that they were Opus fast mode, billed to usage credits
> and never metered. That explanation is withdrawn. Jonathan has never used fast mode.
> masterrig's Claude Code settings carry no fast-mode setting, and the account is not
> eligible for usage credits (`overageCreditGrantCache` reads `eligible: false, available:
> false`), which fast mode needs ("Until they're on, /fast reports 'Fast mode requires usage
> credits'", code.claude.com/docs/en/fast-mode). And this document's own before and after
> table ("Stretches that changed", below) shows the meter counted those sessions' tokens, at
> least in part: taking them out turned some surplus stretches accepted, but
> turned others unaccounted (19 Sep 22:20-23:36 capture 1.99 to 0.68; 21 Sep 10:54-13:26
> 1.62 to 0.43; 21 Sep 13:26-19:26 1.63 to 0.25; 20 Sep 00:16-01:07 1.12 to 0.59).
>
> What was measured stands: some Opus sessions run about 2.5 times faster than others on the
> same model, account and settings, with nothing between, mostly on a1 and a little on a2.
> What causes it is not known. The code now calls them fast sessions: a session whose
> running median speed is at least FAST_FACTOR (1.6) times the model's median. Their tokens
> are back in every passive stretch, on gs and masterrig (`tools/fast_session_restore.py`
> undid the one-off correction below), and each stretch records them as
> `fast_session_tokens`, for diagnosis only. The rest of this document is kept as written.

Anthropic's fast-mode page (code.claude.com/docs/en/fast-mode) says "fast mode is available via
usage credits only and not included in the subscription rate limits", and "Fast mode usage draws
directly from usage credits, even if you have remaining usage on your plan." A fast-mode request's
tokens are written to the transcript, but they never reach the subscription meter. Every passive
stretch that counted them read more tokens per 1% than the account actually gets.
docs/findings-2026-09-23-model-speed.md showed that Claude Code writes `"speed": "standard"` on
fast-mode lines too, so the only way to tell them apart is by timing.

No traffic was sent. The masterrig meter logs and the transcript extract were read on gs and not
committed.

## What changed

- **Classification** (`tracker/speed.py` `fast_mode`) uses PR #87's rule unchanged: an Opus
  request counts as fast mode when its session's running median speed, over `FAST_WINDOW` = 9
  requests, is at least `FAST_FACTOR` = 1.6 times the model's median.

  That rule only judges the requests the speed figures keep: at least 300 output tokens, taking 1
  to 900 s. A fast session's short tool-call requests are billed to credits in the same way, so
  `fast_mode(..., every=True)` applies the same rule to them as well. It reads the running median
  of the 9 kept requests nearest the request in its session.

  On masterrig's extract this matters. The rule over kept requests flags 2,795 requests. With
  `every=True` it flags 5,144, which is 95.5% of the Opus requests in the 40 sessions that have
  any fast request. In the stretches, that is 1,802 fast turns without `every` and 3,101 with it.

  The speed figures still call `fast_mode` without `every`, so they are unchanged.
- **The stretch join** (`tracker/join.py` `build_stretches`, `Stretch.add`) takes the set of
  fast-mode message ids.
  - A fast turn's tokens go to `fast_mode_tokens` instead of `tokens` (by model, then class,
    in `tokens`' shape). The turn is counted in `fast_mode_turns` instead of `turns`.
  - A fast turn adds nothing to `usd`.
  - A stretch keeps its meter movement, so it stays a valid stretch whatever its fast share.
- **Where the ids come from.** `tracker/gs_passive.py` `report` classifies each account's own
  transcripts (`fast_mode_ids`) and passes the ids to the join. This covers gs's hourly run and
  masterrig's `bin/passive.sh` (`tracker.gs_passive --masterrig`). From masterrig's next daily
  run, its stretch file is built this way from its own transcripts.
- **Refactor.** `report`'s second half is now `summarise`: the capture check and every figure
  read off it. The one-off correction can then re-judge the stretches with the same code.
- **masterrig's committed stretches** were corrected once, by `tools/fast_mode_correction.py`:

  ```
  python3 tools/fast_mode_correction.py --extract ~/wf-data/masterrig-timing.jsonl.gz \
      --meter-home <scratch>/mr
  ```

  The inputs are the text-free extract (4,159 transcripts, 97,479 timed requests) and copies of
  masterrig's two meter logs.

  1. Before changing anything, the tool rebuilds the committed file from its own records and
     requires an identical result. This proves the records, prices and probe rows are the ones
     the file was written from. Two `usd` figures differ in the fourth decimal, because
     per-turn and per-model sums round differently, and the comparison allows exactly that.
  2. It joins the fast turns against the meter the way `build_stretches` does. A fast turn in a
     sample pair that straddles a gap or a reset counts for nothing, as it did in the original
     build. Every stretch this rebuild closes must match a committed stretch on start and end.
  3. It subtracts the fast turns' tokens and revalues each stretch.
  4. It re-runs `summarise`, which includes the capture check.

  It writes `fast_mode_correction` at the top of the file. A file that already has that key is
  left alone, so the tool is idempotent.
- **gs's stretches** (`history/gs-passive.json`) were rebuilt from gs's transcripts with the new
  code. The "before" below is the old code run on the same transcripts at the same time (17:2x
  UTC), and both runs closed the same stretches. Between the two runs, one meter reading arrived
  for a3 and one for a4. That is the only reason `account_feeds.a3/a4.feed_at` and
  `meter_read_at` differ.
- **Refit and rebuild.** `tools/model_rates.py` was refit on both sets of stretches. The
  publisher was then built with `rebuild_public_json` at the same `now` for before and after.
  The committed `history/model-rates.json` is the "after" refit.

## Stretches that changed

| account | stretches | with fast-mode tokens | fast-mode tokens | fast turns | capture status before | after |
|---|---|---|---|---|---|---|
| a1 | 254 | 19 | 466,588,737 (1,554,172 output) of 9,087,945,579 | 3,101 | accepted 124, unaccounted 97, surplus 33 | accepted 126, unaccounted 103, surplus 25 |
| a2 | 141 | 4 | 6,592,749 (50,923 output) of 7,872,347,506 | 30 | accepted 83, unaccounted 50, surplus 8 | accepted 84, unaccounted 51, surplus 6 |
| a3 | 52 | 0 | 0 | 0 | accepted 32, unaccounted 16, surplus 4 | unchanged |
| a4 | 3 | 0 | 0 | 0 | unjudged 3 | unchanged |

The published `status` did not change on any account: a1 has 211 accepted and 43 unaccounted,
a2 has 134 accepted and 7 unaccounted, a3 has 52 accepted and a4 has 3 accepted. No change was
corroborated either before or after.

Of masterrig's 5,144 fast requests, 3,101 fall inside a stretch. The rest fall between 12 and 18
September, where masterrig has one stretch (12 Sep 20:51 to 18 Sep 00:52 UTC) and its meter log
has gaps. Pairs across those gaps count for nothing.

Every stretch that has fast-mode tokens or whose capture status changed is listed below. Times
are UTC, rates are meter dollars per 1%, and capture is the rate over the account's reference. A
stretch with no fast-mode tokens can still change status, because its reference is the median of
the account's recent accepted stretches, and those moved.

| account | stretch (UTC) | fast-mode tokens | fast turns | $/1% | capture | capture status |
|---|---|---|---|---|---|---|
| a1 | 10 Sep 21:19 – 11 Sep 00:36 | 8.0M of 47.7M (17%) | 67 | 1.8133 → 1.5618 | 1.0985 → 0.9462 | accepted → accepted |
| a1 | 18 Sep 01:43 – 01:53 | 0.6M of 59.8M (1%) | 8 | 1.6053 → 1.5265 | 1.0723 → 1.0197 | accepted → accepted |
| a1 | 18 Sep 01:53 – 02:03 | 9.3M of 87.8M (11%) | 73 | 1.6424 → 1.2768 | 1.0971 → 0.8529 | accepted → accepted |
| a1 | 18 Sep 02:03 – 02:13 | 1.1M of 52.1M (2%) | 6 | 1.3761 → 1.3611 | 0.8953 → 0.9524 | accepted → accepted |
| a1 | 18 Sep 02:13 – 02:28 | 7.3M of 84.7M (9%) | 63 | 1.5025 → 1.3072 | 0.9775 → 0.9156 | accepted → accepted |
| a1 | 18 Sep 02:30 – 02:38 | 2.2M of 45.1M (5%) | 19 | 1.4462 → 1.3778 | 0.9734 → 1.0030 | accepted → accepted |
| a1 | 18 Sep 02:38 – 02:48 | 5.9M of 50.9M (12%) | 36 | 1.9328 → 1.7140 | 1.3260 → 1.2516 | surplus → accepted |
| a1 | 18 Sep 02:48 – 03:12 | 10.3M of 71.8M (14%) | 77 | 2.0758 → 1.7669 | 1.4242 → 1.2902 | surplus → surplus |
| a1 | 18 Sep 03:12 – 03:27 | 3.6M of 86.1M (4%) | 33 | 1.1906 → 1.0654 | 0.8168 → 0.7780 | accepted → unaccounted |
| a1 | 18 Sep 14:17 – 14:48 | 0 | 0 | 1.0662 | 0.7940 → 0.7614 | accepted → unaccounted |
| a1 | 19 Sep 11:21 – 17:31 | 0 | 0 | 1.8736 | 1.5603 → 1.4121 | surplus → accepted |
| a1 | 19 Sep 17:31 – 18:52 | 0 | 0 | 1.5279 | 1.2725 → 1.1516 | surplus → accepted |
| a1 | 19 Sep 18:52 – 22:20 | 27.5M of 47.9M (57%) | 182 | 1.9778 → 1.3149 | 1.6471 → 0.9344 | surplus → accepted |
| a1 | 19 Sep 22:20 – 23:36 | 54.2M of 90.1M (60%) | 260 | 2.3914 → 0.9510 | 1.9916 → 0.6758 | surplus → unaccounted |
| a1 | 19 Sep 23:36 – 20 Sep 00:16 | 25.2M of 71.2M (35%) | 164 | 2.0296 → 1.6609 | 1.6903 → 1.1802 | surplus → accepted |
| a1 | 20 Sep 00:16 – 01:07 | 34.7M of 45.3M (77%) | 198 | 1.3499 → 0.8400 | 1.1242 → 0.5945 | accepted → unaccounted |
| a1 | 20 Sep 01:07 – 01:47 | 22.1M of 45.7M (48%) | 122 | 1.4522 → 1.0916 | 1.1671 → 0.7726 | accepted → accepted |
| a1 | 20 Sep 01:47 – 03:18 | 9.5M of 33.0M (29%) | 44 | 1.4389 → 1.3223 | 1.0500 → 0.9358 | accepted → accepted |
| a1 | 20 Sep 21:08 – 21 Sep 10:54 | 7.9M of 60.2M (13%) | 76 | 1.8313 → 1.6068 | 1.3186 → 1.2009 | accepted → accepted |
| a1 | 21 Sep 10:54 – 13:26 | 89.2M of 152.3M (59%) | 578 | 2.2871 → 0.5884 | 1.6188 → 0.4294 | surplus → unaccounted |
| a1 | 21 Sep 13:26 – 19:26 | 99.5M of 133.0M (75%) | 768 | 2.3072 → 0.3424 | 1.6330 → 0.2498 | surplus → unaccounted |
| a1 | 21 Sep 19:26 – 22 Sep 13:37 | 48.6M of 97.0M (50%) | 327 | 2.9101 → 1.6275 | 2.0597 → 1.1877 | surplus → accepted |
| a1 | 23 Sep 08:26 – 09:07 | 0 | 0 | 1.7852 | 1.2635 → 1.3028 | accepted → surplus |
| a2 | 16 Sep 16:45 – 18:24 | 4.0M of 92.3M (4%) | 10 | 2.8511 → 2.7511 | 1.8558 → 1.7907 | surplus → surplus |
| a2 | 16 Sep 18:24 – 18:46 | 1.5M of 71.4M (2%) | 8 | 1.9301 → 1.8894 | 1.2563 → 1.2298 | surplus → accepted |
| a2 | 16 Sep 19:46 – 23:54 | 0.5M of 86.2M (1%) | 4 | 2.2226 → 2.1878 | 1.4467 → 1.3703 | surplus → accepted |
| a2 | 17 Sep 13:05 – 14:05 | 0 | 0 | 1.2299 | 0.8006 → 0.7621 | accepted → unaccounted |
| a2 | 21 Sep 21:15 – 23 Sep 08:11 | 0.5M of 20.0M (2%) | 8 | 1.4499 → 1.3866 | 0.8533 → 0.8161 | accepted → accepted |

masterrig's stretch from 10 Sep 20:45 to 21:19 UTC is unchanged. Its first fast request ended at
21:57 UTC.

The rate fits read stretches that pass the capture test. The pooled fit went from 175 to 178
stretches: a1 went from 65 to 67 clean stretches, and a2 from 80 to 81 before the fit's own
exclusions (78 to 79 after them).

## Published figures that moved

The publisher was built at 2026-09-23T18:00Z, before and after. 458 leaves differ. Three of them
are the meter-reading times noted above. Every other one is in `credits`, `events[0]` or
`last_change`, and all of those come from the corrected stretches and the refit. `tools/credits_report.py --publish-check`
reproduces all 18,314 figures of the after file from the committed history files.

**Rates** (credits per token, relative to Opus, 80% interval):

| | before | after |
|---|---|---|
| Fable, × Opus | 2.111 [2.004, 2.249] | 2.084 [1.968, 2.219] |
| Fable, credits per input token | 1.4074 [1.3362, 1.4995] | 1.3895 [1.3123, 1.4792] |
| Fable, pre / post 14 Sep | 2.122 / 2.096, post/pre 0.988 [0.889, 1.103] | 2.101 / 2.064, post/pre 0.982 [0.895, 1.089] |
| Sonnet, × Opus | 0.553 [0.455, 0.640] | 0.533 [0.437, 0.622] |
| Sonnet, credits per input token | 0.3689 [0.3035, 0.4270] | 0.3552 [0.2916, 0.4148] |
| Haiku, pooled fit point (not published) | 0.613 [0.000, 2.327] | 0.762 [0.000, 3.019] |
| Cache-read weight, fit point (not published) | 0.0018 [0.0000, 0.0052] | 0.0026 [0.0000, 0.0063] |
| `fable_interval` × Opus | [2.00, 2.25] | [1.97, 2.22] |

Haiku and Opus 5.5 remain shown at their inferred rates. The Opus 5.5 status sentence now says
that 1 of the fit's stretches carries Opus 5.5, not 2.

**Credits per 1%, per account and era** (the pooled fit's group scales):

| | before | after |
|---|---|---|
| a1 pre | 161,312 | 160,497 |
| a1 post | 153,951 | 152,922 |
| a2 pre | 190,606 | 192,281 |
| a2 post | 186,585 | 192,338 |
| a3 post | 147,486 | 149,575 |

**Five-hour window across 14 September** (median credits per 1%, before → after the cut, at the
pooled fit's rates):

| | before | after |
|---|---|---|
| a1 | 166,573 → 153,216, **−8.0%** (n 34 / 25) | 164,303 → 147,618, **−10.2%** (n 34 / 24) |
| a2 | 191,785 → 191,704, **−0.0%** (n 50 / 26) | 194,006 → 199,458, **+2.8%** (n 50 / 27) |
| a3 (after only) | 154,588 (n 31) | 155,118 (n 31) |
| pooled (median of a1, a2) | **−4.0%** | **−3.7%** |

**Tokens per week across 14 September**: windows per week −23.2%, unchanged. Compounded with the
five-hour change, the result goes from **−26.3% to −26.0%**.

**Window size**:

| | before | after |
|---|---|---|
| `window_credits.value` (credits per five-hour window) | 18,762,131 [8,256,314, 19,986,906] | 18,820,763 [8,282,115, 20,049,365] |
| `window_credits.credits_per_pct` | 187,621 | 188,208 |
| `window_tokens.all` (Opus input-equivalent tokens) | 432,570,048 | 433,921,829 |
| Sonnet tokens per window, input | 50,856,193 | 52,989,677 |
| Fable tokens per window, input | 13,330,709 | 13,544,751 |
| Sonnet sessions per window | 1,678.0 | 1,748.4 |
| Fable sessions per window | 41.3 | 41.9 |
| Opus sessions per window | 337.7 | 338.7 |

Other leaves also moved with the rates and the window: `effort_credits`, `per_model.*.api_value_per_window_usd`,
`per_model.*.tokens_per_window`, `sessions.*` and `window_tokens.per_week`. They are in the
before and after files, which were built from this branch's inputs at the commit before
(`be04171`, gs rebuilt with that code) and at this branch.

## Reproducing

```
# masterrig (once; a second run prints "already corrected" and writes nothing)
python3 tools/fast_mode_correction.py --extract ~/wf-data/masterrig-timing.jsonl.gz --meter-home <scratch>/mr
# gs
python3 -m tracker.gs_passive --prices data/prices.json --probes history/probes.jsonl --out history/gs-passive.json
# refit and check
python3 -m tools.model_rates history/masterrig-passive.json --json history/model-rates.json
python3 tools/credits_report.py --publish-check <published claude-usage.json>
```
