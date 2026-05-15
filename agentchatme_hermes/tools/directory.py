"""Directory tool — handle-prefix search.

AgentChat's directory is *handle-only* by design (phone-book
semantics — exact handle lookup, no name/description ranking).
Display name and description are returned in results but NOT
searched against. Discovery happens out-of-band (e.g., MoltBook
profiles).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ._common import (
    ToolArgError,
    format_sdk_error,
    handle_arg_error,
    ok,
    optional_int,
    require_str,
)

if TYPE_CHECKING:
    from ..runtime import Runtime


SEARCH_DIRECTORY_SCHEMA = {
    "name": "agentchat_search_directory",
    "description": (
        "Search the AgentChat directory by handle prefix. Returns agents "
        "whose @handle starts with `q`. Display name and description are "
        "returned but NOT matched against — this is phone-book semantics, "
        "not full-text search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": "Handle prefix to search for (e.g. 'ali' matches 'alice', 'alicat').",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 20, max 50).",
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": ["q"],
    },
}


def _build_search_directory(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            q = require_str(args, "q", max_len=64).strip()
            if not q:
                raise ToolArgError("q must be a non-empty string")
            limit = optional_int(args, "limit", minimum=1, maximum=50) or 20
        except ToolArgError as exc:
            return handle_arg_error(exc)

        try:
            result = runtime.client.search_agents(q, limit=limit)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"results": result})

    return _handler


TOOLS = (
    (
        "agentchat_search_directory",
        SEARCH_DIRECTORY_SCHEMA,
        _build_search_directory,
        "🔎",
    ),
)
