"""This profile's AgentChat key, in a file only this plugin reads.

Why not the profile ``.env``
────────────────────────────
Up to 0.5.3 the key lived in the profile's ``.env`` as ``AGENTCHAT_API_KEY``.
Hermes loads that file into its process environment, and from there the key
reached places it never should (found testing on a real Hermes box, 2026-11-17):

* the environment of every command a reply turn runs — a turn started by a
  stranger's message curl'd attachments with ``$AGENTCHAT_API_KEY``, wrote a
  script reading ``os.environ["AGENTCHAT_API_KEY"]``, and Hermes' terminal
  env snapshots (``cache/scratch/hermes-snap-*.sh``) kept ``declare -x
  AGENTCHAT_API_KEY=…``;
* every new profile: ``hermes profile create`` copies the launch profile's
  ``.env`` (and ``--clone`` copies it with ``config.yaml``), so the new
  profile silently became the same AgentChat agent.

The key is the agent's password on AgentChat. It now lives in
``<profile home>/agentchat/credentials.json``: one file per profile, mode
``0600`` in a ``0700`` directory (on Windows, a directory whose access list
names only the user, SYSTEM and Administrators — :mod:`.owner_only`),
written atomically, read only by this plugin — the socket, the CLI, and the secret source that hands it to Hermes'
MCP client for the hosted tools (:mod:`.key_source`). Hermes copies no part
of it on ``profile create`` / ``--clone``.

Bound to its profile
────────────────────
The file records the profile home it was saved for. ``--clone-all`` (and a
hand copy) copies every file, this one included; a copy is told apart from a
move by the profile it names:

* it names this profile → ``ok``;
* it names another profile that still holds the same key → ``copied``: the
  key is that profile's agent, and this profile must not act as it;
* it names a profile that no longer holds it (renamed, moved, restored to
  another path) → ``moved``: usable, and the next write records this home.

Honest limit: an agent with a shell running as the same user can still read
this file if it deliberately looks for it. What this removes is the key's
accidental spread: in the environment of every command, in snapshots, in
copies of ``.env``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import owner_only
from .state import remove_file, replace_file

logger = logging.getLogger(__name__)

STORE_DIRNAME = "agentchat"
STORE_FILENAME = "credentials.json"
STORE_VERSION = 1

# POSIX file modes and owners mean something; on Windows they do not (every
# file reads 0o666 whatever was asked): there the access list says who may
# use the file (:mod:`.owner_only`, which also holds the two modes). A
# constant rather than a platform check inline, so the type checker on each
# platform sees both branches.
_POSIX = os.name != "nt"

StoreStatus = Literal["missing", "ok", "moved", "copied", "unsafe", "unreadable"]


class KeyStoreError(RuntimeError):
    """The key could not be saved (and was not)."""


@dataclass(frozen=True)
class StoredKey:
    api_key: str
    handle: str
    api_base: str
    # The profile home the key was saved for (as written).
    profile_home: str


@dataclass(frozen=True)
class StoreRead:
    """One read of a profile's key file. Never carries an error the caller has to catch."""

    status: StoreStatus
    path: Path
    key: StoredKey | None = None
    detail: str = ""
    # ``copied`` / ``moved``: the profile home the file names.
    origin_home: Path | None = None
    # Readable by the group or others (POSIX; on Windows by any account but the
    # user, SYSTEM and Administrators, or not granted to the user itself):
    # still used, tightened at activation and by ``doctor --fix``.
    loose_mode: bool = False
    # ``unsafe`` only because other accounts can change it, while it is yours
    # (Windows: a file an earlier release wrote in a home whose list lets them,
    # e.g. ``C:\hermes`` inheriting Authenticated Users: Modify): ``doctor
    # --fix`` makes it yours only and keeps the key (:func:`make_yours`).
    repairable: bool = False

    @property
    def usable(self) -> bool:
        """The key may be used for this profile."""
        return self.status in ("ok", "moved") and self.key is not None


def store_dir(home: Path) -> Path:
    return Path(home) / STORE_DIRNAME


def store_path(home: Path) -> Path:
    return store_dir(home) / STORE_FILENAME


def fingerprint(api_key: str) -> str:
    """Short, non-reversible tag of a key, for comparing without holding it."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def same_home(a: Path, b: Path) -> bool:
    """Do two spellings name the same profile home?"""
    try:
        return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(
            os.path.expanduser(str(b))
        )
    except (OSError, ValueError):
        return str(a) == str(b)


# ─── reading ───────────────────────────────────────────────────────────────


def read_store(home: Path) -> StoreRead:
    """Read ``home``'s key file. Never raises."""
    path = store_path(home)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return StoreRead("missing", path)
    except OSError as exc:
        return StoreRead("unreadable", path, detail=f"cannot read {path}: {exc.strerror or exc}")
    judged = _judge(path, st, home)
    if judged.problem is not None:
        return StoreRead("unsafe", path, detail=judged.problem, repairable=judged.repairable)
    record = _parse(path)
    if isinstance(record, str):
        return StoreRead("unreadable", path, detail=record)
    loose = judged.loose
    recorded = Path(record.profile_home) if record.profile_home else None
    if recorded is None or same_home(recorded, home):
        return StoreRead("ok", path, key=record, loose_mode=loose)
    if _holds_same_key(recorded, record.api_key):
        return StoreRead(
            "copied",
            path,
            key=record,
            origin_home=recorded,
            loose_mode=loose,
            detail=f"{path} was saved for the profile at {recorded}, which still holds the same key",
        )
    return StoreRead("moved", path, key=record, origin_home=recorded, loose_mode=loose)


