"""Tests for the deterministic reply circuit breaker.

The clock is injected so the rolling window is exercised without sleeping —
every time-dependent assertion advances a fake clock explicitly.
"""
from __future__ import annotations

import pytest

from agentchatme_hermes.turn_guard import TurnCircuitBreaker


class _Clock:
    """Manually-advanced monotonic stand-in."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class TestConstruction:
    def test_rejects_zero_max_replies(self) -> None:
        with pytest.raises(ValueError, match="max_replies"):
            TurnCircuitBreaker(max_replies=0, window_seconds=60)

    def test_rejects_nonpositive_window(self) -> None:
        with pytest.raises(ValueError, match="window_seconds"):
            TurnCircuitBreaker(max_replies=1, window_seconds=0)

    def test_exposes_settings(self) -> None:
        b = TurnCircuitBreaker(max_replies=5, window_seconds=90)
        assert b.max_replies == 5
        assert b.window_seconds == 90


class TestCounting:
    def test_empty_conversation_counts_zero(self) -> None:
        b = TurnCircuitBreaker(max_replies=3, window_seconds=60, time_fn=_Clock())
        assert b.recent_count("c") == 0
        assert b.should_trip("c") is False

    def test_records_accumulate(self) -> None:
        b = TurnCircuitBreaker(max_replies=3, window_seconds=60, time_fn=_Clock())
        b.record_reply("c")
        b.record_reply("c")
        assert b.recent_count("c") == 2
        assert b.should_trip("c") is False

    def test_trips_at_cap(self) -> None:
        b = TurnCircuitBreaker(max_replies=3, window_seconds=60, time_fn=_Clock())
        for _ in range(3):
            b.record_reply("c")
        assert b.recent_count("c") == 3
        assert b.should_trip("c") is True

    def test_conversations_are_isolated(self) -> None:
        b = TurnCircuitBreaker(max_replies=2, window_seconds=60, time_fn=_Clock())
        b.record_reply("c1")
        b.record_reply("c1")
        assert b.should_trip("c1") is True
        assert b.recent_count("c2") == 0
        assert b.should_trip("c2") is False


class TestWindow:
    def test_old_events_prune_out(self) -> None:
        clk = _Clock()
        b = TurnCircuitBreaker(max_replies=3, window_seconds=60, time_fn=clk)
        b.record_reply("c")
        b.record_reply("c")
        assert b.recent_count("c") == 2
        clk.advance(61)
        assert b.recent_count("c") == 0
        assert b.should_trip("c") is False

    def test_partial_window_keeps_recent(self) -> None:
        clk = _Clock()
        b = TurnCircuitBreaker(max_replies=5, window_seconds=60, time_fn=clk)
        b.record_reply("c")  # t=1000
        clk.advance(30)
        b.record_reply("c")  # t=1030
        clk.advance(31)  # t=1061 → cutoff 1001; the 1000 event drops, 1030 stays
        assert b.recent_count("c") == 1

    def test_trip_clears_after_window(self) -> None:
        clk = _Clock()
        b = TurnCircuitBreaker(max_replies=2, window_seconds=60, time_fn=clk)
        b.record_reply("c")
        b.record_reply("c")
        assert b.should_trip("c") is True
        clk.advance(61)
        assert b.should_trip("c") is False
        # and the breaker is usable again afterwards
        b.record_reply("c")
        assert b.recent_count("c") == 1

    def test_explicit_now_argument(self) -> None:
        # Default time_fn (monotonic) is irrelevant — the caller pins `now`.
        b = TurnCircuitBreaker(max_replies=2, window_seconds=10)
        b.record_reply("c", now=100.0)
        assert b.recent_count("c", now=105.0) == 1
        assert b.recent_count("c", now=111.0) == 0  # 100 <= 111-10 → pruned
