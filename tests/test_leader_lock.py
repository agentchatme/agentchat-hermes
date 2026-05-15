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

from agentchatme_hermes.leader_lock import (
    GATEWAY_ENV_MARKER,
    _argv_matches_gateway_run,
    describe_lock_holder,
    is_hermes_gateway_process,
    release_leader_lock,
    try_acquire_ws_leader_lock,
)

# The fcntl-using tests only run on POSIX. The is_hermes_gateway_process
# and argv-detection tests are platform-independent and run everywhere.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl is POSIX-only",
)


class TestIsHermesGatewayProcess:
    def test_unset_env_and_innocent_argv_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GATEWAY_ENV_MARKER, raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes"])
        assert is_hermes_gateway_process() is False

    def test_env_set_to_one_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "1")
        monkeypatch.setattr(sys, "argv", ["hermes"])
        assert is_hermes_gateway_process() is True

    def test_argv_gateway_run_returns_true_even_without_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critical regression guard.

        Hermes' ``hermes_cli/main.py`` runs ``discover_plugins()``
        BEFORE ``gateway/run.py`` gets a chance to set the env var,
        so at ``register()`` time the env is unset even in the real
        gateway process. The argv fallback handles this case — 0.2.1
        without argv shipped a gateway-that-never-opens-a-WS bug.
        """
        monkeypatch.delenv(GATEWAY_ENV_MARKER, raising=False)
        monkeypatch.setattr(
            sys, "argv", ["hermes", "gateway", "run", "--replace"]
        )
        assert is_hermes_gateway_process() is True

    def test_env_set_to_other_value_falls_through_to_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "1" is the only canonical value. With argv also innocent,
        # we're not a gateway.
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "true")
        monkeypatch.setattr(sys, "argv", ["hermes"])
        assert is_hermes_gateway_process() is False

    def test_empty_env_falls_through_to_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "")
        monkeypatch.setattr(sys, "argv", ["hermes"])
        assert is_hermes_gateway_process() is False


class TestArgvMatchesGatewayRun:
    """Exhaustive coverage of the argv detection logic."""

    def test_bare_hermes_is_not_gateway(self) -> None:
        assert _argv_matches_gateway_run(["hermes"]) is False

    def test_hermes_gateway_run(self) -> None:
        assert _argv_matches_gateway_run(["hermes", "gateway", "run"]) is True

    def test_hermes_gateway_run_with_replace(self) -> None:
        assert (
            _argv_matches_gateway_run(
                ["hermes", "gateway", "run", "--replace"]
            )
            is True
        )

    def test_python_dash_m_form(self) -> None:
        """Hermes can also be invoked via ``python -m hermes_cli.main``."""
        argv = ["hermes_cli.main", "gateway", "run", "--replace"]
        assert _argv_matches_gateway_run(argv) is True

    def test_flag_between_gateway_and_run_matches(self) -> None:
        """``hermes gateway -p profile run`` matches.

        Any argv containing ``gateway`` is treated as gateway-class.
        Hermes only routes long-lived commands through plugin
        discovery, so we don't need finer parsing.
        """
        assert (
            _argv_matches_gateway_run(
                ["hermes", "gateway", "-p", "myprofile", "run"]
            )
            is True
        )

    def test_named_subcommand_is_not_gateway(self) -> None:
        assert (
            _argv_matches_gateway_run(["hermes", "agentchat", "status"])
            is False
        )

    def test_chat_is_not_gateway(self) -> None:
        assert _argv_matches_gateway_run(["hermes", "chat"]) is False

    def test_oneshot_is_not_gateway(self) -> None:
        assert _argv_matches_gateway_run(["hermes", "oneshot", "do"]) is False

    def test_no_gateway_token_is_false(self) -> None:
        """Absence of the token is the False signal."""
        assert _argv_matches_gateway_run(["hermes", "cron", "tick"]) is False
        assert _argv_matches_gateway_run(["hermes", "mcp", "serve"]) is False
        assert _argv_matches_gateway_run([]) is False


@_posix_only
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


@_posix_only
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


@_posix_only
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
