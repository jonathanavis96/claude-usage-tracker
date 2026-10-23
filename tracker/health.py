"""One health check for the masterrig side of the tracker.

    python3 -m tracker.health            # exit 0 "ok", or exit 1 and one line naming the first failure
    python3 -m tracker.health --all      # every check, one line each

Checks, in order (the first failure is the one-line reason):

  collection   the newest meter sample (last line's `ts` in the usage log, default
               moonlighter's ~/.moonlighter/usage_log.jsonl, sampled every 30 min) is
               younger than two intervals.
  run          the supervisor's last successful passive run (tracker/supervise.py state
               file, `last_ok`) is younger than two intervals of the hourly cron, and the
               last run was not a withheld publish (bin/daily.sh exit 9).
  publisher    the last commit touching history/gs-passive.json (gs's "Daily publisher
               state", every 30 min) is younger than --publisher-max-age.
  page         the public JSON's own `generated_at` is younger than 2 h: on gs the site
               checkout's copy, on masterrig the live URL (--page-url). A fetch that has
               failed for 2 h in a row is its own reason; a shorter failure passes.
  token        ~/.claude/.credentials.json is readable and claudeAiOauth.expiresAt is
               not further in the past than --token-grace. Claude Code refreshes the
               access token only when it runs, so an idle few hours is normal; a token
               long expired, or an expired refresh token, means a re-login is needed.
  lock         no lock pid file whose process is dead, and none older than --lock-max-age.
  sizes        each log under --max-log-bytes and the history/ directory under
               --max-history-bytes.
  meters       (gs, masterrig) each per-account meter log's newest usable reading (a line
               with `five_hour`, not a logged gap) is younger than --meter-max-age. The
               timers read every minute and 429s are routine, so 15 min is a stopped
               timer or an account whose reads all fail, never one slow tick. It is far
               above the ~130 s between reads that meter_log's 110 s spacing gives
               (a skipped tick exits 0 and writes nothing). Also fails when more than
               half of the last 40+ lines are 429s (2026-09-23: 55%, unseen nine days).
               A logged Retry-After that meter_log is honouring extends the age limit
               by its length (at most 2 h). An account whose reads since its newest
               reading are all auth_expired is idle (its access token lapsed and only
               Claude Code refreshes it), not broken: it fails only when its refresh
               token has expired, or after 24 h. A header-probe log (meter_log
               --token-env) whose newest line is a reading in a lapsed five-hour window
               or at a 100% seven-day meter is held, not stale: meter_log writes
               nothing while it holds. Trailing `token_rejected` lines fail with that
               reason. Every failing log is reported, joined
               with " | ", so each account is its own incident in the supervisor.
               An account the publisher uses (one history/gs-passive.json holds) is
               blind once its meter has not read for an hour, idle token or held probe
               or not: what it spends on another host meanwhile is not measured.
  advisory     (gs profile) no advisory step of bin/daily.sh (list_prices, contributed,
               gs_passive, the model_rates refit) has failed on every run for 3 h.

Profiles pick which checks run. `masterrig` (default, masterrig_config) is collection,
run, publisher, page, token, lock, sizes and meters, the last on a1's own meter log
(deploy/systemd-masterrig/claude-usage-meter-a1.timer): moonlighter's 30-minute log is
too sparse for the passive join (MAX_PAIR_GAP is 15 min), so a1 forms stretches only
while that timer reads, and the 5-minute reader before it went blind for two days
(2026-10-18) while every check here said ok. `gs` is run (the supervised bin/daily.sh,
every 30 min), page, meters, lock, sizes and advisory: gs has no moonlighter log, and its
publisher is the job itself. A bare Config() keeps every check but meters and advisory,
with no meter logs and no live page.

Each check reads its file fresh on every call; nothing is cached between runs, except when
the live page's fetches started failing (~/.cache/claude-usage-tracker/page-fetch.json).
Timestamps are parsed, never compared as strings.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .meter_log import MASTERRIG_METER_LOG

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("HOME", str(Path.home())))


@dataclass
class Config:
    usage_log: Path = HOME / ".moonlighter" / "usage_log.jsonl"
    collection_interval_s: int = 30 * 60
    state: Path = ROOT / ".supervise-state.json"
    run_interval_s: int = 60 * 60
    repo: Path = ROOT
    publisher_path: str = "history/gs-passive.json"
    publisher_max_age_s: int = 2 * 60 * 60
    credentials: Path = HOME / ".claude" / ".credentials.json"
    token_grace_s: int = 12 * 60 * 60
    lock_pidfile: Path = ROOT / ".supervise.pid"
    lock_max_age_s: int = 2 * 60 * 60
    logs: tuple[Path, ...] = ()
    max_log_bytes: int = 5 * 1024 * 1024
    history_dir: Path = ROOT / "history"
    max_history_bytes: int = 200 * 1024 * 1024
    meter_logs: tuple[Path, ...] = ()
    meter_max_age_s: int = 15 * 60
    meter_config_dirs: dict | None = None  # account -> config dir; None: GS_METER_CONFIG_DIRS under HOME
    #: The stretch record whose accounts the publisher uses (gs: history/gs-passive.json).
    #: A meter log of one of those accounts is blind after meter_blind_s, whatever the reason.
    passive_record: Path | None = None
    meter_blind_s: int = 60 * 60
    #: The page: the site checkout's JSON on gs (`page_json`), the live URL on masterrig
    #: (`page_url`); None: not checked.
    page_json: Path | None = None
    page_url: str | None = None
    page_max_age_s: int = 2 * 60 * 60
    page_fetch_state: Path = HOME / ".cache" / "claude-usage-tracker" / "page-fetch.json"
    page_fetch_grace_s: int = 2 * 60 * 60
    #: bin/daily.sh's advisory steps (tracker/advisory.py); None: not checked.
    advisory_state: Path | None = None
    advisory_grace_s: int = 3 * 60 * 60
    checks: tuple[str, ...] | None = None  # None: every check but meters and advisory (masterrig)


GS_OPS = HOME / ".paperclip" / "ops"
GS_METER_ACCOUNTS = ("avis", "dave", "jwork")
GS_LOCK = GS_OPS / "claude-usage-daily.lock"   # the supervisor's flock; its pid file is .pid
GS_STATE = GS_OPS / "claude-usage-daily-state.json"
GS_LOG = GS_OPS / "claude-usage-daily.log"
GS_ADVISORY = GS_OPS / "claude-usage-advisory.json"
#: The site checkout bin/daily.sh publishes into, and the JSON in it.
GS_SITE = HOME / "all-done-sites-platform"
SITE_JSON = Path("website") / "public" / "data" / "claude-usage.json"
#: The page as readers get it; masterrig reads it, so a push or a Pages deploy that fails
#: after gs is done is seen from outside gs too.
LIVE_PAGE_URL = "https://alldonesites.com/data/claude-usage.json"


def gs_config(state: Path, pidfile: Path, logs: tuple[Path, ...] = (), ops: Path = GS_OPS,
              repo: Path = ROOT, site: Path = GS_SITE) -> Config:
    """Health on gs: the supervised publisher run every 30 min, the age of the JSON it
    published, the three meter timers and the advisory steps of bin/daily.sh."""
    return Config(state=state, run_interval_s=30 * 60, lock_pidfile=pidfile, logs=logs, repo=repo,
                  history_dir=repo / "history",
                  meter_logs=tuple(ops / f"claude-usage-meter-{a}.log" for a in GS_METER_ACCOUNTS),
                  passive_record=repo / "history" / "gs-passive.json",
                  page_json=site / SITE_JSON, advisory_state=ops / GS_ADVISORY.name,
                  checks=("run", "page", "meters", "lock", "sizes", "advisory"))


#: masterrig's own account and its config dir under HOME: the default ~/.claude, which
#: deploy/systemd-masterrig/claude-usage-meter-a1.service samples into MASTERRIG_METER_LOG.
MASTERRIG_METER_CONFIG_DIRS = {"a1": ".claude"}
MASTERRIG_CHECKS = ("collection", "run", "publisher", "page", "token", "lock", "sizes", "meters")


def masterrig_config(*, ops: Path = GS_OPS, home: Path = HOME, meter_logs: tuple[Path, ...] = (),
                     checks: tuple[str, ...] = MASTERRIG_CHECKS, **kw) -> Config:
    """Health on masterrig as the supervisor and the CLI run it: every masterrig check, with
    the live page (which a bare Config, as tests build one, leaves out) and `meters` on a1's
    own log (or the given ones). Other keyword arguments are Config's, so a caller can set
    any of them, page_url=None among them.

    The a1 log is the only reading of that account close enough together to pair, and
    the same failures that stop a gs timer stop it -- a stopped timer, a refusal (exit 2:
    signed out, or the log names another account), a run of 429s -- so it gets the same
    check, 15 min by default against its ~130 s reads. A lapsed token on an idle a1 is
    told from a dead feed by ~/.claude's refresh-token expiry, as on gs."""
    kw.setdefault("page_url", LIVE_PAGE_URL)
    return Config(meter_logs=meter_logs or (ops / MASTERRIG_METER_LOG,),
                  meter_config_dirs={a: home / d for a, d in MASTERRIG_METER_CONFIG_DIRS.items()},
                  checks=checks, **kw)


