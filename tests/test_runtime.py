"""Tests for ``agentchatme_hermes.runtime``.

Two modes need to behave correctly:

* ``gateway_mode=True`` — full runtime (WS daemon + invoker).
* ``gateway_mode=False`` — light runtime: identity + sync client
  only. The WS daemon and invoker must NOT be constructed. This is
  the fix for the multi-WS-per-machine bug.

We patch the heavy components (HTTP client, WS daemon, invoker) so
the tests can exercise the wiring without real network IO.
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
def _patched_runtime() -> Any:
    """Yield a context where Runtime's heavy components are mocked.

    Yields ``(ws_cls, invoker_cls, fake_client)``: the patched class
    objects (so tests can assert on calls) and the sync-client mock.
    """
    from agentchatme_hermes.runtime import Runtime

    fake_client = MagicMock()
    fake_client.get_me.return_value = {"handle": "alice"}

    ws_cls = MagicMock()
    invoker_cls = MagicMock()

    with patch(
        "agentchatme_hermes.runtime.WSDaemon", ws_cls
    ), patch(
        "agentchatme_hermes.runtime.AgentInvoker", invoker_cls
    ), patch.object(
        Runtime, "_build_sync_client", return_value=fake_client
    ):
        yield ws_cls, invoker_cls, fake_client


class TestRuntimeStartGatewayMode:
    def test_gateway_starts_ws_and_invoker(self) -> None:
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime() as (ws_cls, invoker_cls, fake_client):
            rt = Runtime(_config(), gateway_mode=True)
            rt.start()

            assert ws_cls.called
            assert invoker_cls.called
            ws_cls.return_value.start.assert_called_once()
            invoker_cls.return_value.start.assert_called_once()

            assert rt.identity.handle == "alice"
            assert rt.client is fake_client


class TestRuntimeStartCliMode:
    def test_cli_skips_ws_and_invoker(self) -> None:
        """Light runtime: no WS, no invoker — just client + identity."""
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime() as (ws_cls, invoker_cls, fake_client):
            rt = Runtime(_config(), gateway_mode=False)
            rt.start()

            assert not ws_cls.called, (
                "CLI runtime must NOT construct a WSDaemon — fixes "
                "the multi-WS-per-machine bug"
            )
            assert not invoker_cls.called, (
                "CLI runtime must NOT construct an AgentInvoker"
            )

            assert rt.identity.handle == "alice"
            assert rt.client is fake_client

    def test_cli_queue_is_unavailable(self) -> None:
        """In CLI mode, accessing the queue raises (it doesn't exist)."""
        from agentchatme_hermes.runtime import Runtime

        with _patched_runtime():
            rt = Runtime(_config(), gateway_mode=False)
            rt.start()

            with pytest.raises(RuntimeError):
                _ = rt.queue


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

        with _patched_runtime() as (ws_cls, _invoker_cls, _fake_client):
            r1 = get_runtime(_config(), gateway_mode=False)
            r1.start()
            assert not ws_cls.called

            # Second call says gateway_mode=True but we already have a
            # CLI runtime — that's intentional, the mode is fixed for
            # the process.
            r2 = get_runtime(_config(), gateway_mode=True)
            assert r2 is r1
            assert not ws_cls.called
