"""Stable Hermes identity for every AgentChat request the plugin makes.

The SDK sends it as ``X-AgentChat-Client: hermes`` and
``X-AgentChat-Client-Version: <this package's version>`` on REST calls
(registration and recovery included) and on the socket. The hosted tools
are reached by Hermes' own MCP client, not the SDK, so the same two headers
are written into the ``mcp_servers.agentchat`` entry
(:func:`hermes_client_headers`); the server shows a socket integration such
as this one no wake-webhook tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ._version import __version__

if TYPE_CHECKING:
    from agentchatme import AgentChatClientIdentity

CLIENT_NAME: Final = "hermes"
CLIENT_HEADER: Final = "X-AgentChat-Client"
CLIENT_VERSION_HEADER: Final = "X-AgentChat-Client-Version"


def hermes_client_identity() -> AgentChatClientIdentity:
    """Construct lazily so CLI diagnostics can still report a missing SDK."""
    from agentchatme import AgentChatClientIdentity

    return AgentChatClientIdentity(name=CLIENT_NAME, version=__version__)


def hermes_client_headers() -> dict[str, str]:
    """The identity as the two request headers, for a client that is not the SDK."""
    return {CLIENT_HEADER: CLIENT_NAME, CLIENT_VERSION_HEADER: __version__}
