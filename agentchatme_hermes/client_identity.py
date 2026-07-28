"""Stable Hermes identity for AgentChat SDK and raw registration traffic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._version import __version__

if TYPE_CHECKING:
    from agentchatme import AgentChatClientIdentity

HERMES_CLIENT_HEADERS: dict[str, str] = {
    "X-AgentChat-Client": "hermes",
    "X-AgentChat-Client-Version": __version__,
}


def hermes_client_identity() -> AgentChatClientIdentity:
    """Construct lazily so CLI diagnostics can still report a missing SDK."""
    from agentchatme import AgentChatClientIdentity

    return AgentChatClientIdentity(name="hermes", version=__version__)
