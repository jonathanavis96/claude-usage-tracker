"""Passive measurement per gs account: each account's own transcripts against its own meter.

Why per account, and why gs (issue #39). The passive join on masterrig reads
Jonathan's own account, whose meter also counts the web, the phone and other
machines, so its day-to-day rate is noise. The two probe accounts, Jono Work
(`jwork`) and Dave, are used almost only through gs, so gs should hold the
transcripts for nearly every token their meters count. Each account is joined
separately -- its own transcripts, its own meter log -- because one meter
paired with another account's tokens, or two meters averaged in one log,
measures nothing. tracker/capture.py then withholds every stretch the
transcripts cannot account for; that check, not this join, is what makes a
passive reading publishable.

`gs_accounts` is the one place that says where each account's transcripts and
meter live. Assumptions it depends on, as of 2026-09-15:

- jwork's meter is tracker.meter_log's own reset-bearing log,
  claude-usage-meter-jwork.log (deploy/systemd, not yet installed on
  2026-09-16). Until its first reading, jwork is read from
  gs-usage-ceiling.log, written by greenscape-org's usage-ceiling.py. That log
  read the default `~/.claude` -- a different account -- until the seat.conf
  drop-in of 2026-09-05 (its first jwork reading is 06:14:32Z, the meter
  falling from 100%/31% to 2%/18%), so everything before JWORK_CEILING_SINCE
  is dropped. It records no reset times; resets are inferred from the meter
  falling, which misses a reset when the new window has already passed the old
  reading by the next read (audit finding 10). Stretches built on it are
  `reset_verified: false`, and the publisher keeps them out of the measured
  series.
- jwork's `projects/` is a symlink to `~/.claude/projects`, which
  `~/.claude` and `~/.claude-jono` also write to. Neither held credentials on
  2026-09-15, but the takeoff pipeline ran 95 sessions under `~/.claude-jono`
  on the night of 2026-09-04/05 on another account, and those transcripts
  sit in jwork's directory. The report names every config dir sharing an
  account's transcripts (`shared_with`); what they add shows up in the check
  as surplus.
- Dave's meter is tracker.meter_log's own log, sampled from 2026-09-15; there
  is no Dave meter history before that.

`masterrig_account` adds Jonathan's own account beside them (issue #52). It is
deliberately not part of `gs_accounts`: it is not a clean instrument, because its
meter counts the account everywhere and only this host's transcripts are read
(MASTERRIG_METER_NOTE). It is joined here all the same because it is the
tracker's longest meter record -- back to 2026-06-13 against gs's 2026-09-05 --
and the per-stretch, per-model token detail was previously thrown away:
tracker/passive.py keeps one tokens-per-percent number per day. Both paths run;
neither replaces the other.

    python3 -m tracker.gs_passive --masterrig --out history/masterrig-passive.json

Units. A stretch is valued exactly as the publisher valued a probe row
(tracker/join.py bundle_meter_usd), and `passive_dollar_readings` returns
(time, meter dollars per full window) pairs, the element shape of
tracker/publish.py's `dollar_readings`, revalued at whatever prices the caller
publishes with. Since 2026-09-16 this is the published series on its own: the
probe (retired, see bin/daily.sh) read the same meter on a different scale --
its payload was 98% cache_write by list value where real sessions are 59%
cache_read, 24% cache_write and 17% output -- and the two never joined
cleanly. The probe rows stay in history/probes.jsonl and `calibrate` stays
here for the record; nothing publishes through them.

The capture-completeness check (tracker/capture.py) still runs and its verdict
is recorded per stretch as `capture_status`, but it no longer decides what is
published: CAPTURE_GATE is False. With its 15% tolerance on a quantity whose
stretch-to-stretch spread is 15-20%, it withheld half of jwork's real
stretches (49 of 99), and the half it withheld read low, so the published
median was the median of the expensive half. Every priced stretch counts now
(bar one the transcripts leave empty, see `publishable`), pooled per UTC day for
the published rate, and step detection reads the stretches one by one in meter
credits, weighted by the meter movement each one carries
(`passive_credit_points`; tracker/detect.py `detect_credit_changes` read them until the
four-account detector replaced it on 2026-10-23), instead of gating stretch by stretch.

    python3 -m tracker.gs_passive --out history/passive-gs.json
    python3 -m tracker.gs_passive --account jwork --until 2026-09-13T00:00:00+00:00
    python3 -m tracker.gs_passive --account jwork --withhold 'jwork:-home-jonathan-code-takeoff*'

`--withhold ACCOUNT:GLOB` leaves out transcripts whose path under `projects/`
matches, to watch the check catch deliberately missing capture on real data.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from fnmatch import fnmatch
from itertools import pairwise
from pathlib import Path
from statistics import mean, median, stdev

from .atomic import write_text_atomic
from . import credits as credit_model
from . import extract as extracts
from . import speed
from .capture import ACCEPTED, COLLECTION_GAP, UNJUDGED, UNPRICED, Verdict, check
from .cloud_sessions import CloudSpan, cloud_spans, overlaps
from .join import Stretch, build_stretches, bundle_meter_usd, window_points
from .meter_log import MASTERRIG_METER_LOG
from .rows import usable_rows
from .samples import Sample, infer_resets, merge_samples, parse_log
from .turns import iter_turns, transcript_paths, transcript_session_id
from .unclaimed import airlock_record
from .unclaimed import attribute as attribute_unclaimed
from .weekly_meter import seven_day_steps

JWORK_CEILING_SINCE = datetime(2026, 9, 5, 6, 14, 32, tzinfo=timezone.utc)
#: When True, only stretches the capture check accepts are published (the
#: 2026-09-15 design); when False, every priced stretch is, and the check's
#: verdict is advisory (`capture_status`). Off since 2026-09-16 -- see the
#: module docstring. The check itself is kept, not deleted. The 2026-09-16
#: audit (finding 10) asked for incomplete capture to be exposed and the rate
#: qualified, and warned against reviving a narrow band around the expected
#: rate: the band selects observations toward the answer. So the gate stays off
#: and the publisher labels every passive rate conditional on capture instead.
CAPTURE_GATE = False
#: A stretch whose transcripts hold no tokens at all: the meter moved and this host saw
#: nothing of it. Recorded as its own status rather than left as the capture check's
#: `accepted`, so nothing downstream reads `status == accepted` and then values $0 of
#: work against real meter movement. See `publishable`.
NO_TOKENS = "no-tokens"
#: How transcripts are found and attributed (`_split_sources`). Bumped when that changes on purpose,
#: so the turn-count guard (`transcript_loss`) does not mistake a deliberate recount for lost
#: transcripts. 2: every host's extracts, the cached archive, claims keyed by account, nested
#: workflow sub-agents and the masterrig shared-subtree filter (2026-10-23).
SOURCES_VERSION = 2
#: The guard's bar. An account's transcript turns only grow from run to run -- every turn since
#: its first meter sample, which never moves -- so a fall is lost transcripts. On 2026-10-15 a
#: gs disk cleanup took jwork from 91,131 turns to 18,209, dave from 76,690 to 39,941 and avis
#: from 24,494 to 3,897 between two runs, and every September stretch was silently rewritten
#: with no tokens. A fall of LOSS_FRACTION or more, and at least LOSS_MIN_TURNS turns, refuses
#: to write (exit LOSS_EXIT) and prints LOSS_MARKER for the alerting to pick up.
LOSS_FRACTION = 0.05
LOSS_MIN_TURNS = 500
LOSS_EXIT = 3
LOSS_MARKER = "INCIDENT transcript-loss"


@dataclass(frozen=True)
class MeterLog:
    """One more log of the same account's meter, in any tracker/samples.py format."""
    path: Path
    format: str


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path
    meter_log: Path
    #: A tracker/samples.py format name: "meter" (tracker.meter_log), "gs-ceiling"
    #: (usage-ceiling.py), "moonlighter" or "ceiling" (masterrig's two).
    meter_format: str
    meter_since: datetime | None = None
    #: A reset-less log this account was read from before `meter_log` existed. Its
    #: readings are used only up to the first reading of `meter_log`, and every
    #: stretch built on them is `reset_verified: false` (audit finding 10).
    legacy_meter_log: Path | None = None
    #: Further logs of this same meter, merged with `meter_log` a minute at a time
    #: (tracker/samples.py merge_samples, reset-bearing sources winning). masterrig's
    #: meter is two logs: moonlighter's JSONL and its ceiling's systemd log.
    extra_meter_logs: tuple[MeterLog, ...] = ()
    #: What a reader of this account's stretches has to know about its meter. Recorded
    #: in the report's `meter` meta, not used in any arithmetic.
    meter_note: str | None = None
    #: Where the cloud tooling on this account's host logs `claude --cloud` launches
    #: (tracker/cloud_sessions.py), and whether a launch is this account's only once its
    #: teleported transcript has been grabbed (a host with several logins, whose log does
    #: not name one). None: no cloud sessions are recorded for this account.
    cloud_dir: Path | None = None
    cloud_requires_grab: bool = False


