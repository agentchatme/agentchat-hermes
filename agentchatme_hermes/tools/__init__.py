"""Hermes tool registration for AgentChat.

All ``agentchat_*`` tools are added to the global registry from one
function (:func:`register_tools`). Per Hermes' plugin contract
(``hermes_cli/plugins.py:317-344``) each tool needs a name, toolset,
JSON schema, handler. Handlers return JSON strings (success or error
envelope) — the LLM parses them as structured tool output.

Modules in this package each export a ``TOOLS`` tuple of
``(name, schema, handler_builder)``. ``handler_builder`` is a
factory taking the runtime singleton — it captures the shared HTTP
client so handlers don't have to re-resolve it on every call.
"""
from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from ..runtime import RuntimeHandle, RuntimeNotReady
from . import (
    attachments,
    contacts,
    conversations,
    directory,
    groups,
    messages,
    mutes,
    presence,
    profile,
)
from ._common import err

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..runtime import Runtime

logger = logging.getLogger(__name__)

# Single source of truth for the toolset name. Tools share this so
# `hermes tools` groups them cleanly and `disabled_toolsets`
# / `enabled_toolsets` works against one bucket.
TOOLSET = "agentchat"

_MODULES = (
    messages,
    conversations,
    contacts,
    profile,
    presence,
    directory,
    groups,
    mutes,
    attachments,
)


def register_tools(ctx: Any, runtime: Runtime | RuntimeHandle) -> None:
    """Register every ``agentchat_*`` tool with Hermes.

    ``runtime`` is normally a :class:`RuntimeHandle` bound later by the
    activation watcher, so tools exist from the first turn even on a
    profile that has no key yet. A call before activation returns the
    stable ``NOT_CONFIGURED`` error with the fix, instead of a traceback.

    Idempotent within a process — Hermes' tool registry rejects a
    second registration for the same name, so a re-register (e.g.,
    from a hot reload) logs a debug message and moves on.
    """
    registered = 0
    for module in _MODULES:
        for name, schema, builder, emoji in module.TOOLS:
            ctx.register_tool(
                name=name,
                toolset=TOOLSET,
                schema=schema,
                handler=_guard_not_ready(builder(runtime)),
                emoji=emoji,
            )
            registered += 1
    logger.info("agentchat: registered %d tools under toolset '%s'", registered, TOOLSET)


def _guard_not_ready(handler: Callable[..., str]) -> Callable[..., str]:
    """Translate :class:`RuntimeNotReady` into the tool error envelope."""

    @functools.wraps(handler)
    def wrapped(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            return handler(args, **kwargs)
        except RuntimeNotReady as exc:
            return err("NOT_CONFIGURED", str(exc))

    return wrapped
