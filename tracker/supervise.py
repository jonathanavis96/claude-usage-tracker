"""Run a scheduled tracker job unattended: one at a time, retried, logged, bounded, alerted.

    python3 -m tracker.supervise --log ~/.paperclip/ops/claude-usage-passive.log -- bin/passive.sh

What one run does:

  1. Overlap guard. An exclusive non-blocking flock on --lock. A second run while one
     is live exits 0 at once ("skipped: previous run still going"), so a slow join can
     never stack cron runs. The kernel drops the lock when its holder dies, so a lock
     can never go stale; the pid file beside it is only for the health check, and a
     pid file whose process is dead is removed here before the run.
  2. Log rotation. When --log is over --max-log-bytes it moves to .1 (.1 to .2, up to
     --keep), and the command's stdout and stderr are appended to the fresh log.
  3. Retry with backoff. A non-zero exit is retried up to --retries more times, waiting
     --backoff, then twice that, and so on. Each attempt starts the command afresh,
     so it re-reads the OAuth token and the checkout.
  4. State. --state (JSON) records last_ok, last_fail, last_reason, consecutive_failures
     and whether an incident is open.
  5. Alert. An incident opens when consecutive_failures reaches --fail-threshold, or when
     tracker.health reports a failure that has lasted --stale-after seconds (default 1h).
     Opening sends ONE alert; while it stays open nothing more is sent; when a run
     succeeds and health is clean again ONE recovery message is sent and it closes.

  6. Dead-man ping. After a run that exited 0 with health clean, GET the Uptime Kuma push
     URL in --kuma-url-file (default ~/.config/claude-usage-tracker/kuma-push-url, outside
     the repo, never committed). A missing or empty file means no ping and no error, and
     a failed ping is only noted in the state file. Kuma alerts when the pings stop, which
     covers what nothing above can: cron, the host or this script no longer running.
     An incident is pushed to the same URL with status=down and the reason as msg, and its
     recovery with status=up, so gs (no route to pihome's WhatsApp) alerts through Kuma.
     WhatsApp, then email, are used only when that push fails.

--profile gs sets the gs defaults: lock, state and log under ~/.paperclip/ops, label gs,
the gs health checks (tracker.health.gs_config), no retries (bin/daily.sh runs again in
30 minutes and mails subscribers, so it is never rerun at once) and an incident after 2
failed runs in a row (an hour).

The sender is a function (`Sender`): the default ships the text over ssh to the pihome
WhatsApp bridge, `wa_send.py`, which prints OK or FAIL, and falls back to the tracker's
email alert (tracker.alert) when that fails, which is always the case on gs (no ssh route
to pihome). Setting CUT_ALERT_DRY_RUN=1, or
passing --dry-run, prints the message to stderr instead. Tests inject their own sender.
An alert that fails to send leaves the incident unopened, so the next run tries again.

Exit 3 from bin/passive.sh means the checkout is off main: a retry cannot fix that, so it
is not retried, counts as a failed run, and opens an incident on the first occurrence.

Exit status is the command's last exit status (0 when skipped).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from tracker import health

ROOT = Path(__file__).resolve().parent.parent
Sender = Callable[[str], bool]
#: bin/passive.sh's exit when the checkout is not on main; never retried.
EXIT_OFF_MAIN = 3
#: bin/passive.sh and bin/daily.sh: a pull's rebase stopped on a conflict and was aborted (7),
#: or a rebase/merge was already in progress (8). Neither clears by retrying, so both are
#: never retried and alert on the first run, like EXIT_OFF_MAIN.
EXIT_PULL_CONFLICT = 7
EXIT_OP_IN_PROGRESS = 8
EXIT_NEEDS_HAND = (EXIT_OFF_MAIN, EXIT_PULL_CONFLICT, EXIT_OP_IN_PROGRESS)
#: The alert's reason for a wrapper's own exit codes; any other code reads "exit N after M attempts".
EXIT_REASONS = {
    EXIT_OFF_MAIN: "checkout is off main (exit 3), run `git checkout main`",
    4: "tracker/ has uncommitted changes in the cron checkout (exit 4): commit or stash them",
    EXIT_PULL_CONFLICT: ("git pull hit a conflict with origin/main (exit 7); the rebase was aborted and the "
                         "checkout is clean: resolve by hand with `git pull --rebase origin main`"),
    EXIT_OP_IN_PROGRESS: "a rebase or merge is in progress in the checkout (exit 8): `git rebase --abort`",
}

WA_CMD = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "pihome",
          "python3 /home/grafe/wa-assistant/wa_send.py 27822227457"]


def whatsapp_sender(text: str) -> bool:
    try:
        r = subprocess.run(WA_CMD, input=text, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "OK" in r.stdout and "FAIL" not in r.stdout


def email_sender(text: str) -> bool:
    """The tracker's email path (tracker.alert, ~/.claude-usage-notify.env). gs has no
    ssh route to pihome, so there WhatsApp fails and this is what reaches Jonathan."""
    from tracker import alert
    cfg = alert.alert_config()
    if isinstance(cfg, str):
        return False
    try:
        status, _ = alert.send_alert(text.split(":")[0], text, cfg)
    except OSError:
        return False
    return 200 <= status < 300


def whatsapp_or_email_sender(text: str) -> bool:
    return whatsapp_sender(text) or email_sender(text)


def dry_run_sender(text: str) -> bool:
    print(f"[alert dry-run] {text}", file=sys.stderr)
    return True


def default_sender() -> Sender:
    return dry_run_sender if os.environ.get("CUT_ALERT_DRY_RUN") else whatsapp_or_email_sender


def kuma_url_file() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))) / ".config" / "claude-usage-tracker" / "kuma-push-url"


def _with_status(url: str, status: str, msg: str) -> str:
    """The push URL with Kuma's `status` and `msg` query parameters set (others kept)."""
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k not in ("status", "msg")]
    q += [("status", status), ("msg", msg[:250])]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def ping_deadman(url_file: Path | None, timeout: float = 10.0, status: str = "up", msg: str = "OK") -> str | None:
    """GET the push URL in `url_file` with status=up|down and msg. None when there is no
    file (no ping configured), else "ok" or a one-line failure. Never raises: a dead-man
    ping must not fail a run. Kuma marks the monitor down on status=down and notifies with
    msg, so an incident reaches Jonathan through the same channel as a stopped host."""
    if url_file is None:
        return None
    try:
        url = url_file.read_text().strip()
    except OSError:
        return None
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        return "failed: push URL file does not hold an http(s) URL"
    try:
        with urllib.request.urlopen(_with_status(url, status, msg), timeout=timeout) as r:
            return "ok" if 200 <= r.status < 300 else f"failed: HTTP {r.status}"
    except Exception as e:  # noqa: BLE001 - any failure is only recorded
        return f"failed: {type(e).__name__}"


