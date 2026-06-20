"""Deterministic per-conversation reply circuit breaker — the seatbelt
under the LLM reply gate.

The gate (:mod:`agentchatme_hermes.reply_gate`) is the smart layer: it reads
the conversation and decides reply / no-reply. This module is the dumb layer
underneath it. It counts, per conversation, how many replies this agent has
sent inside a rolling time window, and forces no-reply once that count
crosses a cap — regardless of what the gate decided, or whether the gate
ran at all.

Why both: an LLM decision can be wrong, slow, or unavailable. A pure counter
can't reason about whether a conversation is "done", but it CAN guarantee two
agents never trade messages forever. Defence in depth — the gate keeps
behaviour natural; the breaker keeps it bounded.

The breaker is intentionally generous (see :mod:`.config` defaults): it is a
last resort, not the primary control. In a healthy exchange the gate stops
the agent long before the breaker would.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional


class TurnCircuitBreaker:
    """Thread-safe rolling-window count of this agent's replies per conversation.

    One instance is owned by the :class:`~agentchatme_hermes.agent_invoker.AgentInvoker`
    and shared across all turn-worker threads, so it must be safe under
    concurrent access from different conversations running in parallel. A
    single lock guards the backing map; the per-conversation deques are
    never handed out.

    The clock is injectable (``time_fn``) so tests advance time
    deterministically instead of sleeping. Production uses
    :func:`time.monotonic` — monotonic, not wall-clock, so a system clock
    adjustment can't make the window misbehave.
    """

    def __init__(
        self,
        *,
        max_replies: int,
        window_seconds: float,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_replies < 1:
            raise ValueError("max_replies must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_replies = max_replies
        self._window_seconds = window_seconds
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._events: Dict[str, Deque[float]] = {}

    @property
    def max_replies(self) -> int:
        return self._max_replies

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    def recent_count(
        self, conversation_id: str, *, now: Optional[float] = None
    ) -> int:
        """Replies sent into ``conversation_id`` within the rolling window.

        Also fed to the LLM gate as a loop signal ("you've already sent N
        replies here recently"), so the smart layer can see the same
        pressure the breaker is counting.
        """
        ts = self._time_fn() if now is None else now
        with self._lock:
            return self._pruned_count(conversation_id, ts)

    def should_trip(
        self, conversation_id: str, *, now: Optional[float] = None
    ) -> bool:
        """``True`` once the cap is reached — the caller must force no-reply.

        At ``max_replies`` replies in the window the next one is blocked, so
        the agent sends at most ``max_replies`` into one conversation per
        window before the breaker holds it.
        """
        ts = self._time_fn() if now is None else now
        with self._lock:
            return self._pruned_count(conversation_id, ts) >= self._max_replies

    def record_reply(
        self, conversation_id: str, *, now: Optional[float] = None
    ) -> None:
        """Record that the agent just replied into ``conversation_id``."""
        ts = self._time_fn() if now is None else now
        with self._lock:
            dq = self._events.get(conversation_id)
            if dq is None:
                dq = deque()
                self._events[conversation_id] = dq
            dq.append(ts)
            self._prune(conversation_id, dq, ts)

    # -- internals (must be called while holding ``self._lock``) -----------

    def _pruned_count(self, conversation_id: str, now: float) -> int:
        dq = self._events.get(conversation_id)
        if dq is None:
            return 0
        self._prune(conversation_id, dq, now)
        return len(dq)

    def _prune(self, conversation_id: str, dq: Deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if not dq:
            # Drop the empty deque so an agent that talks to thousands of
            # peers over its lifetime doesn't accumulate dead keys.
            self._events.pop(conversation_id, None)
