"""tracker.usage_cache: one poller per host and account, an atomic cache everyone else reads.

Every test stubs the HTTP layer; nothing here calls the real endpoint.
"""
import email.message
import email.utils
import fcntl
import json
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import usage_cache as uc
from tracker.meter_log import MAX_BACKOFF_S, MIN_READ_SPACING_S

BODY = {"five_hour": {"utilization": 24.0, "resets_at": "2026-10-02T05:00:00Z"},
        "seven_day": {"utilization": 33.0, "resets_at": "2026-10-08T03:59:59Z"},
        "seven_day_sonnet": {"utilization": None, "resets_at": None}}
T0 = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)


def http_error(code, retry_after=None):
    h = email.message.Message()
    if retry_after is not None:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://example.invalid", code, "x", h, None)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


class StubFetch:
    """Replays a script of responses: a dict is a body, an exception is raised."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.cache = root / "cache"
        self.cfg = root / "cfg"
        self.cfg.mkdir()
        (self.cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-secret"}}))
        (self.cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-1"}}))
        self.clock = Clock()

    def tearDown(self):
        self._tmp.cleanup()

    def poll(self, fetch, account="avis"):
        return uc.poll_once(account, self.cfg, fetch=fetch, now=self.clock, cache_dir=self.cache)


class PollAndReadTests(Base):
    def test_success_writes_the_record_and_readers_get_buckets_and_age(self):
        f = StubFetch(BODY)
        self.assertEqual(self.poll(f), "fetched")
        self.assertEqual(f.calls[0][0], "https://api.anthropic.com/api/oauth/usage")
        self.clock.advance(42)
        c = uc.read_cached("avis", self.cache, now=self.clock, max_age_s=600)
        self.assertEqual((c.status, c.age_s, c.stale), ("ok", 42.0, False))
        self.assertEqual(c.buckets, BODY)  # raw, including buckets this repo does not parse
        self.assertEqual(c.utilization().five_hour, 24.0)
        self.assertEqual(c.record["account"], "avis")
        self.assertIsNone(c.last_error)
        self.assertEqual(c.consecutive_refusals, 0)
        self.assertEqual(c.next_allowed_at, T0 + timedelta(seconds=MIN_READ_SPACING_S))
        self.assertNotIn("tok-secret", uc.cache_path("avis", self.cache).read_text())

    def test_a_second_poll_inside_the_spacing_makes_no_call(self):
        f = StubFetch(BODY)
        self.poll(f)
        self.clock.advance(MIN_READ_SPACING_S - 1)
        self.assertEqual(self.poll(f), "backoff")
        self.assertEqual(len(f.calls), 1)
        self.clock.advance(1)
        self.assertEqual(self.poll(f), "fetched")
        self.assertEqual(len(f.calls), 2)

    def test_read_never_calls_the_api(self):
        # read_cached takes no fetch at all; a missing cache is reported, not filled.
        c = uc.read_cached("avis", self.cache, now=self.clock)
        self.assertEqual(c.status, "missing")
        self.assertTrue(c.stale)
        self.assertFalse(self.cache.exists())


class RefusalBackoffTests(Base):
    def test_429_honours_retry_after_and_keeps_the_last_good_reading(self):
        f = StubFetch(BODY, http_error(429, retry_after=300))
        self.poll(f)
        self.clock.advance(MIN_READ_SPACING_S)
        self.assertEqual(self.poll(f), "rate_limited")
        c = uc.read_cached("avis", self.cache, now=self.clock)
        self.assertEqual(c.consecutive_refusals, 1)
        self.assertEqual(c.last_error_reason, "rate_limited")
        self.assertEqual(c.buckets, BODY)
        self.assertEqual(c.fetched_at, T0)
        self.assertEqual(c.next_allowed_at, self.clock() + timedelta(seconds=300))
        calls = len(f.calls)
        self.clock.advance(299)
        self.assertEqual(self.poll(f), "backoff")
        self.assertEqual(len(f.calls), calls)

    def test_retry_after_zero_still_backs_off_exponentially_and_success_resets(self):
        f = StubFetch(http_error(429, retry_after=0), http_error(429, retry_after=0),
                      http_error(429), BODY)
        waits = []
        for _ in range(3):
            self.assertEqual(self.poll(f), "rate_limited")
            c = uc.read_cached("avis", self.cache, now=self.clock)
            waits.append((c.next_allowed_at - self.clock()).total_seconds())
            self.clock.t = c.next_allowed_at
        self.assertEqual(waits, [MIN_READ_SPACING_S, MIN_READ_SPACING_S * 2, MIN_READ_SPACING_S * 4])
        self.assertEqual(self.poll(f), "fetched")
        c = uc.read_cached("avis", self.cache, now=self.clock)
        self.assertEqual((c.consecutive_refusals, c.last_error), (0, None))

    def test_backoff_is_capped(self):
        self.assertEqual(uc.refusal_backoff_s(30), MAX_BACKOFF_S)
        self.assertEqual(uc.refusal_backoff_s(1, retry_after=10 ** 6), MAX_BACKOFF_S)

    def test_a_429_on_one_account_holds_every_account_on_the_host(self):
        # The limiter is per host: the gs meters were refused in the same minute far
        # more often than alone, so a refusal for avis must stop dave calling too.
        self.poll(StubFetch(http_error(429, retry_after=200)), account="avis")
        dave = StubFetch(BODY)
        self.assertEqual(self.poll(dave, account="dave"), "backoff")
        self.assertEqual(dave.calls, [])
        self.clock.advance(200)
        self.assertEqual(self.poll(dave, account="dave"), "fetched")

    def test_auth_expired_is_not_counted_as_a_refusal(self):
        self.assertEqual(self.poll(StubFetch(http_error(401))), "auth_expired")
        c = uc.read_cached("avis", self.cache, now=self.clock)
        self.assertEqual((c.consecutive_refusals, c.last_error_reason), (0, "auth_expired"))
        self.assertEqual(c.next_allowed_at, T0 + timedelta(seconds=uc.AUTH_EXPIRED_SPACING_S))
        self.assertFalse((self.cache / uc.HOST_FILE).exists())

    def test_schema_change_is_an_error_not_a_reading(self):
        self.assertEqual(self.poll(StubFetch({"five_hour": {"utilization": 1}})), "schema")
        c = uc.read_cached("avis", self.cache, now=self.clock)
        self.assertIsNone(c.buckets)
        self.assertTrue(c.stale)
        self.assertIn("schema changed", c.last_error)


class StaleCacheTests(Base):
    def test_old_reading_is_stale_with_its_age(self):
        self.poll(StubFetch(BODY))
        self.clock.advance(3600)
        c = uc.read_cached("avis", self.cache, now=self.clock, max_age_s=600)
        self.assertEqual(c.status, "ok")
        self.assertTrue(c.stale)
        self.assertEqual(c.age_s, 3600.0)
        self.assertEqual(c.buckets, BODY)  # still served, flagged stale

    def test_refusals_after_a_good_read_let_its_age_grow(self):
        f = StubFetch(BODY, http_error(429))
        self.poll(f)
        for _ in range(3):
            self.clock.t = uc.read_cached("avis", self.cache, now=self.clock).next_allowed_at
            self.poll(f)
        c = uc.read_cached("avis", self.cache, now=self.clock, max_age_s=300)
        self.assertEqual(c.fetched_at, T0)
        self.assertEqual(c.consecutive_refusals, 3)
        self.assertEqual(c.age_s, 110 + 110 + 220)
        self.assertTrue(c.stale)

    def test_cli_read_exit_codes(self):
        out = self.cache
        self.assertEqual(uc.main(["read", "--account", "avis", "--cache-dir", str(out)]), uc.EXIT_MISSING)
        self.poll(StubFetch(BODY))
        # The CLI reads the wall clock, against which T0 is in the past or the future;
        # a huge max-age makes this fresh either way, a negative one stale.
        self.assertEqual(uc.main(["read", "--account", "avis", "--cache-dir", str(out),
                                  "--max-age", str(10 ** 10)]), uc.EXIT_FRESH)
        self.assertEqual(uc.main(["read", "--account", "avis", "--cache-dir", str(out),
                                  "--max-age=-1e10"]), uc.EXIT_STALE)


class CorruptCacheTests(Base):
    def test_reader_reports_corrupt_instead_of_raising(self):
        p = uc.cache_path("avis", self.cache)
        p.parent.mkdir(parents=True)
        for junk in ('{"fetched_at": "2026-10-0', "[1, 2]", "\x00\x01garbage"):
            p.write_text(junk)
            c = uc.read_cached("avis", self.cache, now=self.clock)
            self.assertEqual(c.status, "corrupt", junk)
            self.assertIsNone(c.buckets)
            self.assertTrue(c.stale)

    def test_poller_overwrites_a_corrupt_cache(self):
        p = uc.cache_path("avis", self.cache)
        p.parent.mkdir(parents=True)
        p.write_text("{not json")
        self.assertEqual(self.poll(StubFetch(BODY)), "fetched")
        self.assertEqual(uc.read_cached("avis", self.cache, now=self.clock).status, "ok")

    def test_corrupt_host_gate_does_not_block_polling(self):
        self.cache.mkdir()
        (self.cache / uc.HOST_FILE).write_text("{torn")
        self.assertEqual(self.poll(StubFetch(BODY)), "fetched")

    def test_bad_account_labels_are_refused(self):
        for bad in ("", "../x", ".hidden", "_host"):
            with self.assertRaises(ValueError):
                uc.cache_path(bad, self.cache)


class ConcurrencyTests(Base):
    def test_readers_never_see_a_torn_record_during_writes(self):
        path = uc.cache_path("avis", self.cache)
        big = dict(BODY, pad="x" * 200_000)
        uc.write_json_durable(path, {"fetched_at": "2026-10-02T01:00:00+00:00", "buckets": big, "n": -1})
        stop = threading.Event()
        bad, reads = [], [0]

        def reader():
            while not stop.is_set():
                c = uc.read_cached("avis", self.cache, now=self.clock)
                reads[0] += 1
                if c.status != "ok" or c.buckets is None or len(c.buckets["pad"]) != 200_000:
                    bad.append(c.status)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for n in range(60):
                uc.write_json_durable(path, {"fetched_at": "2026-10-02T01:00:00+00:00", "buckets": big, "n": n})
        finally:
            stop.set()
            for t in threads:
                t.join()
        self.assertEqual(bad, [])
        self.assertGreater(reads[0], 0)
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), ["avis.json"])  # no temp files left

    def test_a_second_poller_on_the_same_host_and_account_is_busy(self):
        lock = uc.cache_path("avis", self.cache).with_suffix(".lock")
        lock.parent.mkdir(parents=True)
        with open(lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            f = StubFetch(BODY)
            self.assertEqual(self.poll(f), "busy")
            self.assertEqual(f.calls, [])
            # A different account on the same host has its own lock.
            self.assertEqual(self.poll(StubFetch(BODY), account="dave"), "fetched")

    def test_poll_loop_sleeps_until_next_allowed(self):
        f = StubFetch(BODY, http_error(429, retry_after=500), BODY)
        slept = []

        def sleep(s):
            slept.append(s)
            self.clock.advance(s)

        uc.poll_loop("avis", self.cfg, cache_dir=self.cache, fetch=f, now=self.clock, sleep=sleep,
                     max_iterations=3)
        self.assertEqual(slept, [MIN_READ_SPACING_S, 500, MIN_READ_SPACING_S])
        self.assertEqual(len(f.calls), 3)


class ReviewFindingTests(Base):
    """Defects found in review on 2026-10-02; each test failed before its fix."""

    def test_retry_after_as_an_http_date_is_honoured(self):
        when = email.utils.format_datetime(T0 + timedelta(seconds=900), usegmt=True)
        f = StubFetch(http_error(429, retry_after=when))
        self.assertEqual(self.poll(f), "rate_limited")
        c = uc.read_cached("avis", self.cache, now=self.clock)
        self.assertEqual(c.next_allowed_at, T0 + timedelta(seconds=900))
        host = json.loads((self.cache / uc.HOST_FILE).read_text())
        self.assertEqual(uc._parse_stamp(host["next_allowed_at"]), T0 + timedelta(seconds=900))

    def test_retry_after_date_in_the_past_or_garbage_falls_back_to_the_schedule(self):
        past = email.utils.format_datetime(T0 - timedelta(seconds=900), usegmt=True)
        for ra in (past, "soon", "nan", "-5"):
            self.assertEqual(uc.retry_after_s(http_error(429, retry_after=ra), now=T0), 0.0, ra)
        self.assertEqual(uc.retry_after_s(http_error(429, retry_after="1e400"), now=T0), float("inf"))
        self.assertEqual(uc.refusal_backoff_s(1, float("inf")), MAX_BACKOFF_S)

    def test_a_gate_set_by_a_clock_that_ran_ahead_does_not_stall_polling(self):
        # The clock stepped back an hour after a 429: next_allowed_at is now further
        # ahead than any wait the poller ever sets, so it cannot be a real backoff.
        uc.write_json_durable(uc.cache_path("avis", self.cache),
                              {"next_allowed_at": uc._stamp(T0 + timedelta(hours=3))})
        self.assertEqual(self.poll(StubFetch(BODY)), "fetched")

    def test_a_far_future_host_gate_is_ignored_by_poll_and_loop(self):
        self.cache.mkdir()
        uc.write_json_durable(self.cache / uc.HOST_FILE, {"next_allowed_at": uc._stamp(T0 + timedelta(hours=3))})
        slept = []

        def sleep(s):
            slept.append(s)
            self.clock.advance(s)

        f = StubFetch(BODY)
        uc.poll_loop("avis", self.cfg, cache_dir=self.cache, fetch=f, now=self.clock, sleep=sleep,
                     max_iterations=2)
        self.assertEqual(len(f.calls), 2)
        self.assertEqual(slept, [MIN_READ_SPACING_S, MIN_READ_SPACING_S])

    def test_a_real_gate_still_holds(self):
        # The far-future guard must not swallow the longest real wait.
        uc.write_json_durable(uc.cache_path("avis", self.cache),
                              {"next_allowed_at": uc._stamp(T0 + timedelta(seconds=MAX_BACKOFF_S))})
        self.assertEqual(self.poll(StubFetch(BODY)), "backoff")

    def test_default_read_has_a_max_age(self):
        # A reader that passes no max_age_s must not get a day-old reading as fresh.
        self.poll(StubFetch(BODY))
        self.clock.advance(86400)
        self.assertTrue(uc.read_cached("avis", self.cache, now=self.clock).stale)
        self.clock.t = T0 + timedelta(seconds=60)
        self.assertFalse(uc.read_cached("avis", self.cache, now=self.clock).stale)

    def test_a_reading_stamped_in_the_future_is_stale(self):
        # The clock stepped back: the reading's real age is unknown.
        self.poll(StubFetch(BODY))
        self.clock.advance(-3600)
        c = uc.read_cached("avis", self.cache, now=self.clock, max_age_s=600)
        self.assertLess(c.age_s, 0)
        self.assertTrue(c.stale)

    def test_buckets_of_another_account_are_dropped_when_the_config_dir_changes_account(self):
        self.poll(StubFetch(BODY))
        (self.cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-2"}}))
        self.clock.advance(MIN_READ_SPACING_S)
        self.assertEqual(self.poll(StubFetch(http_error(401))), "auth_expired")
        c = uc.read_cached("avis", self.cache, now=self.clock)
        self.assertNotEqual(c.record["identity"], None)
        self.assertIsNone(c.buckets)
        self.assertIsNone(c.fetched_at)
        self.assertTrue(c.stale)

    def test_host_gate_is_not_shortened_by_a_concurrent_refusal(self):
        # avis (Retry-After 110) and dave (Retry-After 1200) are refused together. If
        # avis reads _host.json before dave writes it and writes after, the host hold
        # drops from 1200 s to 110 s. The update must be serialised.
        real_load, real_write = uc._load, uc.write_json_durable
        state = {"armed": False, "fired": False}

        def write(path, doc):
            real_write(path, doc)
            if Path(path).name == "avis.json" and doc.get("last_error_reason") == "rate_limited":
                state["armed"] = True

        def load(path):
            doc = real_load(path)
            if state["armed"] and not state["fired"] and Path(path).name == uc.HOST_FILE \
                    and threading.current_thread() is threading.main_thread():
                state["fired"] = True
                t = threading.Thread(target=lambda: self.poll(StubFetch(http_error(429, retry_after=1200)),
                                                              account="dave"))
                t.start()
                t.join(timeout=1.0)
                state["thread"] = t
            return doc

        uc._load, uc.write_json_durable = load, write
        try:
            self.assertEqual(self.poll(StubFetch(http_error(429, retry_after=0))), "rate_limited")
            state["thread"].join(timeout=5)
        finally:
            uc._load, uc.write_json_durable = real_load, real_write
        self.assertTrue(state["fired"])
        host = json.loads((self.cache / uc.HOST_FILE).read_text())
        self.assertEqual(uc._parse_stamp(host["next_allowed_at"]), T0 + timedelta(seconds=MAX_BACKOFF_S))

    def test_a_crash_before_the_replace_leaves_the_previous_record_readable(self):
        self.poll(StubFetch(BODY))
        real = uc.os.replace

        def boom(src, dst):
            raise KeyboardInterrupt  # stands in for a kill between the temp write and the replace

        uc.os.replace = boom
        try:
            self.clock.advance(MIN_READ_SPACING_S)
            with self.assertRaises(KeyboardInterrupt):
                self.poll(StubFetch(BODY))
        finally:
            uc.os.replace = real
        c = uc.read_cached("avis", self.cache, now=self.clock)
        self.assertEqual((c.status, c.fetched_at, c.buckets), ("ok", T0, BODY))
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), ["avis.json", "avis.lock"])


if __name__ == "__main__":
    unittest.main()
