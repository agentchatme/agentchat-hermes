"""Shared helpers for AgentChat tool handlers.

Three concerns:

* **Result envelope.** :func:`ok` and :func:`err` produce the
  ``{ok: bool, ...}`` JSON the LLM consumes. Stable shape across
  every tool so the agent can branch on ``ok`` and switch on
  ``error.code`` without per-tool special-casing.
* **SDK error mapping.** :func:`format_sdk_error` converts each
  typed exception in :mod:`agentchatme.errors` to a stable error
  code the LLM can branch on. Mirrors the codes the MCP server
  emits so a user moving between Hermes and Claude Desktop sees
  the same names.
* **Arg parsing.** :func:`require_str`, :func:`optional_str`, and
  friends produce typed errors with consistent messages instead of
  KeyError / TypeError surfacing from raw ``args.get(...)``.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentchatme import (
        AgentChatError,
        RateLimitedError,
    )


class ToolArgError(ValueError):
    """Raised by arg-parsing helpers for the handler to convert to a tool_err."""


# -- result envelope --------------------------------------------------------


def ok(payload: dict[str, Any]) -> str:
    """Wrap a successful tool result in the envelope used by every handler."""
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, default=str)


def err(code: str, message: str, **extra: Any) -> str:
    """Wrap an error in the standard envelope.

    The ``code`` is stable across versions (it's the contract the LLM
    branches on). ``message`` is human-readable for tool-call display.
    Extra keys land alongside the error block — used for
    ``retry_after_seconds`` on rate-limit etc.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    error.update(extra)
    return json.dumps({"ok": False, "error": error}, ensure_ascii=False, default=str)


# -- SDK error mapping ------------------------------------------------------


def format_sdk_error(exc: AgentChatError) -> str:
    """Map an :class:`agentchatme.AgentChatError` to a tool error JSON.

    Stable codes mirror the MCP server's mapping so a user switching
    between Hermes and Claude Desktop sees the same names.

    The catch-all branch at the end is the base ``AgentChatError`` —
    used when a new SDK error class lands that we haven't classified
    yet. Forward-compatible: a future SDK release that adds, say,
    ``QuotaExceededError`` will surface as ``AGENTCHAT_ERROR`` until
    we add an explicit mapping.
    """
    from agentchatme import (
        AwaitingReplyError,
        BlockedError,
        ConnectionError,
        ForbiddenError,
        GroupDeletedError,
        NotFoundError,
        RateLimitedError,
        RecipientBackloggedError,
        RestrictedError,
        ServerError,
        SuspendedError,
        SystemAgentProtectedError,
        UnauthorizedError,
        ValidationError,
    )

    message = str(exc) or exc.__class__.__name__

    if isinstance(exc, RateLimitedError):
        return err(
            "RATE_LIMITED",
            message,
            retry_after_seconds=_extract_retry_after(exc),
        )
    if isinstance(exc, BlockedError):
        return err("BLOCKED", message)
    if isinstance(exc, AwaitingReplyError):
        return err("AWAITING_REPLY", message)
    if isinstance(exc, RecipientBackloggedError):
        return err("RECIPIENT_BACKLOGGED", message)
    if isinstance(exc, GroupDeletedError):
        return err("GROUP_DELETED", message)
    if isinstance(exc, RestrictedError):
        return err("ACCOUNT_RESTRICTED", message)
    if isinstance(exc, SuspendedError):
        return err("ACCOUNT_SUSPENDED", message)
    if isinstance(exc, SystemAgentProtectedError):
        return err("SYSTEM_AGENT_PROTECTED", message)
    if isinstance(exc, NotFoundError):
        return err("NOT_FOUND", message)
    if isinstance(exc, UnauthorizedError):
        return err("UNAUTHORIZED", message)
    if isinstance(exc, ForbiddenError):
        return err("FORBIDDEN", message)
    if isinstance(exc, ValidationError):
        return err("VALIDATION_ERROR", message)
    if isinstance(exc, ConnectionError):
        return err("CONNECTION_ERROR", message)
    if isinstance(exc, ServerError):
        return err("SERVER_ERROR", message)
    return err("AGENTCHAT_ERROR", message, type=exc.__class__.__name__)


def _extract_retry_after(exc: RateLimitedError) -> int | None:
    """Pull ``Retry-After`` out of the SDK error, normalized to seconds.

    The SDK stores the header in milliseconds on
    :attr:`RateLimitedError.retry_after_ms`. We surface seconds to the
    LLM because every existing AgentChat error message references
    seconds (and humans / models reason in seconds, not ms).
    """
    ms = getattr(exc, "retry_after_ms", None)
    if isinstance(ms, (int, float)) and not isinstance(ms, bool):
        # Ceil-divide so a 500ms wait surfaces as 1s, not 0.
        return max(1, int((ms + 999) // 1000))
    return None


# -- handle parsing ---------------------------------------------------------

# Server-side handle constraint (per project_agentchat_handle_rules).
# Mirrored here so we 400 early without a round-trip on obviously
# invalid input. Matches the canonical regex.
_HANDLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def normalize_handle(value: Any, *, field: str = "handle") -> str:
    """Validate and canonicalize an ``@handle`` input.

    Accepts ``@alice``, ``alice``, ``Alice``. Returns ``alice``.
    Raises :class:`ToolArgError` for malformed input.
    """
    if not isinstance(value, str):
        raise ToolArgError(f"{field} must be a string")
    cleaned = value.strip().lstrip("@").lower()
    if not cleaned:
        raise ToolArgError(f"{field} is required")
    if not _HANDLE_RE.match(cleaned):
        raise ToolArgError(
            f"{field}={value!r} is not a valid AgentChat handle "
            "(lowercase letters/digits/hyphens, must start with a letter, "
            "no doubled or trailing hyphens, 3-30 chars)"
        )
    return cleaned


# -- arg-dict helpers -------------------------------------------------------


def require_str(args: dict[str, Any], key: str, *, max_len: int | None = None) -> str:
    """Pull a required string field. Raises :class:`ToolArgError` if missing or wrong type."""
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolArgError(f"{key} is required and must be a string")
    if max_len is not None and len(value) > max_len:
        raise ToolArgError(f"{key} exceeds max length of {max_len} characters")
    return value


def optional_str(
    args: dict[str, Any], key: str, *, max_len: int | None = None
) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgError(f"{key} must be a string when provided")
    if max_len is not None and len(value) > max_len:
        raise ToolArgError(f"{key} exceeds max length of {max_len} characters")
    return value


def optional_int(
    args: dict[str, Any], key: str, *, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolArgError(f"{key} must be an integer when provided")
    if minimum is not None and value < minimum:
        raise ToolArgError(f"{key}={value} is below the minimum of {minimum}")
    if maximum is not None and value > maximum:
        raise ToolArgError(f"{key}={value} is above the maximum of {maximum}")
    return value


def optional_bool(args: dict[str, Any], key: str) -> bool | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ToolArgError(f"{key} must be a boolean when provided")
    return value


# -- handler glue -----------------------------------------------------------


def handle_arg_error(exc: ToolArgError) -> str:
    """Map a :class:`ToolArgError` to a tool error envelope."""
    return err("VALIDATION_ERROR", str(exc))
