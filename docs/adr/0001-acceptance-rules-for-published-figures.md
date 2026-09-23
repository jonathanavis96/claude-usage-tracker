# 0001. Acceptance rules for published figures

Status: accepted, 2026-09-23

## Context

The tracker's public figures depend on a handful of rules that decide which evidence counts:
when a change is published, which stretches are read, and when a per-model rate is withheld.
Each rule is in the code with its reasoning in a comment, but they are spread over six
files, and a change to any of them moves published numbers without the change being obvious
from the diff alone. This record fixes them as they stand in the code on 2026-09-23 (main at
b0a8874), so a later change to one is visible as a change to a decision, not only to a
constant.

## Decision

These are the acceptance rules. Each names where it lives.

1. **Two accounts must agree on a published change in the credit series, within 3 days.**
   A step in credits per 1% is published only when at least two accounts each detect a step
   in the same direction with onsets no more than `DOLLAR_AGREEMENT_DAYS = timedelta(days=3)`
   apart. An account needs 2 x `MIN_STRETCH_PCT` (100) points of five-hour meter movement
   before it is testable at all; with fewer than two testable accounts nothing is published.
   Source: `tracker/publish.py:83` (`DOLLAR_AGREEMENT_DAYS`), `tracker/publish.py:316`
   (`_agreeing_credit_events`), `tracker/detect.py:287` (`MIN_STRETCH_PCT`).

2. **Provisional events stay out of the headline.** `last_change`, the figure the page
   states as its headline, is chosen only from events that are not provisional. Events on
   the credit (window) series are provisional because their readings carry no rounding
   bounds; they remain in `events` with `evidence_quality: "provisional"`. Weekly events are
   certified against both levels' rounding intervals and can be the headline.
   Source: `tracker/publish.py:1716` (`_latest_change_with_scope`, the filter at line 1734),
   `tracker/publish.py:1743` (`_provisional`). bin/daily.sh also refuses to announce a
   provisional or legacy-uncertain change.

3. **The capture check on gs stretches.** The check itself (`tracker/capture.py`) runs on
   every gs stretch and its verdict is recorded as `capture_status`. It gates in two
   different places, differently:
   - For the published credit series it is advisory: `CAPTURE_GATE = False`. Every priced
     stretch is published except one whose capture is under `COLLECTION_GAP = 0.10` (the
     meter moved with almost nothing on gs to explain it) and one with any unpriced token.
     Source: `tracker/gs_passive.py:108` (`CAPTURE_GATE`), `tracker/gs_passive.py:336`
     (`publishable`), `tracker/capture.py:87` (`COLLECTION_GAP`).
   - For the per-model rate fits it is a gate: a gs stretch is fitted only if its
     `capture_status` reads `accepted`. masterrig is exempt from this test, and its fit is
     not adopted anyway (rule 7).
     Source: `tracker/credits.py:448` (`clean_stretches`, `require="capture_status"`),
     `tools/model_rates.py:357` (`clean`, `exempt=("masterrig",)`).

4. **`min_n`.** A ratio with fewer than `MIN_N = 3` stretches on either side is reported as
   not measurable rather than as a number, and a family's pooled rate needs at least
   `MIN_N - 1 = 2` fits carrying it. Alongside it, a fit needs `MIN_FIT_N = 8` clean
   stretches and a family enters a fit only with tokens in `MIN_NONZERO = 3` of them.
   Source: `tools/model_rates.py:64` (`MIN_N`, with `MIN_FIT_N` and `MIN_NONZERO` on the
   next two lines), used at `tools/model_rates.py:609` (`adopt`).

5. **A family whose pooled interval reaches zero is withheld (`NOT_IDENTIFIED`).** When the
   union of the fits' bootstrap intervals for a family has a lower end of zero, neither the
   value nor the interval is published; the row carries the `NOT_IDENTIFIED` sentence
   instead. The interval is withheld too because tracker/gs_passive.py prices at an
   interval's midpoint where there is no value.
   Source: `tools/model_rates.py:653` (`NOT_IDENTIFIED`), applied at
   `tools/model_rates.py:685` (`identified = ...`).

6. **Inferred resets are used for no figure (`INFERRED_RESET_VERIFIED = False`).** A
   five-hour reset inferred from a reset-less sample is recorded as `reset_source:
   "inferred"` but does not make a stretch `reset_verified`, and the join, the weekly points
   and every rollup see the samples without it. The bar for changing this is zero wrong and
   zero spurious resets when recorded resets are stripped and re-inferred; jwork and
   masterrig missed it on 2026-09-23.
   Source: `tracker/gs_passive.py:400` (`INFERRED_RESET_VERIFIED`), applied at
   `tracker/gs_passive.py:424` and `tracker/gs_passive.py:536`.

7. **Only jwork and dave are fitted (`FIT_ACCOUNTS`).** The per-model rates are pooled from
   `FIT_ACCOUNTS = ("jwork", "dave")`. masterrig's fit is reported for completeness and
   never adopted: its median absolute relative residual is 0.455 against 0.055 to 0.065 on
   the two gs accounts.
   Source: `tools/model_rates.py:555` (`FIT_ACCOUNTS`).

### Pending

8. **60% dominance for a family's fit (pending, not yet in the code).** A fit counts toward a
   family's rate only if 3 or more of its stretches are at least 60% that family. Another
   PR is adding this; when it merges, this entry takes its source location and moves up
   into the list above. Until then the code has no such rule (the only dominance constant
   today is `DOMINANCE = 0.95` at `tools/model_rates.py:63`, which selects single-model
   stretches for section 1 and does not gate the fits).

## The rule for changing these

Any change to one of these rules is made in its own PR, which changes nothing else and
records the published figures before and after the change: at least `last_change`, the
per-model rates and their status, and the tokens per window, taken from a rebuild of the
same committed inputs with the old and the new code (`python3 tools/verify_publish.py`
checks the "before" against the live file). The same PR updates this record.

## Consequences

A reviewer can check a PR that touches `tracker/publish.py`, `tracker/gs_passive.py`,
`tracker/credits.py`, `tracker/capture.py` or `tools/model_rates.py` against this list, and
a changed rule without its before-and-after figures is visible as a missing section of the
PR rather than as a quiet shift on the page. The line numbers above drift as the files
change; the names do not, and are the reference.
