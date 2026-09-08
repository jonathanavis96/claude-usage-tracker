"""Syntax-check the gs deployment wrappers (Task 12): bin/probe.sh, bin/daily.sh.

Also exercises daily.sh's notify_change step offline, with a stub curl on PATH,
because that step must never be able to fail the publish it runs after; and runs
bin/probe.sh end to end offline, with a stub python3 that fakes tracker.probe and
tracker.alert but hands tracker.rotate to the real interpreter, against a scratch
git clone with a bare origin.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tracker.rotate import expectation

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


class TestDeployScriptsSyntax(unittest.TestCase):
    def _check(self, name: str) -> None:
        script = BIN / name
        self.assertTrue(script.exists(), f"{script} missing")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_probe_sh_syntax(self) -> None:
        self._check("probe.sh")

    def test_daily_sh_syntax(self) -> None:
        self._check("daily.sh")

    def test_output_probe_sh_syntax(self) -> None:
        self._check("output-probe.sh")

    def test_probe_sh_takes_its_flags_from_the_rotation_not_a_literal(self) -> None:
        text = (BIN / "probe.sh").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"^EXPECT=", "the literal expectation is gone; tracker.rotate supplies it")
        self.assertNotRegex(text, r"^MODEL=claude", "the model comes from the rotation, not a literal")
        for sub in ("rotate flags", "rotate check", "rotate flags --rerun", "rotate decide"):
            self.assertIn(f"python3 -m tracker.{sub}", text, sub)

    def test_every_wrapper_raises_alerts_through_the_helper(self) -> None:
        # One helper, one address, one secret: no wrapper may grow its own curl.
        for name in ("probe.sh", "daily.sh", "output-probe.sh"):
            text = (BIN / name).read_text(encoding="utf-8")
            self.assertIn("python3 -m tracker.alert", text, name)
            self.assertIn("alert_jonathan()", text, name)

    def test_output_probe_is_a_five_tick_fable_output_run(self) -> None:
        # The weekly weight run: Fable, --payload output, 5 ticks, same lock and
        # history file as the rate probe, so the publisher finds its row.
        text = (BIN / "output-probe.sh").read_text(encoding="utf-8")
        self.assertIn("MODEL=claude-fable-5-1", text)
        self.assertIn("PAYLOAD=output", text)
        self.assertIn("TICKS=5", text)
        self.assertIn('--payload "$PAYLOAD" --ticks "$TICKS"', text)
        self.assertIn("--out history/probes.jsonl", text)
        self.assertIn("flock -n 9", text)
        self.assertNotIn("--burst", text)

    def test_daily_commits_the_recomputed_weight_back_to_the_tracker_branch(self) -> None:
        text = (BIN / "daily.sh").read_text(encoding="utf-8")
        self.assertIn("git add data/prices.json", text)
        self.assertIn('git push -q origin "$BRANCH"', text)


class TestDailyNotifyChange(unittest.TestCase):
    """Run bin/daily.sh's notify_change function in isolation."""

    @classmethod
    def setUpClass(cls) -> None:
        text = (BIN / "daily.sh").read_text(encoding="utf-8")
        match = re.search(r"^notify_change\(\) \{.*?^\}$", text, re.S | re.M)
        assert match, "notify_change not found in bin/daily.sh"
        cls.func = match.group(0)

    def _run(self, *, last_change, notified=None, env_file=True, curl_status="200"):
        """Return (returncode, stdout, stderr, state-file contents or None)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "site" / "website" / "public" / "data"
            data.mkdir(parents=True)
            (data / "claude-usage.json").write_text(
                json.dumps({"last_change": last_change}), encoding="utf-8"
            )
            home = root / "home"
            home.mkdir()
            if env_file:
                (home / ".claude-usage-notify.env").write_text(
                    "# names NOTIFY_SEND_SECRET in a comment first\nNOTIFY_SEND_SECRET=s3cr3t\n",
                    encoding="utf-8",
                )
            # A stub curl that never touches the network. It records the request
            # so the payload can be asserted, and prints the status curl -w would.
            stub = root / "bin"
            stub.mkdir()
            curl = stub / "curl"
            curl.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$REQUEST_LOG\"\n"
                f"printf '{curl_status}'\n",
                encoding="utf-8",
            )
            curl.chmod(0o755)

            cwd = root / "repo"
            cwd.mkdir()
            if notified is not None:
                (cwd / ".notified-change").write_text(notified + "\n", encoding="utf-8")

            env = dict(os.environ)
            env.update(
                HOME=str(home),
                SITE=str(root / "site"),
                PATH=f"{stub}{os.pathsep}{env['PATH']}",
                REQUEST_LOG=str(root / "request.log"),
                ALERT_LOG=str(root / "alert.log"),
            )
            # The real alert_jonathan is defined by daily.sh outside notify_change and
            # runs tracker.alert; here it is a stub that records its subject and text.
            stub_alert = 'alert_jonathan() { printf \'%s\\n\' "$1" "$2" >> "$ALERT_LOG"; }'
            proc = subprocess.run(
                ["bash", "-c", f"set -uo pipefail\n{stub_alert}\n{self.func}\nnotify_change"],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
            )
            state_path = cwd / ".notified-change"
            state = state_path.read_text(encoding="utf-8").strip() if state_path.exists() else None
            log_path = root / "request.log"
            request = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            alert_path = root / "alert.log"
            self.alerts = alert_path.read_text(encoding="utf-8") if alert_path.exists() else ""
            return proc, state, request

    CHANGE = {"date": "2026-09-11", "direction": "increased", "percent": 7, "model": "claude-opus-5"}

    def test_posts_a_new_change_and_records_it(self) -> None:
        proc, state, request = self._run(last_change=self.CHANGE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(state, "2026-09-11")
        self.assertIn("https://alldonesites.com/api/notify/send", request)
        # The secret must come from the assignment line, not the comment above it.
        self.assertIn("authorization: Bearer s3cr3t", request)
        payload = json.loads(next(ln for ln in request.splitlines() if ln.startswith("{")))
        self.assertEqual(
            payload,
            {"date": "2026-09-11", "direction": "increased", "percent": 7, "model": "claude-opus-5"},
        )
        # Jonathan gets his own alert for the confirmed change, quoting the outcome.
        self.assertIn("Change confirmed: increased 7% on 2026-09-11 (claude-opus-5)", self.alerts)
        self.assertIn("HTTP 200", self.alerts)

    def test_skips_a_change_already_announced(self) -> None:
        proc, _state, request = self._run(last_change=self.CHANGE, notified="2026-09-11")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(request, "", "no request should be made for an announced change")
        self.assertEqual(self.alerts, "", "an announced change must not alert again")

    def test_skips_when_there_is_no_change(self) -> None:
        proc, state, request = self._run(last_change=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(state)
        self.assertEqual(request, "")

    def test_skips_when_the_env_file_is_missing(self) -> None:
        proc, state, request = self._run(last_change=self.CHANGE, env_file=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(state)
        self.assertEqual(request, "")

    def test_a_non_2xx_answer_is_a_warning_and_retries_tomorrow(self) -> None:
        proc, state, _request = self._run(last_change=self.CHANGE, curl_status="500")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(state, "a failed POST must not be recorded as announced")
        self.assertIn("will retry tomorrow", proc.stderr)
        # The alert still goes out, and says the list send failed.
        self.assertIn("Change confirmed: increased 7% on 2026-09-11 (claude-opus-5)", self.alerts)
        self.assertIn("HTTP 500", self.alerts)

    def test_incomplete_change_is_skipped(self) -> None:
        proc, state, request = self._run(last_change={"date": "2026-09-11"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(state)
        self.assertEqual(request, "")



SONNET_0939 = {"ts": "2026-09-06T09:39:26.287942+00:00", "model": "claude-sonnet-5", "effort": "low",
               "tokens_per_pct": 467778.6,
               "tokens": {"input": 86, "output": 215, "cache_read": 415767, "cache_write": 1922825},
               "prompts": 48, "tick_from": 1, "tick_to": 6, "elapsed_s": 4742.686796, "account": "dave"}
SONNET_1433 = {"ts": "2026-09-06T14:33:05.254749+00:00", "model": "claude-sonnet-5", "effort": "low",
               "tokens_per_pct": 579818.6666666666,
               "tokens": {"input": 86, "output": 215, "cache_read": 415767, "cache_write": 1323388},
               "prompts": 63, "tick_from": 5, "tick_to": 8, "elapsed_s": 1569.897585, "payload": "prose",
               "payload_words": 12000, "ticks": 3, "skip": 1, "settle_s": 60, "expect_tokens_per_pct": 468000.0,
               "early_tick": False, "reset_start": False, "account": "dave"}


def _row(ts, model, tpp, ticks=3):
    return {"ts": ts, "model": model, "effort": "low", "tokens_per_pct": float(tpp),
            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": int(tpp * ticks)},
            "prompts": 10, "tick_from": 1, "tick_to": 1 + ticks, "elapsed_s": 100.0, "payload": "prose",
            "ticks": ticks, "account": "dave"}


# The real 11:16 Fable prose row (readings dropped): $1.08 of list value per 1%.
FABLE = {"ts": "2026-09-06T11:16:29.286577+00:00", "model": "claude-fable-5-1", "effort": "low",
         "tokens_per_pct": 117896.8, "tokens": {"input": 28, "output": 70, "cache_read": 160622, "cache_write": 428764},
         "prompts": 17, "tick_from": 8, "tick_to": 13, "elapsed_s": 2063.701164, "account": "dave"}

# The stub python3. tracker.probe appends the canned row for this call (FAKE_ROW_<n>)
# to --out and exits FAKE_RC_<n>; tracker.alert logs its arguments; anything else
# (tracker.rotate) runs on the real interpreter against the real package.
STUB_PYTHON = """#!/usr/bin/env bash
case "${2:-}" in
  tracker.probe)
    n=$(( $(cat "$CALLS" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$CALLS"
    printf '%s\\n' "$*" >> "$PROBE_ARGS"
    out=""; while [ $# -gt 0 ]; do [ "$1" = "--out" ] && out="$2"; shift; done
    row_var="FAKE_ROW_$n"; rc_var="FAKE_RC_$n"
    row="${!row_var:-}"; rc="${!rc_var:-0}"
    [ -n "$row" ] && printf '%s\\n' "$row" >> "$out"
    echo "fake probe $n rc $rc"
    exit "$rc" ;;
  tracker.alert)
    printf '%s\\n' "$*" >> "$ALERT_LOG"; exit 0 ;;
  *)
    PYTHONPATH="$REAL_ROOT" exec "$REAL_PYTHON" "$@" ;;
esac
"""


class TestProbeShFlow(unittest.TestCase):
    """bin/probe.sh end to end, offline: rotation flags, drift check, rerun, decide, alerts."""

    def _git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)

    def _run(self, history, fake_rows=(), fake_rcs=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            stub_dir = home / ".npm-global" / "bin"  # first on the PATH probe.sh exports
            stub_dir.mkdir(parents=True)
            stub = stub_dir / "python3"
            stub.write_text(STUB_PYTHON, encoding="utf-8")
            stub.chmod(0o755)

            origin = root / "origin.git"
            self._git("init", "-q", "--bare", "-b", "build", str(origin), cwd=root)
            repo = root / "repo"
            self._git("clone", "-q", str(origin), str(repo), cwd=root)
            (repo / "bin").mkdir()
            (repo / "bin" / "probe.sh").write_bytes((BIN / "probe.sh").read_bytes())
            (repo / "history").mkdir()
            (repo / "history" / "probes.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in history), encoding="utf-8")
            (repo / "data").mkdir()
            (repo / "data" / "prices.json").write_bytes((ROOT / "data" / "prices.json").read_bytes())
            self._git("-c", "user.name=t", "-c", "user.email=t@t", "checkout", "-q", "-b", "build", cwd=repo)
            self._git("add", "-A", cwd=repo)
            self._git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed", cwd=repo)
            self._git("push", "-q", "-u", "origin", "build", cwd=repo)

            env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_")}
            env.update(HOME=str(home), REAL_ROOT=str(ROOT), REAL_PYTHON=sys.executable,
                       CALLS=str(root / "calls"), PROBE_ARGS=str(root / "probe-args.log"),
                       ALERT_LOG=str(root / "alerts.log"),
                       GIT_CONFIG_GLOBAL=str(root / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
            for i, r in enumerate(fake_rows, start=1):
                if r is not None:  # a run that writes no row
                    env[f"FAKE_ROW_{i}"] = json.dumps(r)
            for i, rc in enumerate(fake_rcs, start=1):
                env[f"FAKE_RC_{i}"] = str(rc)
            proc = subprocess.run(["bash", str(repo / "bin" / "probe.sh")], cwd=repo, env=env,
                                  capture_output=True, text=True)
            args = (root / "probe-args.log").read_text(encoding="utf-8").splitlines() \
                if (root / "probe-args.log").exists() else []
            alerts = (root / "alerts.log").read_text(encoding="utf-8") if (root / "alerts.log").exists() else ""
            rows = [json.loads(ln) for ln in (repo / "history" / "probes.jsonl").read_text(encoding="utf-8").splitlines()]
            log = self._git("log", "--format=%s", "origin/build", cwd=repo).stdout.splitlines()
            return proc, args, alerts, rows, log

    def test_no_drift_runs_once_with_the_rotations_flags_and_pushes_the_row(self):
        opus = _row("2026-09-07T00:00:00+00:00", "claude-opus-5", 190000)
        proc, args, alerts, rows, log = self._run([SONNET_0939], [opus])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(len(args), 1)
        self.assertIn("--model claude-opus-5 --expect-tokens-per-pct 187111", args[0])
        self.assertIn("--effort low", args[0])
        self.assertNotIn("--ticks", args[0])
        self.assertIn("drift check: ok claude-opus-5 $1.188/1%: no earlier prose row", proc.stdout)
        self.assertEqual(rows[-1]["model"], "claude-opus-5")
        self.assertEqual(alerts, "")
        self.assertRegex(log[0], r"^Probe \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z claude-opus-5$")
        self.assertEqual(log[1], "seed")

    def test_drift_reruns_with_two_ticks_and_flags_the_outlier(self):
        # 391,566 tpp is $0.979/1% (real prices.json), agreeing with the earlier median
        rerun = _row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 391566, ticks=2)
        proc, args, alerts, rows, log = self._run([SONNET_0939, FABLE], [SONNET_1433, rerun])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(len(args), 2)
        # first run: Sonnet's turn, expectation through the dollar invariant from the
        # median of the Sonnet and Fable dollar values (about $1.03 per 1%)
        prices = {k: v for k, v in json.loads((ROOT / "data" / "prices.json").read_text()).items() if not k.startswith("_")}
        expect = round(expectation([SONNET_0939, FABLE], "claude-sonnet-5", prices))
        self.assertIn(f"--model claude-sonnet-5 --expect-tokens-per-pct {expect}", args[0])
        self.assertGreater(expect, 467779)
        self.assertLess(expect, 579819)
        # rerun: same model, 2 ticks, the smaller of the median and the drifted reading
        # (in dollars), converted back to tokens through the drifted row's own split
        self.assertIn("--model claude-sonnet-5 --expect-tokens-per-pct 501410 --ticks 2", args[1])
        self.assertIn("drift check: drift claude-sonnet-5 $1.132/1% against median $0.979/1% (+16%)", proc.stdout)
        self.assertIn("decision: outlier claude-sonnet-5", proc.stdout)
        self.assertEqual([r.get("outlier", False) for r in rows], [False, False, True, False])
        self.assertIn("Outlier on claude-sonnet-5", alerts)
        self.assertNotIn("Change confirmed", alerts)
        self.assertEqual(log[0].split(": ")[-1], "outlier")
        self.assertRegex(log[1], r"^Probe rerun .* claude-sonnet-5$")
        self.assertRegex(log[2], r"^Probe .* claude-sonnet-5$")
        self.assertEqual(log[3], "seed")

    def test_drift_confirmed_by_the_rerun_is_a_change_and_flags_nothing(self):
        # 452,798 tpp is $1.132/1% (real prices.json), agreeing with the drifted reading
        rerun = _row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 452798, ticks=2)
        proc, args, alerts, rows, log = self._run([SONNET_0939, FABLE], [SONNET_1433, rerun])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(len(args), 2)
        self.assertIn("decision: change claude-sonnet-5", proc.stdout)
        self.assertFalse(any(r.get("outlier") for r in rows))
        self.assertIn("Change confirmed on claude-sonnet-5", alerts)
        self.assertIn("increased +16%", alerts)
        self.assertEqual(len(log), 3)  # seed, row, rerun: no third commit without a flag

    def test_inconclusive_pair_alerts_and_flags_nothing(self):
        # about 3x the earlier median in dollars: agrees with neither
        rerun = _row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 1174699, ticks=2)
        proc, _args, alerts, rows, _log = self._run([SONNET_0939, FABLE], [SONNET_1433, rerun])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("decision: inconclusive", proc.stdout)
        self.assertFalse(any(r.get("outlier") for r in rows))
        self.assertIn("Drift on claude-sonnet-5 inconclusive", alerts)

    def test_failed_rerun_keeps_the_drifted_row_unflagged_and_exits_with_its_code(self):
        proc, args, alerts, rows, log = self._run([SONNET_0939, FABLE], [SONNET_1433, None], fake_rcs=[0, 4])
        self.assertEqual(proc.returncode, 4, proc.stderr + proc.stdout)
        self.assertEqual(len(args), 2)
        self.assertEqual(len(rows), 3)
        self.assertFalse(any(r.get("outlier") for r in rows))
        self.assertIn("Probe aborted on claude-sonnet-5", alerts)
        self.assertIn("Drift on claude-sonnet-5 unconfirmed: rerun exited 4", alerts)
        self.assertEqual(len(log), 2)  # seed and the first row

    def test_first_run_failure_alerts_writes_nothing_and_bubbles_the_code(self):
        proc, args, alerts, rows, log = self._run([SONNET_0939], [None], fake_rcs=[3])
        self.assertEqual(proc.returncode, 3, proc.stderr + proc.stdout)
        self.assertEqual(len(args), 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("Probe skipped: no idle account for claude-opus-5", alerts)
        self.assertEqual(log, ["seed"])

    def test_no_usable_history_means_no_flags_and_no_probe(self):
        proc, args, alerts, _rows, _log = self._run([])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(args, [])
        self.assertIn("no usable prose row", proc.stderr)
        self.assertEqual(alerts, "")


if __name__ == "__main__":
    unittest.main()
