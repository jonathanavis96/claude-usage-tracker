"""Long chaos soak: UT-4's rig (chaos_rig.py) driven for simulated days with every fault.

Not collected by pytest (no test_ prefix). Run from the repo root:

    HOME=$(mktemp -d) nice -n 10 timeout 2700 python3 tests/chaos/long_soak.py \
        --ticks 100000 --seed 20261002 --out /path/to/report.json

Request faults (all of chaos_rig.REQUEST_FAULTS) fire on ~30% of ticks. Rig-level
faults fire at random on top: a clock running fast, a torn line from a crash mid-write, a read-only data
dir, the clock stepped back, a 429 stamped by a fast clock, an absurd Retry-After,
a long Retry-After, two runs at once, the log deleted, the token expired and later
refreshed, the host TZ switched, a clock reporting +02:00, timer jitter (short and
long steps), long idle spells, mid-window five-hour and weekly resets. Natural
five-hour and seven-day rollovers happen on their own.

Unlike check_invariants (which stops at the first failure), every tick is checked
and each broken invariant is counted with its first examples, so one run reports
everything it saw. A join check (tracker.samples -> tracker.join.window_points)
over every log segment runs once per simulated day and at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1])]

from chaos_rig import REQUEST_FAULTS, Rig, Tick, stamp  # noqa: E402

from tracker import meter_log  # noqa: E402
from tracker.join import window_points  # noqa: E402
from tracker.samples import parse_log  # noqa: E402
from tracker.usage_api import same_reset  # noqa: E402

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_real_connect = socket.socket.connect


def _guarded_connect(self, addr):
    if self.family in (socket.AF_INET, socket.AF_INET6) and addr[0] not in _LOOPBACK:
        raise AssertionError(f"long soak tried to reach {addr!r}: loopback only")
    return _real_connect(self, addr)


socket.socket.connect = _guarded_connect

TICK = timedelta(minutes=2)
SPECIALS = ("torn", "readonly", "clockback", "fastclock429", "absurd429", "long429", "concurrent",
            "deletelog", "tokenexpiry", "tz", "localoffset", "clockfwd", "jitter_short", "jitter_long", "idle",
            "reset_five", "reset_seven")


class Soak:
    def __init__(self, root: Path, seed: int):
        self.rig = Rig(root, seed=seed)
        self.rng = random.Random(seed ^ 0x5EED)
        self.anoms: dict[str, list] = defaultdict(list)
        self.counts: dict[str, int] = defaultdict(int)
        self.events: dict[str, int] = defaultdict(int)
        self.faults: dict[str, int] = defaultdict(int)
        self.pos = 0
        self.last_ts: datetime | None = None
        self.last_call_at: datetime | None = None  # last tick that actually reached the API
        self.last_reading_at: datetime | None = None
        self.longest_stall = timedelta(0)
        self.backoff_until: datetime | None = None  # our own model of the 429 block
        self.segments: list[Path] = []
        self.token_bad_until = -1
        self.idle_until = -1
        self.ticks_done = 0
        self.splits_seen: set[str] = set()
        self.last_was_reading = False

    def note(self, kind: str, i: int, msg: str) -> None:
        self.counts[kind] += 1
        if len(self.anoms[kind]) < 5:
            self.anoms[kind].append({"tick": i, "sim": stamp(self.rig.clock), "msg": msg[:300]})

    # -- reading what one tick appended ------------------------------------------------
    def new_lines(self, i: int, fragment: str | None) -> list[dict]:
        log = self.rig.log
        if not log.exists():
            self.pos = 0
            return []
        size = log.stat().st_size
        if size < self.pos:
            self.pos = 0
        with open(log, "rb") as fh:
            fh.seek(self.pos)
            raw = fh.read().decode("utf-8", errors="replace")
        self.pos = size
        out = []
        for line in raw.split("\n"):
            if not line.strip():
                continue
            if fragment is not None and line == fragment:
                fragment = None
                continue
            try:
                d = json.loads(line)
            except ValueError:
                self.note("unparseable_line", i, repr(line[:200]))
                continue
            if not isinstance(d, dict):
                self.note("non_object_line", i, repr(line[:200]))
                continue
            out.append(d)
        return out

    # -- one tick plus its checks ------------------------------------------------------
    def step(self, i: int) -> None:
        rig = self.rig
        special = None
        if self.rng.random() < 0.03:
            special = self.rng.choice(SPECIALS)
            self.events[special] += 1
        fault = None
        if self.rng.random() < 0.3:
            fault = self.rng.choices(REQUEST_FAULTS, [0.3 if f == "timeout" else 1 for f in REQUEST_FAULTS])[0]
        burn = self.rng.choice((0.0, 0.1, 0.25, 0.5, 1.0))
        step, now, fault_arg = TICK, None, 120
        fragment = None
        if i <= self.idle_until:
            burn = 0.0
        if fault == "429":
            fault_arg = self.rng.choice((60, 120, 300))

        if special == "torn":
            fragment = '{"ts": "%s", "account": "chaos", "five_h' % stamp(rig.clock)
            if rig.log.exists():
                with open(rig.log, "a") as fh:
                    fh.write(fragment)
            else:
                fragment = None
        elif special == "clockback":
            now = rig.clock + TICK - timedelta(minutes=self.rng.choice((1, 5, 10, 19)))
        elif special == "fastclock429":
            fault, fault_arg = "429", 60
            now = rig.clock + TICK + timedelta(hours=self.rng.choice((1, 3, 12)))
        elif special == "clockfwd":
            now = rig.clock + TICK + timedelta(minutes=self.rng.choice((3, 10, 19, 21, 60, 180, 720)))
        elif special == "absurd429":
            fault, fault_arg = "429", self.rng.choice((3600, 86400, 10 ** 7))
        elif special == "long429":
            fault, fault_arg = "429", self.rng.choice((600, 1199, 1200))
        elif special == "deletelog" and rig.log.exists():
            seg = rig.log.with_name(f"segment-{len(self.segments):04d}.log")
            rig.log.rename(seg)
            self.segments.append(seg)
            self.pos = 0
        elif special == "tokenexpiry":
            rig.set_token("expired-token")
            self.token_bad_until = i + self.rng.randint(1, 30)
        elif special == "tz":
            os.environ["TZ"] = self.rng.choice(("UTC", "Africa/Johannesburg", "America/Los_Angeles",
                                                "Pacific/Chatham", "Asia/Kathmandu"))
            time.tzset()
        elif special == "localoffset":
            off = timezone(timedelta(hours=self.rng.choice((2, -7, 13)), minutes=self.rng.choice((0, 45))))
            now = (rig.clock + TICK).astimezone(off)
        elif special == "jitter_short":
            step = timedelta(seconds=self.rng.randint(30, 109))
        elif special == "jitter_long":
            step = timedelta(minutes=self.rng.randint(3, 400))
        elif special == "idle":
            self.idle_until = i + self.rng.randint(30, 400)
        elif special == "reset_five":
            rig.meter.reset_five(rig.clock + timedelta(seconds=30))
        elif special == "reset_seven":
            rig.meter.reset_seven(rig.clock + timedelta(seconds=30))
        if self.token_bad_until == i - 1:
            from chaos_rig import TOKEN
            rig.set_token(TOKEN)

        if fault:
            self.faults[fault] += 1

        if special == "readonly":
            t = self._readonly_tick(fault, step, burn)
        elif special == "concurrent":
            t = self._concurrent_tick(step, burn)
        else:
            t = rig.tick(fault, step=step, burn=burn, now=now, fault_arg=fault_arg)
        if len(rig.ticks) > 4:
            del rig.ticks[:-4]
        self.check(i, t, special, fragment, step)
        self.ticks_done = i + 1

    def _readonly_tick(self, fault, step, burn) -> Tick:
        rig = self.rig
        d = rig.log.parent
        d.mkdir(parents=True, exist_ok=True)
        had = rig.log.exists()
        d.chmod(0o500)
        if had:
            rig.log.chmod(0o400)
        try:
            return rig.tick(fault, step=step, burn=burn)
        finally:
            d.chmod(0o700)
            if had:
                rig.log.chmod(0o600)

    def _concurrent_tick(self, step, burn) -> Tick:
        rig = self.rig
        rig.clock += step
        rig.meter.advance(rig.clock, burn)
        at = rig.clock
        before = rig.api.requests
        results, crashes = [], []

        def run():
            try:
                results.append(meter_log.sample(rig.cfg, "chaos", rig.log, fetch=rig.fetch, now=lambda: at))
            except BaseException as e:  # noqa: BLE001
                crashes.append(e)

        ths = [threading.Thread(target=run) for _ in range(2)]
        for th in ths:
            th.start()
        for th in ths:
            th.join(10)
        t = Tick(at, None, rc=max(results) if results else None, crashed=crashes[0] if crashes else None,
                 truth_five=rig.meter.five.used, truth_five_reset=rig.meter.five.resets_at,
                 truth_seven_reset=rig.meter.seven.resets_at)
        t.requested = rig.api.requests > before
        rig.ticks.append(t)
        return t

    def check(self, i: int, t: Tick, special: str | None, fragment: str | None, step: timedelta) -> None:
        rig = self.rig
        if t.crashed is not None:
            self.note("crash", i, f"{special or t.fault}: {t.crashed!r}")
        if t.rc not in (0, 2, 4):
            self.note("bad_exit_code", i, f"{special or t.fault}: rc={t.rc}")
        lines = self.new_lines(i, fragment)
        if fragment is not None and rig.log.exists():
            pass
        readings = [d for d in lines if (d.get("five_hour") or {}).get("utilization") is not None]
        if len(readings) > 1 or (len(lines) > 1 and special != "concurrent"):
            self.note("more_than_one_line_per_tick", i, f"{special or t.fault}: {len(lines)} lines")
        for d in lines:
            try:
                ts = datetime.fromisoformat(d["ts"])
            except (KeyError, ValueError) as e:
                self.note("bad_ts", i, f"{d!r} {e}")
                continue
            if not d["ts"].endswith("+00:00"):
                self.note("non_utc_stamp", i, d["ts"])
            if d["ts"] != stamp(t.at):
                self.note("stamp_not_tick_time", i, f"{d['ts']} vs {stamp(t.at)}")
            if self.last_ts is not None and ts < self.last_ts:
                self.note("non_monotonic", i, f"{special or t.fault}: {d['ts']} after {self.last_ts.isoformat()}")
            is_reading = (d.get("five_hour") or {}).get("utilization") is not None
            elif_dup = self.last_ts is not None and ts == self.last_ts and is_reading and self.last_was_reading
            if elif_dup:
                self.note("duplicate_reading_stamp", i, f"{special or t.fault}: {d['ts']}")
            self.last_ts, self.last_was_reading = ts, is_reading
        for d in readings:
            if t.fault is not None:
                self.note("reading_on_faulted_tick", i, f"{t.fault}: {d['five_hour']}")
                continue
            if abs(d["five_hour"]["utilization"] - t.truth_five) > 1e-9:
                self.note("wrong_value", i, f"{d['five_hour']['utilization']} vs {t.truth_five}")
            for key, truth in (("five_hour", t.truth_five_reset), ("seven_day", t.truth_seven_reset)):
                got = datetime.fromisoformat(d[key]["resets_at"])
                if abs((got - truth).total_seconds()) > 5:
                    self.note(f"{key}_reset_wrong", i, f"{got} vs {truth}")
        token_bad = i <= self.token_bad_until
        if t.fault in REQUEST_FAULTS and t.requested and not token_bad:
            errs = [d for d in lines if "error" in d]
            if len(errs) != 1 or readings:
                if special != "readonly":
                    self.note("fault_not_one_gap", i, f"{t.fault}: {lines!r}"[:300])
            else:
                want = {"401": "auth_expired", "429": "rate_limited"}.get(t.fault)
                if want and errs[0].get("reason") != want:
                    self.note("wrong_reason", i, f"{t.fault}: {errs[0].get('reason')}")
                if t.fault != "429" and t.fault != "401" and errs[0].get("reason"):
                    self.note("unexpected_reason", i, f"{t.fault}: {errs[0].get('reason')}")
            if t.rc != 4:
                self.note("fault_not_exit_4", i, f"{t.fault}: rc={t.rc}")
        if t.fault is None and t.requested and special not in ("readonly",) and i > self.token_bad_until:
            if t.rc == 0 and not readings:
                self.note("lost_reading", i, f"{special}: rc 0, requested, no reading")
            if t.rc != 0 and not (rig.cfg / ".credentials.json").read_text().count("expired"):
                self.note("healthy_request_failed", i, f"{special}: rc={t.rc} lines={lines!r}"[:300])

        # Our own model of when a call is due: after any 429 the block is min(retry, 1200) s
        # from the tick that logged it (taken from the log line's own stamp).
        sim_now = t.at.astimezone(timezone.utc)
        for d in lines:
            if d.get("reason") == "rate_limited":
                ra = float(d.get("retry_after_s", 0))
                self.backoff_until = datetime.fromisoformat(d["ts"]) + timedelta(seconds=min(ra, 1200))
        if t.requested:
            self.last_call_at = rig.clock
        if readings:
            self.last_reading_at = rig.clock
        if self.last_reading_at is not None:
            stall = rig.clock - self.last_reading_at
            if stall > self.longest_stall:
                self.longest_stall = stall
        # A healthy tick (no fault, valid token, clock not stepped) that made no call
        # while no 429 block or spacing explains it.
        if (not t.requested and t.fault is None and special in (None, "idle", "reset_five", "reset_seven",
                                                                  "tz", "jitter_long")
                and i > self.token_bad_until):
            blocked = self.backoff_until is not None and sim_now < self.backoff_until
            spaced = ((self.last_call_at is not None and (rig.clock - self.last_call_at).total_seconds() < 110)
                      or (self.last_ts is not None and (sim_now - self.last_ts).total_seconds() < 110))
            behind = self.last_ts is not None and self.last_ts > sim_now
            if not (blocked or spaced or behind):
                self.note("unexplained_skip", i, f"{special}: rc={t.rc} last_ts={self.last_ts} "
                          f"backoff_until={self.backoff_until} last_call={self.last_call_at}")
            if behind and (self.last_ts - sim_now).total_seconds() > 1200:
                self.note("stalled_by_future_line", i, f"last_ts={self.last_ts} now={sim_now}")

    # -- the passive join over every segment ---------------------------------------
    def join_check(self, label: str) -> dict:
        files = list(self.segments) + ([self.rig.log] if self.rig.log.exists() else [])
        samples = []
        n_read = 0
        for f in files:
            with open(f, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            for raw in lines:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(d, dict) and (d.get("five_hour") or {}).get("utilization") is not None:
                    n_read += 1
            try:
                samples += parse_log("meter", lines)
            except Exception as e:  # noqa: BLE001
                self.note("join_parse_crash", self.ticks_done, f"{label} {f.name}: {e!r}")
        if len(samples) != n_read:
            self.note("join_dropped_readings", self.ticks_done, f"{label}: {len(samples)} samples vs {n_read} rows")
        hist5 = [w for k, w in self.rig.meter.history if k == "five"]
        try:
            points = window_points(samples)
        except Exception as e:  # noqa: BLE001
            self.note("join_crash", self.ticks_done, f"{label}: {e!r}")
            return {}
        ends = []
        for p in points:
            end = datetime.fromisoformat(p["window_ending"])
            ends.append(end)
            w = next((w for w in hist5 if same_reset(w.resets_at.isoformat(), end.isoformat())), None)
            if w is None or not p["reset_verified"]:
                self.note("join_point_on_no_true_reset", self.ticks_done, f"{label}: {p}")
                continue
            if p["five_hour_pct"] > round(w.used, 1) + 0.05:
                self.note("join_point_exceeds_truth", self.ticks_done,
                          f"{label}: {p['five_hour_pct']} > {w.used} for {end}")
        # Two points for one five-hour window are by design when a weekly reset fell inside
        # it (join.py never pools across one); otherwise the window was split.
        hist7 = [w.resets_at - timedelta(days=7) for k, w in self.rig.meter.history if k == "seven"]
        groups: dict = defaultdict(list)
        for p, end in zip(points, ends):
            w = next((w for w in hist5 if same_reset(w.resets_at.isoformat(), end.isoformat())), None)
            if w is not None:
                groups[id(w)].append((w, p))
        for items in groups.values():
            w = items[0][0]
            total = sum(p["five_hour_pct"] for _, p in items)
            if total > round(w.used, 1) + 0.05 * len(items):
                self.note("join_window_total_exceeds_truth", self.ticks_done, f"{label}: {total} > {w.used}")
            if len(items) > 1:
                start = w.resets_at - timedelta(hours=5)
                weekly_inside = any(start <= s7 <= w.resets_at for s7 in hist7)
                key = w.resets_at.isoformat()
                if not weekly_inside and key not in self.splits_seen:
                    self.splits_seen.add(key)
                    self.note("join_window_split", self.ticks_done,
                              f"{label}: window ending {key} gives {len(items)} points {[p for _, p in items]}")
        return {"samples": len(samples), "points": len(points)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=20261002)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--join-every", type=int, default=720, help="ticks between join checks")
    ap.add_argument("--wall-s", type=float, default=2400, help="stop early (and report) after this much wall time")
    a = ap.parse_args()
    s = Soak(a.root, a.seed)
    started = time.monotonic()
    joins = []
    stopped = "completed"

    def report() -> None:
        out = {"seed": a.seed, "ticks_planned": a.ticks, "ticks_done": s.ticks_done, "stopped": stopped,
               "wall_s": round(time.monotonic() - started, 1),
               "sim_start": "2026-10-01T22:00:00+00:00", "sim_end": stamp(s.rig.clock),
               "sim_days": round((s.rig.clock - datetime(2026, 10, 1, 22, tzinfo=timezone.utc)).total_seconds() / 86400, 2),
               "requests": s.rig.api.requests, "request_faults": dict(s.faults), "rig_faults": dict(s.events),
               "five_hour_windows": sum(1 for k, _ in s.rig.meter.history if k == "five"),
               "seven_day_windows": sum(1 for k, _ in s.rig.meter.history if k == "seven"),
               "segments": len(s.segments), "longest_gap_between_readings": str(s.longest_stall),
               "joins": joins[-3:], "anomaly_counts": dict(s.counts), "anomalies": dict(s.anoms)}
        a.out.write_text(json.dumps(out, indent=1, default=str))

    try:
        with s.rig.running():
            for i in range(a.ticks):
                s.step(i)
                if i and i % a.join_every == 0:
                    joins.append({"tick": i, **s.join_check(f"tick {i}")})
                    report()
                    print(f"tick {i} sim {stamp(s.rig.clock)} wall {time.monotonic() - started:.0f}s "
                          f"anoms {dict(s.counts)}", flush=True)
                if time.monotonic() - started > a.wall_s:
                    stopped = "wall budget"
                    break
    finally:
        joins.append({"tick": s.ticks_done, **s.join_check("final")})
        report()
    print(json.dumps(dict(s.counts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
