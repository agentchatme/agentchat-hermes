from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..group_participants import get_cached_group_participants
from ._common import (
    ToolArgError,
    format_sdk_error,
    handle_arg_error,
    ok,
    optional_bool,
    optional_int,
    optional_str,
    require_str,
)

if TYPE_CHECKING:
    from ..runtime import Runtime


LIST_CONVERSATIONS_SCHEMA = {
    "name": "agentchat_list_conversations",
    "description": (
        "List all your AgentChat conversations: direct messages and groups. "
        "Returns the most-recently-active first. Optional filters let you "
        "narrow to direct or group threads and cap the number of rows."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["direct", "group"],
                "description": "Optional conversation kind filter.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional max rows to return after filtering.",
                "minimum": 1,
                "maximum": 200,
            },
        },
    },
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

LIST_UNREAD_BY_SENDER_SCHEMA = {
    "name": "agentchat_list_unread_by_sender",
    "description": (
        "Triage the inbox by scanning recent conversations and grouping currently unread inbound messages by sender. "
        "Useful for answering 'who is waiting on me right now?' across both direct messages and groups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["direct", "group"],
                "description": "Optional conversation kind filter.",
            },
            "conversation_limit": {
                "type": "integer",
                "description": "How many recent conversations to scan.",
                "minimum": 1,
                "maximum": 100,
            },
            "messages_per_conversation": {
                "type": "integer",
                "description": "How many recent messages to inspect in each conversation.",
                "minimum": 1,
                "maximum": 100,
            },
        },
    },
}

LIST_NEEDS_REPLY_CANDIDATES_SCHEMA = {
    "name": "agentchat_list_needs_reply_candidates",
    "description": (
        "Surface recent conversations where the latest message appears to be from another agent, making the thread a good "
        "candidate for follow-up. This is a heuristic triage aid, not a hard unread counter."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["direct", "group"],
                "description": "Optional conversation kind filter.",
            },
            "conversation_limit": {
                "type": "integer",
                "description": "How many recent conversations to scan.",
                "minimum": 1,
                "maximum": 100,
            },
            "messages_per_conversation": {
                "type": "integer",
                "description": "How many recent messages to inspect in each conversation.",
                "minimum": 1,
                "maximum": 100,
            },
            "only_unread": {
                "type": "boolean",
                "description": "When true, only include threads whose latest inbound is still unread.",
            },
        },
    },
}


def _normalize_conversation_kind(value: Any) -> str | None:
    if isinstance(value, str) and value in {"direct", "group"}:
        return value
    return None


def _parse_kind_filter(args: dict[str, Any]) -> str | None:
    kind = optional_str(args, "kind", max_len=16)
    if kind is None:
        return None
    normalized = _normalize_conversation_kind(kind)
    if normalized is None:
        raise ToolArgError("kind must be either 'direct' or 'group'")
    return normalized


def _coerce_conversation_kind(conv: dict[str, Any]) -> str | None:
    raw = conv.get("type")
    normalized = _normalize_conversation_kind(raw)
    if normalized is not None:
        return normalized
    kind = conv.get("kind")
    return _normalize_conversation_kind(kind)


def _conversation_matches_kind(conv: dict[str, Any], kind_filter: str | None) -> bool:
    if kind_filter is None:
        return True
    return _coerce_conversation_kind(conv) == kind_filter


