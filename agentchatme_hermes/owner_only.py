"""Files and directories only the user who runs the plugin can use.

POSIX: the mode bits, ``0700`` for a directory and ``0600`` for a file, as the
plugin always did.

Windows: mode bits mean nothing there (every file reads ``0o666`` whatever
``chmod`` asked). What the plugin writes is an object owned by the user the
process runs as (its token's user SID) with a protected access list of three
entries: full control for that user, for SYSTEM and for the machine's
Administrators, the principals POSIX root stands for. Python 3.13 gives
``os.mkdir(path, 0o700)`` the same three on Windows, with one difference that
matters: Python names ``OWNER RIGHTS``, and in an elevated process the owner
of a new directory is the Administrators group, so a directory made from a
"Run as administrator" prompt is then closed to the same user unelevated
(Hermes' own ``hermes_dir_acl`` exists for that failure). Naming the user's
SID keeps it theirs either way.

What a check accepts as private (:attr:`Access.private`): the owner is the
user, SYSTEM or Administrators; no allow entry for anyone else grants reading
the contents (``FILE_READ_DATA``, ``GENERIC_READ``, ``GENERIC_ALL``) or
changing them or the object (write and append data, ``DELETE``,
``FILE_DELETE_CHILD``, ``WRITE_DAC``, ``WRITE_OWNER``, ``GENERIC_WRITE``,
``GENERIC_ALL``), inherit-only entries included (on a directory they are what
each new file gets); and an entry that applies to the object grants the user
itself read and write. Entries for the three trusted principals may hold any
rights, deny entries are ignored (they only take access away), and other
entries may hold rights that neither read nor change (``READ_CONTROL``,
``SYNCHRONIZE``, attribute reads).

How Hermes keeps its own secrets on Windows, for comparison: ``.env`` and
``auth.json`` stay under ``%LOCALAPPDATA%\\hermes``, whose access list
(inherited from the user profile) names only the user, SYSTEM and
Administrators; its ``chmod(0o600)`` is a no-op there; its private stores are
``make_user_dir(mode=0o700)``, the Python 3.13 list above plus the user's SID
when elevated. A private key file protects nothing in a Hermes home other
accounts can change (``HERMES_HOME=C:\\hermes`` inheriting ``Authenticated
Users: Modify`` from the drive): they can change the plugin's code and
``config.yaml``. The key file's checks here say who can use the file;
``hermes agentchat doctor`` says the same of the home and fails on one others
can change.

What is checked on Windows, and how:

* Everything goes through a handle opened on the path itself
  (``FILE_FLAG_OPEN_REPARSE_POINT``), never by name after the checks. Refused
  (:class:`OwnerOnlyError`): a symbolic link or junction at the path; a file
  with more than one name (a hard link: its other names may be anywhere); and
  a path whose handle resolves anywhere but the caller's ``root`` (taken as
  the caller chose it, links included) followed by the parts below it, which
  is how a junction between the root and the object shows
  (``GetFinalPathNameByHandleW``, as Hermes' ``hermes_dir_acl`` checks).
* :func:`inspect` answers ``None`` only for a volume that stores no access
  lists at all (``FILE_PERSISTENT_ACLS`` unset: FAT, exFAT). Anything else it
  cannot read is an error. A caller that keeps a secret refuses such a volume
  (``strict``): nothing on it can be private.
* A new directory is created with its list (``CreateDirectoryW`` with the
  security descriptor) and a new private file the same way
  (:func:`create_private_file`, ``CreateFileW``): neither has the list of the
  folder its name resolves to at any moment.
* :func:`ensure_private` is the one place an existing object is made private:
  nothing when it already is; else the list set, the owner made this user,
  and both read back and required. An object of another owner is refused,
  never taken over, and nothing is written into an existing directory of
  another owner.

Only the standard library (``ctypes``, through :mod:`.win32`).
"""
from __future__ import annotations

import contextlib
import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import win32
from .log_once import log_once

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

DIR_MODE = 0o700
FILE_MODE = 0o600

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"
CREATOR_OWNER_SID = "S-1-3-0"