def gs_accounts(home: Path | None = None) -> dict[str, Account]:
    """Where each gs account's transcripts and meter live. The only copy of this mapping."""
    home = Path(home) if home is not None else Path.home()
    ops = home / ".paperclip" / "ops"
    return {
        # jwork: `~/bin/cloud-grab` teleports under ~/.claude-javiswork alone, so a grabbed
        # cloud session is this account's (tracker/cloud_sessions.py).
        "jwork": Account("jwork", home / ".claude-javiswork", ops / "claude-usage-meter-jwork.log", "meter",
                         JWORK_CEILING_SINCE, legacy_meter_log=ops / "gs-usage-ceiling.log",
                         cloud_dir=home / "private" / "cloud", cloud_requires_grab=True),
        "dave": Account("dave", home / ".claude-dave", ops / "claude-usage-meter-dave.log", "meter"),
        # avis: Max 20x, sampled since 2026-09-23 09:47Z. Its projects/ is a symlink into
        # the same pooled ~/.claude/projects as jwork's (see transcript_files), and it has
        # its own session-env, so the same pooled-projects filter attributes its
        # transcripts. No legacy log: meter_log.py's own log is all it has ever had.
        "avis": Account("avis", home / ".claude-avis", ops / "claude-usage-meter-avis.log", "meter"),
    }


#: Why masterrig's stretches are kept but never published as a rate. Its meter counts
#: the account everywhere -- claude.ai in a browser, the phone app, any other machine --
#: while only this host's ~/.claude/projects is read, so a stretch's percent includes
#: movement its tokens cannot explain. That is the phantom the capture check measures
#: (`capture`, `capture_status`), and it is why the same join on this account spreads
#: fifteen-fold day to day where gs's spreads by a fifth. The stretches are written all
#: the same: they are the longest per-model token record the tracker has, and a reader
#: who wants only the clean ones has `capture` to filter on.
MASTERRIG_METER_NOTE = (
    "This meter counts the whole account -- web, phone and every other machine -- while "
    "only this host's transcripts are read, so every stretch may carry usage the tokens "
    "cannot see. Read `capture` and `capture_status` per stretch before using a rate; the "
    "capture check is advisory here (CAPTURE_GATE is False) and never withholds a stretch "
    "from this file."
)


def masterrig_account(home: Path | None = None) -> Account:
    """Jonathan's own account on masterrig: three logs of one meter, and ~/.claude's transcripts.

    The first pair is what tracker/passive.py has always merged by hand -- moonlighter's
    usage_log.jsonl (reset-bearing) and the ceiling's systemd log (not) -- and it is
    the tracker's longest meter record, back to 2026-06-13. Unlike the gs accounts
    this one is not a clean instrument; see MASTERRIG_METER_NOTE.

    The third is tracker.meter_log's own log for this account (MASTERRIG_METER_LOG), the
    sampler gs runs for jwork, dave and avis. The ceiling was the only reading closer
    together than moonlighter's 30 minutes, and MAX_PAIR_GAP (tracker/join.py) is 15: it
    stopped reading at 2026-10-18 00:44Z, when the Paperclip board API it asks which
    account the seats are on began answering 401 (later HTTP 0; the unit was stopped with
    Paperclip on 2026-10-20 and is disabled), so this account formed no stretch after
    2026-10-17 11:14Z. The ceiling never called the usage endpoint once that lookup
    failed; moonlighter, on the same login, read on throughout. The sampler asks nothing
    but the endpoint, labels every line with the account it read, and records reset times.
    A log not yet written reads as no samples, so the join is unchanged until it exists.
    """
    home = Path(home) if home is not None else Path.home()
    ops = home / ".paperclip" / "ops"
    # Every `claude --cloud` masterrig logs is read as this account's. That held while it had one
    # login; since 1 Oct it has four (~/.claude-avis, -jwork, -dave), and ~/bin/cloud-build does
    # not log which it ran under. Every row so far (8, all 2026-09-28) predates the second.
    return Account("masterrig", home / ".claude", home / ".moonlighter" / "usage_log.jsonl", "moonlighter",
                   extra_meter_logs=(MeterLog(ops / "mis-usage-ceiling-systemd.log", "ceiling"),
                                     MeterLog(ops / MASTERRIG_METER_LOG, "meter")),
                   meter_note=MASTERRIG_METER_NOTE, cloud_dir=home / "private" / "cloud")


def _read(path: Path, fmt: str, since: datetime | None) -> list[Sample]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", errors="replace") as fh:  # one bad byte costs its line only
        return parse_log(fmt, fh, since)


def load_samples(account: Account, until: datetime | None = None) -> list[Sample]:
    """Every reading of this account's meter, in time order.

    `meter_log` and each of `extra_meter_logs` read the same meter, so they are
    merged a minute at a time rather than concatenated: two sources agreeing on
    one minute would otherwise pair a reading with itself and add a spurious
    zero-movement pair. `legacy_meter_log` is different -- a log the account was
    read from *before* `meter_log` existed -- so it is only used up to
    `meter_log`'s first reading. It is also the one reset-less source here
    (`gs-ceiling` never records a reset time -- tracker/samples.py), so its
    readings alone go through `infer_resets` (audit finding 10) before joining
    the rest; a reading that already carries a recorded reset is never touched.
    A sample whose reset came from that inference, rather than the log, is
    marked by suffixing its `source` with `-inferred` (`_reset_source` reads
    this to record each stretch's `reset_source`).
    """
    samples = _read(account.meter_log, account.meter_format, account.meter_since)
    if account.extra_meter_logs:
        samples = merge_samples(samples, *(_read(m.path, m.format, account.meter_since)
                                           for m in account.extra_meter_logs))
    if account.legacy_meter_log is not None and account.legacy_meter_log.exists():
        first = min((s.ts for s in samples), default=None)
        legacy_raw = [s for s in _read(account.legacy_meter_log, "gs-ceiling", account.meter_since)
                      if first is None or s.ts < first]
        # `gs-ceiling` never records a reset itself, so every filled `resets_at` here came
        # from inference; tag the source so `_reset_source` can tell a stretch built on it.
        legacy = [replace(s, source=f"{s.source}-inferred") if s.resets_at is not None else s
                  for s in infer_resets(legacy_raw)]
        samples = sorted(legacy + samples, key=lambda s: s.ts)
    return [s for s in samples if until is None or s.ts <= until]


def transcript_files(account: Account, since: datetime | None, withhold: Iterable[str] = (),
                     home: Path | None = None) -> tuple[list[Path], dict | None]:
    """This account's transcripts, and how the pooled-projects filter split them, if it applied.

    jwork's `projects/` is a symlink shared with the bare `~/.claude`,
    `~/.claude-jono` and `~/.claude-avis` config dirs (see the module
    docstring): a raw glob over it would count those other logins' tokens as
    jwork's own -- `.claude-avis` alone holds 430 files and 3.09 billion tokens
    of another account in jwork's meter window. Claude Code writes a
    per-config-dir `<config dir>/session-env/<sessionId>/` for every session it
    runs under that login, and a transcript's session id is its filename stem, or
    its parent session's for a sub-agent file (tracker/turns.py
    transcript_session_id) -- the same rule contrib/sample.py's `own_session_filter` applies for
    the contributed export.

    The filter fires whenever the transcripts root is *shared* -- another config
    dir under `home` resolves to the same directory -- or is reached through a
    symlink at all, and the account has a `session-env` directory to filter by.
    Sharing is the condition that matters; the symlink test is kept beside it
    because a pooled root can be reached without `projects/` itself being the
    link (`~/.claude-javiswork` could be the link instead), and because a pooled
    root whose other config dirs have since been removed is still not this
    account's own. With no `session-env` the filter is a no-op even for a shared
    root, since there is nothing to tell sessions apart with.

    The second return value is `None` when the filter did not apply, and
    otherwise counts what it did, for the account's `transcripts` meta:
    `dropped_to` names the config dir that claims each dropped transcript, and
    `unclaimed` counts the dropped transcripts *no* config dir under `home`
    claims. That last number is the blind spot, and it is reported rather than
    silently folded into `dropped`: a session that never wrote a `session-env`
    entry -- a one-shot `claude -p` that starts no shell, which is what the
    tracker's own retired probe, the airlock bench and the filing judge all are
    -- cannot be attributed to any login, so it is dropped even when it was this
    account's own spend. On 2026-09-23 that was 1,407 files, 1,518 turns and
    39.7M tokens in jwork's meter window, against 1,782 files and 8,316M tokens
    kept: 0.48%, and adding every one of them lifts no unaccounted stretch into
    the accepted band. docs/findings-2026-09-23-unaccounted.md has the working.

    Since 2026-10-23 a session is claimed by an *account*, not a config dir (`_split_sources`): every
    dir signed into the same account counts as this one, the claim records are session-env,
    file-history, sessions/<pid>.json and history.jsonl (tracker/extract.py
    `session_claims`), and a session two accounts claim is split turn by turn at their claim
    times. A file kept whole here may therefore still lose some turns to another account in
    `report`; this list is the files, `report` reads the turns.
    """
    kept, meta, _ = _split_transcripts(account, since, withhold, home)
    return kept, meta


def unclaimed_transcripts(account: Account, since: datetime | None, withhold: Iterable[str] = (),
                          home: Path | None = None) -> list[Path]:
    """The pooled root's transcripts that no config dir under `home` claims (`transcript_files`).

    Nothing tells whose meter they spent on. Headless `claude -p` runs start no shell, so they
    write no `session-env` entry: the auto-mail filing judge, whose seat picker runs it under
    whichever of the gs logins has room, and the airlock bench, which runs under the
    `CLAUDE_CONFIG_DIR` it inherits. Their tokens are recorded per stretch as
    `unclaimed_tokens` so a fit can measure how much of them the account's meter carried
    (`tracker.credits.unclaimed_shares`) rather than leave them out or add them whole. Empty
    when the pooled-projects filter did not apply.
    """
    return _split_transcripts(account, since, withhold, home)[2]


