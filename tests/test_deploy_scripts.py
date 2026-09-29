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
import time
import unittest
from datetime import datetime, timedelta
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

    def _publishes(self, *publishes, notified=None, env_file=True, generated_at=None):
        """Run notify_change once per publish, in order, in one repo checkout.

        Each publish is (last_change, newest weekly window[, curl status[, feeds]]): the
        public JSON that publish wrote, where feeds maps an account label to its
        newest_stretch_end. `generated_at`, one stamp per publish, is each publish's own
        time, which the 24 and 48 hour rules measure from. Returns (procs, requests, alerts), one entry per
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
            for i, publish in enumerate(publishes):
                last_change, newest_window, *rest = publish
                curl_status = rest[0] if rest else "200"
                feeds = rest[1] if len(rest) > 1 else None
                windows = [{"window_ending": newest_window}] if newest_window else []
                public = {"last_change": last_change, "weekly_windows": {"passive": {"by_window": windows}}}
                if generated_at is not None:
                    public["generated_at"] = generated_at[i]
                if feeds is not None:
                    public["account_feeds"] = {label: {"newest_stretch_end": end, "state": "fresh"}
                                               for label, end in feeds.items()}
                (data / "claude-usage.json").write_text(json.dumps(public), encoding="utf-8")
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

    FIVE_HOUR: ClassVar[dict] = {"date": "2026-09-22", "direction": "increased", "percent": 38,
                                 "scope": "five_hour"}
    #: The live stall: the weekly windows stuck at 2026-09-24T18:10 for days.
    STALLED = "2026-09-24T18:10:00.103349+00:00"

    def test_a_newer_stretch_on_any_account_is_new_evidence_while_the_windows_stall(self) -> None:
        # The first publish carries a stretch newer than the windows; the next hour's is
        # the same look; the next day's brings a newer stretch on another account, in a
        # non-UTC offset, and the stalled windows no longer hold the change back.
        feeds1 = {"a1": "2026-09-25T21:38:21+00:00", "a2": "2026-09-25T11:37:07+00:00"}
        feeds2 = {"a1": "2026-09-25T21:38:21+00:00", "a2": "2026-09-26T09:00:00+02:00"}
        _procs, requests, alerts = self._publishes(
            (self.FIVE_HOUR, self.STALLED, "200", feeds1),
            (self.FIVE_HOUR, self.STALLED, "200", feeds1),
            (self.FIVE_HOUR, self.STALLED, "200", feeds2))
        self.assertEqual(requests[:2], ["", ""], "one look at the same evidence is not two")
        self.assertEqual(self._payload(requests[2]),
                         {"date": "2026-09-22", "direction": "increased", "percent": 38, "scope": "five_hour"})
        self.assertIn("Observed change: increased 38% on 2026-09-22", alerts[2])
        self.assertEqual(self.state, ["2026-09-22"])

    def test_stalled_windows_and_unchanged_stretches_never_announce(self) -> None:
        # The bug this gate fixes, the other way round: with neither source moving, hourly
        # publishes stay one look however many there are.
        feeds = {"a1": "2026-09-25T21:38:21+00:00"}
        _procs, requests, _alerts = self._publishes(
            *[(self.FIVE_HOUR, self.STALLED, "200", feeds)] * 4)
        self.assertEqual(requests, ["", "", "", ""])
        self.assertIsNone(self.state)

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

    #: A meter-measured change as the publisher writes it: its instant, state and interval.
    MEASURED: ClassVar[dict] = {"date": "2026-09-22", "at": "2026-09-22T19:41:49+00:00",
                                "direction": "decreased", "percent": 2, "scope": "undetermined",
                                "metric": "windows_per_week", "change_pct": -2.0,
                                "interval_pct": [-33.8, 56.5], "state": "measuring",
                                "interval_excludes_no_change": False,
                                "announced": {"quote": "an announcement never reaches the email"}}

    def _timed(self, change, *hours_after):
        """Two publishes of new evidence per stamp, `hours_after` the change instant."""
        at = datetime.fromisoformat(change["at"])
        stamps, publishes = [], []
        for n, h in enumerate(hours_after):
            stamp = (at + timedelta(hours=h)).isoformat()
            stamps += [stamp, stamp]
            publishes += [(change, f"2026-09-2{n}T0{n}:00:00+00:00"),
                          (change, f"2026-09-2{n}T0{n}:30:00+00:00")]
        return self._publishes(*publishes, generated_at=stamps)

    def test_no_email_before_the_change_is_24_hours_old(self) -> None:
        settled = {**self.MEASURED, "interval_excludes_no_change": True}
        procs, requests, alerts = self._timed(settled, 23.5)
        self.assertEqual(requests, ["", ""])
        self.assertEqual(alerts, ["", ""])
        self.assertIn("under 24", procs[1].stderr)
        self.assertIsNone(self.state)

    def test_from_24_hours_a_change_whose_interval_excludes_no_change_is_sent(self) -> None:
        settled = {**self.MEASURED, "interval_excludes_no_change": True, "state": "provisional"}
        _procs, requests, alerts = self._timed(settled, 24.5)
        payload = self._payload(requests[1])
        self.assertEqual(payload["change_pct"], -2.0)
        self.assertEqual(payload["state"], "provisional")
        self.assertEqual(payload["metric"], "windows_per_week")
        self.assertNotIn("announced", json.dumps(payload))
        self.assertNotIn("announcement", alerts[1])
        self.assertIn("-2% [-33.8, +56.5] windows_per_week, provisional", alerts[1])
        self.assertEqual(self.state, ["2026-09-22"])

    def test_between_24_and_48_hours_an_unsettled_interval_waits(self) -> None:
        procs, requests, _alerts = self._timed(self.MEASURED, 30)
        self.assertEqual(requests, ["", ""])
        self.assertIn("waiting for 48", procs[1].stderr)

    def test_at_48_hours_the_change_is_sent_at_its_figure_then(self) -> None:
        _procs, requests, _alerts = self._timed(self.MEASURED, 30, 48.5)
        self.assertEqual(requests[:2], ["", ""])
        payload = self._payload(requests[2])
        self.assertEqual((payload["date"], payload["change_pct"], payload["state"]),
                         ("2026-09-22", -2.0, "measuring"))
        self.assertEqual(requests[3], "", "never the same change twice")
        self.assertEqual(self.state, ["2026-09-22"])

    def test_the_email_carries_the_fitted_five_hour_and_weekly_figures(self) -> None:
        # A separable joint fit: the five-hour and weekly limit changes, each with its
        # interval. The publisher reads `interval_excludes_no_change` on the larger of the two.
        fitted = {**self.MEASURED, "metric": "five_hour_limit", "percent": 30,
                  "direction": "increased", "change_pct": 30.5, "interval_pct": [-2.0, 62.7],
                  "weekly_limit_change_pct": 27.9, "weekly_limit_change_interval_pct": [-8.4, 78.7],
                  "state": "measured", "interval_excludes_no_change": False}
        _procs, requests, alerts = self._timed(fitted, 30, 48.5)
        self.assertEqual(requests[:2], ["", ""], "an unsettled interval waits for 48 hours")
        payload = self._payload(requests[2])
        self.assertEqual((payload["change_pct"], payload["interval_pct"]), (30.5, [-2.0, 62.7]))
        self.assertEqual((payload["weekly_limit_change_pct"], payload["weekly_limit_change_interval_pct"]),
                         (27.9, [-8.4, 78.7]))
        self.assertEqual(payload["metric"], "five_hour_limit")
        self.assertNotIn("announced", json.dumps(payload))
        self.assertIn("+30.5% [-2, +62.7] five_hour_limit, weekly limit +27.9% [-8.4, +78.7], measured",
                      alerts[2])

    def test_a_change_already_notified_is_never_sent_again_after_48_hours(self) -> None:
        _procs, requests, _alerts = self._publishes(
            (self.MEASURED, self.W1), (self.MEASURED, self.W2), notified="2026-09-22",
            generated_at=["2026-09-26T00:00:00+00:00"] * 2)
        self.assertEqual(requests, ["", ""])

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
            self._git("-c", "user.name=t", "-c", "user.email=t", "commit", "-q", "-m", "seed", cwd=repo)
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


class TestPassiveShGuard(unittest.TestCase):
    """bin/passive.sh runs hourly and skips unless its last success is over 20 hours old.

    The script is run for real against a scratch git clone with a bare origin. Its three
    python steps are stub `tracker` modules in the clone, which record that they ran and
    write the history files, so the guard, the commit, the push and the stamp are the
    script's own.
    """

    STAMP = ".passive-last-ok"

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        origin, self.repo = tmp / "origin.git", tmp / "repo"
        self._git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        self._git(tmp, "clone", "-q", str(origin), str(self.repo))
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "passive.sh").write_text((BIN / "passive.sh").read_text())
        (self.repo / "history").mkdir()
        pkg = self.repo / "tracker"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        for name, out in (("passive", "history/passive.json"),
                          ("gs_passive", "history/masterrig-passive.json"),
                          ("speed", "history/masterrig-speed.json")):
            (pkg / f"{name}.py").write_text(
                "import pathlib, time\n"
                "pathlib.Path('ran').open('a').write('" + name + "\\n')\n"
                f"pathlib.Path('{out}').write_text(str(time.time_ns()))\n")
        (self.repo / ".gitignore").write_text(f"{self.STAMP}\nran\ntracker/\n")
        # One tracked file stands for the join code; the stubs stay untracked.
        (pkg / "version").write_text("1")
        self._git(self.repo, "add", ".gitignore", "bin/passive.sh")
        self._git(self.repo, "add", "-f", "tracker/version")
        self._git(self.repo, "-c", "user.name=t", "-c", "user.email=t", "commit", "-q", "-m", "init")
        self._git(self.repo, "push", "-q", "origin", "HEAD:main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.repo / "bin" / "passive.sh"), *args], cwd=self.repo,
                              capture_output=True, text=True, check=False, timeout=60)

    def _ran(self) -> bool:
        return (self.repo / "ran").exists()

    def _tracker_tree(self, rev: str = "HEAD") -> str:
        return subprocess.run(["git", "rev-parse", f"{rev}:tracker"], cwd=self.repo,
                              capture_output=True, text=True, check=True).stdout.strip()

    def _stamp_age(self, hours: float, code: str | None = None) -> None:
        stamp = self.repo / self.STAMP
        stamp.write_text(self._tracker_tree() if code is None else code)
        when = stamp.stat().st_mtime - hours * 3600
        os.utime(stamp, (when, when))

    def test_a_missing_stamp_runs_pushes_and_writes_the_stamp(self) -> None:
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._ran())
        self.assertTrue((self.repo / self.STAMP).exists())
        log = subprocess.run(["git", "log", "--oneline", "origin/main"], cwd=self.repo,
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("Passive history", log)

    def test_a_fresh_stamp_skips_the_run(self) -> None:
        self._stamp_age(1)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self._ran())

    def test_a_stale_stamp_runs(self) -> None:
        self._stamp_age(21)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._ran())
        self.assertLess(time.time() - (self.repo / self.STAMP).stat().st_mtime, 60)

    def test_force_runs_despite_a_fresh_stamp(self) -> None:
        self._stamp_age(1)
        self.assertEqual(self._run("--force").returncode, 0)
        self.assertTrue(self._ran())

    def test_a_failed_step_writes_no_stamp(self) -> None:
        (self.repo / "tracker" / "passive.py").write_text("raise SystemExit(3)\n")
        self.assertNotEqual(self._run().returncode, 0)
        self.assertFalse((self.repo / self.STAMP).exists())

    def test_the_joins_run_on_the_code_main_holds_now(self) -> None:
        # A commit lands on main after this checkout's last pull. The joins must run on it:
        # a pull only before the commit left each record counted by the previous day's code.
        other = self.repo.parent / "other"
        self._git(self.repo.parent, "clone", "-q", str(self.repo.parent / "origin.git"), str(other))
        (other / "code-version").write_text("main now")
        self._git(other, "add", "code-version")
        self._git(other, "-c", "user.name=t", "-c", "user.email=t", "commit", "-q", "-m", "new code")
        self._git(other, "push", "-q", "origin", "HEAD:main")
        (self.repo / "tracker" / "gs_passive.py").write_text(
            "import pathlib\n"
            "v = pathlib.Path('code-version')\n"
            "pathlib.Path('ran').open('a').write('gs_passive ' + (v.read_text() if v.exists() else 'old') + '\\n')\n"
            "pathlib.Path('history/masterrig-passive.json').write_text('{}')\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gs_passive main now", (self.repo / "ran").read_text())

    def _push_from_other(self, path: str, body: str) -> None:
        other = self.repo.parent / "other"
        if not other.exists():
            self._git(self.repo.parent, "clone", "-q", str(self.repo.parent / "origin.git"), str(other))
        (other / path).parent.mkdir(parents=True, exist_ok=True)
        (other / path).write_text(body)
        self._git(other, "add", "-f", path)
        self._git(other, "-c", "user.name=t", "-c", "user.email=t", "commit", "-q", "-m", path)
        self._git(other, "push", "-q", "origin", "HEAD:main")

    def test_the_stamp_records_the_join_code_it_ran_on(self) -> None:
        self.assertEqual(self._run().returncode, 0)
        self.assertEqual((self.repo / self.STAMP).read_text(), self._tracker_tree())

    def test_new_join_code_on_main_runs_despite_a_fresh_stamp(self) -> None:
        # 2026-09-29: PR #99 changed tracker/turns.py; gs's record took it at once, and
        # masterrig's stayed on the old count for a day behind a fresh stamp.
        self._stamp_age(1)
        self._push_from_other("tracker/version", "2")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._ran())
        self.assertIn("tracker/ changed", proc.stderr)
        self.assertEqual((self.repo / "tracker" / "version").read_text(), "2")
        self.assertEqual((self.repo / self.STAMP).read_text(), self._tracker_tree())
        # The next hourly tick finds the stamp matching main again and skips.
        (self.repo / "ran").unlink()
        self.assertEqual(self._run().returncode, 0)
        self.assertFalse(self._ran())

    def test_other_commits_on_main_do_not_run_a_fresh_stamp(self) -> None:
        self._stamp_age(1)
        self._push_from_other("history/probes.jsonl", "{}\n")
        self.assertEqual(self._run().returncode, 0)
        self.assertFalse(self._ran())

    def test_a_stamp_from_before_the_code_check_runs_once(self) -> None:
        self._stamp_age(1, code="")
        self.assertEqual(self._run().returncode, 0)
        self.assertTrue(self._ran())

    def test_no_origin_tree_to_compare_keeps_the_age_rule(self) -> None:
        self._stamp_age(1, code="stale")
        self._git(self.repo, "remote", "set-url", "origin", str(self.repo.parent / "gone.git"))
        self._git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(self._run().returncode, 0)
        self.assertFalse(self._ran())

    def test_a_failed_push_writes_no_stamp(self) -> None:
        self._git(self.repo, "remote", "set-url", "origin", str(self.repo.parent / "gone.git"))
        proc = self._run()
        self.assertIn("commit made locally only", proc.stderr)
        self.assertFalse((self.repo / self.STAMP).exists())


class TestDailySiteSync(unittest.TestCase):
    """Run bin/daily.sh's site_pull and site_push against a scratch bare origin.

    On 2026-09-29 a site PR merged while the publisher's checkout held unpushed data
    commits; a plain pull refused the divergent branches and every push after it was
    rejected, so the live page stopped updating.
    """

    functions: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        text = (BIN / "daily.sh").read_text()
        found = [re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.DOTALL | re.MULTILINE)
                 for name in ("site_pull", "site_push")]
        assert all(found), "site_pull/site_push not found in bin/daily.sh"
        cls.functions = "\n".join(m.group(0) for m in found)

    def _git(self, *args: str, cwd: Path) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def _commit(self, repo: Path, name: str, body: str) -> None:
        (repo / name).write_text(body)
        self._git("add", name, cwd=repo)
        self._git("commit", "-q", "-m", name, cwd=repo)

    def _diverged(self, tmp: Path, *, conflict: bool = False) -> tuple[Path, Path]:
        """A site checkout one data commit ahead of origin, and origin one merge ahead of it."""
        origin, site, other = tmp / "origin.git", tmp / "site", tmp / "other"
        self._git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp)
        self._git("clone", "-q", str(origin), str(site), cwd=tmp)
        self._commit(site, "data.json", "0")
        self._git("push", "-q", "origin", "main", cwd=site)
        self._git("clone", "-q", str(origin), str(other), cwd=tmp)
        self._commit(site, "data.json", "1")  # the publisher's unpushed data commit
        self._commit(other, "data.json" if conflict else "article.txt", "merged")
        self._git("push", "-q", "origin", "main", cwd=other)
        return origin, site

    def _run(self, site: Path, call: str) -> subprocess.CompletedProcess:
        script = f'SITE="{site}"\n{self.functions}\n{call}\n'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)

    def test_pull_rebases_local_data_commits_onto_a_site_merge(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            origin, site = self._diverged(Path(d))
            self.assertEqual(self._run(site, "site_pull").returncode, 0)
            self.assertEqual(self._git("rev-list", "--count", "HEAD..origin/main", cwd=site), "0")
            self.assertTrue((site / "article.txt").exists())
            self.assertEqual((site / "data.json").read_text(), "1")

    def test_rejected_push_rebases_once_and_lands(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            origin, site = self._diverged(Path(d))  # no site_pull first: the push is rejected
            result = self._run(site, "site_push")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._git("rev-parse", "main", cwd=origin),
                             self._git("rev-parse", "HEAD", cwd=site))

    def test_a_conflicting_rebase_is_aborted_and_the_push_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            origin, site = self._diverged(Path(d), conflict=True)
            result = self._run(site, "site_push")
            self.assertEqual(result.returncode, 1)
            self.assertIn("git push to site repo failed", result.stderr)
            self.assertFalse((site / ".git" / "rebase-merge").exists())
            self.assertFalse((site / ".git" / "rebase-apply").exists())


class TestDailyRatesDue(unittest.TestCase):
    """Run bin/daily.sh's rates_due against a scratch repo.

    On 2026-09-29 PR #101 changed tools/model_rates.py at 10:46Z and the 11:01Z publish
    priced at the 00:01Z fit, because the refit ran once a day.
    """

    function: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        found = re.search(r"^rates_due\(\) \{.*?^\}$", (BIN / "daily.sh").read_text(),
                          re.DOTALL | re.MULTILINE)
        assert found, "rates_due not found in bin/daily.sh"
        cls.function = found.group(0)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self._git("init", "-q", "-b", "main")
        (self.repo / "history").mkdir()
        (self.repo / "tools").mkdir()
        # Midday today, so a run near midnight never puts "a minute ago" on another day.
        self.now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str, when: datetime | None = None) -> None:
        stamp = (when or datetime.now().astimezone()).isoformat()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
               "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        subprocess.run(["git", *args], cwd=self.repo, env=env, check=True, capture_output=True)

    def _commit(self, path: str, when: datetime) -> None:
        (self.repo / path).write_text(str(when))
        self._git("add", path)
        self._git("commit", "-q", "-m", path, when=when)

    def _fit_at(self, when: datetime) -> None:
        (self.repo / "history" / "model-rates.json").write_text(
            json.dumps({"_meta": {"generated_at": when.isoformat()}}))

    def _due(self) -> bool:
        script = f"{self.function}\nrates_due\n"
        return subprocess.run(["bash", "-c", script], cwd=self.repo, check=False,
                              capture_output=True).returncode == 0

    def test_a_fit_from_today_with_nothing_newer_is_kept(self) -> None:
        self._commit("tools/model_rates.py", self.now - timedelta(minutes=2))
        self._fit_at(self.now - timedelta(minutes=1))
        self.assertFalse(self._due())

    def test_a_fit_from_an_earlier_day_is_refitted(self) -> None:
        self._commit("tools/model_rates.py", self.now - timedelta(days=3))
        self._fit_at(self.now - timedelta(days=1))
        self.assertTrue(self._due())

    def test_new_fit_code_after_todays_fit_is_refitted(self) -> None:
        self._fit_at(self.now - timedelta(minutes=2))
        self._commit("tools/model_rates.py", self.now - timedelta(minutes=1))
        self.assertTrue(self._due())

    def test_a_new_masterrig_record_after_todays_fit_is_refitted(self) -> None:
        self._fit_at(self.now - timedelta(minutes=2))
        self._commit("history/masterrig-passive.json", self.now - timedelta(minutes=1))
        self.assertTrue(self._due())

    def test_other_commits_after_todays_fit_do_not_refit(self) -> None:
        self._commit("tools/model_rates.py", self.now - timedelta(minutes=3))
        self._fit_at(self.now - timedelta(minutes=2))
        self._commit("history/gs-passive.json", self.now - timedelta(minutes=1))
        self.assertFalse(self._due())

    def test_a_branch_committed_before_the_fit_and_merged_after_it_is_refitted(self) -> None:
        # The branch's own commit predates the fit; only the merge follows it.
        self._commit("history/gs-passive.json", self.now - timedelta(minutes=5))
        self._git("checkout", "-q", "-b", "feature")
        self._commit("tools/model_rates.py", self.now - timedelta(minutes=4))
        self._git("checkout", "-q", "main")
        self._fit_at(self.now - timedelta(minutes=3))
        self._commit("history/probes.jsonl", self.now - timedelta(minutes=2))
        self._git("merge", "-q", "--no-ff", "-m", "merge", "feature",
                  when=self.now - timedelta(minutes=1))
        self.assertTrue(self._due())

    def test_a_missing_or_unreadable_fit_is_refitted(self) -> None:
        self.assertTrue(self._due())
