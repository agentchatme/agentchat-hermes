"""Tests for the inbound-message envelope (the 0.1.76 framing fix).

Without an envelope, our adapter passed raw message bodies through
to the LLM's user-role slot — e.g. "hey john! 🤙". DeepSeek (and
similar chat-tuned models) pattern-matched that as a normal user
prompt and emitted a free-text reply, never reaching for the
``agentchat_send_message`` tool. The framework-side silence layers
correctly dropped that text, so the agent appeared dead.

The envelope wraps every inbound with a bracketed surface marker so
the model recognises this is AgentChat peer traffic. Direct DMs get
``[AgentChat DM from @<sender>]``; group messages get
``[AgentChat group <conv_id>]`` plus an ``@<sender>:`` byline inside
the body (because group chats have N senders and the model needs
the attribution).

Mirrors OpenClaw's ``formatAgentEnvelope``
(``node_modules/openclaw/dist/envelope-DDby4aj3.js:108``), with two
deliberate departures from that template documented in the adapter.
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
        def set_message_handler(self, handler): self._message_handler = handler

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


def _make_adapter(monkeypatch, handle="fyi-john-4321"):
    _install_gateway_stubs()
    # Defensive: some test files cache gateway stubs without the methods or
    # attributes we need. Re-patch on each invocation so this test stands
    # alone regardless of which other test populated the stub cache first.
    from gateway.platforms.base import (  # type: ignore[import-not-found]
        BasePlatformAdapter as _Base,
        MessageType as _MT,
    )
    if not hasattr(_Base, "set_message_handler"):
        def _stub_set(self, h):
            self._message_handler = h
        _Base.set_message_handler = _stub_set  # type: ignore[attr-defined]
    # Ensure media MessageType members exist on whichever cached stub got
    # populated first — test_inbound_envelope exercises the attachment
    # path, which references PHOTO/VIDEO/AUDIO/DOCUMENT.
    for _name in ("TEXT", "PHOTO", "VIDEO", "AUDIO", "DOCUMENT"):
        if not hasattr(_MT, _name):
            setattr(_MT, _name, _name.lower())

    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_test")
    from agentchatme_hermes import adapter as adapter_mod
    adapter_mod._AdapterCls = None
    AdapterCls = adapter_mod._adapter_class()
    inst = AdapterCls(SimpleNamespace(extra={}))
    inst.handle = handle
    # Capture what gets dispatched to the framework so tests can assert on
    # the enveloped body the model would actually see.
    inst.handle_message = AsyncMock()
    inst.build_source = lambda **kwargs: SimpleNamespace(**kwargs)
    return inst


# ── DM envelope shape ─────────────────────────────────────────────────────


async def test_dm_envelope_wraps_body_with_surface_marker(monkeypatch):
    """Direct DM body must arrive at the LLM wrapped in
    ``[AgentChat DM from @<sender>]\\n<body>`` so a chat-tuned model
    recognises the surface and doesn't fall back on free-text reply."""
    inst = _make_adapter(monkeypatch)

    payload = {
        "id": "msg_abc",
        "sender": "vibecoder-vinny",
        "conversation_id": "conv_IcwGWLdUTRrTrdcN",
        "type": "text",
        "content": {"type": "text", "text": "hey john! 🤙 just circling back"},
    }
    await inst._dispatch_inbound_message(payload, kind="direct")

    inst.handle_message.assert_awaited_once()
    event = inst.handle_message.await_args.args[0]
    assert event.text == (
        "[AgentChat DM from @vibecoder-vinny]\n"
        "hey john! 🤙 just circling back"
    ), (
        f"DM envelope mismatch — got:\n{event.text!r}"
    )


async def test_dm_envelope_strips_leading_at_from_sender(monkeypatch):
    """Server may emit sender with or without leading ``@``. The envelope
    header must always normalize so we don't end up with ``@@vinny``."""
    inst = _make_adapter(monkeypatch)
    payload = {
        "id": "msg_a",
        "sender": "@Vibecoder-Vinny",
        "conversation_id": "conv_a",
        "type": "text",
        "content": {"type": "text", "text": "yo"},
    }
    await inst._dispatch_inbound_message(payload, kind="direct")

    event = inst.handle_message.await_args.args[0]
    assert event.text == "[AgentChat DM from @vibecoder-vinny]\nyo"


# ── Group envelope shape ──────────────────────────────────────────────────


async def test_group_envelope_includes_sender_byline_in_body(monkeypatch):
    """Group bodies have N senders — the model must see who's speaking.
    Envelope shape: ``[AgentChat group <conv_id>]\\n@<sender>: <body>``."""
    inst = _make_adapter(monkeypatch)

    payload = {
        "id": "msg_g",
        "sender": "vibecoder-vinny",
        "conversation_id": "grp_HtQbKsui6aXtnYGB",
        "type": "text",
        "content": {"type": "text", "text": "okay hear me out — what should we build today?"},
    }
    await inst._dispatch_inbound_message(payload, kind="group")

    event = inst.handle_message.await_args.args[0]
    assert event.text == (
        "[AgentChat group grp_HtQbKsui6aXtnYGB]\n"
        "@vibecoder-vinny: okay hear me out — what should we build today?"
    )


# ── Raw payload preservation ──────────────────────────────────────────────


async def test_envelope_does_not_mutate_raw_message(monkeypatch):
    """The envelope wraps the model-facing ``event.text`` only. The raw
    payload stays untouched on ``event.raw_message`` so any downstream
    consumer that wants the unframed shape can still read it."""
    inst = _make_adapter(monkeypatch)

    payload = {
        "id": "msg_z",
        "sender": "alice-bot",
        "conversation_id": "conv_X",
        "type": "text",
        "content": {"type": "text", "text": "plain body"},
    }
    await inst._dispatch_inbound_message(payload, kind="direct")

    event = inst.handle_message.await_args.args[0]
    assert event.raw_message is payload, "raw_message must be the original payload, unmodified"
    # Re-derive the original body from raw_message — the un-enveloped text
    # is still recoverable.
    assert event.raw_message["content"]["text"] == "plain body"


# ── Envelope wraps non-text payloads too ──────────────────────────────────


async def test_envelope_wraps_attachment_placeholder_body(monkeypatch):
    """File/image/audio attachments render to a placeholder body like
    ``[image attachment att_xxx]``. The envelope still wraps that
    placeholder so the model sees the surface even for media events."""
    inst = _make_adapter(monkeypatch)

    payload = {
        "id": "msg_img",
        "sender": "alice-bot",
        "conversation_id": "conv_Y",
        "type": "file",
        "content": {
            "type": "file",
            "attachment_id": "att_AAAA",
            "mime_type": "image/png",
        },
    }
    await inst._dispatch_inbound_message(payload, kind="direct")

    event = inst.handle_message.await_args.args[0]
    assert event.text == (
        "[AgentChat DM from @alice-bot]\n"
        "[image attachment att_AAAA]"
    )


# ── System events still drop BEFORE the envelope ──────────────────────────


async def test_system_events_drop_before_envelope_wrap(monkeypatch):
    """Server-side system notifications (member_joined etc.) must NOT
    reach the agent — that 0.1.72 fix predates the envelope and must
    still hold. The envelope is only applied to real user-input messages."""
    inst = _make_adapter(monkeypatch)

    payload = {
        "id": "msg_sys",
        "sender": "agentchat-system",
        "conversation_id": "grp_X",
        "type": "system",
        "content": {"type": "system", "data": {"event": "member_joined"}},
    }
    await inst._dispatch_inbound_message(payload, kind="group")

    # No dispatch — handle_message never called.
    inst.handle_message.assert_not_awaited()
