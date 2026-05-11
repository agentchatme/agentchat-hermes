"""Regression tests for DM reply routing.

The AgentChat server rejects ``conversation_id=conv_…`` for direct
messages with ``validation: Use 'to' to send to a direct conversation``.
DMs are addressed by the recipient's @handle, not the conversation id;
that's only valid for groups.

Discovered in v0.1.65 verification: an inbound DM arrived, the agent
processed it, generated a reply, and the reply silently failed at
``send()`` because we were routing every chat_id starting with
``conv_`` through ``conversation_id=`` regardless of DM vs group.

Fix: in ``_dispatch_inbound_message``, set ``chat_id = "@<sender>"``
for DMs so Hermes's reply naturally routes via the ``to=`` SDK kwarg.
Groups keep the conversation_id because that's the only address a
group has.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock


def _install_gateway_stubs():
    """Same minimal stubs as test_defensive_init — self-contained."""
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
            self.handle_message = AsyncMock()
            self._fatal_error_log = []

        def _set_fatal_error(self, *_a, **_kw):
            self._fatal_error_log.append((_a, _kw))

        def _mark_connected(self):
            pass

        def _mark_disconnected(self):
            pass

        def _acquire_platform_lock(self, *_a, **_kw):
            return True

        def _release_platform_lock(self, *_a, **_kw):
            pass

        def build_source(self, **kwargs):
            return SimpleNamespace(**kwargs)

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


def _make_adapter(monkeypatch, *, handle: str = "fyi-john-4321"):
    _install_gateway_stubs()
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key_123")
    monkeypatch.setenv("AGENTCHATME_ALLOW_ALL", "true")

    from agentchatme_hermes import adapter as adapter_mod

    adapter_mod._AdapterCls = None
    AdapterCls = adapter_mod._adapter_class()
    inst = AdapterCls(SimpleNamespace(extra={}))
    inst.handle = handle
    # Other test modules may have already cached gateway stubs in
    # sys.modules. Patch on the instance directly so we observe
    # exactly what `_dispatch_inbound_message` passes through:
    inst.handle_message = AsyncMock()
    # build_source from the test_defensive_init stubs returns a MagicMock,
    # which won't compare equal to literal strings. Patch it to return
    # a SimpleNamespace so we can assert on `event.source.chat_id`.
    inst.build_source = lambda **kwargs: SimpleNamespace(**kwargs)
    return inst


async def test_dm_inbound_sets_chat_id_to_sender_handle(monkeypatch):
    """Inbound DM must build chat_id="@<sender>", NOT the conversation_id."""
    inst = _make_adapter(monkeypatch)

    payload = {
        "id": "msg_abc",
        "from": "@vibecoder-vinny",
        "conversation_id": "conv_IcwGWLdUTRrTrdcN",
        "type": "text",
        "content": {"type": "text", "text": "hi there"},
    }
    await inst._dispatch_inbound_message(payload, kind="direct")

    inst.handle_message.assert_awaited_once()
    event = inst.handle_message.await_args.args[0]
    # The crucial assertion: chat_id is the @handle, NOT the conv_ id.
    assert event.source.chat_id == "@vibecoder-vinny"
    assert event.source.chat_type == "dm"
    # And the conversation_id is still preserved on raw_message so any
    # caller that needs it can reach it.
    assert event.raw_message["conversation_id"] == "conv_IcwGWLdUTRrTrdcN"


async def test_group_inbound_keeps_conversation_id_as_chat_id(monkeypatch):
    """Inbound group message must keep conversation_id as chat_id — groups
    don't have a single recipient handle."""
    inst = _make_adapter(monkeypatch)

    payload = {
        "id": "msg_xyz",
        "from": "@vibecoder-vinny",
        "conversation_id": "conv_GroupRoom123",
        "type": "text",
        "content": {"type": "text", "text": "ping group"},
    }
    await inst._dispatch_inbound_message(payload, kind="group")

    inst.handle_message.assert_awaited_once()
    event = inst.handle_message.await_args.args[0]
    assert event.source.chat_id == "conv_GroupRoom123"
    assert event.source.chat_type == "group"


async def test_dm_inbound_normalizes_sender_handle_case(monkeypatch):
    """Sender comes back as @Vibecoder-Vinny → chat_id is @vibecoder-vinny."""
    inst = _make_adapter(monkeypatch)

    payload = {
        "id": "msg_q",
        "from": "@Vibecoder-Vinny",
        "conversation_id": "conv_abc",
        "type": "text",
        "content": {"type": "text", "text": "yo"},
    }
    await inst._dispatch_inbound_message(payload, kind="direct")

    event = inst.handle_message.await_args.args[0]
    assert event.source.chat_id == "@vibecoder-vinny"
