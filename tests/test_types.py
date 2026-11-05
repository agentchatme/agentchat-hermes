"""Tests for ``agentchatme_hermes.types``.

There is no ``InboundEvent`` any more: a ``message.new`` frame is a wake,
and the integration never parses a message body (see ``ws_daemon``).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentchatme_hermes.types import AgentIdentity, GroupInviteEvent


class TestGroupInviteEventFromWsFrame:
    def _payload(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "id": "ginv_abc",
            "group_id": "grp_xyz",
            "group_name": "Agent Council",
            "group_description": "planning room",
            "group_member_count": 4,
            "inviter_handle": "@carol",
            "created_at": "2026-07-24T14:05:04Z",
        }
        base.update(overrides)
        return base

    def test_happy_path(self) -> None:
        ev = GroupInviteEvent.from_ws_frame(self._payload())
        assert ev is not None
        assert ev.invite_id == "ginv_abc"
        assert ev.group_id == "grp_xyz"
        assert ev.group_name == "Agent Council"
        assert ev.inviter_handle == "carol"  # @-stripped + lowered
        assert ev.group_description == "planning room"
        assert ev.member_count == 4
        assert ev.received_at == datetime(2026, 7, 24, 14, 5, 4, tzinfo=timezone.utc)

    def test_missing_invite_id_returns_none(self) -> None:
        assert GroupInviteEvent.from_ws_frame(self._payload(id=None)) is None

    def test_missing_group_id_returns_none(self) -> None:
        p = self._payload()
        del p["group_id"]
        assert GroupInviteEvent.from_ws_frame(p) is None

    def test_informational_fields_degrade_gracefully(self) -> None:
        # Only the load-bearing ids are required; the rest default.
        ev = GroupInviteEvent.from_ws_frame(
            {"id": "ginv_1", "group_id": "grp_1"}
        )
        assert ev is not None
        assert ev.group_name == "grp_1"  # falls back to id when name absent
        assert ev.inviter_handle == "unknown"
        assert ev.group_description is None
        assert ev.member_count is None

    def test_frozen_dataclass_immutable(self) -> None:
        ev = GroupInviteEvent.from_ws_frame(self._payload())
        assert ev is not None
        with pytest.raises(Exception):
            ev.group_id = "grp_other"  # type: ignore[misc]


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
