"""The restart a first install needs, started by the person's Allow (:mod:`agentchatme_hermes.gateway_restart`).

The plugin asks Hermes for its documented graceful restart (SIGUSR1) through a detached helper
(:mod:`agentchatme_hermes.restart_helper`) that then makes sure a gateway is up. These tests drive
the request with Popen recorded, and the helper against real child processes: one that exits on
SIGUSR1 and is left unreaped (the zombie a chat-started gateway becomes), one whose command line
says a chat started it, and a pid that a new process reuses.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

import pytest
from key_files import profile_home

from agentchatme_hermes import allow, gateway_restart, restart_helper

if TYPE_CHECKING:
    from pathlib import Path

posix_only = pytest.mark.skipif(os.name == "nt" or not hasattr(signal, "SIGUSR1"), reason="POSIX signals")


class _Spawned:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "Popen", self._popen)

    def _popen(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append({"argv": argv, **kwargs})
        return None


def _in_gateway(monkeypatch: pytest.MonkeyPatch, *, gateway: bool = True) -> None:
    from agentchatme_hermes import leader_lock

    monkeypatch.setattr(leader_lock, "is_hermes_gateway_process", lambda: gateway)


def _record_files() -> list[Path]:
    return sorted(gateway_restart._record_dir(profile_home()).glob(f"{gateway_restart.RECORD_PREFIX}*.json"))


@posix_only
class TestRequest:
    def test_in_the_gateway_it_spawns_the_helper_for_this_process_in_its_own_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _in_gateway(monkeypatch)
        spawned = _Spawned(monkeypatch)
        assert gateway_restart.request_load_restart(profile_home()) is True
        [call] = spawned.calls
        argv = call["argv"]
        assert argv[:3] == [sys.executable, "-I", str(gateway_restart.HELPER)]
        assert argv[argv.index("--pid") + 1] == str(os.getpid())
        assert argv[argv.index("--") + 1 :] == [sys.executable, "-m", "hermes_cli.main", "--profile", "default"]
        assert call["env"]["HERMES_HOME"] == str(profile_home().resolve())
        assert call["start_new_session"] is True and call["stdin"] == subprocess.DEVNULL
        [record] = _record_files()
        assert json.loads(record.read_text())["state"] == "requested"
        assert gateway_restart.restart_state(profile_home()) == "under_way"

    def test_a_named_profile_home_is_its_own_selector(self, tmp_path: Path) -> None:
        home = tmp_path / "root" / "profiles" / "work"
        home.mkdir(parents=True)
        command, env = gateway_restart._hermes_command(home)
        assert command == [sys.executable, "-m", "hermes_cli.main"]
        assert env["HERMES_HOME"] == str(home.resolve())

    def test_one_request_per_gateway_process_whichever_asks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _in_gateway(monkeypatch)
        spawned = _Spawned(monkeypatch)
        assert gateway_restart.request_load_restart(profile_home()) is True
        assert gateway_restart.request_load_restart(profile_home()) is True
        assert len(spawned.calls) == 1

    def test_a_failed_restart_can_be_asked_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _in_gateway(monkeypatch)
        spawned = _Spawned(monkeypatch)
        assert gateway_restart.request_load_restart(profile_home()) is True
        [record] = _record_files()
        record.write_text(json.dumps({**json.loads(record.read_text()), "state": "failed", "detail": "no ensure"}))
        assert gateway_restart.restart_state(profile_home()) == "failed"
        assert gateway_restart.restart_detail(profile_home()) == "no ensure"
        assert gateway_restart.request_load_restart(profile_home()) is True
        assert len(spawned.calls) == 2

    def test_from_a_persons_terminal_it_targets_the_gateway_hermes_sees(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _in_gateway(monkeypatch, gateway=False)
        monkeypatch.setattr(allow, "gateway_seen", lambda home: (4242, 99))
        spawned = _Spawned(monkeypatch)
        assert gateway_restart.request_load_restart(profile_home()) is True
        argv = spawned.calls[0]["argv"]
        assert argv[argv.index("--pid") + 1] == "4242"

    @pytest.mark.parametrize("seen", ["stopped", "unknown"])
    def test_no_gateway_no_restart(self, monkeypatch: pytest.MonkeyPatch, seen: str) -> None:
        _in_gateway(monkeypatch, gateway=False)
        monkeypatch.setattr(allow, "gateway_seen", lambda home: seen)
        spawned = _Spawned(monkeypatch)
        assert gateway_restart.request_load_restart(profile_home()) is False
        assert spawned.calls == [] and gateway_restart.restart_state(profile_home()) is None

    def test_never_from_a_root_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _in_gateway(monkeypatch)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        spawned = _Spawned(monkeypatch)
        assert gateway_restart.request_load_restart(profile_home()) is False
        assert spawned.calls == []

    def test_a_helper_that_cannot_start_is_said_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _in_gateway(monkeypatch)

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise OSError("no fork")

        monkeypatch.setattr(subprocess, "Popen", refuse)
        assert gateway_restart.request_load_restart(profile_home()) is False


def test_where_signals_are_missing_nothing_is_started(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_restart, "supported", lambda: False)
    spawned = _Spawned(monkeypatch)
    assert gateway_restart.request_load_restart(profile_home()) is False
    assert spawned.calls == []


def test_the_helper_needs_nothing_but_the_standard_library() -> None:
    """It runs with `python -I` outside Hermes' plugin loader: no import of this package."""
    source = gateway_restart.HELPER.read_text(encoding="utf-8")
    assert "agentchatme" not in source.split('"""', 2)[2]


