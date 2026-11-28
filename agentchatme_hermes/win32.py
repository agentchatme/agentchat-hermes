"""The few Win32 calls the plugin makes, bound once (``ctypes``, stdlib only).

Windows only: on every other platform the module imports and its functions
raise ``OSError`` (or answer ``None`` where a caller asks "can this be told").
Argument and result types are pinned on each binding: the ``ctypes`` default
``int`` result truncates a 64-bit handle.

Used by :mod:`.owner_only` (access lists, owners, private files and
directories) and :mod:`.ws_health` (whether a process lives).
"""
from __future__ import annotations

import sys
from typing import Any

if sys.platform == "win32":
    import ctypes
    import functools
    from ctypes import wintypes

    INVALID_HANDLE = ctypes.c_void_p(-1).value
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87
    ERROR_ALREADY_EXISTS = 183
    ERROR_FILE_EXISTS = 80
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _TOKEN_ELEVATION_TYPE = 18
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _WAIT_TIMEOUT = 0x102

    class SidAndAttributes(ctypes.Structure):
        _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD))

    class SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        )

    class FileStandardInfo(ctypes.Structure):
        _fields_ = (
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BOOLEAN),
            ("Directory", wintypes.BOOLEAN),
        )

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = (("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD))

    class Acl(ctypes.Structure):
        _fields_ = (
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        )

    class AceHeader(ctypes.Structure):
        _fields_ = (("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wintypes.WORD))

    def _bind(function: Any, argtypes: tuple[Any, ...], restype: Any) -> Any:
        function.argtypes = argtypes
        function.restype = restype
        return function

    class Api:
        def __init__(self) -> None:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            a = ctypes.WinDLL("advapi32", use_last_error=True)
            vp, dword, wstr, boolean = ctypes.c_void_p, wintypes.DWORD, wintypes.LPCWSTR, wintypes.BOOL
            pvp, pdword = ctypes.POINTER(vp), ctypes.POINTER(dword)
            self.CreateFileW = _bind(k.CreateFileW, (wstr, dword, dword, vp, dword, dword, vp), vp)
            self.CreateDirectoryW = _bind(k.CreateDirectoryW, (wstr, vp), boolean)
            self.CloseHandle = _bind(k.CloseHandle, (vp,), boolean)
            self.LocalFree = _bind(k.LocalFree, (vp,), vp)
            self.GetCurrentProcess = _bind(k.GetCurrentProcess, (), vp)
            self.OpenProcess = _bind(k.OpenProcess, (dword, boolean, dword), vp)
            self.WaitForSingleObject = _bind(k.WaitForSingleObject, (vp, dword), dword)
            self.GetFileInformationByHandleEx = _bind(
                k.GetFileInformationByHandleEx, (vp, ctypes.c_int, vp, dword), boolean
            )
            self.GetFinalPathNameByHandleW = _bind(
                k.GetFinalPathNameByHandleW, (vp, wintypes.LPWSTR, dword, dword), dword
            )
            self.GetVolumeInformationByHandleW = _bind(
                k.GetVolumeInformationByHandleW,
                (vp, wintypes.LPWSTR, dword, vp, vp, pdword, wintypes.LPWSTR, dword),
                boolean,
            )
            self.OpenProcessToken = _bind(a.OpenProcessToken, (vp, dword, pvp), boolean)
            self.GetTokenInformation = _bind(a.GetTokenInformation, (vp, ctypes.c_int, vp, dword, pdword), boolean)
            self.ConvertSidToStringSidW = _bind(a.ConvertSidToStringSidW, (vp, pvp), boolean)
            self.ConvertStringSidToSidW = _bind(a.ConvertStringSidToSidW, (wstr, pvp), boolean)
            self.LookupAccountSidW = _bind(
                a.LookupAccountSidW,
                (wstr, vp, wintypes.LPWSTR, pdword, wintypes.LPWSTR, pdword, pdword),
                boolean,
            )
            self.GetSecurityInfo = _bind(a.GetSecurityInfo, (vp, ctypes.c_int, dword, pvp, pvp, pvp, pvp, pvp), dword)
            self.GetSecurityDescriptorControl = _bind(
                a.GetSecurityDescriptorControl, (vp, ctypes.POINTER(wintypes.WORD), pdword), boolean
            )
            self.GetAce = _bind(a.GetAce, (vp, dword, pvp), boolean)
            self.ConvertStringSecurityDescriptorToSecurityDescriptorW = _bind(
                a.ConvertStringSecurityDescriptorToSecurityDescriptorW, (wstr, dword, pvp, vp), boolean
            )
            self.SetKernelObjectSecurity = _bind(a.SetKernelObjectSecurity, (vp, dword, vp), boolean)

    @functools.lru_cache(maxsize=1)
    def api() -> Api:
        return Api()

    def error(what: str, code: int | None = None) -> OSError:
        """An ``OSError`` for the last Win32 error of this thread (or ``code``)."""
        code = ctypes.get_last_error() if code is None else code
        return OSError(0, f"{what}: {ctypes.FormatError(code).strip()}", None, code)

    def sid_string(sid: int | None) -> str:
        """``S-1-…`` for a SID pointer."""
        if not sid:
            raise OSError(0, "no SID to convert")
        text = ctypes.c_void_p()
        if not api().ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise error("ConvertSidToStringSidW")
        try:
            return ctypes.wstring_at(text.value or 0)
        finally:
            api().LocalFree(text)

    @functools.lru_cache(maxsize=256)
    def account_name(sid: str) -> str | None:
        """``DOMAIN\\name`` for a SID (the name alone when there is no domain),
        or ``None`` when Windows cannot name it (a deleted account, an
        unreachable domain)."""
        binary = ctypes.c_void_p()
        if not api().ConvertStringSidToSidW(sid, ctypes.byref(binary)):
            return None
        try:
            name = ctypes.create_unicode_buffer(256)
            domain = ctypes.create_unicode_buffer(256)
            name_size, domain_size, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
            if not api().LookupAccountSidW(
                None, binary, name, ctypes.byref(name_size), domain, ctypes.byref(domain_size), ctypes.byref(use)
            ):
                return None
            return f"{domain.value}\\{name.value}" if domain.value else name.value
        finally:
            api().LocalFree(binary)

    def _token_information(info_class: int) -> Any:
        token = ctypes.c_void_p()
        if not api().OpenProcessToken(api().GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise error("OpenProcessToken")
        try:
            size = wintypes.DWORD()
            api().GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(max(size.value, ctypes.sizeof(wintypes.DWORD)))
            if not api().GetTokenInformation(token, info_class, buffer, len(buffer), ctypes.byref(size)):
                raise error("GetTokenInformation")
            return buffer
        finally:
            api().CloseHandle(token)

    @functools.lru_cache(maxsize=1)
    def current_user_sid() -> str:
        """The SID of the user this process's token runs as (the same user
        elevated or not)."""
        return sid_string(SidAndAttributes.from_buffer(_token_information(_TOKEN_USER)).Sid)

    def split_admin_token() -> bool:
        """This account is an administrator running unelevated (UAC's limited
        half of a split token): an elevated prompt of this same account exists."""
        try:
            return int(wintypes.DWORD.from_buffer(_token_information(_TOKEN_ELEVATION_TYPE)).value) == 3
        except OSError:
            return False

    def pid_alive(pid: int) -> bool | None:
        """``OpenProcess`` the pid and wait 0 ms on it: running until it exits.
        Access denied means it exists (another user's); an invalid parameter
        means no such process. The shape of Hermes' own stdlib probe
        (``hermes_cli/_subprocess_compat.py`` ``win32_pid_exists``)."""
        handle = api().OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
        if not handle:
            code = ctypes.get_last_error()
            if code == ERROR_ACCESS_DENIED:
                return True
            return False if code == ERROR_INVALID_PARAMETER else None
        try:
            return bool(api().WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT)
        finally:
            api().CloseHandle(handle)

else:

    def current_user_sid() -> str:
        raise OSError("Windows only")

    def account_name(sid: str) -> str | None:
        return None

    def split_admin_token() -> bool:
        return False

    def pid_alive(pid: int) -> bool | None:
        return None
