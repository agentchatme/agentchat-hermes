from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from agentchatme_hermes.lookup_cache import LookupCache
from agentchatme_hermes.tools.groups import (
    _build_check_group_reply_readiness,
    _build_get_group_context,
    _build_get_group_participants,
    _build_list_recent_group_speakers,
)


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        client=MagicMock(),
        lookup_cache=LookupCache(),
        identity=SimpleNamespace(handle="me"),
    )


class TestGetGroupParticipants:
    def test_returns_sorted_roster_with_admin_and_creator_flags(self) -> None:
        runtime = _runtime()
        runtime.client.get_group.return_value = {
            "id": "conv_grp_1",
            "name": "Build Team",
            "created_by": "alice",
            "your_role": "member",
            "member_count": 3,
            "members": [
                {
                    "handle": "bob",
                    "display_name": "Bob",
                    "role": "member",
                    "joined_at": "2026-06-18T09:00:00Z",
                },
                {
                    "handle": "me",
                    "display_name": "Me",
                    "role": "member",
                    "joined_at": "2026-06-18T09:05:00Z",
                },
                {
                    "handle": "alice",
                    "display_name": "Alice",
                    "role": "admin",
                    "joined_at": "2026-06-18T08:55:00Z",
                },
            ],
        }
        handler = _build_get_group_participants(runtime)

        result = json.loads(handler({"group_id": "conv_grp_1"}))

        assert result["ok"] is True
        assert result["group_name"] == "Build Team"
        assert result["creator_handle"] == "alice"
        assert result["your_role"] == "member"
        assert result["admin_count"] == 1
        assert [item["handle"] for item in result["participants"]] == [
            "alice",
            "bob",
            "me",
        ]
        assert result["participants"][0]["is_creator"] is True
        assert result["participants"][2]["is_you"] is True

    def test_reuses_cached_group_detail(self) -> None:
        runtime = _runtime()
        runtime.lookup_cache.set(
            "group:detail:conv_grp_1",
            {
                "id": "conv_grp_1",
                "name": "Build Team",
                "created_by": "alice",
                "your_role": "admin",
                "member_count": 1,
                "members": [
                    {
                        "handle": "alice",
                        "display_name": "Alice",
                        "role": "admin",
                        "joined_at": "2026-06-18T08:55:00Z",
                    }
                ],
            },
            ttl_seconds=60.0,
        )
        handler = _build_get_group_participants(runtime)

        result = json.loads(handler({"group_id": "conv_grp_1"}))

        assert result["ok"] is True
        assert result["member_count"] == 1
        runtime.client.get_group.assert_not_called()


class TestRecentGroupSpeakers:
    def test_summarizes_recent_speakers(self) -> None:
        runtime = _runtime()
        runtime.client.get_group.return_value = {
            "id": "conv_grp_1",
            "name": "Build Team",
            "created_by": "alice",
            "your_role": "member",
            "member_count": 3,
            "members": [
                {"handle": "alice", "display_name": "Alice", "role": "admin"},
                {"handle": "bob", "display_name": "Bob", "role": "member"},
                {"handle": "me", "display_name": "Me", "role": "member"},
            ],
        }
        runtime.client.get_messages.return_value = [
            {
                "id": "m3",
                "sender": "alice",
                "seq": 3,
                "type": "text",
                "content": {"text": "can someone take this?"},
                "created_at": "2026-06-18T10:02:00Z",
            },
            {
                "id": "m2",
                "sender": "bob",
                "seq": 2,
                "type": "text",
                "content": {"text": "I checked the logs"},
                "created_at": "2026-06-18T10:01:00Z",
            },
            {
                "id": "m1",
                "sender": "alice",
                "seq": 1,
                "type": "text",
                "content": {"text": "build is red"},
                "created_at": "2026-06-18T10:00:00Z",
            },
        ]
        handler = _build_list_recent_group_speakers(runtime)

        result = json.loads(handler({"group_id": "conv_grp_1"}))

        assert result["ok"] is True
        assert result["speaker_count"] == 2
        assert result["recent_speakers"][0]["handle"] == "alice"
        assert result["recent_speakers"][0]["message_count"] == 2
        assert result["latest_non_self_speaker"] == "alice"


class TestGroupContext:
    def test_returns_context_with_reply_targets(self) -> None:
        runtime = _runtime()
        runtime.client.get_group.return_value = {
            "id": "conv_grp_1",
            "name": "Build Team",
            "description": "Release coordination",
            "created_by": "alice",
            "your_role": "member",
            "member_count": 3,
            "members": [
                {"handle": "alice", "display_name": "Alice", "role": "admin"},
                {"handle": "bob", "display_name": "Bob", "role": "member"},
                {"handle": "me", "display_name": "Me", "role": "member"},
            ],
        }
        runtime.client.get_messages.return_value = [
            {
                "id": "m3",
                "sender": "alice",
                "seq": 3,
                "type": "text",
                "content": {"text": "can someone take this?"},
                "created_at": "2026-06-18T10:02:00Z",
            },
            {
                "id": "m2",
                "sender": "me",
                "seq": 2,
                "type": "text",
                "content": {"text": "I can look after lunch"},
                "created_at": "2026-06-18T10:01:00Z",
            },
            {
                "id": "m1",
                "sender": "bob",
                "seq": 1,
                "type": "text",
                "content": {"text": "build is red"},
                "created_at": "2026-06-18T10:00:00Z",
            },
        ]
        handler = _build_get_group_context(runtime)

        result = json.loads(handler({"group_id": "conv_grp_1"}))

        assert result["ok"] is True
        assert result["group_name"] == "Build Team"
        assert result["activity_state"] == "active"
        assert result["latest_non_self_speaker"] == "alice"
        assert result["suggested_reply_targets"] == ["alice", "bob"]


class TestGroupReplyReadiness:
    def test_reports_target_presence(self) -> None:
        runtime = _runtime()
        runtime.client.get_group.return_value = {
            "id": "conv_grp_1",
            "name": "Build Team",
            "created_by": "alice",
            "your_role": "member",
            "member_count": 3,
            "members": [
                {"handle": "alice", "display_name": "Alice", "role": "admin"},
                {"handle": "bob", "display_name": "Bob", "role": "member"},
                {"handle": "me", "display_name": "Me", "role": "member"},
            ],
        }
        handler = _build_check_group_reply_readiness(runtime)

        result = json.loads(
            handler({"group_id": "conv_grp_1", "target_handle": "@alice"})
        )

        assert result["ok"] is True
        assert result["can_reply"] is True
        assert result["target_present"] is True
        assert result["target_is_self"] is False
        assert result["suggested_reply_targets"] == ["alice", "bob"]

    def test_reports_missing_target(self) -> None:
        runtime = _runtime()
        runtime.client.get_group.return_value = {
            "id": "conv_grp_1",
            "name": "Build Team",
            "created_by": "alice",
            "your_role": "member",
            "member_count": 2,
            "members": [
                {"handle": "alice", "display_name": "Alice", "role": "admin"},
                {"handle": "me", "display_name": "Me", "role": "member"},
            ],
        }
        handler = _build_check_group_reply_readiness(runtime)

        result = json.loads(
            handler({"group_id": "conv_grp_1", "target_handle": "@carol"})
        )

        assert result["ok"] is True
        assert result["target_present"] is False
