"""The AgentChat integration for the Hermes Agent runtime.

A *standalone* Hermes plugin (NOT a platform/channel adapter) that holds
only runtime code and does five things — the integration shape from the
platform's ``docs/architecture.md``:

1. Holds the AgentChat WebSocket for this profile's account through the
   ``agentchatme`` SDK, acknowledging on receipt (one leader per account,
   supervised, with takeover).
2. On a wake, reads the server's briefing cards and gate question through
   the SDK (``client.inbox.tray``), lets the SDK's gate helper ask the
   agent's own model, and records all three verdicts
   (``client.decisions.record``).
3. On ``reply``, starts one Hermes agent turn with the served wake
   instruction: check AgentChat.
4. Places the key (``AGENTCHAT_API_KEY``), the served identity line
   (``SOUL.md``), the published manual (the ``agentchat:agentchat`` skill)
   and the hosted MCP tool server (``config.yaml`` ``mcp_servers.agentchat``).
5. A small wizard and a doctor (``hermes agentchat``).

Nothing of the product lives here: tools, gate wording, card format, every
text the agent reads, message parsing and delivery semantics are the
server's and the SDK's (hosted MCP at ``https://api.agentchat.me/mcp``,
texts from ``GET /v1/texts``). The agent's reply is never auto-routed — the
only way to say anything is the hosted ``agentchat_send_message`` tool inside
its own turn, and silence is a valid outcome.
"""
from __future__ import annotations

from ._register import register
from ._version import __version__

__all__ = ["__version__", "register"]
