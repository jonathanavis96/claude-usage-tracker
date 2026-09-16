"""Tests for contrib/sample.py, the contributor script.

The script is one self-contained file with no package, so it is loaded from
its path. Nothing here opens a socket: the usage fetch is injected and
urllib.request.urlopen is patched to fail on any call.
"""
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location("contrib_sample", Path(__file__).resolve().parents[1] / "contrib" / "sample.py")
sample = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sample)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FIVE_RESET = NOW + timedelta(hours=2)      # window started NOW-3h
SEVEN_RESET = NOW + timedelta(days=3)      # window started NOW-4d
USAGE = {"five_hour": {"utilization": 37.0, "resets_at": FIVE_RESET.strftime("%Y-%m-%dT%H:%M:%S.5Z")},
         "seven_day": {"utilization": 12.0, "resets_at": SEVEN_RESET.strftime("%Y-%m-%dT%H:%M:%SZ")},
         "seven_day_sonnet": {"utilization": None, "resets_at": None}}

PROJECT_DIR = "-home-alice-secret-project-name"
SESSION_A = "3f1c2a9e-0000-4000-8000-aaaaaaaaaaaa"
SESSION_B = "7b8d0c1f-0000-4000-8000-bbbbbbbbbbbb"
PROMPT = "please refactor the billing module for acme-corp"
EMAIL = "alice@example.com"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def rec(mid, ts, model="claude-opus-5-20260301", inp=10, out=20, cr=300, cw=400, session=SESSION_A):
    return json.dumps({"type": "assistant", "timestamp": _iso(ts), "sessionId": session,
                       "cwd": f"/home/alice/{PROJECT_DIR}", "userType": "external",
                       "message": {"id": mid, "model": model, "role": "assistant",
                                   "content": [{"type": "text", "text": PROMPT}],
                                   "usage": {"input_tokens": inp, "output_tokens": out,
                                             "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}})


def user_rec(ts, session=SESSION_A):
    return json.dumps({"type": "user", "timestamp": _iso(ts), "sessionId": session, "cwd": f"/home/alice/{PROJECT_DIR}",
                       "message": {"role": "user", "content": PROMPT + " " + EMAIL}})


class Fixture:
    """A tmp Claude config dir with two transcripts, and a tmp id file path."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.claude_dir = root / "claude"
        proj = self.claude_dir / "projects" / PROJECT_DIR
        proj.mkdir(parents=True)
        self.path_a = proj / f"{SESSION_A}.jsonl"
        self.path_b = proj / "sub" / f"{SESSION_B}.jsonl"
        self.path_b.parent.mkdir()
        in_five = NOW - timedelta(hours=1)
        in_seven_only = NOW - timedelta(hours=6)
        before_both = NOW - timedelta(days=8)
        self.path_a.write_text("\n".join([
            user_rec(in_five),
            rec("m1", in_five),                                             # opus, both windows
            rec("m1", in_five),                                             # duplicate id, same file
            rec("m2", in_five, model="claude-sonnet-5", inp=1, out=2, cr=3, cw=4),  # sonnet, both windows
            rec("m3", in_seven_only, inp=100, out=200, cr=0, cw=0),          # opus, seven-day only
            rec("m4", before_both, inp=1000, out=1000, cr=1000, cw=1000),   # outside both
            rec("m5", in_five, model="claude-haiku-4-5-20251001", inp=7),   # not a priced model: kept by its id
            "not json at all",
            json.dumps({"type": "assistant", "timestamp": _iso(in_five), "message": {"id": "m6"}}),  # no usage
        ]) + "\n")
        self.path_b.write_text("\n".join([
            rec("m1", in_five, session=SESSION_B),                          # duplicate id across files
            rec("m7", in_five, model="claude-opus-5 [1m]", inp=5, out=5, cr=5, cw=5, session=SESSION_B),
        ]) + "\n")
        self.id_file = root / "contrib.json"
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()

    def run(self, *argv, usage=USAGE, ask=None):
        out = io.StringIO()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = sample.main(["--id-file", str(self.id_file), "--claude-dir", str(self.claude_dir), *argv],
                             usage_fetch=lambda: usage, now=NOW, ask=ask, out=out)
        return rc, out.getvalue(), err.getvalue()


def _no_socket(*a, **k):
    raise AssertionError("urlopen must not be called")


def body_from_print(text):
    start = text.index("{")
    end = text.index("\nWhat each field is:")
    return json.loads(text[start:end])


def cut1_line(text):
    return next(line for line in text.splitlines() if line.startswith("CUT1:"))


EXPECT_FIVE = {
    # Unknown models are retained (after only date/[1m] normalization) so
    # downstream pricing can withhold a monetary estimate instead of losing
    # meter-moving work.
    "claude-haiku-4-5": {"input": 7, "output": 20, "cache_read": 300, "cache_write": 400},
    "claude-opus-5": {"input": 15, "output": 25, "cache_read": 305, "cache_write": 405},   # m1 + m7
    "claude-sonnet-5": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4},        # m2
}
EXPECT_SEVEN = {
    "claude-haiku-4-5": {"input": 7, "output": 20, "cache_read": 300, "cache_write": 400},
    "claude-opus-5": {"input": 115, "output": 225, "cache_read": 305, "cache_write": 405},  # + m3
    "claude-sonnet-5": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4},
}


@mock.patch.object(urllib.request, "urlopen", _no_socket)
class BodyTests(unittest.TestCase):
    def test_first_run_prints_body_with_dedupe_and_window_cut(self):
        with Fixture() as f:
            rc, out, err = f.run("--plan", "max20", "--dry-run")
            self.assertEqual(rc, 0, err)
            body = body_from_print(out)
            self.assertEqual(body["plan"], "max20")
            self.assertEqual(body["plan_source"], "flag")
            self.assertEqual(body["ts"], NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertEqual(body["five_hour"], {"utilization": 37.0, "resets_at": USAGE["five_hour"]["resets_at"]})
            self.assertEqual(body["seven_day"], {"utilization": 12.0, "resets_at": USAGE["seven_day"]["resets_at"]})
            self.assertEqual(body["tokens_since_five_hour_reset"], EXPECT_FIVE)
            self.assertEqual(body["tokens_since_seven_day_reset"], EXPECT_SEVEN)
            self.assertEqual(body["client_version"], sample.CLIENT_VERSION)
            stored = json.loads(f.id_file.read_text())
            self.assertEqual(body["contributor_id"], stored["contributor_id"])
            self.assertEqual(stored["plan"], "max20")
            self.assertIn("What each field is:", out)
            for k in body:
                self.assertIn(f"  {k}: ", out, k)
            self.assertIn("dry run: not sending.", out)

    def test_body_field_set_matches_spec_table(self):
        with Fixture() as f:
            _, out, _ = f.run("--plan", "pro", "--dry-run")
            self.assertEqual(set(body_from_print(out)), {
                "contributor_id", "plan", "plan_source", "ts", "five_hour", "seven_day",
                "tokens_since_five_hour_reset", "tokens_since_seven_day_reset", "capture", "client_version"})
            # The sub-agent's test named an ownership value the sampler never produces. This
            # fixture's projects/ is not pooled with another profile and no filter was asked
            # for, so ownership is the unverified default, and capture carries no path.
            capture = body_from_print(out)["capture"]
            self.assertEqual(set(capture), {"collected_at", "five_hour_started_at", "seven_day_started_at", "ownership"})
            self.assertEqual(capture["ownership"], "local_transcripts_unverified")
            self.assertEqual(capture["collected_at"], NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertNotIn(str(f.claude_dir), json.dumps(capture))

    def test_body_under_2kb(self):
        with Fixture() as f:
            _, out, _ = f.run("--plan", "max5", "--dry-run")
            self.assertLess(len(sample.minify(body_from_print(out)).encode()), 2048)

    def test_no_active_window_gives_empty_token_sums(self):
        usage = {"five_hour": {"utilization": 0, "resets_at": None}, "seven_day": {"utilization": 0, "resets_at": None}}
        with Fixture() as f:
            rc, out, err = f.run("--plan", "pro", "--dry-run", usage=usage)
            self.assertEqual(rc, 0, err)
            body = body_from_print(out)
            self.assertEqual(body["five_hour"], {"utilization": 0.0, "resets_at": None})
            self.assertEqual(body["tokens_since_five_hour_reset"], {})
            self.assertEqual(body["tokens_since_seven_day_reset"], {})

    def test_compact_line_decodes_to_identical_body_and_prints_nothing_else(self):
        with Fixture() as f:
            _, printed, _ = f.run("--plan", "max20", "--dry-run")
            body = body_from_print(printed)
            self.assertEqual(sample.decode_compact_line(cut1_line(printed)), body)
            rc, out, _ = f.run("--compact")
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip().splitlines(), [cut1_line(out)])
            # second run: same body bar plan_source, which is now "stored"
            self.assertEqual(sample.decode_compact_line(out.strip()), dict(body, plan_source="stored"))
            self.assertRegex(cut1_line(out)[5:], r"^[A-Za-z0-9_\-]+=*$")


@mock.patch.object(urllib.request, "urlopen", _no_socket)
class PrivacyTests(unittest.TestCase):
    def test_serialised_body_carries_nothing_from_the_transcripts_but_counts(self):
        with Fixture() as f:
            _, out, _ = f.run("--plan", "max20", "--dry-run")
            body = body_from_print(out)
            for text in (sample.minify(body), cut1_line(out), json.dumps(sample.decode_compact_line(cut1_line(out)))):
                for secret in (str(f.path_a), str(f.path_b), PROJECT_DIR, SESSION_A, SESSION_B, PROMPT,
                               EMAIL, "alice", "acme", "cwd", "sessionId", str(f.claude_dir), "projects"):
                    self.assertNotIn(secret, text, secret)
            # and the whole printed page, minus the id file path the user was told about, is clean too
            page = out.replace(str(f.id_file), "")
            for secret in (str(f.path_a), PROJECT_DIR, SESSION_A, SESSION_B, PROMPT, EMAIL):
                self.assertNotIn(secret, page, secret)

    def test_token_never_appears_in_body(self):
        with Fixture() as f:
            (f.claude_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-SECRET"}}))
            seen = {}

            def fake_urlopen(req, timeout=None):
                seen["auth"] = req.get_header("Authorization")
                r = mock.MagicMock()
                r.__enter__.return_value.read.return_value = json.dumps(USAGE).encode()
                return r
            with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
                out = io.StringIO()
                rc = sample.main(["--id-file", str(f.id_file), "--claude-dir", str(f.claude_dir), "--plan", "pro",
                                  "--dry-run"], now=NOW, out=out)
            self.assertEqual(rc, 0)
            self.assertEqual(seen["auth"], "Bearer sk-ant-oat-SECRET")
            self.assertNotIn("SECRET", out.getvalue())


@mock.patch.object(urllib.request, "urlopen", _no_socket)
class PlanTests(unittest.TestCase):
    def test_first_run_without_plan_fails_and_writes_no_id_file(self):
        with Fixture() as f:
            rc, _out, err = f.run("--dry-run")
            self.assertEqual(rc, 2)
            self.assertIn("--plan", err)
            self.assertFalse(f.id_file.exists())

    def test_stored_plan_is_reused_on_later_runs(self):
        with Fixture() as f:
            f.run("--plan", "max5", "--dry-run")
            first_id = json.loads(f.id_file.read_text())["contributor_id"]
            rc, out, _ = f.run("--dry-run", "--print")
            body = body_from_print(out)
            self.assertEqual(rc, 0)
            self.assertEqual((body["plan"], body["plan_source"]), ("max5", "stored"))
            self.assertEqual(body["contributor_id"], first_id)

    def test_ids_are_local_profile_and_plan_scoped(self):
        with Fixture() as f:
            _, first, _ = f.run("--plan", "pro", "--dry-run")
            pro_id = body_from_print(first)["contributor_id"]
            _, second, _ = f.run("--plan", "max20", "--dry-run", "--print")
            max_id = body_from_print(second)["contributor_id"]
            self.assertNotEqual(pro_id, max_id)
            stored = json.loads(f.id_file.read_text())
            # Keyed locally by a hash of the config dir and its account (never sent), so
            # one machine's second Claude profile does not share an id (audit finding 14).
            profiles = {k.rsplit(":", 1)[0] for k in stored["identities"]}
            self.assertEqual({k.rsplit(":", 1)[1] for k in stored["identities"]}, {"pro", "max20"})
            self.assertEqual(len(profiles), 1)
            self.assertRegex(profiles.pop(), r"^[0-9a-f]{64}$")
            self.assertNotIn(str(f.claude_dir), f.id_file.read_text())
            other = Path(f.tmp.name) / "claude-other"
            (other / "projects").mkdir(parents=True)
            out = io.StringIO()
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                sample.main(["--id-file", str(f.id_file), "--claude-dir", str(other), "--plan", "pro", "--dry-run", "--print"],
                            usage_fetch=lambda: USAGE, now=NOW, out=out)
            self.assertNotIn(body_from_print(out.getvalue())["contributor_id"], (pro_id, max_id))

    @staticmethod
    def _run_profile(f, cfg, *argv):
        out = io.StringIO()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = sample.main(["--id-file", str(f.id_file), "--claude-dir", str(cfg), *argv],
                             usage_fetch=lambda: USAGE, now=NOW, out=out)
        return rc, out.getvalue(), err.getvalue()

    def test_a_0_1_0_id_file_keeps_its_contributor_id_and_plan(self):
        # Review of PR 57, round 2, finding 5: 0.1.0 wrote no profile_scope, so the
        # migration guard never matched a real old file. The upgraded sampler minted a
        # new id (breaking pairing across the upgrade) and refused the stored plan.
        legacy_id = "3f0c6a5e-1d2b-4c7a-9e8f-0a1b2c3d4e5f"
        legacy = {"contributor_id": legacy_id, "created": "2026-09-01T00:00:00Z", "plan": "max20", "plan_from_endpoint": False}
        with Fixture() as f:
            # Given the same plan again, the old sampler still minted a new id.
            f.id_file.write_text(json.dumps(legacy))
            rc, out, err = f.run("--plan", "max20", "--dry-run", "--print")
            self.assertEqual(rc, 0, err)
            self.assertEqual(body_from_print(out)["contributor_id"], legacy_id)
        with Fixture() as f:
            f.id_file.write_text(json.dumps(legacy))
            rc, out, err = f.run("--dry-run", "--print")
            self.assertEqual(rc, 0, err)
            body = body_from_print(out)
            self.assertEqual((body["contributor_id"], body["plan"], body["plan_source"]), (legacy_id, "max20", "stored"))
            # The file now names its profile, so a second profile does not inherit the id.
            other = Path(f.tmp.name) / "claude-other"
            (other / "projects").mkdir(parents=True)
            rc, out, err = self._run_profile(f, other, "--plan", "max20", "--dry-run", "--print")
            self.assertEqual(rc, 0, err)
            self.assertNotEqual(body_from_print(out)["contributor_id"], legacy_id)
            rc, out, _ = f.run("--dry-run", "--print")
            self.assertEqual(body_from_print(out)["contributor_id"], legacy_id)

    def test_each_profile_keeps_its_own_stored_plan(self):
        # Review of PR 57, round 2, finding 6: only the last run's profile kept its
        # stored plan, so alternating two profiles asked for --plan on every run.
        with Fixture() as f:
            other = Path(f.tmp.name) / "claude-other"
            (other / "projects").mkdir(parents=True)
            _, first, _ = f.run("--plan", "max20", "--dry-run", "--print")
            _, second, _ = self._run_profile(f, other, "--plan", "pro", "--dry-run", "--print")
            rc, out, err = f.run("--dry-run", "--print")
            self.assertEqual(rc, 0, err)
            body = body_from_print(out)
            self.assertEqual((body["plan"], body["plan_source"], body["contributor_id"]),
                             ("max20", "stored", body_from_print(first)["contributor_id"]))
            rc, out, err = self._run_profile(f, other, "--dry-run", "--print")
            self.assertEqual(rc, 0, err)
            body = body_from_print(out)
            self.assertEqual((body["plan"], body["contributor_id"]), ("pro", body_from_print(second)["contributor_id"]))

    def test_endpoint_plan_wins_and_is_recorded(self):
        usage = dict(USAGE, subscription_type="Claude Max 20x")
        with Fixture() as f:
            _rc, out, _ = f.run("--plan", "pro", "--dry-run", usage=usage)
            body = body_from_print(out)
            self.assertEqual((body["plan"], body["plan_source"]), ("max20", "endpoint"))
            self.assertTrue(json.loads(f.id_file.read_text())["plan_from_endpoint"])
            # and a later run needs no --plan even if the endpoint stops exposing it
            _rc, out, _ = f.run("--dry-run", "--print")
            self.assertEqual(body_from_print(out)["plan"], "max20")

    def test_normalize_plan(self):
        self.assertEqual(sample.normalize_plan("Max 5x"), "max5")
        self.assertEqual(sample.normalize_plan("max_20x"), "max20")
        self.assertEqual(sample.normalize_plan("claude_pro"), "pro")
        self.assertEqual(sample.normalize_plan("team"), "team")
        self.assertIsNone(sample.endpoint_plan(USAGE))
        self.assertEqual(sample.endpoint_plan({"plan": {"name": "Pro"}}), "pro")


class SendTests(unittest.TestCase):
    def test_dry_run_and_declined_prompt_never_open_a_socket(self):
        with Fixture() as f, mock.patch.object(urllib.request, "urlopen", _no_socket):
            self.assertEqual(f.run("--plan", "pro", "--dry-run")[0], 0)
            self.assertEqual(f.run("--dry-run", "--yes")[0], 0)
            rc, out, _ = f.run("--print", ask=lambda p: "n")
            self.assertEqual(rc, 0)
            self.assertIn("not sent.", out)
            rc, out, _ = f.run()   # not first run, no tty, no --yes
            self.assertEqual(rc, 0)
            self.assertIn("not sending", out)

    def test_yes_posts_minified_body_and_prints_status_and_me_url(self):
        posted = {}

        def fake_urlopen(req, timeout=None):
            posted["url"], posted["data"], posted["timeout"] = req.full_url, req.data, timeout
            posted["ctype"] = req.get_header("Content-type")
            r = mock.MagicMock()
            r.__enter__.return_value.status = 200
            r.__enter__.return_value.read.return_value = b'{"ok": true, "me_url": "https://alldonesites.com/claude-usage-tracker/me/abc"}'
            return r
        with Fixture() as f, mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            rc, out, err = f.run("--plan", "max20", "--yes", "--endpoint", "https://example.test/c")
            self.assertEqual(rc, 0, err)
            self.assertEqual(posted["url"], "https://example.test/c")
            self.assertEqual(posted["timeout"], 10)
            self.assertEqual(posted["ctype"], "application/json")
            body = json.loads(posted["data"])
            self.assertEqual(body["plan"], "max20")
            self.assertEqual(body["tokens_since_five_hour_reset"], EXPECT_FIVE)
            self.assertEqual(posted["data"], sample.minify(body).encode())
            self.assertIn("HTTP 200", out)
            self.assertIn("https://alldonesites.com/claude-usage-tracker/me/abc", out)

    def test_approved_prompt_posts_once_and_handles_missing_me_url(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.data)
            r = mock.MagicMock()
            r.__enter__.return_value.status = 201
            r.__enter__.return_value.read.return_value = b"created"
            return r
        with Fixture() as f, mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            rc, out, _ = f.run("--plan", "pro", ask=lambda p: "y")
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            self.assertIn("HTTP 201", out)
            self.assertNotIn("your page", out)

    def test_http_error_prints_status_and_does_not_retry(self):
        import urllib.error
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(b'{"error":"slow down"}'))
        with Fixture() as f, mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            rc, out, _ = f.run("--plan", "pro", "--yes")
            self.assertEqual(rc, 1)
            self.assertEqual(len(calls), 1)
            self.assertIn("HTTP 429", out)


class TranscriptTests(unittest.TestCase):
    def test_transcripts_are_read_under_claude_dir_and_missing_dir_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sample.transcript_paths(Path(d) / "nope", None), [])
            self.assertEqual(sample.tokens_since([], NOW - timedelta(hours=1), NOW), {})


class PooledProjectsTests(unittest.TestCase):
    """<config dir>/projects can be a symlink several accounts share; own_session_filter
    keeps only transcripts with a matching <config dir>/session-env/<sessionId>/ dir."""

    def _fake_configs(self, tmp: Path):
        shared = tmp / "shared-projects"
        shared.mkdir()
        own_session = SESSION_A
        other_session = SESSION_B
        (shared / f"{own_session}.jsonl").write_text("{}")
        (shared / f"{other_session}.jsonl").write_text("{}")
        cfg = tmp / "cfg"
        cfg.mkdir()
        (cfg / "projects").symlink_to(shared)
        (cfg / "session-env" / own_session).mkdir(parents=True)
        return cfg, own_session, other_session

    def test_pooled_symlink_is_detected(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, _, _ = self._fake_configs(Path(t))
            self.assertTrue(sample._is_pooled_projects(cfg, cfg / "projects"))
            # An ordinary, non-symlinked projects dir is not pooled.
            plain = Path(t) / "plain"
            (plain / "projects").mkdir(parents=True)
            self.assertFalse(sample._is_pooled_projects(plain, plain / "projects"))

    def test_a_sub_agent_transcript_follows_its_parent_session(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, own_session, other_session = self._fake_configs(Path(t))
            shared = cfg / "projects"
            for sess in (own_session, other_session):
                (shared / sess / "subagents").mkdir(parents=True)
                (shared / sess / "subagents" / "agent-1.jsonl").write_text("{}")
            paths = sample.transcript_paths(shared, None)
            self.assertEqual(len(paths), 4)
            with mock.patch("sys.stderr", io.StringIO()):
                kept = sample.own_session_filter(paths, cfg)
            self.assertEqual(sorted(str(p.relative_to(shared)) for p in kept),
                             [f"{own_session}.jsonl", f"{own_session}/subagents/agent-1.jsonl"])

    def test_filters_to_own_sessions_when_symlink_is_pooled(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, own_session, _other_session = self._fake_configs(Path(t))
            paths = sample.transcript_paths(cfg / "projects", None)
            self.assertEqual(len(paths), 2)
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                kept = sample.own_session_filter(paths, cfg)
            self.assertEqual([p.stem for p in kept], [own_session])
            self.assertIn("kept 1 of 2", err.getvalue())
            self.assertIn("session-env", err.getvalue())

    def test_all_sessions_disables_the_filter(self):
        with tempfile.TemporaryDirectory() as t:
            cfg, _, _ = self._fake_configs(Path(t))
            paths = sample.transcript_paths(cfg / "projects", None)
            kept = sample.own_session_filter(paths, cfg, disable=True)
            self.assertEqual(len(kept), 2)

    def test_own_sessions_forces_the_filter_without_a_symlink(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            cfg = tmp / "cfg"
            proj = cfg / "projects"
            proj.mkdir(parents=True)
            (proj / f"{SESSION_A}.jsonl").write_text("{}")
            (proj / f"{SESSION_B}.jsonl").write_text("{}")
            (cfg / "session-env" / SESSION_A).mkdir(parents=True)
            paths = sample.transcript_paths(proj, None)
            self.assertEqual(sample.own_session_filter(paths, cfg), paths)  # not pooled: no-op
            kept = sample.own_session_filter(paths, cfg, force=True)
            self.assertEqual([p.stem for p in kept], [SESSION_A])

    def test_no_session_env_is_a_no_op_even_when_pooled(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            shared = tmp / "shared"
            shared.mkdir()
            (shared / f"{SESSION_A}.jsonl").write_text("{}")
            cfg = tmp / "cfg"
            cfg.mkdir()
            (cfg / "projects").symlink_to(shared)
            paths = sample.transcript_paths(cfg / "projects", None)
            self.assertEqual(sample.own_session_filter(paths, cfg), paths)

    def test_ownership_says_filtered_only_when_the_filter_ran(self):
        # Review of PR 57: the body claimed "filtered_local_transcripts" whenever projects
        # was pooled or --own-sessions was given, even with no session-env to filter by,
        # when every transcript, other logins' included, was kept.
        def ownership(cfg, *flags):
            out = io.StringIO()
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                rc = sample.main(["--id-file", str(cfg.parent / "id.json"), "--claude-dir", str(cfg), "--plan", "max20",
                                  "--dry-run", "--print", *flags], usage_fetch=lambda: USAGE, now=NOW, out=out)
            self.assertEqual(rc, 0)
            return body_from_print(out.getvalue())["capture"]["ownership"]

        with tempfile.TemporaryDirectory() as t:
            cfg, _, _ = self._fake_configs(Path(t))
            self.assertEqual(ownership(cfg), "filtered_local_transcripts")
            self.assertEqual(ownership(cfg, "--all-sessions"), "local_transcripts_unverified")
            (cfg / "session-env" / SESSION_A).rmdir()
            (cfg / "session-env").rmdir()
            self.assertEqual(ownership(cfg), "local_transcripts_unverified")
            self.assertEqual(ownership(cfg, "--own-sessions"), "local_transcripts_unverified")
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / "cfg"
            (cfg / "projects").mkdir(parents=True)
            self.assertEqual(ownership(cfg, "--own-sessions"), "local_transcripts_unverified")
            (cfg / "session-env" / SESSION_A).mkdir(parents=True)
            self.assertEqual(ownership(cfg, "--own-sessions"), "filtered_local_transcripts")

    def test_token_lookup_follows_claude_config_dir(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
            with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": d}):
                self.assertEqual(sample.config_dir(), Path(d))
                self.assertEqual(sample.read_token(sample.config_dir()), "t")
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(Path, "home", return_value=Path(d)):
                self.assertEqual(sample.config_dir(), Path(d) / ".claude")
            with mock.patch.object(sample.platform, "system", return_value="Linux"), self.assertRaises(FileNotFoundError):
                sample.read_token(Path(d) / "missing")


if __name__ == "__main__":
    unittest.main()
