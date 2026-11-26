"""Tests for ``agentchatme_hermes.allow`` — the one switch.

May the agent use the AgentChat tools on its own? One question, asked of a
person (their own terminal, or ``/agentchat allow`` in a chat); never in an
agent's terminal; the tools' entry written only once someone decided, except
the one-time carry-over of a setup made before 0.3; and what the switch says
(:func:`allow.state_of`) for the pause, doctor and status.
"""
from __future__ import annotations

import builtins
import io
import sys
import types
from typing import TYPE_CHECKING, Any

import pytest
from fake_keys import api_key
from key_files import save_key

from agentchatme_hermes import allow, hosted_tools, mcp_registration
from agentchatme_hermes.mcp_registration import Allowance

if TYPE_CHECKING:
    from pathlib import Path


def _allowance(entry: str = "ours", trust: Any = "untrusted", via: Any = None) -> Allowance:
    return Allowance(entry, trust, via)  # type: ignore[arg-type]


# ─── who is at the terminal ────────────────────────────────────────────────


class TestAgentTerminal:
    @pytest.mark.parametrize(
        ("environ", "agent"),
        [
            ({}, False),
            ({"HERMES_SESSION_ID": ""}, False),
            ({"HERMES_SESSION_ID": "  "}, False),
            ({"HERMES_SESSION_ID": "20261126_101500_abc"}, True),
            ({"HERMES_SESSION_ID": "s1", "HERMES_DESKTOP_TERMINAL": "1"}, False),
            ({"HERMES_SESSION_ID": "s1", "HERMES_DESKTOP_TERMINAL": "0"}, True),
            # Every hermes process sets these itself: no signal.
            ({"HERMES_AGENT": "true", "AI_AGENT": "hermes-agent"}, False),
        ],
    )
    def test_the_session_id_marks_an_agents_terminal(self, environ: dict[str, str], agent: bool) -> None:
        assert allow.in_agent_terminal(environ) is agent

    def test_it_reads_the_process_environment_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_SESSION_ID", "s1")
        monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
        assert allow.in_agent_terminal() is True

    def test_stdin_is_tty_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("x"))
        assert allow.stdin_is_tty() is False
        monkeypatch.setattr(sys, "stdin", None)
        assert allow.stdin_is_tty() is False


