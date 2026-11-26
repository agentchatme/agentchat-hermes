"""Shared test fixtures.

The network guard: no test may reach a network beyond this machine. Every
socket connect and every name lookup for a non-loopback destination is
refused (``NetworkAccessBlocked``, an ``OSError``), and the test that tried
fails — even when the code under test swallowed the error. Loopback and
local (AF_UNIX) sockets stay open for local fake servers.

The home guard: every test runs with ``HOME`` / ``USERPROFILE`` /
``HERMES_HOME`` pointing at its own temporary directory, so nothing the
plugin writes by default (the health record, the leader locks, the key
file, ``.env``, ``SOUL.md``) can land in the real home of whoever runs the suite — on a
Hermes box, that is the live agent's.

The thread guard: a test that leaves one of the plugin's threads
(``agentchat-…``) running fails. A leaked leader-retry thread outlives the
test's mocks, wins the real lock later and starts a real receiver in
whatever test runs then.
"""
from __future__ import annotations

import builtins
import io
import ipaddress
import os
import socket
import stat
import sys
import sysconfig
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", ""})


class NetworkAccessBlocked(OSError):
    """Raised by the network guard for a non-loopback connect or lookup."""


def _is_loopback_host(host: Any) -> bool:
    if host is None:
        return True  # getaddrinfo(None, port): this machine
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.strip().strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_loopback_address(address: Any) -> bool:
    if isinstance(address, (str, bytes)):
        return True  # AF_UNIX: a local path
    if isinstance(address, tuple) and address:
        return _is_loopback_host(address[0])
    return False


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def refuse(what: str) -> NetworkAccessBlocked:
        attempts.append(what)
        return NetworkAccessBlocked(f"network access blocked in tests: {what}")

    def connect(self: socket.socket, address: Any) -> None:
        if not _is_loopback_address(address):
            raise refuse(f"connect {address!r}")
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_loopback_address(address):
            raise refuse(f"connect_ex {address!r}")
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback_host(host):
            raise refuse(f"getaddrinfo {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    if attempts:
        pytest.fail("the test tried to reach the network: " + "; ".join(attempts[:5]), pytrace=False)


@pytest.fixture(autouse=True)
def _home_guard(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))


@pytest.fixture(autouse=True)
def _copies_found() -> Iterator[None]:
    """What a process found about a profile holding a copy of another's key
    (``config.copy_of``) is kept for the process: each test starts without."""
    from agentchatme_hermes import config

    config.reset_copies_for_tests()
    yield
    config.reset_copies_for_tests()


class DotenvReadBlocked(OSError):
    """Raised by the ``.env`` guard: the plugin opened a ``.env`` file itself."""


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__import__("agentchatme_hermes").__file__))
_STDLIB_DIRS = tuple(
    {os.path.abspath(p) for p in (sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"])}
)


def _opened_by_the_plugin() -> bool:
    """Is the nearest caller outside the standard library this plugin's own code?"""
    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith("<") or os.path.abspath(filename).startswith(_STDLIB_DIRS):
            frame = frame.f_back
            continue
        return os.path.abspath(filename).startswith(_PACKAGE_DIR)
    return False


def _names_a_dotenv(path: Any) -> bool:
    try:
        name = os.path.basename(os.fsdecode(path))
    except TypeError:
        return False
    return name == ".env"


@pytest.fixture(autouse=True)
def _dotenv_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The plugin never opens a ``.env`` file (Nous review, ask 1): settings
    come from Hermes' ``get_secret``, lines are removed and written by Hermes'
    ``remove_env_value`` / ``save_env_value``. Any open of a file named
    ``.env`` whose nearest non-stdlib caller is the plugin's own code fails
    the test — even when the code under test swallowed the error. Test
    doubles for Hermes (``tests/``) may."""
    attempts: list[str] = []
    real_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open

    def check(path: Any) -> None:
        if _names_a_dotenv(path) and _opened_by_the_plugin():
            attempts.append(os.fsdecode(path))
            raise DotenvReadBlocked(f"the plugin opened a .env file: {os.fsdecode(path)}")

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        check(file)
        return real_open(file, *args, **kwargs)

    def guarded_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        check(file)
        return real_io_open(file, *args, **kwargs)

    def guarded_os_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        check(path)
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    yield
    if attempts:
        pytest.fail("the plugin opened a .env file: " + "; ".join(attempts[:5]), pytrace=False)


@pytest.fixture(autouse=True)
def _manual_sync_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime syncs the manual by itself: once on a daemon thread when it
    is not the leader, and through the kit's ``ManualSync`` on the leader's
    receive loop. Either would download the manual the test's texts announce
    — a network attempt the guard fails, from a thread the test does not
    own. Off by default; ``test_manual.py`` turns them back on
    (:func:`manual_sync_on`) where it tests them."""
    from agentchatme_hermes import manual

    monkeypatch.setattr(manual, "sync_in_background", lambda **_kw: None)
    monkeypatch.setattr(manual, "manual_sync_for", lambda *_a, **_kw: None)


_REAL_MANUAL_SYNC: dict[str, Any] = {}


def _remember_real_manual_sync() -> None:
    from agentchatme_hermes import manual

    _REAL_MANUAL_SYNC.setdefault("sync_in_background", manual.sync_in_background)
    _REAL_MANUAL_SYNC.setdefault("manual_sync_for", manual.manual_sync_for)


_remember_real_manual_sync()


@pytest.fixture
def manual_sync_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo :func:`_manual_sync_off` for one test."""
    from agentchatme_hermes import manual

    for name, real in _REAL_MANUAL_SYNC.items():
        monkeypatch.setattr(manual, name, real)


_THREAD_GRACE_S = 2.0


@pytest.fixture(autouse=True)
def _thread_guard() -> Iterator[None]:
    before = {thread.ident for thread in threading.enumerate()}

    def leaked() -> list[threading.Thread]:
        return [
            thread
            for thread in threading.enumerate()
            if thread.ident not in before and thread.is_alive() and thread.name.startswith("agentchat")
        ]

    yield
    deadline = time.monotonic() + _THREAD_GRACE_S
    while leaked() and time.monotonic() < deadline:
        time.sleep(0.02)
    left = leaked()
    if left:
        pytest.fail(
            "the test left the plugin's threads running: " + ", ".join(sorted(t.name for t in left)),
            pytrace=False,
        )


@pytest.fixture
def assert_private_file() -> Callable[[Path], None]:
    """Assert a file the plugin wrote is owner-only (``0600``).

    POSIX only: Windows reports ``0o666`` for every file whatever ``chmod``
    asked for, so the mode bits say nothing there. The production code
    still opens and ``chmod``s with ``0o600`` on every platform; only the
    assertion is conditional (the pattern ``test_ws_health`` uses inline).
    """

    def check(path: Path) -> None:
        assert path.exists(), f"{path} was not written"
        if os.name == "nt":
            return
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"{path}: expected 0600, got {oct(mode)}"

    return check
