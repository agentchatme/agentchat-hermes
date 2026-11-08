"""Tests for ``agentchatme_hermes.agent_invoker`` — the Hermes side of receiving.

Receiving is the SDK kit's ``ReceiveLoop``; this module supplies its
callbacks. What must hold:

* ``call_model`` is the agent's own model (``ctx.llm`` when Hermes has it),
  inside the profile's runtime scope; a scope that cannot be entered raises
  (the loop then fails closed — nothing recorded);
* ``readiness`` says whether Hermes has registered the hosted tools, read in
  the profile's context;
* ``start_turn`` admits one turn and its ``done`` resolves when the turn's
  thread has ended — completed, failed, not started; a turn that overran
  and did not end after the interrupt is reported failed and frees the
  loop's slot, but its conversation stays held (the shared tracker) until
  its thread really ends; with too many such turns nothing is admitted;
* ``detach_turns`` (the loop going away) reports what is running and keeps
  live turns held;
* ``start_run`` (single-run mode) never overlaps a run still running;
* end to end through the real ``ReceiveLoop``: a reply turn is recorded by
  the D5 rule, an invite gets its own turn, and a stuck turn does not stop
  the next conversation's turn.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from agentchatme.integration import (
    ReceiveLoop,
    RunRequest,
    TrackedTurn,
    TurnRequest,
)
from agentchatme.types import Texts

from agentchatme_hermes import agent_invoker as invoker_module
from agentchatme_hermes import hosted_tools as hosted_tools_module
from agentchatme_hermes import turn_runner as turn_runner_module
from agentchatme_hermes.agent_invoker import _FALLBACK_MODEL, AgentInvoker, _coerce_model_string
from agentchatme_hermes.profile_scope import ProfileScopeError
from agentchatme_hermes.turn import MANUAL_POINTER
from agentchatme_hermes.types import AgentIdentity

if TYPE_CHECKING:
    from pathlib import Path

TEXTS = Texts.model_validate(
    {
        "version": "1",
        "manual": {"url": "https://agentchat.me/skill.md", "version": "2.1.2"},
        "wake_instruction": "You were woken. Call agentchat_inbox first.",
        "invite_prompt": "An invite is pending. Call agentchat_list_group_invites first.",
        "identity_line": "I am @me on AgentChat.",
        "identity_line_template": "I am @<handle> on AgentChat.",
        # The gate prompt's own words, served (contract v1): the SDK's
        # conformance sample, tool names spelled as served for runtime=hermes.
        "gate_call_preamble": 'You are triaging your AgentChat tray before any turn starts. This call has NO tools: do not call mcp__agentchat__agentchat_decide, do not open or send anything, and do not write a reply. Answer the question below for every card, in JSON only; the host records your verdicts from that JSON. Where the question says to record a verdict or to send, that happens later: for each conversation you mark "reply", a turn will be started in which you open it and reply.',
        "gate_call_answer_format": 'Answer with ONLY a JSON object, no prose and no code fences, in this shape: {"decisions": [{"conversation_id": "<conversation_id from a card>", "decision": "reply" | "dismiss" | "defer", "category": "<one of: open_request, new_info, goal_followup, closing, acknowledgement, not_addressed, no_action_needed, spam, other>"}]} — one entry per card, every card decided.',
    }
)


_REAL_PROBE = hosted_tools_module.hosted_tool_names


def _fake_registry(monkeypatch: pytest.MonkeyPatch, home_with_tools: str) -> Any:
    """A Hermes tool registry whose overlay depends on the HERMES_HOME override."""
    import contextvars
    import sys
    import types

    override: contextvars.ContextVar[str | None] = contextvars.ContextVar("fake_override", default=None)
    constants = types.ModuleType("hermes_constants")
    constants.set_hermes_home_override = lambda path: override.set(str(path))  # type: ignore[attr-defined]
    constants.reset_hermes_home_override = lambda token: override.reset(token)  # type: ignore[attr-defined]
    constants.get_hermes_home_override = lambda: override.get()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)

    class Registry:
        def get_tool_names_for_toolset(self, name: str) -> list[str]:
            # the overlay of the profile in context, as Hermes keys it
            if override.get() == home_with_tools and name == "mcp-agentchat":
                return ["mcp__agentchat__agentchat_inbox"]
            return []

        def get_all_tool_names(self) -> list[str]:
            return [*self.get_tool_names_for_toolset("mcp-agentchat"), "terminal"]

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # type: ignore[attr-defined]
    module = types.ModuleType("tools.registry")
    module.registry = Registry()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.registry", module)
    return override


class _FakeHermesScopes:
    """Hermes's per-profile scoping, faked faithfully enough to tell profiles apart.

    * ``hermes_constants``: the context-local HERMES_HOME override.
    * ``agent.secret_scope``: a context-local secret mapping; ``get_secret``
      reads it and, under multiplex with nothing bound, fails closed as the
      real module does (``UnscopedSecretError``).
    * ``gateway.run._profile_runtime_scope(home)``: override + that home's
      secret mapping; records every entry and exit (with the exception that
      ended it, if any).
    * the fallback's three pieces: ``hermes_cli.env_loader.
      hydrate_profile_secret_sources``, ``tui_gateway.launch_profile_policy.
      owner_secret_scope`` and ``tools.terminal_scope.
      install_and_reset_profile_terminal_scope`` — each recorded.
    * ``gateway.run_turn.GatewayTurnMixin._standalone_launch_scope()``.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, secrets: dict[str, dict[str, str]], *,
                 multiplex: bool = True, with_gateway_helper: bool = True,
                 helper_raises: Exception | None = None, helper_signature_drift: bool = False,
                 missing_pieces: tuple[str, ...] = (), standalone_raises: Exception | None = None) -> None:
        import contextlib
        import contextvars
        import sys
        import types

        self.secrets = secrets
        self.multiplex = multiplex
        self.runtime_scope_calls: list[str] = []
        self.runtime_scope_exits: list[str | None] = []
        self.standalone_calls = 0
        self.set_scope_calls: list[tuple[dict[str, str], str | None]] = []
        self.hydrated: list[str] = []
        self.terminal_scopes: list[str] = []
        self.override: contextvars.ContextVar[str | None] = contextvars.ContextVar("fake_override", default=None)
        self.scope: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("fake_secret_scope", default=None)
        fake = self

        def module(name: str, package: bool = False) -> Any:
            mod = types.ModuleType(name)
            if package:
                mod.__path__ = []  # type: ignore[attr-defined]
            return mod

        constants = module("hermes_constants")
        constants.set_hermes_home_override = lambda path: fake.override.set(str(path))  # type: ignore[attr-defined]
        constants.reset_hermes_home_override = lambda token: fake.override.reset(token)  # type: ignore[attr-defined]
        constants.get_hermes_home_override = lambda: fake.override.get()  # type: ignore[attr-defined]

        secret_scope = module("agent.secret_scope")

        def set_secret_scope(mapping: dict[str, str], *, profile_home: str | None = None) -> Any:
            fake.set_scope_calls.append((dict(mapping), profile_home))
            return fake.scope.set(dict(mapping))

        secret_scope.is_multiplex_active = lambda: fake.multiplex  # type: ignore[attr-defined]
        secret_scope.set_secret_scope = set_secret_scope  # type: ignore[attr-defined]
        secret_scope.reset_secret_scope = lambda token: fake.scope.reset(token)  # type: ignore[attr-defined]
        secret_scope.get_secret = fake.get_secret  # type: ignore[attr-defined]

        env_loader = module("hermes_cli.env_loader")
        env_loader.hydrate_profile_secret_sources = lambda home: fake.hydrated.append(str(home)) or {}  # type: ignore[attr-defined]
        launch_policy = module("tui_gateway.launch_profile_policy")
        launch_policy.owner_secret_scope = lambda home: dict(fake.secrets[str(home)])  # type: ignore[attr-defined]

        @contextlib.contextmanager
        def install_and_reset_profile_terminal_scope(home: Any, **_kw: Any) -> Any:
            fake.terminal_scopes.append(str(home))
            yield

        terminal_scope = module("tools.terminal_scope")
        terminal_scope.install_and_reset_profile_terminal_scope = install_and_reset_profile_terminal_scope  # type: ignore[attr-defined]

        @contextlib.contextmanager
        def _profile_runtime_scope(profile_home: Any, prepared_secret_scope: Any = None, **_kw: Any) -> Any:
            fake.runtime_scope_calls.append(str(profile_home))
            if helper_raises is not None:
                raise helper_raises
            home_token = fake.override.set(str(profile_home))
            secret_token = fake.scope.set(dict(fake.secrets[str(profile_home)]))
            try:
                yield
            except BaseException as exc:
                fake.runtime_scope_exits.append(type(exc).__name__)
                raise
            else:
                fake.runtime_scope_exits.append(None)
            finally:
                fake.scope.reset(secret_token)
                fake.override.reset(home_token)

        def _drifted(profile_home: Any, *, some_new_required_kw: Any) -> Any:
            raise AssertionError("never reached: the call itself fails")

        @contextlib.contextmanager
        def _standalone() -> Any:
            fake.standalone_calls += 1
            if standalone_raises is not None:
                raise standalone_raises
            yield

        gateway_run = module("gateway.run")
        gateway_run._profile_runtime_scope = _drifted if helper_signature_drift else _profile_runtime_scope  # type: ignore[attr-defined]
        run_turn = module("gateway.run_turn")
        run_turn.GatewayTurnMixin = type("GatewayTurnMixin", (), {"_standalone_launch_scope": staticmethod(_standalone)})  # type: ignore[attr-defined]

        modules = {
            "hermes_constants": constants,
            "agent": module("agent", package=True),
            "agent.secret_scope": secret_scope,
            "hermes_cli": module("hermes_cli", package=True),
            "hermes_cli.env_loader": env_loader,
            "tui_gateway": module("tui_gateway", package=True),
            "tui_gateway.launch_profile_policy": launch_policy,
            "tools": module("tools", package=True),
            "tools.terminal_scope": terminal_scope,
            "gateway": module("gateway", package=True),
            "gateway.run": gateway_run if with_gateway_helper else None,
            "gateway.run_turn": run_turn,
        }
        for name in missing_pieces:
            modules[name] = None
        for name, mod in modules.items():
            monkeypatch.setitem(sys.modules, name, mod)
        # One-time warnings are per process; each test starts fresh.
        from agentchatme_hermes import profile_scope

        monkeypatch.setattr(profile_scope, "_warned", set())

    def get_secret(self, name: str) -> str | None:
        bound = self.scope.get()
        if bound is not None:
            return bound.get(name)
        if self.multiplex:
            raise RuntimeError(f"UnscopedSecretError: get_secret({name!r}) with no profile secret scope while multiplexing")
        return f"process-env:{name}"




