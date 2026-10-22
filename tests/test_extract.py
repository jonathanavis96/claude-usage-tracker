import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import extract as X
from tracker import speed
from tracker.turns import iter_turns, turns_in

T0 = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)


def stamp(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def user(t, sid="s", text="please do the secret thing", side=False):
    return {"type": "user", "timestamp": stamp(t), "sessionId": sid, "isSidechain": side, "cwd": "/home/x/proj",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def assistant(t, mid, sid="s", out=96, model="claude-opus-5-5", entrypoint="cli", text="the answer", **extra):
    return {"type": "assistant", "timestamp": stamp(t), "sessionId": sid, "entrypoint": entrypoint,
            "isSidechain": False, "cwd": "/home/x/proj", **extra,
            "message": {"id": mid, "model": model, "role": "assistant",
                        "content": [{"type": "thinking", "thinking": text}, {"type": "text", "text": text}],
                        "usage": {"input_tokens": 3, "output_tokens": out, "cache_read_input_tokens": 1000,
                                  "cache_creation_input_tokens": 200, "speed": "standard"}}}


def write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(d) + "\n" for d in lines))
    return path


def signed_in(config_dir: Path, uuid: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}))


def ident(uuid: str) -> str:
    return hashlib.sha256(uuid.encode()).hexdigest()[:12]


def transcript(t0=T0, sid="s"):
    """A session: two requests, one opening with a thinking block whose first line carries the
    stream's opening output count, a repeated echo, a sidechain request, and a summary line."""
    return [
        {"type": "summary", "summary": "a private title"},
        user(t0, sid),
        assistant(t0 + timedelta(seconds=5), "m1", sid, out=5),
        assistant(t0 + timedelta(seconds=40), "m1", sid, out=1921),
        assistant(t0 + timedelta(seconds=41), "m1", sid, out=0),
        user(t0 + timedelta(minutes=1), sid, side=True),
        assistant(t0 + timedelta(minutes=1, seconds=30), "m2", sid, out=700),
        user(t0 + timedelta(minutes=2), sid),
        assistant(t0 + timedelta(minutes=2, seconds=20), "m3", sid, out=650, entrypoint="sdk-cli"),
    ]


class SlimLineTests(unittest.TestCase):
    def test_an_extract_counts_exactly_what_the_transcript_counts_and_holds_no_text(self):
        lines = transcript()
        slim = [s for s in map(X.slim_line, lines) if s is not None]
        self.assertEqual(list(turns_in(slim, set())), list(turns_in(lines, set())))
        self.assertEqual(speed.requests_in(slim, "f"), speed.requests_in(lines, "f"))
        text = json.dumps(slim)
        for secret in ("secret thing", "the answer", "private title", "content", "thinking", "/home/x/proj"):
            self.assertNotIn(secret, text)

    def test_a_sub_agent_file_keeps_its_place_so_its_turns_stay_sub_agent_turns(self):
        with tempfile.TemporaryDirectory() as d:
            src = write(Path(d, "src", "s", "subagents", "agent-1.jsonl"), transcript())
            dst = Path(d, "dst", "s", "subagents", "agent-1.jsonl")
            X._write_lines(dst, [s for s in map(X.slim_line, transcript()) if s], None)
            self.assertEqual([(t.id, t.subagent, t.headless) for t in iter_turns([dst])],
                             [(t.id, t.subagent, t.headless) for t in iter_turns([src])])


