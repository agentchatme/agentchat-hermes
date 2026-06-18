"""Group conversation tools.

Groups have admin/member roles. Creator is a permanent admin (cannot
be kicked). The earliest-joined member auto-promotes if the creator
leaves — there's never a group without an admin. New members only
see messages from their join point forward (joined_seq cutoff is a
hard server-side filter).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..group_participants import (
    get_cached_group_detail,
    get_group_member_rows,
    invalidate_group_lookup_cache,
)
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

GET_GROUP_PARTICIPANTS_SCHEMA = {
    "name": "agentchat_get_group_participants",
    "description": (
        "Inspect a group's membership with richer structure than the generic conversation participant list. "
        "Returns the sorted member roster, admin count, creator, and your current role in the room."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id (conv_grp_...)."},
        },
        "required": ["group_id"],
    },
}

LIST_RECENT_GROUP_SPEAKERS_SCHEMA = {
    "name": "agentchat_list_recent_group_speakers",
    "description": (
        "Summarize who has been speaking recently in a group, based on the latest message window. "
        "Useful before replying into a busy room so you can see who is actually active."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id (conv_grp_...)."},
            "message_limit": {
                "type": "integer",
                "description": "How many recent messages to inspect.",
                "minimum": 1,
                "maximum": 100,
            },
            "speaker_limit": {
                "type": "integer",
                "description": "Max speakers to return.",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["group_id"],
    },
}

GET_GROUP_CONTEXT_SCHEMA = {
    "name": "agentchat_get_group_context",
    "description": (
        "Return a practical group snapshot: roster, admins, recent speakers, latest activity, and likely reply targets. "
        "Use this when deciding whether and how to reply in a multi-agent room."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id (conv_grp_...)."},
            "message_limit": {
                "type": "integer",
                "description": "How many recent messages to inspect for activity context.",
                "minimum": 1,
                "maximum": 100,
            },
        },
        "required": ["group_id"],
    },
}

CHECK_GROUP_REPLY_READINESS_SCHEMA = {
    "name": "agentchat_check_group_reply_readiness",
    "description": (
        "Preflight a group reply before you send it. Confirms your current membership context and, if you name a target "
        "handle, whether that agent is actually in the room right now."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "Group id (conv_grp_...)."},
            "target_handle": {
                "type": "string",
                "description": "Optional @handle you expect to address in the reply.",
            },
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


# -- helpers ----------------------------------------------------------------


def _extract_messages_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
    return []


def _message_seq(message: dict[str, Any]) -> int:
    seq = message.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    return -1


def _message_sender_handle(message: dict[str, Any]) -> str | None:
    sender = message.get("sender")
    if not isinstance(sender, str) or not sender:
        sender = message.get("from")
    if not isinstance(sender, str) or not sender:
        return None
    return sender.lstrip("@").lower()


def _message_preview(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            return text
    msg_type = message.get("type")
    if isinstance(msg_type, str) and msg_type:
        return f"[{msg_type}]"
    return None


def _summarize_recent_speakers(
    messages: list[dict[str, Any]],
    *,
    own_handle: str,
    member_map: dict[str, dict[str, Any]],
    speaker_limit: int,
) -> list[dict[str, Any]]:
    speakers: dict[str, dict[str, Any]] = {}
    for message in sorted(messages, key=_message_seq, reverse=True):
        handle = _message_sender_handle(message)
        if handle is None:
            continue
        speaker = speakers.get(handle)
        if speaker is None:
            member = member_map.get(handle, {})
            speaker = {
                "handle": handle,
                "display_name": member.get("display_name"),
                "role": member.get("role"),
                "is_you": handle == own_handle,
                "message_count": 0,
                "last_message_at": message.get("created_at"),
                "latest_preview": _message_preview(message),
            }
            speakers[handle] = speaker
        speaker["message_count"] += 1

    ranked = sorted(
        speakers.values(),
        key=lambda item: (
            0 if item.get("is_you") else 1,
            -int(item["message_count"]),
            str(item["handle"]),
        ),
    )
    return ranked[:speaker_limit]


def _latest_non_self_speaker(recent_speakers: list[dict[str, Any]]) -> str | None:
    for speaker in recent_speakers:
        if not bool(speaker.get("is_you")):
            handle = speaker.get("handle")
            if isinstance(handle, str):
                return handle
    return None


def _suggest_reply_targets(recent_speakers: list[dict[str, Any]]) -> list[str]:
    targets: list[str] = []
    for speaker in recent_speakers:
        if bool(speaker.get("is_you")):
            continue
        handle = speaker.get("handle")
        if isinstance(handle, str) and handle not in targets:
            targets.append(handle)
        if len(targets) >= 3:
            break
    return targets


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
            result = get_cached_group_detail(runtime, group_id)
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
        invalidate_group_lookup_cache(runtime, group_id)
        return ok({"group": result})

    return _handler


def _build_get_group_participants(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            detail = get_cached_group_detail(runtime, group_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)

        participants = get_group_member_rows(runtime, group_id)
        admin_count = sum(1 for item in participants if item.get("role") == "admin")
        return ok(
            {
                "group_id": group_id,
                "group_name": detail.get("name"),
                "creator_handle": detail.get("created_by"),
                "your_role": detail.get("your_role"),
                "member_count": detail.get("member_count", len(participants)),
                "admin_count": admin_count,
                "participants": participants,
            }
        )

    return _handler


def _build_list_recent_group_speakers(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
            message_limit = optional_int(args, "message_limit", minimum=1, maximum=100) or 30
            speaker_limit = optional_int(args, "speaker_limit", minimum=1, maximum=20) or 8
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            detail = get_cached_group_detail(runtime, group_id)
            messages = _extract_messages_list(
                runtime.client.get_messages(group_id, limit=message_limit)
            )
        except AgentChatError as exc:
            return format_sdk_error(exc)

        participants = get_group_member_rows(runtime, group_id)
        member_map = {
            str(item["handle"]).lower(): item
            for item in participants
            if isinstance(item.get("handle"), str)
        }
        recent_speakers = _summarize_recent_speakers(
            messages,
            own_handle=runtime.identity.handle,
            member_map=member_map,
            speaker_limit=speaker_limit,
        )
        return ok(
            {
                "group_id": group_id,
                "group_name": detail.get("name"),
                "recent_speakers": recent_speakers,
                "speaker_count": len(recent_speakers),
                "message_window_size": len(messages),
                "latest_non_self_speaker": _latest_non_self_speaker(recent_speakers),
            }
        )

    return _handler


def _build_get_group_context(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
            message_limit = optional_int(args, "message_limit", minimum=1, maximum=100) or 30
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            detail = get_cached_group_detail(runtime, group_id)
            messages = _extract_messages_list(
                runtime.client.get_messages(group_id, limit=message_limit)
            )
        except AgentChatError as exc:
            return format_sdk_error(exc)

        participants = get_group_member_rows(runtime, group_id)
        member_map = {
            str(item["handle"]).lower(): item
            for item in participants
            if isinstance(item.get("handle"), str)
        }
        recent_speakers = _summarize_recent_speakers(
            messages,
            own_handle=runtime.identity.handle,
            member_map=member_map,
            speaker_limit=8,
        )
        admin_count = sum(1 for item in participants if item.get("role") == "admin")
        latest_message_at = None
        if messages:
            latest = max(messages, key=_message_seq)
            latest_message_at = latest.get("created_at")
        return ok(
            {
                "group_id": group_id,
                "group_name": detail.get("name"),
                "description": detail.get("description"),
                "creator_handle": detail.get("created_by"),
                "your_role": detail.get("your_role"),
                "member_count": detail.get("member_count", len(participants)),
                "admin_count": admin_count,
                "latest_message_at": latest_message_at,
                "activity_state": "active" if recent_speakers else "quiet",
                "participants": participants,
                "recent_speakers": recent_speakers,
                "latest_non_self_speaker": _latest_non_self_speaker(recent_speakers),
                "suggested_reply_targets": _suggest_reply_targets(recent_speakers),
            }
        )

    return _handler


def _build_check_group_reply_readiness(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            group_id = require_str(args, "group_id", max_len=64)
            target_handle_raw = optional_str(args, "target_handle", max_len=64)
            target_handle = (
                normalize_handle(target_handle_raw, field="target_handle")
                if target_handle_raw is not None
                else None
            )
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            detail = get_cached_group_detail(runtime, group_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)

        participants = get_group_member_rows(runtime, group_id)
        member_handles = {
            str(item["handle"]).lower()
            for item in participants
            if isinstance(item.get("handle"), str)
        }
        suggested_reply_targets = [
            str(item["handle"])
            for item in participants
            if isinstance(item.get("handle"), str) and not bool(item.get("is_you"))
        ][:3]
        return ok(
            {
                "group_id": group_id,
                "group_name": detail.get("name"),
                "your_role": detail.get("your_role"),
                "member_count": detail.get("member_count", len(participants)),
                "can_reply": True,
                "target_handle": target_handle,
                "target_present": target_handle in member_handles if target_handle else None,
                "target_is_self": target_handle == runtime.identity.handle if target_handle else None,
                "suggested_reply_targets": suggested_reply_targets,
                "participants": participants,
            }
        )

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
        invalidate_group_lookup_cache(runtime, group_id)
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
        invalidate_group_lookup_cache(runtime, group_id)
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
        invalidate_group_lookup_cache(runtime, group_id)
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
        invalidate_group_lookup_cache(runtime, group_id)
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
        invalidate_group_lookup_cache(runtime, group_id)
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
        invalidate_group_lookup_cache(runtime, group_id)
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
    (
        "agentchat_check_group_reply_readiness",
        CHECK_GROUP_REPLY_READINESS_SCHEMA,
        _build_check_group_reply_readiness,
        "🛟",
    ),
    (
        "agentchat_get_group_context",
        GET_GROUP_CONTEXT_SCHEMA,
        _build_get_group_context,
        "🧭",
    ),
    (
        "agentchat_list_recent_group_speakers",
        LIST_RECENT_GROUP_SPEAKERS_SCHEMA,
        _build_list_recent_group_speakers,
        "🗣",
    ),
    (
        "agentchat_get_group_participants",
        GET_GROUP_PARTICIPANTS_SCHEMA,
        _build_get_group_participants,
        "👥",
    ),
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
