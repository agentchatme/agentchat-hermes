"""Files and directories only the user who runs the plugin can use.

POSIX: the mode bits, ``0700`` for a directory and ``0600`` for a file, as the
plugin always did.

Windows: mode bits mean nothing there (every file reads ``0o666`` whatever
``chmod`` asked), so a private directory is one whose access list is set to
exactly three entries, protected from the parent's: full control for the
user the process runs as (its token's user SID), for SYSTEM and for the
machine's Administrators. Those are the principals POSIX root stands for,
and the same three Python 3.13 gives ``os.mkdir(path, 0o700)`` on Windows,
with one difference that matters: Python names ``OWNER RIGHTS``, and in an
elevated process the owner of a new directory is the Administrators group,
so a directory made from a "Run as administrator" prompt is then closed to
the same user unelevated (Hermes' own ``hermes_dir_acl`` exists for that
failure). Naming the user's SID keeps it theirs either way. Files created in
such a directory inherit the three entries, so a key file is private from its
first byte, as ``0600`` is on POSIX.

How Hermes keeps its own secrets on Windows, for comparison: ``.env`` and
``auth.json`` stay under ``%LOCALAPPDATA%\\hermes``, whose access list
(inherited from the user profile) already names only the user, SYSTEM and
Administrators; its ``chmod(0o600)`` is a no-op there; its private stores are
``make_user_dir(mode=0o700)``, the Python 3.13 list above plus the user's
SID when elevated. A Hermes home outside the user profile (``HERMES_HOME`` on
another drive) inherits that drive's list, which commonly lets every local
user read and Authenticated Users modify. The explicit list here does not
depend on where the home is or which Python runs.

Every access-list change goes through a handle opened on the path itself,
never through a junction, symbolic link or other name surrogate, and never on
a file with more than one name (a hard link): an elevated process that set
"full control for the user" through a planted link would hand the user an
object outside the Hermes home.

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

logger = logging.getLogger(__name__)

DIR_MODE = 0o700
FILE_MODE = 0o600

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"
_CREATOR_SIDS = frozenset({"S-1-3-0", "S-1-3-1"})  # CREATOR OWNER / CREATOR GROUP: inheritance placeholders

# Names for the SIDs a too-open list usually names, for messages.
_WELL_KNOWN_NAMES = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
    "S-1-5-32-546": "Guests",
    "S-1-5-4": "INTERACTIVE",
    "S-1-5-7": "ANONYMOUS LOGON",
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
    away, so ignoring them can only over-report)."""

    user: str
    owner: str
    owner_trusted: bool
    # Principals outside the trusted three that an allow entry lets read the
    # contents / change the contents or the access list.
    others_read: tuple[str, ...]
    others_write: tuple[str, ...]
    # An allow entry gives this user read and write by its own SID (or by
    # OWNER RIGHTS while it owns the object): it can use the object
    # unelevated too.
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
    def writable_by_others(self) -> bool:
        return self.null_dacl or not self.owner_trusted or bool(self.others_write)


class OwnerOnlyError(OSError):
    """The access list could not be set (or the path is a link it must not follow)."""