def rotate_log(log: Path, max_bytes: int, keep: int) -> bool:
    try:
        if log.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    for i in range(keep - 1, 0, -1):
        src = log.with_name(f"{log.name}.{i}")
        if src.exists():
            src.replace(log.with_name(f"{log.name}.{i + 1}"))
    log.replace(log.with_name(f"{log.name}.1"))
    return True


def write_state(path: Path, state: dict) -> bool:
    """Save `state`; False (and a stderr line) when the disk refuses it."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
        tmp.replace(path)
        return True
    except OSError as e:
        print(f"cannot write state {path}: {e}", file=sys.stderr)
        return False


def clear_stale_pidfile(pidfile: Path) -> bool:
    try:
        text = pidfile.read_text().strip()
    except OSError:
        return False
    if text.isdigit() and health.pid_alive(int(text)) and int(text) != os.getpid():
        return False
    pidfile.unlink(missing_ok=True)
    return True


#: A run longer than this is killed (exit 124): a `git push` stalled on a half-open
#: connection would otherwise hold the lock, and silence every later run, for good.
RUN_TIMEOUT_S = 45 * 60


def _open_log(log: Path | None, header: str):
    """The run log opened for append with `header` written, or None when there is no log
    or the disk refuses it (full disk): the job still runs, its output discarded."""
    if not log:
        return None
    try:
        out = open(log, "a")
    except OSError as e:
        print(f"run log {log} unwritable ({e}); running without it", file=sys.stderr)
        return None
    try:
        out.write(header)
        out.flush()
        return out
    except OSError as e:
        print(f"run log {log} unwritable ({e}); running without it", file=sys.stderr)
        try:
            out.close()
        except OSError:
            pass
        return None


def run_with_retry(cmd: list[str], log: Path | None, retries: int, backoff: float,
                   sleep: Callable[[float], None] = time.sleep, timeout: float = RUN_TIMEOUT_S) -> int:
    code = 0
    for attempt in range(retries + 1):
        out = _open_log(log, f"--- {time.strftime('%Y-%m-%dT%H:%M:%S%z')} attempt {attempt + 1}: {' '.join(cmd)}\n")
        sink = out if out else (subprocess.DEVNULL if log else None)
        try:
            code = subprocess.run(cmd, stdout=sink, stderr=subprocess.STDOUT if sink else None,
                                  check=False, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            code = 124
            print(f"run killed after {int(timeout)} s: {' '.join(cmd)}", file=sys.stderr)
        except OSError as e:
            code = 127
            print(f"cannot start {cmd[0]}: {e}", file=sys.stderr)
        finally:
            if out:
                try:
                    out.close()
                except OSError:
                    pass
        if code == 0 or code in EXIT_NEEDS_HAND or attempt == retries:
            return code
        sleep(backoff * (2 ** attempt))
    return code


def decide_alert(state: dict, now: float, health_reason: str | None, fail_threshold: int,
                 stale_after: float, send: Sender, label: str, send_up: Sender | None = None) -> None:
    """Open or close an incident, sending at most one message per transition.

    `send` carries the opening message, `send_up` (default `send`) the recovery."""
    if health_reason:
        state.setdefault("unhealthy_since", now)
    else:
        state.pop("unhealthy_since", None)
    failing = (state.get("consecutive_failures", 0) >= fail_threshold
               or state.get("last_exit") in EXIT_NEEDS_HAND)
    stale = bool(health_reason) and now - state["unhealthy_since"] >= stale_after
    # What kind of fault this is: failed runs, or the failing health check's name. An open
    # incident of one kind must not swallow a later fault of another: that sends one more
    # message and the incident takes on the new kind.
    kinds = ({"runs"} if failing else set()) | ({health_reason.split(":")[0]} if stale else set())
    if state.get("incident_open") and kinds - set(state.get("incident_kinds", [])):
        new = sorted(kinds - set(state.get("incident_kinds", [])))
        reason = (f"{state['consecutive_failures']} runs in a row failed ({state.get('last_reason')})"
                  if "runs" in new else health_reason)
        if send(f"Claude usage tracker ({label}) has a further problem: {reason}. "
                f"Check: python3 -m tracker.health --all"):
            state["incident_kinds"] = sorted(set(state.get("incident_kinds", [])) | kinds)
            state["incident_reason"] = f"{state.get('incident_reason')}; {reason}"
    if not state.get("incident_open"):
        if failing or stale:
            reason = (f"{state['consecutive_failures']} runs in a row failed ({state.get('last_reason')})"
                      if failing else health_reason)
            if not send(f"Claude usage tracker ({label}) needs attention: {reason}. "
                        f"Check: python3 -m tracker.health --all"):
                state["last_alert_error"] = f"alert not sent at {now:.0f}: every channel failed"
                state["alert_failures"] = state.get("alert_failures", 0) + 1
                print(f"alert NOT sent (every channel failed): {reason}", file=sys.stderr)
            else:
                state.pop("alert_failures", None)
                state["incident_open"] = True
                state["incident_since"] = now
                state["incident_reason"] = reason
                state["incident_kinds"] = sorted(kinds)
    elif not failing and health_reason is None:
        mins = int((now - state.get("incident_since", now)) // 60)
        if (send_up or send)(f"Claude usage tracker ({label}) recovered after {mins} min: {state.get('incident_reason')}"):
            for k in ("incident_open", "incident_since", "incident_reason", "incident_kinds"):
                state.pop(k, None)


def _kuma_first(send: Sender, kuma_file: Path | None, state: dict) -> tuple[Sender, Sender]:
    """Senders that push the incident to Kuma first (status=down; status=up on recovery)
    and fall back to `send` (WhatsApp, then email) only when that push fails. With no
    push URL configured they are `send` itself."""
    if kuma_file is None:
        return send, send

    def via(status: str) -> Sender:
        def go(text: str) -> bool:
            ping = ping_deadman(kuma_file, status=status, msg=text)
            if ping is not None:
                state["last_ping"] = f"{status}: {ping}"
            return ping == "ok" or send(text)
        return go
    return via("down"), via("up")


def _alert_if_hung(pidfile: Path, state_path: Path, fail_threshold: int, send: Sender, label: str,
                   t: float) -> None:
    """A run holding the lock past health's lock limit is an incident, raised from here:
    the hung run itself will never get to its own health check."""
    reason = health.check_lock(health.Config(lock_pidfile=pidfile), time.time())
    if reason is None:
        return
    state = health.read_state(state_path)
    decide_alert(state, t, reason, fail_threshold, 0.0, send, label)
    write_state(state_path, state)


def supervise(cmd: list[str], *, lock: Path, state_path: Path, log: Path | None, retries: int = 2,
              backoff: float = 30.0, max_log_bytes: int = 5 * 1024 * 1024, keep: int = 3,
              fail_threshold: int = 3, stale_after: float = 3600.0, send: Sender | None = None,
              health_cfg: health.Config | None = None, label: str = "masterrig",
              sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time,
              kuma_file: Path | None = None) -> int:
    send = send or default_sender()
    pidfile = lock.with_suffix(".pid")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("skipped: previous run still going", file=sys.stderr)
            _alert_if_hung(pidfile, state_path, fail_threshold, send, label, now())
            return 0
        clear_stale_pidfile(pidfile)
        pidfile.write_text(str(os.getpid()))
        try:
            if log:
                rotate_log(log, max_log_bytes, keep)
            code = run_with_retry(cmd, log, retries, backoff, sleep)
            state = health.read_state(state_path)
            t = now()
            state["last_exit"] = code
            if code == 0:
                state["last_ok"] = t
                state["consecutive_failures"] = 0
            else:
                state["last_fail"] = t
                state["last_reason"] = EXIT_REASONS.get(code, f"exit {code} after {retries + 1} attempts")
                state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            cfg = health_cfg or health.Config(state=state_path, lock_pidfile=pidfile,
                                              logs=(log,) if log else ())
            if not write_state(state_path, state):  # the run check reads it
                # Nothing persists, so the in-a-row count cannot grow: alert on this run.
                state["consecutive_failures"] = max(fail_threshold, state.get("consecutive_failures", 0))
                state["last_reason"] = f"state file {state_path} unwritable (disk full or read-only)"
            reason = health.first_failure(cfg, t, skip=("lock",))
            send_down, send_up = _kuma_first(send, kuma_file, state)
            decide_alert(state, t, reason, fail_threshold, stale_after, send_down, label, send_up=send_up)
            state["last_health"] = reason or "ok"
            if code == 0 and reason is None and not state.get("incident_open"):
                ping = ping_deadman(kuma_file)
                if ping is not None:
                    state["last_ping"] = ping
            write_state(state_path, state)
            return code
        finally:
            pidfile.unlink(missing_ok=True)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--profile", choices=("masterrig", "gs"), default="masterrig")
    p.add_argument("--kuma-url-file", type=Path, default=None,
                   help="file holding the Uptime Kuma push URL (default ~/.config/claude-usage-tracker/kuma-push-url)")
    p.add_argument("--lock", type=Path)
    p.add_argument("--state", type=Path)
    p.add_argument("--log", type=Path)
    p.add_argument("--retries", type=int)
    p.add_argument("--backoff", type=float, default=30.0)
    p.add_argument("--max-log-bytes", type=int, default=5 * 1024 * 1024)
    p.add_argument("--keep", type=int, default=3)
    p.add_argument("--fail-threshold", type=int)
    p.add_argument("--stale-after", type=float, default=3600.0)
    p.add_argument("--label")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        p.error("no command given")
    gs = a.profile == "gs"
    lock = a.lock or (health.GS_LOCK if gs else ROOT / ".supervise.lock")
    state = a.state or (health.GS_STATE if gs else ROOT / ".supervise-state.json")
    log = a.log or (health.GS_LOG if gs else None)
    cfg = health.gs_config(state=state, pidfile=lock.with_suffix(".pid"), logs=(log,) if log else ()) if gs else None
    return supervise(cmd, lock=lock, state_path=state, log=log,
                     retries=a.retries if a.retries is not None else (0 if gs else 2), backoff=a.backoff,
                     max_log_bytes=a.max_log_bytes, keep=a.keep,
                     fail_threshold=a.fail_threshold if a.fail_threshold is not None else (2 if gs else 3),
                     stale_after=a.stale_after, label=a.label or a.profile, health_cfg=cfg,
                     send=dry_run_sender if a.dry_run else None, kuma_file=a.kuma_url_file or kuma_url_file())


if __name__ == "__main__":
    sys.exit(main())