def _split_transcripts(account: Account, since: datetime | None, withhold: Iterable[str] = (),
                       home: Path | None = None) -> tuple[list[Path], dict | None, list[Path]]:
    """(this account's transcripts, the filter's counts or None, the unclaimed transcripts)."""
    s = _split_sources(account, since, withhold, home)
    return s.kept, s.meta, s.unclaimed


@dataclass(frozen=True)
class Sources:
    """Where an account's transcripts are read besides its own `projects/` tree (`_split_sources`).

    - `archive_root`: `<dir>/transcripts-*/<subtree>/*.tar.zst`, transcripts a cleanup packed
      away (tracker/extract.py `build_archive_cache`), read in place through `archive_cache`.
    - `remote_root`: `<dir>/<host>/<identity>/`, other hosts' text-free extracts of this
      account's work (tracker/extract.py `extract`, shipped by bin/ship-extracts.sh).

    The defaults are fixed paths under the home directory, so bin/daily.sh needs no flag.
    """
    archive_root: Path
    archive_cache: Path
    remote_root: Path


def default_sources(home: Path) -> Sources:
    home = Path(home)
    return Sources(home / extracts.ARCHIVE_ROOT, home / extracts.ARCHIVE_CACHE, home / extracts.REMOTE_ROOT)


@dataclass(frozen=True)
class _Resolved:
    """`Sources` as found on disk for one run: the archive roots, the shipped extract roots and
    which setup-token fingerprints are which account (tracker/extract.py `token_bindings`)."""
    archives: tuple[extracts.ArchiveRoot, ...] = ()
    remotes: tuple[tuple[str, str, Path], ...] = ()
    bindings: tuple[tuple[str, str], ...] = ()


def resolve_sources(home: Path, sources: Sources | None = None, build: bool = False) -> _Resolved:
    """What `sources` holds on disk. `build` brings the archive cache up to date first (a full
    run does, once); without it only what is already cached is read."""
    sources = sources or default_sources(home)
    archives = (extracts.build_archive_cache(home, sources.archive_root, sources.archive_cache) if build
                else extracts.archive_roots(home, sources.archive_root, sources.archive_cache))
    remotes = extracts.remote_roots(sources.remote_root)
    # A shipped token-named identity is joined through the meter units' token; read the token
    # only when there is one to join.
    bindings = extracts.token_bindings(home) if any(i.startswith("tok-") for _, i, _ in remotes) else {}
    return _Resolved(tuple(archives), tuple(remotes), tuple(sorted(bindings.items())))


@dataclass
class _Splitted:
    kept: list[Path]
    meta: dict | None
    unclaimed: list[Path]
    #: (label, root, kept files under it), live root first, for per-source turn counts.
    by_source: list[tuple[str, Path, list[Path]]]
    #: Contested files' claim segments; `keep` reads them turn by turn.
    segments: dict[Path, extracts.Segments]
    group: str
    identities: set[str]

    def keep(self, path: Path, turn) -> bool:
        segs = self.segments.get(path)
        return segs is None or extracts.owner_at(segs, turn.ts) == self.group


def shared_subtrees(account: Account, home: Path) -> dict[Path, list[str]]:
    """Project directories inside this account's `projects/` that another config dir links into
    its own tree: real path -> the linking dirs' names.

    `shared_with` catches a whole pooled root. This catches one project: masterrig's
    `~/.claude-avis/projects/<a project>` has been a symlink into `~/.claude/projects` since
    2026-10-18 11:27Z, so a1's join counted avis's work there as a1's: a1's tokens_per_pct went
    from 10.7M on 17 Oct to 47.5M on 18 Oct, and 87% of 18-19 Oct's meter dollars in that
    directory were avis's.
    """
    root = account.config_dir / "projects"
    if not root.exists():
        return {}
    mine = root.resolve()
    out: dict[Path, list[str]] = {}
    for d in sorted(Path(home).glob(".claude*")):
        p = d / "projects"
        if d == account.config_dir or not d.is_dir() or not p.is_dir() or p.resolve() == mine:
            continue
        for e in sorted(p.iterdir()):
            if e.is_symlink() and e.is_dir() and e.resolve().is_relative_to(mine):
                out.setdefault(e.resolve(), []).append(d.name)
    return out


def _split_sources(account: Account, since: datetime | None, withhold: Iterable[str] = (), home: Path | None = None,
           resolved: _Resolved | None = None) -> _Splitted:
    """This account's transcripts from every source, attributed by account identity.

    Sources, each read the same way: the live `projects/` tree, the cached archive of that
    same directory (`Sources.archive_root`), and every other host's extracts shipped under
    this account's identity (`Sources.remote_root`). Turns are deduplicated by message id
    across all of them (tracker/turns.py `iter_turns`), live first.

    The pooled-projects filter (see `transcript_files`) applies to the live tree and its
    archive, and now keys claims by account, not by config dir: gs's `~/.claude-jono` and
    `~/.claude-javiswork` are both jwork's login, and jono's sessions were dropped as
    "another login" (`dropped_to {'.claude-jono': 7}`, 350 turns since 1 Oct). A session
    claimed by dirs of two different accounts is split turn by turn at their claim times
    (tracker/extract.py `owner_at`). Shipped extracts were attributed on the host that wrote
    them and are kept whole.
    """
    home = Path(home) if home is not None else account.config_dir.parent
    resolved = resolved if resolved is not None else resolve_sources(home)
    me = extracts.login(account.config_dir, home)
    root = account.config_dir / "projects"
    identities = {me.identity} if me.identity else set()
    bindings = dict(resolved.bindings)
    identities |= {fp for fp, ident in bindings.items() if ident in identities}
    remote = [(f"remote:{host}", r) for host, ident, r in resolved.remotes if ident in identities]
    if not root.exists():
        return _Splitted([], None, [], [], {}, me.group, identities)
    patterns = list(withhold)

    def listed(r: Path) -> list[Path]:
        return [p for p in transcript_paths(r, since)
                if not any(fnmatch(str(p.relative_to(r)), pat) for pat in patterns)]

    mine = root.resolve()
    pooled_roots = [("live", root)] + [(a.label, a.cache_root) for a in resolved.archives
                                       if a.cache_root.exists() and a.source_projects.exists()
                                       and a.source_projects.resolve() == mine]
    remote_files = [(label, r, listed(r)) for label, r in remote]
    others = shared_with(account, home)
    subtrees = {real.relative_to(mine): names for real, names in shared_subtrees(account, home).items()}
    whole = bool(others) or root.is_symlink()
    claimants_of = {}  # relative subtree (or None for the whole root) -> other dirs that may claim
    if whole:
        claimants_of[None] = others
    claimants_of.update(subtrees)
    if not claimants_of or (whole and not subtrees and not (account.config_dir / "session-env").is_dir()):
        by_source = [(label, r, listed(r)) for label, r in pooled_roots] + remote_files
        return _Splitted([p for _, _, fs in by_source for p in fs], None, [], by_source, {}, me.group, identities)

    def contested_by(rel: Path) -> list[str] | None:
        """The other dirs that may claim a file at `rel` under the root, or None if no one may."""
        for sub, names in subtrees.items():
            if rel.is_relative_to(sub):
                return names + (others if whole else [])
        return others if whole else None

    # Each config dir's account, read once (a `.claude.json` can run to tens of kilobytes).
    group_of = {d.name: extracts.login(d, home).group for d in sorted(home.glob(".claude*")) if d.is_dir()}
    group_of[account.config_dir.name] = me.group
    groups: dict[str, list[Path]] = {me.group: [account.config_dir]}
    for n in sorted(set(others) | {n for ns in subtrees.values() for n in ns}):
        groups.setdefault(group_of.get(n, f"dir:{n}"), []).append(home / n)
    # Every dir of this account counts as its own, whatever its projects root (jono and javiswork).
    for n, g in group_of.items():
        if g == me.group and home / n not in groups[me.group]:
            groups[me.group].append(home / n)
    book = extracts.ClaimBook(groups)
    listed_roots = [(label, r, listed(r)) for label, r in pooled_roots]
    book.load({transcript_session_id(p) for _, _, fs in listed_roots for p in fs})
    dropped_to: dict[str, int] = {}
    attributed: dict[str, int] = {}
    unclaimed_paths: list[Path] = []
    segments: dict[Path, extracts.Segments] = {}
    contested = 0
    harness, airlock = _attribution_records(home)
    by_source = []
    n_paths = 0
    for label, r, paths in listed_roots:
        kept_here = []
        for p in paths:
            n_paths += 1
            who_may = contested_by(p.relative_to(r))
            if who_may is None:
                kept_here.append(p)
                continue
            sid = transcript_session_id(p)
            allowed = {me.group} | {group_of.get(n, f"dir:{n}") for n in who_may}
            segs = [(t, g) for t, g in book.segments(sid) if g in allowed]
            claiming = {g for _, g in segs}
            if claiming == {me.group}:
                kept_here.append(p)
            elif me.group in claiming:
                contested += 1
                segments[p] = segs
                kept_here.append(p)
            elif claiming:
                first = next(g for _, g in segs)
                name = next(d.name for d in groups[first])
                dropped_to[name] = dropped_to.get(name, 0) + 1
            else:
                # No login claims it: a headless `claude -p` run. Where a launcher's own
                # record says which config dir it ran under (tracker/unclaimed.py), it is that
                # dir's work -- kept here if it is this account's, dropped if another's. Only the
                # rest stays unclaimed, for `unclaimed_shares` to fit.
                who = attribute_unclaimed(p, home, harness, airlock)
                if who is None:
                    unclaimed_paths.append(p)
                    continue
                attributed[who.rule] = attributed.get(who.rule, 0) + 1
                if group_of.get(who.config_dir, f"dir:{who.config_dir}") == me.group:
                    kept_here.append(p)
                else:
                    dropped_to[who.config_dir] = dropped_to.get(who.config_dir, 0) + 1
        by_source.append((label, r, sorted(kept_here)))
    by_source += remote_files
    kept = [p for _, _, fs in by_source for p in fs]
    pooled_kept = [p for _, _, fs in by_source[:len(listed_roots)] for p in fs]
    meta = {"kept": len(pooled_kept), "dropped": n_paths - len(pooled_kept),
            "subagent_files": sum(1 for p in pooled_kept if "subagents" in p.parts),
            "dropped_to": dict(sorted(dropped_to.items())),
            "attributed_by_record": dict(sorted(attributed.items())),
            "unclaimed": len(unclaimed_paths)}
    same = sorted(d.name for d in groups.get(me.group, []) if d != account.config_dir)
    if same:
        meta["same_account"] = same
    if contested:
        meta["contested"] = contested
    if subtrees:
        # Counted per linking config dir, never named: this meta is stored as the account's
        # `own_sessions`, and masterrig's passive.sh commits history/masterrig-passive.json to the
        # public repo. Project directory names leave a host only in the extracts shipped to gs's
        # private directory (bin/ship-extracts.sh); the 2026-10-23 review found the first cut of
        # this record publishing a masterrig project directory by name.
        linked: dict[str, int] = {}
        for names in subtrees.values():
            for n in names:
                linked[n] = linked.get(n, 0) + 1
        meta["shared_subtrees"] = dict(sorted(linked.items()))
    return _Splitted(kept, meta, unclaimed_paths, by_source, segments, me.group, identities)