#: A stamp further in the future than this is clock skew, which would otherwise hide
#: staleness by exactly the skew.
CLOCK_SKEW_S = 10 * 60


def _parse_ts(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value / 1000 if value > 1e11 else value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _last_line(path: Path) -> dict | None:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 65536))
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            return row
    return None


def _age(seconds_ago: float) -> str:
    m = int(seconds_ago // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def check_collection(c: Config, now: float) -> str | None:
    try:
        rows = _tail_rows(c.usage_log, n=10_000)
    except OSError as e:
        return f"collection: cannot read {c.usage_log}: {e.strerror}"
    # The newest real reading: an error row carries a ts but no utilization.
    row = next((r for r in reversed(rows) if isinstance(r.get("five_hour"), dict)
                and isinstance(r["five_hour"].get("utilization"), (int, float))), None)
    ts = _parse_ts(row.get("ts")) if row else None
    if ts is None:
        return f"collection: no reading (a line with five_hour utilization) near the end of {c.usage_log}"
    if ts - now > CLOCK_SKEW_S:
        return f"collection: newest sample is stamped {_age(ts - now)} in the future (clock skew)"
    if now - ts > 2 * c.collection_interval_s:
        return f"collection: newest meter sample is {_age(now - ts)} old (limit {_age(2 * c.collection_interval_s)})"
    return None


def read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def check_run(c: Config, now: float) -> str | None:
    st = read_state(c.state)
    last_ok = st.get("last_ok")
    last_fail = st.get("last_fail")
    if st.get("last_exit") == 3 and isinstance(last_fail, (int, float)) and last_fail >= (last_ok or 0):
        return "run: bin/passive.sh refused to run, the checkout is off main (exit 3)"
    # A withheld publish (bin/daily.sh exit 9, supervise.EXIT_WITHHELD) fails at once: the run
    # finished and is recent, so the age rule below read it as healthy for nine days.
    if st.get("last_exit") == 9 and isinstance(last_fail, (int, float)) and last_fail >= (last_ok or 0):
        since = f", last successful run {_age(now - last_ok)} ago" if isinstance(last_ok, (int, float)) else ""
        return f"run: bin/daily.sh withheld the public JSON (exit 9){since}: the page is not updating"
    if not isinstance(last_ok, (int, float)):
        return f"run: no successful supervised run recorded in {c.state.name}"
    if now - last_ok > 2 * c.run_interval_s:
        reason = st.get("last_reason") or "unknown"
        return f"run: last successful run {_age(now - last_ok)} ago, last failure: {reason}"
    return None


def check_publisher(c: Config, now: float) -> str | None:
    # The newest of HEAD and its upstream: on masterrig bin/passive.sh fetches origin every
    # hour but pulls only about once a day, so HEAD alone would read gs's half-hourly
    # publisher commits as a day old and alert every day.
    stamps = []
    for rev in ("HEAD", "@{upstream}"):
        try:
            out = subprocess.run(["git", "-C", str(c.repo), "log", "-1", "--format=%ct", rev, "--", c.publisher_path],
                                 capture_output=True, text=True, timeout=30, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"publisher: git log failed: {e}"
        if out.isdigit():
            stamps.append(int(out))
    if not stamps:
        return f"publisher: no commit touches {c.publisher_path}"
    # bin/daily.sh records a failing site push in history/site-push.json: the tracker
    # state above stays fresh while the page itself does not update.
    for rev in ("@{upstream}", "HEAD"):
        try:
            raw = subprocess.run(["git", "-C", str(c.repo), "show", f"{rev}:history/site-push.json"],
                                 capture_output=True, text=True, timeout=30, check=False).stdout
            site = json.loads(raw) if raw.strip() else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            site = None
        if isinstance(site, dict):
            if site.get("ok") is False:
                return (f"publisher: the site push is failing (last failure {site.get('at')}, "
                        f"last success {site.get('last_ok') or 'unknown'}); the page is not updating")
            break
    age = now - max(stamps)
    if age > c.publisher_max_age_s:
        return f"publisher: {c.publisher_path} last committed {_age(age)} ago (limit {_age(c.publisher_max_age_s)})"
    return None


def fetch_page(url: str, timeout: float) -> bytes:
    """GET `url` uncached; raises on any failure. Tests replace it."""
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                               "User-Agent": "claude-usage-tracker-health"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_failing_since(c: Config, now: float, ok: bool) -> float | None:
    """When fetches of the live page started failing, kept across runs in page_fetch_state:
    None after a good fetch (the file is removed), else the first failure's time."""
    if ok:
        c.page_fetch_state.unlink(missing_ok=True)
        return None
    try:
        since = json.loads(c.page_fetch_state.read_text()).get("failing_since")
        if isinstance(since, (int, float)) and since <= now:
            return float(since)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        c.page_fetch_state.parent.mkdir(parents=True, exist_ok=True)
        c.page_fetch_state.write_text(json.dumps({"failing_since": now}))
    except OSError:
        pass
    return now


def check_page(c: Config, now: float) -> str | None:
    """The public JSON is younger than page_max_age_s, by its own `generated_at`.

    On gs it reads the site checkout's JSON, which bin/daily.sh puts back to the last
    published one when it withholds; on masterrig the live URL, which also catches a push
    or a Pages deploy that failed after gs was done. Nothing read the page's age before
    2026-10-22, and it sat at 2026-10-13T10:31Z for nine days with every check green.
    A failed fetch is its own reason, after page_fetch_grace_s of failing in a row: one
    unreachable hour on a desktop's network is not the page going stale."""
    if c.page_json is not None:
        try:
            doc = json.loads(c.page_json.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return f"page: cannot read the published JSON {c.page_json}: {e}"
        where = f"the published JSON in the site checkout ({c.page_json.name})"
    elif c.page_url:
        try:
            body = fetch_page(c.page_url, 15.0)
        except Exception as e:  # noqa: BLE001 - any failure to fetch is the same reason
            since = _fetch_failing_since(c, now, ok=False)
            if now - since <= c.page_fetch_grace_s:
                return None
            return (f"page: cannot fetch {c.page_url} for {_age(now - since)} "
                    f"(grace {_age(c.page_fetch_grace_s)}): {type(e).__name__}: {e}")
        _fetch_failing_since(c, now, ok=True)
        try:
            doc = json.loads(body)
        except ValueError:
            return f"page: {c.page_url} does not parse as JSON"
        where = c.page_url
    else:
        return None
    gen = _parse_ts(doc.get("generated_at")) if isinstance(doc, dict) else None
    if gen is None:
        return f"page: {where} has no generated_at"
    if now - gen > c.page_max_age_s:
        return (f"page: {where} was generated {_age(now - gen)} ago (limit {_age(c.page_max_age_s)}): "
                f"the page is not updating")
    return None


def check_token(c: Config, now: float) -> str | None:
    try:
        oauth = json.loads(c.credentials.read_text()).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        return f"token: {c.credentials} unreadable"
    if not isinstance(oauth, dict):
        return f"token: claudeAiOauth in {c.credentials} has an unexpected shape"
    if not oauth.get("accessToken"):
        return "token: no claudeAiOauth.accessToken (signed out)"
    refresh = _parse_ts(oauth.get("refreshTokenExpiresAt"))
    if refresh is not None and refresh < now:
        return "token: refresh token expired, run `claude /login`"
    exp = _parse_ts(oauth.get("expiresAt"))
    if exp is None:
        return "token: no expiresAt"
    if now - exp > c.token_grace_s and refresh is None:
        # Only without a known-good refresh token: Claude Code refreshes the access token
        # when it next runs, so a weekend away is not a failure. What the token is for,
        # collection, has its own check.
        return f"token: access token expired {_age(now - exp)} ago and not refreshed"
    return None


def _tail_rows(path: Path, n: int = 60) -> list[dict]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - 65536)
        f.seek(start)
        lines = f.read().splitlines()
    if start:
        lines = lines[1:]  # the first line is cut part way
    rows = []
    for raw in lines[-n:]:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def check_lock(c: Config, now: float) -> str | None:
    try:
        text = c.lock_pidfile.read_text().strip()
        mtime = c.lock_pidfile.stat().st_mtime
    except OSError:
        return None
    if not text.isdigit() or not pid_alive(int(text)):
        return f"lock: stale lock {c.lock_pidfile.name} (pid {text or '?'} not running)"
    if now - mtime > c.lock_max_age_s:
        return f"lock: run pid {text} has held the lock {_age(now - mtime)}"
    return None


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def check_sizes(c: Config, now: float) -> str | None:
    for log in c.logs:
        try:
            size = log.stat().st_size
        except OSError:
            continue
        if size > c.max_log_bytes:
            return f"sizes: {log.name} is {size // 1024} KiB (limit {c.max_log_bytes // 1024} KiB)"
    if c.history_dir.is_dir():
        size = _dir_bytes(c.history_dir)
        if size > c.max_history_bytes:
            return f"sizes: {c.history_dir.name}/ is {size // (1024 * 1024)} MiB (limit {c.max_history_bytes // (1024 * 1024)} MiB)"
    return None


#: gs meter account -> its Claude config dir under HOME (deploy/systemd/claude-usage-meter-*).
GS_METER_CONFIG_DIRS = {"avis": ".claude-avis", "dave": ".claude-dave", "jwork": ".claude-javiswork"}
#: An account whose newest reads are all `auth_expired` while its refresh token is still
#: good is idle, not broken: meter_log never refreshes a token (only Claude Code does, when
#: someone uses that account), and the access token lapses ~8 h after the last refresh. It
#: fails `meters` only once the refresh token itself has expired, or after this long.
IDLE_TOKEN_GRACE_S = 24 * 60 * 60
#: Longest a logged Retry-After extends the meter age limit; matches meter_log's own cap.
MAX_HONOURED_BACKOFF_S = 2 * 60 * 60


def _meter_account(log: Path) -> str | None:
    name = log.name
    if name.startswith("claude-usage-meter-") and name.endswith(".log"):
        return name[len("claude-usage-meter-"):-len(".log")]
    return None


def _refresh_token_expiry(c: Config, account: str | None) -> float | None:
    """When the account's refresh token expires (epoch s), or None when unknown.

    Reads only `claudeAiOauth.refreshTokenExpiresAt`; no token value is ever read out."""
    dirs = c.meter_config_dirs if c.meter_config_dirs is not None else {
        a: Path(os.environ.get("HOME", str(Path.home()))) / d for a, d in GS_METER_CONFIG_DIRS.items()}
    if account not in dirs:
        return None
    try:
        oauth = json.loads((Path(dirs[account]) / ".credentials.json").read_text()).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        return None
    return _parse_ts(oauth.get("refreshTokenExpiresAt"))


def check_meters(c: Config, now: float) -> str | None:
    """Every failing meter log, joined with " | " so each account is its own incident."""
    failures = [r for r in (_check_meter_log(c, log, now) for log in c.meter_logs) if r]
    return " | ".join(failures) if failures else None


def _check_meter_log(c: Config, log: Path, now: float) -> str | None:
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().splitlines()
    except OSError as e:
        return f"meters: cannot read {log.name}: {e.strerror}"
    newest = None
    newest_row: dict = {}
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("five_hour"), dict):
            newest = _parse_ts(row.get("ts"))
            if newest is not None:
                newest_row = row
                if row.get("schema"):
                    return (f"meters: the usage API changed shape ({'; '.join(map(str, row['schema']))}) "
                            f"in {log.name}; readings may be wrong")
                break
    try:
        rows = _tail_rows(log)
    except OSError:
        rows = []
    limited = sum(1 for r in rows if r.get("reason") == "rate_limited")
    if len(rows) >= 40 and limited * 2 > len(rows):
        return f"meters: {limited} of the last {len(rows)} lines in {log.name} are 429s"
    # The lines after the newest usable reading: why the meter has not read since.
    trailing = []
    for r in reversed(rows):
        if isinstance(r.get("five_hour"), dict):
            break
        trailing.append(r)
    # An account the publisher uses whose meter has not read for meter_blind_s is blind,
    # whatever the reason: an idle token lapse or a held header probe is quiet on this host
    # only. dave's held probe read healthy for 5.4 h on 22 Oct while masterrig used dave for
    # 3,238 messages (six such holds of 3.7-5.4 h from 12 to 22 Oct), and a lapse with only
    # auth_expired lines after the last reading passed for 24 h (jwork's 41.7 h on 19-20 Oct
    # failed only because 429s were mixed in). Those reasons still say why; they no longer pass.
    blind = (newest is not None and now - newest > c.meter_blind_s
             and _meter_account(log) in _publisher_accounts(c, log))

    def blind_reason(why: str) -> str:
        return (f"meters: {_meter_account(log)} is blind, no reading in {log.name} for {_age(now - newest)} "
                f"(limit {_age(c.meter_blind_s)}; {why}): the publisher uses this account, "
                f"so what it spends meanwhile is not measured")
    if trailing and all(r.get("reason") == "auth_expired" for r in trailing):
        account = _meter_account(log)
        refresh_until = _refresh_token_expiry(c, account)
        if refresh_until is not None and refresh_until <= now:
            return (f"meters: {account}'s refresh token expired {_age(now - refresh_until)} ago "
                    f"({log.name}): that account needs a login")
        # The lapse is timed from the newest usable reading. With none in the scanned tail
        # (about 41 h of auth_expired lines) its start is unknown and older than the grace,
        # so it falls through to "no usable reading" below and fails.
        if refresh_until is not None and newest is not None:
            if now - newest <= IDLE_TOKEN_GRACE_S:
                # token lapsed (idle): it refreshes the next time the account is used here
                return blind_reason("token lapsed (idle), use that account once to refresh it") if blind else None
            return (f"meters: token lapsed (idle) for {_age(now - newest)} in "
                    f"{log.name}: use that account once to refresh it (limit {_age(IDLE_TOKEN_GRACE_S)})")
        # No readable refresh-token expiry: idle cannot be told from signed out, so the
        # plain age rule below applies.
    if trailing and all(r.get("reason") == "token_rejected" for r in trailing):
        return (f"meters: the setup token was refused (401) since {_age(now - newest) if newest else 'the start'} "
                f"in {log.name}: that seat needs a new token")
    if not trailing and newest is not None and _header_hold(newest_row, now):
        # header probe held (lapsed window or capped week): meter_log writes nothing then
        return blind_reason("header probe held, no session on this host since its window lapsed") if blind else None
    if newest is None:
        return f"meters: no usable reading near the end of {log.name}"
    if newest - now > CLOCK_SKEW_S:
        return f"meters: newest reading in {log.name} is stamped {_age(newest - now)} in the future (clock skew)"
    limit = c.meter_max_age_s
    # A Retry-After meter_log is honouring stops reads for that long: not a stopped meter.
    last = rows[-1] if rows else {}
    if last.get("reason") == "rate_limited":
        try:
            limit += min(max(float(last.get("retry_after_s") or 0), 0.0), MAX_HONOURED_BACKOFF_S)
        except (TypeError, ValueError):
            pass
    if now - newest > limit:
        return (f"meters: newest usable reading in {log.name} is {_age(now - newest)} old "
                f"(limit {_age(limit)})")
    if blind:
        return blind_reason("honouring a Retry-After")
    return None


def _publisher_accounts(c: Config, log: Path) -> set[str]:
    """The accounts the publisher uses, read from its stretch record (`passive_record`):
    every account it holds, by its label or by the meter log it reads. Derived from the
    data, so a new account counts once the publisher uses it and no list is kept by hand."""
    if c.passive_record is None:
        return set()
    try:
        accounts = json.loads(c.passive_record.read_text(encoding="utf-8")).get("accounts") or {}
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(accounts, dict):
        return set()
    out = set()
    for label, block in accounts.items():
        out.add(label)
        meter_log = ((block or {}).get("meter") or {}).get("log") if isinstance(block, dict) else None
        if isinstance(meter_log, str) and Path(meter_log).name == log.name:
            out.add(_meter_account(log))
    return out


def check_advisory(c: Config, now: float) -> str | None:
    """No advisory step of bin/daily.sh has failed on every run for advisory_grace_s.

    The steps (tracker.list_prices, tracker.contributed, tracker.gs_passive,
    tools.model_rates) never stop the publish, which carries on with their previous output,
    and each failure was one warning line in the run log: the model_rates refit crashed on
    every run from 2026-10-13T19:30Z, 48 times a day, and nothing alerted. bin/daily.sh
    records each outcome (tracker/advisory.py). Each step failing past the grace is named,
    joined with " | ", so each is its own incident. A step not run in ADVISORY_SILENT_S is
    left out: it is no longer run (the refit runs only when due), not failing."""
    if c.advisory_state is None:
        return None
    steps = read_state(c.advisory_state).get("steps") or {}
    failures = []
    for step, rec in sorted(steps.items()) if isinstance(steps, dict) else []:
        if not isinstance(rec, dict):
            continue
        since, last_run = rec.get("failing_since"), rec.get("last_run")
        if not isinstance(since, (int, float)) or not isinstance(last_run, (int, float)):
            continue
        if now - last_run > ADVISORY_SILENT_S or now - since <= c.advisory_grace_s:
            continue
        failures.append(f"advisory: {step} has failed on every run for {_age(now - since)} "
                        f"(limit {_age(c.advisory_grace_s)}, last exit {rec.get('last_exit')}); "
                        f"the publish keeps using its previous output")
    return " | ".join(failures) if failures else None


#: How long since an advisory step last ran before its record is no longer read.
ADVISORY_SILENT_S = 26 * 60 * 60


def _header_hold(row: dict, now: float) -> bool:
    """A header-probe reading meter_log is deliberately not following up (see meter_log.header_hold)."""
    if row.get("source") != "ratelimit_headers":
        return False
    sd = row.get("seven_day") or {}
    sd_reset = _parse_ts(sd.get("resets_at"))
    try:
        if sd.get("utilization") is not None and float(sd["utilization"]) >= 100 and sd_reset and sd_reset > now:
            return True
    except (TypeError, ValueError):
        pass
    fh_reset = _parse_ts((row.get("five_hour") or {}).get("resets_at"))
    return fh_reset is not None and fh_reset <= now


check_meter = check_meters  # the name the red-team tests and older notes use


CHECKS: dict[str, Callable[[Config, float], str | None]] = {
    "collection": check_collection,
    "run": check_run,
    "publisher": check_publisher,
    # After run and publisher: a withheld or failed publish is named by those first, and
    # `page` is the reason only when the page goes stale with every run succeeding.
    "page": check_page,
    "token": check_token,
    "lock": check_lock,
    "sizes": check_sizes,
    "meters": check_meters,
    "advisory": check_advisory,
}
#: Checks a bare Config leaves out: gs runs both, masterrig_config runs meters.
GS_ONLY = ("meters", "advisory")


def run_checks(c: Config, now: float | None = None, only: list[str] | None = None) -> list[tuple[str, str | None]]:
    now = time.time() if now is None else now
    chosen = c.checks if c.checks is not None else tuple(n for n in CHECKS if n not in GS_ONLY)
    return [(name, _safe(name, fn, c, now)) for name, fn in CHECKS.items()
            if name in chosen and (not only or name in only)]


def _safe(name: str, fn: Callable[[Config, float], str | None], c: Config, now: float) -> str | None:
    """A check that raises is a failure of that check, never a crash of the caller."""
    try:
        return fn(c, now)
    except Exception as e:  # noqa: BLE001 - the supervisor must reach its alert path
        return f"{name}: check crashed: {type(e).__name__}: {e}"


def first_failure(c: Config, now: float | None = None, skip: tuple[str, ...] = ()) -> str | None:
    for name, reason in run_checks(c, now):
        if reason and name not in skip:
            return reason
    return None


def all_failures(c: Config, now: float | None = None, skip: tuple[str, ...] = ()) -> str | None:
    """Every failing check's reason, joined with " | ", or None when all pass.

    The supervisor reads this, not first_failure: tracker.supervise.health_kinds splits it
    on " | " and keeps an incident per kind, so a later check is never hidden behind an
    earlier one. With only the first failure, gs's `advisory` (last in its order) never
    alerted while the page was withheld, and a blind meter appearing and clearing ahead of
    it sent a false "advisory recovered" and then a "further problem" for the same step."""
    bad = [reason for name, reason in run_checks(c, now) if reason and name not in skip]
    return " | ".join(bad) if bad else None


def build_parser() -> argparse.ArgumentParser:
    d = Config()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--profile", choices=("masterrig", "gs"), default="masterrig",
                   help="gs: run, meters, lock, sizes against ~/.paperclip/ops (see module doc)")
    p.add_argument("--all", action="store_true", help="print every check, not just the first failure")
    p.add_argument("--skip", action="append", default=[], choices=list(CHECKS), help="leave a check out")
    p.add_argument("--usage-log", type=Path, default=d.usage_log)
    p.add_argument("--collection-interval", type=int, default=d.collection_interval_s)
    p.add_argument("--state", type=Path, default=d.state)
    p.add_argument("--run-interval", type=int, default=d.run_interval_s)
    p.add_argument("--repo", type=Path, default=d.repo)
    p.add_argument("--publisher-max-age", type=int, default=d.publisher_max_age_s)
    p.add_argument("--credentials", type=Path, default=d.credentials)
    p.add_argument("--token-grace", type=int, default=d.token_grace_s)
    p.add_argument("--pidfile", type=Path, default=d.lock_pidfile)
    p.add_argument("--log", type=Path, action="append", default=[], help="a log whose size is bounded (repeatable)")
    p.add_argument("--max-log-bytes", type=int, default=d.max_log_bytes)
    p.add_argument("--max-history-bytes", type=int, default=d.max_history_bytes)
    p.add_argument("--meter-log", type=Path, action="append", default=[],
                   help="a per-account meter log (tracker.meter_log) to check for freshness (repeatable); "
                        "masterrig default: ~/.paperclip/ops/" + MASTERRIG_METER_LOG)
    p.add_argument("--meter-max-age", type=int, default=d.meter_max_age_s)
    p.add_argument("--page-url", default=LIVE_PAGE_URL,
                   help="the live JSON whose age `page` checks on masterrig ('' to leave it out)")
    return p


