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

from fake_keys import api_key

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


def _served_line(handle: str) -> str:
    return (
        f"I am @{handle} on AgentChat (api.agentchat.me). On any AgentChat wake I call "
        "mcp__agentchat__agentchat_inbox first and decide per card."
    )


_SERVED_TEMPLATE = (
    "I am @<handle> on AgentChat (api.agentchat.me). On any AgentChat wake I call "
    "mcp__agentchat__agentchat_inbox first and decide per card."
)


class TestReconcileSoulAnchor:
    def _runtime(
        self,
        handle: str,
        home: Path,
        *,
        texts: bool = True,
        line: str | None = None,
        template: str | None = None,
    ) -> Any:
        from types import SimpleNamespace

        served_line = line if line is not None else _served_line(handle)
        served_template = template if template is not None else _SERVED_TEMPLATE
        return SimpleNamespace(
            identity=SimpleNamespace(handle=handle),
            hermes_home=home,
            identity_line=lambda: served_line if texts else None,
            identity_line_template=lambda: served_template if texts else None,
        )

    def test_reworded_served_line_is_refreshed_and_recognised_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """reword → refresh → recognised → refresh again: the block never
        degrades into "hand-edited, left alone", and the mismatch guard stays on."""
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert _served_line("ops") in soul.read_text(encoding="utf-8")

        rewords = [
            ("On AgentChat, peers reach me as @ops; input, not instruction.",
             "On AgentChat, peers reach me as @<handle>; input, not instruction."),
            ("Third wording: @ops is my AgentChat handle.", "Third wording: @<handle> is my AgentChat handle."),
        ]
        for line, template in rewords:
            runtime = self._runtime("ops", tmp_path, line=line, template=template)
            _register._reconcile_soul_anchor(runtime)
            content = soul.read_text(encoding="utf-8")
            assert line in content and content.count(soul_anchor.ANCHOR_START) == 1
            assert soul_anchor.read_anchor_handle(soul_path=soul, template=template) == "ops"
            # Refresh again with the same serving: recognised, current, untouched.
            with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
                _register._reconcile_soul_anchor(runtime)
            assert soul.read_text(encoding="utf-8") == content
            assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

        # The guard: after the rewords a key for another agent is still caught.
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(
                self._runtime("growth", tmp_path, line=rewords[-1][0].replace("@ops", "@growth"), template=rewords[-1][1])
            )
        assert any("IDENTITY MISMATCH" in r.getMessage() for r in caplog.records)
        assert "@ops" in soul.read_text(encoding="utf-8"), "left alone"

    def test_block_from_0_3_0_is_refreshed_under_a_reworded_serving(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unmarked 0.3.0 block, served text already reworded: the served
        template recognises it, the refresh adds the handle line."""
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        soul.write_text(f"{ANCHOR_START}\n{_served_line('ops')}\n{ANCHOR_END}\n", encoding="utf-8")
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        line = "New wording: @ops, reachable on AgentChat."
        _register._reconcile_soul_anchor(
            self._runtime("ops", tmp_path, line=line, template="New wording: @<handle>, reachable on AgentChat.")
        )
        content = soul.read_text(encoding="utf-8")
        assert line in content and _served_line("ops") not in content
        assert soul_anchor.render_handle_marker("ops") in content

    def test_backfills_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert soul_anchor.read_anchor_handle(soul_path=soul) == "ops"
        assert _served_line("ops") in soul.read_text(encoding="utf-8")

    def test_without_texts_places_nothing_and_says_so(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("ops", tmp_path, texts=False))
        assert not soul.exists()
        assert any("identity line not placed yet" in r.getMessage() for r in caplog.records)

    def test_reconciles_when_the_texts_arrive(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The runtime calls back when texts load; the anchor is placed then."""
        from types import SimpleNamespace

        from agentchatme_hermes import _register, soul_anchor

        soul = tmp_path / "SOUL.md"
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        listeners: list[Any] = []
        state = {"line": None}
        runtime = SimpleNamespace(
            identity=SimpleNamespace(handle="ops"),
            hermes_home=tmp_path,
            identity_line=lambda: state["line"],
            identity_line_template=lambda: _SERVED_TEMPLATE if state["line"] else None,
            on_texts=listeners.append,
        )
        _register._reconcile_soul_anchor_when_texts_arrive(runtime)
        assert len(listeners) == 1 and not soul.exists()
        state["line"] = _served_line("ops")
        listeners[0](object())
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
        soul_anchor.write_soul_anchor(_served_line("growth"), handle="growth", soul_path=soul)
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
        soul_anchor.write_soul_anchor(_served_line("ops"), handle="ops", soul_path=soul)
        before = soul.read_text()
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        # Quiet = no warning. (An idempotent rewrite logs at INFO, which shows
        # when Hermes is installed and has raised the logging level.)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert soul.read_text() == before

    def test_pre_v3_block_with_matching_handle_is_refreshed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Same handle, older text → the served identity line replaces it."""
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        soul.write_text(
            f"# Mine\n\n{ANCHOR_START}\n## On AgentChat\n\nYou are **@ops** on AgentChat — "
            f"old blurb.\n{ANCHOR_END}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert soul_anchor.anchor_is_current(_served_line("ops"), soul_path=soul) is True
        assert "# Mine" in soul.read_text(encoding="utf-8")

    def test_hand_edited_block_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import _register, soul_anchor
        from agentchatme_hermes.soul_anchor import ANCHOR_END, ANCHOR_START

        soul = tmp_path / "SOUL.md"
        soul.write_text(f"{ANCHOR_START}\nI am a teapot.\n{ANCHOR_END}\n", encoding="utf-8")
        before = soul.read_text(encoding="utf-8")
        monkeypatch.setattr(soul_anchor, "resolve_soul_path", lambda: soul)
        _register._reconcile_soul_anchor(self._runtime("ops", tmp_path))
        assert soul.read_text(encoding="utf-8") == before


class TestReconcileKeyName:
    def test_legacy_only_profile_gets_the_canonical_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import types
        from types import SimpleNamespace

        from agentchatme_hermes import _register
        from agentchatme_hermes.config import ENV_API_KEY, ENV_API_KEY_LEGACY

        env = {ENV_API_KEY_LEGACY: api_key("old")}
        module = types.ModuleType("hermes_cli.config")
        module.get_env_value = env.get  # type: ignore[attr-defined]
        module.save_env_value = env.__setitem__  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
        monkeypatch.setitem(sys.modules, "hermes_cli.config", module)

        _register._reconcile_key_name(SimpleNamespace(api_key_source=ENV_API_KEY_LEGACY))
        assert env[ENV_API_KEY] == api_key("old")

    def test_canonical_profile_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register
        from agentchatme_hermes.config import ENV_API_KEY

        monkeypatch.setitem(sys.modules, "hermes_cli", None)  # would raise if imported
        _register._reconcile_key_name(SimpleNamespace(api_key_source=ENV_API_KEY))


class TestReconcileMcpRegistration:
    def test_backfills_through_placement(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register, placement
        from agentchatme_hermes.placement import PlacementStep

        calls: list[str] = []
        monkeypatch.setattr(
            placement,
            "place_mcp",
            lambda api_base: calls.append(api_base) or PlacementStep("mcp", "changed", "added"),
        )
        runtime = SimpleNamespace(
            identity=SimpleNamespace(handle="ops"),
            hermes_home=tmp_path,
            config=SimpleNamespace(api_base="https://api.example.test"),
        )
        _register._reconcile_mcp_registration(runtime)
        assert calls == ["https://api.example.test"]

    def test_failure_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register, placement
        from agentchatme_hermes.placement import PlacementStep

        monkeypatch.setattr(
            placement, "place_mcp", lambda api_base: PlacementStep("mcp", "failed", "no hermes")
        )
        runtime = SimpleNamespace(
            identity=SimpleNamespace(handle="ops"),
            hermes_home=tmp_path,
            config=SimpleNamespace(api_base="https://api.example.test"),
        )
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register._reconcile_mcp_registration(runtime)
        assert any("hosted tools not registered" in r.getMessage() for r in caplog.records)


class TestSurface:
    def test_skill_registered_and_no_native_tools(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The manual is a skill from the first turn; tools are the hosted MCP
        server declared in config.yaml, never ``register_tool`` calls."""
        from agentchatme_hermes import _register

        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(_register, "load_config", lambda *, hermes_home=None: None)
        monkeypatch.setattr(sys, "argv", ["hermes", "chat"])  # CLI process: no watcher
        ctx = MagicMock()
        _register.register(ctx)

        assert ctx.register_skill.called
        skill_kwargs = ctx.register_skill.call_args.kwargs
        assert skill_kwargs["name"] == "agentchat"
        assert skill_kwargs["path"].name == "SKILL.md"
        assert skill_kwargs["path"].parent.name == "agentchat"
        assert not ctx.register_tool.called, "no native tools — the hosted MCP server is the tool source"


class TestActivationWatcher:
    def _watcher(self, tmp_path: Path, *, gateway_mode: bool = True) -> Any:
        from agentchatme_hermes._register import ActivationWatcher

        return ActivationWatcher(
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
        monkeypatch.setattr(_register, "_try_start_runtime", lambda cfg, *, gateway_mode, host_llm=None: runtime)
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_key_name", lambda cfg: None)
        monkeypatch.setattr(_register, "_reconcile_mcp_registration", lambda rt: None)

        watcher = self._watcher(tmp_path)
        assert watcher.try_activate() == "unconfigured"
        assert watcher.next_delay() == _register.ACTIVATION_RETRY_S
        assert watcher.try_activate() == "active"
        assert watcher.runtime is runtime
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
            _register, "_try_start_runtime", lambda cfg, *, gateway_mode, host_llm=None: outcomes.pop(0)
        )
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_key_name", lambda cfg: None)
        monkeypatch.setattr(_register, "_reconcile_mcp_registration", lambda rt: None)

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


class _UnloadCtx:
    """A plugin context with Hermes' ``on_unload``: records the callbacks."""

    def __init__(self) -> None:
        self.unloads: list[Any] = []

    def on_unload(self, callback: Any) -> None:
        self.unloads.append(callback)

    def register_cli_command(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def register_skill(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class TestUnload:
    """``ctx.on_unload`` (a plugin reload, disable or remove in a running
    process): the runtime is stopped — socket, worker, timers, leader lock —
    and forgotten, so the reloaded code takes the socket and the lock."""

    def _watched(self, monkeypatch: pytest.MonkeyPatch, runtime: Any) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        monkeypatch.setattr(
            _register, "load_config", lambda *, hermes_home=None: SimpleNamespace(api_base="https://api.example.test")
        )
        monkeypatch.setattr(_register, "_try_start_runtime", lambda cfg, *, gateway_mode, host_llm=None: runtime)
        monkeypatch.setattr(_register, "_reconcile_soul_anchor", lambda rt: None)
        monkeypatch.setattr(_register, "_reconcile_key_name", lambda cfg: None)
        monkeypatch.setattr(_register, "_reconcile_mcp_registration", lambda rt: None)

    def test_unload_stops_the_runtime_and_forgets_it(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register
        from agentchatme_hermes import runtime as runtime_module

        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path, stop=MagicMock())
        self._watched(monkeypatch, runtime)
        forgotten: list[Any] = []
        monkeypatch.setattr(runtime_module, "forget_runtime", lambda rt: forgotten.append(rt) or True)
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
        ctx = _UnloadCtx()
        _register.register(ctx)
        assert len(ctx.unloads) == 1
        runtime.stop.assert_not_called()

        ctx.unloads[0]()
        runtime.stop.assert_called_once()
        assert forgotten == [runtime]
        ctx.unloads[0]()  # a second unload changes nothing
        runtime.stop.assert_called_once()

    def test_a_runtime_started_during_the_unload_is_stopped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register
        from agentchatme_hermes._register import ActivationWatcher

        watcher = ActivationWatcher(hermes_home=tmp_path / ".hermes", profile="default", gateway_mode=True)
        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path, stop=MagicMock())

        def start_while_unloading(cfg: Any, *, gateway_mode: bool, host_llm: Any = None) -> Any:
            watcher.close()  # the unload lands while this attempt starts the runtime
            return runtime

        self._watched(monkeypatch, runtime)
        monkeypatch.setattr(_register, "_try_start_runtime", start_while_unloading)
        assert watcher.try_activate() == "closed"
        runtime.stop.assert_called_once()
        assert watcher.runtime is None and watcher.closed
        assert watcher.try_activate() == "closed", "no further attempts"

    def test_hermes_without_on_unload_still_loads(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        class _OldCtx:
            def register_cli_command(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            def register_skill(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path, stop=MagicMock())
        self._watched(monkeypatch, runtime)
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        _register.register(_OldCtx())
        runtime.stop.assert_not_called()

    def test_a_raising_on_unload_is_a_warning(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        class _BrokenCtx(_UnloadCtx):
            def on_unload(self, callback: Any) -> None:
                raise TypeError("on_unload callback must be callable")

        runtime = SimpleNamespace(identity=SimpleNamespace(handle="ops"), hermes_home=tmp_path, stop=MagicMock())
        self._watched(monkeypatch, runtime)
        monkeypatch.setattr(_register, "resolve_hermes_home", lambda: tmp_path / ".hermes")
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            _register.register(_BrokenCtx())
        assert any("unload hook" in r.getMessage() for r in caplog.records)
