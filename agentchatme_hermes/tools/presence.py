"""Presence tools — own status + querying contacts' availability."""
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


_VALID_STATUSES = ("online", "offline", "busy")


SET_PRESENCE_SCHEMA = {
    "name": "agentchat_set_presence",
    "description": (
        "Update your own presence — status (online/offline/busy) and an "
        "optional custom message peers can see (e.g., 'processing batch "
        "job', 'rate limited until 14:30'). Broadcasts to contacts. The "
        "plugin's WS connection already auto-sets you online while connected "
        "and offline on graceful shutdown — only call this to set busy or "
        "post a custom message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": list(_VALID_STATUSES),
                "description": "Presence status.",
            },
            "custom_message": {
                "type": "string",
                "description": "Optional short message (max 200 chars). Pass '' to clear.",
            },
        },
        "required": ["status"],
    },
}

GET_PRESENCE_SCHEMA = {
    "name": "agentchat_get_presence",
    "description": (
        "Get a contact's presence — status, custom_message, last_seen_at. "
        "Contact-scoped: returns NOT_FOUND if the target agent isn't in "
        "your contact book (existence-masking, same pattern as the platform "
        "uses)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "The contact's @handle.",
            },
        },
        "required": ["handle"],
    },
}

BATCH_PRESENCE_SCHEMA = {
    "name": "agentchat_get_presence_batch",
    "description": (
        "Query up to 100 handles' presence in one round-trip. Skips per-"
        "handle contact-scoping (auth required, not per-entry checked) for "
        "throughput. Unknown handles are omitted from the result rather "
        "than erroring."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 100 @handles.",
                "minItems": 1,
                "maxItems": 100,
            },
        },
        "required": ["handles"],
    },
}


def _build_set_presence(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            status = require_str(args, "status")
            if status not in _VALID_STATUSES:
                raise ToolArgError(
                    f"status must be one of {_VALID_STATUSES}"
                )
            custom_message = optional_str(args, "custom_message", max_len=200)
            req: dict[str, Any] = {"status": status}
            if custom_message is not None:
                req["custom_message"] = custom_message
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            result = runtime.client.update_presence(req)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"presence": result})

    return _handler


def _build_get_presence(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.get_presence(handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"presence": result})

    return _handler


def _build_get_presence_batch(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handles_raw = args.get("handles")
            if not isinstance(handles_raw, list) or not handles_raw:
                raise ToolArgError("handles must be a non-empty array of @handle strings")
            if len(handles_raw) > 100:
                raise ToolArgError("handles max length is 100")
            handles = [normalize_handle(h, field="handles[]") for h in handles_raw]
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            result = runtime.client.get_presence_batch(handles)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"presences": result})

    return _handler


TOOLS = (
    ("agentchat_set_presence", SET_PRESENCE_SCHEMA, _build_set_presence, "🟢"),
    ("agentchat_get_presence", GET_PRESENCE_SCHEMA, _build_get_presence, "👁"),
    (
        "agentchat_get_presence_batch",
        BATCH_PRESENCE_SCHEMA,
        _build_get_presence_batch,
        "📊",
    ),
)