class LoginTests(unittest.TestCase):
    def test_a_dir_is_its_oauth_account_and_the_default_dir_reads_the_home_login(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            signed_in(home / ".claude-jwork", "uuid-jwork")
            (home / ".claude").mkdir()
            (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-a1"}}))
            self.assertEqual(X.login(home / ".claude-jwork", home).identity, ident("uuid-jwork"))
            # gs's ~/.claude has no .claude.json of its own; Claude Code keeps the default
            # login in ~/.claude.json.
            a1 = X.login(home / ".claude", home)
            self.assertEqual((a1.identity, a1.source), (ident("uuid-a1"), "default-login"))
            self.assertIsNone(X.login(home / ".claude-nothing", home).identity)

    def test_a_token_only_dir_is_named_by_a_fingerprint_that_cannot_give_the_token_back(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            dave = home / ".claude-dave"
            dave.mkdir()
            (dave / ".oauth-token").write_text("fake-setup-token-not-a-real-token\n")
            lg = X.login(dave, home)
            self.assertEqual(lg.source, "token-file")
            self.assertEqual(lg.identity, X.token_fingerprint("fake-setup-token-not-a-real-token"))
            self.assertRegex(lg.identity, r"^tok-[0-9a-f]{12}$")
            self.assertNotIn("not-a-real", repr(lg))
            # A login *and* a token beside it: the login names the dir, and the doubt is recorded.
            signed_in(dave, "uuid-dave")
            lg = X.login(dave, home)
            self.assertEqual((lg.identity, lg.token_override_possible), (ident("uuid-dave"), True))

    def test_a_token_in_the_dirs_settings_env_overrides_its_login(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d, ".claude-x")
            signed_in(cfg, "uuid-x")
            (cfg / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "tok-value"}}))
            lg = X.login(cfg, Path(d))
            self.assertEqual((lg.identity, lg.source), (X.token_fingerprint("tok-value"), "settings-env"))

    def test_the_meter_units_bind_a_setup_token_to_the_account_it_reads(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            signed_in(home / ".claude-dave", "uuid-dave")
            units = home / ".config" / "systemd" / "user"
            units.mkdir(parents=True)
            (units / "claude-usage-meter-dave.service").write_text(
                "[Service]\nExecStart=/usr/bin/python3 -m tracker.meter_log --config-dir %h/.claude-dave "
                "--account dave --log %h/x.log --token-env GS_TEST_DAVE_TOKEN\n")
            (units / "claude-usage-meter-jwork.service").write_text(
                "[Service]\nExecStart=/usr/bin/python3 -m tracker.meter_log --config-dir %h/.claude-jwork\n")
            env = home / ".config" / "greenscape" / "env"
            env.parent.mkdir(parents=True)
            env.write_text("OTHER=1\nexport GS_TEST_DAVE_TOKEN='fake-setup-token-dave'\n")
            self.assertEqual(X.token_bindings(home), {X.token_fingerprint("fake-setup-token-dave"): ident("uuid-dave")})


class ClaimTests(unittest.TestCase):
    def test_each_turn_goes_to_the_latest_claim_before_it_and_early_turns_to_the_first(self):
        a, b = T0, T0 + timedelta(days=2)
        segs = [(a, "avis"), (b, "a1"), (b + timedelta(minutes=8), "avis")]
        self.assertEqual(X.owner_at(segs, a - timedelta(minutes=3)), "avis")
        self.assertEqual(X.owner_at(segs, a + timedelta(days=1)), "avis")
        self.assertEqual(X.owner_at(segs, b + timedelta(minutes=1)), "a1")
        self.assertEqual(X.owner_at(segs, b + timedelta(minutes=9)), "avis")
        self.assertIsNone(X.owner_at([], a))

    def test_claims_come_from_session_env_sessions_and_history_never_the_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d, ".claude-avis")
            (cfg / "session-env" / "s1").mkdir(parents=True)
            (cfg / "sessions").mkdir()
            started = T0 + timedelta(days=2)
            (cfg / "sessions" / "123.json").write_text(json.dumps(
                {"pid": 123, "sessionId": "s1", "startedAt": int(started.timestamp() * 1000)}))
            prompt = T0 + timedelta(days=3)
            (cfg / "history.jsonl").write_text(json.dumps(
                {"display": "a prompt", "timestamp": int(prompt.timestamp() * 1000), "sessionId": "s1"}) + "\n")
            claims = X.session_claims(cfg, {"s1", "s2"})
            self.assertEqual(set(claims), {"s1"})
            self.assertEqual(len(claims["s1"]), 3)
            self.assertIn(started, claims["s1"])
            self.assertIn(prompt, claims["s1"])


