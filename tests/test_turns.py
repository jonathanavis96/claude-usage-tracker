import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker.turns import (
    iter_turns,
    model_family_id,
    normalize_model,
    session_tokens_by_model,
    transcript_paths,
)


def rec(mid, ts="2026-09-05T20:20:48.817Z", model="claude-fable-5-1", **usage):
    u = {"input_tokens": 2, "cache_creation_input_tokens": 41398, "cache_read_input_tokens": 24483, "output_tokens": 96}
    u.update(usage)
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": model, "usage": u}})

class TurnTests(unittest.TestCase):
    def test_dedups_same_message_id_across_blocks_and_files(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d, "a.jsonl")
            b = Path(d, "b.jsonl")
            a.write_text("\n".join([rec("m1"), rec("m1"), json.dumps({"type": "user", "timestamp": "x"}), "garbage", rec("m2", model="claude-sonnet-5")]) + "\n")
            b.write_text(rec("m1") + "\n")
            turns = list(iter_turns([a, b]))
        self.assertEqual([t.model for t in turns], ["claude-fable-5-1", "claude-sonnet-5"])
        t = turns[0]
        self.assertEqual((t.input, t.output, t.cache_read, t.cache_write), (2, 96, 24483, 41398))
        self.assertEqual(t.total, 2 + 96 + 24483 + 41398)
        self.assertEqual(t.ts, datetime(2026, 9, 5, 20, 20, 48, 817000, tzinfo=timezone.utc))

    def test_final_usage_of_a_streamed_message_not_its_first_line(self):
        # The shape Claude Code writes for a response that opens with a thinking block: the
        # thinking block's line carries the stream's opening usage (output 5), the tool-use
        # line the final one (output 1921), and a later echo reads zero. Reading the first
        # line counted 5 output tokens for a message that spent 1921.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "a.jsonl")
            p.write_text("\n".join([
                rec("m1", ts="2026-09-23T10:00:00Z", model="claude-sonnet-5", output_tokens=5),
                rec("m1", ts="2026-09-23T10:00:09Z", model="claude-sonnet-5", output_tokens=1921,
                    output_tokens_details={"thinking_tokens": 1665}),
                rec("m1", ts="2026-09-23T10:00:10Z", model="claude-sonnet-5", input_tokens=0,
                    output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0),
                rec("m2", ts="2026-09-23T10:01:00Z", output_tokens=96),
            ]) + "\n")
            turns = list(iter_turns([p]))
        self.assertEqual([(t.id, t.output) for t in turns], [("m1", 1921), ("m2", 96)])
        t = turns[0]
        self.assertEqual((t.input, t.cache_read, t.cache_write), (2, 24483, 41398))
        self.assertEqual(t.ts, datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc))

    def test_a_teleported_cloud_turn_is_marked_remote(self):
        # The line shape `claude --teleport` writes into the local transcript for a cloud
        # session's turn (gs, 2026-09-27, anonymised): `remoteSourced` set, no `requestId`.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "a.jsonl")
            cloud = json.loads(rec("m1", ts="2026-09-27T12:41:55.472Z", model="claude-opus-5-5"))
            cloud.update({"remoteSourced": True, "entrypoint": "cli", "cwd": "/tmp/claude-1000/cgrab.x"})
            p.write_text("\n".join([json.dumps(cloud), rec("m2", ts="2026-09-27T12:50:00Z")]) + "\n")
            turns = list(iter_turns([p]))
        self.assertEqual([(t.id, t.remote) for t in turns], [("m1", True), ("m2", False)])

    def test_a_headless_runs_own_turn_is_headless_and_its_sub_agents_are_not(self):
        # `entrypoint` is on every line; a sub-agent's transcript sits under `subagents/`.
        with tempfile.TemporaryDirectory() as d:
            main = Path(d, "s.jsonl")
            sub = Path(d, "s", "subagents", "agent-1.jsonl")
            sub.parent.mkdir(parents=True)

            def line(mid, entrypoint):
                r = json.loads(rec(mid))
                r["entrypoint"] = entrypoint
                return json.dumps(r)
            main.write_text("\n".join([line("h", "sdk-cli"), line("i", "cli")]) + "\n")
            sub.write_text(line("s", "sdk-cli") + "\n")
            turns = {t.id: t for t in iter_turns([main, sub])}
        self.assertEqual({k: (t.entrypoint, t.subagent, t.headless) for k, t in turns.items()},
                         {"h": ("sdk-cli", False, True), "i": ("cli", False, False),
                          "s": ("sdk-cli", True, False)})

    def test_paths_filtered_by_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d, "sub", "old.jsonl")
            old.parent.mkdir()
            old.write_text("")
            new = Path(d, "new.jsonl")
            new.write_text("")
            os.utime(old, (0, 0))
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.assertEqual(transcript_paths(Path(d), since), [new])
            self.assertEqual(set(transcript_paths(Path(d), None)), {old, new})


