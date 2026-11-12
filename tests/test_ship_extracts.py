"""bin/ship-extracts.sh: masterrig's hourly shipment of text-free extracts to gs."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "ship-extracts.sh"


def _home(d: Path) -> Path:
    home = d / "home"
    jwork = home / ".claude-jwork"
    (jwork / "projects" / "-p").mkdir(parents=True)
    (jwork / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-jwork"}}))
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude" / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-a1"}}))
    line = {"type": "assistant", "timestamp": "2026-10-17T03:34:08Z", "sessionId": "s",
            "message": {"id": "m1", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "private"}],
                        "usage": {"input_tokens": 1, "output_tokens": 2}}}
    (jwork / "projects" / "-p" / "s.jsonl").write_text(json.dumps(line) + "\n")
    return home


@unittest.skipUnless(shutil.which("rsync"), "no rsync on this host")
class ShipExtractsTests(unittest.TestCase):
    def _run(self, d: Path, dest: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(_home(d)), "SHIP_EXTRACTS_OUT": str(d / "out"),
               "SHIP_EXTRACTS_DEST": dest}
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)

    def test_ships_the_extracts_and_manifest_owner_only_and_keeps_its_state_home(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            r = self._run(d, f"{d}/dest/rig/")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertRegex(r.stderr, r"(?m)^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ship-extracts.sh: shipped to ")
            shipped = sorted(p.relative_to(d / "dest" / "rig").as_posix() for p in (d / "dest" / "rig").rglob("*")
                             if p.is_file())
            ident = __import__("hashlib").sha256(b"uuid-jwork").hexdigest()[:12]
            self.assertEqual(shipped, sorted(["manifest.json", f"{ident}/-p/s.jsonl"]))
            self.assertTrue((d / "out" / ".extract-state.json").exists())
            self.assertNotIn("private", (d / "dest" / "rig" / ident / "-p" / "s.jsonl").read_text())
            self.assertEqual((d / "dest" / "rig" / "manifest.json").stat().st_mode & 0o077, 0)

    def test_an_unreachable_destination_is_a_warning_and_exit_zero(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run(Path(d), "nohost.invalid:remote-transcripts/rig/")
            self.assertEqual(r.returncode, 0)
            self.assertIn("warning: rsync to nohost.invalid:remote-transcripts/rig/ failed", r.stderr)


if __name__ == "__main__":
    unittest.main()
