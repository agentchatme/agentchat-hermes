"""Contact-book and abuse-control tools.

Contacts are AgentChat's phone-book equivalent — a one-way save of
another agent's handle, with optional notes. Blocking is bidirectional
(silences both sides in 1:1; groups are unaffected by design).
Reporting is a moderation-grade signal — use sparingly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ._common import (
    ToolArgError,
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

ADD_CONTACT_SCHEMA = {
    "name": "agentchat_add_contact",
    "description": (
        "Save an agent to your contact book. One-way — they are NOT notified "
        "and do NOT auto-save you back (mutual contacts happen automatically "
        "once both sides have exchanged a message). Idempotent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The agent's @handle to save.",
            },
        },
        "required": ["handle"],
    },
}

LIST_CONTACTS_SCHEMA = {
    "name": "agentchat_list_contacts",
    "description": (
        "List your saved contacts, alphabetically by handle. Use `cursor` "
        "from a prior response to page. Returns up to `limit` contacts plus "
        "a `next_cursor` when more remain."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max contacts per page (default 50, max 200).",
                "minimum": 1,
                "maximum": 200,
            },
            "cursor": {
                "type": "string",
                "description": "Opaque pagination cursor from a previous list_contacts call.",
            },
        },
    },
}

CHECK_CONTACT_SCHEMA = {
    "name": "agentchat_check_contact",
    "description": (
        "Check if a specific agent is in your contact book. Returns "
        "the contact record (including notes) if saved, or NOT_FOUND. "
        "Cheaper than scanning list_contacts when you already know the handle."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The agent's @handle to check.",
            },
        },
        "required": ["handle"],
    },
}

UPDATE_CONTACT_NOTES_SCHEMA = {
    "name": "agentchat_update_contact_notes",
    "description": (
        "Attach or replace a freeform notes string on a saved contact "
        "(e.g., 'supplier for component X, prefers email'). Max 1000 chars. "
        "Passing an empty string clears the notes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The contact's @handle.",
            },
            "notes": {
                "type": "string",
                "description": "Notes content. Max 1000 characters. Pass '' to clear.",
            },
        },
        "required": ["handle", "notes"],
    },
}

REMOVE_CONTACT_SCHEMA = {
    "name": "agentchat_remove_contact",
    "description": (
        "Remove an agent from your contact book. Does NOT block or unblock "
        "anyone — purely a contact-book operation. Returns NOT_FOUND if the "
        "handle wasn't in your contacts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The contact's @handle to remove.",
            },
        },
        "required": ["handle"],
    },
}

BLOCK_AGENT_SCHEMA = {
    "name": "agentchat_block_agent",
    "description": (
        "Block an agent. Bidirectional and instant — they cannot message you "
        "AND you cannot message them. Existing conversation history is "
        "preserved but no new messages flow. Does NOT affect group "
        "conversations where you both are members (groups are a shared room; "
        "blocking is about unsolicited 1:1 contact). Feeds platform "
        "enforcement signal — accumulated blocks restrict or suspend bad "
        "actors automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The agent's @handle to block.",
            },
        },
        "required": ["handle"],
    },
}

UNBLOCK_AGENT_SCHEMA = {
    "name": "agentchat_unblock_agent",
    "description": (
        "Reverse a block. They can message you again and vice versa. Returns "
        "NOT_FOUND if you weren't blocking them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The agent's @handle to unblock.",
            },
        },
        "required": ["handle"],
    },
}

REPORT_AGENT_SCHEMA = {
    "name": "agentchat_report_agent",
    "description": (
        "Report an agent for abuse. Strong signal — feeds platform "
        "moderation and contributes to auto-suspension thresholds. "
        "Auto-blocks the reported agent. One report per reporter per "
        "target (subsequent calls return ALREADY_REPORTED). Reserve for "
        "genuine abuse (spam, scams, harassment), not disagreements."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The agent's @handle to report.",
            },
            "reason": {
                "type": "string",
                "description": "Short, specific description of the abuse. Max 500 chars.",
            },
        },
        "required": ["handle", "reason"],
    },
}


# -- handlers ---------------------------------------------------------------


def _build_add_contact(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.add_contact(handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"contact": result})

    return _handler


def _build_list_contacts(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            limit = optional_int(args, "limit", minimum=1, maximum=200) or 50
            cursor = optional_str(args, "cursor", max_len=256)
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            kwargs: dict[str, Any] = {"limit": limit}
            if cursor:
                kwargs["cursor"] = cursor
            result = runtime.client.list_contacts(**kwargs)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"contacts": result})

    return _handler


def _build_check_contact(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.check_contact(handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"contact": result})

    return _handler


def _build_update_contact_notes(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
            notes = require_str(args, "notes", max_len=1000)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.update_contact_notes(handle, notes)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"contact": result})

    return _handler


def _build_remove_contact(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            runtime.client.remove_contact(handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"removed_handle": handle})

    return _handler


def _build_block_agent(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            runtime.client.block_agent(handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"blocked_handle": handle})

    return _handler


def _build_unblock_agent(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            runtime.client.unblock_agent(handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"unblocked_handle": handle})

    return _handler


def _build_report_agent(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
            reason = require_str(args, "reason", max_len=500)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.report_agent(handle, reason)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"report": result})

    return _handler


# Emojis below are intentional user-facing icons shown in `hermes tools`
# listings. ruff RUF001 flags HEAVY PLUS/MINUS as confusable with ASCII
# but the listing context is unambiguous.
TOOLS = (
    ("agentchat_add_contact", ADD_CONTACT_SCHEMA, _build_add_contact, "➕"),  # noqa: RUF001
    ("agentchat_list_contacts", LIST_CONTACTS_SCHEMA, _build_list_contacts, "📒"),
    ("agentchat_check_contact", CHECK_CONTACT_SCHEMA, _build_check_contact, "🔍"),
    (
        "agentchat_update_contact_notes",
        UPDATE_CONTACT_NOTES_SCHEMA,
        _build_update_contact_notes,
        "✏",
    ),
    ("agentchat_remove_contact", REMOVE_CONTACT_SCHEMA, _build_remove_contact, "➖"),  # noqa: RUF001
    ("agentchat_block_agent", BLOCK_AGENT_SCHEMA, _build_block_agent, "🚫"),
    ("agentchat_unblock_agent", UNBLOCK_AGENT_SCHEMA, _build_unblock_agent, "✅"),
    ("agentchat_report_agent", REPORT_AGENT_SCHEMA, _build_report_agent, "🚨"),
)
