"""Tests for ``agentchatme_hermes.leader_lock``.

These tests exercise the real OS lock on a real tmp-path lock file:
``fcntl.flock`` on Linux and macOS, ``msvcrt.locking`` (``LockFile``) on
Windows. Every test runs on every platform: the CI matrix's Windows job is
where the Windows lock is proven.

The acquire/release contract is the only load-bearing surface, and the
properties the plugin relies on are each tested here (:class:`TestLockSemantics`):

* non-blocking: a held lock answers at once, busy;
* owned by the open file: a second descriptor of the same process is
  refused, as two profiles of one multiplexed gateway must be;
* across processes: another process holding it keeps this one out;
* released by the OS when the holder ends, cleanly or killed;
* not inherited by a child process;
* ``held_lock``'s held / busy / unavailable states;
* ``describe_lock_holder`` reports the right state without perturbing a
  held lock.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from typing import TYPE_CHECKING, Any

import pytest
from fake_keys import api_key

if TYPE_CHECKING:
    from pathlib import Path

from agentchatme_hermes.leader_lock import (
    GATEWAY_ENV_MARKER,
    LOCK_DIRNAME,
    _argv_matches_gateway_run,
    describe_lock_holder,
    held_lock,
    is_hermes_gateway_process,
    legacy_lock_path,
    lock_path_for,
    release_leader_lock,
    try_acquire_ws_leader_lock,
)
from agentchatme_hermes.profile_scope import account_fingerprint

_POSIX = sys.platform != "win32"


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


class TestTryAcquireLeaderLock:
    def test_first_caller_acquires(self, tmp_path: Path) -> None:
        lock = tmp_path / "agentchat-ws.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        assert lock.exists()
        release_leader_lock(fd)

    def test_second_caller_fails(self, tmp_path: Path) -> None:
        """Two concurrent acquires: only one wins.

        We don't spawn a subprocess (:class:`TestLockSemantics` does): the
        lock belongs to the open file on both platforms (flock: the open
        file description; Windows: the file handle), so the descriptor held
        by the first acquire blocks the second open-and-lock from the same
        process.
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

    @pytest.mark.skipif(not _POSIX, reason="POSIX modes (Windows: the access list, test_owner_only)")
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
        leader election. Created owner-only when it is the agentchat-locks
        dir (0700; on Windows the owner-only access list)."""
        lock = tmp_path / LOCK_DIRNAME / "ws-abc.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            assert lock.parent.is_dir()
            if _POSIX:
                assert (lock.parent.stat().st_mode & 0o777) == 0o700
            else:
                from agentchatme_hermes import owner_only

                access = owner_only.inspect(lock.parent)
                assert access is not None and access.private and access.protected
        finally:
            release_leader_lock(fd)

    def test_the_lock_file_stays_empty(self, tmp_path: Path) -> None:
        """Nothing is written into the lock file (no PID): on Windows the
        range locked lies past the end of the empty file, which LockFile
        allows."""
        lock = tmp_path / "agentchat-ws.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            assert lock.stat().st_size == 0
        finally:
            release_leader_lock(fd)
        assert lock.stat().st_size == 0


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


# ───────────────────── the lock's semantics, both platforms ─────────────────────

# A child process that takes the lock and reports, then holds it until its
# stdin closes (or it is killed).
_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from agentchatme_hermes.leader_lock import try_acquire_ws_leader_lock

    fd = try_acquire_ws_leader_lock(Path(sys.argv[1]))
    print("held" if fd is not None else "busy", flush=True)
    sys.stdin.readline()
    """
)