FAST = turn_runner_module.TurnLimits(idle_s=0.05, max_s=5.0, grace_s=0.05, poll_s=0.01)


class _Agent:
    """``AIAgent`` as the turn runner sees it. ``block`` holds the turn until
    ``release`` is set; ``obey`` lets Hermes' stop (``hard_interrupt``)
    release it."""

    def __init__(self, *, result: Any = None, block: bool = False, obey: bool = True, raises: Exception | None = None) -> None:
        self.result = result if result is not None else {"final_response": "ok", "messages": [
            {"role": "tool", "name": "mcp__agentchat__agentchat_get_conversation", "content": '{"result": "{}"}'},
            {"role": "tool", "name": "mcp__agentchat__agentchat_send_message", "content": '{"result": "{}"}'},
        ]}
        self.block = block
        self.obey = obey
        self.raises = raises
        self.release = threading.Event()
        self.started = threading.Event()
        self.prompts: list[str] = []
        self.interrupts: list[str | None] = []
        self.on_run: Any = None

    def run_conversation(self, prompt: str, conversation_history: list[Any]) -> Any:
        self.prompts.append(prompt)
        self.started.set()
        if self.on_run is not None:
            self.on_run()
        if self.raises is not None:
            raise self.raises
        if self.block:
            self.release.wait(10.0)
            return {"completed": False, "interrupted": True, "messages": []}
        return self.result

    def get_activity_summary(self) -> dict[str, Any]:
        return {"last_activity_at": 1.0, "api_call_count": 1, "current_tool": None}

    def hard_interrupt(self, message: str | None = None, *, tool_reason: str | None = None) -> None:
        self.interrupts.append(tool_reason)
        if self.obey:
            self.release.set()


