"""The test network guard (``conftest.py``) itself: a non-loopback connect or
lookup fails the test that tried, even when the code swallowed the error;
loopback stays open."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

pytest_plugins = ["pytester"]

_CONFTEST = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
_INI = "[pytest]\nasyncio_default_fixture_loop_scope = function\n"


def test_a_swallowed_non_loopback_connect_still_fails_the_test(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_CONFTEST)
    pytester.makeini(_INI)
    pytester.makepyfile(
        """
        import socket

        def test_swallows_a_connect():
            try:
                socket.create_connection(("203.0.113.7", 443), timeout=1)
            except OSError:
                pass

        def test_swallows_a_lookup():
            try:
                socket.getaddrinfo("api.example.com", 443)
            except OSError:
                pass

        def test_loopback_is_open():
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            client = socket.create_connection(server.getsockname(), timeout=1)
            client.close()
            server.close()
        """
    )
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=3, errors=2)
    result.stdout.fnmatch_lines(["*the test tried to reach the network*"])