class TestTheQuestion:
    """``[Y/n]``: Enter is a yes; end of input and Ctrl-C are a no."""

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
        ("answer", "yes"),
        [
            ("", True),
            ("y", True),
            (" Y ", True),
            ("yes", True),
            ("n", False),
            ("no", False),
            ("maybe", False),
            (EOFError(), False),
            (KeyboardInterrupt(), False),
        ],
    )
    def test_answers(self, monkeypatch: pytest.MonkeyPatch, answer: Any, yes: bool) -> None:
        asked = self._reader(monkeypatch, answer)
        assert allow.ask_in_terminal("Allow @alice to use AgentChat tools on its own?") is yes
        assert asked == ["  Allow @alice to use AgentChat tools on its own? [Y/n]: "]

    def test_without_hermes_plain_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "hermes_cli", None)
        monkeypatch.setitem(sys.modules, "hermes_cli.cli_output", None)

        def eof(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr(builtins, "input", eof)
        assert allow.ask_in_terminal("q?") is False
        monkeypatch.setattr(builtins, "input", lambda _prompt: "")
        assert allow.ask_in_terminal("q?") is True


class TestDecide:
    def _config(self, monkeypatch: pytest.MonkeyPatch, allowance: Any, *, managed: bool = False) -> None:
        def inspect(*, api_base: str) -> Allowance:
            if allowance is None:
                raise mcp_registration.McpRegistrationError("no hermes")
            return allowance  # type: ignore[no-any-return]

        monkeypatch.setattr(allow, "inspect_allowance", inspect)
        monkeypatch.setattr(allow, "config_is_managed", lambda: managed)

    def _decide(self, *, tty: bool = True, environ: Any = None, answer: bool = True) -> allow.SetupDecision:
        asked: list[str] = []

        def ask(question: str) -> bool:
            asked.append(question)
            return answer

        decision = allow.decide("alice", api_base="https://a.test", tty=tty, ask=ask, environ=environ or {})
        self.asked = asked
        return decision

    def test_a_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._config(monkeypatch, _allowance("missing", None))
        decision = self._decide(answer=True)
        assert decision == allow.SetupDecision("yes", "AgentChat tools allowed")
        assert self.asked == ["Allow @alice to use AgentChat tools on its own?"]

    def test_a_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._config(monkeypatch, _allowance("ours", "untrusted"))
        decision = self._decide(answer=False)
        assert decision.decision == "no"
        assert decision.line == (
            "AgentChat tools not allowed. @alice can read AgentChat but does not answer on its own. "
            "To allow later: send /agentchat allow in a chat with it."
        )

    def test_an_agents_terminal_is_never_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._config(monkeypatch, _allowance("missing", None))
        decision = self._decide(environ={"HERMES_SESSION_ID": "s1"})
        assert decision.decision is None and self.asked == []
        assert decision.line == (
            "AgentChat tools: not allowed yet. This is an agent's terminal, so the plugin does not ask here. "
            "The person who runs @alice allows them once by sending /agentchat allow in a chat with it."
        )

    def test_no_terminal_is_never_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._config(monkeypatch, _allowance("missing", None))
        decision = allow.decide("alice", api_base="https://a.test", tty=False, environ={}, cmd="hermes -p ops agentchat")
        assert decision.decision is None
        assert decision.line == (
            "AgentChat tools: not allowed yet (no terminal to ask in). To allow: send /agentchat allow in a "
            "chat with @alice, or run hermes -p ops agentchat allow in a terminal."
        )

    @pytest.mark.parametrize(
        ("allowance", "line"),
        [
            (_allowance("ours", "full", "trust"), allow.ALREADY_ALLOWED_LINE),
            (_allowance("ours", "unset", "no_trust_key"), allow.ALREADY_ALLOWED_LINE),
            (_allowance("ours", "untrusted", "approvals_off"), allow.ALREADY_ALLOWED_LINE),
            (_allowance("user_managed", "untrusted"), allow.HAND_MANAGED_LINE),
            (_allowance("disabled", "full"), allow.DISABLED_LINE),
        ],
    )
    def test_not_asked_when_there_is_nothing_to_decide(
        self, monkeypatch: pytest.MonkeyPatch, allowance: Allowance, line: str
    ) -> None:
        self._config(monkeypatch, allowance)
        decision = self._decide()
        assert decision == allow.SetupDecision(None, line) and self.asked == []

    def test_a_managed_config_is_not_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._config(monkeypatch, _allowance("missing", None), managed=True)
        assert self._decide() == allow.SetupDecision(None, allow.MANAGED_LINE)

    def test_an_unreadable_config_still_asks_the_person(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._config(monkeypatch, None)
        assert self._decide(answer=True).decision == "yes"

    def test_an_unknown_handle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._config(monkeypatch, _allowance("missing", None))
        allow.decide(None, api_base="https://a.test", tty=True, ask=lambda q: self.__dict__.setdefault("q", q) and True, environ={})
        assert self.__dict__["q"] == "Allow this agent to use AgentChat tools on its own?"


# ─── what the switch says ──────────────────────────────────────────────────


class TestState:
    @pytest.mark.parametrize(
        ("allowance", "live", "state"),
        [
            (None, None, "unknown"),
            (_allowance("missing", None), None, "not_allowed_yet"),
            (_allowance("disabled", "full"), None, "disabled"),
            (_allowance("ours", "untrusted"), None, "not_allowed"),
            (_allowance("ours", "untrusted"), "full", "not_allowed"),
            (_allowance("ours", "full", "trust"), None, "allowed"),
            (_allowance("ours", "full", "trust"), "full", "allowed"),
            (_allowance("ours", "full", "trust"), "untrusted", "restart_to_apply"),
            (_allowance("ours", "unset", "no_trust_key"), "untrusted", "restart_to_apply"),
            # Read by Hermes at each call: no restart needed.
            (_allowance("ours", "untrusted", "approvals_off"), "untrusted", "allowed"),
            (_allowance("ours", "untrusted", "yolo"), "untrusted", "allowed"),
            (_allowance("ours", "untrusted", "always_grant"), "untrusted", "allowed"),
            (_allowance("user_managed", "full", "trust"), "untrusted", "restart_to_apply"),
        ],
    )
    def test_state_of(self, allowance: Any, live: Any, state: str) -> None:
        assert allow.state_of(allowance, live) == state

    def test_paused_states(self) -> None:
        assert {"not_allowed_yet", "not_allowed", "restart_to_apply", "disabled"} == allow.PAUSED_STATES
        for state in allow.PAUSED_STATES:
            assert allow.reason(state) != state  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("state", "allowance", "carried", "words"),
        [
            ("allowed", _allowance("ours", "full", "trust"), False, "allowed (trust: full)"),
            (
                "allowed",
                _allowance("ours", "full", "trust"),
                True,
                "allowed (carried over from your setup under an earlier release)",
            ),
            ("allowed", _allowance("ours", "unset", "no_trust_key"), False, "which Hermes reads as full"),
            ("allowed", _allowance("ours", "untrusted", "approvals_off"), False, "approvals.mode: off"),
            ("allowed", _allowance("ours", "untrusted", "yolo"), False, "HERMES_YOLO_MODE"),
            ("allowed", _allowance("ours", "untrusted", "always_grant"), False, "/agentchat allow covers them all"),
            ("not_allowed_yet", _allowance("missing", None), False, "@alice has no AgentChat tools until the person allows them"),
            ("not_allowed", _allowance("ours", "untrusted"), False, "@alice can read AgentChat but does not answer on its own"),
            ("restart_to_apply", None, False, "/reload-mcp in a chat it serves, or run hermes gateway restart"),
            ("disabled", None, False, "disabled"),
            ("unknown", None, False, "not known"),
        ],
    )
    def test_describe(self, state: Any, allowance: Any, carried: bool, words: str) -> None:
        assert words in allow.describe(state, allowance, "alice", carried=carried)

    def test_describe_names_the_profile_command(self) -> None:
        text = allow.describe("not_allowed", _allowance(), "alice", cmd="hermes -p ops agentchat")
        assert "run hermes -p ops agentchat allow" in text


# ─── the record and the one-time carry-over ────────────────────────────────


class TestFirstPlacement:
    def test_a_setup_from_before_0_3_is_carried_over(self, tmp_path: Path) -> None:
        assert allow.first_placement(tmp_path, entry="missing", legacy_key_set=True) == "carried_over"
        assert allow.read_record(tmp_path) is None, "recorded when the entry is written"

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
        allow.write_record(tmp_path, how="asked", trust="untrusted")
        assert allow.first_placement(tmp_path, entry="missing", legacy_key_set=True) is None

    def test_the_record_is_the_plugins_own_private_file(self, tmp_path: Path, assert_private_file: Any) -> None:
        allow.write_record(tmp_path, how="carried_over", trust="full")
        path = tmp_path / "agentchat" / "mcp-entry.json"
        assert allow.record_path(tmp_path) == path
        assert_private_file(path)
        record = allow.read_record(tmp_path)
        assert record is not None
        assert {k: record[k] for k in ("version", "how", "trust")} == {"version": 1, "how": "carried_over", "trust": "full"}
        assert allow.carried_over(tmp_path) is True

    def test_it_survives_logout(self, tmp_path: Path) -> None:
        from agentchatme_hermes import key_store

        save_key(api_key("logout_0000000000000000"), home=tmp_path)
        allow.write_record(tmp_path, how="carried_over", trust="full")
        assert key_store.delete_store(tmp_path) is True
        assert allow.carried_over(tmp_path) is True

    def test_an_unwritable_home_is_logged_not_raised(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        blocker = tmp_path / "agentchat"
        blocker.write_text("a file where the directory goes", encoding="utf-8")
        allow.write_record(tmp_path, how="asked", trust="full")
        assert any("could not write" in r.getMessage() for r in caplog.records)

    def test_the_legacy_name_is_read_through_the_profiles_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import profile_scope

        monkeypatch.setattr(profile_scope, "resolve_setting", lambda name: "ac_x" if name == "AGENTCHATME_API_KEY" else None)
        assert allow.legacy_key_set() is True
        monkeypatch.setattr(profile_scope, "resolve_setting", lambda name: "  ")
        assert allow.legacy_key_set() is False

        def boom(name: str) -> str:
            raise RuntimeError("unscoped")

        monkeypatch.setattr(profile_scope, "resolve_setting", boom)
        assert allow.legacy_key_set() is False


# ─── the tier Hermes holds ─────────────────────────────────────────────────


class TestLiveTier:
    def test_a_registration_keeps_the_tier_it_was_first_seen_with(self) -> None:
        tier, first = allow.LiveTier(), object()
        assert tier.observe(first, "untrusted") == "untrusted"
        assert tier.observe(first, "full") == "untrusted", "the same registration: Hermes keeps its tier"
        again = object()
        assert tier.observe(again, "full") == "full", "registered again (/reload-mcp): read anew"
        assert tier.observe(again, "untrusted") == "full"

    def test_no_registration_is_not_known(self) -> None:
        tier, entry = allow.LiveTier(), object()
        tier.observe(entry, "untrusted")
        assert tier.observe(None, "full") is None
        assert tier.observe(entry, "full") == "full", "gone and back: a registration seen anew"

    def test_an_entry_without_trust_is_full(self) -> None:
        assert allow.LiveTier().observe(object(), "unset") == "full"
        assert allow.LiveTier().observe(object(), None) is None

    def test_one_tracker_per_profile(self, tmp_path: Path) -> None:
        assert allow.live_tier(tmp_path / "a") is allow.live_tier(tmp_path / "a")
        assert allow.live_tier(tmp_path / "a") is not allow.live_tier(tmp_path / "b")

    def test_read_switch(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        entries = [object()]
        monkeypatch.setattr(hosted_tools, "hosted_tool_entry", lambda: entries[0])
        trust = ["untrusted"]
        monkeypatch.setattr(
            allow, "inspect_allowance", lambda *, api_base: _allowance("ours", trust[0], "trust" if trust[0] == "full" else None)
        )
        home = tmp_path / "rs"
        assert allow.read_switch(home, api_base="https://a.test")[0] == "not_allowed"
        trust[0] = "full"
        assert allow.read_switch(home, api_base="https://a.test")[0] == "restart_to_apply"
        entries[0] = object()
        assert allow.read_switch(home, api_base="https://a.test")[0] == "allowed"

    def test_read_switch_never_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def boom(*, api_base: str) -> Allowance:
            raise mcp_registration.McpRegistrationError("no hermes")

        monkeypatch.setattr(allow, "inspect_allowance", boom)
        assert allow.read_switch(tmp_path, api_base="https://a.test") == ("unknown", None)

    def test_the_registry_entry_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        entry = object()
        table = {"mcp__agentchat__agentchat_send_message": entry, "mcp__agentchat__agentchat_inbox": "inbox"}
        registry = types.SimpleNamespace(get_entry=table.get)
        module = types.ModuleType("tools.registry")
        module.registry = registry  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
        monkeypatch.setitem(sys.modules, "tools.registry", module)
        assert hosted_tools.hosted_tool_entry() is entry
        del table["mcp__agentchat__agentchat_send_message"]
        monkeypatch.setattr(hosted_tools, "hosted_tool_names", lambda: ["mcp__agentchat__agentchat_inbox"])
        assert hosted_tools.hosted_tool_entry() == "inbox", "filtered out: another of the server's tools"
        monkeypatch.setitem(sys.modules, "tools.registry", None)
        assert hosted_tools.hosted_tool_entry() is None


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
    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
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
        monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: (allow.state_of(allowance), allowance))
        monkeypatch.setattr(allow, "config_is_managed", lambda: False)
        monkeypatch.setattr(allow, "observe_live", lambda home, a: placed.append("observed"))
        status = "failed" if outcome == "failed" else "changed"
        monkeypatch.setattr(
            placement,
            "place_mcp",
            lambda api_base, decision=None: placed.append(decision) or PlacementStep("mcp", status, "why", outcome),
        )
        monkeypatch.setattr(leader_lock, "is_hermes_gateway_process", lambda: gateway)
        return placed

    def _run(self, tmp_path: Path, chat: _Chat, args: str = "allow") -> str:
        allow.register_allow_command(chat, tmp_path)
        handler, _description, hint = chat.commands["agentchat"]
        assert hint == "allow"
        return str(handler(args))

    def test_allow_with_no_entry_writes_it_full(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, allowance=_allowance("missing", None))
        chat = _Chat("allow")
        reply = self._run(tmp_path, chat)
        assert chat.asked == [
            (
                "Allow @alice to use AgentChat tools on its own?",
                [{"value": "allow", "label": "Allow"}, {"value": "no", "label": "Don't allow"}],
            )
        ]
        assert placed == ["observed", "yes"]
        assert reply == "AgentChat tools allowed for @alice. The gateway connects them within about a minute."

    def test_allow_on_an_untrusted_entry_names_reload_in_the_gateway(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        placed = self._wire(monkeypatch, tmp_path, allowance=_allowance("ours", "untrusted"), outcome="allowed")
        reply = self._run(tmp_path, _Chat("allow"))
        assert placed == ["observed", "yes"], "observed before the change"
        assert reply == "AgentChat tools allowed for @alice. Send /reload-mcp to apply it now."

    def test_allow_on_an_untrusted_entry_outside_the_gateway(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._wire(monkeypatch, tmp_path, allowance=_allowance("ours", "untrusted"), outcome="allowed", gateway=False)
        reply = self._run(tmp_path, _Chat("allow"))
        assert "/reload-mcp in a chat the gateway serves, or hermes gateway restart" in reply

    def test_dont_allow_writes_untrusted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, allowance=_allowance("missing", None))
        reply = self._run(tmp_path, _Chat("no"))
        assert placed == ["no"]
        assert reply.startswith("AgentChat tools not allowed. @alice can read AgentChat")

    def test_unanswered_changes_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, allowance=_allowance("missing", None))
        reply = self._run(tmp_path, _Chat(None))
        assert placed == [] and reply == "Nothing changed: the question was not answered."

    def test_already_allowed_asks_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, allowance=_allowance("ours", "full", "trust"))
        chat = _Chat("allow")
        assert self._run(tmp_path, chat) == "AgentChat tools are already allowed for @alice."
        assert chat.asked == [] and placed == []

    @pytest.mark.parametrize(
        ("allowance", "reply"),
        [
            (_allowance("user_managed", "untrusted"), allow.HAND_MANAGED_LINE),
            (_allowance("disabled", "full"), allow.DISABLED_LINE),
        ],
    )
    def test_entries_the_plugin_leaves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allowance: Any, reply: str
    ) -> None:
        placed = self._wire(monkeypatch, tmp_path, allowance=allowance)
        chat = _Chat("allow")
        assert self._run(tmp_path, chat) == reply
        assert chat.asked == [] and placed == []

    def test_a_managed_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._wire(monkeypatch, tmp_path, allowance=_allowance("missing", None))
        monkeypatch.setattr(allow, "config_is_managed", lambda: True)
        assert self._run(tmp_path, _Chat("allow")) == allow.MANAGED_LINE

    def test_a_restart_still_to_apply(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(allow, "read_switch", lambda home, *, api_base: ("restart_to_apply", _allowance("ours", "full", "trust")))
        save_key(api_key("slash_0000000000000000"), handle="alice", home=tmp_path)
        chat = _Chat("allow")
        reply = self._run(tmp_path, chat)
        assert chat.asked == [] and "/reload-mcp" in reply

    def test_not_set_up(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._wire(monkeypatch, tmp_path, allowance=_allowance("missing", None), key=False)
        for name in ("AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        assert self._run(tmp_path, _Chat("allow")).startswith("AgentChat is not set up in this profile yet")

    def test_a_hermes_that_cannot_ask(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, allowance=_allowance("missing", None))
        chat = _Chat()
        chat.ask_choice = None  # type: ignore[assignment,method-assign]
        reply = self._run(tmp_path, chat)
        assert placed == [] and "hermes agentchat allow in a terminal" in reply

    def test_a_failed_write(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._wire(monkeypatch, tmp_path, allowance=_allowance("missing", None), outcome="failed")
        assert self._run(tmp_path, _Chat("allow")) == "AgentChat tools: nothing changed — why"

    def test_without_allow_it_says_the_state(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        placed = self._wire(monkeypatch, tmp_path, allowance=_allowance("missing", None))
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

    def test_a_hermes_without_commands_registers_nothing(self, tmp_path: Path) -> None:
        allow.register_allow_command(object(), tmp_path)  # no register_command: no error

    def test_a_refused_registration_is_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        class Refusing:
            def register_command(self, *a: Any, **kw: Any) -> None:
                raise ValueError("conflicts with a built-in")

        allow.register_allow_command(Refusing(), tmp_path)
        assert any("/agentchat could not be registered" in r.getMessage() for r in caplog.records)
