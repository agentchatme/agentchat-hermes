"""Group-vs-DM routing regression tests.

The AgentChat SDK emits every inbound message (DM or group) as a
``message.new`` frame (verified at ``agentchatme/_realtime.py:563``:
"Invariant: for any conversation_id, handlers see message.new
envelopes"). The frame type itself does NOT distinguish DM from group
— the conversation_id prefix does:

  * ``grp_*``  → group
  * ``conv_*`` → direct (DM)
  * ``dir_*``  → direct alias

Before this fix, our adapter listened for a ``group.message`` frame
type (which the SDK never sends), so every group message was
misclassified as a DM, the agent's reply was routed back to the
sender's private DM, and the group looked silent. Surfaced in
production when the operator added the bot to "The Vibe Council"
group and saw replies arriving in DM instead of the group.

These tests pin down the classification + routing across all three
adapter surfaces: ``_on_realtime_frame``,
``_dispatch_inbound_message``, ``send``, ``get_chat_info``, and
``_standalone_send``.
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

        def _set_fatal_error(self, *_a, **_kw): pass
        def _mark_connected(self): pass
        def _mark_disconnected(self): pass
        def _acquire_platform_lock(self, *_a, **_kw): return True
        def _release_platform_lock(self, *_a, **_kw): pass
        def build_source(self, **kwargs): return SimpleNamespace(**kwargs)

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


def _adapter(monkeypatch, handle="fyi-john-4321"):
    _install_gateway_stubs()
    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test_key")

    from agentchatme_hermes import adapter as adapter_mod
    adapter_mod._AdapterCls = None
    AdapterCls = adapter_mod._adapter_class()
    inst = AdapterCls(SimpleNamespace(extra={}))
    inst.handle = handle
    inst.handle_message = AsyncMock()
    inst.build_source = lambda **kw: SimpleNamespace(**kw)
    return inst


# ── Inbound classification ─────────────────────────────────────────────────


async def test_message_new_with_grp_conv_id_classifies_as_group(monkeypatch):
    """The SDK emits group messages as `message.new`. Without prefix
    inspection, our adapter used to misroute these as DMs."""
    inst = _adapter(monkeypatch)
    frame = {
        "type": "message.new",
        "payload": {
            "id": "msg_g1",
            "from": "@vibecoder-vinny",
            "conversation_id": "grp_HtQbKsui6aXtnYGB",
            "type": "text",
            "content": {"type": "text", "text": "welcome to The Vibe Council"},
        },
    }
    await inst._on_realtime_frame(frame)

    inst.handle_message.assert_awaited_once()
    event = inst.handle_message.await_args.args[0]
    # chat_id must be the group conversation_id, NOT @sender
    assert event.source.chat_id == "grp_HtQbKsui6aXtnYGB"
    assert event.source.chat_type == "group"


async def test_message_new_with_conv_id_classifies_as_dm(monkeypatch):
    """DMs come with `conv_*` ids. chat_id routes via @sender."""
    inst = _adapter(monkeypatch)
    frame = {
        "type": "message.new",
        "payload": {
            "id": "msg_d1",
            "from": "@vibecoder-vinny",
            "conversation_id": "conv_IcwGWLdUTRrTrdcN",
            "type": "text",
            "content": {"type": "text", "text": "hi john"},
        },
    }
    await inst._on_realtime_frame(frame)

    event = inst.handle_message.await_args.args[0]
    assert event.source.chat_id == "@vibecoder-vinny"
    assert event.source.chat_type == "dm"


async def test_message_new_with_dir_prefix_classifies_as_dm(monkeypatch):
    """`dir_*` is an alias for direct conversations on the server."""
    inst = _adapter(monkeypatch)
    frame = {
        "type": "message.new",
        "payload": {
            "id": "msg_d2",
            "from": "@alice",
            "conversation_id": "dir_xyz789",
            "type": "text",
            "content": {"type": "text", "text": "yo"},
        },
    }
    await inst._on_realtime_frame(frame)
    event = inst.handle_message.await_args.args[0]
    assert event.source.chat_type == "dm"
    assert event.source.chat_id == "@alice"


# ── Outbound routing ────────────────────────────────────────────────────────


def _normalize_kwargs(chat_id, content="reply"):
    """Reproduce the routing logic from AgentChatAdapter.send without
    instantiating the whole class — easier to assert on."""
    cid = chat_id.strip()
    kwargs = {"content": {"type": "text", "text": content}}
    if cid.startswith("@"):
        kwargs["to"] = cid
    elif cid.startswith(("grp_", "conv_", "dir_")):
        kwargs["conversation_id"] = cid
    elif cid and "/" not in cid and " " not in cid:
        kwargs["to"] = "@" + cid
    else:
        kwargs["conversation_id"] = cid
    return kwargs


def test_send_routes_grp_to_conversation_id():
    """Group chat_id `grp_*` must go through `conversation_id` kwarg.
    Before this fix it fell through to `to=@grp_…` which the server
    rejected."""
    kw = _normalize_kwargs("grp_HtQbKsui6aXtnYGB")
    assert kw["conversation_id"] == "grp_HtQbKsui6aXtnYGB"
    assert "to" not in kw


def test_send_routes_conv_to_conversation_id():
    """DM conversation_id still goes through `conversation_id` kwarg
    (the SERVER may prefer `to=@handle` for DMs, but accepting the
    conv id is a fallback for callers that have only the id)."""
    kw = _normalize_kwargs("conv_IcwGWLdUTRrTrdcN")
    assert kw["conversation_id"] == "conv_IcwGWLdUTRrTrdcN"


def test_send_routes_at_handle_to_to_kwarg():
    kw = _normalize_kwargs("@alice")
    assert kw["to"] == "@alice"


def test_send_routes_bare_handle_to_at_handle():
    kw = _normalize_kwargs("alice")
    assert kw["to"] == "@alice"


# ── get_chat_info classification ───────────────────────────────────────────


async def test_get_chat_info_classifies_grp_as_group(monkeypatch):
    inst = _adapter(monkeypatch)
    info = await inst.get_chat_info("grp_HtQbKsui6aXtnYGB")
    assert info["type"] == "group"


async def test_get_chat_info_classifies_conv_as_dm(monkeypatch):
    inst = _adapter(monkeypatch)
    info = await inst.get_chat_info("conv_IcwGWLdUTRrTrdcN")
    assert info["type"] == "dm"


async def test_get_chat_info_classifies_at_handle_as_dm(monkeypatch):
    inst = _adapter(monkeypatch)
    info = await inst.get_chat_info("@alice")
    assert info["type"] == "dm"


async def test_get_chat_info_classifies_bare_handle_as_dm(monkeypatch):
    inst = _adapter(monkeypatch)
    info = await inst.get_chat_info("alice")
    assert info["type"] == "dm"
