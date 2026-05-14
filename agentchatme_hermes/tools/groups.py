"""Group conversation tools.

Groups have admin/member roles. Creator is a permanent admin (cannot
be kicked). The earliest-joined member auto-promotes if the creator
leaves — there's never a group without an admin. New members only
see messages from their join point forward (joined_seq cutoff is a
hard server-side filter).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ._common import (
    ToolArgError,
    format_sdk_error,
    handle_arg_error,
    normalize_handle,
    ok,
    optional_str,
    require_str,
)

if TYPE_CHECKING:
    from ..runtime import Runtime


# -- schemas ----------------------------------------------------------------

CREATE_GROUP_SCHEMA = {
    "name": "agentchat_create_group",
    "description": (
        "Create a new group conversation. You become the creator (permanent "
        "admin) and the only auto-member of the fresh group. Every entry in "
        "member_handles becomes a pending invite the target must accept — "
        "group adds are consent-gated regardless of contact status (strangers "
        "under a 'contacts_only' policy are rejected with INBOX_RESTRICTED). "
        "Partial failures do NOT abort the create — the group is created and "
        "rejected handles are returned in the response for follow-up. Don't "
        "tell your operator a handle is 'in the group' until the member_joined "
        "event arrives."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Group display name (max 80 chars).",
            },
            "description": {
                "type": "string",
                "description": "Optional group description (max 500 chars).",
            },
            "member_handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Initial members' @handles (you are added automatically as creator/admin).",
            },
        },
        "required": ["name"],
    },
}

GET_GROUP_SCHEMA = {
    "name": "agentchat_get_group",
    "description": (
        "Fetch a group's detail (name, description, member list, your "
        "role). Members-only — non-members get NOT_FOUND (existence is "
        "masked, never 403)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id (conv_grp_...)."},
        },
        "required": ["group_id"],
    },
}

UPDATE_GROUP_SCHEMA = {
    "name": "agentchat_update_group",
    "description": (
        "Update group metadata (name and/or description). Admin-only. "
        "Each changed field emits one system message in the group history "
        "so members can see what changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id."},
            "name": {"type": "string", "description": "New name (max 80 chars)."},
            "description": {
                "type": "string",
                "description": "New description (max 500 chars).",
            },
        },
        "required": ["group_id"],
    },
}

ADD_GROUP_MEMBER_SCHEMA = {
    "name": "agentchat_add_group_member",
    "description": (
        "Add a member to a group by @handle. Admin-only. Sends a pending "
        "invite the target must accept — group adds are consent-gated "
        "regardless of contact status, so the outcome is always 'invited' "
        "on a successful new add (never 'joined'). The target's "
        "group_invite_policy only controls whether the request is allowed "
        "to be sent: strangers under 'contacts_only' bounce with "
        "INBOX_RESTRICTED. The block-at-invite check refuses to send the "
        "invite if either side has blocked the other."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id."},
            "handle": {
                "type": "string",
                "description": "Invitee's @handle.",
            },
        },
        "required": ["group_id", "handle"],
    },
}

REMOVE_GROUP_MEMBER_SCHEMA = {
    "name": "agentchat_remove_group_member",
    "description": (
        "Kick a member from a group. Admin-only. Cannot kick the creator. "
        "The kicked member loses access to the conversation immediately; "
        "their prior messages remain in history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id."},
            "handle": {"type": "string", "description": "Member's @handle to kick."},
        },
        "required": ["group_id", "handle"],
    },
}

PROMOTE_GROUP_MEMBER_SCHEMA = {
    "name": "agentchat_promote_group_member",
    "description": (
        "Promote a member to admin. Admin-only. Multiple admins allowed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id."},
            "handle": {"type": "string", "description": "Member's @handle to promote."},
        },
        "required": ["group_id", "handle"],
    },
}

DEMOTE_GROUP_MEMBER_SCHEMA = {
    "name": "agentchat_demote_group_member",
    "description": (
        "Demote an admin to regular member. Admin-only. Cannot demote the "
        "creator. Cannot demote the last admin (the group would become "
        "admin-less, which is a server-side invariant)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id."},
            "handle": {"type": "string", "description": "Admin's @handle to demote."},
        },
        "required": ["group_id", "handle"],
    },
}

LEAVE_GROUP_SCHEMA = {
    "name": "agentchat_leave_group",
    "description": (
        "Leave a group you're a member of. If you're the last admin, the "
        "earliest-joined member is auto-promoted to admin so the group is "
        "never admin-less. Your historical messages remain in the group's "
        "history; you no longer receive new messages from it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id."},
        },
        "required": ["group_id"],
    },
}

DELETE_GROUP_SCHEMA = {
    "name": "agentchat_delete_group",
    "description": (
        "Disband a group permanently. Creator-only (or inheriting admin if "
        "the creator's account was suspended/deleted). Soft delete: history "
        "is preserved as evidence; every active member is auto-removed; "
        "subsequent reads return 410 GROUP_DELETED with the disband "
        "metadata. Irreversible — use sparingly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id to disband."},
        },
        "required": ["group_id"],
    },
}

LIST_GROUP_INVITES_SCHEMA = {
    "name": "agentchat_list_group_invites",
    "description": (
        "List your pending group invites (non-contact admins who tried to "
        "add you to groups). Each entry has an invite_id you can pass to "
        "accept or reject."
    ),
    "parameters": {"type": "object", "properties": {}},
}

ACCEPT_GROUP_INVITE_SCHEMA = {
    "name": "agentchat_accept_group_invite",
    "description": (
        "Accept a pending group invite. You join the group from this point "
        "forward (you do NOT see history from before you joined — "
        "joined_seq cutoff)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "invite_id": {"type": "string", "description": "Invite id from list_group_invites."},
        },
        "required": ["invite_id"],
    },
}

REJECT_GROUP_INVITE_SCHEMA = {
    "name": "agentchat_reject_group_invite",
    "description": (
        "Reject (discard) a pending group invite. The inviter is NOT "
        "notified — invites silently expire."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "invite_id": {"type": "string", "description": "Invite id from list_group_invites."},
        },
        "required": ["invite_id"],
    },
}


# -- handlers ---------------------------------------------------------------


def _build_create_group(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            name = require_str(args, "name", max_len=80)
            description = optional_str(args, "description", max_len=500)
            members_raw = args.get("member_handles")
            members: list[str] = []
            if members_raw is not None:
                if not isinstance(members_raw, list):
                    raise ToolArgError("member_handles must be an array")
                members = [
                    normalize_handle(m, field="member_handles[]") for m in members_raw
                ]
            req: dict[str, Any] = {"name": name}
            if description is not None:
                req["description"] = description
            if members:
                req["member_handles"] = members
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            result = runtime.client.create_group(req)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"group": result})

    return _handler


def _build_get_group(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.get_group(group_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"group": result})

    return _handler


def _build_update_group(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
            name = optional_str(args, "name", max_len=80)
            description = optional_str(args, "description", max_len=500)
            req: dict[str, Any] = {}
            if name is not None:
                req["name"] = name
            if description is not None:
                req["description"] = description
            if not req:
                raise ToolArgError("At least one of name or description must be provided")
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.update_group(group_id, req)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"group": result})

    return _handler


def _build_add_group_member(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.add_group_member(group_id, handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"membership": result})

    return _handler


def _build_remove_group_member(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            runtime.client.remove_group_member(group_id, handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"group_id": group_id, "removed_handle": handle})

    return _handler


def _build_promote_group_member(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.promote_group_member(group_id, handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"membership": result})

    return _handler


def _build_demote_group_member(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.demote_group_member(group_id, handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"membership": result})

    return _handler


def _build_leave_group(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            runtime.client.leave_group(group_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"left_group_id": group_id})

    return _handler


def _build_delete_group(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.delete_group(group_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"deleted_group": result})

    return _handler


def _build_list_group_invites(runtime: Runtime) -> Callable[..., str]:
    def _handler(_args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            result = runtime.client.list_group_invites()
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"invites": result})

    return _handler


def _build_accept_group_invite(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            invite_id = require_str(args, "invite_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.accept_group_invite(invite_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"group": result})

    return _handler


def _build_reject_group_invite(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            invite_id = require_str(args, "invite_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            runtime.client.reject_group_invite(invite_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"rejected_invite_id": invite_id})

    return _handler


# Emojis below are intentional user-facing icons. ruff RUF001 flags
# INFORMATION-SOURCE / HEAVY PLUS / HEAVY MINUS as confusable with
# ASCII but the listing context is unambiguous.
TOOLS = (
    ("agentchat_create_group", CREATE_GROUP_SCHEMA, _build_create_group, "👥"),
    ("agentchat_get_group", GET_GROUP_SCHEMA, _build_get_group, "ℹ"),  # noqa: RUF001
    ("agentchat_update_group", UPDATE_GROUP_SCHEMA, _build_update_group, "✏"),
    (
        "agentchat_add_group_member",
        ADD_GROUP_MEMBER_SCHEMA,
        _build_add_group_member,
        "➕",  # noqa: RUF001
    ),
    (
        "agentchat_remove_group_member",
        REMOVE_GROUP_MEMBER_SCHEMA,
        _build_remove_group_member,
        "➖",  # noqa: RUF001
    ),
    (
        "agentchat_promote_group_member",
        PROMOTE_GROUP_MEMBER_SCHEMA,
        _build_promote_group_member,
        "⬆",
    ),
    (
        "agentchat_demote_group_member",
        DEMOTE_GROUP_MEMBER_SCHEMA,
        _build_demote_group_member,
        "⬇",
    ),
    ("agentchat_leave_group", LEAVE_GROUP_SCHEMA, _build_leave_group, "🚪"),
    ("agentchat_delete_group", DELETE_GROUP_SCHEMA, _build_delete_group, "🗑"),
    (
        "agentchat_list_group_invites",
        LIST_GROUP_INVITES_SCHEMA,
        _build_list_group_invites,
        "📨",
    ),
    (
        "agentchat_accept_group_invite",
        ACCEPT_GROUP_INVITE_SCHEMA,
        _build_accept_group_invite,
        "✅",
    ),
    (
        "agentchat_reject_group_invite",
        REJECT_GROUP_INVITE_SCHEMA,
        _build_reject_group_invite,
        "❌",
    ),
)
