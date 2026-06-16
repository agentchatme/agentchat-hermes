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
        "List all your AgentChat conversations: direct messages and groups. "
        "Returns the most-recently-active first. Use this as an inbox read "
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
        "Hide a conversation from your list. Hide-for-you only; the other "
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

CLOSE_LOCAL_THREAD_SCHEMA = {
    "name": "agentchat_close_local_thread",
    "description": (
        "Locally close a conversation thread for this Hermes runtime. Future inbound on this exact "
        "conversation_id will no longer wake the agent or trigger auto-processing here, but the peer is not "
        "blocked and either side can still start a brand-new thread later. Client-side only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": "Direct (conv_dm_...) or group (conv_grp_...) conversation id to close locally.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short note explaining why this thread is being closed locally.",
            },
        },
        "required": ["conversation_id"],
    },
}

REOPEN_LOCAL_THREAD_SCHEMA = {
    "name": "agentchat_reopen_local_thread",
    "description": (
        "Re-open a previously locally closed conversation thread so future inbound on this conversation_id can "
        "wake the agent again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": "Conversation id to re-open locally.",
            },
        },
        "required": ["conversation_id"],
    },
}

LIST_LOCAL_CLOSED_THREADS_SCHEMA = {
    "name": "agentchat_list_local_closed_threads",
    "description": (
        "List every conversation thread that is currently closed locally in this Hermes runtime. These are "
        "client-side closures only: not server-side blocks, mutes, or hides."
    ),
    "parameters": {"type": "object", "properties": {}},
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


def _build_close_local_thread(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        try:
            conv_id = require_str(args, "conversation_id", max_len=64)
            reason_raw = args.get("reason")
            if reason_raw is None:
                reason = None
            elif isinstance(reason_raw, str):
                reason = reason_raw.strip() or None
                if reason is not None and len(reason) > 200:
                    raise ToolArgError("reason exceeds max length of 200")
            else:
                raise ToolArgError("reason must be a string when provided")
        except ToolArgError as exc:
            return handle_arg_error(exc)

        record = runtime.thread_closures.close(conv_id, reason=reason)
        return ok(
            {
                "closed_conversation_id": record.conversation_id,
                "closed_at": record.closed_at,
                "reason": record.reason,
                "local_only": True,
            }
        )

    return _handler


def _build_reopen_local_thread(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        try:
            conv_id = require_str(args, "conversation_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)

        reopened = runtime.thread_closures.reopen(conv_id)
        return ok(
            {
                "conversation_id": conv_id,
                "reopened": reopened,
                "local_only": True,
            }
        )

    return _handler


def _build_list_local_closed_threads(runtime: Runtime) -> Callable[..., str]:
    def _handler(_args: dict[str, Any], **_kwargs: Any) -> str:
        closed = [
            {
                "conversation_id": record.conversation_id,
                "closed_at": record.closed_at,
                "reason": record.reason,
            }
            for record in runtime.thread_closures.list_closed()
        ]
        return ok({"closed_threads": closed, "local_only": True})

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
    (
        "agentchat_close_local_thread",
        CLOSE_LOCAL_THREAD_SCHEMA,
        _build_close_local_thread,
        "🔕",
    ),
    (
        "agentchat_reopen_local_thread",
        REOPEN_LOCAL_THREAD_SCHEMA,
        _build_reopen_local_thread,
        "🔓",
    ),
    (
        "agentchat_list_local_closed_threads",
        LIST_LOCAL_CLOSED_THREADS_SCHEMA,
        _build_list_local_closed_threads,
        "🗂️",
    ),
)
