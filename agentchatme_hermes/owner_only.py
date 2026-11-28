"""Files and directories only the user who runs the plugin can use.

POSIX: the mode bits, ``0700`` for a directory and ``0600`` for a file, as the
plugin always did.

Windows: mode bits mean nothing there (every file reads ``0o666`` whatever
``chmod`` asked), so a private object is one owned by the user the process
runs as (its token's user SID), whose access list is exactly three entries,
protected from the parent's: full control for that user, for SYSTEM and for
the machine's Administrators. Those are the principals POSIX root stands for,
and the same three Python 3.13 gives ``os.mkdir(path, 0o700)`` on Windows,
with one difference that matters: Python names ``OWNER RIGHTS``, and in an
elevated process the owner of a new directory is the Administrators group, so
a directory made from a "Run as administrator" prompt is then closed to the
same user unelevated (Hermes' own ``hermes_dir_acl`` exists for that
failure). Naming the user's SID keeps it theirs either way. Files created in
such a directory inherit the three entries, so a key file is private from its
first byte, as ``0600`` is on POSIX.

How Hermes keeps its own secrets on Windows, for comparison: ``.env`` and
``auth.json`` stay under ``%LOCALAPPDATA%\\hermes``, whose access list
(inherited from the user profile) already names only the user, SYSTEM and
Administrators; its ``chmod(0o600)`` is a no-op there; its private stores are
``make_user_dir(mode=0o700)``, the Python 3.13 list above plus the user's SID
when elevated. A Hermes home outside the user profile (``HERMES_HOME`` on
another drive) inherits that drive's list, which commonly lets every local
user read and Authenticated Users modify. The explicit list here does not
depend on where the home is or which Python runs.

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
  cannot read is an error, never "nothing to judge".
* Inherit-only entries count: on a directory they are the list each new file
  gets.
* A new directory is created with its list (``CreateDirectoryW`` with the
  security descriptor): there is no moment where it has the parent's.
* :func:`ensure_private` is the one place an object is made private: nothing
  when it already is; else the list set, the owner made this user, and both
  read back and required. An object of another owner is refused, never taken
  over, and nothing is written into an existing directory of another owner.

Only the standard library (``ctypes`` on ``advapi32``/``kernel32``).
"""
from __future__ import annotations

import contextlib
import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

DIR_MODE = 0o700
FILE_MODE = 0o600

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"
CREATOR_OWNER_SID = "S-1-3-0"

# Names for the SIDs a too-open list usually names, for messages.
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
# lets them change the contents or the access itself.
_FILE_READ_DATA = 0x0001
_FILE_WRITE_DATA = 0x0002
_FILE_APPEND_DATA = 0x0004
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_GENERIC_ALL = 0x10000000
_GENERIC_WRITE = 0x40000000
_GENERIC_READ = 0x80000000
_READ_MASK = _FILE_READ_DATA | _GENERIC_READ | _GENERIC_ALL
_WRITE_MASK = _FILE_WRITE_DATA | _FILE_APPEND_DATA | _WRITE_DAC | _WRITE_OWNER | _GENERIC_WRITE | _GENERIC_ALL


def describe_sid(sid: str) -> str:
    """A SID as a person reads it: a well-known name, else the SID."""
    return _WELL_KNOWN_NAMES.get(sid, sid)


