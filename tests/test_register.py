"""Tests for ``agentchatme_hermes._register``.

The load-bearing logic that needs coverage:

1. ``_is_gateway_context`` must reliably distinguish gateway vs CLI
   processes — a wrong answer here means either every CLI invocation
   spawns a WS (the multi-WS-per-machine bug we just fixed) or the
   gateway runs without inbound (the silently-dead-runtime bug).
2. The module must NOT register an ``on_session_end`` hook —
   regression guard against re-introducing the hook that fired on
   every per-session ending and killed the runtime mid-flight.
"""
from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

from agentchatme_hermes._register import _is_gateway_context


class _FakeManager:
    """Mimics Hermes' PluginManager just enough for the detector."""

    def __init__(self, cli_ref: Any) -> None:
        self._cli_ref = cli_ref


class _FakeCtx:
    """Stand-in for the ctx Hermes hands to plugins at register time."""

    def __init__(self, manager: _FakeManager | None) -> None:
        self._manager = manager


class TestIsGatewayContext:
    def test_gateway_when_cli_ref_is_none(self) -> None:
        ctx = _FakeCtx(_FakeManager(cli_ref=None))
        assert _is_gateway_context(ctx) is True

    def test_cli_when_cli_ref_is_set(self) -> None:
        # In CLI mode Hermes sets _cli_ref to a CLI runner instance.
        ctx = _FakeCtx(_FakeManager(cli_ref=object()))
        assert _is_gateway_context(ctx) is False

    def test_argv_fallback_returns_true_when_gateway_in_argv(
        self, monkeypatch: Any
    ) -> None:
        # ctx exposes nothing useful — must fall through to argv.
        ctx = _FakeCtx(manager=None)
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        assert _is_gateway_context(ctx) is True

    def test_argv_fallback_returns_false_for_cli_subcommand(
        self, monkeypatch: Any
    ) -> None:
        ctx = _FakeCtx(manager=None)
        monkeypatch.setattr(sys, "argv", ["hermes", "agentchat", "status"])
        assert _is_gateway_context(ctx) is False

    def test_manager_without_cli_ref_attr_falls_through_to_argv(
        self, monkeypatch: Any
    ) -> None:
        # An older Hermes might not have the attribute at all — we
        # must not raise; fall through to argv.
        class _OldManager:
            pass

        ctx = _FakeCtx(_OldManager())  # type: ignore[arg-type]
        monkeypatch.setattr(sys, "argv", ["hermes"])
        assert _is_gateway_context(ctx) is False

    def test_raising_manager_falls_through_to_argv(
        self, monkeypatch: Any
    ) -> None:
        # Defensive: if reading _cli_ref blows up, the detector must
        # not propagate the exception out.
        class _RaisingManager:
            @property
            def _cli_ref(self) -> Any:  # type: ignore[override]
                raise RuntimeError("Hermes private API moved")

        ctx = _FakeCtx(_RaisingManager())  # type: ignore[arg-type]
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway"])
        # Falls back to argv — sees "gateway" → True.
        assert _is_gateway_context(ctx) is True


class TestRegisterNoSessionEndHook:
    """Regression guard against re-introducing the on_session_end hook.

    The first spike's primary bug: Hermes fires ``on_session_end`` on
    every individual session ending (TUI sessions, cron jobs, adapter
    chats), not just process shutdown. Wiring our ``runtime.stop()``
    to that hook killed the WS daemon mid-conversation.
    """

    def test_no_on_session_end_registration(self) -> None:
        from agentchatme_hermes import _register

        ctx = MagicMock()
        ctx._manager = _FakeManager(cli_ref=None)
        # CLI registration is unconditional; everything else short-circuits
        # because load_config will return None when AGENTCHATME_API_KEY
        # is absent in the test env.
        _register.register(ctx)

        # The forbidden call.
        assert not any(
            call.args == ("on_session_end",)
            or (call.args and call.args[0] == "on_session_end")
            or "on_session_end" in str(call)
            for call in ctx.method_calls
        ), (
            "register() must NOT wire an on_session_end hook — Hermes "
            "fires that per-session, not per-process-shutdown."
        )
