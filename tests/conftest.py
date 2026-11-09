"""Shared test fixtures.

The network guard: no test may reach a network beyond this machine. Every
socket connect and every name lookup for a non-loopback destination is
refused (``NetworkAccessBlocked``, an ``OSError``), and the test that tried
fails — even when the code under test swallowed the error. Loopback and
local (AF_UNIX) sockets stay open for local fake servers.

The home guard: every test runs with ``HOME`` / ``USERPROFILE`` /
``HERMES_HOME`` pointing at its own temporary directory, so nothing the
plugin writes by default (the health record, the leader locks, ``.env``,
``SOUL.md``) can land in the real home of whoever runs the suite — on a
Hermes box, that is the live agent's.

The thread guard: a test that leaves one of the plugin's threads
(``agentchat-…``) running fails. A leaked leader-retry thread outlives the
test's mocks, wins the real lock later and starts a real receiver in
whatever test runs then.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import stat
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