@dataclass(frozen=True)
class _Judged:
    # Why the file must not be trusted as this user's own; ``None`` when it may.
    problem: str | None
    # Others may read it (or, Windows, the user has no entry of its own).
    loose: bool = False
    # The problem is only that others can change a file that is yours.
    repairable: bool = False


def _judge(path: Path, st: os.stat_result, home: Path) -> _Judged:
    """Whether the key file at ``path`` (in profile ``home``) is this user's own.

    A symlink, a non-regular file, another owner, or a file the group or
    others can write: any of them lets someone else choose which agent this
    profile is. On Windows the same, read from the access list, and also a
    file with more than one name (a hard link), a path that resolves outside
    the profile home (a junction on the way) or a list that cannot be read:
    the check fails closed. Only a volume that stores no access lists (FAT,
    exFAT) is judged by its contents alone, as a POSIX file system without
    modes would be.
    """
    if stat.S_ISLNK(st.st_mode):
        return _Judged(f"{path} is a symbolic link; the key file must be a regular file")
    if not stat.S_ISREG(st.st_mode):
        return _Judged(f"{path} is not a regular file")
    if not _POSIX:
        return _judge_windows(path, home)
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and st.st_uid != getuid():
        return _Judged(f"{path} is owned by uid {st.st_uid}, not by you (uid {getuid()})")
    if st.st_mode & 0o022:
        return _Judged(f"{path} can be written by its group or by others (mode {stat.S_IMODE(st.st_mode):o})")
    return _Judged(None, loose=bool(st.st_mode & 0o044))


def _judge_windows(path: Path, home: Path) -> _Judged:
    try:
        access = owner_only.inspect(path, root=home)
    except owner_only.OwnerOnlyError as exc:
        return _Judged(str(exc.strerror or exc))
    except OSError as exc:
        return _Judged(f"{path}: its access list cannot be read ({exc.strerror or exc}), so it is not trusted")
    if access is None:
        return _Judged(None)  # a volume without access lists
    if not access.owner_trusted:
        return _Judged(f"{path} is owned by {owner_only.describe_sid(access.owner)}, not by you ({access.user})")
    if access.null_dacl:
        return _Judged(f"{path} has no access list: every account on this computer can change it", repairable=True)
    if access.others_write:
        names = ", ".join(owner_only.describe_sid(sid) for sid in access.others_write)
        return _Judged(f"{path} can be changed by other accounts on this computer ({names})", repairable=True)
    return _Judged(None, loose=not access.private)


def _parse(path: Path) -> StoredKey | str:
    try:
        # utf-8-sig: a byte-order mark from a Windows editor is not a broken file.
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return f"cannot read {path}: {exc.strerror or exc}"
    try:
        data = json.loads(raw)
    except ValueError:
        return f"{path} is not valid JSON"
    if not isinstance(data, dict):
        return f"{path} does not hold a key record"
    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        return f"{path} holds no api_key"
    handle = data.get("handle")
    api_base = data.get("api_base")
    profile_home = data.get("profile_home")
    return StoredKey(
        api_key=api_key.strip(),
        handle=handle.strip().lstrip("@") if isinstance(handle, str) else "",
        api_base=api_base.strip().rstrip("/") if isinstance(api_base, str) else "",
        profile_home=profile_home if isinstance(profile_home, str) else "",
    )


def _holds_same_key(home: Path, api_key: str) -> bool:
    """Does the key file at ``home`` (followed as written) hold ``api_key``?"""
    path = store_path(home)
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if _judge(path, st, home).problem is not None:
        return False
    record = _parse(path)
    return isinstance(record, StoredKey) and record.api_key == api_key


# ─── writing ───────────────────────────────────────────────────────────────


