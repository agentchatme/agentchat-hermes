"""Tests for the message-tool-only mode (the 0.1.73 silence-by-default fix).

AgentChat is peer-to-peer between agents. Hermes's framework default is
"reactive bot" — every inbound spawns a session, the LLM's final text
auto-routes to the source chat. That works for Telegram/Slack where the
bot talks to a human; it breaks AgentChat because two agents would both
auto-reply to every message forever (and they'd auto-reply WITH their
turn-end reasoning text, producing slop on top of the loop).

The fix: override `set_message_handler` to wrap whatever Hermes
registers in a shim that runs the real handler (so the LLM, tools,
session lifecycle all still execute) and then ALWAYS returns None to
suppress the framework's auto-reply. The agent's only path to chat
becomes the explicit `agentchat_send_message` tool.

These tests pin down that:
  * `set_message_handler` wraps Hermes's handler
  * The wrapped handler returns None unconditionally
  * Exceptions in the inner handler are caught (not propagated)
  * The inner handler is still called (so tools / session still run)
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock


def _install_gateway_stubs():
    if "gateway" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    config_mod = types.ModuleType("gateway.config")
    platforms_mod = types.ModuleType("gateway.platforms")
    base_mod = types.ModuleType("gateway.platforms.base")

    class _Platform:
        def __init__(self, name):
            self.value = name

    class _BasePlatformAdapter:
        def __init__(self, config=None, platform=None):
            self.config = config
            self.platform = platform
            self._message_handler = None

        def _set_fatal_error(self, *_a, **_kw): pass
        def _mark_connected(self): pass
        def _mark_disconnected(self): pass
        def _acquire_platform_lock(self, *_a, **_kw): return True
        def _release_platform_lock(self, *_a, **_kw): pass
        def build_source(self, **kw): return SimpleNamespace(**kw)

        def set_message_handler(self, handler):
            """Base implementation just stores."""
            self._message_handler = handler

    class _MessageType:
        TEXT = "text"
        PHOTO = "photo"
        VIDEO = "video"
        AUDIO = "audio"
        DOCUMENT = "document"

    class _MessageEvent:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _SendResult:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    config_mod.Platform = _Platform
    base_mod.BasePlatformAdapter = _BasePlatformAdapter
    base_mod.MessageType = _MessageType
    base_mod.MessageEvent = _MessageEvent
    base_mod.SendResult = _SendResult

    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = config_mod
    sys.modules["gateway.platforms"] = platforms_mod
    sys.modules["gateway.platforms.base"] = base_mod


def _adapter(monkeypatch):
    _install_gateway_stubs()
    # Other tests may have cached gateway stubs without
    # `set_message_handler` on _BasePlatformAdapter. Patch it on if
    # missing so our override's `super().set_message_handler` works.
    from gateway.platforms.base import (
        BasePlatformAdapter as _Base,  # type: ignore[import-not-found]
    )
    if not hasattr(_Base, "set_message_handler"):
        def _stub_set(self, handler):
            self._message_handler = handler
        _Base.set_message_handler = _stub_set  # type: ignore[attr-defined]

    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test")
    from agentchatme_hermes import adapter as adapter_mod
    adapter_mod._AdapterCls = None
    AdapterCls = adapter_mod._adapter_class()
    inst = AdapterCls(SimpleNamespace(extra={}))
    # Ensure the instance always has _message_handler so our wrapper's
    # super() call has somewhere to store the wrapped handler even on
    # stubs that don't pre-populate it.
    if not hasattr(inst, "_message_handler"):
        inst._message_handler = None
    return inst


async def test_wrapped_handler_returns_none_even_when_inner_returns_text(monkeypatch):
    """The wrapper must ALWAYS return None — even if the inner handler
    returned a string the framework would otherwise have auto-replied with."""
    inst = _adapter(monkeypatch)

    inner = AsyncMock(return_value="this text would auto-reply on a normal Hermes adapter")
    inst.set_message_handler(inner)

    wrapped = inst._message_handler
    event = SimpleNamespace(text="inbound")
    result = await wrapped(event)

    assert result is None, "wrapper must suppress framework auto-reply"
    inner.assert_awaited_once_with(event)


async def test_wrapped_handler_returns_none_when_inner_returns_none(monkeypatch):
    """When the inner already wanted silence, wrapper still returns None."""
    inst = _adapter(monkeypatch)
    inner = AsyncMock(return_value=None)
    inst.set_message_handler(inner)

    result = await inst._message_handler(SimpleNamespace(text="x"))
    assert result is None


async def test_wrapped_handler_swallows_exceptions(monkeypatch):
    """If the inner handler raises, the wrapper logs + returns None.
    Without this, an exception would surface as if the wrapper itself
    raised, and Hermes's session supervisor would treat the adapter
    as misbehaving."""
    inst = _adapter(monkeypatch)
    inner = AsyncMock(side_effect=RuntimeError("agent loop blew up"))
    inst.set_message_handler(inner)

    # Must not raise.
    result = await inst._message_handler(SimpleNamespace(text="x"))
    assert result is None
    inner.assert_awaited_once()


async def test_wrapper_runs_inner_so_tool_calls_still_fire(monkeypatch):
    """The inner handler is what runs the LLM + tools. The wrapper
    MUST still invoke it — we only discard the return value, we
    don't skip the work."""
    inst = _adapter(monkeypatch)

    side_effects = []

    async def inner(event):
        side_effects.append(("ran with", event.text))
        return "would-be-reply"

    inst.set_message_handler(inner)
    await inst._message_handler(SimpleNamespace(text="hello"))

    # Critical: the inner handler ran. That means the LLM ran. That
    # means agentchat_send_message tool calls (if any) fired. Only
    # the framework auto-reply path is suppressed.
    assert side_effects == [("ran with", "hello")]


async def test_wrapper_preserves_inner_via_wrapped_attribute(monkeypatch):
    """Hermes-side introspection (e.g. `_handler.__wrapped__`) finds
    the original handler. Lets ops debugging work the same as on
    non-overridden adapters."""
    inst = _adapter(monkeypatch)
    inner = AsyncMock(return_value=None)
    inst.set_message_handler(inner)

    wrapped = inst._message_handler
    assert getattr(wrapped, "__wrapped__", None) is inner


def test_platform_hint_teaches_message_tool_only_contract(monkeypatch):
    """The system-prompt contribution must teach the LLM that its
    end-of-turn text doesn't auto-route. Otherwise the LLM keeps
    producing wrap-up text expecting it to be sent, and the agent
    appears silent on every turn (because the wrapper discards
    that text)."""
    monkeypatch.setenv("AGENTCHATME_HANDLE", "fyi-john-4321")
    from agentchatme_hermes.adapter import _build_platform_hint

    hint = _build_platform_hint()
    # The key contract the LLM must understand:
    assert "silence" in hint.lower() or "silent" in hint.lower()
    assert "agentchat_send_message" in hint


def test_platform_hint_no_handle_also_teaches_contract(monkeypatch):
    """Fallback template (when handle env not set yet) must carry the
    same contract — otherwise the agent's behavior diverges between
    fresh installs and post-registration runs."""
    monkeypatch.delenv("AGENTCHATME_HANDLE", raising=False)
    from agentchatme_hermes.adapter import _build_platform_hint

    hint = _build_platform_hint()
    assert "silence" in hint.lower() or "silent" in hint.lower()
    assert "agentchat_send_message" in hint
