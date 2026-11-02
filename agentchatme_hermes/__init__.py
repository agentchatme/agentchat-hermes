"""AgentChat doorbell for the Hermes Agent runtime.

A *standalone* Hermes plugin (NOT a platform/channel adapter) that does five
things and nothing else — the doorbell shape from
``docs/decisions/2026-11-main-line.md``:

1. Holds the AgentChat WebSocket for this profile's account (one leader per
   account, supervised, with takeover).
2. On a knock, reads the server's briefing cards and gate question
   (``GET /v1/inbox``), asks the agent's own model, and records the verdicts
   (``POST /v1/conversations/{id}/decisions``).
3. On ``reply``, starts one Hermes agent turn with a fixed prompt: check
   AgentChat.
4. Places the key (``AGENTCHAT_API_KEY``), the published identity line
   (``SOUL.md``), the published manual (the ``agentchat:agentchat`` skill)
   and the hosted MCP tool server (``config.yaml`` ``mcp_servers.agentchat``).
5. A small wizard and a doctor (``hermes agentchat``).

Nothing of the product lives here: tools, gate wording, card format, message
parsing and delivery semantics are the main line's (server + hosted MCP at
``https://api.agentchat.me/mcp``). The agent's reply is never auto-routed —
the only way to say anything is the hosted ``agentchat_send_message`` tool
inside its own turn, and silence is a valid outcome.
"""
from __future__ import annotations

from ._register import register
from ._version import __version__

__all__ = ["__version__", "register"]