class ExtractTests(unittest.TestCase):
    """masterrig's layout on 2026-10-22, in miniature: a1 in ~/.claude, avis linking one of a1's
    project directories into its own tree, jwork on its own, dave on a token file."""

    def _home(self, d: Path) -> Path:
        home = d / "home"
        signed_in(home / ".claude", "uuid-a1")
        for name, uuid in ((".claude-avis", "uuid-avis"), (".claude-jwork", "uuid-jwork")):
            signed_in(home / name, uuid)
        dave = home / ".claude-dave"
        (dave / "projects").mkdir(parents=True)
        (dave / ".oauth-token").write_text("fake-setup-token-dave\n")
        (home / ".claude-avis" / "projects").mkdir(parents=True)
        shared = home / ".claude" / "projects" / "-mnt-d-skin"
        shared.mkdir(parents=True)
        (home / ".claude-avis" / "projects" / "-mnt-d-skin").symlink_to(shared)
        # One session in the shared directory: avis claims it first, a1 for a moment two days
        # later, avis again eight minutes after that (session 27b80904's shape).
        lines = []
        for i in range(6):
            t = T0 + timedelta(days=2, minutes=-4 + 4 * i)  # turns at -4, 0, 4, 8, 12, 16 min
            lines += [user(t, "shared"), assistant(t + timedelta(seconds=10), f"sh{i}", "shared")]
        write(shared / "shared.jsonl", lines)
        avis_claim = home / ".claude-avis" / "session-env" / "shared"
        avis_claim.mkdir(parents=True)
        os.utime(avis_claim, (T0.timestamp(),) * 2)
        a1_claim = home / ".claude" / "session-env" / "shared"
        a1_claim.mkdir(parents=True)
        os.utime(a1_claim, ((T0 + timedelta(days=2)).timestamp(),) * 2)
        (home / ".claude-avis" / "sessions").mkdir()
        (home / ".claude-avis" / "sessions" / "9.json").write_text(json.dumps(
            {"sessionId": "shared", "startedAt": int((T0 + timedelta(days=2, minutes=8)).timestamp() * 1000)}))
        write(shared / "nobody.jsonl", [user(T0, "nobody"), assistant(T0, "nb", "nobody", entrypoint="sdk-py")])
        write(home / ".claude" / "projects" / "-own" / "a1only.jsonl", [assistant(T0, "a1x", "a1only")])
        write(home / ".claude-jwork" / "projects" / "-p" / "j.jsonl", transcript(sid="j"))
        write(home / ".claude-jwork" / "projects" / "-p" / "j" / "subagents" / "workflows" / "wf_1" / "agent-2.jsonl",
              [assistant(T0, "jsub", "j")])
        write(dave / "projects" / "-q" / "dv.jsonl", [assistant(T0, "dv1", "dv", entrypoint="sdk-cli")])
        return home

    def _births_are_mtimes(self):
        """The fixture sets claim times with utime; read mtimes, not the filesystem's birth times."""
        real = X._births
        X._births = lambda paths: {p: p.stat().st_mtime for p in paths}
        self.addCleanup(setattr, X, "_births", real)

    def test_every_non_local_account_is_extracted_and_a_shared_session_split_by_claim_time(self):
        self._births_are_mtimes()
        with tempfile.TemporaryDirectory() as d:
            home = self._home(Path(d))
            out = Path(d, "out")
            m = X.extract(home, out, host="rig", now=T0)
            avis, jwork, dave = ident("uuid-avis"), ident("uuid-jwork"), X.token_fingerprint("fake-setup-token-dave")
            self.assertEqual(sorted(p.name for p in out.iterdir() if p.is_dir()), sorted([avis, jwork, dave]))
            self.assertEqual(m["local_identity"], ident("uuid-a1"))
            self.assertFalse((out / ident("uuid-a1")).exists())
            shared = [t.id for t in iter_turns([out / avis / "-mnt-d-skin" / "shared.jsonl"])]
            # sh0 comes before a1's claim, so it is avis's (claimed two days earlier); sh1 and sh2
            # fall in a1's eight minutes and stay with the local join; avis's restart takes the rest.
            self.assertEqual(shared, ["sh0", "sh3", "sh4", "sh5"])
            self.assertEqual(m["unclaimed_in_shared_subtrees"], {"files": 1, "turns": 1})
            self.assertEqual(m["identities"][dave]["source"], "token-file")
            self.assertEqual(m["identities"][jwork]["turns"], 4)
            nested = out / jwork / "-p" / "j" / "subagents" / "workflows" / "wf_1" / "agent-2.jsonl"
            self.assertEqual([t.subagent for t in iter_turns([nested])], [True])
            self.assertNotIn("secret", "".join(p.read_text() for p in out.rglob("*.jsonl")))

    def test_an_unchanged_source_is_not_rewritten(self):
        self._births_are_mtimes()
        with tempfile.TemporaryDirectory() as d:
            home = self._home(Path(d))
            out = Path(d, "out")
            X.extract(home, out, host="rig", now=T0)
            target = out / ident("uuid-jwork") / "-p" / "j.jsonl"
            target.write_text("sentinel\n")
            X.extract(home, out, host="rig", now=T0)
            self.assertEqual(target.read_text(), "sentinel\n")
            src = home / ".claude-jwork" / "projects" / "-p" / "j.jsonl"
            src.write_text(src.read_text() + json.dumps(assistant(T0 + timedelta(hours=1), "m9", "j")) + "\n")
            X.extract(home, out, host="rig", now=T0)
            self.assertIn("m9", target.read_text())


