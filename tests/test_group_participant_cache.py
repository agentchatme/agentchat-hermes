from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agentchatme_hermes.group_participants import (
    get_cached_group_detail,
    get_cached_group_participants,
    invalidate_group_lookup_cache,
)
from agentchatme_hermes.lookup_cache import LookupCache


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        client=MagicMock(),
        lookup_cache=LookupCache(),
    )


class TestGroupParticipantCache:
    def test_group_detail_uses_cache_on_repeat_lookup(self) -> None:
        runtime = _runtime()
        runtime.client.get_group.return_value = {"id": "conv_grp_1", "name": "Build Team"}

        first = get_cached_group_detail(runtime, "conv_grp_1")
        second = get_cached_group_detail(runtime, "conv_grp_1")

        assert first["id"] == "conv_grp_1"
        assert second["name"] == "Build Team"
        runtime.client.get_group.assert_called_once_with("conv_grp_1")

    def test_group_participants_can_be_derived_from_cached_group_detail(self) -> None:
        runtime = _runtime()
        runtime.lookup_cache.set(
            "group:detail:conv_grp_1",
            {
                "id": "conv_grp_1",
                "members": [
                    {"handle": "bob", "display_name": "Bob", "role": "member"},
                    {"handle": "alice", "display_name": "Alice", "role": "admin"},
                ],
            },
            ttl_seconds=60.0,
        )

        participants = get_cached_group_participants(runtime, "conv_grp_1")

        assert participants == [
            {"handle": "alice", "display_name": "Alice"},
            {"handle": "bob", "display_name": "Bob"},
        ]
        runtime.client.get_conversation_participants.assert_not_called()

    def test_group_participants_use_cache_on_repeat_lookup(self) -> None:
        runtime = _runtime()
        runtime.client.get_conversation_participants.return_value = [
            {"handle": "bob", "display_name": "Bob"},
            {"handle": "alice", "display_name": "Alice"},
        ]

        first = get_cached_group_participants(runtime, "conv_grp_1")
        second = get_cached_group_participants(runtime, "conv_grp_1")

        assert first == second
        runtime.client.get_conversation_participants.assert_called_once_with("conv_grp_1")

    def test_invalidate_group_lookup_cache_clears_detail_and_participants(self) -> None:
        runtime = _runtime()
        runtime.lookup_cache.set("group:detail:conv_grp_1", {"id": "conv_grp_1"}, ttl_seconds=60.0)
        runtime.lookup_cache.set(
            "group:participants:conv_grp_1",
            [{"handle": "alice"}],
            ttl_seconds=60.0,
        )

        invalidate_group_lookup_cache(runtime, "conv_grp_1")

        assert runtime.lookup_cache.get("group:detail:conv_grp_1") is None
        assert runtime.lookup_cache.get("group:participants:conv_grp_1") is None