# Names for SIDs Windows may not name (and the fallback when it cannot).
_WELL_KNOWN_NAMES = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
    "S-1-5-32-546": "Guests",
    "S-1-5-4": "INTERACTIVE",
    "S-1-5-7": "ANONYMOUS LOGON",
    "S-1-3-1": "CREATOR GROUP",
}

# Access-mask bits (winnt.h): what lets someone read the contents, and what
# lets them change the contents, the object or its access.
_FILE_READ_DATA = 0x0001
_FILE_WRITE_DATA = 0x0002
_FILE_APPEND_DATA = 0x0004
_FILE_DELETE_CHILD = 0x0040
_DELETE = 0x00010000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_GENERIC_ALL = 0x10000000
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_READ_MASK = _FILE_READ_DATA | _GENERIC_READ | _GENERIC_ALL
_WRITE_MASK = (
    _FILE_WRITE_DATA | _FILE_APPEND_DATA | _FILE_DELETE_CHILD | _DELETE
    | _WRITE_DAC | _WRITE_OWNER | _GENERIC_WRITE | _GENERIC_ALL
)


def current_user_sid() -> str:
    """The SID of the user this process runs as (Windows)."""
    return win32.current_user_sid()


def describe_sid(sid: str) -> str:
    """A SID as a person reads it: the account Windows names (``DOMAIN\\name``),
    else a well-known name, else the SID."""
    return win32.account_name(sid) or _WELL_KNOWN_NAMES.get(sid, sid)


def no_access_lists_message(path: Path) -> str:
    return (
        f"{path} is on a volume that keeps no access lists (FAT or exFAT): any account on this "
        "computer can read and change it, so no key is kept there — move the Hermes home to an NTFS drive"
    )


def foreign_owner_message(path: Path, owner: str, what: str) -> str:
    return (
        f"{path} is owned by {describe_sid(owner)}, another account: {what} (on a network share, "
        "files belong to the server's accounts: keep the Hermes home on a local disk)"
    )


@dataclass(frozen=True)
class Access:
    """Who may use one file or directory (Windows), as its access list says
    (the module docstring has what counts). ``user`` is the SID this process
    runs as; "trusted" is that user, SYSTEM and Administrators."""

    user: str
    owner: str
    owner_trusted: bool
    # Principals outside the trusted three that an allow entry lets read the
    # contents / change the contents, the object or its access.
    others_read: tuple[str, ...]
    others_write: tuple[str, ...]
    # An entry that applies to the object itself gives this user read and
    # write by its own SID (or by OWNER RIGHTS while it owns the object): it
    # can use the object unelevated too.
    user_granted: bool
    # Protected from the parent's inheritable entries.
    protected: bool
    # No access list at all: everyone has full access.
    null_dacl: bool

    @property
    def private(self) -> bool:
        """Only the user, SYSTEM and Administrators can use it, and the user can."""
        return (
            self.owner_trusted
            and not self.null_dacl
            and not self.others_read
            and not self.others_write
            and self.user_granted
        )

    @property
    def settled(self) -> bool:
        """What :func:`ensure_private` leaves: private, protected, owned by this user."""
        return self.private and self.protected and self.owner == self.user


class OwnerOnlyError(OSError):
    """The object must not be used as private: a link, a hard link, a path
    that resolves elsewhere, another owner, a volume without access lists
    where one is required, or a list that did not become the private one."""


# ─── creating, making private ───────────────────────────────────────────────


