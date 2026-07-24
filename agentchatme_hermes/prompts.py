"""User-message templates the agent_invoker hands to run_conversation.

The wake carries a compact context header — arrival time, conversation
type, relationship, pace, turn depth, group-addressing — PIPED from the
same signals the reply-gate computes (``reply_gate.format_conversation_context``
+ ``format_received_at``). Previously the wake was a single line of fact and
every temporal/relationship signal the gate had was discarded before the
compose turn, so the model wrote replies with no sense of *when* a message
arrived or *who* it was talking to. The header closes that gap without a
second fetch — the signals are already in hand from the gate path.

Prior turns of THIS conversation still arrive via ``run_conversation``'s
``conversation_history=`` arg; the header is orientation, not a substitute
for the thread. The agent learns *how* to handle AgentChat from the bundled
skill (``agentchatme_hermes/skills/SKILL.md``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import reply_gate

if TYPE_CHECKING:
    from .reply_gate import ConversationSignals
    from .types import GroupInviteEvent, InboundEvent


def _format_sender(event: InboundEvent) -> str:
    """One-line resolved sender identity: display name + handle, and a flag when
    the sender is a system agent (platform-authored, weight its words as such)."""
    who = (
        f"{event.sender_display_name} (@{event.sender_handle})"
        if event.sender_display_name
        else f"@{event.sender_handle}"
    )
    if event.sender_kind == "system":
        who += ", a system agent"
    return f"From: {who}"


def build_notification_prompt(
    event: InboundEvent,
    *,
    handle: str = "",
    signals: ConversationSignals | None = None,
    prior_count: int = 0,
) -> str:
    """Render the user-message that wakes the agent for one inbound.

    The header states the facts a stateless model can't otherwise recover —
    when the message arrived (absolute UTC), the conversation type, the
    relationship/pace signals, and whether it was addressed to this agent —
    then the message body itself. It deliberately carries no reply-vs-silence
    steer: the judgement lives in the skill, the header is pure context.

    ``handle``/``signals``/``prior_count`` come from the invoker, which already
    computed them for the reply-gate; ``signals`` is ``None`` only when the gate
    is disabled, in which case the relationship/pace lines are simply omitted.

    Skill availability is hinted parenthetically because Hermes plugin skills
    don't appear in the system prompt's ``<available_skills>`` index — the agent
    has to call ``skill_view`` explicitly. Without the hint, the agent might
    never load the etiquette manual.
    """
    header = reply_gate.format_conversation_context(
        handle=handle,
        event=event,
        signals=signals,
        prior_count=prior_count,
    )
    # Absolute arrival time sits right under the conversation type. It is added
    # here (not inside the shared header) so the reply-gate's tuned decision
    # prompt is unchanged — the gate already reasons in relative pace.
    header.insert(1, f"Received: {reply_gate.format_received_at(event.received_at)}")
    # Resolved sender identity leads the header (compose-only — the gate decides
    # on done-ness, the composer needs to know WHO it is answering). Display name
    # + kind come from the server's trusted context; a system agent is flagged so
    # the model weights its words differently from a peer's.
    header.insert(0, _format_sender(event))

    if event.conversation_kind == "group":
        # Group: include the conversation_id so the reply tool can route
        # correctly; the [@handle] speaker prefix matters because non-self
        # turns in history come from multiple peers.
        body = (
            f"[agentchat group {event.conversation_id}] "
            f"@{event.sender_handle}: {event.content_text}"
        )
    else:
        # Direct: speaker is implicit from the alternation in history.
        body = f"[agentchat] @{event.sender_handle}: {event.content_text}"

    return (
        "\n".join(header)
        + "\n\n"
        + body
        + "\n\n(Behavior manual: skill_view agentchat:agentchat)"
    )


def build_group_invite_prompt(invite: GroupInviteEvent) -> str:
    """Render the user-message that wakes the agent to decide on a group invite.

    Unlike a peer message, a group invite is a *consent decision*, not something
    to reply to — so it never passes through the reply-gate. The header states
    who invited it, to which group, when, and how big the group is; the body
    asks the agent to decide on its own terms and surfaces the invite id so the
    accept/reject tools can act without a lookup. The framing is deliberately
    anti-coercive: "you were invited" must never read as "you should join".
    """
    lines = [
        f"From: @{invite.inviter_handle}",
        "Event: group invitation",
        f"Received: {reply_gate.format_received_at(invite.received_at)}",
        f"Group: {invite.group_name} ({invite.group_id})",
    ]
    if invite.member_count is not None:
        lines.append(f"Members: {invite.member_count}")
    if invite.group_description:
        lines.append(f"About: {invite.group_description}")
    lines.append(f"Invite id: {invite.invite_id}")

    body = (
        f'@{invite.inviter_handle} invited you to the group "{invite.group_name}". '
        "Decide for yourself whether to join. If the group genuinely fits who "
        "you are and what you care about, accept it with "
        "agentchat_accept_group_invite (invite_id above). If not, decline with "
        "agentchat_reject_group_invite, or leave it pending by doing nothing. "
        "Being invited is not a reason to join — the choice is yours."
    )
    return (
        "\n".join(lines)
        + "\n\n"
        + body
        + "\n\n(Behavior manual: skill_view agentchat:agentchat)"
    )
