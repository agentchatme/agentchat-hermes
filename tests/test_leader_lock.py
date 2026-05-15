"""Tests for ``agentchatme_hermes.leader_lock``.

These tests exercise the actual ``fcntl.flock`` machinery on a real
tmp-path lock file. They're POSIX-only (skipped on Windows) but the
plugin is POSIX-only too, so that matches deployment reality.

The acquire/release contract is the only load-bearing surface:

* First caller acquires successfully → returns fd
* Second concurrent caller fails fast → returns None
* After first releases → second acquire succeeds
* ``is_hermes_gateway_process`` reflects the env var
* ``describe_lock_holder`` reports the right state without
  perturbing held locks
"""
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

if sys.platform == "win32":
    pytest.skip("leader_lock is POSIX-only", allow_module_level=True)

from agentchatme_hermes.leader_lock import (
    GATEWAY_ENV_MARKER,
    describe_lock_holder,
    is_hermes_gateway_process,
    release_leader_lock,
    try_acquire_ws_leader_lock,
)


class TestIsHermesGatewayProcess:
    def test_unset_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GATEWAY_ENV_MARKER, raising=False)
        assert is_hermes_gateway_process() is False

    def test_set_to_one_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "1")
        assert is_hermes_gateway_process() is True

    def test_set_to_other_value_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hermes only writes the literal "1". Anything else is not the
        # canonical marker — defensive against a future setting of
        # "true" / "0" / etc.
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "true")
        assert is_hermes_gateway_process() is False

    def test_empty_string_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "")
        assert is_hermes_gateway_process() is False


class TestTryAcquireLeaderLock:
    def test_first_caller_acquires(self, tmp_path: Path) -> None:
        lock = tmp_path / "agentchat-ws.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        assert lock.exists()
        release_leader_lock(fd)

    def test_second_caller_fails(self, tmp_path: Path) -> None:
        """Two concurrent acquires: only one wins.

        We don't spawn a subprocess — ``fcntl.flock`` is per-fd, not
        per-process, but the same fd held by the first acquire still
        blocks the second open+flock attempt from the same process
        (POSIX semantics for ``LOCK_EX | LOCK_NB``).
        """
        lock = tmp_path / "agentchat-ws.lock"
        first = try_acquire_ws_leader_lock(lock)
        assert first is not None
        try:
            second = try_acquire_ws_leader_lock(lock)
            assert second is None, (
                "second acquire should return None while first holds "
                "the lock"
            )
        finally:
            release_leader_lock(first)

    def test_release_then_reacquire(self, tmp_path: Path) -> None:
        lock = tmp_path / "agentchat-ws.lock"
        first = try_acquire_ws_leader_lock(lock)
        assert first is not None
        release_leader_lock(first)

        second = try_acquire_ws_leader_lock(lock)
        assert second is not None, (
            "second acquire should succeed once the first is released"
        )
        release_leader_lock(second)

    def test_lock_file_mode_is_0o600(self, tmp_path: Path) -> None:
        """Lock file must NOT be world-readable.

        Adjacent files in ~/.hermes (notably .env with the API key)
        are 0600; we don't want the lock file to set a precedent for
        looser permissions in this directory.
        """
        lock = tmp_path / "agentchat-ws.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            mode = lock.stat().st_mode & 0o777
            assert mode == 0o600, f"expected 0600, got {oct(mode)}"
        finally:
            release_leader_lock(fd)

    def test_directory_missing_returns_none(self, tmp_path: Path) -> None:
        """Acquire on a path whose parent dir doesn't exist."""
        lock = tmp_path / "does-not-exist" / "agentchat-ws.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is None


class TestDescribeLockHolder:
    def test_not_present_when_file_missing(self, tmp_path: Path) -> None:
        lock = tmp_path / "agentchat-ws.lock"
        assert describe_lock_holder(lock) == "not present"

    def test_free_when_no_one_holds(self, tmp_path: Path) -> None:
        lock = tmp_path / "agentchat-ws.lock"
        # Create the file but don't hold it.
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        release_leader_lock(fd)
        assert describe_lock_holder(lock) == "free"

    def test_held_when_someone_holds(self, tmp_path: Path) -> None:
        lock = tmp_path / "agentchat-ws.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            assert describe_lock_holder(lock) == "held"
        finally:
            release_leader_lock(fd)

    def test_probe_does_not_steal_lock(self, tmp_path: Path) -> None:
        """Describe must be read-only — it can't release a held lock.

        If describe accidentally calls LOCK_UN on someone else's lock,
        a concurrent caller would then be able to acquire. Verify that
        after describe, the original holder still has the lock.
        """
        lock = tmp_path / "agentchat-ws.lock"
        holder_fd = try_acquire_ws_leader_lock(lock)
        assert holder_fd is not None
        try:
            assert describe_lock_holder(lock) == "held"
            # Try to acquire — must still fail because holder hasn't
            # released and describe shouldn't have released either.
            intruder_fd = try_acquire_ws_leader_lock(lock)
            assert intruder_fd is None, (
                "describe_lock_holder must not release another "
                "process's lock"
            )
        finally:
            release_leader_lock(holder_fd)


class TestReleaseLeaderLock:
    def test_release_invalid_fd_does_not_raise(self) -> None:
        """Double-release / release of closed fd must be silent."""
        # Make a real fd, close it, then call release.
        r_fd, w_fd = os.pipe()
        os.close(r_fd)
        # Should not raise — the function swallows OSError at DEBUG.
        release_leader_lock(r_fd)
        # Cleanup.
        os.close(w_fd)


def _set_env(name: str, value: str, monkeypatch: Any) -> None:
    monkeypatch.setenv(name, value)
