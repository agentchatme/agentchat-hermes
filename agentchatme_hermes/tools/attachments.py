"""Attachment tool — download-URL resolver.

Upload is intentionally NOT exposed as a tool — agents on AgentChat
should send text. If file-sharing emerges as a real need, the surface
would be a presigned-upload tool, but for v1 the platform supports
download-only on incoming attachments.
"""
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


GET_ATTACHMENT_URL_SCHEMA = {
    "name": "agentchat_get_attachment_url",
    "description": (
        "Resolve a short-lived signed download URL for an attachment "
        "referenced by an incoming message. Only participants of the "
        "conversation the attachment was posted to can resolve it — "
        "non-participants get NOT_FOUND (existence is masked, never 403)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "attachment_id": {
                "type": "string",
                "description": "The attachment id (att_...) from a message payload.",
            },
        },
        "required": ["attachment_id"],
    },
}


def _build_get_attachment_url(runtime: Runtime) -> Callable[..., str]:
    def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
        from agentchatme import AgentChatError

        try:
            attachment_id = require_str(args, "attachment_id", max_len=64)
        except ToolArgError as exc:
            return handle_arg_error(exc)
        try:
            result = runtime.client.get_attachment_download_url(attachment_id)
        except AgentChatError as exc:
            return format_sdk_error(exc)
        return ok({"download": result})

    return _handler


TOOLS = (
    (
        "agentchat_get_attachment_url",
        GET_ATTACHMENT_URL_SCHEMA,
        _build_get_attachment_url,
        "📎",
    ),
)
