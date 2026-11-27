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
    def test_registry_raising_is_unknown_not_a_hold(self, monkeypatch: pytest.MonkeyPatch, raise_on: str, caplog: pytest.LogCaptureFixture) -> None:
        """Registry drift must not hold and ladder every cycle: unknown, warned once."""
        _fake_registry(monkeypatch, toolset=[], all_names=[], raise_on=raise_on)
        hosted_tools._warned.clear()
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.hosted_tools"):
            assert hosted_tools.hosted_tool_names() is None
            assert hosted_tools.hosted_tool_names() is None
        assert len([r for r in caplog.records if "registry probe raised" in r.getMessage()]) == 1

    def test_registry_import_error_other_than_importerror_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import types

        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
        monkeypatch.setitem(sys.modules, "tools.registry", None)  # import raises ImportError
        assert hosted_tools.hosted_tool_names() is None

    def test_stray_registry_without_hermes_api_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import types

        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []  # type: ignore[attr-defined]
        module = types.ModuleType("tools.registry")
        module.registry = object()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
        monkeypatch.setitem(sys.modules, "tools.registry", module)
        hosted_tools._warned.clear()
        assert hosted_tools.hosted_tool_names() is None


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
            ok = hosted_tools.wait_for_hosted_tools(
                timeout_s=2.0, poll_s=0.5, probe=lambda: [], sleep=clock.sleep, now=clock.now, state_probe=lambda: None
            )
        assert ok is False
        assert sum(clock.sleeps) == pytest.approx(2.0)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "not started" in warnings[0].getMessage()
        assert "hermes agentchat doctor" in warnings[0].getMessage()

    def test_stopped_ends_the_hold(self, caplog: pytest.LogCaptureFixture) -> None:
        import threading

        stopped = threading.Event()
        clock = _Clock()
        polls = {"n": 0}

        def probe() -> list[str]:
            polls["n"] += 1
            if polls["n"] == 2:
                stopped.set()  # the stop lands between two polls
            return []

        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.hosted_tools"):
            result = hosted_tools.wait_for_hosted_tools(timeout_s=30.0, poll_s=0.5, probe=probe, sleep=clock.sleep, now=clock.now, stopped=stopped)
        assert result is None
        assert clock.sleeps == [0.5, 0.5], "the stop landed between polls; the next poll wait ended the hold"
        assert any("stopping while holding" in r.getMessage() for r in caplog.records)

    def test_already_stopped_does_not_probe(self) -> None:
        import threading

        stopped = threading.Event()
        stopped.set()
        probed: list[int] = []
        assert hosted_tools.wait_for_hosted_tools(probe=lambda: probed.append(1) or [], stopped=stopped) is None
        assert probed == []

    def test_poll_waits_on_the_stop_event_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import threading

        stopped = threading.Event()
        waits: list[float] = []
        monkeypatch.setattr(stopped, "wait", lambda s: waits.append(s) or False)
        answers: list[list[str]] = [[], ["mcp__agentchat__agentchat_inbox"]]
        hosted_tools.wait_for_hosted_tools(poll_s=0.25, probe=lambda: answers.pop(0), stopped=stopped)
        assert waits == [0.25]

    def test_default_bound_is_thirty_seconds(self) -> None:
        assert hosted_tools.HOSTED_TOOLS_WAIT_S == 30.0


class _ParkedHermes:
    """Hermes' MCP client with ``mcp_servers.agentchat`` parked, as a gateway
    started offline leaves it: ``get_mcp_status`` says ``failed`` with the
    connect error and no ``mcp__agentchat__`` tool is registered until Hermes'
    own probe (or ``/reload-mcp``, a restart) brings it back (``come_back``)."""

    TOOL = "mcp__agentchat__agentchat_inbox"

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.registered: list[str] = []
        self.status = "failed"
        self.error = "All connection attempts failed; [Errno 111] Connect call failed ('66.241.125.41', 443)"
        parked = self

        class Registry:
            def get_tool_names_for_toolset(self, name: str) -> list[str]:
                return list(parked.registered) if name == hosted_tools.HOSTED_TOOLSET else []

            def get_all_tool_names(self) -> list[str]:
                return ["terminal", *parked.registered]

        def get_mcp_status() -> list[dict[str, Any]]:
            row: dict[str, Any] = {"name": "agentchat", "status": parked.status, "tools": len(parked.registered),
                                   "connected": parked.status == "connected", "needs_auth": False}
            if parked.status == "failed":
                row["error"] = parked.error
            return [row]

        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []  # type: ignore[attr-defined]
        registry = types.ModuleType("tools.registry")
        registry.registry = Registry()  # type: ignore[attr-defined]
        discovery = types.ModuleType("tools.mcp_tool_discovery")
        discovery.get_mcp_status = get_mcp_status  # type: ignore[attr-defined]
        for name, module in (("tools", tools_pkg), ("tools.registry", registry),
                             ("tools.mcp_tool_discovery", discovery)):
            monkeypatch.setitem(sys.modules, name, module)
        # The plugin asks Hermes for no reconnect: there is no such module to reach.
        monkeypatch.setitem(sys.modules, "tools.mcp_tool_loop", None)

    def come_back(self) -> None:
        self.status, self.registered = "connected", [self.TOOL]


