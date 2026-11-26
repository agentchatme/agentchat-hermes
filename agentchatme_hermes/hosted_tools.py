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

A parked server
───────────────
Hermes' MCP client tries a server's first connection three times; when they
all fail (the gateway started while api.agentchat.me could not be reached: a
reboot before the network, a DNS or provider outage) it *parks* the server:
its tools are dropped from the registry and Hermes probes it again on its
own, after 5 minutes and then at doubling intervals up to an hour
(``tools/mcp_tool.py`` ``_PARKED_RETRY_INTERVAL`` / ``_PARKED_PROBE_BACKOFF_MAX``).
Hermes has no public way for a plugin to ask for a reconnect, so this plugin
does not: while the server is parked every wake ends ``tools_unavailable``,
and the log line and ``doctor`` name the cause and the remedy — ``/reload-mcp``
in a chat this gateway serves, or ``hermes gateway restart``. Once Hermes
connects it again, the gateway's minute check
(:meth:`.agent_invoker.AgentInvoker.check_tools`) wakes the receive loop for
the cards that waited.
:func:`hosted_server_state` is Hermes' own reading of the server (``hermes
mcp list``'s), read only.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from .turn import MCP_SERVER_NAME, MCP_TOOL_PREFIX

if TYPE_CHECKING:
    import threading
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

HOSTED_TOOLSET = f"mcp-{MCP_SERVER_NAME}"
HOSTED_TOOL_NAME_PREFIX = f"{MCP_TOOL_PREFIX}{MCP_SERVER_NAME}__"

# How long a turn waits for Hermes to register the hosted tools before it
# counts as "not started" (nothing recorded; the server re-wakes). A
# warm tool cache registers them 4-10 s after start; a cold one needs
# Hermes' reconcile tick (~60 s) — the server's first re-wake (+1 m) covers that.
HOSTED_TOOLS_WAIT_S = 30.0
HOSTED_TOOLS_POLL_S = 0.5

Probe = Callable[[], Optional[list[str]]]

# What a parked server waits for on its own (Hermes' self-probe), and what
# brings it back now: said in the log line and by ``doctor``.
PARKED_REMEDY = (
    "Hermes probes it again on its own after 5 minutes, then at longer intervals up to an "
    "hour; to reconnect it now, send /reload-mcp in a chat this gateway serves, or run "
    "`hermes gateway restart`"
)


@dataclass(frozen=True)
class ServerState:
    """Hermes' own reading of ``mcp_servers.agentchat`` in this process and
    profile (``tools.mcp_tool_discovery.get_mcp_status``, what ``hermes mcp
    list`` shows): ``status`` is Hermes' (``connected``, ``connecting``,
    ``failed`` — a failed connection, a parked server included —,
    ``disabled``, ``lazy``, ``configured``), or ``missing`` when the profile's
    config has no such entry."""

    status: str
    error: str | None = None
    tools: int = 0
    needs_auth: bool = False

    @property
    def parked(self) -> bool:
        return self.status == "failed"


StateProbe = Callable[[], Optional[ServerState]]


def hosted_server_state(entry: Mapping[str, Any] | None = None) -> ServerState | None:
    """Hermes' status of the hosted tools server in the current profile scope;
    ``None`` when this is not a Hermes process or its MCP client has no such
    reading (a Hermes that moved it): unknown. ``entry``: read the status for
    this entry rather than the one in ``config.yaml`` (Hermes' runtime state
    for the name, also after the entry was removed). Reads Hermes' cached
    state, never connects. Never raises."""
    try:
        from tools.mcp_tool_discovery import get_mcp_status
    except Exception:
        return None
    try:
        rows = get_mcp_status() if entry is None else get_mcp_status(configured={MCP_SERVER_NAME: dict(entry)})
    except Exception as exc:
        _warn_once(f"hosted_tools: Hermes' MCP status raised ({exc!r}) — the server's state is not known")
        return None
    for row in rows or []:
        if isinstance(row, dict) and row.get("name") == MCP_SERVER_NAME:
            error = row.get("error")
            tools = row.get("tools")
            return ServerState(
                status=str(row.get("status") or "unknown"),
                error=str(error) if isinstance(error, str) and error else None,
                tools=tools if isinstance(tools, int) else 0,
                needs_auth=bool(row.get("needs_auth")),
            )
    return ServerState(status="missing")


def _cmd() -> str:
    """``hermes agentchat`` for the profile in scope (``hermes -p NAME agentchat``)."""
    try:
        from .config import cli_prefix
        from .profile_scope import resolve_hermes_home

        return cli_prefix(resolve_hermes_home())
    except Exception:
        return "hermes agentchat"


