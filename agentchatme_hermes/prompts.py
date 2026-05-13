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
    ``conversation_history=`` arg, NOT in this prompt — keeping the
    prompt one-line-ish minimizes the token cost of the wake itself.
    """
    if event.conversation_kind == "group":
        # Group: tell the agent which group + which speaker; the
        # speaker prefix matters because every "user" role turn in
        # history is from a different peer.
        return (
            f"[agentchat group {event.conversation_id}] "
            f"@{event.sender_handle}: {event.content_text}\n\n"
            "Decide. Silence is a valid outcome."
        )
    # Direct: speaker is implicit from the alternation in history.
    return (
        f"[agentchat] @{event.sender_handle}: {event.content_text}\n\n"
        "Decide. Silence is a valid outcome."
    )
