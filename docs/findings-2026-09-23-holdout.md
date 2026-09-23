# Do the per-model rates predict stretches they were not fitted on? 2026-09-23

The per-model rates in `history/model-rates.json` are quoted with in-sample residuals: a
median absolute relative residual of about 0.06 on the stretches they were fitted on. That
says the model fits those stretches. It does not say it predicts new ones. This note holds
stretches out and checks.

Commands (no traffic; read-only arithmetic over the committed `history/gs-passive.json`,
`history/masterrig-passive.json` and `history/harness-runs.jsonl` at b0a8874):

```
python3 -m tools.holdout
python3 -m tools.holdout --json /tmp/holdout.json    # every held-out stretch, predicted and actual
```

## Method

`tools/holdout.py` imports the fitting code from `tools/model_rates.py` and changes none of it:
the same stretch selection (`clean`, `prepare`), the same family columns (`fit`), the same
design matrix (`design`) and the same solver (`nnls`), in the variant the publisher adopts
(cache-read weight fitted jointly, output at 5x input). It fits on a training set, predicts
each held-out stretch's five-hour meter movement from its tokens, and compares the prediction
with the movement the meter recorded.

The prediction interval is an 80% bootstrap interval (the 10th and 90th percentiles, the same
`INTERVAL` model_rates.py uses): each of 600 draws refits on the training stretches resampled
with replacement and multiplies its prediction by one plus a relative residual drawn from the
training fit. It carries both the uncertainty in the rates and one stretch's scatter about
them, so a well-calibrated model puts about 80% of held-out stretches inside it.

Two splits:

- **Across accounts.** Fit on jwork, predict dave; and the reverse. Only after the
  14 September cut: dave has no clean stretch before it, so neither pre-cut direction can run.
- **Across time.** Fit on post-cut stretches that ended by 2026-09-18T00:00Z, predict those
  that started after it. No stretch straddles the split point. Only jwork has stretches
  before it (dave's first clean stretch starts 2026-09-18T12:04Z), so dave has no time split
  of its own; the pooled row predicts both accounts' later stretches from jwork's earlier ones.

## Results

| Direction | Train | n | Median abs error (meter points) | Median abs relative error | Inside 80% interval | Median predicted / actual |
|---|---:|---:|---:|---:|---:|---:|
| jwork -> dave, post-cut | 26 | 32 | 2.83 | 0.283 | 0.41 | 0.764 |
| dave -> jwork, post-cut | 32 | 26 | 1.76 | 0.176 | 0.58 | 1.127 |
| jwork, by 18 Sep -> after | 12 | 14 | 0.83 | 0.083 | 0.86 | 0.926 |
| jwork + dave, by 18 Sep -> after | 12 | 46 | 1.85 | 0.173 | 0.61 | 0.871 |

Not run: jwork -> dave and dave -> jwork before the cut (no dave stretches), and dave's own
time split (no dave stretches before 18 September). The median held-out stretch moved the
meter 10 points in every direction that ran.

Where the misses fall: for 17 of dave's 32 stretches the recorded movement sat above the
interval jwork's rates gave, and for 2 below it. For 11 of jwork's 26 stretches it sat below
the interval dave's rates gave, and for none above it.

## What this says

1. **Within one account, the rates predict.** Fitted on jwork's 12 stretches from
   15 to 18 September, they predict jwork's 14 later stretches with a median error of
   0.83 meter points (median relative error 0.083), and 86% of the recorded movements fall inside the 80% interval.
   That is close to the in-sample residual (0.045 on the training fit) and the interval is,
   if anything, slightly wide.

2. **Across accounts, they do not, and the miss has a direction.** jwork's rates
   under-predict dave's meter by a median 24% (predicted over actual 0.764), and dave's
   over-predict jwork's by 13% (1.127). Coverage is 41% and 58% against a nominal 80%. The
   errors are not scatter: they fall on one side. For the same tokens, dave's meter moves
   further than jwork's.

3. **This test cannot say whether that is a rate or a scale.** An account-wide factor (dave's
   window smaller than jwork's, or dave's transcripts missing work the meter counted, which
   raises dave's movement per recorded token) would produce exactly this pattern with the
   per-model ratios unchanged. The published rates are stated relative to the Opus anchor, so
   an account-wide factor cancels out of them; this test predicts absolute meter points, where
   it does not cancel.
   `tools/model_rates.py` section 4 already looks at what explains the jwork/dave gap; this
   note measures its size out of sample.

4. **The pooled time split is mostly the account gap again.** 32 of its 46 held-out stretches
   are dave's, predicted from jwork's rates, so its 0.871 and 61% coverage repeat item 2.

## Limits

- One account's time split, on 12 training stretches. Four families enter that fit, which is
  close to the eight-stretch floor `MIN_FIT_N` sets.
- The interval treats stretches as independent. Consecutive stretches on one account share
  a workload, so the interval may be narrower than the truth even where coverage looks right.
- Nothing here tests the Opus anchor itself. Every rate is measured relative to it.
