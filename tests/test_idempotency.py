"""Tests for the stable `client_msg_id` idempotency helper.

Hermes's `_send_with_retry` (`base.py:2315` in v0.13) retries a `send`
when the result has `retryable=True`. Among our retryable cases is
`ACConnectionError`, which is ambiguous about whether the message
actually reached the server. Without a stable `client_msg_id`, the SDK
auto-generates a fresh UUID per call and a retry produces a duplicate.

`_stable_client_msg_id` derives a deterministic UUIDv5 from
(sender, chat_id, content, reply_to, 120-second bucket). Identical
inputs within the same window → identical id; inputs 2+ minutes apart
→ different id.
"""

from __future__ import annotations

import re
from unittest.mock import patch


def test_same_tuple_same_id_within_window():
    from agentchatme_hermes.adapter import _stable_client_msg_id

    a = _stable_client_msg_id("@me", "@alice", "hello", None)
    b = _stable_client_msg_id("@me", "@alice", "hello", None)
    assert a == b
    # Sanity: looks like a UUID.
    assert re.match(r"[0-9a-f-]{36}", a)


def test_different_chat_id_different_id():
    from agentchatme_hermes.adapter import _stable_client_msg_id

    a = _stable_client_msg_id("@me", "@alice", "hello", None)
    b = _stable_client_msg_id("@me", "@bob", "hello", None)
    assert a != b


def test_different_content_different_id():
    from agentchatme_hermes.adapter import _stable_client_msg_id

    a = _stable_client_msg_id("@me", "@alice", "hello", None)
    b = _stable_client_msg_id("@me", "@alice", "world", None)
    assert a != b


def test_different_reply_to_different_id():
    from agentchatme_hermes.adapter import _stable_client_msg_id

    a = _stable_client_msg_id("@me", "@alice", "hello", "msg_1")
    b = _stable_client_msg_id("@me", "@alice", "hello", "msg_2")
    assert a != b


def test_different_sender_different_id():
    from agentchatme_hermes.adapter import _stable_client_msg_id

    a = _stable_client_msg_id("@me", "@alice", "hello", None)
    b = _stable_client_msg_id("@you", "@alice", "hello", None)
    assert a != b


def test_id_changes_after_window_expires():
    """Two calls 130s apart → different ids (legitimate re-send)."""
    from agentchatme_hermes.adapter import _stable_client_msg_id

    fixed_now = 1_700_000_000.0
    with patch("agentchatme_hermes.adapter.time.time", return_value=fixed_now):
        a = _stable_client_msg_id("@me", "@alice", "hi", None)
    # Jump well past the 120s window (130s).
    with patch("agentchatme_hermes.adapter.time.time", return_value=fixed_now + 130):
        b = _stable_client_msg_id("@me", "@alice", "hi", None)
    assert a != b


def test_id_stable_across_short_retry_intervals():
    """Three calls within 5s → same id (Hermes retry ladder is 1s/2s/4s)."""
    from agentchatme_hermes.adapter import _stable_client_msg_id

    fixed_now = 1_700_000_000.0  # 120s-aligned bucket
    ids = []
    for offset in (0, 1, 3, 7):
        with patch("agentchatme_hermes.adapter.time.time", return_value=fixed_now + offset):
            ids.append(_stable_client_msg_id("@me", "@alice", "retry-me", "msg_99"))
    # All four must land in the same bucket. Note: 7s offset still in same
    # 120s window because fixed_now is bucket-aligned.
    assert len(set(ids)) == 1


def test_id_is_valid_uuid_string():
    """SDK accepts canonical 36-char hex-dash UUID strings."""
    import uuid

    from agentchatme_hermes.adapter import _stable_client_msg_id

    raw = _stable_client_msg_id("@me", "@alice", "x", None)
    parsed = uuid.UUID(raw)
    # UUIDv5 is name-based; the version field encodes 5.
    assert parsed.version == 5


def test_long_content_does_not_explode_id_length():
    """Hashing keeps id at 36 chars regardless of payload size."""
    from agentchatme_hermes.adapter import _stable_client_msg_id

    short = _stable_client_msg_id("@me", "@alice", "x", None)
    long = _stable_client_msg_id("@me", "@alice", "x" * 50_000, None)
    assert len(short) == 36
    assert len(long) == 36
    assert short != long