def _attribution_records(home: Path) -> tuple[list[tuple[str, datetime, datetime]], tuple[str, datetime] | None]:
    """The records `tracker.unclaimed.attribute` reads: the tracker's own runs as (config
    dir, start, end), and airlock's `tune.env`."""
    dirs = {name: acct.config_dir.name for name, acct in gs_accounts(home).items()}
    harness = [(dirs[r.account], r.start, r.end) for r in credit_model.harness_runs() if r.account in dirs]
    return harness, airlock_record(home)


def shared_with(account: Account, home: Path) -> list[str]:
    """Other config dirs under `home` whose `projects/` is this account's directory."""
    root = account.config_dir / "projects"
    if not root.exists():
        return []
    mine = root.resolve()
    out = []
    for d in sorted(Path(home).glob(".claude*")):
        if d == account.config_dir or not d.is_dir():
            continue
        p = d / "projects"
        if p.exists() and p.resolve() == mine:
            out.append(d.name)
    return out


def probe_readings(rows: list[dict], prices: dict) -> list[tuple[datetime, float]]:
    """(time, meter dollars per 1%) for usable prose probe rows, the evidence tracker/capture.py weighs steps against."""
    out = []
    for r in usable_rows(rows):
        ticks = r.get("tick_to", 0) - r.get("tick_from", 0)
        usd = bundle_meter_usd(r["model"], r["tokens"], prices) if ticks > 0 else None
        if usd is not None:
            out.append((datetime.fromisoformat(r["ts"]), usd / ticks))
    return sorted(out)


def publishable(v: Verdict) -> bool:
    """Whether a stretch is published (see CAPTURE_GATE).

    With the gate off every priced stretch is, except one the transcripts leave
    all but empty: capture under COLLECTION_GAP (a tenth of the account's own
    reference). That is not the 15% band -- it is the meter moving with nothing
    on gs to explain it, which is off-gs use of the account. Real case: jwork's
    meter rose 67% between 21:47 and 00:51 on 2026-09-13/14 with zero transcript
    turns; published, those seven stretches read $0.00 per 1% and dragged the day
    to nothing.

    A stretch with any unpriced token is never published, whatever its share of
    the raw tokens (audit finding 9): a small share of unpriced output can be most
    of the meter dollars next to a large free cache-read bundle, so no raw-token
    tolerance bounds the error.

    Whether the stretch's meter log carried reset ids is not decided here: it is
    recorded as `reset_verified` and the publisher treats reset-less stretches as
    legacy, conditional evidence.
    """
    if v.status == UNPRICED or v.stretch.unpriced_tokens > 0:
        return False
    if not any(sum(by_class.values()) for by_class in v.stretch.tokens.values()):
        # No tokens at all is the extreme of the collection gap above, and it slips
        # past that rule whenever the stretch has no reference to divide by (capture
        # None: unjudged, or an account whose own bootstrap median is zero). masterrig
        # has 28 such stretches in 237; on gs they already read capture 0.0 and were
        # withheld, so this changes nothing there.
        return False
    if CAPTURE_GATE:
        return v.status == ACCEPTED
    return v.capture is None or v.capture >= COLLECTION_GAP


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None or x == float("inf") else round(x, n)


def _reset_source(s: Stretch, samples: list[Sample]) -> str:
    """How the stretch's five-hour resets are known: `"logged"`, `"inferred"`, or `"none"`.

    Mirrors the rule `build_stretches` already applies to `reset_verified` (any
    sample in range with no `resets_at` makes the whole stretch unverified): a
    stretch with such a sample is `"none"`. Otherwise every boundary carries a
    reset, and it is `"inferred"` if any of them came from `infer_resets` (its
    `source` is suffixed `-inferred` by `load_samples`), or `"logged"` if every
    one was read straight from a meter log.
    """
    in_range = [x for x in samples if s.start <= x.ts <= s.end]
    if any(x.resets_at is None for x in in_range):
        return "none"
    if any(x.source.endswith("-inferred") for x in in_range):
        return "inferred"
    return "logged"


#: Whether an inferred reset (tracker/samples.py `infer_resets`) counts as verified evidence.
#: False: validated against dave, jwork and masterrig's own reset-bearing logs with their
#: recorded resets stripped and compared back (2026-09-23) -- dave's inferred resets matched
#: exactly (577/577, zero spurious), but jwork still missed one real window by ~7 minutes
#: (56/1092 samples outside a one-sample-gap tolerance) and masterrig, whose 30-minute cadence
#: widens the same blind spot, missed more (402/2738 wrong, 180 spurious). The acceptance bar
#: is zero wrong and zero spurious; this misses it, so an inferred reset is recorded (`reset_
#: source: "inferred"`) but not certified (`reset_verified` stays false) until that holds.
INFERRED_RESET_VERIFIED = False


def _stretch_record(v: Verdict, reset_source: str = "logged",
                    unclaimed: dict[datetime, dict] | None = None,
                    cloud: list[CloudSpan] | None = None) -> dict:
    s = v.stretch
    lo, hi = s.bounds
    # Unpriced work is kept by raw model id beside the priced models, so the record
    # shows what the meter was charged for even when no price covers it.
    tokens = {**s.tokens, **s.unpriced}
    if publishable(v):
        status = ACCEPTED
    elif s.unpriced_tokens:
        status = UNPRICED
    elif v.status == ACCEPTED:
        # The capture check accepted it and `publishable` did not: the one case is a
        # stretch with no tokens at all, which has no reference to be judged against
        # (see `publishable`). Naming it keeps `accepted` meaning publishable, without
        # relabelling a stretch the check itself withheld.
        status = NO_TOKENS
    else:
        status = v.status
    # join.py flags `reset_verified` from `resets_at` alone, with no notion of where that
    # value came from; an inferred one only counts if INFERRED_RESET_VERIFIED says the
    # inference has earned it (see that constant).
    reset_verified = s.reset_verified and (reset_source != "inferred" or INFERRED_RESET_VERIFIED)
    return {"start": s.start.isoformat(), "end": s.end.isoformat(), "delta_pct": s.delta_pct, "windows": s.windows,
            "usd": _r(s.usd), "usd_per_pct": _r(s.usd_per_pct), "bounds": [_r(lo), _r(hi)], "tokens": tokens,
            "unpriced_tokens": s.unpriced_tokens, "unpriced": s.unpriced, "turns": s.turns,
            "reset_verified": reset_verified, "reset_source": reset_source, "status": status, "capture_status": v.status,
            "reference": _r(v.reference), "capture": _r(v.capture),
            "fast_session_tokens": s.fast_session_tokens, "fast_session_turns": s.fast_session_turns,
            "first_turns": {m: ts.isoformat() for m, ts in sorted(s.first_turns.items())},
            # Cloud-session work in the stretch: turns teleported back from a cloud session,
            # or a recorded cloud session of this account running in its span
            # (tracker/cloud_sessions.py). tracker/credits.py `clean_stretches` leaves such a
            # stretch out of every fit and change figure.
            "remote_sourced_turns": s.remote_sourced_turns,
            "cloud_session": bool(s.remote_sourced_turns or overlaps(cloud or [], s.start, s.end)),
            # The part of `tokens` from headless `claude -p` runs' own turns, which the
            # five-hour meter charges more per credit (tracker/credits.py `headless_factor`).
            "headless_tokens": s.headless_tokens, "headless_turns": s.headless_turns,
            # Only where the pooled-projects filter ran: the work no login claims, in this
            # stretch's pairs (`unclaimed_transcripts`); an empty dict when there was none.
            **({"unclaimed_tokens": unclaimed.get(s.start, {})} if unclaimed is not None else {})}


def _pieces(stretches: list[Stretch]) -> int:
    """Separate window pieces under a pooled set of stretches.

    Consecutive stretches that meet at one reading telescope: the reading
    between them cancels, so they share a piece and its one point of rounding.
    """
    joins = sum(1 for a, b in pairwise(stretches) if b.start == a.end)
    return sum(s.windows for s in stretches) - joins


