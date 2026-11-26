"""Tests for ``agentchatme_hermes.allow`` — the one switch.

May the agent use the AgentChat tools on its own? One question, asked of a
person (their own terminal, or ``/agentchat allow`` in a chat); not asked in
an agent's terminal (best effort); the tools' entry written only once someone
decided, except the one-time carry-over of a setup made before 0.3; a yes over
a connected untrusted entry applied in two steps; and the one reading of the
switch (:func:`allow.read_switch`) for the pause, doctor, status and the wizard.
"""
from __future__ import annotations

import builtins
import io
import sys
import threading
import types
from typing import TYPE_CHECKING, Any

import pytest
from fake_keys import api_key
from key_files import save_key

from agentchatme_hermes import allow, hosted_tools, mcp_registration
from agentchatme_hermes.mcp_registration import Allowance
from agentchatme_hermes.profile_scope import ProfileScope, ProfileScopeError

if TYPE_CHECKING:
    from pathlib import Path


def _allowance(entry: str = "ours", trust: Any = "untrusted", via: Any = None) -> Allowance:
    return Allowance(entry, trust, via)  # type: ignore[arg-type]


def _config_says(monkeypatch: pytest.MonkeyPatch, allowance: Any) -> list[Any]:
    """``config.yaml`` as :func:`allow.read_switch` reads it (``None``: unreadable)."""
    box = [allowance]

    def inspect(*, api_base: str) -> Allowance:
        if box[0] is None:
            raise mcp_registration.McpRegistrationError("no hermes")
        return box[0]  # type: ignore[no-any-return]

    monkeypatch.setattr(allow, "inspect_allowance", inspect)
    return box


# ─── who is at the terminal (best effort) ──────────────────────────────────


class TestAgentTerminal:
    @pytest.mark.parametrize(
        ("environ", "agent"),
        [
            ({}, False),
            ({"HERMES_SESSION_ID": ""}, False),
            ({"HERMES_SESSION_ID": "  "}, False),
            ({"HERMES_SESSION_ID": "20261126_101500_abc"}, True),
            # The Desktop terminal pane's marker reaches agent commands too: no exception.
            ({"HERMES_SESSION_ID": "s1", "HERMES_DESKTOP_TERMINAL": "1"}, True),
            # Every hermes process sets these itself: no signal.
            ({"HERMES_AGENT": "true", "AI_AGENT": "hermes-agent"}, False),
        ],
    )
    def test_the_session_id_marks_an_agents_terminal(self, environ: dict[str, str], agent: bool) -> None:
        assert allow.in_agent_terminal(environ) is agent

    def test_it_reads_the_process_environment_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_SESSION_ID", "s1")
        assert allow.in_agent_terminal() is True

    def test_stdin_is_tty_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("x"))
        assert allow.stdin_is_tty() is False
        monkeypatch.setattr(sys, "stdin", None)
        assert allow.stdin_is_tty() is False


