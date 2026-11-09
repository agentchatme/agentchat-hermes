"""Cross-module value types.

Kept in their own module so the WS daemon, invoker and runtime don't form a
circular import graph. Message bodies never appear here: a ``message.new``
frame is a wake, and the daemon reads only the sender (to skip its own
echo) and the ids (for the log line). What the message says is read by the
agent, through the hosted tools, inside its turn.
"""
from __future__ import annotations

from dataclasses import dataclass


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