def _daily(verdicts: list[Verdict]) -> list[dict]:
    """Published stretches pooled per UTC day, with their rounding bounds and what qualifies them.

    `bounds` concedes one point of rounding per separate window piece, as a
    stretch's own bounds do; `reset_verified` is true only when every stretch of
    the day came from a log with reset ids; `capture_diagnosed` counts the day's
    stretches the capture check did not accept (published with CAPTURE_GATE off).
    """
    days: dict[str, list[Verdict]] = {}
    for v in verdicts:
        if publishable(v):
            days.setdefault(v.stretch.end.astimezone(timezone.utc).date().isoformat(), []).append(v)
    out = []
    for day, vs in sorted(days.items()):
        ss = [v.stretch for v in vs]
        delta = sum(s.delta_pct for s in ss)
        pieces = _pieces(ss)
        usd = sum(s.usd for s in ss)
        lo = usd / (delta + pieces)
        hi = usd / (delta - pieces) if delta > pieces else None
        out.append({"date": day, "usd_per_pct": _r(usd / delta), "delta_pct": delta,
                    "stretches": len(ss), "rounding": _r(pieces / delta),
                    "bounds": [_r(lo), _r(hi)], "pieces": pieces,
                    "reset_verified": all(s.reset_verified for s in ss),
                    "capture_diagnosed": sum(1 for v in vs if v.status != ACCEPTED)})
    return out


def spread(values: list[float]) -> dict:
    """How tightly a series of readings agrees: coefficient of variation, half-range and MAD, each relative."""
    if not values:
        return {"n": 0}
    m = median(values)
    if not m:
        # Every relative measure below divides by the median. A zero median is a set of
        # readings that spent nothing, which has a count and nothing else to say.
        return {"n": len(values), "median": 0.0, "half_range": None, "mad": None, "cv": None}
    out = {"n": len(values), "median": _r(m), "half_range": _r((max(values) - min(values)) / 2 / m),
           "mad": _r(1.4826 * median(abs(v - m) for v in values) / m)}
    out["cv"] = _r(stdev(values) / mean(values)) if len(values) > 1 else None
    return out


def _state(verdicts: list[Verdict], account_runs: list) -> dict:
    judged = [v for v in verdicts if v.status not in (UNJUDGED, UNPRICED)]
    if not verdicts:
        return {"state": "no data"}
    if not CAPTURE_GATE:
        return {"state": "ok"} if any(publishable(v) for v in verdicts) else {"state": "nothing publishable"}
    if not judged:
        return {"state": "unjudged"}
    if judged[-1].status == ACCEPTED:
        return {"state": "ok"}
    run = account_runs[-1]
    return {"state": "withholding", "since": run.start.isoformat(), "kind": run.kind}


def _run_record(run) -> dict:
    return {"start": run.start.isoformat(), "end": run.end.isoformat(), "kind": run.kind, "direction": run.direction,
            "stretches": len(run.verdicts), "step": _r(run.step), "capture": _r(run.capture),
            "corroborated_by": run.corroborated_by, "contradicted_by": run.contradicted_by}


def _split(stretches: list[Stretch]) -> dict:
    """Token-class shares over a set of stretches, what a full window buys divided into classes."""
    tot = {c: 0 for c in ("input", "output", "cache_read", "cache_write")}
    for s in stretches:
        for by_class in s.tokens.values():
            for c in tot:
                tot[c] += by_class.get(c, 0)
    grand = sum(tot.values())
    if not grand:
        return {}
    out = {c: round(n / grand, 6) for c, n in tot.items()}
    one_hour = sum(by_class.get("cache_write_1h", 0)
                   for s in stretches for by_class in s.tokens.values())
    if one_hour:
        out["cache_write_1h"] = round(one_hour / grand, 6)
    return out


def fast_session_ids(files: Iterable[Path]) -> set[str]:
    """Message ids of the fast-session requests in these transcripts.

    tracker/speed.py's rule, over the same files the stretches are built from: an Opus
    request whose session's running median speed is at least FAST_FACTOR times the
    model's median over the scan. Their tokens stay in the stretch; each stretch also
    records them as `fast_session_tokens` (tracker/join.py Stretch).
    """
    return speed.fast_sessions(speed.requests_from_files(files), every=True)


def account_turns(split: _Splitted, until: datetime | None = None) -> tuple[list, list[dict]]:
    """A split's turns, each message id once across its sources, and what each source added.

    Sources are read in `split.by_source` order (live, archive, other hosts) through one
    `seen` set, so a turn two sources both hold is credited to the first. Contested files
    keep only this account's turns (`_Splitted.keep`).
    """
    seen: set[str] = set()
    turns, per_source = [], []
    for label, root, files in split.by_source:
        before = len(turns)
        turns += [t for t in iter_turns(files, keep=split.keep, seen=seen) if until is None or t.ts <= until]
        per_source.append({"source": label, "root": str(root), "files": len(files), "turns": len(turns) - before})
    return turns, per_source


def report(accounts: dict[str, Account], prices: dict, probe_rows: Iterable[dict] = (), now: datetime | None = None,
           withhold: dict[str, list[str]] | None = None, until: datetime | None = None,
           home: Path | None = None, sources: Sources | None = None) -> dict:
    """Join, judge and summarise every account; the JSON tracker.gs_passive writes."""
    now = now or datetime.now(timezone.utc)
    withhold = withhold or {}
    stretches, meta, weekly, reset_sources = {}, {}, {}, {}
    unclaimed: dict[str, dict[datetime, dict]] = {}
    cloud: dict[str, list[CloudSpan]] = {}
    base = Path(home) if home is not None else next(iter(accounts.values())).config_dir.parent if accounts else None
    resolved = resolve_sources(base, sources, build=True) if base is not None else _Resolved()
    joined_remote: set[tuple[str, str]] = set()
    for name, account in accounts.items():
        samples = load_samples(account, until)
        # Until inferred resets are certified, nothing is computed from them: the join,
        # the weekly points and every `reset_verified` rollup see the reset-less samples
        # they saw before, and only `_reset_source` reads the inferred ones.
        joined = samples if INFERRED_RESET_VERIFIED else [
            replace(s, resets_at=None) if s.source.endswith("-inferred") else s for s in samples]
        since = samples[0].ts if samples else None
        split = (_split_sources(account, since, withhold.get(name, ()), home, resolved) if samples
                 else _Splitted([], None, [], [], {}, "", set()))
        files, own_sessions, unclaimed_files = split.kept, split.meta, split.unclaimed
        turns, per_source = account_turns(split, until)
        joined_remote |= {(host, ident) for host, ident, _ in resolved.remotes if ident in split.identities}
        fast = fast_session_ids(files)
        stretches[name] = build_stretches(joined, turns, prices, fast=fast)
        if own_sessions is not None:
            # The same samples close the same stretches whatever the turns, so building them
            # again over the unclaimed turns gives each stretch the unclaimed work in its pairs.
            others = [t for t in iter_turns(unclaimed_files) if until is None or t.ts <= until]
            unclaimed[name] = {s.start: {**s.tokens, **s.unpriced}
                               for s in build_stretches(joined, others, prices)}
        cloud[name], unattributed = cloud_spans(account.cloud_dir, account.cloud_requires_grab)
        reset_sources[name] = {s.start: _reset_source(s, samples) for s in stretches[name]}
        weekly[name] = window_points(joined)
        # The weekly limit measured directly: every one-point step of the seven-day meter
        # with this account's own tokens in it (tracker/weekly_meter.py).
        steps = seven_day_steps(joined, turns, prices, cloud[name], fast=fast)
        root = account.config_dir / "projects"
        meta[name] = {
            "meter": {"log": str(account.meter_log), "format": account.meter_format,
                      "legacy_log": str(account.legacy_meter_log) if account.legacy_meter_log else None,
                      "extra_logs": [{"log": str(m.path), "format": m.format} for m in account.extra_meter_logs],
                      "note": account.meter_note,
                      "reset_verified_samples": sum(1 for s in joined if s.resets_at is not None),
                      "since": account.meter_since.isoformat() if account.meter_since else None,
                      "samples": len(samples), "first": since.isoformat() if since else None,
                      "last": samples[-1].ts.isoformat() if samples else None},
            "transcripts": {"root": str(root), "resolves_to": str(root.resolve()),
                            "shared_with": shared_with(account, home or account.config_dir.parent),
                            "files": len(files), "turns": len(turns), "withheld_patterns": list(withhold.get(name, ())),
                            "own_sessions": own_sessions,
                            # How many files and turns each source gave: the guard in `main`
                            # compares these run to run (`transcript_loss`). Never the account's
                            # identity hash or token fingerprint, nor another host's name: this
                            # record is committed to the public repo (`public_sources`).
                            "sources": public_sources(per_source)},
            "cloud_sessions": {"log": str(account.cloud_dir / "sessions.tsv") if account.cloud_dir else None,
                               "spans": [c.record() for c in cloud[name]],
                               "launches_unattributed": unattributed},
            "weekly_steps": steps,
        }
    out = summarise(stretches, reset_sources, meta, weekly, probe_rows, prices, now, until, unclaimed, cloud)
    out["sources_version"] = SOURCES_VERSION
    # Shipped extracts that joined no account here: never dropped silently. The public record
    # carries only counts; the host and identity of each go to stderr, which lands in the
    # host's private cron log, never in this repo.
    unjoined = [(host, ident, len(transcript_paths(r, None)))
                for host, ident, r in resolved.remotes if (host, ident) not in joined_remote]
    out["remote_unjoined"] = {"extracts": len(unjoined), "files": sum(n for _, _, n in unjoined)}
    for host, ident, n in unjoined:
        print(f"gs_passive: shipped extracts from {host} under {ident} ({n} files) joined no account",
              file=sys.stderr)
    return out


