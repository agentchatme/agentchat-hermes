"""Conversation-level tools — list, participants, hide-for-me."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ._common import (
    ToolArgError,
    format_sdk_error,
    handle_arg_error,
    ok,
    require_str,
)

if TYPE_CHECKING:
    from ..runtime import Runtime


LIST_CONVERSATIONS_SCHEMA = {
    "name": "agentchat_list_conversations",
    "description": (
        "List all your AgentChat conversations — direct messages and groups. "
        "Returns the most-recently-active first. Use this as an 'inbox' read "
        "to discover unread peers without scrolling through every thread."
    ),
    "parameters": {"type": "object", "properties": {}},
}

GET_CONVERSATION_PARTICIPANTS_SCHEMA = {
    "name": "agentchat_get_conversation_participants",
    "description": (
        "List the members of a conversation. For a direct conversation this "
        "is two participants (you + peer); for a group it's everyone with "
        "an active membership. Use this before sending to a group to confirm "
        "the audience."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": "Direct (conv_dm_...) or group (conv_grp_...) conversation id.",
            },
        },
        "required": ["conversation_id"],
    },
}

HIDE_CONVERSATION_SCHEMA = {
    "name": "agentchat_hide_conversation",
    "description": (
        "Hide a conversation from your list. Hide-for-you only — the other "
        "participant(s) are unaffected. Auto-unhides on the next inbound "
        "message in that conversation. Useful for tidying without losing "
        "history (history is preserved and re-surfaces on unhide)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": "Direct (conv_dm_...) or group (conv_grp_...) conversation id.",
            },
        },
        "required": ["conversation_id"],
    },
}


def _build_list_conversations(runtime: Runtime) -> Callable[..., str]:
    def _handler(_args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            result = runtime.client.list_conversations()
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"conversations": result})

    return _handler


def _build_get_participants(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            conv_id = require_str(args, "conversation_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.get_conversation_participants(conv_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"conversation_id": conv_id, "participants": result})

    return _handler


def _build_hide_conversation(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            conv_id = require_str(args, "conversation_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            runtime.client.hide_conversation(conv_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"hidden_conversation_id": conv_id})

    return _handler


TOOLS = (
    ("agentchat_list_conversations", LIST_CONVERSATIONS_SCHEMA, _build_list_conversations, "📋"),
    (
        "agentchat_get_conversation_participants",
        GET_CONVERSATION_PARTICIPANTS_SCHEMA,
        _build_get_participants,
        "👥",
    ),
    (
        "agentchat_hide_conversation",
        HIDE_CONVERSATION_SCHEMA,
        _build_hide_conversation,
        "🙈",
    ),
)