class TestParked:
    """MS-F19: the gateway started while api.agentchat.me could not be reached
    and Hermes parked the hosted tools server. Hermes has no public reconnect
    for a plugin: the plugin holds the turn, names the cause and the remedy,
    and Hermes' own probe brings the tools back."""

    def test_the_parked_state_is_read_from_hermes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hermes = _ParkedHermes(monkeypatch)
        state = hosted_tools.hosted_server_state()
        assert state is not None and state.parked and state.status == "failed"
        assert state.error == hermes.error and state.tools == 0
        assert hosted_tools.hosted_tool_names() == []

    def test_a_held_turn_starts_once_hermes_brings_the_tools_back(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        hermes = _ParkedHermes(monkeypatch)
        clock = _Clock()

        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            if clock.t >= 5.0:
                hermes.come_back()

        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.hosted_tools"):
            ok = hosted_tools.wait_for_hosted_tools(timeout_s=30.0, poll_s=0.5, sleep=sleep, now=clock.now)
        assert ok is True
        messages = [r.getMessage() for r in caplog.records]
        assert any("Hermes parked mcp_servers.agentchat" in m and "holding the turn" in m for m in messages)
        assert not any("reconnect" in m for m in messages), "nothing is asked of Hermes"

    def test_still_offline_names_the_cause_and_the_remedy(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _ParkedHermes(monkeypatch)
        clock = _Clock()
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.hosted_tools"):
            ok = hosted_tools.wait_for_hosted_tools(timeout_s=2.0, poll_s=0.5, sleep=clock.sleep, now=clock.now)
        assert ok is False
        (line,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert "Cause: Hermes parked mcp_servers.agentchat after its connection failed (All connection attempts failed" in line
        assert "after 5 minutes, then at longer intervals up to an hour" in line
        assert "run `hermes gateway restart`, or send /reload-mcp in a messaging chat this gateway serves" in line
        assert "not in Desktop or a terminal chat" in line
        assert "mcp SDK" not in line, "not the wrong cause"

    def test_the_plugin_has_no_reconnect_request(self) -> None:
        assert not hasattr(hosted_tools, "request_reconnect")
        assert not hasattr(hosted_tools, "_hermes_reconnect")

    def test_missing_entry_and_other_states_name_their_cause(self) -> None:
        S = hosted_tools.ServerState
        assert "not allowed yet — /agentchat allow in a chat with the agent, or `hermes agentchat allow`, adds it" in hosted_tools.describe_cause(S("missing"))
        assert "disabled" in hosted_tools.describe_cause(S("disabled"))
        assert "still connecting" in hosted_tools.describe_cause(S("connecting"))
        assert "mcp SDK" in hosted_tools.describe_cause(None)


def test_module_level_constants_match_turn_spelling() -> None:
    from agentchatme_hermes.turn import mcp_tool_name

    assert mcp_tool_name("agentchat_inbox").startswith(hosted_tools.HOSTED_TOOL_NAME_PREFIX)
    assert hosted_tools.HOSTED_TOOLSET == "mcp-agentchat"


def _unused(_: Any) -> None:  # keeps the Any import honest for mypy on 3.9
    return None


class TestDoctorLines:
    """doctor reads the running gateway's hosted tools state from its health
    record (the CLI process has no MCP connection of its own)."""

    def _line(self, section: dict[str, Any] | None) -> tuple[str, str] | None:
        from agentchatme_hermes.cli import _describe_hosted_tools

        return _describe_hosted_tools({"hosted_tools": section} if section is not None else {}, now=1000.0)

    def test_parked_is_a_failure_with_the_remedy(self) -> None:
        level, line = self._line({
            "status": "failed", "error": "All connection attempts failed", "tools": 0, "at": 990.0,
        }) or ("", "")
        assert level == "fail"
        assert "Hermes parked mcp_servers.agentchat after its connection failed (All connection attempts failed)" in line
        assert "the agent is woken but cannot answer" in line
        assert "Hermes probes it again on its own after 5 minutes" in line
        assert line.endswith(hosted_tools.PARKED_REMEDY[1:])
        assert "run `hermes gateway restart`, or send /reload-mcp in a messaging chat" in line

    def test_a_record_from_an_older_release_still_reads(self) -> None:
        level, line = self._line({"status": "failed", "error": None, "reconnect": {"requests": 2, "asked_ago_s": 4.0}}) or ("", "")
        assert level == "fail" and "/reload-mcp" in line and "asked Hermes" not in line

    def test_connected_and_connecting(self) -> None:
        assert self._line({"status": "connected", "tools": 31, "at": 995.0}) == (
            "ok", "The running gateway has the hosted tools connected (31 tool(s), read 5s ago)"
        )
        assert (self._line({"status": "connecting"}) or ("", ""))[0] == "warn"
        assert (self._line({"status": "configured"}) or ("", ""))[0] == "warn"
        assert (self._line({"status": "lazy", "tools": 31}) or ("", ""))[0] == "ok", "lazy: registered, connects on first use"

    def test_missing_and_disabled_name_their_cause(self) -> None:
        assert "not allowed yet — /agentchat allow" in (self._line({"status": "missing"}) or ("", ""))[1]
        assert "disabled" in (self._line({"status": "disabled"}) or ("", ""))[1]

    def test_no_section_says_nothing(self) -> None:
        assert self._line(None) is None
        assert self._line({"status": None}) is None  # type: ignore[dict-item]

    def test_a_tools_unavailable_cycle_carries_the_remedy(self) -> None:
        from agentchatme_hermes.cli import _describe_worker

        cycle = {"outcome": "tools_unavailable", "failure": "the hosted AgentChat tools are not registered in Hermes yet",
                 "at": "2026-11-22T14:52:08.624Z"}
        level, line = _describe_worker({"state": "idle", "loop": {"recent_cycles": [cycle]}}, now=1000.0) or ("", "")
        assert level == "warn" and "tools_unavailable" in line
        assert "`hermes gateway restart`, or /reload-mcp in a messaging chat this gateway serves (not Desktop or a terminal chat), reconnects them" in line
        level, line = _describe_worker({"state": "idle", "loop": {"recent_cycles": [cycle] * 3}}, now=1000.0) or ("", "")
        assert level == "fail" and "/reload-mcp in a messaging chat this gateway serves" in line

    @pytest.mark.parametrize("state", ["not_allowed_yet", "not_allowed", "applying", "disabled"])
    def test_a_run_paused_by_the_switch_is_said_not_alarmed_on(self, state: str) -> None:
        from agentchatme_hermes import allow
        from agentchatme_hermes.cli import _describe_worker

        cycle = {"outcome": "tools_unavailable", "failure": allow.reason(state), "at": "2026-11-26T10:00:00Z"}  # type: ignore[arg-type]
        worker = {"state": "idle", "loop": {"recent_cycles": [cycle] * 5}, "tools_allowed": {"state": state}}
        level, line = _describe_worker(worker, now=1000.0) or ("", "")
        assert level == "warn" and line.startswith("The receive loop is paused: ")
        assert allow.reason(state) in line  # type: ignore[arg-type]

    def test_allowed_again_alarms_as_before(self) -> None:
        from agentchatme_hermes.cli import _describe_worker

        cycle = {"outcome": "tools_unavailable", "failure": "the hosted AgentChat tools are not registered in Hermes yet"}
        worker = {"state": "idle", "loop": {"recent_cycles": [cycle] * 3}, "tools_allowed": {"state": "allowed"}}
        assert (_describe_worker(worker, now=1000.0) or ("", ""))[0] == "fail"
