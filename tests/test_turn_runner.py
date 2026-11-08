"""Tests for ``agentchatme_hermes.turn_runner`` — one turn under a time limit.

The agent is a small double with the parts of ``AIAgent`` the runner uses:
``run_conversation``, ``get_activity_summary`` and ``hard_interrupt`` /
``interrupt``. What must hold:

* a turn that ends is reported ``completed`` / ``failed`` with its AgentChat
  call counts, and its thread has ended;
* a turn with no activity past the idle limit, or running past the ceiling,
  is interrupted the way Hermes' own watchdogs do it (``hard_interrupt``
  with a fixed ``tool_reason``) and reported ``timeout``;
* a turn the interrupt does not stop is reported ``timeout`` with
  ``ended=False``; its ``finished`` event and end callback come only when
  its thread really ends (the invoker keeps its conversation held until
  then);
* the caller's ContextVars (the profile scope) reach the turn thread;
* ``stopped`` interrupts a running turn.
"""
from __future__ import annotations

import contextvars
import threading
import time
from typing import Any
from unittest.mock import MagicMock

from agentchatme_hermes.turn_runner import (
    INTERRUPT_STOP_REASON,
    INTERRUPT_TOOL_REASON,
    TurnLimits,
    interrupt_agent,
    run_turn,
)

FAST = TurnLimits(idle_s=0.15, max_s=5.0, grace_s=0.5, poll_s=0.01)


class _Agent:
    """``AIAgent`` as the runner sees it."""

    def __init__(self, *, result: Any = None, block: bool = False, obey: bool = True) -> None:
        self.result = result if result is not None else {"final_response": "ok", "messages": []}
        self.block = block
        self.obey = obey
        self.release = threading.Event()
        self.interrupts: list[tuple[str | None, str | None]] = []
        self.activity_at = 1000.0
        self.api_calls = 0
        self.started = threading.Event()

    def run_conversation(self, prompt: str, conversation_history: list[Any]) -> Any:
        self.started.set()
        if self.block:
            self.release.wait(10.0)
            return {"completed": False, "interrupted": True, "messages": []}
        return self.result

    def get_activity_summary(self) -> dict[str, Any]:
        return {
            "last_activity_at": self.activity_at,
            "api_call_count": self.api_calls,
            "current_tool": None,
            "last_activity_desc": "waiting for the provider",
        }

    def hard_interrupt(self, message: str | None = None, *, tool_reason: str | None = None) -> None:
        self.interrupts.append((message, tool_reason))
        if self.obey:
            self.release.set()


def test_completed_turn_reports_calls_and_has_ended() -> None:
    agent = _Agent(
        result={
            "final_response": "",
            "completed": True,
            "messages": [
                {"role": "tool", "name": "mcp__agentchat__agentchat_inbox", "content": '{"result": "{}"}'},
                {"role": "tool", "name": "mcp__agentchat__agentchat_send_message", "content": '{"error": "AWAITING_REPLY"}'},
            ],
        }
    )
    report = run_turn(agent, "go", session_id="agentchat:wake:t1", limits=FAST)
    assert report.status == "completed" and report.ended
    assert report.calls is not None and (report.calls.made, report.calls.failed) == (2, 1)
    end = report.turn_end()
    assert end.status == "completed" and end.agentchat_calls == report.calls
    assert agent.interrupts == []


def test_failed_result_and_raising_turn_are_failed() -> None:
    failed = run_turn(_Agent(result={"failed": True, "error": "quota"}), "go", session_id="s", limits=FAST)
    assert failed.status == "failed" and failed.ended and "quota" in (failed.detail or "")

    agent = MagicMock()
    agent.run_conversation.side_effect = RuntimeError("provider exploded")
    raised = run_turn(agent, "go", session_id="s", limits=FAST)
    assert raised.status == "failed" and raised.ended and "provider exploded" in (raised.detail or "")
    assert raised.calls is None, "a turn that raised has no transcript: unknown"
    assert raised.turn_end().status == "failed"


def test_idle_turn_is_interrupted_like_hermes_does_and_reported_timeout() -> None:
    agent = _Agent(block=True)
    report = run_turn(agent, "go", session_id="s", limits=FAST)
    assert report.status == "timeout" and report.ended
    assert agent.interrupts == [(f"AgentChat: {INTERRUPT_TOOL_REASON}", INTERRUPT_TOOL_REASON)]
    assert report.turn_end().status == "failed", "stopped by the interrupt: a failed turn"
    assert "no activity" in (report.detail or "") and "waiting for the provider" in (report.detail or "")


