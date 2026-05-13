"""User-message templates the agent_invoker hands to run_conversation.

Kept deliberately short. With ``conversation_history`` passed
alongside (mirroring ``gateway/run.py:15329``), the agent already has
prior turns of THIS conversation. The notification just delivers the
new fact: a message just landed.

The agent learns *how* to handle AgentChat from the bundled skill
(``agentchatme_hermes/skills/SKILL.md``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import InboundEvent


def build_notification_prompt(event: InboundEvent) -> str:
    """Render the user-message that wakes the agent for one inbound.

    Conversation context arrives via ``run_conversation``'s
    ``conversation_history=`` arg, NOT in this prompt — the wake
    itself stays a single line of fact so the model isn't biased
    toward any particular action. The skill carries the
    reply-vs-silence judgment; the prompt just delivers the event.

    Skill availability is hinted parenthetically because Hermes plugin
    skills don't appear in the system prompt's ``<available_skills>``
    index — the agent has to call ``skill_view`` explicitly. Without
    the hint, the agent might never load the etiquette manual.
    """
    if event.conversation_kind == "group":
        # Group: include the conversation_id so the reply tool can
        # route correctly; the [@handle] speaker prefix matters
        # because non-self turns in history come from multiple peers.
        body = (
            f"[agentchat group {event.conversation_id}] "
            f"@{event.sender_handle}: {event.content_text}"
        )
    else:
        # Direct: speaker is implicit from the alternation in history.
        body = f"[agentchat] @{event.sender_handle}: {event.content_text}"

    return body + "\n\n(Behavior manual: skill_view agentchat:agentchat)"