def _is_name_surrogate(path: Path) -> bool:
    """A symbolic link or junction (followed by name lookups), never followed here."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    tag = getattr(st, "st_reparse_tag", 0)
    return bool(tag & 0x20000000)  # IsReparseTagNameSurrogate


# ─── creating ───────────────────────────────────────────────────────────────


def make_private_dir(path: Path, *, enforce: bool = False, strict: bool = False) -> None:
    """Create ``path`` (and missing parents) for this user only, as above.

    The leaf only: parents are created as ``mkdir`` creates them. On an
    existing directory nothing changes unless ``enforce``: then POSIX
    ``chmod 0700`` and, on Windows, the access list set again when it is
    not already the private one.

    Raises ``OSError`` when the directory cannot be created (or, POSIX, its
    mode set). On Windows a failure to set the access list (a file system
    that stores none, a directory of another owner) raises only with
    ``strict``; otherwise it is logged and the directory is used as it is:
    a lock or a state file must not stop on it, the key must.
    """
    path = Path(path)
    if sys.platform == "win32":
        existed = path.is_dir()
        # Never ``mode=0o700`` here: Python 3.13+ turns it into an OWNER
        # RIGHTS list, which an elevated run closes to the user unelevated.
        path.mkdir(parents=True, exist_ok=True)
        if not existed or enforce:
            _restrict_dir(path, strict=strict)
    else:
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        if enforce:
            os.chmod(path, DIR_MODE)


def _restrict_dir(path: Path, *, strict: bool) -> None:
    """Windows: the private list on ``path`` unless it already has it."""
    try:
        current = inspect(path)
        if current is not None and current.private and current.protected:
            return
        restrict(path)
    except OSError as exc:
        if strict:
            raise
        logger.warning("owner_only: %s keeps its inherited access list (%s)", path, exc)


# ─── Windows: the access list ──────────────────────────────────────────────

if sys.platform == "win32":
    import ctypes
    import functools
    from ctypes import wintypes

    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _READ_CONTROL = 0x00020000
    _FILE_READ_ATTRIBUTES = 0x0080  # the attribute and link-count queries in _open
    _FILE_SHARE_ALL = 0x00000007  # read | write | delete: never in another process's way
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # needed to open a directory
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000  # the link itself, never its target
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_STANDARD_INFO = 1
    _FILE_ATTRIBUTE_TAG_INFO = 9
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

    class _Api:
        """The few Win32 calls used, with their argument and result types pinned
        (the ctypes default ``int`` result truncates a 64-bit handle)."""

        def __init__(self) -> None:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            a = ctypes.WinDLL("advapi32", use_last_error=True)
            self.CreateFileW = k.CreateFileW
            self.CreateFileW.argtypes = (
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            )
            self.CreateFileW.restype = ctypes.c_void_p
            self.CloseHandle = k.CloseHandle
            self.CloseHandle.argtypes = (ctypes.c_void_p,)
            self.CloseHandle.restype = wintypes.BOOL
            self.LocalFree = k.LocalFree
            self.LocalFree.argtypes = (ctypes.c_void_p,)
            self.LocalFree.restype = ctypes.c_void_p
            self.GetCurrentProcess = k.GetCurrentProcess
            self.GetCurrentProcess.argtypes = ()
            self.GetCurrentProcess.restype = ctypes.c_void_p
            self.GetFileInformationByHandleEx = k.GetFileInformationByHandleEx
            self.GetFileInformationByHandleEx.argtypes = (
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            )
            self.GetFileInformationByHandleEx.restype = wintypes.BOOL
            self.OpenProcessToken = a.OpenProcessToken
            self.OpenProcessToken.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
            self.OpenProcessToken.restype = wintypes.BOOL
            self.GetTokenInformation = a.GetTokenInformation
            self.GetTokenInformation.argtypes = (
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            )
            self.GetTokenInformation.restype = wintypes.BOOL
            self.ConvertSidToStringSidW = a.ConvertSidToStringSidW
            self.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
            self.ConvertSidToStringSidW.restype = wintypes.BOOL
            self.GetSecurityInfo = a.GetSecurityInfo
            self.GetSecurityInfo.argtypes = (
                ctypes.c_void_p, ctypes.c_int, wintypes.DWORD,
                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
            )
            self.GetSecurityInfo.restype = wintypes.DWORD
            self.GetSecurityDescriptorControl = a.GetSecurityDescriptorControl
            self.GetSecurityDescriptorControl.argtypes = (
                ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
            )
            self.GetSecurityDescriptorControl.restype = wintypes.BOOL
            self.GetAce = a.GetAce
            self.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
            self.GetAce.restype = wintypes.BOOL
            self.ConvertStringSecurityDescriptorToSecurityDescriptorW = (
                a.ConvertStringSecurityDescriptorToSecurityDescriptorW
            )
            self.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
                wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
            )
            self.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
            self.SetKernelObjectSecurity = a.SetKernelObjectSecurity
            self.SetKernelObjectSecurity.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
            self.SetKernelObjectSecurity.restype = wintypes.BOOL

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

    def _open(path: Path, access: int) -> int:
        """A handle on ``path`` itself (a link is opened, never followed); refused
        for a name surrogate or a file with more than one name."""
        api = _api()
        handle = api.CreateFileW(
            str(path), access | _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if not handle or handle == _INVALID_HANDLE:
            raise _winerror(f"cannot open {path}")
        try:
            tag = _FileAttributeTagInfo()
            if not api.GetFileInformationByHandleEx(
                handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(tag), ctypes.sizeof(tag)
            ):
                raise _winerror(f"cannot read the attributes of {path}")
            if tag.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT and tag.ReparseTag & 0x20000000:
                raise OwnerOnlyError(0, f"{path} is a symbolic link or junction; it is never followed", str(path))
            if not tag.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
                info = _FileStandardInfo()
                if not api.GetFileInformationByHandleEx(
                    handle, _FILE_STANDARD_INFO, ctypes.byref(info), ctypes.sizeof(info)
                ):
                    raise _winerror(f"cannot read the attributes of {path}")
                if info.NumberOfLinks > 1:
                    raise OwnerOnlyError(0, f"{path} has more than one name (a hard link)", str(path))
        except BaseException:
            api.CloseHandle(handle)
            raise
        return int(handle)

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

    def inspect(path: Path) -> Access | None:
        """Who may use ``path``, from its access list; ``None`` when that cannot
        be read (a file system without access lists, a link, no access)."""
        try:
            return _inspect(Path(path))
        except OSError as exc:
            logger.debug("owner_only: the access list of %s was not read (%s)", path, exc)
            return None

    def _inspect(path: Path) -> Access:
        api = _api()
        user = current_user_sid()
        trusted = {user, SYSTEM_SID, ADMINISTRATORS_SID}
        handle = _open(path, _READ_CONTROL)
        descriptor = ctypes.c_void_p()
        try:
            owner_sid = ctypes.c_void_p()
            dacl = ctypes.c_void_p()
            code = api.GetSecurityInfo(
                handle, _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
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
            count = _Acl.from_address(dacl.value).AceCount
            for index in range(count):
                ace = ctypes.c_void_p()
                if not api.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                    raise _winerror("GetAce")
                header = _AceHeader.from_address(ace.value)
                if header.AceFlags & _INHERIT_ONLY_ACE:
                    continue  # applies to children only
                sid_at = _ace_sid(ace.value, header)
                if sid_at is None:
                    continue  # a deny entry (or an audit one): takes nothing away from privacy
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                sid = _sid_string(sid_at)
                if sid == OWNER_RIGHTS_SID:
                    sid = owner
                if sid in _CREATOR_SIDS:
                    continue
                if sid == user and mask & _READ_MASK and mask & _WRITE_MASK:
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
            api.CloseHandle(handle)

    def restrict(path: Path) -> None:
        """Set ``path``'s access list to the user, SYSTEM and Administrators
        (full control; inherited by new children of a directory), protected
        from the parent's. Existing children keep theirs (nothing propagates).
        Raises :class:`OwnerOnlyError` for a link and ``OSError`` otherwise."""
        api = _api()
        path = Path(path)
        if _is_name_surrogate(path):
            raise OwnerOnlyError(0, f"{path} is a symbolic link or junction; it is never followed", str(path))
        handle = _open(path, _READ_CONTROL | _WRITE_DAC)
        descriptor = ctypes.c_void_p()
        try:
            inherit = "OICI" if path.is_dir() else ""
            sddl = "D:P" + "".join(
                f"(A;{inherit};FA;;;{sid})" for sid in (current_user_sid(), SYSTEM_SID, ADMINISTRATORS_SID)
            )
            if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
            ):
                raise _winerror("ConvertStringSecurityDescriptorToSecurityDescriptorW")
            # The handle form, as Hermes' hermes_dir_acl writes: no name lookup
            # after the checks in _open, and nothing propagates to existing
            # children (a junction planted inside is never walked). The list
            # is protected by its descriptor's control ("D:P").
            if not api.SetKernelObjectSecurity(handle, _DACL_SECURITY_INFORMATION, descriptor):
                raise _winerror(f"cannot set the access list of {path}")
        finally:
            if descriptor.value:
                api.LocalFree(descriptor)
            api.CloseHandle(handle)

else:

    def current_user_sid() -> str:
        raise OSError("Windows only")

    def inspect(path: Path) -> Access | None:
        """Windows only; ``None`` elsewhere (the mode bits say it there)."""
        return None

    def restrict(path: Path) -> None:
        raise OSError("Windows only")


def restrict_children(directory: Path) -> int:
    """Windows: :func:`restrict` each regular file directly in ``directory``
    whose list is not already private (links and other names skipped). The
    number changed; ``0`` elsewhere."""
    changed = 0
    children: list[Path] = []
    if sys.platform == "win32":
        with contextlib.suppress(OSError), os.scandir(directory) as entries:
            children = [Path(entry.path) for entry in entries if entry.is_file(follow_symlinks=False)]
    for child in children:
        current = inspect(child)
        if current is not None and current.private and current.protected:
            continue
        try:
            restrict(child)
            changed += 1
        except OSError as exc:
            logger.debug("owner_only: %s kept its access list (%s)", child, exc)
    return changed
