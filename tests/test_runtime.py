"""Tests for ``agentchatme_hermes.runtime``.

Three modes need to behave correctly:

* **Leader** (gateway_mode=True, lock acquired) — full runtime: WS
  daemon + invoker + sync client + identity.
* **Follower-gateway** (gateway_mode=True, lock NOT acquired) — light
  runtime, loud warning, no WS, no invoker. This is the path a
  second concurrent gateway hits.
* **Follower-cli** (gateway_mode=False) — light runtime, info log,
  no WS, no invoker. TUI / named CLI / one-shot.

We patch the heavy components (HTTP client, WS daemon, invoker) so
the tests can exercise the wiring without real network IO. The
leader-lock acquire path is patched per-test so we can drive both
"won the race" and "lost the race" without touching the filesystem.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_runtime_singleton() -> Any:
    """Each test gets a fresh module-level singleton."""
    from agentchatme_hermes import runtime

    runtime.reset_for_tests()
    yield
    runtime.reset_for_tests()


def _config() -> Any:
    return SimpleNamespace(
        api_key="ac_live_test",
        api_base="https://api.example.test",
        ws_url="wss://api.example.test/v1/ws",
        max_inflight_turns=2,
    )


@contextlib.contextmanager
def _patched_runtime(*, lock_winner: bool = True) -> Any:
    """Yield a context where Runtime's heavy components are mocked.

    ``lock_winner`` controls whether the patched ``try_acquire_ws_leader_lock``
    returns a fake fd (won the race → leader path) or ``None`` (lost
    the race → follower-gateway path).
    """
    from agentchatme_hermes.runtime import Runtime

    fake_client = MagicMock()
    fake_client.get_me.return_value = {"handle": "alice"}

    ws_cls = MagicMock()
    invoker_cls = MagicMock()
    acquire = MagicMock(return_value=99 if lock_winner else None)
    release = MagicMock()

    with patch(
        "agentchatme_hermes.runtime.WSDaemon", ws_cls
    ), patch(
        "agentchatme_hermes.runtime.AgentInvoker", invoker_cls
    ), patch(
        "agentchatme_hermes.runtime.try_acquire_ws_leader_lock", acquire
    ), patch(
        "agentchatme_hermes.runtime.release_leader_lock", release
    ), patch.object(
        Runtime, "_build_sync_client", return_value=fake_client
    ):
        yield ws_cls, invoker_cls, fake_client, acquire, release


class TestRuntimeStartLeaderMode:
    def test_leader_starts_ws_and_invoker(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (
            ws_cls, invoker_cls, fake_client, acquire, _release,
        ):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()

            assert acquire.called, (
                "leader election must be attempted when gateway_mode=True"
            )
            assert ws_cls.called
            assert invoker_cls.called
            ws_cls.return_value.start.assert_called_once()
            invoker_cls.return_value.start.assert_called_once()

            assert rt.identity.handle == "alice"
            assert rt.client is fake_client
            assert rt.is_leader is True


class TestRuntimeStartFollowerGateway:
    """Gateway-class process that LOST the leader-lock race."""

    def test_lost_lock_skips_ws_and_invoker(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=False) as (
            ws_cls, invoker_cls, _fake_client, acquire, _release,
        ):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()

            assert acquire.called, (
                "we should still attempt the acquire so we know we lost"
            )
            assert not ws_cls.called, (
                "follower-gateway must NOT open a WS — fixes the "
                "multi-WS-per-machine bug"
            )
            assert not invoker_cls.called
            assert rt.is_leader is False
            assert rt.identity.handle == "alice"


class TestRuntimeStartFollowerCli:
    def test_cli_skips_ws_and_invoker_and_lock(self) -> None:
        """gateway_mode=False (TUI/CLI/oneshot): no WS, no lock attempt."""
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (
            ws_cls, invoker_cls, _fake_client, acquire, _release,
        ):
            rt = Runtime(_config(), gateway_mode=False)
            rt.start()

            assert not acquire.called, (
                "CLI mode must NOT touch the leader lock — saves an "
                "open/flock syscall pair per short-lived invocation"
            )
            assert not ws_cls.called
            assert not invoker_cls.called
            assert rt.is_leader is False

    def test_cli_queue_is_unavailable(self) -> None:
        """In CLI mode, accessing the queue raises (it doesn't exist)."""
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime():
            rt = Runtime(_config(), gateway_mode=False)
            rt.start()

            with pytest.raises(RuntimeError):
                _ = rt.queue


class TestRuntimeStopReleasesLock:
    def test_leader_stop_releases_lock(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=True) as (
            _ws, _inv, _client, _acquire, release,
        ):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()
            assert rt.is_leader is True
            rt.stop()

            # Should have released exactly the fd we acquired (99).
            release.assert_called_once_with(99)
            assert rt.is_leader is False

    def test_follower_stop_does_not_release(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime(lock_winner=False) as (
            _ws, _inv, _client, _acquire, release,
        ):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()
            rt.stop()
            assert not release.called, (
                "follower never held the lock — releasing the wrong fd "
                "would be a bug"
            )


class TestGetRuntimeIdempotency:
    def test_returns_same_instance(self) -> None:
        from agentchatme_hermes.runtime import get_runtime

        with _patched_runtime():
            r1 = get_runtime(_config(), gateway_mode=True)
            r2 = get_runtime(_config(), gateway_mode=True)
            assert r1 is r2

    def test_first_construction_wins(self) -> None:
        """``gateway_mode`` is honored on first construct only."""
        from agentchatme_hermes.runtime import get_runtime

        with _patched_runtime() as (ws_cls, _invoker_cls, _fake_client, _acq, _rel):
            r1 = get_runtime(_config(), gateway_mode=False)
            r1.start()
            assert not ws_cls.called

            # Second call says gateway_mode=True but we already have a
            # CLI runtime — that's intentional, the mode is fixed for
            # the process.
            r2 = get_runtime(_config(), gateway_mode=True)
            assert r2 is r1
            assert not ws_cls.called