def make_private_dir(
    path: Path, *, root: Path | None = None, enforce: bool = False, strict: bool = False
) -> None:
    """Create ``path`` (and missing parents) for this user only.

    The leaf only: parents are created as ``mkdir`` creates them. ``root`` is
    the directory the caller trusts as it is (default: ``path``'s parent);
    nothing between it and ``path`` may be a link.

    POSIX: ``mkdir`` with mode ``0700``; with ``enforce`` an existing
    directory is ``chmod 0700`` too. Raises ``OSError`` when either fails.

    Windows: a new directory is created with the private list. An existing
    one is refused (:class:`OwnerOnlyError`) when it is a link, resolves
    elsewhere or has another owner: nothing is written into it. With
    ``enforce`` an existing one is made private (:func:`ensure_private`).
    ``strict`` (a directory that keeps a secret) raises every failure, a
    volume without access lists included; otherwise a failure to read or set
    the list is logged once and the directory used as it is (a lock or a
    state file must not stop on a file system quirk; the key must).
    """
    path = Path(path)
    if sys.platform == "win32":
        anchor = Path(root) if root is not None else path.parent
        try:
            created = _create_private_dir(path)
            if strict:
                if inspect(path, root=anchor) is None:
                    raise OwnerOnlyError(0, no_access_lists_message(path), str(path))
                ensure_private(path, root=anchor)
            elif created:
                return
            elif enforce:
                ensure_private(path, root=anchor)
            else:
                _refuse_foreign(path, root=anchor)
        except OwnerOnlyError:
            raise
        except OSError as exc:
            if strict:
                raise
            log_once(logger, f"dir:{path}:{exc}", logging.WARNING, "owner_only: %s keeps its access list as it is (%s)", path, exc)
    else:
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        if enforce:
            os.chmod(path, DIR_MODE)


def ensure_private(path: Path, *, root: Path | None = None) -> bool:
    """Make ``path`` private unless it already is; ``True`` when it changed.

    POSIX: ``chmod`` to ``0700`` (a directory) or ``0600``; a symbolic link
    is left alone. Windows: refused (:class:`OwnerOnlyError`) for another
    owner and everything :func:`inspect` refuses; else the private list and
    this user as owner, read back and required (``settled``). A volume
    without access lists has nothing to set: ``False`` (a caller that keeps
    a secret refuses it first). Raises ``OSError`` when the change fails.
    """
    path = Path(path)
    if sys.platform == "win32":
        current = inspect(path, root=root)
        if current is None or current.settled:
            return False
        if not current.owner_trusted:
            raise OwnerOnlyError(0, foreign_owner_message(path, current.owner, "it is not taken over"), str(path))
        restrict(path, root=root)
        after = inspect(path, root=root)
        if after is not None and not after.settled:
            raise OwnerOnlyError(0, f"{path} did not become private ({after})", str(path))
        changed = True
    else:
        st = os.lstat(path)
        mode = DIR_MODE if stat.S_ISDIR(st.st_mode) else FILE_MODE
        changed = not stat.S_ISLNK(st.st_mode) and stat.S_IMODE(st.st_mode) != mode
        if changed:
            os.chmod(path, mode)
    return changed


def restrict_children(directory: Path, *, root: Path | None = None) -> int:
    """Windows: :func:`ensure_private` on each regular file directly in
    ``directory`` (``root`` as there; default: ``directory``'s parent). The
    number changed. A file it refuses or cannot change is logged and left.
    ``0`` elsewhere (the caller sets the modes it needs)."""
    changed = 0
    children: list[Path] = []
    anchor = Path(root) if root is not None else Path(directory).parent
    if sys.platform == "win32":
        with contextlib.suppress(OSError), os.scandir(directory) as entries:
            children = [Path(entry.path) for entry in entries if entry.is_file(follow_symlinks=False)]
    for child in children:
        try:
            if ensure_private(child, root=anchor):
                changed += 1
        except OSError as exc:
            logger.debug("owner_only: %s kept its access list (%s)", child, exc)
    return changed


def create_private_file(path: Path) -> int:
    """Create ``path`` (it must not exist) for writing, private from its first
    byte whatever folder its name resolves to, and return a descriptor.

    POSIX: ``O_CREAT | O_EXCL`` with mode ``0600``. Windows: ``CreateFileW``
    with ``CREATE_NEW`` and the private descriptor (this user as owner, the
    three entries, protected), the handle wrapped as a descriptor. Raises
    ``FileExistsError`` when something is there (a planted link included)."""
    if sys.platform == "win32":
        fd = _create_private_file(Path(path))
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    return fd


