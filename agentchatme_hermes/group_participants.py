from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime import Runtime


_GROUP_DETAIL_TTL_SECONDS = 20.0
_GROUP_PARTICIPANTS_TTL_SECONDS = 20.0


def _group_detail_cache_key(group_id: str) -> str:
    return f"group:detail:{group_id}"


def _group_participants_cache_key(group_id: str) -> str:
    return f"group:participants:{group_id}"


def _normalize_basic_participant_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        handle = row.get("handle")
        if not isinstance(handle, str) or not handle:
            continue
        item: dict[str, Any] = {"handle": handle}
        display_name = row.get("display_name")
        if isinstance(display_name, str) and display_name:
            item["display_name"] = display_name
        normalized.append(item)
    normalized.sort(key=lambda item: str(item["handle"]).lower())
    return normalized


def invalidate_group_lookup_cache(runtime: Runtime, group_id: str) -> None:
    runtime.lookup_cache.invalidate(_group_detail_cache_key(group_id))
    runtime.lookup_cache.invalidate(_group_participants_cache_key(group_id))


def get_cached_group_detail(runtime: Runtime, group_id: str) -> dict[str, Any]:
    cache_key = _group_detail_cache_key(group_id)
    cached = runtime.lookup_cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    result = runtime.client.get_group(group_id)
    runtime.lookup_cache.set(
        cache_key,
        result,
        ttl_seconds=_GROUP_DETAIL_TTL_SECONDS,
    )
    return result


def get_cached_group_participants(runtime: Runtime, group_id: str) -> list[dict[str, Any]]:
    cache_key = _group_participants_cache_key(group_id)
    cached = runtime.lookup_cache.get(cache_key)
    if isinstance(cached, list):
        return cached

    detail_cached = runtime.lookup_cache.get(_group_detail_cache_key(group_id))
    if isinstance(detail_cached, dict):
        members = detail_cached.get("members")
        if isinstance(members, list):
            normalized = _normalize_basic_participant_rows(
                [member for member in members if isinstance(member, dict)]
            )
            runtime.lookup_cache.set(
                cache_key,
                normalized,
                ttl_seconds=_GROUP_PARTICIPANTS_TTL_SECONDS,
            )
            return normalized

    result = runtime.client.get_conversation_participants(group_id)
    normalized = _normalize_basic_participant_rows(
        [row for row in result if isinstance(row, dict)]
    )
    runtime.lookup_cache.set(
        cache_key,
        normalized,
        ttl_seconds=_GROUP_PARTICIPANTS_TTL_SECONDS,
    )
    return normalized


def get_group_member_rows(runtime: Runtime, group_id: str) -> list[dict[str, Any]]:
    detail = get_cached_group_detail(runtime, group_id)
    members = detail.get("members")
    if not isinstance(members, list):
        return []

    own_handle = runtime.identity.handle
    creator_handle = detail.get("created_by")
    normalized: list[dict[str, Any]] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        handle = member.get("handle")
        if not isinstance(handle, str) or not handle:
            continue
        item: dict[str, Any] = {
            "handle": handle,
            "role": member.get("role"),
            "is_you": handle.lstrip("@").lower() == own_handle,
            "is_creator": handle == creator_handle,
        }
        display_name = member.get("display_name")
        if isinstance(display_name, str) and display_name:
            item["display_name"] = display_name
        joined_at = member.get("joined_at")
        if isinstance(joined_at, str) and joined_at:
            item["joined_at"] = joined_at
        normalized.append(item)

    normalized.sort(
        key=lambda item: (
            0 if item.get("role") == "admin" else 1,
            str(item["handle"]).lower(),
        )
    )
    return normalized
