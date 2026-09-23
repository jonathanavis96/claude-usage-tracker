import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from tracker.gs_passive import (
    JWORK_CEILING_SINCE,
    Account,
    gs_accounts,
    load_samples,
    main,
    passive_dollar_readings,
    probe_readings,
    report,
    transcript_files,
)
from tracker.publish import usd_per_pct

T0 = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
DOLLAR = 400_000  # Sonnet cache-write tokens worth exactly $1 of meter value
PRICES = {"claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5},
          "claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25}}


def turn_line(ts, mid, cw=DOLLAR, model="claude-sonnet-5"):
    return json.dumps({"type": "assistant", "timestamp": ts.isoformat().replace("+00:00", "Z"), "sessionId": "s",
                       "message": {"id": mid, "model": model,
                                   "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                             "cache_creation_input_tokens": cw}}})


def dave_home(root: Path, stretches=8, withheld_stretch=None) -> Path:
    """Dave's meter rising 2% every five minutes, $1 of transcripts per reading: $0.50 per 1%."""
    home = root / "home"
    dave = home / ".claude-dave"
    for d in ("projects/-proj-a", "projects/-proj-b"):
        (dave / d).mkdir(parents=True)
    (dave / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-dave"}}))
    ops = home / ".paperclip" / "ops"
    ops.mkdir(parents=True)
    meter = []
    for i in range(stretches * 5 + 1):
        meter.append(json.dumps({"ts": (T0 + timedelta(minutes=5 * i)).isoformat(), "account": "dave", "identity": "x",
                                 "five_hour": {"utilization": 2.0 * i, "resets_at": "2026-09-14T13:00:00+00:00"},
                                 "seven_day": {"utilization": float(i // 3), "resets_at": "2026-09-17T23:00:00+00:00"}}))
    (ops / "claude-usage-meter-dave.log").write_text("\n".join(meter) + "\n")
    kept, held = [], []
    for i in range(stretches * 5):
        (held if i // 5 == withheld_stretch else kept).append(turn_line(T0 + timedelta(minutes=5 * i + 1), f"m{i}"))
    (dave / "projects/-proj-a/s1.jsonl").write_text("\n".join(kept) + "\n")
    (dave / "projects/-proj-b/s2.jsonl").write_text("\n".join(held) + "\n")
    return home


def jwork_home(root: Path) -> Path:
    home = root / "home"
    shared = home / ".claude" / "projects" / "-proj"
    shared.mkdir(parents=True)
    for name in (".claude-javiswork", ".claude-jono"):
        (home / name).mkdir()
        (home / name / "projects").symlink_to(home / ".claude" / "projects")
    ops = home / ".paperclip" / "ops"
    ops.mkdir(parents=True)
    before = [f"{(JWORK_CEILING_SINCE - timedelta(minutes=5 * k)).isoformat()} ok five_hour=90% seven_day=31%"
              for k in range(12, 0, -1)]
    after = [f"{(JWORK_CEILING_SINCE + timedelta(minutes=1 + 5 * i)).isoformat()} ok five_hour={2 * i}% seven_day={i // 3}%"
             for i in range(41)]
    (ops / "gs-usage-ceiling.log").write_text("\n".join(before + ["x usage read FAILED — failing open"] + after) + "\n")
    turns = [turn_line(JWORK_CEILING_SINCE + timedelta(minutes=2 + 5 * i), f"j{i}") for i in range(40)]
    (shared / "s.jsonl").write_text("\n".join(turns) + "\n")
    return home


class AccountTests(unittest.TestCase):
    def test_the_account_mapping_lives_in_one_place(self):
        a = gs_accounts(Path("/h"))
        self.assertEqual(a["dave"].config_dir, Path("/h/.claude-dave"))
        self.assertEqual(a["dave"].meter_log, Path("/h/.paperclip/ops/claude-usage-meter-dave.log"))
        self.assertEqual(a["jwork"].config_dir, Path("/h/.claude-javiswork"))
        # jwork moves to a reset-bearing log of its own (audit finding 10); the ceiling
        # log it was read from stays its legacy source until that log has readings.
        self.assertEqual(a["jwork"].meter_log, Path("/h/.paperclip/ops/claude-usage-meter-jwork.log"))
        self.assertEqual((a["jwork"].meter_format, a["jwork"].legacy_meter_log),
                         ("meter", Path("/h/.paperclip/ops/gs-usage-ceiling.log")))
        self.assertEqual(a["jwork"].meter_since, JWORK_CEILING_SINCE)
        self.assertNotEqual(a["dave"].meter_log, a["jwork"].meter_log)
        self.assertEqual(a["avis"].config_dir, Path("/h/.claude-avis"))
        self.assertEqual(a["avis"].meter_log, Path("/h/.paperclip/ops/claude-usage-meter-avis.log"))
        self.assertEqual(a["avis"].meter_format, "meter")
        self.assertIsNone(a["avis"].legacy_meter_log)  # no log predates meter_log.py for this account


class TranscriptFilesTests(unittest.TestCase):
    """The pooled-projects filter (review finding, real jwork data): projects/ symlinked
    to ~/.claude keeps the bare and jono-jono accounts' transcripts alongside jwork's own
    unless session-env tells them apart -- contrib/sample.py's own_session_filter rule,
    restated here since tracker/ does not import contrib/."""

    def _account(self, config_dir: Path) -> Account:
        return Account("x", config_dir, config_dir / "meter.log", "meter")

    def test_symlinked_root_with_session_env_keeps_only_this_logins_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shared = d / "shared-projects" / "-proj"
            shared.mkdir(parents=True)
            (shared / "own-session.jsonl").write_text(turn_line(T0, "m1") + "\n")
            (shared / "other-account-session.jsonl").write_text(turn_line(T0, "m2") + "\n")
            cfg = d / ".claude-javiswork"
            (cfg / "session-env" / "own-session").mkdir(parents=True)
            (cfg / "projects").symlink_to(d / "shared-projects")
            files, own_sessions = transcript_files(self._account(cfg), None)
            self.assertEqual([p.stem for p in files], ["own-session"])
            self.assertEqual(own_sessions, {"kept": 1, "dropped": 1, "subagent_files": 0,
                                            "dropped_to": {}, "unclaimed": 1})

    def test_a_sub_agent_transcript_belongs_to_its_parent_session(self):
        # Real jwork data, 2026-09-16: `<session>/subagents/agent-<id>.jsonl` has no
        # session-env entry of its own, and matching the stem dropped all 191 of them
        # (45% of the turns). It is kept when its parent session is this login's.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shared = d / "shared-projects" / "-proj"
            (shared / "own-session" / "subagents").mkdir(parents=True)
            (shared / "other-session" / "subagents").mkdir(parents=True)
            (shared / "own-session.jsonl").write_text(turn_line(T0, "m1") + "\n")
            (shared / "own-session" / "subagents" / "agent-abc.jsonl").write_text(turn_line(T0, "m2") + "\n")
            (shared / "other-session.jsonl").write_text(turn_line(T0, "m3") + "\n")
            (shared / "other-session" / "subagents" / "agent-def.jsonl").write_text(turn_line(T0, "m4") + "\n")
            cfg = d / ".claude-javiswork"
            (cfg / "session-env" / "own-session").mkdir(parents=True)
            (cfg / "projects").symlink_to(d / "shared-projects")
            files, own_sessions = transcript_files(self._account(cfg), None)
            self.assertEqual(sorted(p.name for p in files), ["agent-abc.jsonl", "own-session.jsonl"])
            self.assertEqual(own_sessions, {"kept": 2, "dropped": 2, "subagent_files": 1,
                                            "dropped_to": {}, "unclaimed": 2})

    def test_non_symlink_root_is_untouched_even_with_a_session_env_dir(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = d / ".claude-dave"
            proj = cfg / "projects" / "-proj"
            proj.mkdir(parents=True)
            (proj / "a.jsonl").write_text(turn_line(T0, "m1") + "\n")
            (proj / "b.jsonl").write_text(turn_line(T0, "m2") + "\n")
            (cfg / "session-env" / "a").mkdir(parents=True)
            files, own_sessions = transcript_files(self._account(cfg), None)
            self.assertEqual({p.stem for p in files}, {"a", "b"})
            self.assertIsNone(own_sessions)

    def test_a_dropped_transcript_is_named_by_the_config_dir_that_claims_it(self):
        # Real jwork data, 2026-09-23: of 3,620 pooled transcripts in the meter window,
        # 430 are claimed by .claude-avis (another account, 3.09 billion tokens) and
        # 1,407 are claimed by nobody. One `dropped` count cannot tell those apart, and
        # they mean opposite things -- see docs/findings-2026-09-23-unaccounted.md.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shared = d / ".claude" / "projects" / "-proj"
            shared.mkdir(parents=True)
            for stem in ("mine", "avis-session", "nobodys-session"):
                (shared / f"{stem}.jsonl").write_text(turn_line(T0, stem) + "\n")
            cfg = d / ".claude-javiswork"
            (cfg / "session-env" / "mine").mkdir(parents=True)
            (cfg / "projects").symlink_to(d / ".claude" / "projects")
            (d / ".claude-avis" / "session-env" / "avis-session").mkdir(parents=True)
            (d / ".claude-avis" / "projects").symlink_to(d / ".claude" / "projects")
            files, own_sessions = transcript_files(self._account(cfg), None, home=d)
            self.assertEqual([p.stem for p in files], ["mine"])
            self.assertEqual(own_sessions, {"kept": 1, "dropped": 2, "subagent_files": 0,
                                            "dropped_to": {".claude-avis": 1}, "unclaimed": 1})

    def test_a_shared_root_is_filtered_even_when_this_account_owns_the_directory(self):
        # The condition that matters is that another config dir writes into the same
        # directory, not which way the symlink points. Here this account holds the real
        # `projects/` and the other login's is the link into it, so `root.is_symlink()`
        # is False and the old test let the other login's sessions through.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = d / ".claude-javiswork"
            proj = cfg / "projects" / "-proj"
            proj.mkdir(parents=True)
            for stem in ("mine", "theirs"):
                (proj / f"{stem}.jsonl").write_text(turn_line(T0, stem) + "\n")
            (cfg / "session-env" / "mine").mkdir(parents=True)
            other = d / ".claude-jono"
            (other / "session-env" / "theirs").mkdir(parents=True)
            (other / "projects").symlink_to(cfg / "projects")
            files, own_sessions = transcript_files(self._account(cfg), None, home=d)
            self.assertEqual([p.stem for p in files], ["mine"])
            self.assertEqual(own_sessions, {"kept": 1, "dropped": 1, "subagent_files": 0,
                                            "dropped_to": {".claude-jono": 1}, "unclaimed": 0})

    def test_symlinked_root_with_no_session_env_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            shared = d / "shared-projects" / "-proj"
            shared.mkdir(parents=True)
            (shared / "a.jsonl").write_text(turn_line(T0, "m1") + "\n")
            cfg = d / ".claude-javiswork"
            cfg.mkdir()
            (cfg / "projects").symlink_to(d / "shared-projects")
            files, own_sessions = transcript_files(self._account(cfg), None)
            self.assertEqual([p.stem for p in files], ["a"])
            self.assertIsNone(own_sessions)


class ReportTests(unittest.TestCase):
    def test_each_account_is_valued_against_its_own_meter(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            r = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))["accounts"]["dave"]
        self.assertEqual([s["status"] for s in r["stretches"]], ["accepted"] * 8)
        self.assertAlmostEqual(r["stretches"][0]["usd_per_pct"], 0.5)
        self.assertEqual(r["daily"], [{"date": "2026-09-14", "usd_per_pct": 0.5, "delta_pct": 80.0, "stretches": 8,
                                       "rounding": 0.0125, "bounds": [0.4938, 0.5063], "pieces": 1,
                                       "reset_verified": True, "capture_diagnosed": 0}])
        self.assertEqual(r["state"], {"state": "ok"})
        self.assertEqual(r["last_usable_at"], (T0 + timedelta(minutes=200)).isoformat())
        self.assertEqual(r["spread"]["stretch"]["n"], 8)
        self.assertTrue(r["weekly_by_window"])

    def test_fast_mode_requests_are_left_out_of_their_stretch_and_recorded(self):
        def opus_session(start_min, n, rate, prefix):
            lines, t = [], T0 + timedelta(minutes=start_min)
            for i in range(n):
                end = t + timedelta(seconds=600 / rate)
                lines.append(json.dumps({"type": "user", "timestamp": t.isoformat().replace("+00:00", "Z")}))
                lines.append(json.dumps({"type": "assistant", "timestamp": end.isoformat().replace("+00:00", "Z"),
                                         "message": {"id": f"{prefix}{i}", "model": "claude-opus-5",
                                                     "usage": {"input_tokens": 3, "output_tokens": 600,
                                                               "cache_read_input_tokens": 0,
                                                               "cache_creation_input_tokens": 0}}}))
                t = end + timedelta(seconds=2)
            return "\n".join(lines) + "\n"

        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            projects = home / ".claude-dave" / "projects" / "-proj-a"
            (projects / "fast.jsonl").write_text(opus_session(1, 20, 170, "f"))
            (projects / "std.jsonl").write_text(opus_session(101, 40, 70, "s"))
            r = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))["accounts"]["dave"]
        first, fifth = r["stretches"][0], r["stretches"][4]
        self.assertEqual(first["fast_mode_tokens"], {"claude-opus-5": {"input": 60, "output": 12_000,
                                                                       "cache_read": 0, "cache_write": 0}})
        self.assertEqual(first["fast_mode_turns"], 20)
        self.assertEqual(list(first["tokens"]), ["claude-sonnet-5"])
        self.assertAlmostEqual(first["usd_per_pct"], 0.5)
        self.assertEqual((fifth["fast_mode_tokens"], fifth["fast_mode_turns"]), ({}, 0))
        self.assertEqual(fifth["tokens"]["claude-opus-5"]["output"], 40 * 600)

    def test_with_the_gate_off_a_stretch_the_transcripts_leave_empty_is_still_not_published(self):
        # CAPTURE_GATE is False (2026-09-16): the check still judges every stretch and
        # its verdict is kept as capture_status. A stretch outside the 15% band is
        # published all the same (next test), but one the transcripts leave empty
        # (capture under COLLECTION_GAP: the meter moved, gs saw nothing) is not.
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d), withheld_stretch=6)
            full = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))
            cut = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1),
                         withhold={"dave": ["-proj-b/*"]})
        self.assertFalse(cut["capture_gate"])
        self.assertEqual(full["accounts"]["dave"]["stretches"][6]["capture_status"], "accepted")
        held = cut["accounts"]["dave"]["stretches"][6]
        self.assertEqual((held["status"], held["capture_status"], held["capture"]), ("unaccounted", "unaccounted", 0.0))
        self.assertEqual(cut["accounts"]["dave"]["transcripts"]["withheld_patterns"], ["-proj-b/*"])
        self.assertEqual([(r["kind"], r["stretches"]) for r in cut["accounts"]["dave"]["runs"]], [("collection_gap", 1)])
        self.assertEqual(cut["accounts"]["dave"]["state"], {"state": "ok"})
        readings = passive_dollar_readings(cut, PRICES, by="stretch")
        self.assertEqual(len(readings), 7)
        self.assertNotIn(datetime.fromisoformat(held["end"]), [t for t, _ in readings])
        self.assertEqual([round(v, 6) for _, v in passive_dollar_readings(cut, PRICES)], [50.0])

    def test_with_the_gate_off_a_stretch_outside_the_band_is_published(self):
        # Three of stretch 6's five $1 turns withheld: capture 0.4, outside the 15% band
        # (capture_status unaccounted) yet far above COLLECTION_GAP, so it is published
        # and the day's pooled figure carries it: 80%, $37 -> $46.25 per window.
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d), withheld_stretch=6)
            dave = home / ".claude-dave" / "projects" / "-proj-b" / "s2.jsonl"
            lines = dave.read_text().splitlines()
            (home / ".claude-dave" / "projects" / "-proj-b" / "s3.jsonl").write_text("\n".join(lines[len(lines) // 2:]) + "\n")
            dave.write_text("\n".join(lines[:len(lines) // 2]) + "\n")
            cut = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1),
                         withhold={"dave": ["-proj-b/s3.jsonl"]})
        half = cut["accounts"]["dave"]["stretches"][6]
        self.assertEqual((half["status"], half["capture_status"]), ("accepted", "unaccounted"))
        self.assertAlmostEqual(half["capture"], 0.4, places=2)
        self.assertEqual(len(passive_dollar_readings(cut, PRICES, by="stretch")), 8)
        self.assertEqual([round(v, 6) for _, v in passive_dollar_readings(cut, PRICES)], [46.25])

    def test_with_the_gate_on_a_withheld_stretch_never_reaches_the_publisher(self):
        from unittest import mock

        import tracker.gs_passive as gp
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gp, "CAPTURE_GATE", True):
            home = dave_home(Path(d), withheld_stretch=6)
            cut = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1),
                         withhold={"dave": ["-proj-b/*"]})
            held = cut["accounts"]["dave"]["stretches"][6]
            self.assertEqual((held["status"], held["capture"]), ("unaccounted", 0.0))
            readings = passive_dollar_readings(cut, PRICES, by="stretch")
        self.assertTrue(cut["capture_gate"])
        self.assertEqual(len(readings), 7)
        self.assertNotIn(datetime.fromisoformat(held["end"]), [t for t, _ in readings])
        self.assertEqual([round(v, 6) for _, v in passive_dollar_readings(cut, PRICES)], [50.0])

    def test_the_report_carries_the_accounts_class_split(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            r = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))["accounts"]["dave"]
        self.assertEqual(r["split"], {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 1.0})

    def test_fit_output_weight_recovers_the_weight_the_stretches_were_valued_at(self):
        from tracker.gs_passive import fit_output_weight
        # Stretch A: $2 of cache_write moves 10%; stretch B: $1 cache_write + $1 output moves 10%.
        # Both classes move the meter alike, so the fitted output weight is 1.0.
        rpt = {"accounts": {"x": {"stretches": [
            {"status": "accepted", "delta_pct": 10, "tokens": {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 800_000}}},
            {"status": "accepted", "delta_pct": 10, "tokens": {"claude-sonnet-5": {"input": 0, "output": 100_000, "cache_read": 0, "cache_write": 400_000}}},
            {"status": "unpriced", "delta_pct": 10, "tokens": {"claude-sonnet-5": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 1}}},
        ]}}}
        fit = fit_output_weight(rpt, PRICES)
        self.assertEqual(fit["n"], 2)
        self.assertAlmostEqual(fit["output_weight"], 1.0, places=3)
        self.assertAlmostEqual(fit["usd_per_pct_at_fit"], 0.2, places=3)

    def test_a_stretch_with_a_model_the_prices_do_not_cover_is_left_out_not_valued_at_zero(self):
        # Review finding on #41: `bundle_meter_usd(...) or 0.0` made an unpriced model's
        # spend read as $0, deflating the day's reading. Now the stretch is dropped whole.
        import copy
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            r = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))
        tainted = copy.deepcopy(r)
        stretch = tainted["accounts"]["dave"]["stretches"][3]
        stretch["tokens"]["claude-haiku-4-5-20251001"] = {"input": 5_000_000, "output": 0, "cache_read": 0, "cache_write": 0}
        full = passive_dollar_readings(r, PRICES, by="stretch")
        readings = passive_dollar_readings(tainted, PRICES, by="stretch")
        self.assertEqual(len(readings), len(full) - 1)
        self.assertNotIn(datetime.fromisoformat(stretch["end"]), [t for t, _ in readings])
        self.assertEqual([round(v, 6) for _, v in passive_dollar_readings(tainted, PRICES)], [50.0])

    def test_published_readings_are_meter_dollars_per_window_at_the_prices_they_are_published_with(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            r = report({"dave": gs_accounts(home)["dave"]}, PRICES, now=T0 + timedelta(days=1))
        dearer = {m: dict(p, cache_write=p["cache_write"] * 2) for m, p in PRICES.items()}
        self.assertEqual([(t, round(v, 6)) for t, v in passive_dollar_readings(r, PRICES)],
                         [(T0 + timedelta(minutes=200), 50.0)])
        self.assertEqual([round(v, 6) for _, v in passive_dollar_readings(r, dearer)], [100.0])

    def test_jwork_readings_from_before_the_ceiling_read_jwork_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            home = jwork_home(Path(d))
            r = report({"jwork": gs_accounts(home)["jwork"]}, PRICES, now=T0)["accounts"]["jwork"]
        self.assertEqual(r["meter"]["samples"], 41)
        self.assertGreaterEqual(datetime.fromisoformat(r["meter"]["first"]), JWORK_CEILING_SINCE)
        self.assertEqual(len(r["stretches"]), 8)
        # Read from the ceiling log alone: no reset ids, so nothing is certified.
        self.assertEqual(r["meter"]["reset_verified_samples"], 0)
        self.assertEqual({s["reset_verified"] for s in r["stretches"]}, {False})
        self.assertEqual(passive_dollar_readings({"accounts": {"jwork": r}}, PRICES), [])
        self.assertTrue(passive_dollar_readings({"accounts": {"jwork": r}}, PRICES, allow_legacy_unverified=True))

    def test_jworks_reset_bearing_log_takes_over_from_its_first_reading(self):
        with tempfile.TemporaryDirectory() as d:
            home = jwork_home(Path(d))
            first = JWORK_CEILING_SINCE + timedelta(minutes=101)
            resets = (first + timedelta(hours=4)).isoformat()
            lines = [json.dumps({"ts": (first + timedelta(minutes=5 * i)).isoformat(), "account": "jwork",
                                 "five_hour": {"utilization": 40 + 2 * i, "resets_at": resets},
                                 "seven_day": {"utilization": 20 + i // 3, "resets_at": "2026-09-11T04:00:00+00:00"}})
                     for i in range(20)]
            (home / ".paperclip" / "ops" / "claude-usage-meter-jwork.log").write_text("\n".join(lines) + "\n")
            samples = load_samples(gs_accounts(home)["jwork"])
        self.assertEqual(sum(1 for s in samples if s.resets_at is None), 20)   # ceiling readings before `first`
        self.assertEqual(sum(1 for s in samples if s.resets_at is not None), 20)
        self.assertTrue(all(a.ts < b.ts for a, b in pairwise(samples)))

    def test_load_samples_tags_an_inferred_reset_and_never_overwrites_a_logged_one(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ops = home / ".paperclip" / "ops"
            ops.mkdir(parents=True)
            since = T0
            before = [f"{(since + timedelta(minutes=5 * i)).isoformat()} ok five_hour={40 - 4 * i}% seven_day=10%"
                      for i in range(10)]  # falls to 0%, then climbs -- a resolvable reset
            after = [f"{(since + timedelta(minutes=5 * (10 + i))).isoformat()} ok five_hour={2 * i}% seven_day=10%"
                     for i in range(8)]
            (ops / "gs-usage-ceiling.log").write_text("\n".join(before + after) + "\n")
            account = Account("t", home / ".claude-t", ops / "claude-usage-meter-t.log", "meter",
                              meter_since=since, legacy_meter_log=ops / "gs-usage-ceiling.log")
            samples = load_samples(account)
        # Every legacy reading is still gs-ceiling-sourced or its inferred variant -- never
        # silently promoted to a source name that would claim it came from a real reset log.
        self.assertTrue(all(s.source in ("gs-ceiling", "gs-ceiling-inferred") for s in samples))
        inferred = [s for s in samples if s.source == "gs-ceiling-inferred"]
        self.assertTrue(inferred)
        self.assertTrue(all(s.resets_at is not None for s in inferred))
        # The stretch pairing rules in tracker/join.py never used the plain source name to
        # decide anything, so this changes nothing about which stretches got built --
        # only what they know about where their reset evidence came from.
        untouched = [s for s in samples if s.source == "gs-ceiling" and s.resets_at is not None]
        self.assertEqual(untouched, [])  # gs-ceiling itself never carries a real reset

    def test_stretch_records_carry_reset_source_and_withhold_verification_for_inferred_resets(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ops = home / ".paperclip" / "ops"
            projects = home / ".claude-t" / "projects" / "-p"
            projects.mkdir(parents=True)
            ops.mkdir(parents=True)
            since = T0
            # A resolvable reset (falls to 0%, climbs back with real use) followed by a plateau
            # with no further evidence, so one stretch should read "inferred" and the tail "none".
            before = [f"{(since + timedelta(minutes=5 * i)).isoformat()} ok five_hour={40 - 4 * i}% seven_day=10%"
                      for i in range(10)]
            after = [f"{(since + timedelta(minutes=5 * (10 + i))).isoformat()} ok five_hour={2 * i}% seven_day=10%"
                     for i in range(20)]
            (ops / "gs-usage-ceiling.log").write_text("\n".join(before + after) + "\n")
            turns = [turn_line(since + timedelta(minutes=5 * i + 1), f"t{i}") for i in range(29)]
            (projects / "s.jsonl").write_text("\n".join(turns) + "\n")
            account = Account("t", home / ".claude-t", ops / "claude-usage-meter-t.log", "meter",
                              meter_since=since, legacy_meter_log=ops / "gs-usage-ceiling.log")
            r = report({"t": account}, PRICES, now=since + timedelta(hours=3))["accounts"]["t"]
        sources = {st["reset_source"] for st in r["stretches"]}
        self.assertTrue(sources <= {"inferred", "none"})
        self.assertIn("inferred", sources)
        # An inferred reset is recorded, but the validation in tracker/gs_passive.py
        # INFERRED_RESET_VERIFIED did not clear the acceptance bar, so it is not certified.
        for st in r["stretches"]:
            if st["reset_source"] == "inferred":
                self.assertFalse(st["reset_verified"])
            if st["reset_source"] == "none":
                self.assertFalse(st["reset_verified"])
        # Nothing rolled up from the stretches may certify what the stretches themselves do
        # not: no day and no sample count reads as reset-verified on inferred resets alone.
        self.assertTrue(r["daily"])
        self.assertFalse(any(day["reset_verified"] for day in r["daily"]))
        self.assertEqual(r["meter"]["reset_verified_samples"], 0)

    def test_a_transcript_directory_shared_with_other_config_dirs_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            home = jwork_home(Path(d))
            r = report({"jwork": gs_accounts(home)["jwork"]}, PRICES, now=T0)["accounts"]["jwork"]
        self.assertEqual(r["transcripts"]["shared_with"], [".claude", ".claude-jono"])


class ProbeReadingTests(unittest.TestCase):
    def test_usable_probe_rows_become_dollar_readings_for_step_evidence(self):
        row = {"ts": "2026-09-14T12:00:00+00:00", "model": "claude-opus-5", "tick_from": 2, "tick_to": 5,
               "tokens": {"input": 60, "output": 150, "cache_read": 468_390, "cache_write": 814_306}}
        rows = [row, dict(row, ts="2026-09-14T13:00:00+00:00", outlier=True),
                dict(row, ts="2026-09-14T14:00:00+00:00", payload="output")]
        self.assertEqual(probe_readings(rows, PRICES),
                         [(datetime.fromisoformat(row["ts"]), usd_per_pct(row, PRICES["claude-opus-5"]))])


class CalibrateTests(unittest.TestCase):
    def test_calibrate_prints_the_ratio_of_medians_as_json(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))  # 8 accepted dave stretches -> one $50/window day at PRICES
            prices = Path(d) / "prices.json"
            prices.write_text(json.dumps(PRICES))
            probe_row = {"ts": "2026-09-14T09:00:00+00:00", "model": "claude-sonnet-5", "tick_from": 0, "tick_to": 1,
                        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 100_000}}  # $25/window
            probes = Path(d) / "probes.jsonl"
            probes.write_text(json.dumps(probe_row) + "\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--home", str(home), "--account", "dave", "--prices", str(prices),
                           "--probes", str(probes), "--calibrate",
                           "--since", "2026-09-14T00:00:00+00:00", "--until", "2026-09-15T00:00:00+00:00"])
            self.assertEqual(rc, 0)
            result = json.loads(buf.getvalue())
        self.assertEqual(result, {"ratio": 2.0, "window": ["2026-09-14", "2026-09-15"], "passive_n": 1, "probe_n": 1})

    def test_calibrate_without_since_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            prices = Path(d) / "prices.json"
            prices.write_text(json.dumps(PRICES))
            with self.assertRaises(SystemExit):
                main(["--home", str(home), "--prices", str(prices), "--probes", str(Path(d) / "none.jsonl"),
                     "--calibrate", "--until", "2026-09-15T00:00:00+00:00"])


class CliTests(unittest.TestCase):
    def test_writes_the_report(self):
        with tempfile.TemporaryDirectory() as d:
            home = dave_home(Path(d))
            prices, out = Path(d) / "prices.json", Path(d) / "out" / "passive-gs.json"
            prices.write_text(json.dumps(dict(PRICES, _source="test")))
            rc = main(["--home", str(home), "--account", "dave", "--prices", str(prices),
                       "--probes", str(Path(d) / "none.jsonl"), "--out", str(out)])
            self.assertEqual(rc, 0)
            self.assertEqual(list(json.loads(out.read_text())["accounts"]), ["dave"])


class UtcArgTests(unittest.TestCase):
    def test_a_bare_date_is_aware_utc_and_until_covers_the_whole_day(self):
        from tracker.gs_passive import _utc_arg, _utc_until
        since = _utc_arg("2026-09-05")
        until = _utc_until("2026-09-15")
        self.assertEqual(since.tzinfo, timezone.utc)
        self.assertEqual((since.hour, since.minute, since.second), (0, 0, 0))
        self.assertEqual((until.hour, until.minute, until.second), (23, 59, 59))
        # An aware sample on the 15th afternoon falls inside the window.
        self.assertTrue(since <= datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc) <= until)

    def test_an_explicit_datetime_is_kept_as_given(self):
        from tracker.gs_passive import _utc_until
        self.assertEqual(_utc_until("2026-09-15T12:00:00+00:00"), datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