def config_from_args(a: argparse.Namespace) -> Config:
    if a.profile == "gs":
        d = Config()
        return gs_config(state=GS_STATE if a.state == d.state else a.state,
                         pidfile=GS_LOCK.with_suffix(".pid") if a.pidfile == d.lock_pidfile else a.pidfile,
                         logs=tuple(a.log) or (GS_LOG,), repo=a.repo)
    return masterrig_config(usage_log=a.usage_log, collection_interval_s=a.collection_interval, state=a.state,
                            run_interval_s=a.run_interval, repo=a.repo, history_dir=a.repo / "history",
                            publisher_max_age_s=a.publisher_max_age, credentials=a.credentials,
                            token_grace_s=a.token_grace, lock_pidfile=a.pidfile, logs=tuple(a.log),
                            max_log_bytes=a.max_log_bytes, max_history_bytes=a.max_history_bytes,
                            meter_logs=tuple(a.meter_log), meter_max_age_s=a.meter_max_age,
                            page_url=a.page_url or None)


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    c = config_from_args(a)
    results = [(n, r) for n, r in run_checks(c) if n not in a.skip]
    if a.all:
        for name, reason in results:
            print(f"{name}: ok" if reason is None else reason)
    bad = [r for _, r in results if r]
    if not a.all:
        print(bad[0] if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
