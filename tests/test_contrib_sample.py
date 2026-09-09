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
            rec("m5", in_five, model="claude-haiku-4-5-20251001", inp=7),   # not a canonical model: dropped
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
    "claude-opus-5": {"input": 15, "output": 25, "cache_read": 305, "cache_write": 405},   # m1 + m7
    "claude-sonnet-5": {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4},        # m2
}
EXPECT_SEVEN = {
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
                "tokens_since_five_hour_reset", "tokens_since_seven_day_reset", "client_version"})

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
            rc, out, err = f.run("--dry-run")
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

    def test_endpoint_plan_wins_and_is_recorded(self):
        usage = dict(USAGE, subscription_type="Claude Max 20x")
        with Fixture() as f:
            rc, out, _ = f.run("--plan", "pro", "--dry-run", usage=usage)
            body = body_from_print(out)
            self.assertEqual((body["plan"], body["plan_source"]), ("max20", "endpoint"))
            self.assertTrue(json.loads(f.id_file.read_text())["plan_from_endpoint"])
            # and a later run needs no --plan even if the endpoint stops exposing it
            rc, out, _ = f.run("--dry-run", "--print")
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

    def test_token_lookup_follows_claude_config_dir(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
            with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": d}):
                self.assertEqual(sample.config_dir(), Path(d))
                self.assertEqual(sample.read_token(sample.config_dir()), "t")
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(Path, "home", return_value=Path(d)):
                self.assertEqual(sample.config_dir(), Path(d) / ".claude")
            with mock.patch.object(sample.platform, "system", return_value="Linux"):
                with self.assertRaises(FileNotFoundError):
                    sample.read_token(Path(d) / "missing")


if __name__ == "__main__":
    unittest.main()
