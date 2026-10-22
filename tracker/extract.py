"""Text-free per-turn extracts, so each account's work is counted on every host it ran on.

Why (docs: the 2026-10-22 capture diagnosis). gs joins each metered account against the
transcripts gs holds, but jwork, dave and avis were also run on masterrig from 12, 19 and
13 Oct. Their meters moved with work gs never saw: 24,554 jwork turns (3.39B tokens),
13,449 dave turns (2.93B) and 8,063 avis turns (1.35B) by 22 Oct, which read as 30 jwork
and 32 dave "unaccounted" stretches and, through them, as a -46.8% "change" on 17 Oct.
Dave's gs-only $/pct on 19-22 Oct read 1.21, 0.70, 0.21 and 0.42; with the masterrig turns
added, 1.47-1.49. Nothing here is a list of accounts: every `~/.claude*` dir is found on
disk, and which account it spends on is read from the dir itself (`login`).

Three jobs live here, one per host role:

- `extract` (masterrig, hourly via bin/ship-extracts.sh): every non-local account's turns,
  stripped of all text, written per account identity under one directory that the shell
  wrapper rsyncs to gs. "Non-local" is every identity except the one the host's own join
  already reads (`~/.claude`'s, a1 on masterrig: tracker/gs_passive.py `masterrig_account`).
- `build_archive_cache` (gs, inside tracker.gs_passive): the 2026-10-15 disk cleanup packed
  every gs transcript older than 14 days into `~/archive/transcripts-20261015/<subtree>/
  *.tar.zst` and deleted the originals, and gs_passive, which rebuilds all of history from
  what is on disk, lost every September stretch's tokens (jwork 91,131 -> 18,209 turns at
  the 14:00Z run). The archive is 10.2 GB raw on a disk the cleanup ran to relieve, so it is
  read in place: each archive is streamed once into the same text-free shape and cached.
- `owner_at` and friends: which login a turn of a session several logins reach belongs to.

What an extract line keeps. `user` and `assistant` lines only, and of those only `type`,
`timestamp`, `isSidechain`, `entrypoint`, `sessionId`, `remoteSourced`, `agentId` and, for an
assistant line, `message.{id, model, usage}`. No message content, no tool input or output,
no prompt. That is exactly what tracker/turns.py `turns_in` and tracker/speed.py
`requests_in` read, so a stretch built from extracts is built from the same numbers as one
built from the transcripts (round-trip tested). The directory layout is the transcripts'
own (`<project>/<session>.jsonl`, `<session>/subagents/...`), so session ids and sub-agent
flags come off the path as they always have. Project directory names and session ids do
leave the host; text does not.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .atomic import write_text_atomic
from .meter_log import account_identity
from .turns import parse_ts_or_none, transcript_paths, transcript_session_id

#: The keys an extract line keeps from a transcript line (see the module docstring).
KEEP_KEYS = ("type", "timestamp", "isSidechain", "entrypoint", "sessionId", "remoteSourced", "agentId")
KEEP_MESSAGE_KEYS = ("id", "model", "usage")
#: A setup token kept beside a config dir for a launcher to inject as CLAUDE_CODE_OAUTH_TOKEN.
#: ~/bin/dave-agent on masterrig is the one user (2026-10-19): ~/.claude-dave holds Dave's
#: token there and no `oauthAccount`, so the dir alone names no account.
TOKEN_FILE = ".oauth-token"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
#: The tracker's own meter units, which bind a setup token to the config dir whose account
#: it reads (deploy/systemd/claude-usage-meter-dave.service: `--config-dir ~/.claude-dave
#: --token-env GS_DAVE_CLAUDE_OAUTH_TOKEN`), as installed under the user's systemd dir.
METER_UNITS = Path(".config") / "systemd" / "user"
METER_UNIT_GLOB = "claude-usage-meter-*.service"
DEFAULT_TOKEN_ENV_FILE = Path(".config") / "greenscape" / "env"
#: Where masterrig's extracts land on gs, one directory per host; tracker/gs_passive.py reads
#: every host under it.
REMOTE_ROOT = Path(".paperclip") / "ops" / "remote-transcripts"
ARCHIVE_ROOT = Path("archive")
ARCHIVE_GLOB = "transcripts-*"
ARCHIVE_CACHE = Path(".cache") / "claude-usage-tracker" / "transcript-archive"
ARCHIVE_SUFFIXES = (".tar.zst", ".tar.gz", ".tgz", ".tar")
MANIFEST = "manifest.json"
STATE = ".extract-state.json"
UNCLAIMED = "unclaimed"


# ---------------------------------------------------------------- which account a dir spends on

def token_fingerprint(token: str) -> str:
    """`tok-` and 12 hex characters of the token's SHA-256: the same value on every host that
    holds the same token, and no way back to the token. The token is never logged, printed or
    stored; only this is."""
    return "tok-" + hashlib.sha256(token.strip().encode()).hexdigest()[:12]


def _file_token(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token_fingerprint(token) if token else None


def _settings_token(config_dir: Path) -> str | None:
    """A token Claude Code itself injects into every session of this dir: `env` in its
    settings. None on masterrig and gs today; checked because it would override the login."""
    for name in ("settings.local.json", "settings.json"):
        try:
            doc = json.loads((config_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        env = doc.get("env") if isinstance(doc, dict) else None
        token = env.get(TOKEN_ENV) if isinstance(env, dict) else None
        if isinstance(token, str) and token.strip():
            return token_fingerprint(token)
    return None


@dataclass(frozen=True)
class Login:
    """A config dir and the account its sessions spend on, as far as the dir can say.

    `identity` is tracker.meter_log's `account_identity` (a hash of `oauthAccount.
    accountUuid`, what every gs meter log is labelled with), or for a dir whose sessions run
    on a setup token, that token's `token_fingerprint`. `source` says which, and
    `token_override_possible` marks a dir that has a login *and* a token file beside it: a
    launcher may run some of its sessions on the token, and nothing in a transcript says which.
    """
    config_dir: Path
    identity: str | None
    source: str
    token_override_possible: bool = False

    @property
    def group(self) -> str:
        """The key claims are pooled under: the account, or the dir alone when it names none
        (two unidentified dirs are never assumed to be one account)."""
        return self.identity or f"dir:{self.config_dir.name}"


def login(config_dir: Path, home: Path | None = None) -> Login:
    """Which account `config_dir`'s sessions spend on (`Login`).

    Order: a token in the dir's settings `env` (every session gets it); the dir's own
    `oauthAccount`; for the default `~/.claude`, whose login Claude Code keeps in
    `~/.claude.json` when CLAUDE_CONFIG_DIR is unset (gs's `~/.claude` has no `.claude.json`
    of its own, and reads as a1 that way, 3e05a16188da); then a `.oauth-token` file. A
    CLAUDE_CODE_OAUTH_TOKEN set by whoever launches a session cannot be seen from the dir
    at all: Mission Control seats have run on account 3 from masterrig's `~/.claude` that way
    (~/.claude-account2-rollback/README.md). Such work stays counted as the dir's login.
    """
    config_dir = Path(config_dir)
    if (fp := _settings_token(config_dir)) is not None:
        return Login(config_dir, fp, "settings-env")
    ident, source = account_identity(config_dir), "oauthAccount"
    if ident is None and home is not None and config_dir == Path(home) / ".claude":
        ident, source = account_identity(Path(home)), "default-login"
    token = _file_token(config_dir / TOKEN_FILE)
    if ident is not None:
        return Login(config_dir, ident, source, token_override_possible=token is not None)
    if token is not None:
        return Login(config_dir, token, "token-file")
    return Login(config_dir, None, "none")


def logins(home: Path) -> list[Login]:
    """Every `~/.claude*` config dir with a `projects/` tree, and its `login`. Found on disk."""
    home = Path(home)
    return [login(d, home) for d in sorted(home.glob(".claude*")) if d.is_dir() and (d / "projects").exists()]


def _unit_args(text: str) -> dict[str, str]:
    """`--flag value` pairs of a unit's ExecStart line."""
    for line in text.splitlines():
        if line.strip().startswith("ExecStart="):
            words = line.split("=", 1)[1].split()
            return {w: v for w, v in zip(words, words[1:]) if w.startswith("--")}
    return {}


def _unit_path(value: str, home: Path) -> Path:
    """A unit argument's path: systemd's `%h` and a leading `~/` are the home directory."""
    value = value.replace("%h", str(home))
    return Path(home) / value[2:] if value.startswith("~/") else Path(value)


def token_bindings(home: Path) -> dict[str, str]:
    """Setup-token fingerprint -> the account identity it reads, from the tracker's own meter units.

    The same token is the account: gs's dave meter is read with `--token-env
    GS_DAVE_CLAUDE_OAUTH_TOKEN` against `--config-dir ~/.claude-dave`, whose `oauthAccount`
    is Dave's (b64d8ea4794c). masterrig's ~/.claude-dave holds a copy of that token and no
    login, so its extracts arrive as `tok-<fp>` and this is what joins them to dave. The token
    is read in-process (tracker/header_usage.py `load_token`) and only its fingerprint kept.
    """
    from .header_usage import load_token
    home = Path(home)
    out: dict[str, str] = {}
    for unit in sorted((home / METER_UNITS).glob(METER_UNIT_GLOB)):
        try:
            args = _unit_args(unit.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if "--token-env" not in args or "--config-dir" not in args:
            continue
        config_dir = _unit_path(args["--config-dir"], home)
        ident = login(config_dir, home).identity
        env_file = _unit_path(args.get("--env-file", str(home / DEFAULT_TOKEN_ENV_FILE)), home)
        try:
            token = load_token(args["--token-env"], env_file)
        except LookupError:
            continue
        if ident is not None and not ident.startswith("tok-"):
            out[token_fingerprint(token)] = ident
    return out


# ---------------------------------------------------------------- which login a turn belongs to

def _births(paths: list[Path]) -> dict[Path, float]:
    """Each path's birth time (statx, through GNU `stat -c %W`), or its mtime where the
    filesystem keeps none. Python has no birth time on Linux before 3.15's os.statx."""
    out: dict[Path, float] = {}
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        try:
            res = subprocess.run(["stat", "-c", "%W %Y", "--", *map(str, chunk)], capture_output=True,
                                 text=True, timeout=60)
            rows = res.stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            rows = []
        if len(rows) != len(chunk):
            continue  # a path vanished mid-call: the per-path fallback below covers the chunk
        for p, row in zip(chunk, rows):
            birth, _, mtime = row.partition(" ")
            try:
                out[p] = float(birth) if birth.isdigit() and int(birth) > 0 else float(mtime)
            except ValueError:
                pass
    for p in paths:
        if p not in out:
            try:
                out[p] = p.stat().st_mtime
            except OSError:
                pass
    return out


def _ms(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def session_claims(config_dir: Path, sids: set[str]) -> dict[str, list[datetime]]:
    """When `config_dir` claimed each of `sids`: every record Claude Code keeps per login.

    - `session-env/<sid>/` and `file-history/<sid>/`: their birth time, when the session
      first opened a shell or edited a file under this login.
    - `sessions/<pid>.json`: `startedAt`, an interactive process starting (or resuming) it.
    - `history.jsonl`: the `timestamp` of every prompt typed into it. Only `sessionId` and
      `timestamp` are read; the prompt text is not kept.

    Session 27b80904 on masterrig is reached by both ~/.claude-avis and ~/.claude (a1) and
    each has a session-env entry; their births (avis 2026-10-18 11:30:16Z, a1 2026-10-20
    11:21:53Z) and avis's sessions/16759.json startedAt 11:29:20Z leave none of its 2,736
    turns in a1's eight minutes, so all 590M tokens are avis's (`owner_at`).
    """
    config_dir = Path(config_dir)
    out: dict[str, list[datetime]] = {}
    if not sids:
        return out
    dirs = [config_dir / sub / sid for sub in ("session-env", "file-history") for sid in sorted(sids)]
    dirs = [p for p in dirs if p.is_dir()]
    for p, t in _births(dirs).items():
        out.setdefault(p.name, []).append(datetime.fromtimestamp(t, tz=timezone.utc))
    sessions = config_dir / "sessions"
    if sessions.is_dir():
        for p in sessions.glob("*.json"):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("sessionId") in sids and (t := _ms(doc.get("startedAt"))):
                out.setdefault(doc["sessionId"], []).append(t)
    history = config_dir / "history.jsonl"
    if history.exists():
        with open(history, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"sessionId"' not in line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(doc, dict) and doc.get("sessionId") in sids and (t := _ms(doc.get("timestamp"))):
                    out.setdefault(doc["sessionId"], []).append(t)
    return {sid: sorted(ts) for sid, ts in out.items()}


#: (claim time, group) per contested session, in time order.
Segments = list[tuple[datetime, str]]


def claim_segments(sid: str, claims: dict[str, dict[str, list[datetime]]]) -> Segments:
    """Every group's claims on `sid`, merged in time order. `claims` is group -> sid -> times."""
    return sorted((t, group) for group, by_sid in claims.items() for t in by_sid.get(sid, ()))


def owner_at(segments: Segments, ts: datetime) -> str | None:
    """The group whose latest claim is at or before `ts`; before every claim, the first claimant.

    A session's first turns usually precede its first claim (session-env is born with the
    first shell, a few minutes in), so they go to whoever claimed it first. None: no claims.
    """
    if not segments:
        return None
    owner = segments[0][1]
    for t, group in segments:
        if t > ts:
            break
        owner = group
    return owner


@dataclass
class ClaimBook:
    """Claims of many config dirs, read once per dir for the sessions asked about."""
    dirs: dict[str, list[Path]] = field(default_factory=dict)  # group -> its config dirs
    _cache: dict[tuple[str, str], list[datetime]] = field(default_factory=dict)

    def load(self, sids: set[str]) -> None:
        todo = {sid for sid in sids if not any((g, sid) in self._cache for g in self.dirs)}
        if not todo:
            return
        for group, dirs in self.dirs.items():
            merged: dict[str, list[datetime]] = {}
            for d in dirs:
                for sid, ts in session_claims(d, todo).items():
                    merged.setdefault(sid, []).extend(ts)
            for sid in todo:
                self._cache[(group, sid)] = sorted(merged.get(sid, []))

    def segments(self, sid: str) -> Segments:
        return sorted((t, g) for g in self.dirs for t in self._cache.get((g, sid), ()))


# ---------------------------------------------------------------- the text-free line shape

def slim_line(d: dict) -> dict | None:
    """The extract shape of one parsed transcript line, or None for a line it drops."""
    kind = d.get("type")
    if kind not in ("user", "assistant"):
        return None
    out = {k: d[k] for k in KEEP_KEYS if k in d}
    if kind == "assistant":
        m = d.get("message")
        if isinstance(m, dict):
            out["message"] = {k: m[k] for k in KEEP_MESSAGE_KEYS if k in m}
    return out


def _parsed(lines: Iterable[bytes | str]) -> Iterator[dict]:
    for line in lines:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(d, dict):
            yield d


def _write_lines(path: Path, lines: list[dict], mtime: float | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for d in lines:
            fh.write(json.dumps(d, separators=(",", ":")) + "\n")
    if mtime is not None:
        os.utime(tmp, (mtime, mtime))
    os.replace(tmp, path)


# ---------------------------------------------------------------- masterrig: the extractor

def reach(all_logins: list[Login]) -> dict[str, list[tuple[Login, Path]]]:
    """Real path of every transcript -> each (login, path relative to its projects/) reaching it.

    A file reached from two config dirs is a symlinked subtree; which of them it belongs to
    is a question for the claims, not for the walk.
    """
    out: dict[str, list[tuple[Login, Path]]] = {}
    for lg in all_logins:
        root = lg.config_dir / "projects"
        for p in transcript_paths(root, None):
            out.setdefault(os.path.realpath(p), []).append((lg, p.relative_to(root)))
    return out


def _identity_of_group(group: str) -> str | None:
    return None if group.startswith("dir:") or group == UNCLAIMED else group


def split_file(lines: list[dict], groups: set[str], book: ClaimBook, sid: str) -> dict[str, list[dict]]:
    """One transcript's slim lines per owning group.

    A file one group reaches is that group's whole. A file several reach is split turn by
    turn by `owner_at`; with no claim at all its assistant lines go to UNCLAIMED. User lines
    carry no tokens and go to every group that owns any turn of the file, so tracker/speed.py
    still sees each request's trigger.
    """
    slim = [s for s in map(slim_line, lines) if s is not None]
    if len(groups) == 1:
        return {next(iter(groups)): slim}
    segments = [(t, g) for t, g in book.segments(sid) if g in groups]

    def owner(s: dict) -> str:
        ts = parse_ts_or_none(s.get("timestamp"))
        return (owner_at(segments, ts) if ts is not None else None) or UNCLAIMED

    owners = [owner(s) if s.get("type") == "assistant" else None for s in slim]
    out: dict[str, list[dict]] = {g: [] for g in owners if g is not None}
    for s, g in zip(slim, owners):
        for target in ([g] if g is not None else out):
            out[target].append(s)
    return out


def _turns(lines: list[dict]) -> int:
    return len({(s.get("message") or {}).get("id") for s in lines
                if s.get("type") == "assistant" and (s.get("message") or {}).get("id")})


def extract(home: Path, out: Path, host: str | None = None, now: datetime | None = None) -> dict:
    """Write every non-local account's text-free extracts under `out/<identity>/`; return the manifest.

    `out/<identity>/<project>/<session>.jsonl` mirrors the projects tree. An unchanged
    source (same size, mtime and claims) is not re-read: `out/.extract-state.json` keys
    each one by real path. A file that no longer has any turn for an identity it used to
    is rewritten empty there rather than deleted, so an rsync without `--delete` still
    carries the change. Nothing is ever removed because its source was: gs keeps history.
    """
    home, out = Path(home), Path(out)
    now = now or datetime.now(timezone.utc)
    all_logins = logins(home)
    local = login(home / ".claude", home).identity
    by_group: dict[str, list[Login]] = {}
    for lg in all_logins:
        by_group.setdefault(lg.group, []).append(lg)
    book = ClaimBook({g: [lg.config_dir for lg in lgs] for g, lgs in by_group.items()})
    reached = reach(all_logins)
    try:
        state = json.loads((out / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    contested = {transcript_session_id(Path(rel)) for real, hits in reached.items()
                 if len({lg.group for lg, _ in hits}) > 1 for _, rel in hits[:1]}
    book.load(contested)
    new_state: dict[str, dict] = {}
    for real, hits in sorted(reached.items()):
        groups = {lg.group for lg, _ in hits}
        if local is not None and groups == {local}:
            continue  # the host's own join reads these; nothing to ship
        rel = hits[0][1]
        sid = transcript_session_id(rel)
        try:
            st = os.stat(real)
        except OSError:
            continue
        sig = json.dumps([sorted(groups), [(t.isoformat(), g) for t, g in book.segments(sid)]
                          if len(groups) > 1 else []])
        prev = state.get(real) if isinstance(state.get(real), dict) else None
        if prev and prev.get("size") == st.st_size and prev.get("mtime_ns") == st.st_mtime_ns \
                and prev.get("sig") == sig:
            new_state[real] = prev
            continue
        try:
            with open(real, encoding="utf-8", errors="replace") as fh:
                lines = list(_parsed(fh))
        except OSError:
            continue
        parts = split_file(lines, groups, book, sid)
        written: dict[str, int] = {}
        for group, slim in parts.items():
            ident = _identity_of_group(group)
            if ident is None or ident == local:
                written[group] = _turns(slim)
                continue
            _write_lines(out / ident / rel, slim, st.st_mtime)
            written[group] = _turns(slim)
        for group in (prev or {}).get("turns", {}):
            ident = _identity_of_group(group)
            if group not in written and ident is not None and ident != local:
                _write_lines(out / ident / rel, [], st.st_mtime)
                written[group] = 0
        new_state[real] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sig": sig, "rel": str(rel),
                           "turns": written}
    write_text_atomic(out / STATE, json.dumps(new_state, sort_keys=True) + "\n")
    manifest = _manifest(new_state, all_logins, local, host or socket.gethostname(), now)
    write_text_atomic(out / MANIFEST, json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def _manifest(state: dict[str, dict], all_logins: list[Login], local: str | None, host: str,
              now: datetime) -> dict:
    """What was extracted, per identity: generated from the state, never edited by hand."""
    ids: dict[str, dict] = {}
    for lg in all_logins:
        if lg.identity is not None and lg.identity != local:
            e = ids.setdefault(lg.identity, {"dirs": [], "files": 0, "turns": 0, "source": lg.source,
                                             "token_override_possible": False})
            e["dirs"].append(lg.config_dir.name)
            e["token_override_possible"] |= lg.token_override_possible
    unclaimed = {"files": 0, "turns": 0}
    for rec in state.values():
        for group, n in rec.get("turns", {}).items():
            if group == UNCLAIMED:
                unclaimed["files"] += 1
                unclaimed["turns"] += n
            elif group in ids and n:
                ids[group]["files"] += 1
                ids[group]["turns"] += n
    return {"host": host, "generated_at": now.isoformat(timespec="seconds"), "local_identity": local,
            "identities": ids, "unclaimed_in_shared_subtrees": unclaimed,
            "unidentified_dirs": sorted(lg.config_dir.name for lg in all_logins if lg.identity is None)}


# ---------------------------------------------------------------- gs: the transcript archive, in place

@dataclass(frozen=True)
class ArchiveRoot:
    """One archive subtree's cached extracts, and the projects dir its transcripts came from."""
    label: str
    source_projects: Path
    cache_root: Path
    archive_dir: Path


def _archive_name(p: Path) -> str | None:
    for suffix in ARCHIVE_SUFFIXES:
        if p.name.endswith(suffix):
            return p.name[: -len(suffix)]
    return None


def _open_stream(path: Path) -> tuple[tarfile.TarFile, subprocess.Popen | None]:
    """A streaming reader over one archive. zstd through the `zstd` binary (gs has it), or
    Python's own compression.zstd (3.14+) where there is none; tarfile reads the rest."""
    if path.name.endswith(".tar.zst"):
        if shutil.which("zstd"):
            proc = subprocess.Popen(["zstd", "-dcq", "--", str(path)], stdout=subprocess.PIPE)
            return tarfile.open(fileobj=proc.stdout, mode="r|"), proc
        return tarfile.open(path, mode="r|zst"), None
    return tarfile.open(path, mode="r|*"), None


def _safe_member(name: str) -> Path | None:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not name.endswith(".jsonl"):
        return None
    return Path(*p.parts)


def _cache_one(archive: Path, staging: Path) -> tuple[int, int]:
    """Stream one archive's `*.jsonl` members into slim files under `staging`: (files, lines).

    Each extract opens with one `{"type": "extract", "timestamp", "cwd"}` line holding the
    transcript's first timestamp and working directory, which tracker/unclaimed.py
    `first_record` reads to attribute a headless run no login claims. Archived files stay
    on gs, so the working directory never leaves the host.
    """
    tf, proc = _open_stream(archive)
    files = lines = 0
    try:
        for m in tf:
            rel = _safe_member(m.name) if m.isfile() else None
            if rel is None:
                continue
            fh = tf.extractfile(m)
            if fh is None:
                continue
            first_ts = first_cwd = None
            slim: list[dict] = []
            for d in _parsed(fh):
                if first_ts is None and d.get("timestamp"):
                    first_ts = d["timestamp"]
                first_cwd = first_cwd or d.get("cwd")
                s = slim_line(d)
                if s is not None:
                    slim.append(s)
            head = {"type": "extract", "timestamp": first_ts, "cwd": first_cwd}
            _write_lines(staging / rel, [head, *slim], float(m.mtime))
            files += 1
            lines += len(slim)
    finally:
        tf.close()
        if proc is not None:
            # tarfile stops at the end-of-archive block; the zero padding after it is still in the
            # pipe, and closing on it kills zstd with SIGPIPE (12 of 2,412 archives on gs did).
            while proc.stdout.read(1 << 20):
                pass
            proc.stdout.close()
            if proc.wait() != 0:
                raise OSError(f"zstd exited {proc.returncode} on {archive}")
    return files, lines


def archive_roots(home: Path, archive_root: Path | None = None, cache_dir: Path | None = None) -> list[ArchiveRoot]:
    """Every archive subtree on disk and where its cache lives. Built by `build_archive_cache`.

    `<archive_root>/transcripts-*/<subtree>/*.tar.zst`, where `<subtree>` names the config dir
    whose `projects/` the archive came from (`claude` -> `~/.claude/projects`, `claude-dave`
    -> `~/.claude-dave/projects`), as the 2026-10-15 cleanup's RUN.log names them.
    """
    home = Path(home)
    archive_root = Path(archive_root) if archive_root is not None else home / ARCHIVE_ROOT
    cache_dir = Path(cache_dir) if cache_dir is not None else home / ARCHIVE_CACHE
    out = []
    for adir in sorted(archive_root.glob(ARCHIVE_GLOB)) if archive_root.is_dir() else []:
        for sub in sorted(p for p in adir.iterdir() if p.is_dir()):
            if any(_archive_name(p) for p in sub.iterdir()):
                out.append(ArchiveRoot(f"archive:{adir.name}/{sub.name}", home / f".{sub.name}" / "projects",
                                       cache_dir / adir.name / sub.name, sub))
    return out


def build_archive_cache(home: Path, archive_root: Path | None = None, cache_dir: Path | None = None,
                        log=None) -> list[ArchiveRoot]:
    """Bring every archive's cache up to date, and return the roots (`archive_roots`).

    An archive is read once: its stamp (`.stamps/<subtree>/<archive>.json`, size and mtime)
    says the cache holds it, and only a new or changed archive is streamed again. Members
    go to a staging directory and are moved into place before the stamp is written, so an
    interrupted run leaves no stamp and the next one redoes that archive. A failed archive
    is reported on `log` and skipped; tracker/gs_passive.py's turn-count guard is what
    notices its turns missing.
    """
    log = log or sys.stderr
    roots = archive_roots(home, archive_root, cache_dir)
    for root in roots:
        stamps = root.cache_root.parent / ".stamps" / root.cache_root.name
        for archive in sorted(root.archive_dir.iterdir()):
            name = _archive_name(archive)
            if name is None:
                continue
            st = archive.stat()
            want = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
            stamp = stamps / f"{archive.name}.json"
            try:
                have = json.loads(stamp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                have = None
            if isinstance(have, dict) and {k: have.get(k) for k in want} == want:
                continue
            staging = root.cache_root.parent / f".staging-{os.getpid()}" / name
            try:
                files, lines = _cache_one(archive, staging)
                for src in sorted(staging.rglob("*.jsonl")):
                    dst = root.cache_root / src.relative_to(staging)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dst)
            except (OSError, tarfile.TarError, EOFError, ValueError) as e:
                print(f"warning: transcript archive {archive} not read ({type(e).__name__}: {e}); "
                      "its turns are missing from this run", file=log)
                continue
            finally:
                shutil.rmtree(staging.parent, ignore_errors=True)
            write_text_atomic(stamp, json.dumps({**want, "files": files, "lines": lines}) + "\n")
    return roots


# ---------------------------------------------------------------- gs: what masterrig shipped

def remote_roots(remote_root: Path) -> list[tuple[str, str, Path]]:
    """(host, identity, root) for every shipped extract directory under `remote_root`."""
    remote_root = Path(remote_root)
    out = []
    for host in sorted(p for p in remote_root.iterdir() if p.is_dir()) if remote_root.is_dir() else []:
        for ident in sorted(p for p in host.iterdir() if p.is_dir() and not p.name.startswith(".")):
            out.append((host.name, ident.name, ident))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Text-free per-turn extracts of every non-local account (masterrig), "
                                             "or the transcript archive's cache (gs, --archive)")
    ap.add_argument("--home", type=Path, default=Path.home())
    ap.add_argument("--out", type=Path, help="extract directory, shipped to gs by bin/ship-extracts.sh")
    ap.add_argument("--host", help="host name recorded in the manifest (default: this host's)")
    ap.add_argument("--archive", action="store_true", help="bring the transcript archive's cache up to date and exit")
    ap.add_argument("--archive-root", type=Path, help=f"default ~/{ARCHIVE_ROOT}")
    ap.add_argument("--archive-cache", type=Path, help=f"default ~/{ARCHIVE_CACHE}")
    a = ap.parse_args(argv)
    if a.archive:
        for root in build_archive_cache(a.home, a.archive_root, a.archive_cache):
            n = len(transcript_paths(root.cache_root, None)) if root.cache_root.exists() else 0
            print(f"{root.label}: {n} cached transcripts for {root.source_projects}")
        return 0
    if a.out is None:
        ap.error("--out is required unless --archive")
    m = extract(a.home, a.out, a.host)
    for ident, e in sorted(m["identities"].items()):
        print(f"{ident} ({', '.join(e['dirs'])}, {e['source']}): {e['files']} files, {e['turns']} turns")
    print(f"local {m['local_identity']} not extracted; unclaimed in shared subtrees: "
          f"{m['unclaimed_in_shared_subtrees']}; unidentified dirs: {m['unidentified_dirs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
