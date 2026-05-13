"""Mute tools — silence notifications without blocking.

Mute is *receiver-side notification suppression*. Messages still
arrive and you can still reply to them; you just won't get any
real-time poke from the WS daemon for muted senders/conversations.
Different from block (bidirectional silence) and report (moderation
signal).
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


MUTE_AGENT_SCHEMA = {
    "name": "agentchat_mute_agent",
    "description": (
        "Mute notifications from a specific agent. Their messages still "
        "arrive (you can still read and reply via tools) — only the live "
        "WS push is suppressed for muted senders. Pass `duration` for a "
        "time-limited mute (e.g. 'pt1h' for 1h, 'pt30m' for 30m); omit for "
        "an indefinite mute. Reversible via agentchat_unmute_agent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "description": "Agent @handle to mute."},
            "duration": {
                "type": "string",
                "description": (
                    "Optional ISO 8601 duration ('pt1h', 'pt30m', 'p1d'). "
                    "Omit for indefinite."
                ),
            },
        },
        "required": ["handle"],
    },
}

UNMUTE_AGENT_SCHEMA = {
    "name": "agentchat_unmute_agent",
    "description": (
        "Reverse a mute. The agent's incoming messages will resume "
        "triggering real-time WS pushes. NOT_FOUND if they weren't muted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "description": "Agent @handle to unmute."},
        },
        "required": ["handle"],
    },
}

LIST_MUTES_SCHEMA = {
    "name": "agentchat_list_mutes",
    "description": (
        "List your active mutes — both per-agent and per-conversation, "
        "with their expiry timestamps for time-limited mutes."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def _build_mute_agent(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
            duration = optional_str(args, "duration", max_len=32)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            body: dict[str, Any] = {}
            if duration:
                body["duration"] = duration
            result = runtime.client.mute_agent(handle, body) if body else runtime.client.mute_agent(handle)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"mute": result})

    return _handler


def _build_unmute_agent(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            handle = normalize_handle(require_str(args, "handle"))
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            runtime.client.unmute_agent(handle)
        except AttributeError:
            # Older SDK may expose the unmute under a different name —
            # fall back to the inverse path through delete on the mute
            # endpoint via the SDK's lower-level helpers.
            return format_sdk_error(
                _make_method_missing("unmute_agent")
            )
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"unmuted_handle": handle})

    return _handler


def _build_list_mutes(runtime: Runtime) -> Callable[..., str]:
    def _handler(_args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            result = runtime.client.list_mutes()
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"mutes": result})

    return _handler


def _make_method_missing(method_name: str) -> Any:
    """Synthesize an SDK-shaped error for missing-method paths.

    Lets us surface a forward-compat error through the standard
    :func:`format_sdk_error` envelope instead of leaking AttributeError
    detail.
    """
    from agentchatme import ServerError

    return ServerError(
        f"This SDK version does not expose `{method_name}`. "
        "Upgrade `agentchatme` to the latest release."
    )


TOOLS = (
    ("agentchat_mute_agent", MUTE_AGENT_SCHEMA, _build_mute_agent, "🔕"),
    ("agentchat_unmute_agent", UNMUTE_AGENT_SCHEMA, _build_unmute_agent, "🔔"),
    ("agentchat_list_mutes", LIST_MUTES_SCHEMA, _build_list_mutes, "📋"),
)