def _invoker(monkeypatch: pytest.MonkeyPatch, agent: Any = None, *, config: Any = None, host_llm: Any = None, home: Any = None) -> AgentInvoker:
    inv = AgentInvoker(
        config=config or SimpleNamespace(),
        identity=AgentIdentity(handle="me"),
        runtime=SimpleNamespace(hermes_home=home),
        host_llm=host_llm,
    )
    inv._limits = FAST
    monkeypatch.setattr(inv, "_ensure_hermes_resolved", lambda: None)
    monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {"provider": "p"}))
    if agent is not None:
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: agent)
    # Hermes has the hosted tools registered unless a test says otherwise.
    monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", lambda: ["mcp__agentchat__agentchat_inbox"])
    return inv


def _reply_request(cid: str = "conv_a", anchor: str = "msg_a") -> TurnRequest:
    return TurnRequest(
        kind="reply",
        prompt=f"{TEXTS.wake_instruction}\n\nconversation_id: {cid}",
        texts=TEXTS,
        stopping=asyncio.Event(),
        conversation_id=cid,
        card={"conversation_id": cid},
        anchor=anchor,
    )


def _invite_request(invite_id: str = "ginv_1") -> TurnRequest:
    return TurnRequest(
        kind="invite",
        prompt=f"{TEXTS.invite_prompt}\n\ninvite_id: {invite_id}",
        texts=TEXTS,
        stopping=asyncio.Event(),
        invite={"invite_id": invite_id},
    )