class ArchiveCacheTests(unittest.TestCase):
    def _archive(self, home: Path, members: dict[str, list[dict]], name="-proj.tar.gz") -> Path:
        adir = home / "archive" / "transcripts-20261015" / "claude"
        adir.mkdir(parents=True, exist_ok=True)
        path = adir / name
        with tarfile.open(path, "w:gz") as tf:
            for member, lines in members.items():
                data = "".join(json.dumps(x) + "\n" for x in lines).encode()
                info = tarfile.TarInfo(member)
                info.size, info.mtime = len(data), int(T0.timestamp())
                tf.addfile(info, io.BytesIO(data))
        return path

    def test_an_archive_is_read_in_place_once_and_counts_what_the_transcripts_counted(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".claude" / "projects").mkdir(parents=True)
            lines = transcript(sid="old")
            self._archive(home, {"-proj/old.jsonl": lines, "-proj/old/subagents/agent-1.jsonl": [assistant(T0, "sub", "old")],
                                 "-proj/old/tool-results/x.txt": [], "../escape.jsonl": lines})
            roots = X.build_archive_cache(home)
            self.assertEqual([(r.label, r.source_projects) for r in roots],
                             [("archive:transcripts-20261015/claude", home / ".claude" / "projects")])
            cached = sorted(p.relative_to(roots[0].cache_root).as_posix() for p in roots[0].cache_root.rglob("*.jsonl"))
            self.assertEqual(cached, ["-proj/old.jsonl", "-proj/old/subagents/agent-1.jsonl"])
            old = roots[0].cache_root / "-proj" / "old.jsonl"
            self.assertEqual(list(iter_turns([old])), list(turns_in(lines, set())))
            # The first line keeps what tracker/unclaimed.py attributes a headless run by.
            from tracker.unclaimed import first_record
            self.assertEqual(first_record(old), (T0, "/home/x/proj"))
            self.assertEqual(old.stat().st_mtime, T0.timestamp())
            old.write_text("sentinel\n")
            X.build_archive_cache(home)  # the stamp matches: not read again
            self.assertEqual(old.read_text(), "sentinel\n")

    @unittest.skipUnless(shutil.which("zstd"), "no zstd binary on this host")
    def test_a_zstd_archive_streams_through_the_zstd_binary(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".claude-dave" / "projects").mkdir(parents=True)
            raw = self._archive(home, {"-p/a.jsonl": transcript(sid="a")}, name="-p.tar.gz")
            subprocess.run(["sh", "-c", f"gzip -dc '{raw}' | zstd -q -o '{raw.with_name('-p.tar.zst')}'"], check=True)
            raw.unlink()
            (raw.parent.parent / "claude-dave").mkdir()
            raw.with_name("-p.tar.zst").rename(raw.parent.parent / "claude-dave" / "-p.tar.zst")
            roots = {r.label: r for r in X.build_archive_cache(home)}
            cache = roots["archive:transcripts-20261015/claude-dave"].cache_root
            self.assertEqual([t.id for t in iter_turns(sorted(cache.rglob("*.jsonl")))], ["m1", "m2", "m3"])


if __name__ == "__main__":
    unittest.main()
