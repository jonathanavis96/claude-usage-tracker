# Chaos soak findings, 2026-10-02 (UT-4)

Harness: `tests/chaos/` (run `python3 -m pytest tests/chaos -q`, about 13 s). It drives the real collector, `tracker.meter_log.sample` → `tracker.usage_api.read_usage` → production's `NO_RETRY_FETCH` (urllib), against a stdlib fake of the usage endpoint on 127.0.0.1. A fake clock is passed through `sample(now=)`. The log it writes is then read back through `tracker.samples.parse_log("meter")` and `tracker.join.window_points`. A conftest fixture fails any socket connection to a host other than loopback. The only shim is urlopen's timeout, which is cut from 30 s to 0.3 s so that a timeout fault stays cheap.

`tests/chaos` has no `__init__.py`, so `python3 -m unittest discover -s tests` skips it. The strict xfails only mean something under pytest.

## What holds

These pass across 240 seeded cycles plus three more seeds of 120 cycles each, with about 30% of ticks faulted:

- **Faults that become gaps.** 401, 429 with Retry-After, 500/502/503, timeouts, connection resets, malformed JSON, truncated JSON, a missing `five_hour`, null utilization and a list body each become exactly one `error` line with exit 4. None is ever recorded as a reading, and none as zero.
- **Auth failure.** An auth failure is logged with `reason: auth_expired`, carries no data and never writes the token to the log. A 401 followed by a token refreshed on disk is retried once and then succeeds.
- **429 backoff.** A 429's Retry-After skips later ticks without making a request.
- **Reset jitter.** `resets_at` jitter of ±3 s never splits a window. A mid-window five-hour reset and a weekly reset are attributed to the right true window, both per row and in the `window_points` output.
- **Timezone.** The host TZ (UTC or Africa/Johannesburg) does not change a byte of the log, and a clock that reports +02:00 still stamps UTC.
- **Deleted log.** When the log is deleted, the next tick starts a fresh one.

## Failing invariants (all six fixed on the integration branch, UT-I)

Status: fixed in `tracker/meter_log.py` and the xfail markers removed; `pytest tests/chaos` is 18 passed. Fixes: (1) `_append` writes a newline first when the log does not end in one; (2) `_append` returns False on `OSError` and `sample()` exits 4; (3) `sample()` holds `fcntl.flock` on `<log>.lock` (waits up to 45 s, then exits 4), so the second run sees the first's reading and skips under the 110 s spacing; (4) a clock up to 20 min behind the last line skips the tick with exit 4; (5) a last line more than 20 min in the future is ignored, and a backoff never exceeds its own Retry-After; (6) Retry-After is capped at 1200 s (`RETRY_429_MAX_S`).

Original findings:

| # | Invariant | Test | Cause |
|---|---|---|---|
| 1 | No reading lost after a crash mid-write | `test_crash_mid_write_does_not_lose_the_next_reading` | `tracker/meter_log.py:108` `_append` opens the log with `"a"` and never checks for a trailing newline. After a torn partial line, the next good reading is glued onto the fragment and becomes unparseable, so one real reading is lost per crash. Fix: write a newline first when the file does not end in one, or use a single `os.write` with `O_APPEND`. |
| 2 | No crash on a full disk or read-only directory | `test_read_only_data_dir_fails_cleanly` | `_append` (`meter_log.py:108`) raises `PermissionError`/`OSError` straight out of `sample()`. That includes the calls inside its own `except` handlers (`meter_log.py:180-205`), so the timer gets a traceback and exit 1 instead of exit 4 and a stderr gap note. |
| 3 | One writer at a time; no duplicate rows | `test_two_runs_at_once_write_one_row` | `sample()` (`meter_log.py:154`) takes no lock. Two overlapping runs (a manual run next to the timer, or a slow tick overlapping the next one) both fetch and both append, which writes two rows for one tick. Fix: `fcntl.flock` on `<log>.lock` around the whole read-backoff-fetch-append sequence. Skip with exit 4 when the lock is busy. |
| 4 | Monotonic timestamps | `test_clock_stepped_back_keeps_log_monotonic` | `sample()` stamps `clock()` (`meter_log.py:168`) and appends with no comparison to the last line's `ts`, so a clock stepped backwards writes an out-of-order row. `parse_moonlighter` keeps file order. `tracker/join.py` sorts, but other readers (`_last_line`, weekly) assume that the last line is the latest. |
| 5 | A 429 does not block sampling past Retry-After | `test_429_logged_by_a_fast_clock_does_not_block_for_hours` | `_backoff_remaining_s` (`meter_log.py:150`) measures the backoff from the logged `ts`. If the clock ran fast when a 429 was logged, every later tick is skipped until the real clock passes that stamp. With a 3 h skew, nothing is sampled for 3 h. Fix: cap `remaining` at `retry_after_s`. |
| 6 | Backoff is bounded | `test_absurd_retry_after_is_capped` | `_retry_after_s` (`meter_log.py:130`) accepts any numeric Retry-After, so `86400` stops sampling for a day. `usage_api` caps its own waits at `RETRY_429_MAX_S` (1200 s), and this path should do the same. |

All of these are in `tracker/meter_log.py`, which UT-H claimed on the board (atomic writes and malformed input), so UT-4 changed no production code. When a fix lands, its xfail turns into a strict XPASS failure, and the marker should be removed.

## Caveats

- Test 2 uses chmod to make the directory read-only. Run as root, the write succeeds, and the strict xfail would report XPASS.
- The soak exercises the meter-log collector (gs and Dave's path). It does not run `bin/passive.sh` itself, because that script runs `git pull` and `git push`. It also does not run moonlighter's masterrig log, which an external tool writes.
- `_default_fetch`'s 30 s urlopen timeout (`tracker/usage_api.py:77`) cannot be configured. The harness shortens it by patching `urllib.request.urlopen`.