async def _run(inv: AgentInvoker, request: TurnRequest) -> Any:
    """What the loop does: begin the tracker entry, admit, follow to the end."""
    tracked = TrackedTurn.invite(request.invite["invite_id"]) if request.kind == "invite" else TrackedTurn.reply(str(request.conversation_id), request.anchor)  # type: ignore[index]
    handle = inv.tracker.begin(tracked)
    assert handle is not None
    admission = await inv.start_turn(request)
    if not admission.started:
        inv.tracker.end(handle)
        return admission
    end = await inv.tracker.confirmed_end(handle, admission.done)
    inv.tracker.end(handle)
    return end


async def _until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


# ─── helpers that stay Hermes' ──────────────────────────────────────────────


class TestCoerceModelString:
    def test_nested_dict_with_default(self) -> None:
        assert _coerce_model_string({"default": "deepseek-v4-flash", "provider": "x"}) == "deepseek-v4-flash"

    def test_default_beats_model(self) -> None:
        assert _coerce_model_string({"default": "a", "model": "b"}) == "a"

    def test_model_when_no_default(self) -> None:
        assert _coerce_model_string({"model": "b"}) == "b"

    def test_flat_string(self) -> None:
        assert _coerce_model_string("flat") == "flat"

    @pytest.mark.parametrize("value", [None, {}, {"default": ""}, 7, []])
    def test_unusable_shapes_fall_back(self, value: Any) -> None:
        assert _coerce_model_string(value) == _FALLBACK_MODEL


class TestBuildAgent:
    def _build(self, monkeypatch: pytest.MonkeyPatch, agent_cls: Any, config: Any) -> Any:
        import sys
        import types

        module = types.ModuleType("run_agent")
        module.AIAgent = agent_cls  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "run_agent", module)
        inv = AgentInvoker(config=config, identity=AgentIdentity(handle="me"), runtime=SimpleNamespace())
        monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {}))
        return inv._build_agent("agentchat:wake:test")

    def test_run_budget_passed_when_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class NewAgent:
            def __init__(self, *, run_budget_seconds: float | None = None, **kwargs: Any) -> None:
                self.budget = run_budget_seconds
                self.kwargs = kwargs

        built = self._build(monkeypatch, NewAgent, SimpleNamespace(turn_max_s=1200.0))
        assert built.budget == 1200.0 and built.kwargs["platform"] == "agentchat"
        assert built.kwargs["load_soul_identity"] is True and built.kwargs["skip_context_files"] is True
        off = self._build(monkeypatch, NewAgent, SimpleNamespace(turn_max_s=0.0))
        assert off.budget is None, "no ceiling, no budget"

    def test_run_budget_omitted_for_an_older_hermes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class OldAgent:
            def __init__(self, *, model: str, session_id: str, session_db: Any, platform: str, quiet_mode: bool, load_soul_identity: bool, skip_context_files: bool) -> None:
                self.platform = platform

        built = self._build(monkeypatch, OldAgent, SimpleNamespace())
        assert built is not None and built.platform == "agentchat"

    def test_constructor_errors_are_a_turn_not_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class ExplodingAgent:
            def __init__(self, **_kwargs: Any) -> None:
                raise RuntimeError("no session db")

        assert self._build(monkeypatch, ExplodingAgent, SimpleNamespace()) is None


# ─── call_model ────────────────────────────────────────────────────────────


