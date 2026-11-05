"""Are the hosted AgentChat tools registered in this Hermes process?

The agent's tools are the hosted MCP server (``mcp_servers.agentchat`` in
``config.yaml``), connected by Hermes itself. Hermes registers an MCP
server's tools in its tool registry under the toolset ``mcp-<server>`` with
names ``mcp__<server>__<tool>`` — some seconds after a gateway starts (a
cold tool cache can take a reconcile tick, ~60 s), and only when the
``mcp`` SDK (the ``hermes-agent[mcp]`` extra) is importable.

A turn started before that point runs without any AgentChat tool: it
cannot read, cannot send, and must not count as having looked. The 0.4.0
live-fire caught exactly that on every gateway start (leg 7a: a backlog
gated ``reply``, a tool-less turn 3.6 s after start, tools registered at
9.8 s). So the integration asks the registry before it starts a turn and
holds the turn, bounded, until the tools are there.

Outside a Hermes process (tests, ``pip``-time inspection) the registry is
not importable: :func:`hosted_tool_names` returns ``None`` — *unknown* —
and the caller does not hold; the turn then fails to start on the missing
``run_agent`` import, as before.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .turn import MCP_SERVER_NAME, MCP_TOOL_PREFIX

logger = logging.getLogger(__name__)

HOSTED_TOOLSET = f"mcp-{MCP_SERVER_NAME}"
HOSTED_TOOL_NAME_PREFIX = f"{MCP_TOOL_PREFIX}{MCP_SERVER_NAME}__"

# How long a turn waits for Hermes to register the hosted tools before it
# counts as "not started" (nothing recorded, the retry ladder re-wakes). A
# warm tool cache registers them 4-10 s after start; a cold one needs
# Hermes' reconcile tick (~60 s) — the ladder's first rung covers that.
HOSTED_TOOLS_WAIT_S = 30.0
HOSTED_TOOLS_POLL_S = 0.5

Probe = Callable[[], Optional[list[str]]]


def hosted_tool_names() -> list[str] | None:
    """The hosted AgentChat tools Hermes has registered right now.

    ``[]`` when Hermes is here but has not registered them (MCP server not
    connected yet, ``mcp`` SDK missing, entry disabled); ``None`` when this
    is not a Hermes process (the registry is not importable), which callers
    treat as *unknown*, not as absent.
    """
    try:
        from tools.registry import registry
    except ImportError:
        return None
    try:
        names = [str(n) for n in registry.get_tool_names_for_toolset(HOSTED_TOOLSET)]
        if not names:
            names = [
                str(n)
                for n in registry.get_all_tool_names()
                if str(n).startswith(HOSTED_TOOL_NAME_PREFIX)
            ]
    except Exception:
        # A registry shape we do not know is "nothing registered": a turn
        # waits and, if it comes to it, does not start — never a crash.
        logger.debug("hosted_tools: registry probe raised", exc_info=True)
        return []
    return sorted(names)


def hosted_tools_ready(probe: Probe | None = None) -> bool | None:
    """``True`` when at least one ``mcp__agentchat__*`` tool is registered,
    ``False`` when Hermes has none yet, ``None`` outside Hermes."""
    names = (probe or hosted_tool_names)()
    if names is None:
        return None
    return bool(names)


def wait_for_hosted_tools(
    *,
    timeout_s: float = HOSTED_TOOLS_WAIT_S,
    poll_s: float = HOSTED_TOOLS_POLL_S,
    probe: Probe | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
    label: str = "turn",
) -> bool:
    """Hold until the hosted tools are registered, at most ``timeout_s``.

    Returns ``True`` when they are there (or when it cannot be known —
    outside Hermes), ``False`` after the bound. Logs once when it starts
    waiting and once when the wait ends, so a slow start is visible. The
    probe and the clock are resolved at call time (tests swap them).
    """
    probe = probe or hosted_tool_names
    sleep = sleep or time.sleep
    now = now or time.monotonic
    ready = hosted_tools_ready(probe)
    if ready is None or ready:
        return True
    started = now()
    logger.info(
        "AgentInvoker: hosted AgentChat tools not registered in Hermes yet — holding the %s up "
        "to %.0fs (mcp_servers.agentchat connects after the gateway starts)",
        label,
        timeout_s,
    )
    while True:
        elapsed = now() - started
        if elapsed >= timeout_s:
            logger.warning(
                "AgentInvoker: hosted AgentChat tools still not registered after %.0fs — the %s "
                "is not started: nothing is recorded, the cards stay on the tray and the next "
                "wake tries again. If this repeats, check `hermes agentchat doctor` (the mcp "
                "SDK — Hermes' [mcp] extra — and mcp_servers.agentchat in config.yaml).",
                elapsed,
                label,
            )
            return False
        sleep(min(poll_s, max(0.0, timeout_s - elapsed)))
        ready = hosted_tools_ready(probe)
        if ready is None or ready:
            logger.info(
                "AgentInvoker: hosted AgentChat tools registered after %.1fs — starting the %s",
                now() - started,
                label,
            )
            return True
