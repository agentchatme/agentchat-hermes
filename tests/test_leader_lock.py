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
    LOCK_DIRNAME,
    _argv_matches_gateway_run,
    describe_lock_holder,
    is_hermes_gateway_process,
    legacy_lock_path,
    lock_path_for,
    release_leader_lock,
    try_acquire_ws_leader_lock,
)
from agentchatme_hermes.profile_scope import account_fingerprint

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

    def test_env_marker_alone_is_not_enough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inherited marker alone no longer makes a leader candidate:
        every child the gateway spawns inherits it, and a child that won
        the lock would drain and ack the backlog into its own memory and
        then exit."""
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "1")
        monkeypatch.setattr(sys, "argv", ["hermes"])
        assert is_hermes_gateway_process() is False

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

    def test_missing_lock_directory_is_created(self, tmp_path: Path) -> None:
        """The lock directory is ours — a fresh profile root must not block
        leader election. Created 0700 when it is the agentchat-locks dir."""
        lock = tmp_path / LOCK_DIRNAME / "ws-abc.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            assert lock.parent.is_dir()
            assert (lock.parent.stat().st_mode & 0o777) == 0o700
        finally:
            release_leader_lock(fd)


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


# ───────────────────── lock_path_for: per-account scoping ─────────────────────


class TestLockPathFor:
    def test_lives_under_the_hermes_root_not_the_profile(self, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        path = lock_path_for(
            hermes_home=root / "profiles" / "growth", account_fingerprint="abcd"
        )
        assert path == root / LOCK_DIRNAME / "ws-abcd.lock"

    def test_default_profile_uses_its_own_home_as_root(self, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        assert lock_path_for(hermes_home=root, account_fingerprint="abcd") == (
            root / LOCK_DIRNAME / "ws-abcd.lock"
        )

    def test_same_account_two_profiles_share_one_lock(self, tmp_path: Path) -> None:
        """Cloned credentials must still yield exactly one live socket."""
        root = tmp_path / ".hermes"
        fp = account_fingerprint("https://api.agentchat.me", "ac_live_shared")
        a = lock_path_for(hermes_home=root / "profiles" / "a", account_fingerprint=fp)
        b = lock_path_for(hermes_home=root / "profiles" / "b", account_fingerprint=fp)
        assert a == b

    def test_different_accounts_never_share_a_lock(self, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        a = lock_path_for(
            hermes_home=root / "profiles" / "a",
            account_fingerprint=account_fingerprint("https://api.agentchat.me", "k1"),
        )
        b = lock_path_for(
            hermes_home=root / "profiles" / "b",
            account_fingerprint=account_fingerprint("https://api.agentchat.me", "k2"),
        )
        assert a != b
        assert a.parent == b.parent

    def test_lock_dir_override(self, tmp_path: Path) -> None:
        path = lock_path_for(
            hermes_home=tmp_path / ".hermes",
            account_fingerprint="abcd",
            lock_dir=tmp_path / "run",
        )
        assert path == tmp_path / "run" / "ws-abcd.lock"

    def test_legacy_path_is_the_old_per_machine_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert legacy_lock_path() == tmp_path / ".hermes" / "agentchat-ws.lock"


@_posix_only
class TestTwoProfilesOneMachine:
    """Reproduction of the September 2026 fleet failure, now passing.

    Chief, Growth and Operations ran as separate Hermes profiles under one
    OS home. With a per-machine lock only the first to start received.
    With per-account locks every profile holds its own socket, while two
    profiles on one key still elect a single leader.
    """

    def test_distinct_accounts_both_acquire(self, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        base = "https://api.agentchat.me"
        growth = lock_path_for(
            hermes_home=root / "profiles" / "growth",
            account_fingerprint=account_fingerprint(base, "ac_live_growth"),
        )
        ops = lock_path_for(
            hermes_home=root / "profiles" / "ops",
            account_fingerprint=account_fingerprint(base, "ac_live_ops"),
        )
        fd_growth = try_acquire_ws_leader_lock(growth)
        fd_ops = try_acquire_ws_leader_lock(ops)
        try:
            assert fd_growth is not None
            assert fd_ops is not None, "second profile must receive too"
        finally:
            for fd in (fd_growth, fd_ops):
                if fd is not None:
                    release_leader_lock(fd)

    def test_same_account_second_profile_follows_until_release(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / ".hermes"
        fp = account_fingerprint("https://api.agentchat.me", "ac_live_shared")
        a = lock_path_for(hermes_home=root / "profiles" / "a", account_fingerprint=fp)
        b = lock_path_for(hermes_home=root / "profiles" / "b", account_fingerprint=fp)
        fd_a = try_acquire_ws_leader_lock(a)
        assert fd_a is not None
        try:
            assert try_acquire_ws_leader_lock(b) is None
            assert describe_lock_holder(b) == "held"
        finally:
            release_leader_lock(fd_a)
        fd_b = try_acquire_ws_leader_lock(b)
        assert fd_b is not None
        release_leader_lock(fd_b)


class TestArgvGatewayRunOnly:
    """Only the long-lived `hermes gateway run` process may own a socket."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["hermes", "gateway", "run"],
            ["hermes", "gateway", "run", "--replace"],
            ["hermes", "gateway", "-p", "growth", "run"],
            ["hermes", "gateway", "--profile", "growth", "run"],
            ["hermes", "gateway", "--replace", "run"],
            ["python", "-m", "hermes_cli.main", "gateway", "run"],
            ["hermes", "gateway"],  # legacy spelling of "start the gateway"
        ],
    )
    def test_gateway_run_forms_are_candidates(self, argv: list[str]) -> None:
        assert _argv_matches_gateway_run(argv) is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["hermes", "gateway", "status"],
            ["hermes", "gateway", "stop"],
            ["hermes", "gateway", "restart"],
            ["hermes", "gateway", "list"],
            ["hermes", "gateway", "install"],
            ["hermes", "gateway", "migrate", "--multiplex"],
            ["hermes", "gateway", "-p", "growth", "status"],
            ["hermes", "agentchat", "doctor"],
            ["hermes", "agentchat", "status"],
            ["hermes", "chat"],
            ["hermes"],
        ],
    )
    def test_short_lived_commands_are_never_candidates(self, argv: list[str]) -> None:
        assert _argv_matches_gateway_run(argv) is False
