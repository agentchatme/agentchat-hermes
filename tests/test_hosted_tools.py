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
    connect error, no ``mcp__agentchat__`` tool is registered, and only a
    reconnect request (``reconnect_mcp_server``) brings the tools back — once
    the network is back (``online``)."""

    TOOL = "mcp__agentchat__agentchat_inbox"

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, online: bool = True, adopted: bool = True) -> None:
        self.online = online
        self.adopted = adopted
        self.registered: list[str] = []
        self.status = "failed"
        self.error = "All connection attempts failed; [Errno 111] Connect call failed ('66.241.125.41', 443)"
        self.requests: list[str] = []
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

        def reconnect_mcp_server(name: str) -> bool:
            parked.requests.append(name)
            if not parked.adopted:
                return False
            if parked.online:  # the revival connects and re-registers the tools
                parked.status, parked.registered = "connected", [parked.TOOL]
            return True

        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []  # type: ignore[attr-defined]
        registry = types.ModuleType("tools.registry")
        registry.registry = Registry()  # type: ignore[attr-defined]
        discovery = types.ModuleType("tools.mcp_tool_discovery")
        discovery.get_mcp_status = get_mcp_status  # type: ignore[attr-defined]
        loop = types.ModuleType("tools.mcp_tool_loop")
        loop.reconnect_mcp_server = reconnect_mcp_server  # type: ignore[attr-defined]
        for name, module in (("tools", tools_pkg), ("tools.registry", registry),
                             ("tools.mcp_tool_discovery", discovery), ("tools.mcp_tool_loop", loop)):
            monkeypatch.setitem(sys.modules, name, module)
        hosted_tools._gates.clear()


class TestParked:
    """MS-F19: the gateway started while api.agentchat.me could not be reached,
    Hermes parked the hosted tools server, and nothing asked it to reconnect."""

    def test_the_parked_state_is_read_from_hermes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hermes = _ParkedHermes(monkeypatch)
        state = hosted_tools.hosted_server_state()
        assert state is not None and state.parked and state.status == "failed"
        assert state.error == hermes.error and state.tools == 0
        assert hosted_tools.hosted_tool_names() == []

    def test_a_held_turn_asks_for_the_reconnect_and_starts(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        hermes = _ParkedHermes(monkeypatch)
        clock = _Clock()
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.hosted_tools"):
            ok = hosted_tools.wait_for_hosted_tools(timeout_s=30.0, poll_s=0.5, sleep=clock.sleep, now=clock.now, key="/p")
        assert ok is True, "the tools came back within the hold: the turn starts in this cycle"
        assert hermes.requests == ["agentchat"]
        messages = [r.getMessage() for r in caplog.records]
        assert any("asked Hermes to reconnect mcp_servers.agentchat (a turn is waiting for the tools" in m for m in messages)
        assert any("Hermes parked mcp_servers.agentchat" in m and "a reconnect was requested" in m for m in messages)
        assert hosted_tools.hosted_tool_names() == [_ParkedHermes.TOOL]

    def test_still_offline_names_the_real_cause(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        hermes = _ParkedHermes(monkeypatch, online=False)
        clock = _Clock()
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.hosted_tools"):
            ok = hosted_tools.wait_for_hosted_tools(timeout_s=2.0, poll_s=0.5, sleep=clock.sleep, now=clock.now, key="/p")
        assert ok is False and hermes.requests == ["agentchat"], "asked once in the hold, not on every poll"
        (line,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert "Cause: Hermes parked mcp_servers.agentchat after its connection failed (All connection attempts failed" in line
        assert "a reconnect was just requested — if the tools do not come back, run `hermes gateway restart`" in line
        assert "mcp SDK" not in line, "not the wrong cause"

    def test_requests_are_rate_limited_and_back_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hermes = _ParkedHermes(monkeypatch, online=False)
        clock = _Clock()
        ask = lambda: hosted_tools.request_reconnect(key="/p", reason="t", now=clock.now).outcome  # noqa: E731
        assert ask() == "requested"
        clock.t += 59
        assert ask() == "too_soon"
        clock.t += 1
        assert ask() == "requested", "60 s after the first"
        clock.t += 119
        assert ask() == "too_soon", "still parked after one: the next waits 120 s"
        clock.t += 1
        assert ask() == "requested"
        for _ in range(10):
            clock.t += hosted_tools.RECONNECT_MAX_INTERVAL_S
            assert ask() == "requested"
        clock.t += hosted_tools.RECONNECT_MAX_INTERVAL_S - 1
        assert ask() == "too_soon", "capped at 30 min, never a loop"
        assert len(hermes.requests) == 13
        assert hosted_tools.request_reconnect(key="/other", reason="t", now=clock.now).outcome == "requested", "per profile"

    def test_a_connected_server_is_never_asked_and_resets_the_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hermes = _ParkedHermes(monkeypatch, online=False)
        clock = _Clock()
        assert hosted_tools.request_reconnect(key="/p", reason="t", now=clock.now).outcome == "requested"
        hermes.status = "connected"
        assert hosted_tools.request_reconnect(key="/p", reason="t", now=clock.now).outcome == "not_parked"
        hermes.status = "failed"
        assert hosted_tools.request_reconnect(key="/p", reason="t", now=clock.now).outcome == "requested", "reset"
        assert hermes.requests == ["agentchat", "agentchat"]

    @pytest.mark.parametrize("status", ["connecting", "disabled", "configured", "lazy"])
    def test_only_a_parked_server_is_asked(self, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
        hermes = _ParkedHermes(monkeypatch)
        hermes.status = status
        assert hosted_tools.request_reconnect(key="/p", reason="t").outcome == "not_parked"
        assert hermes.requests == [], "a reconnect would tear down a live or connecting session"

    def test_no_parked_connection_says_restart(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        _ParkedHermes(monkeypatch, adopted=False)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.hosted_tools"):
            assert hosted_tools.request_reconnect(key="/p", reason="t").outcome == "no_connection"
        assert any("`hermes gateway restart`" in r.getMessage() for r in caplog.records)

    def test_a_hermes_without_the_reconnect_request_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ParkedHermes(monkeypatch)
        monkeypatch.setitem(sys.modules, "tools.mcp_tool_loop", None)
        hosted_tools._warned.clear()
        assert hosted_tools.request_reconnect(key="/p", reason="t").outcome == "unavailable"

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
            "status": "failed", "error": "All connection attempts failed", "tools": 0,
            "reconnect": {"requests": 2, "asked_ago_s": 40.0}, "at": 990.0,
        }) or ("", "")
        assert level == "fail"
        assert "Hermes parked mcp_servers.agentchat after its connection failed (All connection attempts failed)" in line
        assert "the agent is woken but cannot answer" in line
        assert "asked Hermes to reconnect them 2 time(s), last 40s ago" in line
        assert line.endswith("run `hermes gateway restart` to reconnect them now")

    def test_parked_before_any_request(self) -> None:
        level, line = self._line({"status": "failed", "error": None, "reconnect": {"requests": 0, "asked_ago_s": None}}) or ("", "")
        assert level == "fail" and "asks Hermes to reconnect them when a wake needs them" in line

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
        assert "`hermes gateway restart` reconnects them at once" in line
        level, line = _describe_worker({"state": "idle", "loop": {"recent_cycles": [cycle] * 3}}, now=1000.0) or ("", "")
        assert level == "fail" and "`hermes gateway restart` reconnects them at once" in line

    @pytest.mark.parametrize("state", ["not_allowed_yet", "not_allowed", "restart_to_apply", "disabled"])
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

    def test_the_running_gateway_still_to_apply(self) -> None:
        from agentchatme_hermes.cli import _describe_tools_allowed

        assert _describe_tools_allowed({"tools_allowed": {"state": "restart_to_apply"}}) == (
            "warn",
            "AgentChat tools: allowed in config.yaml, but the running gateway connected them "
            "before that — send /reload-mcp in a chat it serves, or run hermes gateway restart",
        )
        for section in (None, {"state": "allowed"}, {"state": "not_allowed"}, "x"):
            assert _describe_tools_allowed({"tools_allowed": section}) is None
