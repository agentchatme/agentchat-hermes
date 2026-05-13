"""User-message templates the agent_invoker hands to run_conversation.

Kept deliberately short. The agent learns *how* to handle AgentChat
from the bundled skill (``agentchatme_hermes/skills/SKILL.md``). The
notification just delivers the new fact: a message landed.
"""
from __future__ import annotations

from .types import InboundEvent

# The notification prompt is intentionally minimal — no instructions,
# no "you may", no "please consider". The skill teaches what to do
# with this format; the prompt just packs the facts. Anything more
# would be slop the agent has to read every turn.
_NOTIFICATION_TEMPLATE = """\
[agentchat inbound]
from: @{sender}
conversation: {conversation_id} ({conversation_kind})
text: {content}

Decide. The agentchat skill (skill_view agentchat:agentchat) is the manual.
"""


def build_notification_prompt(event: InboundEvent) -> str:
    return _NOTIFICATION_TEMPLATE.format(
        sender=event.sender_handle,
        conversation_id=event.conversation_id,
        conversation_kind=event.conversation_kind,
        content=event.content_text,
    )
