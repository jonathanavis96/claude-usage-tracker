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
from typing import ClassVar

from tracker.publish import usd_per_pct
from tracker.rotate import _usd, expectation

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"


# The probe rotation is retired (2026-09-16) and its figures below were pinned at the
# price table of its day (output class weight 1.8); data/prices.json has moved on to
# the weight measured from the passive stretches, so the rotation is tested against
# a frozen copy rather than today's table.
ROTATION_PRICES = ROOT / "tests" / "fixtures" / "prices_output_weight_1.8.json"

class TestDeployScriptsSyntax(unittest.TestCase):
    def _check(self, name: str) -> None:
        script = BIN / name
        self.assertTrue(script.exists(), f"{script} missing")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
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

    def test_daily_runs_gs_passive_before_publish_and_never_lets_it_stop_the_publish(self) -> None:
        # Passive is the instrument now (issue #39): a failed gs_passive run must
        # still let the publish carry on with whatever gs-passive.json is already
        # committed, exactly like the contributed step above.
        text = (BIN / "daily.sh").read_text(encoding="utf-8")
        gs_passive = text.index("python3 -m tracker.gs_passive")
        publish = text.index("python3 -m tracker.publish")
        self.assertLess(gs_passive, publish)
        step = text[gs_passive:publish]
        self.assertIn("--prices data/prices.json", step)
        self.assertIn("--probes history/probes.jsonl", step)
        self.assertIn("--out history/gs-passive.json", step)
        self.assertIn("|| echo \"warning: tracker.gs_passive failed", step)
        self.assertIn("--gs-passive history/gs-passive.json", text[publish:])
        commit = text.index('git -c user.name=publisher')
        self.assertIn("history/gs-passive.json", text[publish:commit])

    def test_daily_runs_contributed_before_publish_and_never_lets_it_stop_the_publish(self) -> None:
        text = (BIN / "daily.sh").read_text(encoding="utf-8")
        contributed = text.index("python3 -m tracker.contributed")
        publish = text.index("python3 -m tracker.publish")
        lock = text.index("flock -w 600 9")
        self.assertLess(lock, contributed)
        self.assertLess(contributed, publish)
        step = text[contributed:publish]
        self.assertIn("--history history/contributed.jsonl", step)
        self.assertIn("--out data/contributed.json", step)
        self.assertIn('--env-file "$HOME/.claude-usage-notify.env"', step)
        # Advisory: the step's failure is caught on the same command, not left to fail the script.
        self.assertIn("|| echo \"warning: tracker.contributed failed", step)
        self.assertIn("--contributed data/contributed.json", text[publish:])
        commit = text.index('git -c user.name=publisher')
        self.assertIn("history/contributed.jsonl data/contributed.json", text[publish:commit])


