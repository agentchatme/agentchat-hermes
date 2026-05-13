"""Per-conversation inbound queue.

Lives between the WS daemon (producer, in the background asyncio
thread) and the agent invoker (consumer, on a worker thread per turn).
Both ends are non-async; the data structure itself is plain Python
with a single :class:`threading.Lock` guarding mutation.

What it isn't:

* Not an asyncio queue — the consumer is a synchronous worker.
* Not a persistent store — backlog beyond an agent process restart
  is the server's responsibility (``GET /v1/messages/sync`` drains
  anything missed; the WS daemon issues that drain on reconnect).
* Not a fan-out / pub-sub. One consumer.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from collections import OrderedDict, deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import InboundEvent

logger = logging.getLogger(__name__)

# Per-conversation history kept warm so the agent can scroll up via
# the conversation context without paying an HTTP round-trip for
# every turn. Bounded — older entries fall off LRU-style. Set high
# enough to cover a normal back-and-forth without paying for active
# fetches; small enough that a runaway group doesn't drain memory.
DEFAULT_PER_CONVERSATION_CAP = 100

# Cap on the total number of conversations we keep recent-history
# state for. Beyond this, oldest-touched conversation falls off and
# its buffer is dropped. Recovery is automatic (a new inbound on the
# evicted conversation re-creates the buffer).
DEFAULT_CONVERSATION_CAP = 256


class MessageQueue:
    """Thread-safe inbox keyed by ``conversation_id``.

    The queue is a *signal* of new inbound that needs agent attention,
    plus a small ring of recent context per conversation. The agent
    invoker pops one event at a time via :meth:`pop` and decides
    whether to wake the agent.
    """

    def __init__(
        self,
        *,
        per_conversation_cap: int = DEFAULT_PER_CONVERSATION_CAP,
        conversation_cap: int = DEFAULT_CONVERSATION_CAP,
    ) -> None:
        if per_conversation_cap < 1:
            raise ValueError("per_conversation_cap must be >= 1")
        if conversation_cap < 1:
            raise ValueError("conversation_cap must be >= 1")

        self._per_conversation_cap = per_conversation_cap
        self._conversation_cap = conversation_cap

        self._lock = threading.Lock()
        # Ring of recent events per conversation. OrderedDict gives
        # LRU eviction in O(1) via move_to_end + popitem(last=False).
        self._history: OrderedDict[str, deque[InboundEvent]] = OrderedDict()
        # FIFO of conversations with at least one un-consumed event.
        # Separate from _history because the invoker wants "what to
        # process next" without scanning every buffer.
        self._pending: deque[str] = deque()
        self._pending_set: set[str] = set()

    def push(self, event: InboundEvent) -> None:
        """Record an inbound event. O(1) under the lock.

        Idempotent on ``message_id`` — duplicate frames (e.g., reconnect
        replay of the same message) are dropped silently. Without this,
        the WS drain on reconnect would wake the agent twice per
        message.
        """
        with self._lock:
            buf = self._history.get(event.conversation_id)
            if buf is None:
                if len(self._history) >= self._conversation_cap:
                    evicted_id, _ = self._history.popitem(last=False)
                    if evicted_id in self._pending_set:
                        self._pending_set.discard(evicted_id)
                        with contextlib.suppress(ValueError):
                            self._pending.remove(evicted_id)
                    logger.debug(
                        "MessageQueue: evicted history for conversation %s "
                        "(cap=%d)",
                        evicted_id,
                        self._conversation_cap,
                    )
                buf = deque(maxlen=self._per_conversation_cap)
                self._history[event.conversation_id] = buf
            else:
                self._history.move_to_end(event.conversation_id)

            if any(e.message_id == event.message_id for e in buf):
                logger.debug(
                    "MessageQueue: dropping duplicate message_id=%s",
                    event.message_id,
                )
                return

            buf.append(event)

            if event.conversation_id not in self._pending_set:
                self._pending_set.add(event.conversation_id)
                self._pending.append(event.conversation_id)

    def pop(self) -> InboundEvent | None:
        """Return the oldest un-consumed event, or ``None`` if empty.

        Pulled by the invoker's dispatch loop. The history ring still
        retains the popped event so the agent can scroll back.
        """
        with self._lock:
            while self._pending:
                conv_id = self._pending.popleft()
                self._pending_set.discard(conv_id)
                buf = self._history.get(conv_id)
                if not buf:
                    continue
                # The newest event in the buffer is the one that flipped
                # this conversation into pending. We hand THAT to the
                # invoker — older events are already in the ring as
                # context, not as "new attention required" signals.
                event = buf[-1]
                self._history.move_to_end(conv_id)
                return event
            return None

    def history_for(self, conversation_id: str) -> list[InboundEvent]:
        """Return a snapshot of recent events for one conversation.

        Newest last, oldest first. Empty list if we have no buffer
        (either never seen this conversation or it was LRU-evicted).
        """
        with self._lock:
            buf = self._history.get(conversation_id)
            if buf is None:
                return []
            return list(buf)

    def pending_count(self) -> int:
        """Diagnostic: how many conversations have un-consumed events."""
        with self._lock:
            return len(self._pending)