def public_sources(per_source: list[dict]) -> list[dict]:
    """`account_turns`' per-source counts as the public record may carry them.

    A shipped source's label is `remote:<host>` and its root `<dir>/<host>/<identity>/`, so
    every other host's shipment is summed into one `remote` entry with a host count and no
    root. Live and archive sources are kept as they are: their roots are this host's config
    and cache directories.
    """
    out = [dict(s) for s in per_source if not str(s.get("source", "")).startswith("remote:")]
    remote = [s for s in per_source if str(s.get("source", "")).startswith("remote:")]
    if remote:
        out.append({"source": "remote", "hosts": len(remote), "files": sum(s["files"] for s in remote),
                    "turns": sum(s["turns"] for s in remote)})
    return out


def summarise(stretches: dict[str, list[Stretch]], reset_sources: dict[str, dict[datetime, str]], meta: dict,
              weekly: dict, probe_rows: Iterable[dict], prices: dict, now: datetime,
              until: datetime | None = None, unclaimed: dict[str, dict[datetime, dict]] | None = None,
              cloud: dict[str, list[CloudSpan]] | None = None) -> dict:
    """The capture check and everything read off it, from built stretches: `report`'s second half.

    Split out so a stretch file can be re-judged from its own records after they are
    corrected (tools/fast_session_restore.py), by the same code that wrote it.
    `reset_sources` is each account's `_reset_source` per stretch, keyed by its start.
    `unclaimed` is, for an account whose pooled-projects filter ran, the unclaimed work in
    each stretch keyed by its start; its records carry it as `unclaimed_tokens`. `cloud` is
    each account's recorded cloud-session spans (tracker/cloud_sessions.py).
    """
    checked = check(stretches, probe_readings(list(probe_rows), prices))
    out_accounts = {}
    for name in stretches:
        vs, rs = checked.verdicts[name], checked.runs[name]
        daily = _daily(vs)
        accepted = [v for v in vs if publishable(v)]
        out_accounts[name] = {
            "account": name, **meta[name],
            "stretches": [_stretch_record(v, reset_sources[name][v.stretch.start],
                                          (unclaimed or {}).get(name), (cloud or {}).get(name)) for v in vs],
            "runs": [_run_record(r) for r in rs],
            "daily": daily,
            "split": _split([v.stretch for v in accepted]),
            "last_usable_at": accepted[-1].stretch.end.isoformat() if accepted else None,
            "state": _state(vs, rs),
            "spread": {"stretch": spread([v.stretch.usd_per_pct for v in accepted]),
                       "daily": spread([d["usd_per_pct"] for d in daily])},
            "weekly_by_window": weekly[name],
        }
    return {"generated_at": now.isoformat(), "until": until.isoformat() if until else None,
            "capture_gate": CAPTURE_GATE, "model_normalization": {"version": 1,
             "aliases": {"claude-fable-5": "claude-fable-5-1"},
             "rule": "strip date suffix and [1m]; retain every other raw model id"},
            "accounts": out_accounts, "changes": checked.changes}


def _utc_day(stamp: str) -> str:
    at = datetime.fromisoformat(stamp)
    return (at.astimezone(timezone.utc) if at.tzinfo else at).date().isoformat()


def passive_dollar_readings(report: dict, prices: dict, by: str = "day",
                            allow_legacy_unverified: bool = False) -> list[tuple[datetime, float]]:
    """Accepted passive readings as (time, meter dollars per full window), revalued at `prices`.

    The publisher's hook: the same element shape as build_public_json's
    `dollar_readings` (usd_per_pct x 100 per probe row). Only stretches whose
    `status` is accepted are read: every priced one with CAPTURE_GATE off,
    only what the capture check accepted with it on. A stretch whose meter log
    carried no reset ids (`reset_verified` false, or absent in a report from
    before the field existed) is read only with `allow_legacy_unverified`: it can
    miss a reset between two readings (audit finding 10), so the publisher keeps
    it out of the measured series.
    `by="day"` pools each account's accepted stretches per UTC day (the
    precision the issue is after); `by="stretch"` returns them one by one.
    """
    out = []
    for account in report.get("accounts", {}).values():
        groups: dict[str, list[dict]] = {}
        for s in account["stretches"]:
            if s["status"] != ACCEPTED:
                continue
            # Archived ceiling-log stretches lack reset ids.  They remain
            # available as explicitly conditional references, but never enter
            # the certified series or its change detector.
            if s.get("reset_verified") is not True and not allow_legacy_unverified:
                continue
            if s.get("unpriced_tokens", 0) > 0 or not s.get("tokens"):
                # No captured work at all is a collection gap, not a free window.
                continue
            # A stretch with a model these prices do not cover is left out whole:
            # valuing that model at $0 would deflate the reading and look like a
            # cheaper meter, and the stretch's percent is spent on it all the same.
            valued = {m: bundle_meter_usd(m, tok, prices) for m, tok in s["tokens"].items()}
            if any(v is None for v in valued.values()):
                continue
            # Per UTC day, as the docstring says: a +02:00 stretch's own date is not its UTC date.
            key = _utc_day(s["end"]) if by == "day" else s["end"]
            groups.setdefault(key, []).append((s, sum(valued.values())))
        for ss in groups.values():
            usd = sum(v for _, v in ss)
            out.append((datetime.fromisoformat(ss[-1][0]["end"]), usd / sum(s["delta_pct"] for s, _ in ss) * 100))
    return sorted(out)


#: How a stretch's credit value was priced, best first. A stretch takes the worst
#: source any of its models needed, so one reference-table family marks the whole
#: reading. `anchor` leads because Opus's rate is definitional rather than a fallback:
#: it is what a credit means here, nothing in this repository measures it, and every
#: measured rate is expressed against it. A stretch that reads `measured` therefore had
#: at least one family whose rate was fitted; one that reads `anchor` was pure Opus.
CREDIT_RATE_SOURCES = ("anchor", "measured", "interval_midpoint", "reference_table",
                       "inferred_list_price")


def _empty(tok) -> bool:
    """A token bundle that holds no tokens (Claude Code's `<synthetic>` placeholder rows)."""
    return isinstance(tok, dict) and not credit_model.raw_tokens(tok)


def stretch_credits(tokens: dict, credits: dict, model_rates: dict | None,
                    prices: dict | None = None) -> tuple[float | None, str]:
    """A token bundle's value in meter credits, and which rate source it needed.

    The meter does not charge list-price ratios. It charges a small rational rate
    per token per family, measured in history/model-rates.json: Sonnet at about
    0.78x Opus where the list price implies 0.4x. Valuing a stretch in credits
    therefore takes the model mix out of the reading, which is the whole point --
    in list dollars the same meter movement reads dearer or cheaper purely by which
    model did the work (tracker/detect.py, "Credit stretches").

    Each family's rate is taken in the order CREDIT_RATE_SOURCES names, falling back
    the way tracker/credits.py already falls back rather than inventing a step:

    - `family_rate` with a value: the measured rate ("measured"), or Opus's
      definitional one ("anchor").
    - `family_rate` with an interval and no single value -- Fable, whose input rate
      the data bound only loosely -- the midpoint of the interval
      ("interval_midpoint"). Dropping those stretches instead would throw away most
      of both accounts' evidence; pricing them at the midpoint is the same choice
      `fable_interval` publishes, with the reading's source recorded.
    - no measurement at all: the January reference table's rate ("reference_table").
    - no measurement and no reference row -- Opus 5.5, and any family of its own
      (`credit_model.auto_family`) such as a future Sonnet 5.5 -- its API list-price ratio
      to the Opus anchor from data/prices.json ("inferred_list_price"). A rate the fit
      publishes as `provisional` is skipped for this one too. Every Opus 5.5 stretch comes
      after the 22 September five-hour change, so a rate fitted from them alone absorbs
      that change: valuing them at it would cancel the very step detection looks for. The
      list ratio is fixed in advance, and is replaced by the measured rate as soon as the
      fit's interval passes the published-rate rule.
    - no rate anywhere, or a model in no family: `(None, "unpriced")`, and the caller
      drops the stretch whole, exactly as an unpriced model already does in dollars.

    Cache writes join the input side at the plain input rate and cache reads at the
    table's cache-read weight, which is `credits.price_tokens`' rule; this does the
    same arithmetic per family so it can report the source and use the measured rate
    rather than the reference one.
    """
    weight = credit_model.cache_read_weight(credits)
    total, worst = 0.0, CREDIT_RATE_SOURCES[0]
    for model, tok in tokens.items():
        # A bundle of no tokens is nothing to value: Claude Code's `<synthetic>` placeholder
        # rows carry the model id `<synthetic>` and zero tokens, belong to no family, and
        # would otherwise drop the whole stretch as unpriced. One that ever carries tokens
        # is not skipped, and lands in `unpriced_credit_models`.
        if not isinstance(tok, dict) or not credit_model.raw_tokens(tok):
            continue
        fam = credit_model.family(model, credits)
        if fam is None:
            return None, "unpriced"
        rate = credit_model.family_rate(fam, credits, model_rates)
        provisional = bool(((model_rates or {}).get("per_family", {}).get(fam) or {}).get("provisional"))
        if rate.rate_source == "inferred" or provisional:
            # An inferred rate is shown on the page, and a provisional one published, but
            # neither values a stretch: the credit series must not move because a family was
            # shown before it was measured. Falls through to the reference table, then to
            # the list-price ratio below.
            rate = replace(rate, input=None, output=None, input_interval=None, output_interval=None)
        if rate.input is not None and rate.output is not None:
            rate_in, rate_out = rate.input, rate.output
            source = "anchor" if rate.anchor else "measured"
        elif rate.input_interval and rate.output_interval:
            rate_in = sum(rate.input_interval) / 2
            rate_out = sum(rate.output_interval) / 2
            source = "interval_midpoint"
        elif (pair := credit_model.rates(fam, credits)) is not None:
            rate_in, rate_out = pair
            source = "reference_table"
        else:
            ratio = credit_model.list_price_ratio(fam, credits, prices)
            anchor = credit_model.rates("opus", credits)
            if ratio is None or anchor is None:
                return None, "unpriced"
            rate_in, rate_out = ratio * anchor[0], ratio * anchor[1]
            source = "inferred_list_price"
        if CREDIT_RATE_SOURCES.index(source) > CREDIT_RATE_SOURCES.index(worst):
            worst = source
        total += credit_model.input_side(tok, weight) * rate_in + tok.get("output", 0) * rate_out
    return total, worst