class NormalizeModelTests(unittest.TestCase):
    def test_canonical_ids_pass_through(self):
        for m in ("claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"):
            self.assertEqual(normalize_model(m), m)

    def test_older_priced_ids_pass_through(self):
        # Priced from their own rows on the pricing page (issue #63).  Before this they
        # returned None, and any stretch that touched one of them was dropped whole.
        for m in ("claude-opus-5-5", "claude-opus-4-8", "claude-opus-4-7",
                  "claude-sonnet-4-6", "claude-haiku-4-5"):
            self.assertEqual(normalize_model(m), m)

    def test_older_priced_ids_keep_their_own_identity(self):
        # Opus 4.7 and 4.8 list at exactly Opus 5's prices but are not aliased onto it:
        # a stretch's tokens must still say which model spent them.
        self.assertEqual(normalize_model("claude-opus-4-7[1m]"), "claude-opus-4-7")
        self.assertEqual(normalize_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_date_suffix_stripped(self):
        self.assertEqual(normalize_model("claude-sonnet-5-20250929"), "claude-sonnet-5")

    def test_1m_marker_stripped(self):
        self.assertEqual(normalize_model("claude-sonnet-5[1m]"), "claude-sonnet-5")

    def test_unpriced_ids_still_dropped(self):
        # `<synthetic>` has no row on the pricing page and never gets one; it costs
        # nothing because every `<synthetic>` turn carries zero tokens.  A model id
        # newer than the table is the case this guard exists for.
        for m in ("<synthetic>", "unknown", "claude-opus-9", "claude-sonnet-9-9", "claude-sonnet-9-9-20991231"):
            self.assertIsNone(normalize_model(m))

    def test_model_family_id_keeps_any_claude_id(self):
        # Issue #116: speed needs a model's id whether or not it has a price yet.
        self.assertEqual(model_family_id("claude-sonnet-9-9-20991231"), "claude-sonnet-9-9")
        self.assertEqual(model_family_id("claude-sonnet-5-5[1m]"), "claude-sonnet-5-5")
        self.assertEqual(model_family_id("claude-fable-5-20260301"), "claude-fable-5-1")
        self.assertEqual(model_family_id("claude-opus-5"), "claude-opus-5")
        for m in ("<synthetic>", "unknown", "", None, "gpt-5"):
            self.assertIsNone(model_family_id(m))

    def test_fable_5_is_priced_as_fable_5_1(self):
        # Same list price for input, output and cache_write; cache_read differs but the
        # meter weights that class at 0.0. Sub-agents on gs still run it.
        self.assertEqual(normalize_model("claude-fable-5"), "claude-fable-5-1")
        self.assertEqual(normalize_model("claude-fable-5-20260301"), "claude-fable-5-1")


class SessionTokensByModelTests(unittest.TestCase):
    def test_groups_by_file_sums_and_assigns_top_model(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
            a = Path(d, "a.jsonl")
            # Two turns, both sonnet: session total 2000, assigned to sonnet.
            a.write_text("\n".join([
                rec("m1", ts=recent, model="claude-sonnet-5", output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=500),
                rec("m2", ts=recent, model="claude-sonnet-5", output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=500),
            ]) + "\n")
            b = Path(d, "b.jsonl")
            # One turn only -- dropped (needs at least 2 turns).
            b.write_text(rec("m3", ts=recent, model="claude-sonnet-5") + "\n")
            result = session_tokens_by_model([a, b], now=now)
        self.assertEqual(result, {"claude-sonnet-5": 2000})

    def test_drops_sessions_outside_window(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            stale = (now - timedelta(days=40)).isoformat().replace("+00:00", "Z")
            a = Path(d, "a.jsonl")
            a.write_text("\n".join([rec("m1", ts=stale), rec("m2", ts=stale)]) + "\n")
            result = session_tokens_by_model([a], now=now)
        self.assertEqual(result, {})

    def test_drops_sessions_whose_top_model_does_not_normalize(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
            a = Path(d, "a.jsonl")
            a.write_text("\n".join([
                rec("m1", ts=recent, model="<synthetic>", output_tokens=900, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=900),
                rec("m2", ts=recent, model="claude-sonnet-5", output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=1),
            ]) + "\n")
            result = session_tokens_by_model([a], now=now)
        self.assertEqual(result, {})

    def test_median_across_multiple_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            recent = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
            paths = []
            for i, out in enumerate((100, 300, 500)):
                p = Path(d, f"s{i}.jsonl")
                p.write_text("\n".join([
                    rec(f"a{i}", ts=recent, model="claude-opus-5", output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=0),
                    rec(f"b{i}", ts=recent, model="claude-opus-5", output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, input_tokens=0),
                ]) + "\n")
                paths.append(p)
            result = session_tokens_by_model(paths, now=now)
        self.assertEqual(result, {"claude-opus-5": 300})


class MalformedTranscriptTests(unittest.TestCase):
    def test_bad_lines_skip_that_line_only(self):
        from tracker.turns import turns_in
        lines = [{"type": "assistant", "timestamp": "2026-09-05T20:20:48Z", "message": "oops"},
                 {"type": "assistant", "timestamp": "garbage", "message": {"id": "a", "usage": {"input_tokens": 1}}},
                 {"type": "assistant", "timestamp": "2026-09-05T20:20:48Z",
                  "message": {"id": "b", "usage": {"input_tokens": "lots"}}},
                 json.loads(rec("ok"))]
        self.assertEqual([t.id for t in turns_in(lines, set())], ["ok"])

    def test_transcript_deleted_mid_scan_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "s.jsonl")
            p.write_text(rec("m1") + "\n")
            gone = Path(d, "gone.jsonl")
            self.assertEqual(len(list(iter_turns([gone, p]))), 1)
