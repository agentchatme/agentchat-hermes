"""Stable Hermes identity for every AgentChat request the plugin makes.

The SDK sends it as ``X-AgentChat-Client: hermes`` and
``X-AgentChat-Client-Version: <this package's version>`` on REST calls
(registration and recovery included) and on the socket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._version import __version__

if TYPE_CHECKING:
    from agentchatme import AgentChatClientIdentity


def hermes_client_identity() -> AgentChatClientIdentity:
    """Construct lazily so CLI diagnostics can still report a missing SDK."""
    from agentchatme import AgentChatClientIdentity

    return AgentChatClientIdentity(name="hermes", version=__version__)