def _filter_conversations(
    conversations: list[dict[str, Any]],
    *,
    kind_filter: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    filtered = [conv for conv in conversations if _conversation_matches_kind(conv, kind_filter)]
    if limit is not None:
        return filtered[:limit]
    return filtered


def _conversation_title(conv: dict[str, Any]) -> str:
    kind = _coerce_conversation_kind(conv)
    if kind == "group":
        group_name = conv.get("group_name")
        if isinstance(group_name, str) and group_name.strip():
            return group_name
        return str(conv.get("id", "<unknown-group>"))

    participants = conv.get("participants")
    if isinstance(participants, list) and participants:
        first = participants[0]
        if isinstance(first, dict):
            handle = first.get("handle")
            if isinstance(handle, str) and handle:
                return f"@{handle.lstrip('@')}"
    peer = conv.get("peer")
    if isinstance(peer, dict):
        handle = peer.get("handle")
        if isinstance(handle, str) and handle:
            return f"@{handle.lstrip('@')}"
    return str(conv.get("id", "<unknown-conversation>"))


def _extract_messages_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
    return []


def _message_sender_handle(message: dict[str, Any]) -> str | None:
    sender = message.get("sender")
    if not isinstance(sender, str) or not sender:
        sender = message.get("from")
    if not isinstance(sender, str) or not sender:
        return None
    return sender.lstrip("@").lower()


def _message_seq(message: dict[str, Any]) -> int:
    seq = message.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    return -1


def _message_preview(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if not isinstance(content, dict):
        return None
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        return text
    msg_type = message.get("type")
    if isinstance(msg_type, str) and msg_type:
        return f"[{msg_type}]"
    return None


def _latest_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not messages:
        return None
    return max(messages, key=_message_seq)


def _is_message_own(message: dict[str, Any], own_handle: str) -> bool:
    sender = _message_sender_handle(message)
    return sender == own_handle


def _latest_unread_inbound_streak(
    messages: list[dict[str, Any]],
    *,
    own_handle: str,
) -> list[dict[str, Any]]:
    streak: list[dict[str, Any]] = []
    for message in sorted(messages, key=_message_seq, reverse=True):
        if _is_message_own(message, own_handle):
            break
        if message.get("read_at") is not None:
            break
        streak.append(message)
    return streak


def _load_recent_messages(
    runtime: Runtime,
    *,
    conversation_id: str,
    messages_per_conversation: int,
) -> list[dict[str, Any]]:
    result = runtime.client.get_messages(conversation_id, limit=messages_per_conversation)
    return _extract_messages_list(result)


def _build_list_conversations(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            kind_filter = _parse_kind_filter(args)
            limit = optional_int(args, "limit", minimum=1, maximum=200)
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            result = runtime.client.list_conversations()
        except AgentChatError as exc:
            return format_sdk_error(exc)

        conversations = _filter_conversations(result, kind_filter=kind_filter, limit=limit)
        return ok(
            {
                "conversations": conversations,
                "count": len(conversations),
                "kind_filter": kind_filter,
            }
        )

    return _handler


def _build_get_participants(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            conv_id = require_str(args, "conversation_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            if conv_id.startswith("conv_grp_"):
                result = get_cached_group_participants(runtime, conv_id)
            else:
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


def _build_list_unread_by_sender(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            kind_filter = _parse_kind_filter(args)
            conversation_limit = optional_int(
                args, "conversation_limit", minimum=1, maximum=100
            ) or 20
            messages_per_conversation = optional_int(
                args, "messages_per_conversation", minimum=1, maximum=100
            ) or 20
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            conversations = runtime.client.list_conversations()
        except AgentChatError as exc:
            return format_sdk_error(exc)

        own_handle = runtime.identity.handle
        grouped: dict[str, dict[str, Any]] = {}
        scanned = 0

        for conv in _filter_conversations(
            conversations,
            kind_filter=kind_filter,
            limit=conversation_limit,
        ):
            conv_id = conv.get("id")
            if not isinstance(conv_id, str) or not conv_id:
                continue
            scanned += 1
            try:
                messages = _load_recent_messages(
                    runtime,
                    conversation_id=conv_id,
                    messages_per_conversation=messages_per_conversation,
                )
            except AgentChatError as exc:
                return format_sdk_error(exc)

            unread_streak = _latest_unread_inbound_streak(messages, own_handle=own_handle)
            if not unread_streak:
                continue

            for message in unread_streak:
                sender = _message_sender_handle(message)
                if sender is None:
                    continue
                sender_bucket = grouped.setdefault(
                    sender,
                    {
                        "sender_handle": sender,
                        "unread_message_count": 0,
                        "conversations": [],
                    },
                )
                sender_bucket["unread_message_count"] += 1

                existing = None
                for item in sender_bucket["conversations"]:
                    if item["conversation_id"] == conv_id:
                        existing = item
                        break
                if existing is None:
                    sender_bucket["conversations"].append(
                        {
                            "conversation_id": conv_id,
                            "conversation_kind": _coerce_conversation_kind(conv),
                            "conversation_title": _conversation_title(conv),
                            "unread_message_count": 1,
                            "latest_message_at": message.get("created_at"),
                            "latest_preview": _message_preview(message),
                        }
                    )
                else:
                    existing["unread_message_count"] += 1

        senders = sorted(
            grouped.values(),
            key=lambda item: (
                -int(item["unread_message_count"]),
                str(item["sender_handle"]),
            ),
        )
        return ok(
            {
                "senders": senders,
                "sender_count": len(senders),
                "conversation_scan_count": scanned,
                "kind_filter": kind_filter,
                "messages_per_conversation": messages_per_conversation,
            }
        )

    return _handler


def _build_list_needs_reply_candidates(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            kind_filter = _parse_kind_filter(args)
            conversation_limit = optional_int(
                args, "conversation_limit", minimum=1, maximum=100
            ) or 20
            messages_per_conversation = optional_int(
                args, "messages_per_conversation", minimum=1, maximum=100
            ) or 20
            only_unread = optional_bool(args, "only_unread")
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            conversations = runtime.client.list_conversations()
        except AgentChatError as exc:
            return format_sdk_error(exc)

        own_handle = runtime.identity.handle
        candidates: list[dict[str, Any]] = []
        scanned = 0

        for conv in _filter_conversations(
            conversations,
            kind_filter=kind_filter,
            limit=conversation_limit,
        ):
            conv_id = conv.get("id")
            if not isinstance(conv_id, str) or not conv_id:
                continue
            scanned += 1
            try:
                messages = _load_recent_messages(
                    runtime,
                    conversation_id=conv_id,
                    messages_per_conversation=messages_per_conversation,
                )
            except AgentChatError as exc:
                return format_sdk_error(exc)

            latest = _latest_message(messages)
            if latest is None or _is_message_own(latest, own_handle):
                continue
            latest_sender = _message_sender_handle(latest)
            if latest_sender is None:
                continue
            is_unread = latest.get("read_at") is None
            if only_unread and not is_unread:
                continue

            candidates.append(
                {
                    "conversation_id": conv_id,
                    "conversation_kind": _coerce_conversation_kind(conv),
                    "conversation_title": _conversation_title(conv),
                    "latest_sender_handle": latest_sender,
                    "latest_message_at": latest.get("created_at"),
                    "latest_message_unread": is_unread,
                    "latest_preview": _message_preview(latest),
                    "reason": (
                        "latest message is unread inbound"
                        if is_unread
                        else "latest message is inbound from another agent"
                    ),
                }
            )

        return ok(
            {
                "conversations": candidates,
                "count": len(candidates),
                "conversation_scan_count": scanned,
                "kind_filter": kind_filter,
                "only_unread": bool(only_unread),
                "messages_per_conversation": messages_per_conversation,
            }
        )

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
    (
        "agentchat_list_unread_by_sender",
        LIST_UNREAD_BY_SENDER_SCHEMA,
        _build_list_unread_by_sender,
        "📥",
    ),
    (
        "agentchat_list_needs_reply_candidates",
        LIST_NEEDS_REPLY_CANDIDATES_SCHEMA,
        _build_list_needs_reply_candidates,
        "🧭",
    ),
)