def _refuse_foreign(path: Path, *, root: Path) -> None:
    """Windows: an existing directory of another owner, or one :func:`inspect`
    refuses, is never written into."""
    current = inspect(path, root=root)
    if current is not None and not current.owner_trusted:
        raise OwnerOnlyError(0, foreign_owner_message(path, current.owner, "nothing is written into it"), str(path))


# ─── Windows: the access list ──────────────────────────────────────────────

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _READ_CONTROL = 0x00020000
    _FILE_READ_ATTRIBUTES = 0x0080  # the attribute, link-count and name queries in _open
    _FILE_SHARE_ALL = 0x00000007  # read | write | delete: never in another process's way
    _OPEN_EXISTING = 3
    _CREATE_NEW = 1
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # needed to open a directory
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000  # the link itself, never its target
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _NAME_SURROGATE = 0x20000000  # IsReparseTagNameSurrogate: a symbolic link or junction
    _FILE_STANDARD_INFO = 1
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _FILE_PERSISTENT_ACLS = 0x00000008
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x1
    _DACL_SECURITY_INFORMATION = 0x4
    _SE_DACL_PROTECTED = 0x1000
    _SDDL_REVISION_1 = 1
    _ACCESS_ALLOWED_ACE_TYPE = 0x0
    _ACCESS_ALLOWED_CALLBACK_ACE_TYPE = 0x9
    _ACCESS_ALLOWED_OBJECT_ACE_TYPE = 0x5
    _ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE = 0xB
    _INHERIT_ONLY_ACE = 0x08
    _ACE_OBJECT_TYPE_PRESENT = 0x1
    _ACE_INHERITED_OBJECT_TYPE_PRESENT = 0x2

    def _private_sddl(*, is_dir: bool) -> str:
        inherit = "OICI" if is_dir else ""
        user = current_user_sid()
        entries = "".join(f"(A;{inherit};FA;;;{sid})" for sid in (user, SYSTEM_SID, ADMINISTRATORS_SID))
        return f"O:{user}D:P{entries}"

    @contextlib.contextmanager
    def _descriptor(sddl: str) -> Iterator[ctypes.c_void_p]:
        api = win32.api()
        descriptor = ctypes.c_void_p()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise win32.error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            yield descriptor
        finally:
            api.LocalFree(descriptor)

    def _strip_verbatim(path: str) -> str:
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[8:]
        return path[4:] if path.startswith("\\\\?\\") else path

    def _expected(path: Path, root: Path | None) -> str:
        """Where ``path`` must resolve: ``root``'s real path (``root`` may itself
        be a link, the caller's choice) followed by the parts below it."""
        absolute = Path(os.path.abspath(path))
        anchor = Path(os.path.abspath(root)) if root is not None else absolute.parent
        try:
            parts = absolute.relative_to(anchor).parts
        except ValueError:
            anchor, parts = absolute.parent, (absolute.name,)
        return os.path.normcase(os.path.join(os.path.realpath(anchor), *parts).rstrip("\\"))

    @dataclass(frozen=True)
    class _Opened:
        handle: int
        is_dir: bool
        has_acls: bool

    def _open(path: Path, access: int, root: Path | None) -> _Opened:
        """A handle on ``path`` itself (a link is opened, never followed), with
        the checks in the module docstring. Raises :class:`OwnerOnlyError` for
        what is refused, ``OSError`` for what cannot be read."""
        api = win32.api()
        handle = api.CreateFileW(
            str(path), access | _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if not handle or handle == win32.INVALID_HANDLE:
            raise win32.error(f"cannot open {path}")
        try:
            tag = win32.FileAttributeTagInfo()
            if not api.GetFileInformationByHandleEx(handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(tag), ctypes.sizeof(tag)):
                raise win32.error(f"cannot read the attributes of {path}")
            if tag.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT and tag.ReparseTag & _NAME_SURROGATE:
                raise OwnerOnlyError(0, f"{path} is a symbolic link or junction; it is never followed", str(path))
            is_dir = bool(tag.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY)
            if not is_dir:
                info = win32.FileStandardInfo()
                if not api.GetFileInformationByHandleEx(handle, _FILE_STANDARD_INFO, ctypes.byref(info), ctypes.sizeof(info)):
                    raise win32.error(f"cannot read the attributes of {path}")
                if info.NumberOfLinks > 1:
                    raise OwnerOnlyError(
                        0, f"{path} has {info.NumberOfLinks} names (a hard link): its other names may be anyone's",
                        str(path),
                    )
            buffer = ctypes.create_unicode_buffer(32768)
            length = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise win32.error(f"cannot read where {path} is")
            final = os.path.normcase(_strip_verbatim(buffer.value).rstrip("\\"))
            if final != _expected(path, root):
                raise OwnerOnlyError(
                    0, f"{path} resolves to {buffer.value} (a link or junction on the way); it is never followed",
                    str(path),
                )
            flags = wintypes.DWORD()
            if not api.GetVolumeInformationByHandleW(handle, None, 0, None, None, ctypes.byref(flags), None, 0):
                raise win32.error(f"cannot read the volume of {path}")
            return _Opened(int(handle), is_dir, bool(flags.value & _FILE_PERSISTENT_ACLS))
        except BaseException:
            api.CloseHandle(handle)
            raise

    def _ace_sid(ace: int, header: win32.AceHeader) -> int | None:
        """Where the SID of an allow entry starts; ``None`` for an entry type
        that grants nothing."""
        if header.AceType in (_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_ALLOWED_CALLBACK_ACE_TYPE):
            return ace + 8
        if header.AceType in (_ACCESS_ALLOWED_OBJECT_ACE_TYPE, _ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE):
            flags = ctypes.c_uint32.from_address(ace + 8).value
            offset = 12
            if flags & _ACE_OBJECT_TYPE_PRESENT:
                offset += 16
            if flags & _ACE_INHERITED_OBJECT_TYPE_PRESENT:
                offset += 16
            return ace + offset
        return None

    def inspect(path: Path, *, root: Path | None = None) -> Access | None:
        """Who may use ``path``, from its access list. ``None`` only on a volume
        that stores no access lists. Raises :class:`OwnerOnlyError` for what
        the module refuses (a link, a hard link, a path resolving elsewhere)
        and ``OSError`` when the list cannot be read: never "nothing to
        judge"."""
        api = win32.api()
        path = Path(path)
        user = current_user_sid()
        trusted = {user, SYSTEM_SID, ADMINISTRATORS_SID}
        opened = _open(path, _READ_CONTROL, root)
        descriptor = ctypes.c_void_p()
        try:
            if not opened.has_acls:
                return None
            owner_sid = ctypes.c_void_p()
            dacl = ctypes.c_void_p()
            code = api.GetSecurityInfo(
                opened.handle, _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                ctypes.byref(owner_sid), None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
            )
            if code != 0:
                raise win32.error(f"cannot read the access list of {path}", code)
            owner = win32.sid_string(owner_sid.value)
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise win32.error("GetSecurityDescriptorControl")
            protected = bool(control.value & _SE_DACL_PROTECTED)
            if not dacl.value:
                return Access(user, owner, owner in trusted, (), (), True, protected, True)
            others_read: list[str] = []
            others_write: list[str] = []
            user_granted = False
            for index in range(win32.Acl.from_address(dacl.value).AceCount):
                ace = ctypes.c_void_p()
                if not api.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                    raise win32.error("GetAce")
                header = win32.AceHeader.from_address(ace.value)
                sid_at = _ace_sid(ace.value, header)
                if sid_at is None:
                    continue  # a deny entry (or an audit one): takes nothing away from privacy
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                inherit_only = bool(header.AceFlags & _INHERIT_ONLY_ACE)
                sid = win32.sid_string(sid_at)
                if sid == OWNER_RIGHTS_SID:
                    sid = owner
                if sid == CREATOR_OWNER_SID:
                    # Becomes the creator of each new child: this user, or
                    # Administrators when it creates elevated. Never the object itself.
                    continue
                if sid == user and not inherit_only and mask & _READ_MASK and mask & _WRITE_MASK:
                    user_granted = True
                if sid in trusted:
                    continue
                if mask & _READ_MASK:
                    others_read.append(sid)
                if mask & _WRITE_MASK:
                    others_write.append(sid)
            return Access(
                user=user,
                owner=owner,
                owner_trusted=owner in trusted,
                others_read=tuple(dict.fromkeys(others_read)),
                others_write=tuple(dict.fromkeys(others_write)),
                user_granted=user_granted,
                protected=protected,
                null_dacl=False,
            )
        finally:
            if descriptor.value:
                api.LocalFree(descriptor)
            api.CloseHandle(opened.handle)

    def restrict(path: Path, *, root: Path | None = None) -> None:
        """Set ``path``'s access list to the user, SYSTEM and Administrators
        (full control; inherited by new children of a directory), protected
        from the parent's, then make this user its owner. Each write goes
        through a handle opened with the checks of :func:`inspect`; nothing
        propagates to existing children (a junction planted inside is never
        walked). Raises :class:`OwnerOnlyError` for what is refused and
        ``OSError`` otherwise."""
        api = win32.api()
        path = Path(path)
        # The list first: it is what grants this user WRITE_OWNER for the second write.
        for access, what in ((_WRITE_DAC, _DACL_SECURITY_INFORMATION), (_WRITE_OWNER, _OWNER_SECURITY_INFORMATION)):
            opened = _open(path, _READ_CONTROL | access, root)
            try:
                with _descriptor(_private_sddl(is_dir=opened.is_dir)) as descriptor:
                    if not api.SetKernelObjectSecurity(opened.handle, what, descriptor):
                        name = "access list" if what == _DACL_SECURITY_INFORMATION else "owner"
                        raise win32.error(f"cannot set the {name} of {path}")
            finally:
                api.CloseHandle(opened.handle)

    def _create_private_dir(path: Path) -> bool:
        """Create ``path`` with the private list and this user as owner in the one
        call that makes it; ``False`` when a directory is already there."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        api = win32.api()
        with _descriptor(_private_sddl(is_dir=True)) as descriptor:
            attributes = win32.SecurityAttributes(ctypes.sizeof(win32.SecurityAttributes), descriptor, False)
            if api.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                return True
            code = ctypes.get_last_error()
        if code == win32.ERROR_ALREADY_EXISTS and Path(path).is_dir():
            return False
        if code == win32.ERROR_ALREADY_EXISTS:
            raise FileExistsError(0, f"{path} exists and is not a directory", str(path))
        raise win32.error(f"cannot create {path}", code)

    def _create_private_file(path: Path) -> int:
        api = win32.api()
        generic_write = 0x40000000
        with _descriptor(_private_sddl(is_dir=False)) as descriptor:
            attributes = win32.SecurityAttributes(ctypes.sizeof(win32.SecurityAttributes), descriptor, False)
            handle = api.CreateFileW(
                str(path), generic_write, 0, ctypes.byref(attributes), _CREATE_NEW, _FILE_ATTRIBUTE_NORMAL, None
            )
            code = ctypes.get_last_error()
        if not handle or handle == win32.INVALID_HANDLE:
            if code in (win32.ERROR_FILE_EXISTS, win32.ERROR_ALREADY_EXISTS):
                raise FileExistsError(0, f"{path} exists", str(path))
            raise win32.error(f"cannot create {path}", code)
        try:
            return msvcrt.open_osfhandle(int(handle), os.O_WRONLY | os.O_BINARY)
        except BaseException:
            api.CloseHandle(handle)
            raise

else:

    def inspect(path: Path, *, root: Path | None = None) -> Access | None:
        """Windows only; ``None`` elsewhere (the mode bits say it there)."""
        return None

    def restrict(path: Path, *, root: Path | None = None) -> None:
        raise OSError("Windows only")
