"""Wake coalescing — one gate cycle per burst, not per frame.

A ``message.new`` frame, a ``wake`` frame and a (re)connect are all wakes: a
hint that the tray may have changed, never the carrier of what changed. A
group burst arrives as many frames within a second; the server already
bundles its webhook wakes for such bursts, and the integration does the
same for the socket: the first wake arms a short timer, every wake inside
the window is absorbed, and the timer fires exactly once. Whoever fires then
reads the whole tray, so nothing a later frame announced is missed — the
tray is the truth, the wake is not.

There is no delayed wake: a cycle that could not complete leaves the cards
on the tray, and the server re-wakes this socket while they stay unanswered
(contract v1 D3, the ``rewake`` capability).

Thread-safe; callbacks run on a timer thread and must return quickly (the
invoker's callback sets an event and returns).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_S = 2.0


class WakeCoalescer:
    """Debounce wakes into single fires.

    * :meth:`wake` — arm the timer if none is armed; otherwise absorb.
    * :meth:`stop` — cancel everything; later wakes are ignored.
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
        self._stopped = False
        self.wakes = 0
        self.absorbed = 0
        self.fires = 0

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._timer is not None

    def wake(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self.wakes += 1
            if self._timer is not None:
                self.absorbed += 1
                return
            timer = threading.Timer(self._debounce_s, self._fire)
            timer.name = "agentchat-wake-debounce"
            timer.daemon = True
            self._timer = timer
        timer.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    # -- timer callbacks ----------------------------------------------------

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
            # kill the timer machinery. The next wake re-arms as usual.
            logger.exception("WakeCoalescer: on_fire callback raised")
