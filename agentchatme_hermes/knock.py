"""Knock coalescing — one gate cycle per burst, not per frame.

A ``message.new`` frame is a knock: a hint that the tray changed, never the
carrier of what changed (the server's rule 2). A group burst arrives as many
frames within a second; the server already bundles its *wakes* for such
bursts, and the doorbell does the same for the socket: the first knock arms
a short timer, every knock inside the window is absorbed, and the timer
fires exactly once. Whoever fires then reads the whole tray, so nothing a
later frame announced is missed — the tray is the truth, the knock is not.

Also offers :meth:`knock_after` for the local retry ladder: when a gate cycle
could not complete (model unreachable, server error) the doorbell schedules
another knock later instead of dropping the tray. The server's own ladder
only rings webhooks; a connected socket gets no re-ring, so the doorbell
carries that duty itself.

Thread-safe; callbacks run on a timer thread and must return quickly (the
invoker's callback sets an event and returns).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_S = 2.0


class KnockCoalescer:
    """Debounce knocks into single fires.

    * :meth:`knock` — arm the timer if none is armed; otherwise absorb.
    * :meth:`knock_after` — a delayed knock (retry ladder). Replaces any
      earlier delayed knock still pending; an immediate knock in between
      still fires on its own schedule.
    * :meth:`stop` — cancel everything; later knocks are ignored.
    """

    def __init__(
        self,
        *,
        on_fire: Callable[[], None],
        debounce_s: float = DEFAULT_DEBOUNCE_S,
    ) -> None:
        if debounce_s < 0:
            raise ValueError("debounce_s must be >= 0")
        self._on_fire = on_fire
        self._debounce_s = debounce_s
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._delayed: threading.Timer | None = None
        self._stopped = False
        self.knocks = 0
        self.absorbed = 0
        self.fires = 0

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._timer is not None

    def knock(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self.knocks += 1
            if self._timer is not None:
                self.absorbed += 1
                return
            timer = threading.Timer(self._debounce_s, self._fire)
            timer.name = "agentchat-knock-debounce"
            timer.daemon = True
            self._timer = timer
        timer.start()

    def knock_after(self, delay_s: float) -> None:
        with self._lock:
            if self._stopped:
                return
            if self._delayed is not None:
                self._delayed.cancel()
            timer = threading.Timer(max(0.0, delay_s), self._delayed_knock)
            timer.name = "agentchat-knock-retry"
            timer.daemon = True
            self._delayed = timer
        timer.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            timers = [t for t in (self._timer, self._delayed) if t is not None]
            self._timer = None
            self._delayed = None
        for timer in timers:
            timer.cancel()

    # -- timer callbacks ----------------------------------------------------

    def _delayed_knock(self) -> None:
        with self._lock:
            self._delayed = None
        self.knock()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            if self._stopped:
                return
            self.fires += 1
        try:
            self._on_fire()
        except Exception:
            # The callback belongs to the invoker; a failure there must not
            # kill the timer machinery. The next knock re-arms as usual.
            logger.exception("KnockCoalescer: on_fire callback raised")