def passive_credit_points(report: dict, prices: dict, credits: dict,
                          model_rates: dict | None = None,
                          allow_legacy_unverified: bool = False) -> list[tuple]:
    """Accepted passive stretches as (end, credits, delta_pct, pieces) points, one per stretch.

    The credit-valued twin of `passive_dollar_readings`, and the series change detection
    ran on until 2026-10-23 (`tracker.detect.detect_credit_changes`, retired for
    tracker/four_accounts.py). Two differences,
    both deliberate:

    - No day pooling. A day is not a unit of evidence -- the meter is -- so each
      stretch is its own point and carries its own `delta_pct` as its weight. Pooling
      to one median per UTC day gave a 10% day and a 60% day the same vote and left
      jwork with 15 points and dave with 4.
    - Credits, not list dollars, so the reading does not move with the model mix.

    Eligibility is unchanged from `passive_dollar_readings`, deliberately: `status`
    accepted, `reset_verified` unless `allow_legacy_unverified`, no unpriced tokens,
    some tokens, and every model priceable in `prices`. The dollar gate stays in place
    so that widening it stays one decision in one place (agent A's price table), and a
    stretch this cannot price in credits at all is dropped the same way.

    `pieces` is the stretch's own `windows` count, which is what it concedes of
    `delta_pct` to whole-percent rounding. Consecutive stretches are not telescoped
    the way `_daily` telescopes them: the detector reorders and resplits the series,
    so which stretches end up pooled together is not known here. That over-concedes
    at most one point per join, which widens intervals and never narrows them.
    """
    out = []
    for account in report.get("accounts", {}).values():
        for s in account["stretches"]:
            if s["status"] != ACCEPTED:
                continue
            if s.get("reset_verified") is not True and not allow_legacy_unverified:
                continue
            if s.get("unpriced_tokens", 0) > 0 or not s.get("tokens"):
                continue
            if any(bundle_meter_usd(m, tok, prices) is None for m, tok in s["tokens"].items()
                   if not _empty(tok)):
                continue
            value, _source = stretch_credits(s["tokens"], credits, model_rates, prices)
            if value is None or not s.get("delta_pct"):
                continue
            if value <= 0:
                # Priced, but nothing charged: a stretch whose only bundles are empty
                # `<synthetic>` ones. The meter moved and no work explains it, so it is no
                # reading of credits per percent, and a zero here would pull the level down.
                continue
            out.append((datetime.fromisoformat(s["end"]), value, float(s["delta_pct"]),
                        int(s.get("windows") or 1)))
    return sorted(out, key=lambda p: p[0])


def unpriced_credit_models(report: dict, prices: dict, credits: dict,
                           model_rates: dict | None = None) -> list[str]:
    """Every model id in a stretch that neither the credit rates nor the list prices can value.

    Such a stretch is dropped from change detection whole, the same as before this list
    existed; the list is what keeps the drop visible. A model that joins with no row in
    data/prices.json -- a release the price table has not caught up with -- lands here until
    its list price is added, instead of disappearing from the series without a word.
    """
    out = set()
    for account in report.get("accounts", {}).values():
        for s in account.get("stretches") or []:
            for model, tok in (s.get("tokens") or {}).items():
                # A bundle of no tokens hides no work (Claude Code's `<synthetic>` rows).
                if not isinstance(tok, dict) or model in out or not credit_model.raw_tokens(tok):
                    continue
                if (bundle_meter_usd(model, tok, prices) is None
                        or stretch_credits({model: tok}, credits, model_rates, prices)[0] is None):
                    out.add(model)
    return sorted(out)


def credit_rate_sources(report: dict, prices: dict, credits: dict,
                        model_rates: dict | None = None,
                        allow_legacy_unverified: bool = False) -> dict[str, int]:
    """How many of the points `passive_credit_points` returns needed each rate source."""
    counts: dict[str, int] = {}
    for account in report.get("accounts", {}).values():
        for s in account["stretches"]:
            if s["status"] != ACCEPTED or not s.get("tokens") or not s.get("delta_pct"):
                continue
            if s.get("reset_verified") is not True and not allow_legacy_unverified:
                continue
            if s.get("unpriced_tokens", 0) > 0:
                continue
            if any(bundle_meter_usd(m, tok, prices) is None for m, tok in s["tokens"].items()
                   if not _empty(tok)):
                continue
            value, source = stretch_credits(s["tokens"], credits, model_rates, prices)
            # The same admission rule as passive_credit_points: priced and charged.
            if value is not None and value > 0:
                counts[source] = counts.get(source, 0) + 1
    return counts


def calibrate(rpt: dict, probe_rows: list[dict], prices: dict, since: datetime, until: datetime) -> dict:
    """The passive/probe dollar-per-window ratio over [since, until] (issue #39 follow-up).

    Jono Work's accepted passive days and the probe read the same meter on
    different scales -- $1.26-$1.81 per window passive against $0.97 probed,
    on the real 2026-09-05..15 gs data -- so joining them raw invents change
    events the live page does not have. `ratio` = median(accepted passive
    daily readings in the window) / median(usable probe rows' dollars per
    window in the same window); tracker/publish.py divides a passive reading
    by this ratio before joining it to the probe series. `None` when either
    side has no readings in the window, so the caller can refuse to publish
    a ratio computed from nothing.

    Probe dollars-per-window is restated via `bundle_meter_usd` rather than
    importing tracker/publish.py's `usd_per_pct` (same formula: meter dollars
    per tick times 100, see bundle_meter_usd's own docstring) -- publish.py
    imports this module for `passive_dollar_readings`, so importing back
    would be circular.
    """
    passive_vals = [v for t, v in passive_dollar_readings(rpt, prices, by="day") if since <= t <= until]
    probe_vals = []
    for r in usable_rows(probe_rows):
        ts = datetime.fromisoformat(r["ts"])
        if not (since <= ts <= until):
            continue
        ticks = r.get("tick_to", 0) - r.get("tick_from", 0)
        usd = bundle_meter_usd(r["model"], r["tokens"], prices) if ticks > 0 else None
        if usd is not None:
            probe_vals.append(usd / ticks * 100)
    ratio = median(passive_vals) / median(probe_vals) if passive_vals and probe_vals else None
    return {"ratio": _r(ratio, 6), "window": [since.date().isoformat(), until.date().isoformat()],
            "passive_n": len(passive_vals), "probe_n": len(probe_vals)}


def fit_output_weight(rpt: dict, prices: dict) -> dict:
    """Least-squares fit of class_weight.output from the passive stretches themselves.

    Each published stretch says: meter movement (delta_pct) = a x (list dollars of
    input + cache_write) + b x (list dollars of output), with cache_read at its
    measured weight of 0.0. The output weight relative to cache_write is b / a.
    Two unknowns, so the 2x2 normal equations are solved directly; no numpy.
    The 1.8 hand-entered on 2026-09-06 came from one probe pair; this reads it
    from every stretch the capture check accepted instead. `--fit-output-weight` prints it; the
    value is pasted into data/prices.json by hand, as the calibration ratio
    was meant to be, so it stays fixed while real movement in the series shows.
    """
    rows = []
    for account in rpt.get("accounts", {}).values():
        for st in account["stretches"]:
            # The capture check's own accepted set, not everything published: a
            # stretch with part of its use off gs has the wrong left-hand side.
            if st.get("capture_status", st["status"]) != ACCEPTED:
                continue
            base = out = 0.0
            for m, tok in st["tokens"].items():
                price = prices.get(m)
                if price is None:
                    break
                base += (tok.get("input", 0) * price["input"] + tok.get("cache_write", 0) * price["cache_write"]) / 1e6
                out += tok.get("output", 0) * price["output"] / 1e6
            else:
                rows.append((base, out, st["delta_pct"]))
    if len(rows) < 2:
        return {"n": len(rows), "output_weight": None}
    sxx = sum(b * b for b, _, _ in rows)
    sxy = sum(b * o for b, o, _ in rows)
    syy = sum(o * o for _, o, _ in rows)
    sxz = sum(b * d for b, _, d in rows)
    syz = sum(o * d for _, o, d in rows)
    det = sxx * syy - sxy * sxy
    if not det:
        return {"n": len(rows), "output_weight": None}
    a = (sxz * syy - syz * sxy) / det
    b = (sxx * syz - sxy * sxz) / det
    fitted = [a * base + b * out for base, out, _ in rows]
    resid = [d - f for (_, _, d), f in zip(rows, fitted)]
    rms = (sum(r * r for r in resid) / len(rows)) ** 0.5
    return {"n": len(rows), "output_weight": _r(b / a) if a else None,
            "usd_per_pct_at_fit": _r(1 / a) if a else None, "residual_rms_pct": _r(rms, 2),
            "output_share_of_list_value": _r(sum(o for _, o, _ in rows) / sum(b + o for b, o, _ in rows))}


