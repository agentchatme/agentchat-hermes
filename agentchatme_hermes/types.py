"""Cross-module value types.

Kept in their own module so the WS daemon, message queue, agent
invoker, and prompt builder don't form a circular import graph.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

ConversationKind = Literal["direct", "group"]


@dataclass(frozen=True)
class InboundEvent:
    """A single peer-message poke delivered from the WS daemon to the agent.

    Frozen so it can pass between threads (WS thread → invoker thread)
    safely without defensive copies. Sender filtering happens upstream
    in the daemon — events handed to the queue are never self-authored.
    """

    message_id: str
    conversation_id: str
    conversation_kind: ConversationKind
    sender_handle: str
    content_text: str
    received_at: datetime

    @classmethod
    def from_ws_message(cls, payload: dict[str, Any]) -> InboundEvent | None:
        """Build an event from a raw ``message.new`` WS frame payload.

        Returns ``None`` when the payload is malformed (missing required
        fields, unknown conversation kind). Tolerant by design — we'd
        rather skip a single frame we don't understand than crash the
        WS loop.
        """
        msg_id = payload.get("id")
        conv_id = payload.get("conversation_id")
        sender = payload.get("from") or payload.get("sender")
        content = payload.get("content") or {}

        if not isinstance(msg_id, str) or not isinstance(conv_id, str):
            return None
        if not isinstance(sender, str) or not sender:
            return None
        if not isinstance(content, dict):
            return None

        # Server returns "@handle" — normalize to bare handle for
        # consistency with our tool surface (which strips the @ too).
        sender_handle = sender.lstrip("@").lower()

        # The server uses message.content.text for type="text" and
        # message.content.data for everything else. We only surface
        # text in the notification — non-text types still produce an
        # event, but with a placeholder string so the agent at least
        # knows something landed.
        msg_type = payload.get("type", "text")
        if msg_type == "text":
            text = str(content.get("text", "")).strip()
        else:
            text = f"[non-text message: type={msg_type}]"

        conv_kind: ConversationKind = (
            "group" if str(conv_id).startswith("conv_grp_") else "direct"
        )

        created_at_raw = payload.get("created_at")
        try:
            received_at = (
                datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                if isinstance(created_at_raw, str)
                else datetime.now(timezone.utc)
            )
        except ValueError:
            received_at = datetime.now(timezone.utc)

        return cls(
            message_id=msg_id,
            conversation_id=conv_id,
            conversation_kind=conv_kind,
            sender_handle=sender_handle,
            content_text=text,
            received_at=received_at,
        )


@dataclass(frozen=True)
class AgentIdentity:
    """Resolved identity of the local AgentChat account.

    Loaded once at runtime start via ``GET /v1/agents/me``. Used by the
    WS daemon to filter our own outbound from the inbound stream
    (sender_handle == handle).

    **Handle only — no internal ``agt_…`` id.** Internal database ids
    are server-side only. Agents identify themselves and each other by
    handle on the wire. Surfacing the internal id in the plugin would
    be a needless attack-surface expansion (leak through logs, error
    messages, agent-visible context) for zero functional benefit — the
    runtime never needs it; self-echo filtering uses handle equality.
    The server's ``GET /v1/agents/me`` endpoint reflects this contract
    and does not return ``id`` in its response.
    """

    handle: str