def describe_cause(state: ServerState | None) -> str:
    """Why the hosted tools are not registered, in words, with what to do."""
    cmd = _cmd()
    if state is None:
        return (
            f"Hermes' MCP state is not readable here; check `{cmd} doctor` (the mcp SDK — "
            "Hermes' [mcp] extra — and mcp_servers.agentchat in config.yaml)"
        )
    if state.parked:
        why = f" ({state.error})" if state.error else ""
        return f"Hermes parked mcp_servers.agentchat after its connection failed{why}: {PARKED_REMEDY}"
    if state.status == "connecting":
        return "Hermes is still connecting mcp_servers.agentchat"
    if state.status == "disabled":
        return "mcp_servers.agentchat is disabled in this profile's config.yaml — enable it, then `hermes gateway restart`"
    if state.status == "missing":
        return (
            "mcp_servers.agentchat is not in this profile's config.yaml: AgentChat tools are not allowed yet "
            f"— /agentchat allow in a chat with the agent, or `{cmd} allow`, adds it"
        )
    if state.status == "connected":
        return (
            "Hermes reports mcp_servers.agentchat connected but no mcp__agentchat__ tool is registered for "
            "this profile — run `hermes gateway restart`"
        )
    return (
        f"Hermes has not connected mcp_servers.agentchat in this process (status {state.status}); check "
        f"`{cmd} doctor` (the mcp SDK — Hermes' [mcp] extra — and mcp_servers.agentchat in config.yaml)"
    )


def hosted_tool_names() -> list[str] | None:
    """The hosted AgentChat tools Hermes has registered right now.

    ``[]`` when Hermes is here but has not registered them (MCP server not
    connected yet, ``mcp`` SDK missing, entry disabled); ``None`` when this
    is not a Hermes process (the registry is not importable), which callers
    treat as *unknown*, not as absent.
    """
    try:
        from tools.registry import registry
    except Exception:
        # Not a Hermes process (or a registry that does not import): unknown.
        return None
    by_toolset = getattr(registry, "get_tool_names_for_toolset", None)
    all_names = getattr(registry, "get_all_tool_names", None)
    if not callable(by_toolset) or not callable(all_names):
        # A stray ``tools/registry.py`` that is not Hermes': unknown, no hold.
        _warn_once("hosted_tools: tools.registry has no Hermes tool registry — not holding turns")
        return None
    try:
        names = [str(n) for n in by_toolset(HOSTED_TOOLSET)]
        if not names:
            names = [str(n) for n in all_names() if str(n).startswith(HOSTED_TOOL_NAME_PREFIX)]
    except Exception as exc:
        # Registry drift: unknown rather than "none registered", so a probe
        # that cannot work does not hold every turn to its bound.
        _warn_once(f"hosted_tools: registry probe raised ({exc!r}) — not holding turns")
        return None
    return sorted(names)


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _warned:
        return
    _warned.add(message)
    logger.warning(message)


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
    stopped: threading.Event | None = None,
    label: str = "turn",
    state_probe: StateProbe | None = None,
) -> bool | None:
    """Hold until the hosted tools are registered, at most ``timeout_s``.

    Returns ``True`` when they are there (or when it cannot be known —
    outside Hermes), ``False`` after the bound, ``None`` when ``stopped``
    was set meanwhile (the invoker is stopping — its leader lease is gone,
    nothing may start). Logs once when it starts waiting and once when the
    wait ends, so a slow start is visible; the closing line names the cause
    Hermes gives (a parked server: :data:`PARKED_REMEDY`). The probes and
    the clock are resolved at call time (tests swap them); with ``stopped``
    the poll waits on the event, so a stop ends the hold at once.
    """
    probe = probe or hosted_tool_names
    state_probe = state_probe or hosted_server_state
    now = now or time.monotonic
    if sleep is None:
        if stopped is not None:
            event = stopped

            def sleep(seconds: float) -> None:
                event.wait(seconds)

        else:
            sleep = time.sleep
    if stopped is not None and stopped.is_set():
        return None
    ready = hosted_tools_ready(probe)
    if ready is None or ready:
        return True
    started = now()
    state = state_probe()
    if state is not None and state.parked:
        logger.info(
            "AgentInvoker: hosted AgentChat tools not registered — Hermes parked mcp_servers.agentchat "
            "(%s); holding the %s up to %.0fs",
            state.error or "its connection failed",
            label,
            timeout_s,
        )
    else:
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
                "wake tries again. Cause: %s.",
                elapsed,
                label,
                describe_cause(state_probe()),
            )
            return False
        sleep(min(poll_s, max(0.0, timeout_s - elapsed)))
        if stopped is not None and stopped.is_set():
            logger.info("AgentInvoker: stopping while holding the %s — it is not started", label)
            return None
        ready = hosted_tools_ready(probe)
        if ready is None or ready:
            logger.info(
                "AgentInvoker: hosted AgentChat tools registered after %.1fs — starting the %s",
                now() - started,
                label,
            )
            return True
