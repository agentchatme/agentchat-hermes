"""Tests for ``agentchatme_hermes.hosted_tools`` — is Hermes ready to give a
turn the hosted AgentChat tools?

* outside Hermes (no ``tools.registry``) the answer is *unknown*: no hold;
* the toolset ``mcp-agentchat`` is asked first, then every registered name
  with the ``mcp__agentchat__`` prefix;
* a registry that raises counts as "nothing registered", never a crash;
* ``wait_for_hosted_tools`` polls up to the bound, logs when it starts
  waiting and when it ends, returns ``False`` past the bound.
"""
from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest

from agentchatme_hermes import hosted_tools


def _fake_registry(monkeypatch: pytest.MonkeyPatch, *, toolset: list[str] | None = None, all_names: list[str] | None = None, raise_on: str | None = None) -> None:
    class Registry:
        def get_tool_names_for_toolset(self, name: str) -> list[str]:
            if raise_on == "toolset":
                raise RuntimeError("scope not set")
            return list(toolset or []) if name == hosted_tools.HOSTED_TOOLSET else []

        def get_all_tool_names(self) -> list[str]:
            if raise_on == "all":
                raise RuntimeError("boom")
            return list(all_names or [])

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # type: ignore[attr-defined]
    module = types.ModuleType("tools.registry")
    module.registry = Registry()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.registry", module)


class TestProbe:
    def test_outside_hermes_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "tools.registry", None)
        assert hosted_tools.hosted_tool_names() is None
        assert hosted_tools.hosted_tools_ready() is None

    def test_toolset_names_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_registry(
            monkeypatch,
            toolset=["mcp__agentchat__agentchat_send_message", "mcp__agentchat__agentchat_inbox"],
            all_names=["terminal"],
        )
        assert hosted_tools.hosted_tool_names() == [
            "mcp__agentchat__agentchat_inbox",
            "mcp__agentchat__agentchat_send_message",
        ]
        assert hosted_tools.hosted_tools_ready() is True

    def test_prefix_fallback_over_all_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_registry(monkeypatch, toolset=[], all_names=["terminal", "mcp__agentchat__agentchat_decide", "mcp__other__x"])
        assert hosted_tools.hosted_tool_names() == ["mcp__agentchat__agentchat_decide"]

    def test_hermes_without_the_tools_is_not_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_registry(monkeypatch, toolset=[], all_names=["terminal", "tool_search"])
        assert hosted_tools.hosted_tool_names() == []
        assert hosted_tools.hosted_tools_ready() is False

    @pytest.mark.parametrize("raise_on", ["toolset", "all"])
    def test_registry_raising_means_nothing_registered(self, monkeypatch: pytest.MonkeyPatch, raise_on: str) -> None:
        _fake_registry(monkeypatch, toolset=[], all_names=[], raise_on=raise_on)
        assert hosted_tools.hosted_tool_names() == []


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class TestWait:
    def test_ready_at_once_does_not_wait(self) -> None:
        clock = _Clock()
        assert hosted_tools.wait_for_hosted_tools(probe=lambda: ["mcp__agentchat__agentchat_inbox"], sleep=clock.sleep, now=clock.now)
        assert clock.sleeps == []

    def test_unknown_does_not_hold(self) -> None:
        clock = _Clock()
        assert hosted_tools.wait_for_hosted_tools(probe=lambda: None, sleep=clock.sleep, now=clock.now)
        assert clock.sleeps == []

    def test_holds_until_registered_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = _Clock()
        answers: list[list[str]] = [[], [], ["mcp__agentchat__agentchat_inbox"]]

        def probe() -> list[str]:
            return answers.pop(0) if answers else ["mcp__agentchat__agentchat_inbox"]

        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.hosted_tools"):
            ok = hosted_tools.wait_for_hosted_tools(timeout_s=30.0, poll_s=0.5, probe=probe, sleep=clock.sleep, now=clock.now, label="turn t1")
        assert ok is True
        assert clock.sleeps == [0.5, 0.5]
        messages = [r.getMessage() for r in caplog.records]
        assert any("holding the turn t1 up to 30s" in m for m in messages)
        assert any("registered after 1.0s" in m for m in messages)

    def test_past_the_bound_is_not_started(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = _Clock()
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.hosted_tools"):
            ok = hosted_tools.wait_for_hosted_tools(timeout_s=2.0, poll_s=0.5, probe=lambda: [], sleep=clock.sleep, now=clock.now)
        assert ok is False
        assert sum(clock.sleeps) == pytest.approx(2.0)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "not started" in warnings[0].getMessage()
        assert "hermes agentchat doctor" in warnings[0].getMessage()

    def test_default_bound_is_thirty_seconds(self) -> None:
        assert hosted_tools.HOSTED_TOOLS_WAIT_S == 30.0


def test_module_level_constants_match_turn_spelling() -> None:
    from agentchatme_hermes.turn import mcp_tool_name

    assert mcp_tool_name("agentchat_inbox").startswith(hosted_tools.HOSTED_TOOL_NAME_PREFIX)
    assert hosted_tools.HOSTED_TOOLSET == "mcp-agentchat"


def _unused(_: Any) -> None:  # keeps the Any import honest for mypy on 3.9
    return None
