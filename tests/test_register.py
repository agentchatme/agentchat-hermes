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

import logging
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from agentchatme_hermes._register import _is_gateway_context
from agentchatme_hermes.leader_lock import GATEWAY_ENV_MARKER

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestIsGatewayContext:
    def test_gateway_when_argv_is_gateway_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GATEWAY_ENV_MARKER, raising=False)
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        assert _is_gateway_context(ctx=object()) is True

    def test_env_marker_alone_is_not_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GATEWAY_ENV_MARKER, "1")
        monkeypatch.setattr(sys, "argv", ["hermes", "agentchat", "doctor"])
        assert _is_gateway_context(ctx=object()) is False

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
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
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


class TestProfileScopedRegistration:
    def test_config_is_loaded_for_the_resolved_profile_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register

        home = tmp_path / ".hermes" / "profiles" / "growth"
        captured: dict[str, Any] = {}

        def fake_load_config(*, hermes_home: Any = None) -> None:
            captured["hermes_home"] = hermes_home
            return None  # CLI-only: stops before any runtime is built

        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: home)
        monkeypatch.setattr(_register, "load_config", fake_load_config)
        _register.register(MagicMock())
        assert captured["hermes_home"] == home


class TestReconcileSoulAnchor:
    def _runtime(self, handle: str, home: Path) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(identity=SimpleNamespace(handle=handle), hermes_home=home)

    def test_backfills_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert soul_anchor.read_anchor_handle(soul_path=soul) == "ops"

    def test_mismatch_is_an_error_and_file_is_left_alone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SOUL.md says Growth, the key says Operations: shout, don't rewrite."""
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        soul_anchor.write_soul_anchor("growth", soul_path=soul)
        before = soul.read_text()
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("operations", tmp_path))
        assert soul.read_text() == before
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("IDENTITY MISMATCH" in m and "@growth" in m and "@operations" in m for m in messages)

    def test_matching_anchor_is_quiet(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        soul_anchor.write_soul_anchor("ops", soul_path=soul)
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert not caplog.records


class TestSurfaceAlwaysRegistered:
    def test_tools_and_skill_exist_before_configuration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A fresh profile with no key still gets the tool surface; the tools
        answer NOT_CONFIGURED instead of being absent."""
        import json

        from agentchatme_hermes import _register

        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "chat"])  # CLI process: no watcher
        ctx = MagicMock()
        _register.register(ctx)

        names = [c.kwargs["name"] for c in ctx.register_tool.call_args_list]
        assert "agentchat_send_message" in names
        assert ctx.register_skill.called

        send = next(
            c.kwargs["handler"] for c in ctx.register_tool.call_args_list
            if c.kwargs["name"] == "agentchat_send_message"
        )
        result = json.loads(send({"to": "@peer", "text": "hi"}))
        assert result["ok"] is False
        assert result["error"]["code"] == "NOT_CONFIGURED"
        assert "hermes agentchat register" in result["error"]["message"]


class TestActivationWatcher:
    def _watcher(self, tmp_path: Path, *, gateway_mode: bool = True) -> Any:
        from agentchatme_hermes._register import ActivationWatcher
        from agentchatme_hermes.runtime import RuntimeHandle

        return ActivationWatcher(
            handle=RuntimeHandle(),
            hermes_home=tmp_path / ".hermes",
            profile="default",
            gateway_mode=gateway_mode,
        )

    def test_unconfigured_then_active(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Key appears after the gateway booted: the next check activates."""
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        configs: list[Any] = [None, SimpleNamespace(api_base="https://api.example.test")]
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: configs.pop(0))
        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path)
        monkeypatch.setattr(_register, "_try_start_runtime", lambda cfg, *, gateway_mode: runtime)
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)

        watcher = self._watcher(tmp_path)
        assert watcher.try_activate() == "unconfigured"
        assert watcher.next_delay() == _register.ACTIVATION_RETRY_S
        assert watcher.try_activate() == "active"
        assert watcher._handle.runtime is runtime
        assert watcher.try_activate() == "active", "idempotent once bound"

    def test_start_failure_backs_off_then_recovers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Key present but the server unreachable at boot: retry with backoff."""
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        monkeypatch.setattr(
            _register, "load_config",
            lambda *, hermes_home=None: SimpleNamespace(api_base="https://api.example.test"),
        )
        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path)
        outcomes: list[Any] = [None, None, runtime]
        monkeypatch.setattr(
            _register, "_try_start_runtime", lambda cfg, *, gateway_mode: outcomes.pop(0)
        )
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)

        watcher = self._watcher(tmp_path)
        assert watcher.try_activate() == "failed"
        first = watcher.next_delay()
        assert watcher.try_activate() == "failed"
        second = watcher.next_delay()
        assert second > first, "exponential backoff after repeated failures"
        assert second <= _register.ACTIVATION_RETRY_MAX_S
        assert watcher.try_activate() == "active"
        assert watcher.next_delay() == _register.ACTIVATION_RETRY_S

    def test_gateway_without_key_starts_the_watcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register

        started: list[Any] = []
        monkeypatch.setattr(_register.ActivationWatcher, "start", lambda self: started.append(self))
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        _register.register(MagicMock())
        assert len(started) == 1

    def test_cli_process_without_key_does_not_start_a_watcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register

        started: list[Any] = []
        monkeypatch.setattr(_register.ActivationWatcher, "start", lambda self: started.append(self))
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
        _register.register(MagicMock())
        assert started == []
