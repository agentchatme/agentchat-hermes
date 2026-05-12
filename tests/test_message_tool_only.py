"""Tests for the message-tool-only mode (silence-by-default contract).

AgentChat is peer-to-peer between agents. Hermes's framework default is
"reactive bot" — every inbound spawns a session, the LLM's final text
auto-routes to the source chat. That works for Telegram/Slack where the
bot talks to a human; it breaks AgentChat because two agents would both
auto-reply to every message forever (and they'd auto-reply WITH their
turn-end reasoning text, producing slop on top of the loop).

The silence contract has three layers, each tested below:

  1. ``SUPPORTS_MESSAGE_EDITING = False`` on the class — Hermes's stream
     consumer (``run.py:14363``) skips setup for editing-incapable
     adapters, killing mid-turn streaming-delta leakage at the source.
     Added in 0.1.75 after 0.1.73's set_message_handler override alone
     was found to miss the streaming path (and the per-turn intermediate
     "Let me check ..." assistant text leaked into the group).
  2. ``adapter.send`` is a no-op — every framework-internal proactive
     send (final-response delivery, ``_deliver_platform_notice``, status
     callbacks, interim assistant messages, stream consumer fallback)
     reaches ``adapter.send`` and is dropped. The agent's only sanctioned
     delivery path is the ``agentchat_send_message`` tool, which calls
     ``client.send_message`` directly and bypasses this method.
  3. ``set_message_handler`` wraps Hermes's handler so its return value
     (the LLM's wrap-up text) is always ``None``. Defense-in-depth
     against any future Hermes change that bypasses ``adapter.send``
     for the handler's return path.
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


# ── Layer 1: stream consumer is skipped via SUPPORTS_MESSAGE_EDITING ────────


def test_supports_message_editing_is_false(monkeypatch):
    """The class attribute that gates Hermes's stream consumer must be
    False. Hermes reads it via ``getattr(adapter, "SUPPORTS_MESSAGE_EDITING",
    True)`` (``run.py:14363``); any truthy value reopens the streaming path
    and intermediate "thinking" text starts leaking into chat again.

    Pinned as a class attribute (not instance) so the value is queryable
    without constructing the adapter and so subclasses inherit it."""
    inst = _adapter(monkeypatch)
    assert type(inst).SUPPORTS_MESSAGE_EDITING is False, (
        "AgentChatAdapter must declare SUPPORTS_MESSAGE_EDITING = False "
        "so Hermes skips stream consumer setup — otherwise mid-turn "
        "token deltas leak into chat bypassing both the handler wrapper "
        "and adapter.send"
    )


# ── Layer 2: adapter.send is a no-op for every Hermes-internal path ────────


async def test_send_is_noop_and_returns_synthetic_success(monkeypatch):
    """Every framework-internal call to ``adapter.send`` must drop the
    message and return ``success=True`` so ``_send_with_retry``
    (``base.py:2315``) doesn't retry forever. The agent's tool path uses
    ``client.send_message`` directly and is unaffected by this no-op."""
    inst = _adapter(monkeypatch)
    # Sentinel client to assert it's never reached.
    sent_calls: list = []

    class _SentinelClient:
        async def send_message(self, **kwargs):
            sent_calls.append(kwargs)
            raise AssertionError(
                "adapter.send must NOT reach the SDK — it's a no-op "
                "to enforce message-tool-only silence"
            )

    inst._client = _SentinelClient()

    result = await inst.send("grp_abc", "this would be framework-internal text")

    assert sent_calls == [], "SDK send_message must not be invoked"
    assert getattr(result, "success", None) is True, (
        "no-op must return synthetic success so Hermes doesn't retry"
    )
    assert getattr(result, "message_id", "missing") is None, (
        "no message was sent, so message_id must be None — anything else "
        "would lie to the framework"
    )


async def test_send_noop_works_when_client_not_initialized(monkeypatch):
    """If Hermes calls ``adapter.send`` before/after the SDK client is
    open (early-startup notice, post-disconnect drain), the no-op must
    still return success — surfacing 'not connected' would just trigger
    retry loops for a send we don't want to make anyway."""
    inst = _adapter(monkeypatch)
    inst._client = None  # simulate not-yet-connected / disconnected

    result = await inst.send("@alice", "hi")
    assert getattr(result, "success", None) is True
    assert getattr(result, "message_id", "missing") is None


async def test_send_noop_accepts_all_chat_id_shapes(monkeypatch):
    """The no-op must not raise on any chat_id shape Hermes might pass
    (DM @handle, conversation_id, group id, raw string). Hermes
    auto-discovers chat ids from inbound events and from internal state
    like ``home_channel`` — we don't get to dictate the shape."""
    inst = _adapter(monkeypatch)
    inst._client = object()  # any truthy non-SDK sentinel

    for chat_id in [
        "@alice",
        "grp_HtQbKsui6aXtnYGB",
        "conv_F8QIXjhXM4h1uCdM",
        "bare-handle-no-prefix",
        "weird/slashed/value",  # we just don't crash
        "",                     # empty edge case
    ]:
        result = await inst.send(chat_id, "x")
        assert getattr(result, "success", None) is True, (
            f"no-op must accept chat_id={chat_id!r} without raising"
        )


async def test_send_noop_drops_platform_notice_payload(monkeypatch):
    """Smoke test for the specific user-visible regression that prompted
    this fix: Hermes's '📬 No home channel is set' notice
    (``run.py:7090``) calls ``adapter.send`` with a multi-line setup
    string. Without the no-op, this lands in the group on every fresh
    install and is the first message the operator's agent sends — to
    *other* agents — before the operator can say anything."""
    inst = _adapter(monkeypatch)
    inst._client = object()

    notice = (
        "📬 No home channel is set for Agentchat. "
        "A home channel is where Hermes delivers cron job results "
        "and cross-platform messages.\n\n"
        "Type /sethome to make this chat your home channel, "
        "or ignore to skip."
    )
    result = await inst.send("grp_abc", notice)
    assert getattr(result, "success", None) is True
    assert getattr(result, "message_id", "missing") is None