def write_store(home: Path, *, api_key: str, handle: str, api_base: str) -> Path:
    """Save the key for ``home``; return the file's path.

    The directory is ``0700`` and the file ``0600`` from its first byte (it
    is created with that mode, never chmod'ed after the key is in it); on
    Windows the directory gets the owner-only access list and the file
    inherits it as it is created. The write goes to a temporary file in the
    same directory, is flushed to disk, then renamed over the old one (on
    Windows retried for a moment while another process has the file open),
    so a crash leaves the old file or the new one, never half of either. The
    file is read back before this returns.

    Raises:
        KeyStoreError: the key was not saved; whatever file was there is
            unchanged.
    """
    api_key = api_key.strip()
    if not api_key:
        raise KeyStoreError("no key to save")
    directory = store_dir(home)
    path = store_path(home)
    try:
        owner_only.make_private_dir(directory, root=Path(home), enforce=True, strict=True)
    except OSError as exc:
        raise KeyStoreError(f"cannot create {directory}: {exc.strerror or exc}") from exc
    record = {
        "version": STORE_VERSION,
        "api_key": api_key,
        "handle": handle.strip().lstrip("@"),
        "api_base": api_base.strip().rstrip("/"),
        "profile_home": str(Path(home).expanduser()),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    tmp = directory / f".{STORE_FILENAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(tmp, flags, owner_only.FILE_MODE)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        replace_file(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise KeyStoreError(f"cannot write {path}: {exc.strerror or exc}") from exc
    _fsync_dir(directory)
    check = read_store(home)
    if not check.usable or check.key is None or check.key.api_key != api_key:
        raise KeyStoreError(f"{path} did not read back as written ({check.status})")
    return path


def _fsync_dir(directory: Path) -> None:
    if not _POSIX:
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        logger.debug("key_store: fsync of %s failed", directory, exc_info=True)
    finally:
        os.close(fd)


def tighten(home: Path) -> bool:
    """Make an existing key file ``0600`` and its directory ``0700`` (on Windows:
    the owner-only list and owner on the directory and on every file in it,
    :func:`.owner_only.ensure_private`). ``True`` when changed. Never raises:
    a file it may not change is logged and left (the read says what is wrong)."""
    directory, path = store_dir(home), store_path(home)
    changed = False
    for target in (directory, path):
        if not os.path.lexists(target):
            continue
        try:
            changed = owner_only.ensure_private(target, root=Path(home)) or changed
        except OSError:
            logger.debug("key_store: %s was not made private", target, exc_info=True)
    if directory.is_dir():
        changed = owner_only.restrict_children(directory, root=Path(home)) > 0 or changed
    return changed


def make_yours(home: Path) -> StoreRead | None:
    """``doctor --fix`` for a key file that is ``repairable`` (yours, but other
    accounts can change it): its directory and the files in it made yours only,
    the key kept. Returns the read before the repair when one was made, ``None``
    when there was nothing to repair. Raises :class:`KeyStoreError` when the
    file is still not usable afterwards (the read's detail says why)."""
    before = read_store(home)
    if before.status != "unsafe" or not before.repairable:
        return None
    try:
        owner_only.make_private_dir(store_dir(home), root=Path(home), enforce=True, strict=True)
        owner_only.ensure_private(store_path(home), root=Path(home))
    except OSError as exc:
        raise KeyStoreError(f"{store_path(home)} was not made yours: {exc.strerror or exc}") from exc
    owner_only.restrict_children(store_dir(home), root=Path(home))
    after = read_store(home)
    if not after.usable:
        raise KeyStoreError(f"{after.path} is still not usable: {after.detail or after.status}")
    return before


def delete_store(home: Path) -> bool:
    """Remove ``home``'s key file (a link is removed, never followed). ``True`` when one was there."""
    path = store_path(home)
    try:
        remove_file(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KeyStoreError(f"cannot remove {path}: {exc.strerror or exc}") from exc
    with contextlib.suppress(OSError):
        store_dir(home).rmdir()  # only when empty
    return True


# ─── other profiles on this machine ─────────────────────────────────────────


@dataclass(frozen=True)
class Holder:
    """Another profile home on this Hermes root holding the same key: in its key
    file, or in its settings as this process resolved them for it."""

    home: Path
    where: Literal["key file", "settings"] = "key file"


def profile_homes(root: Path) -> list[Path]:
    """The Hermes root (the default profile) and every named profile under it."""
    homes = [root]
    profiles = root / "profiles"
    with contextlib.suppress(OSError):
        homes.extend(sorted(p for p in profiles.iterdir() if p.is_dir()))
    return homes


def other_holders(home: Path, api_key: str, *, root: Path) -> list[Holder]:
    """Every *other* profile of ``root`` whose AgentChat key file holds ``api_key``.

    Only this plugin's own key files are read (``<profile>/agentchat/
    credentials.json``), never another profile's settings or ``.env``.
    Nothing read is kept or shown: values are compared and dropped.
    """
    return [
        Holder(candidate)
        for candidate in profile_homes(root)
        if not same_home(candidate, home) and _holds_same_key(candidate, api_key)
    ]


def holds_fingerprint(home: Path, key_fingerprint: str) -> bool:
    """Does ``home``'s key file (a regular file of yours) hold a key with this :func:`fingerprint`?"""
    path = store_path(home)
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if _judge(path, st, home).problem is not None:
        return False
    record = _parse(path)
    return isinstance(record, StoredKey) and fingerprint(record.api_key) == key_fingerprint
