"""Tests for ``agentchatme_hermes.message_queue``."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from agentchatme_hermes.message_queue import (
    DEFAULT_CONVERSATION_CAP,
    DEFAULT_PER_CONVERSATION_CAP,
    MessageQueue,
)
from agentchatme_hermes.types import InboundEvent


def _event(
    msg_id: str = "msg_1",
    conv_id: str = "conv_dm_a",
    sender: str = "alice",
    text: str = "hello",
) -> InboundEvent:
    return InboundEvent(
        message_id=msg_id,
        conversation_id=conv_id,
        conversation_kind="direct",
        sender_handle=sender,
        content_text=text,
        received_at=datetime.now(timezone.utc),
    )


class TestPushPop:
    def test_empty_pop_returns_none(self) -> None:
        q = MessageQueue()
        assert q.pop() is None
        assert q.pending_count() == 0

    def test_push_then_pop(self) -> None:
        q = MessageQueue()
        e = _event()
        q.push(e)
        assert q.pending_count() == 1
        popped = q.pop()
        assert popped is e
        assert q.pop() is None
        assert q.pending_count() == 0

    def test_fifo_across_conversations(self) -> None:
        q = MessageQueue()
        e1 = _event(msg_id="m1", conv_id="conv_a")
        e2 = _event(msg_id="m2", conv_id="conv_b")
        q.push(e1)
        q.push(e2)
        assert q.pop() is e1
        assert q.pop() is e2

    def test_pop_returns_newest_in_conversation(self) -> None:
        # Multiple events on the same conversation while one is "in flight"
        # are not separately popped — the latest event is what surfaces.
        q = MessageQueue()
        e1 = _event(msg_id="m1", text="first")
        e2 = _event(msg_id="m2", text="second")
        q.push(e1)
        q.push(e2)
        popped = q.pop()
        assert popped is e2  # newest, not e1
        # No more pending — same conversation, single pending slot
        assert q.pop() is None

    def test_history_preserves_both_events(self) -> None:
        q = MessageQueue()
        e1 = _event(msg_id="m1", text="first")
        e2 = _event(msg_id="m2", text="second")
        q.push(e1)
        q.push(e2)
        q.pop()
        # Both events still readable from history
        history = q.history_for(e1.conversation_id)
        assert [e.message_id for e in history] == ["m1", "m2"]


class TestIdempotency:
    def test_duplicate_message_id_is_dropped(self) -> None:
        q = MessageQueue()
        q.push(_event(msg_id="m1", text="original"))
        # Same message_id, different text (e.g., reconnect replay) —
        # should be a no-op
        q.push(_event(msg_id="m1", text="replayed"))
        history = q.history_for("conv_dm_a")
        assert len(history) == 1
        assert history[0].content_text == "original"


class TestEviction:
    def test_per_conversation_ring_bounded(self) -> None:
        q = MessageQueue(per_conversation_cap=3)
        for i in range(5):
            q.push(_event(msg_id=f"m{i}", text=f"msg-{i}"))
        history = q.history_for("conv_dm_a")
        assert len(history) == 3
        # Oldest two evicted
        assert [e.message_id for e in history] == ["m2", "m3", "m4"]

    def test_conversation_cap_lru_eviction(self) -> None:
        q = MessageQueue(conversation_cap=2)
        q.push(_event(msg_id="m1", conv_id="conv_a"))
        q.push(_event(msg_id="m2", conv_id="conv_b"))
        q.push(_event(msg_id="m3", conv_id="conv_c"))
        # conv_a was the oldest-touched and should be gone
        assert q.history_for("conv_a") == []
        assert len(q.history_for("conv_b")) == 1
        assert len(q.history_for("conv_c")) == 1

    def test_touching_a_conversation_resets_lru_position(self) -> None:
        q = MessageQueue(conversation_cap=2)
        q.push(_event(msg_id="m1", conv_id="conv_a"))
        q.push(_event(msg_id="m2", conv_id="conv_b"))
        # Re-touch conv_a so it's no longer the oldest
        q.push(_event(msg_id="m3", conv_id="conv_a"))
        # Now push a 3rd — conv_b should evict, conv_a survives
        q.push(_event(msg_id="m4", conv_id="conv_c"))
        assert q.history_for("conv_b") == []
        assert len(q.history_for("conv_a")) == 2
        assert len(q.history_for("conv_c")) == 1


class TestConfiguration:
    def test_per_conv_cap_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            MessageQueue(per_conversation_cap=0)

    def test_conversation_cap_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            MessageQueue(conversation_cap=0)

    def test_default_caps_are_sane(self) -> None:
        q = MessageQueue()
        assert q._per_conversation_cap == DEFAULT_PER_CONVERSATION_CAP
        assert q._conversation_cap == DEFAULT_CONVERSATION_CAP


class TestConcurrency:
    def test_concurrent_pushes_dont_lose_events(self) -> None:
        q = MessageQueue()
        n_threads = 8
        events_per_thread = 50

        def producer(thread_idx: int) -> None:
            for i in range(events_per_thread):
                q.push(
                    _event(
                        msg_id=f"t{thread_idx}-m{i}",
                        conv_id=f"conv_t{thread_idx}",
                        text=str(i),
                    )
                )

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread's conversation should have all its events
        for i in range(n_threads):
            history = q.history_for(f"conv_t{i}")
            assert len(history) == events_per_thread, f"thread {i} lost events"
            assert len({e.message_id for e in history}) == events_per_thread