@posix_only
class TestHelper:
    def _gateway(self, *extra: str) -> subprocess.Popen[bytes]:
        """A stand-in gateway: exits on SIGUSR1 (as Hermes' does once idle); never reaped by the helper."""
        code = "import signal, sys, time\nsignal.signal(signal.SIGUSR1, lambda *a: sys.exit(0))\ntime.sleep(60)\n"
        child = subprocess.Popen([sys.executable, "-c", code, *extra])
        time.sleep(0.5)
        return child

    def test_a_zombie_counts_as_ended(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        deadline = time.time() + 10
        while not restart_helper.ended(child.pid) and time.time() < deadline:
            time.sleep(0.1)
        assert restart_helper.ended(child.pid) is True, "an exited, unreaped child is ended"
        child.wait()

    def test_a_running_process_has_not_ended_and_a_reused_pid_has(self) -> None:
        child = self._gateway()
        try:
            started = restart_helper.fingerprint(child.pid)
            assert started
            assert restart_helper.ended(child.pid, started) is False
            assert restart_helper.ended(child.pid, "another process's start") is True
        finally:
            child.kill()
            child.wait()

    def test_signal_wait_then_ensure(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        child = self._gateway()
        status = tmp_path / "status.json"
        ensure = [sys.executable, "-c", "import sys; print('ready', sys.argv[1:])"]
        rc = restart_helper.main(
            ["--pid", str(child.pid), "--delay", "0", "--relaunch-wait", "0", "--status", str(status), "--", *ensure]
        )
        child.wait()
        out = capsys.readouterr().out
        assert rc == 0
        assert "(SIGUSR1)" in out and f"gateway {child.pid} ended" in out
        assert "gateway ensure exit 0" in out and "'gateway', 'ensure'" in out
        assert json.loads(status.read_text())["state"] == "ensured"

    def test_a_chat_started_gateway_is_left_to_its_chat(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        child = self._gateway("gateway", "run", "--idle-exit")
        assert restart_helper.started_by_a_chat(child.pid) is True
        status = tmp_path / "status.json"
        ensure = [sys.executable, "-c", "raise SystemExit('ensure must not run')"]
        rc = restart_helper.main(
            ["--pid", str(child.pid), "--delay", "0", "--relaunch-wait", "0", "--status", str(status), "--", *ensure]
        )
        child.wait()
        assert rc == 0
        assert "the chat starts the next one" in capsys.readouterr().out
        assert json.loads(status.read_text())["state"] == "left_to_chat"

    def test_a_gateway_that_cannot_be_signalled_is_a_failure_on_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        child = self._gateway()
        status = tmp_path / "status.json"
        real_kill = os.kill

        def refuse(pid: int, sig: int) -> None:
            if sig == signal.SIGUSR1:
                raise PermissionError("not yours")
            real_kill(pid, sig)

        monkeypatch.setattr(restart_helper.os, "kill", refuse)
        try:
            rc = restart_helper.main(["--pid", str(child.pid), "--delay", "0", "--status", str(status), "--", "hermes"])
        finally:
            monkeypatch.undo()
            child.kill()
            child.wait()
        assert rc == 1
        record = json.loads(status.read_text())
        assert record["state"] == "failed" and "not yours" in record["detail"]

    def test_the_wait_slows_down_but_never_gives_up(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        polls: list[float] = []
        checks = iter([False] * 5 + [True])
        clock = iter(range(0, 10_000_000, 1000))
        monkeypatch.setattr(restart_helper, "ended", lambda pid, started="": next(checks))
        monkeypatch.setattr(restart_helper, "fingerprint", lambda pid: "x")
        monkeypatch.setattr(restart_helper, "started_by_a_chat", lambda pid: True)
        monkeypatch.setattr(restart_helper.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(restart_helper.time, "monotonic", lambda: float(next(clock)))
        monkeypatch.setattr(restart_helper.time, "sleep", polls.append)
        status = tmp_path / "status.json"
        assert restart_helper.main(["--pid", "1", "--delay", "0", "--status", str(status), "--", "hermes"]) == 0
        assert restart_helper.SLOW_POLL_S in polls, "after an hour it keeps waiting, slower"

    def test_an_ensure_that_reports_draining_is_retried(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        child = self._gateway()
        os.kill(child.pid, signal.SIGUSR1)
        child.wait()
        monkeypatch.setattr(restart_helper.time, "sleep", lambda s: None)
        counter = tmp_path / "ensure-count"
        ensure = [
            sys.executable, "-c",
            "import os, sys\n"
            f"p = {str(counter)!r}\n"
            "n = int(open(p).read()) + 1 if os.path.exists(p) else 1\n"
            "open(p, 'w').write(str(n))\n"
            "sys.exit(6 if n == 1 else 0)\n",
        ]
        rc = restart_helper.main(["--pid", str(child.pid), "--delay", "0", "--relaunch-wait", "0", "--", *ensure])
        assert rc == 0
        assert counter.read_text() == "2"
        assert capsys.readouterr().out.count("gateway ensure exit") == 2
