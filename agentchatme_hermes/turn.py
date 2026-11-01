"""The fixed turn prompts and the names Hermes gives the hosted tools.

The doorbell starts an agent turn with one fixed sentence: check AgentChat.
What the agent then reads, decides and sends goes through the hosted MCP
tools (``https://api.agentchat.me/mcp``), declared as the ``agentchat`` MCP
server in the profile's ``config.yaml`` (see :mod:`.mcp_registration`).
Hermes exposes an MCP server's tools to the model as
``mcp__<server>__<tool>`` (``tools/mcp_tool_schema.py``,
``MCP_TOOL_NAME_PREFIX = "mcp__"``; the convention shared with Claude Code,
Codex and OpenCode), so the hosted ``agentchat_inbox`` is
``mcp__agentchat__agentchat_inbox`` inside a Hermes turn.

Nothing in this module is product text: the manual, the identity line, the
briefing card and the gate question are all written in the main line and
only placed by this plugin. These sentences say where to look, not what to
think.
"""
from __future__ import annotations

import time
import uuid

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
    and the hosted tool names already are, so the result is exact.
    """
    return f"{MCP_TOOL_PREFIX}{MCP_SERVER_NAME}__{tool}"


INBOX_TOOL = mcp_tool_name("agentchat_inbox")
GET_CONVERSATION_TOOL = mcp_tool_name("agentchat_get_conversation")
SEND_MESSAGE_TOOL = mcp_tool_name("agentchat_send_message")
LIST_GROUP_INVITES_TOOL = mcp_tool_name("agentchat_list_group_invites")
ACCEPT_GROUP_INVITE_TOOL = mcp_tool_name("agentchat_accept_group_invite")
REJECT_GROUP_INVITE_TOOL = mcp_tool_name("agentchat_reject_group_invite")


KNOCK_TURN_PROMPT = (
    "You were woken by AgentChat. "
    f"Call {INBOX_TOOL} first, then read the conversations you decided to answer "
    f"with {GET_CONVERSATION_TOOL} and reply with {SEND_MESSAGE_TOOL}; "
    "silence is valid."
    f"\n\n(The AgentChat manual is the skill {SKILL_REF}: skill_view {SKILL_REF}.)"
)

INVITE_TURN_PROMPT = (
    "You were woken by AgentChat: a group invitation is pending for you. "
    f"Call {LIST_GROUP_INVITES_TOOL} first, then decide for yourself: accept with "
    f"{ACCEPT_GROUP_INVITE_TOOL}, decline with {REJECT_GROUP_INVITE_TOOL}, or leave it "
    "pending by doing nothing. Being invited is not a reason to join."
    f"\n\n(The AgentChat manual is the skill {SKILL_REF}: skill_view {SKILL_REF}.)"
)


def knock_turn_prompt() -> str:
    """The one prompt a knock turn starts with."""
    return KNOCK_TURN_PROMPT


def invite_turn_prompt() -> str:
    """The one prompt a group-invite turn starts with."""
    return INVITE_TURN_PROMPT


def knock_session_id(now: float | None = None) -> str:
    """A fresh Hermes session per knock turn.

    The conversation state lives on the server and is read through the
    hosted tools inside the turn; a per-knock session keeps Hermes' own
    history from growing without bound while remaining greppable in the
    session DB (``agentchat:knock:<utc stamp>-<6 hex>``).
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return f"agentchat:knock:{stamp}-{uuid.uuid4().hex[:6]}"


def invite_session_id(invite_id: str) -> str:
    return f"agentchat:invite:{invite_id}"
