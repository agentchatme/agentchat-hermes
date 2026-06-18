from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from agentchatme_hermes.lookup_cache import LookupCache
from agentchatme_hermes.tools.groups import _build_get_group_participants


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
