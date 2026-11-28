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
from .state import replace_file

logger = logging.getLogger(__name__)

STORE_DIRNAME = "agentchat"
STORE_FILENAME = "credentials.json"
STORE_VERSION = 1

_FILE_MODE = 0o600
_DIR_MODE = 0o700
# POSIX file modes and owners mean something; on Windows they do not (every
# file reads 0o666 whatever was asked): there the access list says who may
# use the file (:mod:`.owner_only`). A constant rather than a platform check
# inline, so the type checker on each platform sees both branches.
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
    problem = _ownership_problem(path, st)
    if problem is not None:
        return StoreRead("unsafe", path, detail=problem)
    record = _parse(path)
    if isinstance(record, str):
        return StoreRead("unreadable", path, detail=record)
    loose = _is_loose(path, st)
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


def _ownership_problem(path: Path, st: os.stat_result) -> str | None:
    """Why the file must not be trusted as this user's own; ``None`` when it may.

    A symlink, a non-regular file, another owner, or a file the group or
    others can write: any of them lets someone else choose which agent this
    profile is.
    """
    if stat.S_ISLNK(st.st_mode):
        return f"{path} is a symbolic link; the key file must be a regular file"
    if not stat.S_ISREG(st.st_mode):
        return f"{path} is not a regular file"
    if not _POSIX:
        return _windows_problem(path)
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and st.st_uid != getuid():
        return f"{path} is owned by uid {st.st_uid}, not by you (uid {getuid()})"
    if st.st_mode & 0o022:
        return f"{path} can be written by its group or by others (mode {stat.S_IMODE(st.st_mode):o})"
    return None


def _windows_problem(path: Path) -> str | None:
    """The POSIX owner and group/other-write checks, read from the access list.
    ``None`` when the list cannot be read (a file system without one): the
    file is then judged as before this check existed, by its contents."""
    access = owner_only.inspect(path)
    if access is None:
        return None
    if not access.owner_trusted:
        return f"{path} is owned by {owner_only.describe_sid(access.owner)}, not by you ({access.user})"
    if access.null_dacl:
        return f"{path} has no access list: every account on this computer can change it"
    if access.others_write:
        names = ", ".join(owner_only.describe_sid(sid) for sid in access.others_write)
        return f"{path} can be changed by other accounts on this computer ({names})"
    return None


def _is_loose(path: Path, st: os.stat_result) -> bool:
    if _POSIX:
        return bool(st.st_mode & 0o044)
    access = owner_only.inspect(path)
    return access is not None and not access.private


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
    if _ownership_problem(path, st) is not None:
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
        owner_only.make_private_dir(directory, enforce=True, strict=True)
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
        fd = os.open(tmp, flags, _FILE_MODE)
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
    the owner-only access list on the directory and on the files in it).
    ``True`` when changed."""
    if not _POSIX:
        return _tighten_windows(home)
    path = store_path(home)
    changed = False
    for target, mode in ((store_dir(home), _DIR_MODE), (path, _FILE_MODE)):
        try:
            st = os.lstat(target)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        if stat.S_IMODE(st.st_mode) != mode:
            try:
                os.chmod(target, mode)
                changed = True
            except OSError:
                logger.debug("key_store: chmod of %s failed", target, exc_info=True)
    return changed


def _tighten_windows(home: Path) -> bool:
    directory = store_dir(home)
    if not directory.is_dir():
        return False
    changed = False
    current = owner_only.inspect(directory)
    if current is None or not (current.private and current.protected):
        try:
            owner_only.restrict(directory)
        except OSError:
            logger.debug("key_store: the access list of %s was not set", directory, exc_info=True)
            return False
        changed = True
    # The files already in it keep the list they were created with (nothing
    # propagates): each one is set too.
    return owner_only.restrict_children(directory) > 0 or changed


def delete_store(home: Path) -> bool:
    """Remove ``home``'s key file (a link is removed, never followed). ``True`` when one was there."""
    path = store_path(home)
    try:
        path.unlink()
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
    if _ownership_problem(path, st) is not None:
        return False
    record = _parse(path)
    return isinstance(record, StoredKey) and fingerprint(record.api_key) == key_fingerprint
