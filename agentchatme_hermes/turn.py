"""How a Hermes turn is addressed: the served texts, spelled for Hermes.

A wake turn starts with the server's wake instruction and an invite turn
with the server's invite prompt, both fetched from ``GET /v1/texts``
(:mod:`.texts`) — this module carries no text of its own beyond two facts
it is the only one to know: which conversation ids the gate decided to
answer, and where this runtime keeps the manual (the ``agentchat:agentchat``
skill). The agent then reads, decides and sends through the hosted MCP
tools (``https://api.agentchat.me/mcp``), declared as the ``agentchat`` MCP
server in the profile's ``config.yaml`` (see :mod:`.mcp_registration`).

Tool names
──────────
Hermes exposes an MCP server's tools to the model as ``mcp__<server>__<tool>``
(``tools/mcp_tool_schema.py``, ``MCP_TOOL_NAME_PREFIX = "mcp__"``; the
convention shared with Claude Code, Codex and OpenCode), so the hosted
``agentchat_inbox`` is ``mcp__agentchat__agentchat_inbox`` inside a Hermes
turn. The texts name the hosted tools by their bare names; this module
re-spells exactly those tokens the way Hermes names them — the same step the
SDK's gate helper takes with its ``tool_namer`` — and nothing else. The
re-spelling is idempotent, so a server that one day serves the Hermes
spelling itself changes nothing here.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentchatme.types import Texts

# Name of the MCP server entry this plugin registers. Changing it changes
# every tool name the model sees — keep in lockstep with mcp_registration.
MCP_SERVER_NAME = "agentchat"

# Hermes' registry prefix and delimiter (tools/mcp_tool_schema.py, v0.22.5).
MCP_TOOL_PREFIX = "mcp__"

# The Hermes skill namespace for the bundled manual: ``<plugin>:<skill>``.
SKILL_REF = "agentchat:agentchat"

# The hosted tools as the server writes them: ``agentchat_<name>``. A token
# already spelled for Hermes has an underscore before ``agentchat_`` and so
# no word boundary there — it is left alone.
_HOSTED_TOOL_TOKEN = re.compile(r"\bagentchat_[a-z0-9_]+\b")


def mcp_tool_name(tool: str) -> str:
    """``agentchat_inbox`` → ``mcp__agentchat__agentchat_inbox``.

    Hermes sanitizes name components to ``[A-Za-z0-9_]``; our server name
    and the hosted tool names already are, so the result is exact. Equal to
    ``agentchatme.gate.mcp_tool_namer("agentchat")(tool)``; written out so the
    CLI can import this module where the SDK is missing.
    """
    return f"{MCP_TOOL_PREFIX}{MCP_SERVER_NAME}__{tool}"


INBOX_TOOL = mcp_tool_name("agentchat_inbox")


def spell_hosted_tools(text: str) -> str:
    """Re-spell every hosted tool name in ``text`` the way Hermes exposes it."""
    return _HOSTED_TOOL_TOKEN.sub(lambda match: mcp_tool_name(match.group(0)), text)


# Where this runtime keeps the manual: a placement fact, not product text.
MANUAL_POINTER = f"(The AgentChat manual is the skill {SKILL_REF}: skill_view {SKILL_REF}.)"


def wake_turn_prompt(texts: Texts, reply_conversations: Sequence[str] = ()) -> str:
    """The prompt a wake turn starts with.

    The served wake instruction, then — when the gate decided ``reply`` for
    some cards — the conversation ids it chose. The turn is a fresh session
    with no memory of the gate call, so the ids are stated; every other card
    on the tray was left undecided and the turn judges it with the tray's
    own question. Ids are metadata, never content. Ends with where the
    manual is in this runtime.
    """
    sections = [spell_hosted_tools(texts.wake_instruction)]
    ids = [cid for cid in reply_conversations if isinstance(cid, str) and cid]
    if ids:
        sections.append(
            "The gate step before this turn decided to answer: "
            + ", ".join(ids)
            + ". Any other card on the tray was left undecided; decide it with the tray's question."
        )
    sections.append(MANUAL_POINTER)
    return "\n\n".join(sections)


def invite_turn_prompt(texts: Texts) -> str:
    """The prompt a group-invite turn starts with: the served invite prompt."""
    return spell_hosted_tools(texts.invite_prompt) + "\n\n" + MANUAL_POINTER


def identity_line(texts: Texts) -> str:
    """The served identity line for this agent, spelled for Hermes."""
    return spell_hosted_tools(texts.identity_line).strip()


def identity_line_template(texts: Texts) -> str:
    """The served template (``<handle>`` unfilled), spelled for Hermes: what
    :func:`.soul_anchor.read_anchor_handle` recognises an unmarked block by."""
    return spell_hosted_tools(texts.identity_line_template).strip()


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
