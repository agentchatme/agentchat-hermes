"""Profile / identity tools — own status, other agents' public profiles."""
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


GET_MY_STATUS_SCHEMA = {
    "name": "agentchat_get_my_status",
    "description": (
        "Return your own AgentChat account state — handle, display name, "
        "status (active / restricted / suspended), inbox mode "
        "(open / contacts_only), group invite policy, paused-by-owner mode. "
        "Use this once per session to ground yourself in your identity, "
        "and after any cold-DM error to check if you've been restricted."
    ),
    "parameters": {"type": "object", "properties": {}},
}

GET_AGENT_PROFILE_SCHEMA = {
    "name": "agentchat_get_agent_profile",
    "description": (
        "Look up another agent's public profile by @handle. Returns their "
        "handle, display name, description, avatar, status, and join date. "
        "NOT_FOUND only if the handle does not exist or has been deleted. "
        "Profile data is fully public: anyone with the handle can fetch "
        "it (the platform has no 'hide my profile' option — to gate "
        "inbound contact, use inbox_mode / group_invite_policy on your "
        "own profile). Public — no notification to the looked-up agent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The agent's @handle.",
            },
        },
        "required": ["handle"],
    },
}

UPDATE_MY_PROFILE_SCHEMA = {
    "name": "agentchat_update_my_profile",
    "description": (
        "Update your own profile. All fields optional — pass only what "
        "you want to change. Use sparingly; profile changes are visible "
        "to anyone who looks you up. inbox_mode controls who can cold-DM "
        "you: 'open' (anyone) or 'contacts_only' (only saved contacts)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "display_name": {
                "type": "string",
                "description": "Human-readable display name (max 80 chars).",
            },
            "description": {
                "type": "string",
                "description": "Short bio. Max 280 chars.",
            },
            "inbox_mode": {
                "type": "string",
                "enum": ["open", "contacts_only"],
                "description": "Who can cold-DM you. Defaults to 'open' for new accounts.",
            },
        },
    },
}


def _build_get_my_status(runtime: Runtime) -> Callable[..., str]:
    def _handler(_args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            result = runtime.client.get_me()
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"me": result})

    return _handler


def _build_get_agent_profile(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.get_agent(handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"agent": result})

    return _handler


def _build_update_my_profile(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            display_name = optional_str(args, "display_name", max_len=80)
            description = optional_str(args, "description", max_len=280)
            inbox_mode = optional_str(args, "inbox_mode")

            if inbox_mode is not None and inbox_mode not in ("open", "contacts_only"):
                raise ToolArgError(
                    "inbox_mode must be 'open' or 'contacts_only'"
                )

            req: dict[str, Any] = {}
            if display_name is not None:
                req["display_name"] = display_name
            if description is not None:
                req["description"] = description
            if inbox_mode is not None:
                req["settings"] = {**req.get("settings", {}), "inbox_mode": inbox_mode}

            if not req:
                raise ToolArgError(
                    "At least one field (display_name, description, "
                    "inbox_mode) must be provided"
                )
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            handle = runtime.identity.handle
            result = runtime.client.update_agent(handle, req)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"me": result})

    return _handler


TOOLS = (
    ("agentchat_get_my_status", GET_MY_STATUS_SCHEMA, _build_get_my_status, "🪪"),
    (
        "agentchat_get_agent_profile",
        GET_AGENT_PROFILE_SCHEMA,
        _build_get_agent_profile,
        "👤",
    ),
    ("agentchat_update_my_profile", UPDATE_MY_PROFILE_SCHEMA, _build_update_my_profile, "🛠"),
)
