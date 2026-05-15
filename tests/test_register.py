"""Tests for ``agentchatme_hermes._register``.

The load-bearing logic that needs coverage:

1. ``_is_gateway_context`` must reliably distinguish gateway vs every
   non-gateway process class (TUI, named CLI, one-shot, gateway-
   spawned subprocess). The 0.2.0 detector misclassified the TUI as
   gateway and opened a second WS, causing duplicate-delivery and
   lock contention. 0.2.1 switched to the canonical
   ``_HERMES_GATEWAY=1`` env marker — these tests pin that down.
2. The module must NOT register an ``on_session_end`` hook —
   regression guard against re-introducing the hook that fired on
   every per-session ending and killed the runtime mid-flight.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from agentchatme_hermes._register import _is_gateway_context
from agentchatme_hermes.leader_lock import GATEWAY_ENV_MARKER

if TYPE_CHECKING:
    import pytest


class TestIsGatewayContext:
    def test_gateway_when_env_marker_is_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "1")
        assert _is_gateway_context(ctx=object()) is True

    def test_not_gateway_when_env_marker_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TUI / named CLI / one-shot — none of these set the marker.
        monkeypatch.delenv(GATEWAY_ENV_MARKER, raising=False)
        assert _is_gateway_context(ctx=object()) is False

    def test_not_gateway_when_marker_is_other_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defensive: only literal "1" counts. A future Hermes that
        # ever sets it to "true" or anything else should NOT be
        # silently classified as gateway by us.
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "true")
        assert _is_gateway_context(ctx=object()) is False

    def test_ctx_value_does_not_change_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ctx arg is intentionally unused.

        Earlier 0.2.0 logic inspected ``ctx._manager._cli_ref`` and
        misfired. The 0.2.1 detector is process-level only — any ctx
        with the marker set returns True; any ctx without it returns
        False.
        """
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "1")
        assert _is_gateway_context(ctx=None) is True
        assert _is_gateway_context(ctx=MagicMock()) is True
        assert _is_gateway_context(ctx="anything") is True


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
        # CLI registration is unconditional; everything else short-circuits
        # because load_config will return None when AGENTCHATME_API_KEY
        # is absent in the test env.
        _register.register(ctx)

        # The forbidden call.
        forbidden = any(
            call.args == ("on_session_end",)
            or (call.args and call.args[0] == "on_session_end")
            or "on_session_end" in str(call)
            for call in ctx.method_calls
        )
        assert not forbidden, (
            "register() must NOT wire an on_session_end hook — Hermes "
            "fires that per-session, not per-process-shutdown."
        )


def _unused_param_silencer(_: Any) -> None:
    """Silences linter complaints about unused-imports in adapted tests."""
    return None
