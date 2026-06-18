from __future__ import annotations

from agentchatme_hermes.lookup_cache import LookupCache


class TestLookupCache:
    def test_get_returns_cached_value_before_ttl(self) -> None:
        cache = LookupCache()

        cache.set("directory:alice", {"handle": "alice"}, ttl_seconds=60.0)

        assert cache.get("directory:alice") == {"handle": "alice"}

    def test_expired_entry_is_dropped(self) -> None:
        cache = LookupCache()

        cache.set("directory:alice", {"handle": "alice"}, ttl_seconds=-1.0)

        assert cache.get("directory:alice") is None

    def test_invalidate_prefix_removes_matching_keys_only(self) -> None:
        cache = LookupCache()
        cache.set("contacts:list:1", ["alice"], ttl_seconds=60.0)
        cache.set("contacts:check:alice", {"handle": "alice"}, ttl_seconds=60.0)
        cache.set("directory:search", ["alice"], ttl_seconds=60.0)

        cache.invalidate_prefix("contacts:")

        assert cache.get("contacts:list:1") is None
        assert cache.get("contacts:check:alice") is None
        assert cache.get("directory:search") == ["alice"]