class TestDailyNotifyChange(unittest.TestCase):
    """Run bin/daily.sh's notify_change function in isolation, over a run of publishes."""

    @classmethod
    def setUpClass(cls) -> None:
        text = (BIN / "daily.sh").read_text(encoding="utf-8")
        match = re.search(r"^notify_change\(\) \{.*?^\}$", text, re.DOTALL | re.MULTILINE)
        assert match, "notify_change not found in bin/daily.sh"
        cls.func = match.group(0)

    def _publishes(self, *publishes, notified=None, env_file=True):
        """Run notify_change once per publish, in order, in one repo checkout.

        Each publish is (last_change, newest weekly window[, curl status]): the public
        JSON that publish wrote. Returns (procs, requests, alerts), one entry per
        publish, and sets self.state to the announced dates recorded, or None.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "site" / "website" / "public" / "data"
            data.mkdir(parents=True)
            home = root / "home"
            home.mkdir()
            if env_file:
                (home / ".claude-usage-notify.env").write_text(
                    "# names NOTIFY_SEND_SECRET in a comment first\nNOTIFY_SEND_SECRET=s3cr3t\n",
                    encoding="utf-8",
                )
            stub = root / "bin"
            stub.mkdir()
            cwd = root / "repo"
            cwd.mkdir()
            if notified is not None:
                (cwd / ".notified-change").write_text(notified + "\n", encoding="utf-8")
            procs, requests, alerts = [], [], []
            for publish in publishes:
                last_change, newest_window, curl_status = (*publish, "200")[:3]
                windows = [{"window_ending": newest_window}] if newest_window else []
                (data / "claude-usage.json").write_text(
                    json.dumps({"last_change": last_change, "weekly_windows": {"passive": {"by_window": windows}}}),
                    encoding="utf-8",
                )
                # A stub curl that never touches the network. It records the request
                # so the payload can be asserted, and prints the status curl -w would.
                curl = stub / "curl"
                curl.write_text(
                    "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$REQUEST_LOG\"\n"
                    f"printf '{curl_status}'\n",
                    encoding="utf-8",
                )
                curl.chmod(0o755)
                request_log, alert_log = root / "request.log", root / "alert.log"
                request_log.unlink(missing_ok=True)
                alert_log.unlink(missing_ok=True)
                env = dict(os.environ)
                env.update(
                    HOME=str(home),
                    SITE=str(root / "site"),
                    PATH=f"{stub}{os.pathsep}{env['PATH']}",
                    REQUEST_LOG=str(request_log),
                    ALERT_LOG=str(alert_log),
                )
                # The real alert_jonathan is defined by daily.sh outside notify_change and
                # runs tracker.alert; here it is a stub that records its subject and text.
                stub_alert = 'alert_jonathan() { printf \'%s\\n\' "$1" "$2" >> "$ALERT_LOG"; }'
                procs.append(subprocess.run(
                    ["bash", "-c", f"set -uo pipefail\n{stub_alert}\n{self.func}\nnotify_change"],
                    check=False,
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                ))
                self.assertEqual(procs[-1].returncode, 0, procs[-1].stderr)
                requests.append(request_log.read_text(encoding="utf-8") if request_log.exists() else "")
                alerts.append(alert_log.read_text(encoding="utf-8") if alert_log.exists() else "")
            state_path = cwd / ".notified-change"
            self.state = state_path.read_text(encoding="utf-8").split() if state_path.exists() else None
            return procs, requests, alerts

    @staticmethod
    def _payload(request: str) -> dict:
        return json.loads(next(ln for ln in request.splitlines() if ln.startswith("{")))

    CHANGE: ClassVar[dict] = {"date": "2026-09-11", "direction": "increased", "percent": 7, "model": "claude-opus-5"}
    # masterrig's daily history pushes: the newest window each one carries.
    W1, W2, W3, W4 = ("2026-09-15T02:30:00+00:00", "2026-09-16T02:30:00+00:00",
                      "2026-09-17T02:30:00+00:00", "2026-09-18T02:30:00+00:00")

    def test_posts_a_change_once_two_publishes_of_new_evidence_show_it(self) -> None:
        _procs, requests, alerts = self._publishes((self.CHANGE, self.W1), (self.CHANGE, self.W2))
        self.assertEqual((requests[0], alerts[0]), ("", ""), "one publish is not enough to announce")
        self.assertEqual(self.state, ["2026-09-11"])
        self.assertIn("https://alldonesites.com/api/notify/send", requests[1])
        # The secret must come from the assignment line, not the comment above it.
        self.assertIn("authorization: Bearer s3cr3t", requests[1])
        self.assertEqual(
            self._payload(requests[1]),
            {"date": "2026-09-11", "direction": "increased", "percent": 7, "model": "claude-opus-5"},
        )
        # Jonathan gets his own alert for the confirmed change, quoting the outcome.
        self.assertIn("Observed change: increased 7% on 2026-09-11 (claude-opus-5)", alerts[1])
        self.assertIn("HTTP 200", alerts[1])

    def test_hourly_republishes_of_the_same_windows_are_one_look(self) -> None:
        # The publisher runs hourly on a history that arrives daily: three publishes of
        # the same windows are one observation, and the next day's windows confirm it.
        _procs, requests, _alerts = self._publishes(
            (self.CHANGE, self.W1), (self.CHANGE, self.W1), (self.CHANGE, self.W1), (self.CHANGE, self.W2))
        self.assertEqual(requests[:3], ["", "", ""])
        self.assertEqual(self._payload(requests[3])["date"], "2026-09-11")

    def test_the_real_misdated_cut_is_never_announced_and_the_cut_is(self) -> None:
        # The committed history replayed as masterrig pushed it (tests/test_detect.py):
        # 2026-09-15's windows certify -19% dated 2026-08-28 all day, 2026-09-16's move
        # it to the real cut, and the next push that still shows the cut announces it.
        misdated = {"date": "2026-08-28", "direction": "decreased", "percent": 19, "scope": "weekly"}
        cut = {"date": "2026-09-14", "direction": "decreased", "percent": 29, "scope": "weekly"}
        _procs, requests, alerts = self._publishes(
            (misdated, self.W1), (misdated, self.W1), (cut, self.W2), (cut, self.W2),
            ({**cut, "percent": 28}, "2026-09-16T17:30:00.354502+00:00"))
        self.assertEqual(requests[:4], ["", "", "", ""])
        self.assertEqual(alerts[:4], ["", "", "", ""])
        self.assertEqual(self._payload(requests[4]),
                         {"date": "2026-09-14", "direction": "decreased", "percent": 28, "scope": "weekly"})
        self.assertEqual(self.state, ["2026-09-14"])

    def test_a_change_whose_date_moves_by_a_day_is_one_event_and_is_announced_once(self) -> None:
        # Window by window the real cut first dated itself 2026-09-13, then 2026-09-14.
        cut13 = {"date": "2026-09-13", "direction": "decreased", "percent": 29, "scope": "weekly"}
        cut14 = {"date": "2026-09-14", "direction": "decreased", "percent": 30, "scope": "weekly"}
        _procs, requests, _alerts = self._publishes(
            (cut13, "2026-09-15T16:30:00+00:00"), (cut14, "2026-09-15T21:30:00+00:00"),
            (cut13, self.W2), (cut13, self.W3), (cut14, self.W4))
        self.assertEqual(requests[0], "")
        self.assertEqual(self._payload(requests[1])["date"], "2026-09-14")
        self.assertEqual(requests[2:], ["", "", ""], "the same event must not be announced again")
        self.assertEqual(self.state, ["2026-09-14"])

    def test_a_publish_without_the_change_breaks_the_run(self) -> None:
        _procs, requests, _alerts = self._publishes(
            (self.CHANGE, self.W1), (None, self.W2), (self.CHANGE, self.W3), (self.CHANGE, self.W4))
        self.assertEqual(requests[:3], ["", "", ""])
        self.assertEqual(self._payload(requests[3])["date"], "2026-09-11")

    def test_skips_a_change_already_announced(self) -> None:
        # A one-line state file from before the run rule still counts, and so does a
        # date within a day of the announced one.
        for notified in ("2026-09-11", "2026-09-10"):
            with self.subTest(notified=notified):
                _procs, requests, alerts = self._publishes(
                    (self.CHANGE, self.W1), (self.CHANGE, self.W2), notified=notified)
                self.assertEqual(requests, ["", ""], "no request should be made for an announced change")
                self.assertEqual(alerts, ["", ""], "an announced change must not alert again")

    def test_a_new_announcement_is_added_to_the_dates_already_announced(self) -> None:
        self._publishes((self.CHANGE, self.W1), (self.CHANGE, self.W2), notified="2026-08-14")
        self.assertEqual(self.state, ["2026-08-14", "2026-09-11"])

    def test_skips_provisional_or_legacy_uncertain_evidence(self) -> None:
        for flag in ("provisional", "legacy_uncertain"):
            with self.subTest(flag=flag):
                flagged = {**self.CHANGE, flag: True}
                _procs, requests, alerts = self._publishes((flagged, self.W1), (flagged, self.W2))
                self.assertEqual(requests, ["", ""])
                self.assertEqual(alerts, ["", ""])
                self.assertIsNone(self.state)

    def test_skips_when_there_is_no_change(self) -> None:
        _procs, requests, _alerts = self._publishes((None, self.W1), (None, self.W2))
        self.assertIsNone(self.state)
        self.assertEqual(requests, ["", ""])

    def test_skips_when_the_env_file_is_missing(self) -> None:
        _procs, requests, _alerts = self._publishes((self.CHANGE, self.W1), (self.CHANGE, self.W2), env_file=False)
        self.assertIsNone(self.state)
        self.assertEqual(requests, ["", ""])

    def test_a_non_2xx_answer_is_a_warning_and_retries_on_the_next_publish(self) -> None:
        procs, requests, alerts = self._publishes(
            (self.CHANGE, self.W1), (self.CHANGE, self.W2, "500"), (self.CHANGE, self.W2))
        self.assertIn("will retry tomorrow", procs[1].stderr)
        # The alert still goes out, and says the list send failed.
        self.assertIn("Observed change: increased 7% on 2026-09-11 (claude-opus-5)", alerts[1])
        self.assertIn("HTTP 500", alerts[1])
        # Not recorded as announced, so the next publish sends it again and records it.
        self.assertEqual(self._payload(requests[2])["date"], "2026-09-11")
        self.assertEqual(self.state, ["2026-09-11"])

    def test_incomplete_change_is_skipped(self) -> None:
        _procs, requests, _alerts = self._publishes(({"date": "2026-09-11"}, self.W1), ({"date": "2026-09-11"}, self.W2))
        self.assertIsNone(self.state)
        self.assertEqual(requests, ["", ""])


SONNET_0939 = {"ts": "2026-09-06T09:39:26.287942+00:00", "model": "claude-sonnet-5", "effort": "low",
               "tokens_per_pct": 467778.6,
               "tokens": {"input": 86, "output": 215, "cache_read": 415767, "cache_write": 1922825},
               "prompts": 48, "tick_from": 1, "tick_to": 6, "elapsed_s": 4742.686796, "account": "dave"}
# The real 14:33 Sonnet row with cache_write raised from 1,323,388 to 1,337,560 so it
# still sits 16% above the 09:39 row now that cache_read carries no meter weight.
SONNET_1433 = {"ts": "2026-09-06T14:33:05.254749+00:00", "model": "claude-sonnet-5", "effort": "low",
               "tokens_per_pct": 584542.6666666666,
               "tokens": {"input": 86, "output": 215, "cache_read": 415767, "cache_write": 1337560},
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
            (repo / "data" / "prices.json").write_bytes(ROTATION_PRICES.read_bytes())
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
                                  capture_output=True, text=True, check=False)
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
        # Opus's first run: the dollar median of the one usable row, handed over as is
        # (#27); the probe sizes its own payload from it on Opus's prices.
        self.assertIn("--model claude-opus-5 --expect-usd-per-pct 0.9622", args[0])
        self.assertIn("--effort low", args[0])
        self.assertNotIn("--ticks", args[0])
        self.assertIn("drift check: ok claude-opus-5 $1.188/1%: no earlier prose row", proc.stdout)
        self.assertEqual(rows[-1]["model"], "claude-opus-5")
        self.assertEqual(alerts, "")
        self.assertRegex(log[0], r"^Probe \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z claude-opus-5$")
        self.assertEqual(log[1], "seed")

    def test_drift_reruns_with_two_ticks_and_flags_the_outlier(self):
        # 384,888 tpp is $0.962/1% (real prices.json), agreeing with the earlier median
        rerun = _row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 384888, ticks=2)
        proc, args, alerts, rows, log = self._run([SONNET_0939, FABLE], [SONNET_1433, rerun])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(len(args), 2)
        # first run: Sonnet's turn, expectation through the dollar invariant from the
        # median of the Sonnet and Fable dollar values (about $1.03 per 1%)
        prices = {k: v for k, v in json.loads(ROTATION_PRICES.read_text()).items() if not k.startswith("_")}
        expect = expectation([SONNET_0939, FABLE], "claude-sonnet-5", prices)
        self.assertIn(f"--model claude-sonnet-5 --expect-usd-per-pct {_usd(expect.usd_per_pct)}", args[0])
        self.assertGreater(expect.usd_per_pct, 0.97)
        self.assertLess(expect.usd_per_pct, 1.10)
        # rerun: same model, 2 ticks, the smaller of the median and the drifted reading,
        # in dollars (the probe converts to tokens itself since #27)
        price = prices["claude-sonnet-5"]
        median_usd = usd_per_pct(SONNET_0939, price)
        rerun_usd = min(median_usd, usd_per_pct(SONNET_1433, price))
        self.assertIn(f"--model claude-sonnet-5 --expect-usd-per-pct {_usd(rerun_usd)} --ticks 2", args[1])
        self.assertIn("drift check: drift claude-sonnet-5 $1.116/1% against median $0.962/1% (+16%)", proc.stdout)
        self.assertIn("decision: outlier claude-sonnet-5", proc.stdout)
        self.assertEqual([r.get("outlier", False) for r in rows], [False, False, True, False])
        self.assertIn("Outlier on claude-sonnet-5", alerts)
        self.assertNotIn("Change confirmed", alerts)
        self.assertEqual(log[0].split(": ")[-1], "outlier")
        self.assertRegex(log[1], r"^Probe rerun .* claude-sonnet-5$")
        self.assertRegex(log[2], r"^Probe .* claude-sonnet-5$")
        self.assertEqual(log[3], "seed")

    def test_drift_confirmed_by_the_rerun_is_a_change_and_flags_nothing(self):
        # 446,392 tpp is $1.116/1% (real prices.json), agreeing with the drifted reading
        rerun = _row("2026-09-06T16:00:00+00:00", "claude-sonnet-5", 446392, ticks=2)
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

    def test_first_run_failure_body_leads_with_a_plain_language_summary(self):
        proc, _args, alerts, _rows, log = self._run([SONNET_0939], [None], fake_rcs=[4])
        self.assertEqual(proc.returncode, 4, proc.stderr + proc.stdout)
        self.assertIn("Probe aborted on claude-opus-5", alerts)
        # tracker.report ran for real (the stub only fakes probe and alert): summary first,
        # the captured probe log under the rule.
        self.assertIn("Outcome: FAILED. Rotation run for Opus 5 stopped", alerts)
        self.assertIn("Debug log (last 1 of 1 lines of out/probe-last.log; tracker.probe exit code 4):", alerts)
        self.assertIn("fake probe 1 rc 4", alerts)
        self.assertEqual(log, ["seed"])

    def test_an_unknown_exit_code_is_reported_as_a_crash_not_swallowed(self):
        proc, _args, alerts, rows, log = self._run([SONNET_0939], [None], fake_rcs=[1])
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("Probe crashed on claude-opus-5 (exit 1)", alerts)
        self.assertIn("Outcome: CRASHED.", alerts)
        self.assertEqual(len(rows), 1)
        self.assertEqual(log, ["seed"])

    def test_no_usable_history_means_no_flags_and_no_probe(self):
        proc, args, alerts, _rows, _log = self._run([])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(args, [])
        self.assertIn("no usable prose row", proc.stderr)
        self.assertEqual(alerts, "")


if __name__ == "__main__":
    unittest.main()
