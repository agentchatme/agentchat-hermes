"""How a Hermes turn is addressed: the served texts, as served for Hermes.

A turn's prompt is built by the SDK kit's receive loop from the served
texts (``GET /v1/texts``, :mod:`.texts`): the wake instruction and the
conversation id, or the invite prompt and the invite id. This module adds
the one fact only this runtime knows — where it keeps the manual (the
``agentchat:agentchat`` skill) — and the session ids. The agent then reads, decides and sends through the hosted MCP
tools (``https://api.agentchat.me/mcp``), declared as the ``agentchat`` MCP
server in the profile's ``config.yaml`` (see :mod:`.mcp_registration`).

Tool names
──────────
Hermes exposes an MCP server's tools to the model as ``mcp__<server>__<tool>``
(``tools/mcp_tool_schema.py``, ``MCP_TOOL_NAME_PREFIX = "mcp__"``; the
convention shared with Claude Code, Codex and OpenCode), so the hosted
``agentchat_inbox`` is ``mcp__agentchat__agentchat_inbox`` inside a Hermes
turn. The server spells them that way in every text it serves for
``runtime=hermes`` (``GET /v1/texts?runtime=hermes``), so the texts are used
as served; only the tray's gate question names them bare, and the SDK's gate
helper spells those with :func:`agentchatme.gate.mcp_tool_namer`.
"""
from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentchatme.types import Texts

# Name of the MCP server entry this plugin registers. Changing it changes
# every tool name the model sees — keep in lockstep with mcp_registration.
MCP_SERVER_NAME = "agentchat"

# Hermes' registry prefix and delimiter (tools/mcp_tool_schema.py, v0.22.5).
MCP_TOOL_PREFIX = "mcp__"

# The Hermes skill namespace for the bundled manual: ``<plugin>:<skill>``.
SKILL_REF = "agentchat:agentchat"

def mcp_tool_name(tool: str) -> str:
    """``agentchat_inbox`` → ``mcp__agentchat__agentchat_inbox``.

    Hermes sanitizes name components to ``[A-Za-z0-9_]``; our server name
    and the hosted tool names already are, so the result is exact. Equal to
    ``agentchatme.gate.mcp_tool_namer("agentchat")(tool)``; written out so the
    CLI can import this module where the SDK is missing.
    """
    return f"{MCP_TOOL_PREFIX}{MCP_SERVER_NAME}__{tool}"


INBOX_TOOL = mcp_tool_name("agentchat_inbox")


# Where this runtime keeps the manual: a placement fact, not product text.
MANUAL_POINTER = f"(The AgentChat manual is the skill {SKILL_REF}: skill_view {SKILL_REF}.)"


def with_manual(prompt: str) -> str:
    """A turn's prompt as the kit built it (the served wake instruction and
    the conversation id, or the served invite prompt and the invite id),
    then where this runtime keeps the manual."""
    return f"{prompt.strip()}\n\n{MANUAL_POINTER}"


def identity_line(texts: Texts) -> str:
    """The served identity line for this agent, as served for Hermes."""
    return texts.identity_line.strip()


def identity_line_template(texts: Texts) -> str:
    """The served template (``<handle>`` unfilled): what
    :func:`.soul_anchor.read_anchor_handle` recognises an unmarked block by."""
    return texts.identity_line_template.strip()


def wake_session_id(now: float | None = None) -> str:
    """A fresh Hermes session per wake turn.

    The conversation state lives on the server and is read through the
    hosted tools inside the turn; a per-wake session keeps Hermes' own
    history from growing without bound while remaining greppable in the
    session DB (``agentchat:wake:<utc stamp>-<6 hex>``).
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return f"agentchat:wake:{stamp}-{uuid.uuid4().hex[:6]}"


def invite_session_id(invite_id: str) -> str:
    return f"agentchat:invite:{invite_id}"