def _holder(lock: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _first_line(proc: subprocess.Popen[str]) -> str:
    assert proc.stdout is not None
    return proc.stdout.readline().strip()


def _acquire_within(lock: Path, seconds: float) -> int | None:
    """The lock, once free: Windows unlocks a dead holder's range "as
    resources allow", so a moment may pass after the process is gone."""
    deadline = time.monotonic() + seconds
    while True:
        fd = try_acquire_ws_leader_lock(lock)
        if fd is not None or time.monotonic() >= deadline:
            return fd
        time.sleep(0.05)


def _end(proc: subprocess.Popen[str]) -> None:
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            stream.close()
    proc.wait(timeout=30)


class TestLockSemantics:
    """What the plugin relies on, on the platform the suite runs on."""

    def test_another_process_holding_it_keeps_this_one_out(self, tmp_path: Path) -> None:
        lock = tmp_path / LOCK_DIRNAME / "ws-two.lock"
        proc = _holder(lock)
        try:
            assert _first_line(proc) == "held"
            assert try_acquire_ws_leader_lock(lock) is None, "the other process holds it"
            assert describe_lock_holder(lock) == "held"
            with held_lock(lock, timeout_s=0.2) as state:
                assert state == "busy"
        finally:
            _end(proc)
        fd = _acquire_within(lock, 10.0)
        assert fd is not None, "the holder exited: the lock is free"
        release_leader_lock(fd)
        assert describe_lock_holder(lock) == "free"

    def test_this_process_holding_it_keeps_another_out(self, tmp_path: Path) -> None:
        lock = tmp_path / LOCK_DIRNAME / "ws-two.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            proc = _holder(lock)
            try:
                assert _first_line(proc) == "busy"
            finally:
                _end(proc)
        finally:
            release_leader_lock(fd)

    def test_a_killed_holder_leaves_no_lock_behind(self, tmp_path: Path) -> None:
        """No cleanup runs in a killed process (SIGKILL; TerminateProcess on
        Windows): the OS releases its lock when its descriptors go."""
        lock = tmp_path / LOCK_DIRNAME / "ws-killed.lock"
        proc = _holder(lock)
        try:
            assert _first_line(proc) == "held"
            assert try_acquire_ws_leader_lock(lock) is None
            proc.kill()
            proc.wait(timeout=30)
        finally:
            _end(proc)
        fd = _acquire_within(lock, 10.0)
        assert fd is not None, "a killed holder must not keep the lock"
        release_leader_lock(fd)

    def test_two_descriptors_in_one_process_exclude_each_other(self, tmp_path: Path) -> None:
        """Two profiles of one multiplexed gateway, one key: the second
        descriptor is refused while the first holds it, through both entry
        points, and takes it once the first lets go."""
        lock = tmp_path / LOCK_DIRNAME / "ws-shared.lock"
        first = try_acquire_ws_leader_lock(lock)
        assert first is not None
        try:
            assert try_acquire_ws_leader_lock(lock) is None
            with held_lock(lock, timeout_s=0.1) as state:
                assert state == "busy"
        finally:
            release_leader_lock(first)
        with held_lock(lock, timeout_s=0.1) as state:
            assert state == "held"
            assert try_acquire_ws_leader_lock(lock) is None, "held_lock's descriptor holds it"
        second = try_acquire_ws_leader_lock(lock)
        assert second is not None, "released at the end of the block"
        release_leader_lock(second)

    def test_an_attempt_on_a_held_lock_answers_at_once(self, tmp_path: Path) -> None:
        lock = tmp_path / "agentchat-ws.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            started = time.monotonic()
            assert try_acquire_ws_leader_lock(lock) is None
            assert time.monotonic() - started < 1.0, "non-blocking"
        finally:
            release_leader_lock(fd)

    def test_the_descriptor_is_not_inherited(self, tmp_path: Path) -> None:
        """A child the gateway starts must never keep the lock alive."""
        lock = tmp_path / "agentchat-ws.lock"
        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            assert os.get_inheritable(fd) is False
        finally:
            release_leader_lock(fd)

    def test_held_lock_waits_for_a_holder_that_lets_go(self, tmp_path: Path) -> None:
        lock = tmp_path / LOCK_DIRNAME / "ws-wait.lock"
        proc = _holder(lock)
        try:
            assert _first_line(proc) == "held"
            assert proc.stdin is not None
            proc.stdin.write("\n")
            proc.stdin.flush()
            with held_lock(lock, timeout_s=15.0) as state:
                assert state == "held"
        finally:
            _end(proc)

    def test_held_lock_is_unavailable_where_no_lock_file_can_be_made(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "file"
        not_a_dir.write_text("", encoding="utf-8")
        with held_lock(not_a_dir / "signup.lock", timeout_s=0.1) as state:
            assert state == "unavailable"


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
        fp = account_fingerprint("https://api.agentchat.me", api_key("shared"))
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
        import pathlib

        # Path.home() reads USERPROFILE on Windows and HOME elsewhere; patch
        # the method rather than an env var so the test is platform-neutral.
        monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
        assert legacy_lock_path() == tmp_path / ".hermes" / "agentchat-ws.lock"


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
            account_fingerprint=account_fingerprint(base, api_key("growth")),
        )
        ops = lock_path_for(
            hermes_home=root / "profiles" / "ops",
            account_fingerprint=account_fingerprint(base, api_key("ops")),
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
        fp = account_fingerprint("https://api.agentchat.me", api_key("shared"))
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
            # Native Windows: the Scheduled Task's launcher and `hermes gateway
            # start` both run `<venv>\\Scripts\\python.exe -m hermes_cli.main
            # [--profile X] gateway run` (hermes_cli/gateway_windows.py
            # _gateway_run_argv); inside it argv[0] is the module's path and
            # Hermes has stripped the profile selector.
            [r"C:\Users\me\AppData\Local\hermes\hermes-agent\hermes_cli\main.py", "gateway", "run"],
            [r"C:\Users\me\AppData\Local\hermes\hermes-agent\hermes_cli\main.py", "gateway", "run", "--quiet"],
            # Hermes' launcher script runs the entry point inside `python -c`.
            ["-c", "gateway", "run"],
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