@dataclass(frozen=True)
class Access:
    """Who may use one file or directory (Windows), as its access list says.

    ``user`` is the SID this process runs as; "trusted" is that user, SYSTEM
    and Administrators. Deny entries are ignored (they only take access
    away, so ignoring them can only over-report); inherit-only entries count
    (on a directory they are the list each new file gets)."""

    user: str
    owner: str
    owner_trusted: bool
    # Principals outside the trusted three that an allow entry lets read the
    # contents / change the contents or the access list.
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
    that resolves elsewhere, another owner, or a list that did not become
    the private one."""


_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    """A WARNING the first time for ``key`` in this process, DEBUG after (a
    lock opened every 15 s must not log every 15 s)."""
    if key in _warned:
        logger.debug(message, *args)
        return
    _warned.add(key)
    logger.warning(message, *args)


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
    ``enforce`` an existing one is made private (:func:`ensure_private`). A
    failure to read or set its list is raised with ``strict`` (the key's
    directory) and otherwise logged once, the directory used as it is (a lock
    or a state file must not stop on a file system quirk; the key must).
    """
    path = Path(path)
    if sys.platform == "win32":
        anchor = Path(root) if root is not None else path.parent
        try:
            created = _create_private_dir(path)
            if created and not strict:
                return
            if created or enforce:
                ensure_private(path, root=anchor)
            else:
                _refuse_foreign(path, root=anchor)
        except OwnerOnlyError:
            raise
        except OSError as exc:
            if strict:
                raise
            _warn_once(f"dir:{path}", "owner_only: %s keeps its access list as it is (%s)", path, exc)
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
    without access lists has nothing to set: ``False``. Raises ``OSError``
    when the change fails.
    """
    path = Path(path)
    if sys.platform == "win32":
        current = inspect(path, root=root)
        if current is None or current.settled:
            return False
        if not current.owner_trusted:
            raise OwnerOnlyError(
                0, f"{path} is owned by {describe_sid(current.owner)}, another account: it is not taken over", str(path)
            )
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


def _refuse_foreign(path: Path, *, root: Path) -> None:
    """Windows: an existing directory of another owner, or one :func:`inspect`
    refuses, is never written into."""
    current = inspect(path, root=root)
    if current is not None and not current.owner_trusted:
        raise OwnerOnlyError(
            0, f"{path} is owned by {describe_sid(current.owner)}, another account: nothing is written into it", str(path)
        )


# ─── Windows: the access list ──────────────────────────────────────────────

if sys.platform == "win32":
    import ctypes
    import functools
    from ctypes import wintypes

    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _READ_CONTROL = 0x00020000
    _FILE_READ_ATTRIBUTES = 0x0080  # the attribute, link-count and name queries in _open
    _FILE_SHARE_ALL = 0x00000007  # read | write | delete: never in another process's way
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # needed to open a directory
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000  # the link itself, never its target
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _NAME_SURROGATE = 0x20000000  # IsReparseTagNameSurrogate: a symbolic link or junction
    _FILE_STANDARD_INFO = 1
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _FILE_PERSISTENT_ACLS = 0x00000008
    _ERROR_ALREADY_EXISTS = 183
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
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

    class _FileStandardInfo(ctypes.Structure):
        _fields_ = (
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BOOLEAN),
            ("Directory", wintypes.BOOLEAN),
        )

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = (("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD))

    class _Acl(ctypes.Structure):
        _fields_ = (
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        )

    class _AceHeader(ctypes.Structure):
        _fields_ = (("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wintypes.WORD))

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD))

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        )

    class _Api:
        """The few Win32 calls used, with their argument and result types pinned
        (the ctypes default ``int`` result truncates a 64-bit handle)."""

        def __init__(self) -> None:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            a = ctypes.WinDLL("advapi32", use_last_error=True)
            vp, dword, wstr = ctypes.c_void_p, wintypes.DWORD, wintypes.LPCWSTR
            self.CreateFileW = self._bind(k.CreateFileW, (wstr, dword, dword, vp, dword, dword, vp), vp)
            self.CreateDirectoryW = self._bind(k.CreateDirectoryW, (wstr, vp), wintypes.BOOL)
            self.CloseHandle = self._bind(k.CloseHandle, (vp,), wintypes.BOOL)
            self.LocalFree = self._bind(k.LocalFree, (vp,), vp)
            self.GetCurrentProcess = self._bind(k.GetCurrentProcess, (), vp)
            self.GetFileInformationByHandleEx = self._bind(
                k.GetFileInformationByHandleEx, (vp, ctypes.c_int, vp, dword), wintypes.BOOL
            )
            self.GetFinalPathNameByHandleW = self._bind(
                k.GetFinalPathNameByHandleW, (vp, wintypes.LPWSTR, dword, dword), dword
            )
            self.GetVolumeInformationByHandleW = self._bind(
                k.GetVolumeInformationByHandleW,
                (vp, wintypes.LPWSTR, dword, vp, vp, ctypes.POINTER(dword), wintypes.LPWSTR, dword),
                wintypes.BOOL,
            )
            self.OpenProcessToken = self._bind(a.OpenProcessToken, (vp, dword, ctypes.POINTER(vp)), wintypes.BOOL)
            self.GetTokenInformation = self._bind(
                a.GetTokenInformation, (vp, ctypes.c_int, vp, dword, ctypes.POINTER(dword)), wintypes.BOOL
            )
            self.ConvertSidToStringSidW = self._bind(a.ConvertSidToStringSidW, (vp, ctypes.POINTER(vp)), wintypes.BOOL)
            pvp = ctypes.POINTER(vp)
            self.GetSecurityInfo = self._bind(
                a.GetSecurityInfo, (vp, ctypes.c_int, dword, pvp, pvp, pvp, pvp, pvp), dword
            )
            self.GetSecurityDescriptorControl = self._bind(
                a.GetSecurityDescriptorControl, (vp, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(dword)),
                wintypes.BOOL,
            )
            self.GetAce = self._bind(a.GetAce, (vp, dword, pvp), wintypes.BOOL)
            self.ConvertStringSecurityDescriptorToSecurityDescriptorW = self._bind(
                a.ConvertStringSecurityDescriptorToSecurityDescriptorW, (wstr, dword, pvp, vp), wintypes.BOOL
            )
            self.SetKernelObjectSecurity = self._bind(a.SetKernelObjectSecurity, (vp, dword, vp), wintypes.BOOL)

        @staticmethod
        def _bind(function: Any, argtypes: tuple[Any, ...], restype: Any) -> Any:
            function.argtypes = argtypes
            function.restype = restype
            return function

    @functools.lru_cache(maxsize=1)
    def _api() -> _Api:
        return _Api()

    def _winerror(what: str, code: int | None = None) -> OSError:
        code = ctypes.get_last_error() if code is None else code
        return OSError(0, f"{what}: {ctypes.FormatError(code).strip()}", None, code)

    def _sid_string(sid: int | None) -> str:
        api = _api()
        text = ctypes.c_void_p()
        if not sid or not api.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _winerror("ConvertSidToStringSidW")
        try:
            return ctypes.wstring_at(text.value or 0)
        finally:
            api.LocalFree(text)

    @functools.lru_cache(maxsize=1)
    def current_user_sid() -> str:
        """The SID of the user this process's token runs as (the same user
        elevated or not)."""
        api = _api()
        token = ctypes.c_void_p()
        if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise _winerror("OpenProcessToken")
        try:
            size = wintypes.DWORD()
            api.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not api.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
                raise _winerror("GetTokenInformation")
            user = _SidAndAttributes.from_buffer(buffer)
            return _sid_string(user.Sid)
        finally:
            api.CloseHandle(token)

    def _private_sddl(*, is_dir: bool, owner: bool) -> str:
        inherit = "OICI" if is_dir else ""
        user = current_user_sid()
        entries = "".join(f"(A;{inherit};FA;;;{sid})" for sid in (user, SYSTEM_SID, ADMINISTRATORS_SID))
        return (f"O:{user}" if owner else "") + "D:P" + entries

    @contextlib.contextmanager
    def _descriptor(sddl: str) -> Iterator[ctypes.c_void_p]:
        api = _api()
        descriptor = ctypes.c_void_p()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise _winerror("ConvertStringSecurityDescriptorToSecurityDescriptorW")
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
        api = _api()
        handle = api.CreateFileW(
            str(path), access | _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if not handle or handle == _INVALID_HANDLE:
            raise _winerror(f"cannot open {path}")
        try:
            tag = _FileAttributeTagInfo()
            if not api.GetFileInformationByHandleEx(handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(tag), ctypes.sizeof(tag)):
                raise _winerror(f"cannot read the attributes of {path}")
            if tag.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT and tag.ReparseTag & _NAME_SURROGATE:
                raise OwnerOnlyError(0, f"{path} is a symbolic link or junction; it is never followed", str(path))
            is_dir = bool(tag.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY)
            if not is_dir:
                info = _FileStandardInfo()
                if not api.GetFileInformationByHandleEx(handle, _FILE_STANDARD_INFO, ctypes.byref(info), ctypes.sizeof(info)):
                    raise _winerror(f"cannot read the attributes of {path}")
                if info.NumberOfLinks > 1:
                    raise OwnerOnlyError(
                        0, f"{path} has {info.NumberOfLinks} names (a hard link): its other names may be anyone's",
                        str(path),
                    )
            buffer = ctypes.create_unicode_buffer(32768)
            length = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise _winerror(f"cannot read where {path} is")
            final = os.path.normcase(_strip_verbatim(buffer.value).rstrip("\\"))
            expected = _expected(path, root)
            if final != expected:
                raise OwnerOnlyError(
                    0, f"{path} resolves to {buffer.value} (a link or junction on the way); it is never followed",
                    str(path),
                )
            flags = wintypes.DWORD()
            if not api.GetVolumeInformationByHandleW(handle, None, 0, None, None, ctypes.byref(flags), None, 0):
                raise _winerror(f"cannot read the volume of {path}")
            return _Opened(int(handle), is_dir, bool(flags.value & _FILE_PERSISTENT_ACLS))
        except BaseException:
            api.CloseHandle(handle)
            raise

    def _ace_sid(ace: int, header: _AceHeader) -> int | None:
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
        api = _api()
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
                raise _winerror(f"cannot read the access list of {path}", code)
            owner = _sid_string(owner_sid.value)
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise _winerror("GetSecurityDescriptorControl")
            protected = bool(control.value & _SE_DACL_PROTECTED)
            if not dacl.value:
                return Access(user, owner, owner in trusted, (), (), True, protected, True)
            others_read: list[str] = []
            others_write: list[str] = []
            user_granted = False
            for index in range(_Acl.from_address(dacl.value).AceCount):
                ace = ctypes.c_void_p()
                if not api.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                    raise _winerror("GetAce")
                header = _AceHeader.from_address(ace.value)
                sid_at = _ace_sid(ace.value, header)
                if sid_at is None:
                    continue  # a deny entry (or an audit one): takes nothing away from privacy
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                inherit_only = bool(header.AceFlags & _INHERIT_ONLY_ACE)
                sid = _sid_string(sid_at)
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
        api = _api()
        path = Path(path)
        # The list first: it is what grants this user WRITE_OWNER for the second write.
        for access, what in ((_WRITE_DAC, _DACL_SECURITY_INFORMATION), (_WRITE_OWNER, _OWNER_SECURITY_INFORMATION)):
            opened = _open(path, _READ_CONTROL | access, root)
            try:
                sddl = _private_sddl(is_dir=opened.is_dir, owner=True)
                with _descriptor(sddl) as descriptor:
                    if not api.SetKernelObjectSecurity(opened.handle, what, descriptor):
                        raise _winerror(f"cannot set the {'access list' if what == _DACL_SECURITY_INFORMATION else 'owner'} of {path}")
            finally:
                api.CloseHandle(opened.handle)

    def _create_private_dir(path: Path) -> bool:
        """Create ``path`` with the private list and this user as owner in the one
        call that makes it; ``False`` when a directory is already there."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        api = _api()
        with _descriptor(_private_sddl(is_dir=True, owner=True)) as descriptor:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            if api.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                return True
            code = ctypes.get_last_error()
        if code == _ERROR_ALREADY_EXISTS and Path(path).is_dir():
            return False
        if code == _ERROR_ALREADY_EXISTS:
            raise FileExistsError(0, f"{path} exists and is not a directory", str(path))
        raise _winerror(f"cannot create {path}", code)

else:

    def current_user_sid() -> str:
        raise OSError("Windows only")

    def inspect(path: Path, *, root: Path | None = None) -> Access | None:
        """Windows only; ``None`` elsewhere (the mode bits say it there)."""
        return None

    def restrict(path: Path, *, root: Path | None = None) -> None:
        raise OSError("Windows only")
