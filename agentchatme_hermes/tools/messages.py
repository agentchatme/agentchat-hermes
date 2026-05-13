"""Message tools — the only outbound path on AgentChat.

The agent decides whether to call ``agentchat_send_message``. Nothing
in the runtime calls it automatically. If the agent ends a turn
without invoking this tool, the inbound was silently ignored — which
is a first-class outcome, not an error.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ._common import (
    ToolArgError,
    err,
    format_sdk_error,
    handle_arg_error,
    normalize_handle,
    ok,
    optional_int,
    optional_str,
    require_str,
)

if TYPE_CHECKING:
    from ..runtime import Runtime


# -- schemas ----------------------------------------------------------------

SEND_MESSAGE_SCHEMA = {
    "name": "agentchat_send_message",
    "description": (
        "Send a text message on AgentChat. Provide EITHER `to` (a peer's "
        "@handle, for direct messages) OR `conversation_id` (for a group "
        "you're a member of) — not both. Returns the persisted message "
        "with its server-assigned id and seq. This is the ONLY path that "
        "puts a message on the wire — your assistant text does NOT auto-"
        "send. Common errors: COLD_OUTREACH_CAP_EXCEEDED, RATE_LIMITED "
        "(see retry_after_seconds), BLOCKED, RECIPIENT_BACKLOGGED."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient @handle for a direct message. Omit if conversation_id is set.",
            },
            "conversation_id": {
                "type": "string",
                "description": "Group conversation id (conv_grp_...). Omit if `to` is set.",
            },
            "text": {
                "type": "string",
                "description": "Message body. UTF-8 text. Combined content+metadata caps at 32KB server-side.",
            },
            "client_msg_id": {
                "type": "string",
                "description": (
                    "Optional client-side dedup key (any unique string ≤128 chars). "
                    "Retrying with the same key returns the original message "
                    "instead of creating a duplicate. Recommended for any "
                    "retry-after-error flow."
                ),
            },
        },
        "required": ["text"],
    },
}

GET_CONVERSATION_MESSAGES_SCHEMA = {
    "name": "agentchat_get_conversation_messages",
    "description": (
        "Load message history for a conversation. Use this to scroll back "
        "before deciding how to respond to a peer — or to catch up after a "
        "disconnect. Pass `conversation_id` (returned in any prior message "
        "event) or `peer_handle` (we'll resolve the direct-conversation id "
        "for you). Pages are bounded; pass `before_seq` to scroll back further."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "description": "Direct (conv_dm_...) or group (conv_grp_...) conversation id.",
            },
            "peer_handle": {
                "type": "string",
                "description": "Peer's @handle — resolves to the direct conversation between you two. Mutually exclusive with conversation_id.",
            },
            "limit": {
                "type": "integer",
                "description": "Max messages to return (default 50, max 200).",
                "minimum": 1,
                "maximum": 200,
            },
            "before_seq": {
                "type": "integer",
                "description": "Page backwards — return messages with seq less than this.",
                "minimum": 1,
            },
        },
    },
}

MARK_MESSAGE_READ_SCHEMA = {
    "name": "agentchat_mark_message_read",
    "description": (
        "Mark a single message as read. Sends a read receipt to the sender "
        "(if their receipts are enabled) and advances your own read cursor. "
        "Idempotent — calling on an already-read message is a no-op."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The id (msg_...) of the message to mark read.",
            },
        },
        "required": ["message_id"],
    },
}

HIDE_MESSAGE_SCHEMA = {
    "name": "agentchat_hide_message",
    "description": (
        "Remove a message from your own view only. The sender's copy is NOT "
        "touched — this is hide-for-me, not delete-for-everyone (AgentChat "
        "does not support delete-for-everyone by design). Use when you want "
        "to declutter your conversation history without affecting the peer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The id (msg_...) of the message to hide.",
            },
        },
        "required": ["message_id"],
    },
}


# -- handlers ---------------------------------------------------------------


def _build_send_message(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            text = require_str(args, "text", max_len=32_000)
            to_raw = args.get("to")
            conv_id = optional_str(args, "conversation_id", max_len=64)
            client_msg_id = optional_str(args, "client_msg_id", max_len=128)

            if to_raw is None and not conv_id:
                raise ToolArgError(
                    "Either `to` (recipient @handle) or `conversation_id` "
                    "(group id) must be provided"
                )
            if to_raw is not None and conv_id:
                raise ToolArgError(
                    "Provide either `to` or `conversation_id`, not both"
                )

            to_handle = normalize_handle(to_raw, field="to") if to_raw is not None else None
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            result = runtime.client.send_message(
                to=to_handle,
                conversation_id=conv_id,
                text=text,
                type="text",
                client_msg_id=client_msg_id,
            )
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"message": result})

    return _handler


def _build_get_conversation_messages(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            conv_id = optional_str(args, "conversation_id", max_len=64)
            peer_handle_raw = args.get("peer_handle")
            limit = optional_int(args, "limit", minimum=1, maximum=200) or 50
            before_seq = optional_int(args, "before_seq", minimum=1)

            if not conv_id and peer_handle_raw is None:
                raise ToolArgError(
                    "Either `conversation_id` or `peer_handle` is required"
                )
            if conv_id and peer_handle_raw is not None:
                raise ToolArgError(
                    "Provide either `conversation_id` or `peer_handle`, not both"
                )
        except ToolArgError as exc:
            return handle_arg_error(exc)

        # peer_handle → direct conversation_id by sending an empty list_conversations
        # filter; simpler is to call get_messages with a per-handle helper if the SDK
        # exposes one. The platform's GET /v1/messages/:conversation_id requires the
        # conv id; resolving from handle would cost a round-trip. For now we ask the
        # caller to use conversation_id (which they have from any prior message
        # event) and we let peer_handle fall through to NOT_FOUND if the SDK
        # doesn't accept it.
        target_conv = conv_id
        if target_conv is None:
            assert peer_handle_raw is not None  # narrowed above
            try:
                peer_handle = normalize_handle(peer_handle_raw, field="peer_handle")
            except ToolArgError as exc:
                return handle_arg_error(exc)
            # Resolve direct conversation id via list_conversations — single
            # round-trip, no leaking dependency on internal id format.
            try:
                conversations = runtime.client.list_conversations()
            except AgentChatError as exc:
                return format_sdk_error(exc)
            for conv in conversations:
                if conv.get("kind") != "direct":
                    continue
                peer = conv.get("peer") or {}
                if isinstance(peer, dict) and peer.get("handle", "").lower() == peer_handle:
                    target_conv = conv.get("id")
                    break
            if not target_conv:
                return err(
                    "NOT_FOUND",
                    f"No prior direct conversation found with @{peer_handle}. "
                    "Send a message first to establish the conversation.",
                )

        try:
            kwargs: dict[str, Any] = {"limit": limit}
            if before_seq is not None:
                kwargs["before_seq"] = before_seq
            result = runtime.client.get_messages(target_conv, **kwargs)
        except AgentChatError as exc:
            return format_sdk_error(exc)

        return ok({"conversation_id": target_conv, "messages": result})

    return _handler


def _build_mark_message_read(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            message_id = require_str(args, "message_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            result = runtime.client.mark_as_read(message_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"message": result})

    return _handler


def _build_hide_message(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            message_id = require_str(args, "message_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            runtime.client.delete_message(message_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"hidden_message_id": message_id})

    return _handler


TOOLS = (
    ("agentchat_send_message", SEND_MESSAGE_SCHEMA, _build_send_message, "✉"),
    (
        "agentchat_get_conversation_messages",
        GET_CONVERSATION_MESSAGES_SCHEMA,
        _build_get_conversation_messages,
        "📜",
    ),
    (
        "agentchat_mark_message_read",
        MARK_MESSAGE_READ_SCHEMA,
        _build_mark_message_read,
        "👁",
    ),
    ("agentchat_hide_message", HIDE_MESSAGE_SCHEMA, _build_hide_message, "🙈"),
)
