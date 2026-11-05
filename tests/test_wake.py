"""Tests for ``agentchatme_hermes.wake`` — debounce, retry wakes, stop."""
from __future__ import annotations

import threading
import time

from agentchatme_hermes.wake import WakeCoalescer


def _wait_for(predicate: object, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.005)
    return False


class TestDebounce:
    def test_single_wake_fires_once_after_the_window(self) -> None:
        fired = threading.Event()
        coalescer = WakeCoalescer(on_fire=fired.set, debounce_s=0.02)
        coalescer.wake()
        assert coalescer.armed
        assert fired.wait(1.0)
        assert _wait_for(lambda: not coalescer.armed)
        assert coalescer.fires == 1
        coalescer.stop()

    def test_burst_inside_the_window_is_one_fire(self) -> None:
        fires: list[float] = []
        # A window long enough that 25 synchronous wakes land inside it even
        # on a slow CI runner; the assertion is about coalescing, not speed.
        coalescer = WakeCoalescer(on_fire=lambda: fires.append(time.monotonic()), debounce_s=0.3)
        for _ in range(25):
            coalescer.wake()
        assert coalescer.wakes == 25
        assert coalescer.absorbed == 24
        assert _wait_for(lambda: len(fires) == 1)
        time.sleep(0.1)  # nothing else may fire
        assert len(fires) == 1
        coalescer.stop()

    def test_wake_after_a_fire_arms_again(self) -> None:
        fires: list[int] = []
        coalescer = WakeCoalescer(on_fire=lambda: fires.append(1), debounce_s=0.01)
        coalescer.wake()
        assert _wait_for(lambda: len(fires) == 1)
        coalescer.wake()
        assert _wait_for(lambda: len(fires) == 2)
        coalescer.stop()

    def test_zero_debounce_is_allowed(self) -> None:
        fired = threading.Event()
        coalescer = WakeCoalescer(on_fire=fired.set, debounce_s=0.0)
        coalescer.wake()
        assert fired.wait(1.0)
        coalescer.stop()

    def test_negative_debounce_rejected(self) -> None:
        try:
            WakeCoalescer(on_fire=lambda: None, debounce_s=-1.0)
        except ValueError:
            return
        raise AssertionError("negative debounce must be rejected")

    def test_callback_exception_does_not_break_the_next_wake(self) -> None:
        calls: list[int] = []

        def flaky() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")

        coalescer = WakeCoalescer(on_fire=flaky, debounce_s=0.01)
        coalescer.wake()
        assert _wait_for(lambda: len(calls) == 1)
        coalescer.wake()
        assert _wait_for(lambda: len(calls) == 2)
        coalescer.stop()


class TestRetryWake:
    def test_wake_after_delays_then_wakes(self) -> None:
        fired = threading.Event()
        coalescer = WakeCoalescer(on_fire=fired.set, debounce_s=0.0)
        started = time.monotonic()
        coalescer.wake_after(0.05)
        assert fired.wait(1.0)
        assert time.monotonic() - started >= 0.04
        coalescer.stop()

    def test_sooner_wake_after_replaces_a_later_one(self) -> None:
        fires: list[int] = []
        coalescer = WakeCoalescer(on_fire=lambda: fires.append(1), debounce_s=0.0)
        coalescer.wake_after(5.0)
        coalescer.wake_after(0.02)
        assert _wait_for(lambda: len(fires) == 1)
        time.sleep(0.1)
        assert len(fires) == 1, "the replaced delayed wake must not fire too"
        coalescer.stop()

    def test_later_wake_after_never_pushes_a_sooner_one_back(self) -> None:
        # Two fail-closed cycles inside one outage: the 60 s retry must not be
        # replaced by the 300 s one, or recovery waits five minutes for nothing.
        fires: list[int] = []
        coalescer = WakeCoalescer(on_fire=lambda: fires.append(1), debounce_s=0.0)
        coalescer.wake_after(0.03)
        assert coalescer.delayed_pending
        coalescer.wake_after(5.0)
        assert _wait_for(lambda: len(fires) == 1, timeout=1.0)
        assert not coalescer.delayed_pending
        coalescer.stop()


class TestStop:
    def test_stop_cancels_pending_and_ignores_later_wakes(self) -> None:
        fires: list[int] = []
        coalescer = WakeCoalescer(on_fire=lambda: fires.append(1), debounce_s=0.05)
        coalescer.wake()
        coalescer.wake_after(0.05)
        coalescer.stop()
        coalescer.wake()
        time.sleep(0.15)
        assert fires == []
        assert not coalescer.armed
