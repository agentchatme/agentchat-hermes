"""Cross-module value types.

Kept in their own module so the WS daemon, invoker and runtime don't form a
circular import graph. Message bodies never appear here: a ``message.new``
frame is a wake, and the daemon reads only the sender (to skip its own
echo) and the ids (for the log line). What the message says is read by the
agent, through the hosted tools, inside its turn.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class GroupInviteEvent:
    """A pending group invitation delivered from the WS daemon to the agent.

    Built from a ``group.invite.received`` WS frame. Frozen so it can cross the
    WS thread → invoker thread boundary without defensive copies. This is a
    *consent* event: the agent decides whether to accept or reject — the plugin
    never auto-joins (that would let any admin conscript any agent into any
    group, an abuse vector at platform scale).

    ``invite_id`` and ``group_id`` are load-bearing (you cannot act on the
    invite without them); everything else is informational and degrades
    gracefully so a partial/legacy frame still produces a usable event.
    """

    invite_id: str
    group_id: str
    group_name: str
    inviter_handle: str
    received_at: datetime
    group_description: str | None = None
    member_count: int | None = None

    @classmethod
    def from_ws_frame(cls, payload: dict[str, Any]) -> GroupInviteEvent | None:
        """Build an event from a raw ``group.invite.received`` frame payload.

        Returns ``None`` only when the load-bearing ids are missing/malformed —
        tolerant by design: skip a frame we can't act on rather than crash the
        WS loop.
        """
        invite_id = payload.get("id")
        group_id = payload.get("group_id")
        if not isinstance(invite_id, str) or not invite_id:
            return None
        if not isinstance(group_id, str) or not group_id:
            return None

        name_raw = payload.get("group_name")
        group_name = name_raw if isinstance(name_raw, str) and name_raw else group_id

        inviter_raw = payload.get("inviter_handle")
        inviter_handle = (
            inviter_raw.lstrip("@").lower()
            if isinstance(inviter_raw, str) and inviter_raw
            else "unknown"
        )

        desc_raw = payload.get("group_description")
        group_description = desc_raw if isinstance(desc_raw, str) else None
        mc_raw = payload.get("group_member_count")
        member_count = mc_raw if isinstance(mc_raw, int) else None

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
            invite_id=invite_id,
            group_id=group_id,
            group_name=group_name,
            inviter_handle=inviter_handle,
            received_at=received_at,
            group_description=group_description,
            member_count=member_count,
        )


@dataclass(frozen=True)
class AgentIdentity:
    """Resolved identity of the local AgentChat account.

    Loaded once at runtime start via ``GET /v1/agents/me``. Used by the
    WS daemon to filter our own outbound from the inbound stream
    (sender_handle == handle).

    **Handle only — no internal ``agt_…`` id.** Internal database ids are
    server-side only. Agents identify themselves and each other by
    handle on the wire. Surfacing the internal id in the plugin would
    be a needless attack-surface expansion (leak through logs, error
    messages, agent-visible context) for zero functional benefit — the
    runtime never needs it; self-echo filtering uses handle equality.
    The server's ``GET /v1/agents/me`` endpoint reflects this contract
    and does not return ``id`` in its response.
    """

    handle: str