def _utc_arg(value: str, *, end_of_day: bool = False) -> datetime:
    """An ISO date or date-time from the command line as an aware UTC datetime.

    Meter samples and transcript turns are aware, so a naive argument would raise
    on comparison. A bare date means the whole day: its start for --since, its
    last second for --until, so `--since 2026-09-05 --until 2026-09-15` spans
    both days inclusive."""
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if end_of_day and len(value) == 10:
        t = t.replace(hour=23, minute=59, second=59)
    return t


def _utc_until(value: str) -> datetime:
    return _utc_arg(value, end_of_day=True)


def _summary(name: str, a: dict) -> str:
    statuses: dict[str, int] = {}
    for s in a["stretches"]:
        statuses[s["status"]] = statuses.get(s["status"], 0) + 1
    sp = a["spread"]["daily"]
    return (f"{name}: {a['meter']['samples']} meter samples, {a['transcripts']['turns']} turns, "
            f"stretches {statuses or 0}, runs {[r['kind'] for r in a['runs']]}, state {a['state']['state']}, "
            f"daily median ${sp.get('median')}/1% cv {sp.get('cv')} (n={sp['n']})")


def transcript_loss(previous: dict | None, current: dict) -> list[str]:
    """One line per account whose transcripts fell sharply since `previous` (LOSS_FRACTION).

    Empty when there is nothing comparable: no previous record, or one counted another way
    (`sources_version` differs, as the first run after a deliberate recount does). Each line
    names the account, the fall, and every source whose files or turns fell, so the reader
    sees whether a live tree, an archive or a host's shipment went missing.
    """
    if not isinstance(previous, dict) or previous.get("sources_version") != current.get("sources_version"):
        return []
    out = []
    for name, acct in current.get("accounts", {}).items():
        before = ((previous.get("accounts") or {}).get(name) or {}).get("transcripts") or {}
        now = acct.get("transcripts") or {}
        was, has = before.get("turns"), now.get("turns")
        if not isinstance(was, int) or not isinstance(has, int):
            continue
        if was - has < max(LOSS_MIN_TURNS, LOSS_FRACTION * was):
            continue
        prev_src = {s.get("source"): s for s in before.get("sources") or [] if isinstance(s, dict)}
        cur_src = {s.get("source"): s for s in now.get("sources") or [] if isinstance(s, dict)}
        fell = [f"{label} {s.get('files')}->{(cur_src.get(label) or {}).get('files', 0)} files, "
                f"{s.get('turns')}->{(cur_src.get(label) or {}).get('turns', 0)} turns"
                for label, s in prev_src.items()
                if (cur_src.get(label) or {}).get("turns", 0) < (s.get("turns") or 0)]
        out.append(f"{LOSS_MARKER} gs_passive: {name} transcript turns fell {was} -> {has} "
                   f"({(was - has) / was:.0%}) since {previous.get('generated_at')}"
                   + (f"; {'; '.join(fell)}" if fell else ""))
    return out


def _speed_history(out: Path, accounts: dict[str, Account], home: Path, refresh: bool,
                   sources: Sources | None = None) -> dict | None:
    """The gs accounts' model speed rows (tracker/speed.py): what `out` already holds under
    `speed`, with the recent days recomputed from the transcripts when `refresh`. Only the
    full gs run refreshes, so a replay or a one-account run carries the stored history
    through untouched. A failure keeps the stored rows: the speed rows must never cost the
    day's passive join."""
    from . import speed
    from .publish import ACCOUNT_LABELS
    stored = (speed.load_history(out) or {}).get("speed")
    if not refresh:
        return stored
    now = datetime.now(timezone.utc)
    try:
        since_day, mtime_since = speed.recompute_from(stored, now)
        labels = dict(ACCOUNT_LABELS)
        resolved = resolve_sources(home, sources)
        rows = speed.scan_accounts([(labels[name], _split_sources(acct, mtime_since, (), home, resolved).kept)
                                    for name, acct in accounts.items()], since_day)
        return speed.update(stored, rows, since_day or date.min.isoformat(), now)
    except (OSError, ValueError, KeyError) as e:
        print(f"warning: speed rows not updated, keeping the stored ones: {e}", file=sys.stderr)
        return stored


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Passive join per account; the capture check is advisory (CAPTURE_GATE)")
    ap.add_argument("--home", type=Path, default=Path.home())
    ap.add_argument("--masterrig", action="store_true",
                    help="join masterrig's own account (moonlighter + ceiling logs against ~/.claude/projects) "
                         "instead of the gs accounts; its meter also counts web, phone and other machines, so read "
                         "each stretch's capture before using a rate")
    ap.add_argument("--account", action="append", help="limit to these accounts (default: every gs account)")
    ap.add_argument("--prices", type=Path, default=Path("data/prices.json"))
    ap.add_argument("--probes", type=Path, default=Path("history/probes.jsonl"))
    ap.add_argument("--until", type=_utc_until, help="replay as of this time (a bare date means its last second)")
    ap.add_argument("--withhold", action="append", default=[], metavar="ACCOUNT:GLOB",
                    help="leave out that account's transcripts matching GLOB under projects/")
    ap.add_argument("--calibrate", action="store_true",
                    help="print the passive/probe dollar-per-window ratio for --since..--until as JSON "
                         "and exit; never writes --out or prices.json (Jonathan pastes the ratio in by hand)")
    ap.add_argument("--since", type=_utc_arg, help="calibration window start (with --calibrate); a bare date means its first second")
    ap.add_argument("--fit-output-weight", action="store_true",
                    help="print the least-squares class_weight.output over the published stretches as JSON and exit")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--accept-loss", action="store_true",
                    help=f"write --out even when an account's transcript turns fell sharply ({LOSS_MARKER}); "
                         "for a loss a person has looked at and accepted")
    ap.add_argument("--archive-root", type=Path,
                    help=f"transcript archives, <dir>/{extracts.ARCHIVE_GLOB}/<subtree>/*.tar.zst "
                         f"(default ~/{extracts.ARCHIVE_ROOT})")
    ap.add_argument("--archive-cache", type=Path, help=f"their text-free cache (default ~/{extracts.ARCHIVE_CACHE})")
    ap.add_argument("--remote-root", type=Path,
                    help=f"other hosts' shipped extracts, <dir>/<host>/<identity>/ (default ~/{extracts.REMOTE_ROOT})")
    a = ap.parse_args(argv)
    if a.calibrate and (a.since is None or a.until is None):
        ap.error("--calibrate requires --since and --until")
    if a.out and not (a.calibrate or a.fit_output_weight):
        from .speed import unreadable_record
        if (refusal := unreadable_record(a.out)) is not None:
            print(refusal, file=sys.stderr)
            return 2
    accounts = {"masterrig": masterrig_account(a.home)} if a.masterrig else gs_accounts(a.home)
    if a.account:
        unknown = set(a.account) - set(accounts)
        if unknown:
            ap.error(f"unknown account(s): {', '.join(sorted(unknown))}")
        accounts = {k: v for k, v in accounts.items() if k in a.account}
    withhold: dict[str, list[str]] = {}
    for item in a.withhold:
        name, _, pattern = item.partition(":")
        withhold.setdefault(name, []).append(pattern)
    prices = {k: v for k, v in json.loads(a.prices.read_text()).items() if not k.startswith("_")}
    rows = ([json.loads(line) for line in a.probes.read_text(encoding="utf-8").splitlines() if line.strip()]
            if a.probes.exists() else [])
    defaults = default_sources(a.home)
    sources = Sources(a.archive_root or defaults.archive_root, a.archive_cache or defaults.archive_cache,
                      a.remote_root or defaults.remote_root)
    r = report(accounts, prices, rows, withhold=withhold, until=a.until, home=a.home, sources=sources)
    if a.calibrate:
        print(json.dumps(calibrate(r, rows, prices, a.since, a.until)))
        return 0
    if a.fit_output_weight:
        print(json.dumps(fit_output_weight(r, prices)))
        return 0
    for name, acct in r["accounts"].items():
        print(_summary(name, acct))
    for c in r["changes"]:
        print(f"change: {c['account']} at {c['at']}, step {c['step']:+.1%}, corroborated by {', '.join(c['corroborated_by'])}")
    if a.out and not (a.account or a.until or a.withhold):
        # Only a full run is comparable with the record it would replace.
        try:
            previous = json.loads(a.out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
        if (lost := transcript_loss(previous, r)):
            for line in lost:
                print(line, file=sys.stderr)
            if not a.accept_loss:
                print(f"error: not writing {a.out}: transcripts went missing, and rewriting the record from "
                      "what is left would rewrite its history. Restore them, or rerun with --accept-loss once "
                      "the loss is understood (docs/OPERATIONS.md).", file=sys.stderr)
                return LOSS_EXIT
            print(f"warning: --accept-loss: writing {a.out} without the lost transcripts", file=sys.stderr)
    if a.out:
        speed_rows = _speed_history(a.out, accounts, a.home,
                                    refresh=not (a.masterrig or a.account or a.until or a.withhold), sources=sources)
        if speed_rows is not None:
            r["speed"] = speed_rows
    if a.out:
        write_text_atomic(a.out, json.dumps(r, indent=1) + "\n")
        print(f"wrote {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