def test_activity_keeps_the_turn_alive_until_the_ceiling() -> None:
    agent = _Agent(block=True)
    stop = threading.Event()

    def busy() -> None:
        while not stop.wait(0.02):
            agent.api_calls += 1  # activity: the idle limit never fires

    threading.Thread(target=busy, daemon=True).start()
    try:
        started = time.monotonic()
        report = run_turn(
            agent, "go", session_id="s", limits=TurnLimits(idle_s=0.1, max_s=0.4, grace_s=0.5, poll_s=0.01)
        )
    finally:
        stop.set()
    assert report.status == "timeout" and "ceiling" in (report.detail or "")
    assert time.monotonic() - started >= 0.4, "not cut by the idle limit while busy"


def test_turn_that_ignores_the_interrupt_ends_later() -> None:
    agent = _Agent(block=True, obey=False)
    ended: list[str] = []
    report = run_turn(
        agent,
        "go",
        session_id="s",
        limits=TurnLimits(idle_s=0.05, max_s=5.0, grace_s=0.05, poll_s=0.01),
        on_finished=lambda: ended.append("end"),
    )
    assert report.status == "timeout" and not report.ended
    assert report.turn_end().status == "still_running", "it keeps its conversation"
    assert ended == []

    agent.release.set()  # the stuck call finally returns
    assert report.finished.wait(2.0)
    assert ended == ["end"], "the end callback comes with the real end, once"


def test_a_normal_turn_calls_the_end_callback_before_returning() -> None:
    ended: list[str] = []
    report = run_turn(_Agent(), "go", session_id="s", limits=FAST, on_finished=lambda: ended.append("end"))
    assert report.ended and ended == ["end"]


def test_profile_context_reaches_the_turn_thread() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("home", default="launch")
    seen: list[str] = []

    class _Scoped(_Agent):
        def run_conversation(self, prompt: str, conversation_history: list[Any]) -> Any:
            seen.append(var.get())
            return {"final_response": "ok"}

    token = var.set("profile-b")
    try:
        run_turn(_Scoped(), "go", session_id="s", limits=FAST)
    finally:
        var.reset(token)
    assert seen == ["profile-b"]


def test_stop_interrupts_a_running_turn() -> None:
    agent = _Agent(block=True)
    stopped = threading.Event()
    threading.Timer(0.05, stopped.set).start()
    report = run_turn(
        agent, "go", session_id="s", limits=TurnLimits(idle_s=None, max_s=None, grace_s=0.5, poll_s=0.01), stopped=stopped
    )
    assert report.status == "timeout" and report.detail == "stopping"
    assert agent.interrupts == [(f"AgentChat: {INTERRUPT_STOP_REASON}", INTERRUPT_STOP_REASON)]


def test_no_limits_waits_for_the_end() -> None:
    agent = _Agent(block=True)
    threading.Timer(0.1, agent.release.set).start()
    report = run_turn(agent, "go", session_id="s", limits=TurnLimits(idle_s=None, max_s=None, poll_s=0.01))
    assert report.status == "failed", "the stub returns interrupted=True once released"
    assert agent.interrupts == []


def test_poll_callback_beats_while_the_turn_runs() -> None:
    agent = _Agent(block=True)
    beats: list[float] = []
    threading.Timer(0.1, agent.release.set).start()
    run_turn(agent, "go", session_id="s", limits=TurnLimits(idle_s=None, max_s=None, poll_s=0.01), on_poll=lambda: beats.append(1.0))
    assert len(beats) >= 3


def test_interrupt_agent_shapes() -> None:
    class _Legacy:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def interrupt(self, message: str | None = None) -> None:
            self.calls.append(message)

    legacy = _Legacy()
    assert interrupt_agent(legacy, "why") is True
    assert legacy.calls == ["AgentChat: why"], "no tool_reason for an interrupt() that does not take it"

    # A dynamic proxy is not taken as having the method (Hermes' own rule).
    assert interrupt_agent(MagicMock(), "why") is False

    class _Raises:
        def hard_interrupt(self, message: str | None = None, *, tool_reason: str | None = None) -> None:
            raise RuntimeError("boom")

        def interrupt(self, message: str | None = None) -> None:
            self.used = True

    raises = _Raises()
    assert interrupt_agent(raises, "why") is True and raises.used, "falls back to interrupt()"
