"""Tests for ``agentchatme_hermes.types``."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentchatme_hermes.types import (
    AgentIdentity,
    GroupInviteEvent,
    InboundEvent,
)


class TestInboundEventFromWsMessage:
    def _payload(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "id": "msg_abc123",
            "conversation_id": "conv_dm_xyz",
            "from": "@alice",
            "type": "text",
            "content": {"text": "hello there"},
            "created_at": "2026-05-12T10:30:00Z",
        }
        base.update(overrides)
        return base

    def test_parses_trusted_context_block(self) -> None:
        event = InboundEvent.from_ws_message(
            self._payload(
                context={
                    "sender": {
                        "handle": "alice",
                        "display_name": "Alice Liddell",
                        "kind": "system",
                    },
                    "conversation": {
                        "type": "group",
                        "group_name": "Ops",
                        "member_count": 5,
                    },
                    "mentions": ["me", "Bob"],
                }
            )
        )
        assert event is not None
        assert event.sender_display_name == "Alice Liddell"
        assert event.sender_kind == "system"
        assert event.group_name == "Ops"
        assert event.member_count == 5
        assert event.mentions == ("me", "bob")  # lowercased

    def test_absent_context_degrades_to_defaults(self) -> None:
        event = InboundEvent.from_ws_message(self._payload())
        assert event is not None
        assert event.sender_display_name is None
        assert event.sender_kind == "agent"
        assert event.group_name is None
        assert event.mentions == ()

    def test_malformed_context_never_fails_the_frame(self) -> None:
        event = InboundEvent.from_ws_message(self._payload(context="not-a-dict"))
        assert event is not None
        assert event.sender_kind == "agent"
        assert event.mentions == ()

    def test_happy_path_direct(self) -> None:
        event = InboundEvent.from_ws_message(self._payload())
        assert event is not None
        assert event.message_id == "msg_abc123"
        assert event.conversation_id == "conv_dm_xyz"
        assert event.conversation_kind == "direct"
        assert event.sender_handle == "alice"
        assert event.content_text == "hello there"

    def test_happy_path_group(self) -> None:
        event = InboundEvent.from_ws_message(
            self._payload(conversation_id="conv_grp_team42")
        )
        assert event is not None
        assert event.conversation_kind == "group"

    def test_handle_normalized_lowercase(self) -> None:
        event = InboundEvent.from_ws_message(self._payload(**{"from": "@Alice"}))
        assert event is not None
        assert event.sender_handle == "alice"

    def test_at_prefix_stripped(self) -> None:
        event = InboundEvent.from_ws_message(self._payload(**{"from": "alice"}))
        assert event is not None
        assert event.sender_handle == "alice"

    def test_sender_field_fallback(self) -> None:
        # Some platform versions use `sender` instead of `from` — we
        # accept either.
        payload = self._payload()
        del payload["from"]
        payload["sender"] = "@bob"
        event = InboundEvent.from_ws_message(payload)
        assert event is not None
        assert event.sender_handle == "bob"

    def test_text_content_extracted(self) -> None:
        event = InboundEvent.from_ws_message(self._payload())
        assert event is not None
        assert event.content_text == "hello there"

    def test_non_text_message_uses_placeholder(self) -> None:
        payload = self._payload(type="file", content={"data": {"file_id": "f_1"}})
        event = InboundEvent.from_ws_message(payload)
        assert event is not None
        assert "non-text" in event.content_text
        assert "file" in event.content_text

    def test_missing_id_returns_none(self) -> None:
        payload = self._payload()
        del payload["id"]
        assert InboundEvent.from_ws_message(payload) is None

    def test_missing_conversation_id_returns_none(self) -> None:
        payload = self._payload()
        del payload["conversation_id"]
        assert InboundEvent.from_ws_message(payload) is None

    def test_missing_sender_returns_none(self) -> None:
        payload = self._payload()
        del payload["from"]
        assert InboundEvent.from_ws_message(payload) is None

    def test_empty_sender_returns_none(self) -> None:
        assert InboundEvent.from_ws_message(self._payload(**{"from": ""})) is None

    def test_non_dict_content_returns_none(self) -> None:
        assert InboundEvent.from_ws_message(self._payload(content="not a dict")) is None

    def test_malformed_created_at_uses_now(self) -> None:
        event = InboundEvent.from_ws_message(self._payload(created_at="not a date"))
        assert event is not None
        # Should fall back to "now" — verify it's a recent UTC datetime
        assert event.received_at.tzinfo is not None
        delta = (datetime.now(timezone.utc) - event.received_at).total_seconds()
        assert -1 < delta < 5

    def test_iso_created_at_parsed(self) -> None:
        event = InboundEvent.from_ws_message(
            self._payload(created_at="2026-05-12T10:30:00Z")
        )
        assert event is not None
        assert event.received_at == datetime(2026, 5, 12, 10, 30, tzinfo=timezone.utc)

    def test_frozen_dataclass_immutable(self) -> None:
        event = InboundEvent.from_ws_message(self._payload())
        assert event is not None
        with pytest.raises(Exception):  # FrozenInstanceError is dataclasses-specific
            event.content_text = "tampered"  # type: ignore[misc]


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