class TestCallModel:
    def test_ctx_llm_answers_and_no_credentials_are_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []

        class _PluginLlm:
            def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                calls.append({"messages": messages, **kwargs})
                return SimpleNamespace(text='{"decisions": []}')

        inv = _invoker(monkeypatch, host_llm=_PluginLlm)

        def no_resolve() -> Any:
            raise AssertionError("no config or credential read when ctx.llm is there")

        monkeypatch.setattr(inv, "_resolve_model_and_runtime", no_resolve)
        assert inv.call_model("THE PROMPT") == '{"decisions": []}'
        assert calls[0]["messages"] == [{"role": "user", "content": "THE PROMPT"}]

    def test_a_failing_accessor_falls_back_once(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        seen: list[dict[str, Any]] = []

        def fake_build_call_model(**kw: Any) -> Any:
            seen.append(kw)
            return lambda prompt: "answer"

        def broken() -> Any:
            raise ImportError("agent.plugin_llm")

        inv = _invoker(monkeypatch, host_llm=broken)
        monkeypatch.setattr(invoker_module, "build_call_model", fake_build_call_model)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            assert inv.call_model("p") == "answer"
            assert inv.call_model("p") == "answer"
        assert seen[0]["host_llm"] is None and seen[0]["main_runtime"] == {"model": "m", "provider": "p"}
        assert sum("ctx.llm" in r.getMessage() for r in caplog.records) == 1


# ─── readiness ─────────────────────────────────────────────────────────────


class TestReadiness:
    def test_ready_when_the_hosted_tools_are_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _invoker(monkeypatch).readiness().ready is True

    def test_not_ready_after_the_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch)
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", list)
        monkeypatch.setattr(hosted_tools_module, "HOSTED_TOOLS_WAIT_S", 0.05)
        monkeypatch.setattr(hosted_tools_module, "HOSTED_TOOLS_POLL_S", 0.01)
        monkeypatch.setattr(invoker_module, "wait_for_hosted_tools", lambda **kw: hosted_tools_module.wait_for_hosted_tools(timeout_s=0.05, poll_s=0.01, **kw))
        answer = inv.readiness()
        assert answer.ready is False and "not registered" in (answer.reason or "")

    def test_stopping_is_not_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch)
        inv.stop()
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", list)
        assert inv.readiness().ready is False

    def test_the_registry_is_read_in_the_profiles_context(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home_b = tmp_path / "profiles" / "b"
        _fake_registry(monkeypatch, str(home_b))
        inv = _invoker(monkeypatch, home=home_b)
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", _REAL_PROBE)
        assert inv.readiness().ready is True, "profile b's overlay has the tools"
        assert _REAL_PROBE() == [], "outside the context the overlay is invisible"


# ─── start_turn ────────────────────────────────────────────────────────────


class TestStartTurn:
    async def test_a_completed_turn_reports_its_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent()
        inv = _invoker(monkeypatch, agent)
        end = await _run(inv, _reply_request())
        assert end.status == "completed"
        assert end.agentchat_calls is not None and (end.agentchat_calls.made, end.agentchat_calls.failed) == (2, 0)
        assert agent.prompts == [f"{TEXTS.wake_instruction}\n\nconversation_id: conv_a\n\n{MANUAL_POINTER}"]
        assert inv.tracker.size == 0 and inv.hung_turns == 0
        assert inv.status()["last_turn"]["status"] == "completed"

    async def test_a_raising_turn_is_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch, _Agent(raises=RuntimeError("provider exploded")))
        end = await _run(inv, _reply_request())
        assert end.status == "failed" and "provider exploded" in (end.detail or "")

    async def test_an_agent_that_cannot_be_built_is_a_turn_not_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: None)
        end = await _run(inv, _reply_request())
        assert end.status == "failed" and (end.detail or "").startswith("not started")
        assert inv.tracker.size == 0

    async def test_a_scope_that_cannot_be_entered_runs_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _launch, up = tmp_path / "hermes", tmp_path / "hermes" / "profiles" / "up"
        _FakeHermesScopes(monkeypatch, {str(up): {}}, helper_raises=OSError("vault locked"))
        agent = _Agent()
        inv = _invoker(monkeypatch, agent, home=up)
        end = await _run(inv, _reply_request())
        assert end.status == "failed" and "profile scope unavailable" in (end.detail or "")
        assert agent.prompts == []

    async def test_an_interrupted_turn_that_ends_is_failed_with_no_hold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent(block=True, obey=True)
        inv = _invoker(monkeypatch, agent)
        inv._limits = turn_runner_module.TurnLimits(idle_s=0.05, max_s=5.0, grace_s=2.0, poll_s=0.01)
        end = await _run(inv, _reply_request())
        assert end.status == "failed" and agent.interrupts == [turn_runner_module.INTERRUPT_TOOL_REASON]
        assert inv.tracker.size == 0 and inv.hung_turns == 0

    async def test_a_stuck_turn_frees_the_slot_and_keeps_its_conversation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The handoff under the kit's release semantics: ``release`` lets go
        of the loop's entry (its wait ends with ``None``: nothing recorded;
        its slot is freed by the loop's release listener) and, in the same
        step, the invoker begins its own entry for the conversation."""
        agent = _Agent(block=True, obey=False)
        inv = _invoker(monkeypatch, agent)
        woken: list[str] = []
        released: list[str] = []
        inv.tracker.on_release(lambda handle: released.append(handle.key))
        inv.set_wake(lambda: woken.append("wake"))
        loop_handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert loop_handle is not None
        admission = await inv.start_turn(_reply_request("conv_a"))
        end = await inv.tracker.confirmed_end(loop_handle, admission.done)
        assert end is None, "released: the loop records nothing for it"
        assert not inv.tracker.holds(loop_handle), "the loop's entry is gone"
        assert released == ["conversation:conv_a"], "the release listeners heard it (the loop frees its slot)"
        inv.tracker.end(loop_handle)  # what the loop does next: a no-op, the entry is the invoker's now
        assert inv.tracker.conversation_running("conv_a"), "held by the invoker until the thread ends"
        assert inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a2")) is None, "no second turn for it"
        assert inv.hung_turns == 1 and inv.status()["hung_turns"] == 1

        agent.release.set()  # the stuck call finally returns
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))
        assert inv.hung_turns == 0 and woken == ["wake"], "the loop is woken to read the tray again"

    async def test_no_admission_while_too_many_turns_are_stuck(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_Agent(block=True, obey=False) for _ in range(invoker_module.MAX_HUNG_TURNS)]
        inv = _invoker(monkeypatch)
        built = iter(agents)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: next(built))
        for i in range(invoker_module.MAX_HUNG_TURNS):
            await _run(inv, _reply_request(f"conv_{i}", f"msg_{i}"))
        assert inv.hung_turns == invoker_module.MAX_HUNG_TURNS
        refused = await _run(inv, _reply_request("conv_new", "msg_new"))
        assert refused.started is False and "still running" in (refused.error or "")
        for agent in agents:
            agent.release.set()
        assert await _until(lambda: inv.hung_turns == 0)

    async def test_stopping_refuses_new_turns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch, _Agent())
        inv.stop()
        refused = await _run(inv, _reply_request())
        assert refused.started is False and refused.error == "stopping"

    async def test_stop_interrupts_a_running_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent(block=True, obey=True)
        inv = _invoker(monkeypatch, agent)
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, grace_s=2.0, poll_s=0.01)
        agent.on_run = lambda: threading.Timer(0.05, inv.stop).start()
        end = await _run(inv, _reply_request())
        assert end.status == "failed" and agent.interrupts == [turn_runner_module.INTERRUPT_STOP_REASON]

    async def test_an_invite_turn_has_its_own_key_and_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent()
        inv = _invoker(monkeypatch, agent)
        sessions: list[str] = []
        monkeypatch.setattr(inv, "_build_agent", lambda sid: sessions.append(sid) or agent)
        end = await _run(inv, _invite_request("ginv_9"))
        assert end.status == "completed" and sessions == ["agentchat:invite:ginv_9"]
        assert agent.prompts[0].endswith(MANUAL_POINTER) and "invite_id: ginv_9" in agent.prompts[0]

    async def test_detach_reports_running_turns_and_keeps_live_ones_held(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent(block=True, obey=False)
        inv = _invoker(monkeypatch, agent)
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, grace_s=0.05, poll_s=0.01)
        handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert handle is not None
        admission = await inv.start_turn(_reply_request())
        assert await _until(agent.started.is_set)
        inv.detach_turns()  # the loop is going away
        end = await inv.tracker.confirmed_end(handle, admission.done)
        inv.tracker.end(handle)
        assert end is None and not inv.tracker.holds(handle), "released to the invoker: nothing recorded"
        assert inv.tracker.conversation_running("conv_a"), "still held: its thread runs"
        inv.stop()
        agent.release.set()
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))


# ─── start_run (single-run mode) ───────────────────────────────────────────


class TestStartRun:
    def _request(self) -> RunRequest:
        return RunRequest(prompt=TEXTS.wake_instruction, texts=TEXTS, cards=1, invites=0, stopping=asyncio.Event())

    def test_a_completed_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent()
        end = _invoker(monkeypatch, agent).start_run(self._request())
        assert end.status == "completed" and agent.prompts[0].endswith(MANUAL_POINTER)

    def test_a_run_not_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: None)
        assert inv.start_run(self._request()).status == "not_started"

    def test_no_second_run_while_the_first_still_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent(block=True, obey=False)
        inv = _invoker(monkeypatch, agent)
        first = inv.start_run(self._request())
        assert first.status == "failed", "overran"
        second = inv.start_run(self._request())
        assert second.status == "not_started" and "still running" in (second.detail or "")
        agent.release.set()
        assert inv._run_finished is not None and inv._run_finished.wait(5.0)


# ─── profile scope ─────────────────────────────────────────────────────────


class TestProfileScope:
    """A served profile's gate call and turn resolve that profile's
    credentials, in the scope the gateway binds for that profile's own
    turns (``gateway/run.py`` ``_profile_runtime_scope``)."""

    @staticmethod
    def _homes(tmp_path: Path) -> tuple[Path, Path]:
        return tmp_path / "hermes", tmp_path / "hermes" / "profiles" / "up"

    @staticmethod
    def _secrets(launch: Path, up: Path) -> dict[str, dict[str, str]]:
        return {str(launch): {"LIVEFIRE_STUB_KEY": "key-launch"}, str(up): {"LIVEFIRE_STUB_KEY": "key-up"}}

    async def test_the_gate_and_the_turn_see_the_profiles_secret_and_nothing_leaks(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, self._secrets(launch, up))
        seen: dict[str, list[Any]] = {"gate": [], "turn": [], "home": []}
        agent = _Agent()
        agent.on_run = lambda: (seen["turn"].append(fake.get_secret("LIVEFIRE_STUB_KEY")), seen["home"].append(fake.override.get()))
        inv = _invoker(monkeypatch, agent, home=up)

        def resolve() -> tuple[str, dict[str, Any]]:
            seen["gate"].append(fake.get_secret("LIVEFIRE_STUB_KEY"))
            return "m", {}

        monkeypatch.setattr(inv, "_resolve_model_and_runtime", resolve)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: (lambda prompt: "{}"))
        inv.call_model("p")
        await _run(inv, _reply_request())
        assert seen["gate"] == ["key-up"] and seen["turn"] == ["key-up"] and seen["home"] == [str(up)]
        assert fake.runtime_scope_calls == [str(up), str(up)] and fake.runtime_scope_exits == [None, None]
        assert fake.scope.get() is None and fake.override.get() is None, "nothing left bound on this thread"

    def test_a_gate_whose_scope_cannot_be_entered_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _launch, up = self._homes(tmp_path)
        _FakeHermesScopes(monkeypatch, {str(up): {}}, helper_raises=OSError("vault locked"))
        inv = _invoker(monkeypatch, home=up)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: (lambda prompt: "{}"))
        with pytest.raises(ProfileScopeError):
            inv.call_model("p")

    def test_single_profile_gateway_uses_the_standalone_launch_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, self._secrets(launch, up), multiplex=False)
        inv = _invoker(monkeypatch, home=launch)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: (lambda prompt: "{}"))
        inv.call_model("p")
        assert fake.standalone_calls == 1 and fake.runtime_scope_calls == []


# ─── end to end, through the real ReceiveLoop ──────────────────────────────


class _Server:
    """The account as the server holds it: unread cards, pending invites,
    what was recorded and dismissed. Async, like the kit's client."""

    def __init__(self) -> None:
        self.unread: dict[str, str] = {}
        self.answered: dict[str, str] = {}
        self.invites: list[str] = []
        self.recorded: list[tuple[str, str, str | None]] = []
        self.dismissed: list[str] = []
        self.answer: dict[str, str] = {}
        self.inbox = SimpleNamespace(tray=self._tray)
        self.decisions = SimpleNamespace(record=self._record)
        self.groups = SimpleNamespace(dismiss_invite=self._dismiss)

    async def _tray(self, **_kw: Any) -> dict[str, Any]:
        read_through = {cid: anchor for cid, decision, anchor in self.recorded if decision in ("reply", "dismiss")}
        cards = []
        for cid, newest in self.unread.items():
            if read_through.get(cid) == newest:
                continue
            cards.append({
                "conversation_id": cid,
                "type": "direct",
                "counterparty": {"handle": "@peer"},
                "relationship": "established",
                "you_owe_reply": self.answered.get(cid) != newest,
                "mentioned_you": False,
                "unread": 1,
                "pace": {"last_60s": 0, "last_10m": 1},
                "preview": {"message_id": newest, "sender": "@peer", "text": "hello", "at": "2026-11-08T10:00:00.000Z"},
            })
        return {
            "version": 1,
            "you": "@me",
            "question": "Decide per card with agentchat_decide.",
            "categories": ["open_request", "no_action_needed"],
            "conversations": cards,
            "deferred": 0,
            "truncated": False,
            "invites": [
                {"invite_id": i, "group": {"id": "grp", "name": "Council", "member_count": 3}, "invited_by": "@carol", "at": "2026-11-08T10:00:00.000Z"}
                for i in self.invites
                if i not in self.dismissed
            ],
            "invites_truncated": False,
        }

    async def _record(self, conversation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append((conversation_id, kwargs["decision"], kwargs.get("through_message_id")))
        return {"decision": kwargs["decision"]}

    async def _dismiss(self, invite_id: str) -> dict[str, Any]:
        self.dismissed.append(invite_id)
        return {"dismissed": True}


def _gate_answer(server: _Server) -> Any:
    import json

    def call_model(prompt: str) -> str:
        cards = [cid for cid in server.unread if f'"conversation_id":"{cid}"' in prompt.replace(" ", "")]
        return json.dumps({"decisions": [{"conversation_id": cid, "decision": server.answer.get(cid, "reply")} for cid in cards]})

    return call_model


def _loop(inv: AgentInvoker, server: _Server) -> ReceiveLoop:
    return ReceiveLoop(
        server,
        TEXTS,
        "gate_call",
        call_model=inv.call_model,
        start_turn=inv.start_turn,
        socket=None,
        handle="me",
        readiness=inv.readiness,
        max_concurrent_turns=1,
        turn_tracker=inv.tracker,
        quiet_ms=1,
        max_ms=5,
    )


class TestThroughTheReceiveLoop:
    async def test_a_reply_turn_that_answered_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _Server()
        server.unread = {"conv_a": "msg_a"}
        agent = _Agent()
        agent.on_run = lambda: server.answered.__setitem__("conv_a", "msg_a")  # the turn sends
        inv = _invoker(monkeypatch, agent)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: _gate_answer(server))
        loop = _loop(inv, server)
        await loop.start()
        loop.wake()
        assert await _until(lambda: server.recorded == [("conv_a", "reply", "msg_a")])
        await loop.stop()
        assert agent.prompts and "conversation_id: conv_a" in agent.prompts[0]

    async def test_an_invite_gets_its_own_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _Server()
        server.invites = ["ginv_1"]
        agent = _Agent()
        agent.on_run = lambda: server.invites.clear()  # the turn answered the invite
        inv = _invoker(monkeypatch, agent)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: _gate_answer(server))
        loop = _loop(inv, server)
        await loop.start()
        loop.wake()
        assert await _until(lambda: bool(agent.prompts) and inv.tracker.size == 0)
        await loop.stop()
        assert "invite_id: ginv_1" in agent.prompts[0] and server.dismissed == []

    async def test_a_stuck_turn_does_not_stop_the_next_conversation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One turn at a time (max_concurrent_turns=1): the stuck turn for
        conv_a is reported failed after the interrupt, so conv_b's turn
        runs; conv_a stays held — never gated again — until its thread ends."""
        server = _Server()
        server.unread = {"conv_a": "msg_a", "conv_b": "msg_b"}
        stuck = _Agent(block=True, obey=False)
        fine = _Agent()
        fine.on_run = lambda: server.answered.__setitem__("conv_b", "msg_b")
        inv = _invoker(monkeypatch)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: stuck if not stuck.started.is_set() else fine)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: _gate_answer(server))
        loop = _loop(inv, server)
        inv.set_wake(loop.wake)
        await loop.start()
        loop.wake()
        assert await _until(lambda: ("conv_b", "reply", "msg_b") in server.recorded, timeout=10.0)
        assert inv.tracker.conversation_running("conv_a") and inv.hung_turns == 1
        status = loop.status()
        assert status.counters["turns_active"] == 0 and status.counters["turns_waiting"] == 0, "the slot was freed"
        assert [t.turn.conversation_id for t in status.turns] == ["conv_a"], "conv_a: held by the invoker"
        # More wakes while conv_a is held: it is never gated again.
        for _ in range(3):
            loop.wake()
            await loop.idle()
        assert len(stuck.prompts) == 1, "no second turn for conv_a while the first may still send"
        assert all(r[0] != "conv_a" for r in server.recorded)
        stuck.release.set()
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))
        await loop.stop()
