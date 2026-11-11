"""Stable Hermes identity for every AgentChat request the plugin makes.

The SDK sends it as ``X-AgentChat-Client: hermes`` and
``X-AgentChat-Client-Version: <this package's version>`` on REST calls
(registration and recovery included) and on the socket. The hosted tools
are reached by Hermes' own MCP client, not the SDK, so the
``mcp_servers.agentchat`` entry carries the client name
(:func:`hermes_mcp_headers`) — the name only: the entry is written without
a prompt (at plugin activation), so it must not change from release to
release, and the server already has the version from the SDK's traffic. The
server shows a socket integration such as this one no wake-webhook tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ._version import __version__

if TYPE_CHECKING:
    from agentchatme import AgentChatClientIdentity

CLIENT_NAME: Final = "hermes"
CLIENT_HEADER: Final = "X-AgentChat-Client"


def hermes_client_identity() -> AgentChatClientIdentity:
    """Construct lazily so CLI diagnostics can still report a missing SDK."""
    from agentchatme import AgentChatClientIdentity

    return AgentChatClientIdentity(name=CLIENT_NAME, version=__version__)


def hermes_mcp_headers() -> dict[str, str]:
    """The identity header of the hosted-tools entry: static, the client name only."""
    return {CLIENT_HEADER: CLIENT_NAME}
