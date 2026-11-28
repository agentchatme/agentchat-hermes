"""The restart a first install needs, started by the person's Allow (:mod:`agentchatme_hermes.gateway_restart`).

The plugin asks Hermes for its documented graceful restart (SIGUSR1) through a detached helper
(:mod:`agentchatme_hermes.restart_helper`) that then makes sure a gateway is up (`hermes gateway
ensure`). These tests drive the request with Popen recorded, and the helper against real child
processes: one that exits on SIGUSR1 and is left unreaped, the zombie a chat-started gateway
becomes (lab, 2026-11-28).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any

import pytest
from key_files import profile_home

from agentchatme_hermes import allow, gateway_restart, restart_helper
from agentchatme_hermes.key_store import store_dir

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
        assert argv[argv.index("--") + 1 :] == [sys.executable, "-m", "hermes_cli.main"]
        assert call["start_new_session"] is True and call["stdin"] == subprocess.DEVNULL
        assert (store_dir(profile_home()) / gateway_restart.RECORD_FILENAME).exists()

    def test_a_second_request_for_the_same_gateway_starts_nothing_new(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _in_gateway(monkeypatch)
        spawned = _Spawned(monkeypatch)
        assert gateway_restart.request_load_restart(profile_home()) is True
        assert gateway_restart.request_load_restart(profile_home()) is True
        assert len(spawned.calls) == 1

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
    def _gateway(self) -> subprocess.Popen[bytes]:
        """A stand-in gateway: exits on SIGUSR1 (as Hermes' does once idle); never reaped by the helper."""
        code = "import signal, sys, time\nsignal.signal(signal.SIGUSR1, lambda *a: sys.exit(0))\ntime.sleep(60)\n"
        child = subprocess.Popen([sys.executable, "-c", code])
        time.sleep(0.5)
        return child

    def test_a_zombie_counts_as_ended(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        deadline = time.time() + 10
        while not restart_helper.ended(child.pid) and time.time() < deadline:
            time.sleep(0.1)
        assert restart_helper.ended(child.pid) is True, "an exited, unreaped child is ended"
        child.wait()

    def test_a_running_process_has_not_ended(self) -> None:
        child = self._gateway()
        try:
            assert restart_helper.ended(child.pid) is False
        finally:
            child.kill()
            child.wait()

    def test_signal_wait_then_ensure(self, capsys: pytest.CaptureFixture[str]) -> None:
        child = self._gateway()
        ensure = [sys.executable, "-c", "import sys; print('ready', sys.argv[1:])"]
        rc = restart_helper.main(
            ["--pid", str(child.pid), "--delay", "0", "--relaunch-wait", "0", "--", *ensure]
        )
        child.wait()
        out = capsys.readouterr().out
        assert rc == 0
        assert "asked gateway" in out and "(SIGUSR1)" in out
        assert f"gateway {child.pid} ended" in out
        assert "gateway ensure exit 0" in out and "'gateway', 'ensure'" in out

    def test_an_ensure_that_reports_draining_is_retried(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        child = self._gateway()
        monkeypatch.setattr(restart_helper.time, "sleep", lambda s: None)
        counter = str(tmp_path / "ensure-count")
        ensure = [
            sys.executable, "-c",
            "import os, sys\n"
            f"p = {counter!r}\n"
            "n = int(open(p).read()) + 1 if os.path.exists(p) else 1\n"
            "open(p, 'w').write(str(n))\n"
            "sys.exit(6 if n == 1 else 0)\n",
        ]
        deadline = time.time() + 10
        os.kill(child.pid, signal.SIGUSR1)
        while child.poll() is None and time.time() < deadline:
            pass
        rc = restart_helper.main(["--pid", str(child.pid), "--delay", "0", "--relaunch-wait", "0", "--", *ensure])
        assert rc == 0
        assert (tmp_path / "ensure-count").read_text() == "2"
        assert capsys.readouterr().out.count("gateway ensure exit") == 2