class TestProcessOrigin:
    """``/agentchat allow`` in a classic chat process: the reading taken at
    ``register(ctx)``, before the chat's own session sets an id."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(allow, "_origin_session", None)

    def test_started_by_an_agents_terminal(self) -> None:
        allow.note_process_origin({"HERMES_SESSION_ID": "agent-session"})
        assert allow.started_in_agent_terminal("cli-session") is True
        assert allow.started_in_agent_terminal("") is True

    def test_a_persons_terminal(self) -> None:
        allow.note_process_origin({})
        assert allow.started_in_agent_terminal("cli-session") is False

    def test_registered_after_the_chat_began_is_the_chats_own_id(self) -> None:
        allow.note_process_origin({"HERMES_SESSION_ID": "cli-session"})
        assert allow.started_in_agent_terminal("cli-session") is False

    def test_taken_once(self) -> None:
        allow.note_process_origin({})
        allow.note_process_origin({"HERMES_SESSION_ID": "later"})
        assert allow.started_in_agent_terminal("x") is False

    def test_not_taken_reads_as_a_persons(self) -> None:
        assert allow.started_in_agent_terminal("x") is False


class TestTheQuestion:
    """``[Y/n]``: Enter is a yes, ``n`` a no; Ctrl-C and the end of input decide nothing."""

    def _reader(self, monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[str]:
        asked: list[str] = []

        def line_input(text: str) -> str:
            asked.append(text)
            if isinstance(answer, BaseException):
                raise answer
            return str(answer)

        module = types.ModuleType("hermes_cli.cli_output")
        module.line_input = line_input  # type: ignore[attr-defined]
        module.color = lambda text, *_codes: text  # type: ignore[attr-defined]
        module.Colors = types.SimpleNamespace(YELLOW="")  # type: ignore[attr-defined]
        package = types.ModuleType("hermes_cli")
        package.cli_output = module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "hermes_cli", package)
        monkeypatch.setitem(sys.modules, "hermes_cli.cli_output", module)
        return asked

    @pytest.mark.parametrize(
        ("answer", "result"),
        [
            ("", "yes"),
            ("y", "yes"),
            (" Y ", "yes"),
            ("yes", "yes"),
            ("n", "no"),
            ("no", "no"),
            ("maybe", "no"),
            (EOFError(), "end_of_input"),
            (KeyboardInterrupt(), "cancelled"),
        ],
    )
    def test_answers(self, monkeypatch: pytest.MonkeyPatch, answer: Any, result: str) -> None:
        asked = self._reader(monkeypatch, answer)
        assert allow.ask_in_terminal("Allow @alice to use AgentChat tools on its own?") == result
        assert asked == ["  Allow @alice to use AgentChat tools on its own? [Y/n]: "]

    def test_without_hermes_plain_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "hermes_cli", None)
        monkeypatch.setitem(sys.modules, "hermes_cli.cli_output", None)

        def eof(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr(builtins, "input", eof)
        assert allow.ask_in_terminal("q?") == "end_of_input"
        monkeypatch.setattr(builtins, "input", lambda _prompt: "")
        assert allow.ask_in_terminal("q?") == "yes"


class TestDecide:
    def _decide(
        self, tmp_path: Path, *, tty: bool = True, environ: Any = None, answer: Any = "yes", cmd: str = "hermes agentchat"
    ) -> allow.SetupDecision:
        asked: list[str] = []

        def ask(question: str) -> Any:
            asked.append(question)
            return answer

        decision = allow.decide(
            "alice", api_base="https://a.test", tty=tty, home=tmp_path, ask=ask, environ=environ or {}, cmd=cmd
        )
        self.asked = asked
        return decision

    @pytest.fixture(autouse=True)
    def _not_managed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(allow, "config_is_managed", lambda: False)

    def test_a_yes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, _allowance("missing", None))
        assert self._decide(tmp_path) == allow.SetupDecision("yes", "AgentChat tools allowed")
        assert self.asked == ["Allow @alice to use AgentChat tools on its own?"]

    def test_a_no(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, _allowance("ours", "untrusted"))
        decision = self._decide(tmp_path, answer="no")
        assert decision.decision == "no"
        assert decision.line == (
            "AgentChat tools not allowed. @alice can read AgentChat but does not answer on its own. "
            "To allow later: send /agentchat allow in a chat with it."
        )

    def test_ctrl_c_decides_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, _allowance("missing", None))
        decision = self._decide(tmp_path, answer="cancelled")
        assert decision.decision is None and decision.line.startswith("AgentChat tools: not decided")

    def test_the_end_of_input_is_no_terminal(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, _allowance("missing", None))
        decision = self._decide(tmp_path, answer="end_of_input")
        assert decision.decision is None and decision.line.startswith("AgentChat tools: not allowed yet (no terminal")

    def test_an_agents_terminal_is_not_asked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, _allowance("missing", None))
        decision = self._decide(tmp_path, environ={"HERMES_SESSION_ID": "s1"})
        assert decision.decision is None and self.asked == []
        assert decision.line == (
            "AgentChat tools: not allowed yet. This is an agent's terminal, so the plugin does not ask here. "
            "The person who runs @alice allows them once by sending /agentchat allow in a chat with it."
        )

    def test_no_terminal_is_not_asked_and_names_the_operator_route(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _config_says(monkeypatch, _allowance("missing", None))
        decision = self._decide(tmp_path, tty=False, cmd="hermes -p ops agentchat")
        assert decision.decision is None and self.asked == []
        first, _, rest = decision.line.partition("\n")
        assert first == (
            "AgentChat tools: not allowed yet (no terminal to ask in). To allow: send /agentchat allow in a "
            "chat with @alice, or run hermes -p ops agentchat allow in a terminal."
        )
        assert rest.startswith("An operator who builds config.yaml (a container image")
        assert mcp_registration.manual_snippet("https://a.test", trust="full") in rest

    @pytest.mark.parametrize(
        ("allowance", "line"),
        [
            (_allowance("ours", "full", "trust"), allow.ALREADY_ALLOWED_LINE),
            (_allowance("ours", "unset", "no_trust_key"), allow.ALREADY_ALLOWED_LINE),
            (_allowance("user_managed", "untrusted"), allow.HAND_MANAGED_LINE),
            (_allowance("disabled", "full"), allow.DISABLED_LINE),
        ],
    )
    def test_not_asked_when_there_is_nothing_to_decide(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allowance: Allowance, line: str
    ) -> None:
        _config_says(monkeypatch, allowance)
        assert self._decide(tmp_path) == allow.SetupDecision(None, line) and self.asked == []

    @pytest.mark.parametrize("via", ["approvals_off", "yolo"])
    def test_a_bypass_alone_is_not_allowed_already(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, via: str) -> None:
        """Only the entry's tier counts as the person's switch."""
        _config_says(monkeypatch, _allowance("ours", "untrusted", via))
        assert self._decide(tmp_path).decision == "yes" and self.asked

    def test_a_yes_being_applied_is_not_asked_again(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, _allowance("missing", None))
        allow.write_record(tmp_path, how="asked", trust="full", decision="yes", pending=True)
        assert self._decide(tmp_path) == allow.SetupDecision(None, allow.APPLYING_LINE)

    def test_a_managed_config_is_not_asked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, _allowance("missing", None))
        monkeypatch.setattr(allow, "config_is_managed", lambda: True)
        assert self._decide(tmp_path) == allow.SetupDecision(None, allow.MANAGED_LINE)

    def test_an_unreadable_config_still_asks_the_person(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, None)
        assert self._decide(tmp_path).decision == "yes"

    def test_an_unknown_handle(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _config_says(monkeypatch, _allowance("missing", None))
        asked: list[str] = []
        allow.decide(None, api_base="x", tty=True, home=tmp_path, ask=lambda q: asked.append(q) or "yes", environ={})
        assert asked == ["Allow this agent to use AgentChat tools on its own?"]

    def test_decide_for_is_this_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[Any] = []
        monkeypatch.setattr(allow, "decide", lambda handle, **kw: seen.append((handle, kw)) or allow.SetupDecision(None, "x"))
        monkeypatch.setattr(allow, "stdin_is_tty", lambda: True)
        allow.decide_for("alice")
        assert seen and seen[0][0] == "alice" and seen[0][1]["tty"] is True
        assert seen[0][1]["cmd"].endswith("agentchat") and seen[0][1]["api_base"] == "https://api.agentchat.me"


# ─── the one reading of the switch ─────────────────────────────────────────


class TestSwitch:
    @pytest.mark.parametrize(
        ("allowance", "pending", "state"),
        [
            (None, False, "unknown"),
            (_allowance("missing", None), False, "not_allowed_yet"),
            (_allowance("missing", None), True, "applying"),
            (_allowance("disabled", "full"), False, "disabled"),
            (_allowance("ours", "untrusted"), False, "not_allowed"),
            (_allowance("ours", "full", "trust"), False, "allowed"),
            (_allowance("ours", "unset", "no_trust_key"), False, "allowed"),
            (_allowance("ours", "untrusted", "approvals_off"), False, "allowed"),
            (_allowance("ours", "untrusted", "yolo"), False, "allowed"),
            (_allowance("user_managed", "full", "trust"), False, "allowed"),
        ],
    )
    def test_read_switch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allowance: Any, pending: bool, state: str
    ) -> None:
        _config_says(monkeypatch, allowance)
        if pending:
            allow.write_record(tmp_path, how="asked", trust="full", decision="yes", pending=True)
        switch = allow.read_switch(tmp_path, api_base="x")
        assert switch.state == state
        assert switch.paused is (state in ("not_allowed_yet", "applying", "not_allowed", "disabled"))

    def test_by_tier(self) -> None:
        assert allow.Switch("allowed", _allowance("ours", "full", "trust")).by_tier
        assert allow.Switch("allowed", _allowance("ours", "unset", "no_trust_key")).by_tier
        assert not allow.Switch("allowed", _allowance("ours", "untrusted", "approvals_off")).by_tier
        assert not allow.Switch("unknown").by_tier

    @pytest.mark.parametrize(
        ("written", "trust", "edited"),
        [("full", "full", False), ("full", "untrusted", True), ("untrusted", "full", True), ("untrusted", "unset", True), (None, "full", False)],
    )
    def test_a_trust_changed_by_hand(self, written: Any, trust: Any, edited: bool) -> None:
        switch = allow.Switch("allowed", _allowance("ours", trust, "trust"), {"how": "asked", "trust": written})
        assert switch.hand_edited is edited

    def test_paused_states(self) -> None:
        assert {"not_allowed_yet", "applying", "not_allowed", "disabled"} == allow.PAUSED_STATES
        for state in allow.PAUSED_STATES:
            assert allow.reason(state) != state  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("switch", "words"),
        [
            (allow.Switch("allowed", _allowance("ours", "full", "trust")), "allowed (trust: full)"),
            (
                allow.Switch("allowed", _allowance("ours", "full", "trust"), {"how": "carried_over", "trust": "full"}),
                "allowed (carried over from your setup under an earlier release)",
            ),
            (allow.Switch("allowed", _allowance("ours", "unset", "no_trust_key")), "which Hermes reads as full"),
            (allow.Switch("allowed", _allowance("ours", "untrusted", "approvals_off")), "approvals.mode: off"),
            (allow.Switch("allowed", _allowance("ours", "untrusted", "yolo")), "HERMES_YOLO_MODE"),
            (
                allow.Switch("allowed", _allowance("ours", "full", "trust"), {"how": "asked", "trust": "untrusted"}),
                "trust changed by hand there",
            ),
            (allow.Switch("applying", _allowance("missing", None)), "within about two minutes"),
            (allow.Switch("not_allowed_yet", _allowance("missing", None)), "@alice has no AgentChat tools until the person allows them"),
            (allow.Switch("not_allowed", _allowance("ours", "untrusted")), "@alice can read AgentChat but does not answer on its own"),
            (allow.Switch("disabled"), "disabled"),
            (allow.Switch("unknown"), "not known"),
        ],
    )
    def test_describe(self, switch: allow.Switch, words: str) -> None:
        assert words in allow.describe(switch, "alice")

    def test_describe_names_the_profile_command(self) -> None:
        text = allow.describe(allow.Switch("not_allowed", _allowance()), "alice", cmd="hermes -p ops agentchat")
        assert "run hermes -p ops agentchat allow" in text


# ─── the record and the one-time carry-over ────────────────────────────────


class TestFirstPlacement:
    def test_a_setup_from_before_0_3_is_carried_over_and_recorded_first(self, tmp_path: Path) -> None:
        assert allow.first_placement(tmp_path, entry="missing", legacy_key_set=True) == "carried_over"
        record = allow.read_record(tmp_path)
        assert record is not None and (record["how"], record["trust"], record["pending"]) == ("carried_over", "full", True)

    def test_without_the_old_name_nothing(self, tmp_path: Path) -> None:
        assert allow.first_placement(tmp_path, entry="missing", legacy_key_set=False) is None
        assert allow.read_record(tmp_path) is None

    @pytest.mark.parametrize("entry", ["ours", "user_managed", "disabled"])
    def test_an_entry_already_there_is_recorded(self, tmp_path: Path, entry: Any) -> None:
        assert allow.first_placement(tmp_path, entry=entry, legacy_key_set=True) is None
        record = allow.read_record(tmp_path)
        assert record is not None and record["how"] == "existing"
        assert allow.first_placement(tmp_path, entry="missing", legacy_key_set=True) is None, "never again"

    def test_after_any_record_never_again(self, tmp_path: Path) -> None:
        allow.write_record(tmp_path, how="asked", trust="untrusted", decision="no")
        assert allow.first_placement(tmp_path, entry="missing", legacy_key_set=True) is None

    def test_a_record_that_cannot_be_written_carries_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "agentchat").write_text("a file where the directory goes", encoding="utf-8")
        assert allow.first_placement(tmp_path, entry="missing", legacy_key_set=True) is None

    def test_the_record_is_the_plugins_own_private_file(self, tmp_path: Path, assert_private_file: Any) -> None:
        assert allow.write_record(tmp_path, how="asked", trust="full", decision="yes") is True
        path = tmp_path / "agentchat" / "mcp-entry.json"
        assert allow.record_path(tmp_path) == path
        assert_private_file(path)
        record = allow.read_record(tmp_path)
        assert record is not None
        assert {k: record[k] for k in ("version", "how", "decision", "trust", "pending")} == {
            "version": 1, "how": "asked", "decision": "yes", "trust": "full", "pending": False,
        }

    def test_it_survives_logout_and_logout_clears_a_pending_yes(self, tmp_path: Path) -> None:
        from agentchatme_hermes import key_store

        save_key(api_key("logout_0000000000000000"), home=tmp_path)
        allow.write_record(tmp_path, how="asked", trust="full", decision="yes", pending=True)
        assert key_store.delete_store(tmp_path) is True
        allow.clear_pending(tmp_path)
        record = allow.read_record(tmp_path)
        assert record is not None and record["decision"] == "yes" and record["pending"] is False

    def test_an_unwritable_home_is_logged_not_raised(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        (tmp_path / "agentchat").write_text("a file where the directory goes", encoding="utf-8")
        assert allow.write_record(tmp_path, how="asked", trust="full") is False
        assert any("could not write" in r.getMessage() for r in caplog.records)

    def test_the_legacy_name_is_read_through_the_profiles_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import profile_scope

        monkeypatch.setattr(profile_scope, "resolve_setting", lambda name: "x" if name == "AGENTCHATME_API_KEY" else None)
        assert allow.legacy_key_set() is True
        monkeypatch.setattr(profile_scope, "resolve_setting", lambda name: "  ")
        assert allow.legacy_key_set() is False

        def boom(name: str) -> str:
            raise RuntimeError("unscoped")

        monkeypatch.setattr(profile_scope, "resolve_setting", boom)
        assert allow.legacy_key_set() is False


# ─── a pending yes: the second step ────────────────────────────────────────


class TestCompletePending:
    def _placement(self, monkeypatch: pytest.MonkeyPatch, outcome: str = "added") -> list[Any]:
        from agentchatme_hermes import placement
        from agentchatme_hermes.placement import PlacementStep

        placed: list[Any] = []
        status = "failed" if outcome == "failed" else "changed"
        monkeypatch.setattr(
            placement, "place_mcp", lambda api_base, decision=None: placed.append(decision) or PlacementStep("mcp", status, "d", outcome)
        )
        return placed

    def test_nothing_pending_writes_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._placement(monkeypatch)
        assert allow.complete_pending(tmp_path, api_base="x", wait=False) is False
        allow.write_record(tmp_path, how="asked", trust="full", decision="yes")
        assert allow.complete_pending(tmp_path, api_base="x", wait=False) is False
        assert placed == []

    @pytest.mark.parametrize(("how", "decision"), [("asked", "yes"), ("carried_over", "carried_over")])
    def test_a_pending_full_is_written(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: Any, decision: str) -> None:
        placed = self._placement(monkeypatch)
        allow.write_record(tmp_path, how=how, trust="full", decision="yes" if how == "asked" else None, pending=True)
        called: list[str] = []
        allow.on_applied(tmp_path, lambda: called.append("applied"))
        try:
            assert allow.complete_pending(tmp_path, api_base="x", wait=False) is True
        finally:
            allow.on_applied(tmp_path, None)
        assert placed == [decision] and called == ["applied"]

    def test_it_waits_for_hermes_to_drop_the_old_server(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from agentchatme_hermes import placement
        from agentchatme_hermes.placement import PlacementStep

        allow.write_record(tmp_path, how="asked", trust="full", decision="yes", pending=True)
        readings = iter([["old"], ["old"], [], ["new"]])
        log: list[str] = []

        def names() -> list[str]:
            current = next(readings, ["new"])
            log.append("held" if current == ["old"] else "none" if not current else "connected")
            return current

        def place(api_base: str, decision: Any = None) -> PlacementStep:
            log.append(f"write {decision}")
            return PlacementStep("mcp", "changed", "d", "added")

        monkeypatch.setattr(hosted_tools, "hosted_tool_names", names)
        monkeypatch.setattr(placement, "place_mcp", place)
        monkeypatch.setattr(allow, "POLL_S", 0.0)
        assert allow.complete_pending(tmp_path, api_base="x", wait=True) is True
        # Written only once Hermes held no AgentChat tools; done once they are connected again.
        assert log == ["held", "held", "none", "write yes", "connected"]

    def test_a_server_that_is_never_dropped_is_written_anyway_and_said(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        placed = self._placement(monkeypatch)
        allow.write_record(tmp_path, how="asked", trust="full", decision="yes", pending=True)
        monkeypatch.setattr(hosted_tools, "hosted_tool_names", lambda: ["mcp__agentchat__agentchat_inbox"])
        monkeypatch.setattr(allow, "DROP_TIMEOUT_S", 0.0)
        monkeypatch.setattr(allow, "CONNECT_TIMEOUT_S", 0.0)
        assert allow.complete_pending(tmp_path, api_base="x", wait=True) is True
        assert placed == ["yes"]
        assert any("hermes gateway restart" in r.getMessage() for r in caplog.records)

    def test_a_failed_write_stays_pending(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._placement(monkeypatch, outcome="failed")
        allow.write_record(tmp_path, how="asked", trust="full", decision="yes", pending=True)
        assert allow.complete_pending(tmp_path, api_base="x", wait=False) is False
        record = allow.read_record(tmp_path)
        assert record is not None and record["pending"] is True

    def test_start_completion_runs_once(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        release = threading.Event()
        runs: list[str] = []

        def slow(home: Any, *, api_base: str, wait: bool, stop: Any = None) -> bool:
            runs.append(api_base)
            release.wait(5.0)
            return True

        monkeypatch.setattr(allow, "complete_pending", slow)
        scope = ProfileScope.capture(tmp_path)
        first = allow.start_completion(scope, api_base="x")
        assert first is not None
        assert allow.start_completion(scope, api_base="x") is None, "one at a time"
        release.set()
        first.join(5.0)
        assert runs == ["x"]

    def test_gateway_lives(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from agentchatme_hermes import ws_health

        assert allow.gateway_lives(tmp_path, api_base="x") is False, "no key"
        save_key(api_key("gw_000000000000000000"), home=tmp_path)
        monkeypatch.setattr(ws_health, "live_health", lambda base, key, home: {"pid": 1})
        assert allow.gateway_lives(tmp_path, api_base="x") is True
        monkeypatch.setattr(ws_health, "live_health", lambda base, key, home: None)
        assert allow.gateway_lives(tmp_path, api_base="x") is False


# ─── /agentchat allow ──────────────────────────────────────────────────────


class _Chat:
    """A ``ctx`` with Hermes' two command methods; ``picked`` is what the
    person taps (``None``: unanswered, or nothing can ask here)."""

    def __init__(self, picked: Any = "allow") -> None:
        self.picked = picked
        self.asked: list[tuple[str, list[Any]]] = []
        self.commands: dict[str, Any] = {}

    def register_command(self, name: str, handler: Any, description: str = "", args_hint: str = "") -> None:
        self.commands[name] = (handler, description, args_hint)

    def ask_choice(self, question: str, choices: list[Any]) -> Any:
        self.asked.append((question, choices))
        return self.picked


class TestSlashCommand:
    @pytest.fixture(autouse=True)
    def _a_persons_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(allow, "_origin_session", "")

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        state: str,
        allowance: Any,
        outcome: str = "added",
        key: bool = True,
        gateway: bool = True,
    ) -> list[Any]:
        from agentchatme_hermes import leader_lock, placement
        from agentchatme_hermes.placement import PlacementStep

        if key:
            save_key(api_key("slash_0000000000000000"), handle="alice", home=tmp_path)
        placed: list[Any] = []
        monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: allow.Switch(state, allowance))  # type: ignore[arg-type]
        monkeypatch.setattr(allow, "config_is_managed", lambda: False)
        monkeypatch.setattr(allow, "start_completion", lambda scope, *, api_base: placed.append("completion"))
        status = "failed" if outcome == "failed" else "changed"
        monkeypatch.setattr(
            placement,
            "place_mcp",
            lambda api_base, decision=None: placed.append(decision) or PlacementStep("mcp", status, "why", outcome),
        )
        monkeypatch.setattr(leader_lock, "is_hermes_gateway_process", lambda: gateway)
        return placed

    def _run(self, tmp_path: Path, chat: _Chat, args: str = "allow", session_id: str = "") -> str:
        allow.register_allow_command(chat, ProfileScope.capture(tmp_path))
        handler, _description, hint = chat.commands["agentchat"]
        assert hint == "allow"
        return str(handler(args, session_id=session_id))

    def test_allow_with_no_entry_writes_it_full(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None))
        chat = _Chat("allow")
        reply = self._run(tmp_path, chat)
        assert chat.asked == [
            (
                "Allow @alice to use AgentChat tools on its own?",
                [{"value": "allow", "label": "Allow"}, {"value": "no", "label": "Don't allow"}],
            )
        ]
        assert placed == ["yes"]
        assert reply == "AgentChat tools allowed for @alice. The gateway connects them within about a minute."

    def test_allow_over_an_untrusted_entry_in_the_gateway_applies_in_two_steps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed", allowance=_allowance(), outcome="applying")
        reply = self._run(tmp_path, _Chat("allow"))
        assert placed == ["yes", "completion"], "the gateway finishes the second step"
        assert reply == "AgentChat tools allowed for @alice; it takes effect within about two minutes."

    def test_allow_with_no_gateway_running_is_changed_in_place(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed", allowance=_allowance(), outcome="allowed", gateway=False)
        reply = self._run(tmp_path, _Chat("allow"))
        assert placed == ["yes"]
        assert reply == "AgentChat tools allowed for @alice. This chat uses them after /reload-mcp."

    def test_dont_allow_writes_untrusted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None))
        reply = self._run(tmp_path, _Chat("no"))
        assert placed == ["no"]
        assert reply.startswith("AgentChat tools not allowed. @alice can read AgentChat")

    def test_unanswered_changes_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None))
        assert self._run(tmp_path, _Chat(None)) == "Nothing changed: the question was not answered."
        assert placed == []

    def test_a_classic_chat_an_agents_terminal_started_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None), gateway=False)
        monkeypatch.setattr(allow, "_origin_session", "the-agents-session")
        chat = _Chat("allow")
        reply = self._run(tmp_path, chat, session_id="this-chats-session")
        assert chat.asked == [] and placed == []
        assert reply.startswith("This chat runs in a process an agent's terminal started")
        assert "Send /agentchat allow in your own chat with @alice" in reply

    def test_the_gateways_chats_are_the_persons(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A gateway an agent's terminal restarted still serves the person's chats."""
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None), gateway=True)
        monkeypatch.setattr(allow, "_origin_session", "the-agents-session")
        assert self._run(tmp_path, _Chat("allow"), session_id="tg-session").startswith("AgentChat tools allowed")
        assert placed == ["yes"]

    @pytest.mark.parametrize("via", ["trust", "no_trust_key"])
    def test_already_allowed_asks_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, via: str) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="allowed", allowance=_allowance("ours", "full", via))
        chat = _Chat("allow")
        assert self._run(tmp_path, chat) == "AgentChat tools are already allowed for @alice."
        assert chat.asked == [] and placed == []

    def test_a_bypass_alone_still_asks(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="allowed", allowance=_allowance("ours", "untrusted", "approvals_off"), outcome="applying")
        chat = _Chat("allow")
        self._run(tmp_path, chat)
        assert chat.asked and placed[0] == "yes"

    def test_a_yes_being_applied_is_finished_not_asked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="applying", allowance=_allowance("missing", None))
        chat = _Chat("allow")
        reply = self._run(tmp_path, chat)
        assert chat.asked == [] and placed == ["completion"]
        assert "within about two minutes" in reply

    @pytest.mark.parametrize(
        ("state", "allowance", "reply"),
        [
            ("not_allowed", _allowance("user_managed", "untrusted"), allow.HAND_MANAGED_LINE),
            ("disabled", _allowance("disabled", "full"), allow.DISABLED_LINE),
        ],
    )
    def test_entries_the_plugin_leaves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str, allowance: Any, reply: str
    ) -> None:
        placed = self._wire(monkeypatch, tmp_path, state=state, allowance=allowance)
        chat = _Chat("allow")
        assert self._run(tmp_path, chat) == reply
        assert chat.asked == [] and placed == []

    def test_a_managed_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None))
        monkeypatch.setattr(allow, "config_is_managed", lambda: True)
        assert self._run(tmp_path, _Chat("allow")) == allow.MANAGED_LINE

    def test_not_set_up(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None), key=False)
        for name in ("AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        assert self._run(tmp_path, _Chat("allow")).startswith("AgentChat is not set up in this profile yet")

    def test_a_hermes_that_cannot_ask(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None))
        chat = _Chat()
        chat.ask_choice = None  # type: ignore[assignment,method-assign]
        reply = self._run(tmp_path, chat)
        assert placed == [] and "hermes agentchat allow in a terminal" in reply

    def test_a_failed_write(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None), outcome="failed")
        assert self._run(tmp_path, _Chat("allow")) == "AgentChat tools: nothing changed — why"

    def test_without_allow_it_says_the_state(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None))
        chat = _Chat("allow")
        reply = self._run(tmp_path, chat, args="")
        assert chat.asked == [] and placed == []
        assert reply.startswith("AgentChat tools for @alice: not allowed yet")

    def test_a_surprise_is_a_reply_not_a_traceback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def boom(home: Any, *, api_base: str) -> Any:
            raise RuntimeError("config backend down")

        save_key(api_key("slash_0000000000000000"), handle="alice", home=tmp_path)
        monkeypatch.setattr(allow, "read_switch", boom)
        assert "config backend down" in self._run(tmp_path, _Chat("allow"))

    def test_a_scope_that_cannot_be_entered_runs_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, state="not_allowed_yet", allowance=_allowance("missing", None))

        def refused(self: ProfileScope, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            raise ProfileScopeError("Hermes refused this profile's secret scope")

        monkeypatch.setattr(ProfileScope, "run", refused)
        chat = _Chat("allow")
        reply = self._run(tmp_path, chat)
        assert reply.startswith("AgentChat: this profile's scope could not be entered") and chat.asked == [] and placed == []

    def test_a_hermes_without_commands_registers_nothing(self, tmp_path: Path) -> None:
        allow.register_allow_command(object(), ProfileScope.capture(tmp_path))  # no register_command: no error

    def test_a_refused_registration_is_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        class Refusing:
            def register_command(self, *a: Any, **kw: Any) -> None:
                raise ValueError("conflicts with a built-in")

        allow.register_allow_command(Refusing(), ProfileScope.capture(tmp_path))
        assert any("/agentchat could not be registered" in r.getMessage() for r in caplog.records)

    def test_the_handler_takes_hermes_context_keywords(self, tmp_path: Path) -> None:
        """Hermes passes only the keywords a handler declares (``invoke_plugin_command``)."""
        import inspect

        chat = _Chat()
        allow.register_allow_command(chat, ProfileScope.capture(tmp_path))
        handler = chat.commands["agentchat"][0]
        params = inspect.signature(handler).parameters
        assert list(params) == ["raw_args", "session_id"]
        assert params["session_id"].kind is inspect.Parameter.KEYWORD_ONLY
