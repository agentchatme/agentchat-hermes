"""Tests for ``agentchatme_hermes.types``.

There is no ``InboundEvent`` any more: a ``message.new`` frame is a wake,
and the integration never parses a message body (see ``ws_daemon``).
"""
from __future__ import annotations

import pytest

from agentchatme_hermes.types import AgentIdentity


class TestAgentIdentity:
    def test_frozen(self) -> None:
        ident = AgentIdentity(handle="alice")
        with pytest.raises(Exception):
            ident.handle = "evil"  # type: ignore[misc]

    def test_no_internal_id_field(self) -> None:
        """AgentIdentity must NOT carry the server-side ``agt_…`` id.

        Internal database ids are a server-only concept; agents
        identify by handle on the wire. Surfacing the id in the plugin
        would be a needless attack-surface expansion (logs, error
        messages, agent-visible context).
        """
        ident = AgentIdentity(handle="alice")
        # Should NOT have an agent_id attribute at all.
        assert not hasattr(ident, "agent_id"), (
            "AgentIdentity must not expose internal id — only handle"
        )
