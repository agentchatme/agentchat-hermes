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
import sys
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
from hermes_doubles import FakeSecretScope

from agentchatme_hermes import agent_invoker as invoker_module
from agentchatme_hermes import hosted_tools as hosted_tools_module
from agentchatme_hermes import turn_runner as turn_runner_module
from agentchatme_hermes.agent_invoker import _FALLBACK_MODEL, AgentInvoker, _coerce_model_string
from agentchatme_hermes.profile_scope import ProfileScope, ProfileScopeError
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
        self.after_block: Any = None
        """What a blocked turn returns once released (default: interrupted)."""

    def run_conversation(self, prompt: str, conversation_history: list[Any]) -> Any:
        self.prompts.append(prompt)
        self.started.set()
        if self.on_run is not None:
            self.on_run()
        if self.raises is not None:
            raise self.raises
        if self.block:
            self.release.wait(10.0)
            if self.after_block is not None:
                return self.after_block
            return {"completed": False, "interrupted": True, "messages": []}
        return self.result

    def get_activity_summary(self) -> dict[str, Any]:
        return {"last_activity_at": 1.0, "api_call_count": 1, "current_tool": None}

    def hard_interrupt(self, message: str | None = None, *, tool_reason: str | None = None) -> None:
        self.interrupts.append(tool_reason)
        if self.obey:
            self.release.set()


def _invoker(
    monkeypatch: pytest.MonkeyPatch,
    agent: Any = None,
    *,
    config: Any = None,
    host_llm: Any = None,
    home: Any = None,
    scope: ProfileScope | None = None,
) -> AgentInvoker:
    inv = AgentInvoker(
        config=config or SimpleNamespace(),
        identity=AgentIdentity(handle="me"),
        runtime=SimpleNamespace(hermes_home=home),
        host_llm=host_llm,
        scope=scope,
    )
    inv._limits = FAST
    monkeypatch.setattr(inv, "_ensure_hermes_resolved", lambda: None)
    monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {"provider": "p"}))
    monkeypatch.setattr(inv, "_turn_runtime_problem", lambda: None)
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
            return lambda prompt, **_: "answer"

        def broken() -> Any:
            raise ImportError("agent.plugin_llm")

        inv = _invoker(monkeypatch, host_llm=broken)
        monkeypatch.setattr(invoker_module, "build_call_model", fake_build_call_model)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            assert inv.call_model("p") == "answer"
            assert inv.call_model("p") == "answer"
        assert seen[0]["host_llm"] is None and seen[0]["main_runtime"] == {"model": "m", "provider": "p"}
        assert sum("ctx.llm" in r.getMessage() for r in caplog.records) == 1


# ─── check_runtime (could not process) ─────────────────────────────────────

_TOOL_KWARGS = {"tools", "tool_choice", "functions", "function_call", "tool_schemas", "parallel_tool_calls"}


class TestCheckRuntime:
    """The kit's runtime check: one minimal completion through the gate's
    own path, never a login or version check; its answer is discarded."""

    async def test_one_minimal_call_on_ctx_llm_like_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []

        class _PluginLlm:
            def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                calls.append({"messages": messages, **kwargs})
                return SimpleNamespace(text="OK")

        inv = _invoker(monkeypatch, host_llm=_PluginLlm)
        ready = await inv.check_runtime()
        assert ready.ready is True
        assert len(calls) == 1
        call = calls[0]
        assert call["messages"] == [{"role": "user", "content": "Reply with the single word OK."}]
        assert call["max_tokens"] == 2048, "the gate's cap: a model that reasons first needs room"
        assert call["timeout"] == 45.0 and call["temperature"] == 0.0
        assert call["purpose"] == "agentchat runtime check"
        assert not (_TOOL_KWARGS & set(call)), "no tools"
        assert not ({"provider", "model", "agent_id", "profile"} & set(call)), "the profile's own model, as the gate"

    async def test_the_fallback_is_the_gates_auxiliary_client_on_the_profiles_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[dict[str, Any]] = []
        prompts: list[str] = []

        def fake_build_call_model(**kw: Any) -> Any:
            seen.append(kw)

            def call(prompt: str, **_: Any) -> str:
                prompts.append(prompt)
                return "Sure, OK."

            return call

        inv = _invoker(monkeypatch)
        monkeypatch.setattr(invoker_module, "build_call_model", fake_build_call_model)
        assert (await inv.check_runtime()).ready is True, "any answer passes; it is discarded"
        assert seen[0]["host_llm"] is None and seen[0]["main_runtime"] == {"model": "m", "provider": "p"}
        assert seen[0]["timeout_s"] == 45.0 and seen[0]["max_tokens"] == 2048
        assert seen[0]["purpose"] == "agentchat runtime check"
        assert seen[0]["gate_loop"] is inv._gate_loop, "the gate's own loop"
        assert prompts == ["Reply with the single word OK."]

    @pytest.mark.parametrize(
        ("answer", "reason"),
        [("   ", "the model answered with no text"), (RuntimeError("401 credentials rejected"), "RuntimeError: 401 credentials rejected")],
    )
    async def test_an_empty_answer_or_a_raised_call_fails_it(self, monkeypatch: pytest.MonkeyPatch, answer: Any, reason: str) -> None:
        def call(prompt: str, **_: Any) -> str:
            if isinstance(answer, Exception):
                raise answer
            return str(answer)

        inv = _invoker(monkeypatch)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: call)
        ready = await inv.check_runtime()
        assert ready.ready is False and ready.reason == reason

    async def test_a_turn_model_that_cannot_be_resolved_fails_it_without_a_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Turns run on the model resolved from the profile's config; when
        that falls back (no credentials), every turn fails while ctx.llm may
        still answer, so the check must not pass."""

        def never(**_kw: Any) -> Any:
            raise AssertionError("no model call when the turns' own model cannot be resolved")

        inv = AgentInvoker(config=SimpleNamespace(), identity=AgentIdentity(handle="me"), runtime=SimpleNamespace(hermes_home=None))
        monkeypatch.setattr(invoker_module, "build_call_model", never)
        # Hermes' config helpers do not import (as outside Hermes). Forced, so
        # the nightly legs, which install Hermes, take the same branch.
        monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
        monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", None)
        ready = await inv.check_runtime()
        assert ready.ready is False and ready.reason == "Hermes' config helpers are unavailable: a turn has no model of its own"
        monkeypatch.setattr(inv, "_resolve_turn_runtime", lambda: ("claude-sonnet-4-6", {}, "the profile's model or provider could not be resolved for a turn"))
        assert (await inv.check_runtime()).reason == "the profile's model or provider could not be resolved for a turn"

    async def test_a_stopped_invoker_makes_no_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def never(**_kw: Any) -> Any:
            raise AssertionError("no call once stopping")

        inv = _invoker(monkeypatch)
        monkeypatch.setattr(invoker_module, "build_call_model", never)
        inv.stop()
        ready = await inv.check_runtime()
        assert ready.ready is False and ready.reason == "stopping"

    async def test_when_the_loop_stops_waiting_the_call_is_told_to_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        told: list[bool] = []

        def wedged(prompt: str, *, cancel: threading.Event | None = None) -> str:
            assert cancel is not None, "the check hands the call a cancel handle, like the gate's"
            told.append(cancel.wait(5.0))
            return "OK"

        inv = _invoker(monkeypatch)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: wedged)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(inv.check_runtime(), 0.1)
        assert await _until(lambda: told == [True])


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

    def test_the_hold_runs_in_the_profiles_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home = tmp_path / "profiles" / "b"
        fake = FakeSecretScope(multiplex=True)
        fake.install(monkeypatch)
        inv = _invoker(monkeypatch, home=home)
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(
            invoker_module,
            "wait_for_hosted_tools",
            lambda **kw: seen.append({**kw, "bound": fake.bound.get() is not None}) or True,
        )
        assert inv.readiness().ready is True
        assert seen == [{"label": "turn", "stopped": inv.stopping, "bound": True}]
        assert fake.binds == [str(home)]

    def test_a_hold_whose_scope_cannot_be_entered_is_not_ready(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = FakeSecretScope(multiplex=True)
        fake.install(monkeypatch)
        fake.fail_set = RuntimeError("hosted scope")
        answer = _invoker(monkeypatch, home=tmp_path).readiness()
        assert answer.ready is False and "profile scope unavailable" in (answer.reason or "")

    def test_the_status_reports_hermes_reading_of_the_server(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        inv = _invoker(monkeypatch, home=tmp_path / "profiles" / "b")

        def state() -> hosted_tools_module.ServerState:
            return hosted_tools_module.ServerState(status="failed", error="offline")

        monkeypatch.setattr(invoker_module, "hosted_server_state", state)
        status = inv.hosted_tools_status()
        assert status is not None and status["status"] == "failed" and status["error"] == "offline"
        assert "reconnect" not in status, "the plugin asks Hermes for no reconnect"

    def test_stopping_is_not_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch)
        inv.stop()
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", list)
        assert inv.readiness().ready is False

    def test_the_registry_is_read_in_the_profiles_context(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The scope captured where Hermes loaded the plugin for profile b (its
        home override) is the one the hold reads the registry in."""
        home_b = tmp_path / "profiles" / "b"
        override = _fake_registry(monkeypatch, str(home_b))
        token = override.set(str(home_b))  # as Hermes' loader binds it around register()
        try:
            scope = ProfileScope.capture(home_b)
        finally:
            override.reset(token)
        inv = _invoker(monkeypatch, home=home_b, scope=scope)
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", _REAL_PROBE)
        assert inv.readiness().ready is True, "profile b's overlay has the tools"
        assert _REAL_PROBE() == [], "outside the context the overlay is invisible"


class TestToolsSwitch:
    """No AgentChat work while the person has not allowed the tools: no gate
    call, no turn (readiness says not ready before the hold); a restart to
    apply is told apart by the registration Hermes holds."""

    def _switch(self, monkeypatch: pytest.MonkeyPatch, trust: Any, *, entry: str = "ours", via: Any = None) -> list[Any]:
        from agentchatme_hermes import allow
        from agentchatme_hermes.mcp_registration import Allowance

        box = [Allowance(entry, trust, via)]  # type: ignore[arg-type]
        monkeypatch.setattr(allow, "inspect_allowance", lambda *, api_base: box[0])
        return box

    def _held(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        held: list[Any] = []
        monkeypatch.setattr(invoker_module, "wait_for_hosted_tools", lambda **kw: held.append(kw) or True)
        return held

    @pytest.mark.parametrize(
        ("entry", "trust", "words"),
        [("missing", None, "not allowed yet"), ("ours", "untrusted", "are not allowed"), ("disabled", "full", "disabled")],
    )
    def test_paused_before_the_hold(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str, trust: Any, words: str
    ) -> None:
        inv = _invoker(monkeypatch, home=tmp_path / "p1")
        self._switch(monkeypatch, trust, entry=entry)
        held = self._held(monkeypatch)
        answer = inv.readiness()
        assert answer.ready is False and words in (answer.reason or "")
        assert held == [], "no 30 s hold, no gate call"

    @pytest.mark.parametrize(("trust", "via"), [("full", "trust"), ("unset", "no_trust_key"), ("untrusted", "approvals_off")])
    def test_allowed_goes_on_to_the_hold(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trust: Any, via: Any) -> None:
        inv = _invoker(monkeypatch, home=tmp_path / "p2")
        self._switch(monkeypatch, trust, via=via)
        held = self._held(monkeypatch)
        assert inv.readiness().ready is True and len(held) == 1

    def test_a_yes_being_applied_pauses_and_is_finished_here(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes import allow

        home = tmp_path / "p3"
        inv = _invoker(monkeypatch, home=home)
        self._switch(monkeypatch, None, entry="missing")
        allow.write_record(home, how="asked", trust="full", decision="yes", pending=True)
        started: list[Any] = []
        monkeypatch.setattr(allow, "start_completion", lambda scope, *, api_base: started.append(scope.home))
        held = self._held(monkeypatch)
        answer = inv.readiness()
        assert answer.ready is False and "being applied" in (answer.reason or "")
        assert held == [] and started == [home]

    def test_a_yes_written_but_not_in_effect_pauses(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Review: trust: full is in config.yaml, but the running gateway that
        holds the untrusted connection has not restarted — no turn starts
        (each write would ask, and nobody answers in an AgentChat turn)."""
        from agentchatme_hermes import allow

        home = tmp_path / "p4"
        inv = _invoker(monkeypatch, home=home)
        self._switch(monkeypatch, "full", via="trust")
        allow.write_record(home, how="asked", trust="full", decision="yes", restart_gateway=(4242, 1000))
        monkeypatch.setattr(allow, "gateway_seen", lambda home: (4242, 1000))
        started: list[Any] = []
        monkeypatch.setattr(allow, "start_completion", lambda scope, *, api_base: started.append(scope.home))
        held = self._held(monkeypatch)
        answer = inv.readiness()
        assert answer.ready is False and allow.RESTART_TO_APPLY in (answer.reason or "")
        assert held == [] and started == [], "nothing pending to complete: only that gateway's restart applies it"
        monkeypatch.setattr(allow, "gateway_seen", lambda home: "unknown")  # cannot be told: still paused
        assert inv.readiness().ready is False
        monkeypatch.setattr(allow, "gateway_seen", lambda home: (4242, 99999))  # restarted
        assert inv.readiness().ready is True

    def test_the_minute_check_applies_a_yes_this_gateway_holds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Lead review: an already active runtime (its health record stale when
        the yes was written in place) is the gateway holding the untrusted
        connection: its minute check takes the two steps itself."""
        import os

        from agentchatme_hermes import allow, mcp_registration
        from agentchatme_hermes.mcp_registration import Allowance

        home = tmp_path / "p5"
        inv = _invoker(monkeypatch, home=home)
        box = self._switch(monkeypatch, "full", via="trust")
        me = (os.getpid(), 777)
        allow.write_record(home, how="asked", trust="full", decision="yes", restart_gateway=me)
        monkeypatch.setattr(allow, "gateway_seen", lambda home: me)
        monkeypatch.setattr(allow, "process_start_time", lambda pid: 777)

        def removed(*, api_base: str) -> bool:
            box[0] = Allowance("missing", None, None)
            return True

        monkeypatch.setattr(mcp_registration, "uninstall_entry", removed)
        started: list[Any] = []
        monkeypatch.setattr(allow, "start_completion", lambda scope, *, api_base: started.append(scope.home))
        self._registered(monkeypatch, ["mcp__agentchat__agentchat_inbox"])
        assert inv.check_tools() == "applying"
        assert started and started[0] == home
        record = allow.read_record(home)
        assert record is not None and record["pending"] is True and "restart_gateway" not in record

    def _registered(self, monkeypatch: pytest.MonkeyPatch, names: list[str] | None = None) -> list[list[str] | None]:
        """What Hermes has registered of the hosted tools (``hosted_tool_names``)."""
        box: list[list[str] | None] = [[] if names is None else names]
        monkeypatch.setattr(invoker_module, "hosted_tool_names", lambda: box[0])
        return box

    def test_the_pause_ending_wakes_the_loop_once_the_tools_are_there(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The cards that waited on the tray are gated as soon as the tools are
        allowed and registered: the minute check wakes the loop, once."""
        from agentchatme_hermes.mcp_registration import Allowance

        inv = _invoker(monkeypatch, home=tmp_path / "p6")
        woken: list[str] = []
        inv.set_wake(lambda: woken.append("wake"))
        box = self._switch(monkeypatch, "untrusted")
        self._held(monkeypatch)
        self._registered(monkeypatch, ["mcp_agentchat_agentchat_inbox"])
        assert inv.readiness().ready is False
        assert woken == []
        box[0] = Allowance("ours", "full", "trust")
        inv.check_tools()  # the receiver's minute check sees it first
        assert woken == ["wake"]
        assert inv.readiness().ready is True
        inv.check_tools()
        assert woken == ["wake"], "once"
        inv.set_wake(None)

    def test_a_yes_after_not_allowed_yet_is_woken_when_hermes_connects_the_tools(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The sequence that lost the wake: no entry (paused); the person says
        yes; the entry is written, but Hermes' watcher connects it up to a
        minute later — a wake at the switch change ran a cycle whose hold
        ended tools_unavailable, and nothing woke the loop again. Now the
        minute check wakes it once the tools are registered."""
        from agentchatme_hermes.mcp_registration import Allowance

        inv = _invoker(monkeypatch, home=tmp_path / "p8")
        woken: list[str] = []
        inv.set_wake(lambda: woken.append("wake"))
        box = self._switch(monkeypatch, None, entry="missing")
        registered = self._registered(monkeypatch)
        assert inv.readiness().ready is False  # paused: not allowed yet
        box[0] = Allowance("ours", "full", "trust")  # the yes: written trust: full
        inv.check_tools()
        assert woken == [], "not registered yet: a cycle now would only hold and fail"
        monkeypatch.setattr(invoker_module, "wait_for_hosted_tools", lambda **kw: False)
        answer = inv.readiness()  # a peer's wake meanwhile: the hold ends tools_unavailable
        assert answer.ready is False and "not registered" in (answer.reason or "")
        inv.check_tools()
        assert woken == []
        registered[0] = ["mcp_agentchat_agentchat_inbox"]  # the watcher connected it
        inv.check_tools()
        assert woken == ["wake"]
        inv.check_tools()
        assert woken == ["wake"], "once"
        inv.set_wake(None)

    def test_a_parked_server_hermes_connects_again_wakes_the_loop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Allowed, but Hermes parked the server: every cycle ends
        tools_unavailable with no switch change. When Hermes' own probe
        connects it again the cards are gated without waiting for a re-wake."""
        inv = _invoker(monkeypatch, home=tmp_path / "p9")
        woken: list[str] = []
        inv.set_wake(lambda: woken.append("wake"))
        self._switch(monkeypatch, "full", via="trust")
        registered = self._registered(monkeypatch)
        monkeypatch.setattr(invoker_module, "wait_for_hosted_tools", lambda **kw: False)
        assert inv.readiness().ready is False
        inv.check_tools()
        assert woken == []
        registered[0] = ["mcp_agentchat_agentchat_inbox"]
        inv.check_tools()
        assert woken == ["wake"]
        inv.set_wake(None)

    def test_no_wake_when_no_cycle_waited(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        inv = _invoker(monkeypatch, home=tmp_path / "p10")
        woken: list[str] = []
        inv.set_wake(lambda: woken.append("wake"))
        self._switch(monkeypatch, "full", via="trust")
        self._registered(monkeypatch, ["mcp_agentchat_agentchat_inbox"])
        self._held(monkeypatch)
        assert inv.readiness().ready is True
        inv.check_tools()
        assert woken == []
        inv.set_wake(None)

    def test_a_yes_applied_elsewhere_reaches_the_loop(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from agentchatme_hermes import allow
        from agentchatme_hermes.mcp_registration import Allowance

        home = tmp_path / "p7"
        inv = _invoker(monkeypatch, home=home)
        woken: list[str] = []
        inv.set_wake(lambda: woken.append("wake"))
        box = self._switch(monkeypatch, None, entry="missing")
        self._registered(monkeypatch, ["mcp_agentchat_agentchat_inbox"])
        inv.readiness()
        box[0] = Allowance("ours", "full", "trust")
        callback = allow._on_applied.get(str(home))
        assert callback is not None, "the invoker listens for the second step"
        callback()
        assert woken == ["wake"]
        inv.set_wake(None)
        assert str(home) not in allow._on_applied

    def test_the_status_reads_nothing_itself(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The health record is written from the receiver's event loop too: the
        status is the last reading; the minute check (on a thread) reads."""
        from agentchatme_hermes import allow

        inv = _invoker(monkeypatch, home=tmp_path / "p11")
        reads: list[str] = []
        box = self._switch(monkeypatch, None, entry="missing")
        monkeypatch.setattr(allow, "inspect_allowance", lambda *, api_base: reads.append("read") or box[0])
        assert inv.tools_allowed_status() is None and reads == []
        inv.check_tools()
        assert reads == ["read"]
        status = inv.tools_allowed_status()
        assert status is not None and status["state"] == "not_allowed_yet" and reads == ["read"]

    def test_a_stalled_yes_says_restart_to_apply(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from agentchatme_hermes import allow

        home = tmp_path / "p12"
        inv = _invoker(monkeypatch, home=home)
        self._switch(monkeypatch, None, entry="missing")
        allow.write_record(home, how="asked", trust="full", decision="yes", pending=True, stalled=True)
        monkeypatch.setattr(allow, "start_completion", lambda scope, *, api_base: None)
        answer = inv.readiness()
        assert answer.ready is False and "restart the gateway to apply" in (answer.reason or "")
        status = inv.tools_allowed_status()
        assert status is not None and status["stalled"] is True

    def test_an_unreadable_config_does_not_pause(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        inv = _invoker(monkeypatch, home=tmp_path / "p4")
        self._held(monkeypatch)
        assert inv.readiness().ready is True  # no Hermes config here: Hermes' own gate decides
        status = inv.tools_allowed_status()
        assert status is not None and status["state"] == "unknown"

    def test_the_status_and_one_line_per_change(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        inv = _invoker(monkeypatch, home=tmp_path / "p5")
        box = self._switch(monkeypatch, None, entry="missing")
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.agent_invoker"):
            for _ in range(3):
                inv.readiness()
            status = inv.tools_allowed_status()
            assert status is not None and status["state"] == "not_allowed_yet" and status["trust"] is None
            from agentchatme_hermes.mcp_registration import Allowance

            box[0] = Allowance("ours", "full", "trust")
            self._held(monkeypatch)
            inv.readiness()
        lines = [r for r in caplog.records if "AgentChat tools" in r.getMessage()]
        assert [r.levelno for r in lines] == [logging.WARNING, logging.INFO]
        assert "/agentchat allow" in lines[0].getMessage() and "@me" in lines[0].getMessage()
        assert inv.tools_allowed_status()["state"] == "allowed"  # type: ignore[index]


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
        assert end.timed_out is False

    async def test_an_agent_that_cannot_be_built_is_a_turn_not_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: None)
        end = await _run(inv, _reply_request())
        assert end.status == "failed" and (end.detail or "").startswith("not started")
        assert end.agentchat_calls is not None and end.agentchat_calls.made == 0, "it never looked"
        assert inv.tracker.size == 0

    async def test_a_scope_that_cannot_be_entered_runs_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        up = tmp_path / "hermes" / "profiles" / "up"
        fake = FakeSecretScope(multiplex=True)
        fake.install(monkeypatch)
        fake.fail_set = OSError("vault locked")
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
        assert end.timed_out is True, "at its own time limit: a could-not-process report says timed_out"
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

    def test_a_failed_run_is_not_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        end = _invoker(monkeypatch, _Agent(raises=RuntimeError("provider refused"))).start_run(self._request())
        assert end.status == "failed" and end.timed_out is False

    def test_a_conversation_run_is_started_with_the_kits_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent()
        prompt = "AgentChat started this run for ONE conversation: conv_a."
        request = RunRequest(prompt=prompt, texts=TEXTS, cards=1, invites=0, stopping=asyncio.Event(), conversation_id="conv_a")
        assert _invoker(monkeypatch, agent).start_run(request).status == "completed"
        assert agent.prompts[0].startswith(prompt) and agent.prompts[0].endswith(MANUAL_POINTER)

    def test_a_run_not_started(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: None)
        assert inv.start_run(self._request()).status == "not_started"

    def test_no_second_run_while_the_first_still_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent(block=True, obey=False)
        inv = _invoker(monkeypatch, agent)
        first = inv.start_run(self._request())
        assert first.status == "failed", "overran"
        assert first.timed_out is True, "at its own time limit: a could-not-process report says timed_out"
        second = inv.start_run(self._request())
        assert second.status == "not_started" and "still running" in (second.detail or "")
        agent.release.set()
        assert inv._run_finished is not None and inv._run_finished.wait(5.0)


# ─── profile scope ─────────────────────────────────────────────────────────


class TestProfileScope:
    """A served profile's gate call, runtime check and turn resolve that
    profile's credentials: each callback runs in the profile's scope
    (:class:`~agentchatme_hermes.profile_scope.ProfileScope`), with the
    profile's secrets bound for the call on a multiplexed gateway."""

    @staticmethod
    def _homes(tmp_path: Path) -> tuple[Path, Path]:
        return tmp_path / "hermes", tmp_path / "hermes" / "profiles" / "up"

    @staticmethod
    def _fake(monkeypatch: pytest.MonkeyPatch, launch: Path, up: Path, *, multiplex: bool = True) -> FakeSecretScope:
        fake = FakeSecretScope(
            multiplex=multiplex,
            by_home={launch: {"LIVEFIRE_STUB_KEY": "key-launch"}, up: {"LIVEFIRE_STUB_KEY": "key-up"}},
        )
        fake.install(monkeypatch)
        return fake

    async def test_the_gate_and_the_turn_see_the_profiles_secret_and_nothing_leaks(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        launch, up = self._homes(tmp_path)
        fake = self._fake(monkeypatch, launch, up)
        seen: dict[str, list[Any]] = {"gate": [], "turn": []}
        agent = _Agent()
        agent.on_run = lambda: seen["turn"].append(fake.get_secret("LIVEFIRE_STUB_KEY"))
        inv = _invoker(monkeypatch, agent, home=up)

        def resolve() -> tuple[str, dict[str, Any]]:
            seen["gate"].append(fake.get_secret("LIVEFIRE_STUB_KEY"))
            return "m", {}

        monkeypatch.setattr(inv, "_resolve_model_and_runtime", resolve)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: (lambda prompt, **_: "{}"))
        inv.call_model("p")
        await _run(inv, _reply_request())
        assert seen["gate"] == ["key-up"] and seen["turn"] == ["key-up"]
        assert set(fake.binds) == {str(up)} and fake.resets == len(fake.binds)
        assert fake.bound.get() is None, "nothing left bound on this thread"

    async def test_the_runtime_check_resolves_the_turns_provider_in_the_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Resolving the turns' provider reads the profile's secrets too
        (``resolve_runtime_provider`` → ``get_secret``): outside the scope that
        read failed closed on a multiplexed gateway and the check failed every time."""
        launch, up = self._homes(tmp_path)
        fake = self._fake(monkeypatch, launch, up)
        inv = _invoker(monkeypatch, home=up)
        read: list[str | None] = []

        def resolve_turn_runtime() -> tuple[str, dict[str, Any], str | None]:
            read.append(fake.get_secret("LIVEFIRE_STUB_KEY"))  # raises outside a scope
            return "m", {}, None

        # The real problem probe (``_invoker`` stands it in), on a stand-in resolver.
        monkeypatch.setattr(inv, "_turn_runtime_problem", AgentInvoker._turn_runtime_problem.__get__(inv))
        monkeypatch.setattr(inv, "_resolve_turn_runtime", resolve_turn_runtime)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: (lambda prompt, **_: "OK"))
        ready = await inv.check_runtime()
        assert ready.ready is True, ready.reason
        assert read == ["key-up"]

    def test_a_stopped_invoker_makes_no_gate_call_and_closes_its_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes.model_call import GateCallCancelled

        inv = _invoker(monkeypatch)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: (lambda prompt, **_: "{}"))
        assert inv.call_model("p", cancel=threading.Event()) == "{}"
        inv.stop()
        with pytest.raises(GateCallCancelled):
            inv.call_model("p", cancel=threading.Event())
        assert inv._gate_loop._closed

    def test_a_gate_whose_scope_cannot_be_entered_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        launch, up = self._homes(tmp_path)
        fake = self._fake(monkeypatch, launch, up)
        fake.fail_set = OSError("vault locked")
        inv = _invoker(monkeypatch, home=up)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: (lambda prompt, **_: "{}"))
        with pytest.raises(ProfileScopeError):
            inv.call_model("p")

    async def test_a_runtime_check_whose_scope_cannot_be_entered_is_not_ready(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        launch, up = self._homes(tmp_path)
        fake = self._fake(monkeypatch, launch, up)
        fake.fail_set = OSError("vault locked")
        ready = await _invoker(monkeypatch, home=up).check_runtime()
        assert ready.ready is False and "profile scope unavailable" in (ready.reason or "")

    def test_a_single_profile_gateway_binds_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        launch, up = self._homes(tmp_path)
        fake = self._fake(monkeypatch, launch, up, multiplex=False)
        inv = _invoker(monkeypatch, home=launch)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: (lambda prompt, **_: "{}"))
        inv.call_model("p")
        assert fake.binds == []


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

    def call_model(prompt: str, **_: Any) -> str:
        cards = [cid for cid in server.unread if f'"conversation_id":"{cid}"' in prompt.replace(" ", "")]
        return json.dumps({"decisions": [{"conversation_id": cid, "decision": server.answer.get(cid, "reply")} for cid in cards]})

    return call_model


def _loop(inv: AgentInvoker, server: _Server, **overrides: Any) -> ReceiveLoop:
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
        **overrides,
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

    @pytest.mark.parametrize(
        ("content", "dismissed"),
        [
            ('{"result": "{\\"invites\\": []}"}', True),
            ("Error executing tool: connection reset", False),
        ],
    )
    async def test_an_invite_left_pending_twice_is_dismissed_only_on_counts_that_all_succeeded(
        self, monkeypatch: pytest.MonkeyPatch, content: str, dismissed: bool
    ) -> None:
        """The second silent invite turn dismisses the invite only when its
        AgentChat calls are known and all succeeded (the transcript count);
        a call that may have failed leaves it on the tray (``silent_unconfirmed``)."""
        server = _Server()
        server.invites = ["ginv_1"]
        agent = _Agent(result={"final_response": "", "messages": [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "mcp__agentchat__agentchat_list_group_invites", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "mcp__agentchat__agentchat_list_group_invites", "content": content},
        ]})
        inv = _invoker(monkeypatch, agent)
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: _gate_answer(server))
        loop = _loop(inv, server, pace_ms=0)
        await loop.start()
        for turns in (1, 2, 3):
            loop.wake()
            assert await _until(lambda turns=turns: len(agent.prompts) >= turns and inv.tracker.size == 0)
            await loop.idle()
            if server.dismissed:
                break
        await loop.stop()
        assert server.dismissed == (["ginv_1"] if dismissed else [])
        assert len(agent.prompts) == (2 if dismissed else 3)

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
        # conv_b's slot is freed just after its verdict is recorded.
        assert await _until(lambda: loop.status().counters["turns_active"] == 0)
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


class _ReportingServer(_Server):
    """The server with the could-not-process route: records each report and
    answers from ``answers`` in order (``none`` once they run out). A card in
    ``hidden`` reads as the server serves a hidden preview: the note as its
    text, ``could_not_process: true``."""

    def __init__(self, answers: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.reports: list[dict[str, Any]] = []
        self.answers = list(answers or [])
        self.hidden: dict[str, str] = {}
        self.conversations = SimpleNamespace(report_processing_failure=self._report)

    async def _tray(self, **kw: Any) -> dict[str, Any]:
        tray = await super()._tray(**kw)
        for card in tray["conversations"]:
            note = self.hidden.get(card["conversation_id"])
            if note is not None:
                card["preview"] = {**card["preview"], "text": note, "could_not_process": True}
        return tray

    async def _report(self, conversation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.reports.append({"conversation_id": conversation_id, **kwargs})
        if self.answers:
            return self.answers.pop(0)
        return {"action": "none", "attempts": len(self.reports), "hidden_message_ids": [], "paused_until": None}


def _poison_gate(server: _Server, prompts: list[str], *, poison: str = "conv_bad") -> Any:
    """The gate's model: it cannot process any prompt that holds ``poison``'s
    card (a provider refusal); otherwise it answers like ``_gate_answer``. The
    runtime check's fixed prompt is answered too: the model works."""
    answer = _gate_answer(server)

    def call_model(prompt: str, **kw: Any) -> str:
        prompts.append(prompt)
        if prompt == "Reply with the single word OK.":
            return "OK"
        if f'"conversation_id":"{poison}"' in prompt.replace(" ", ""):
            raise RuntimeError("API Error: the provider refused this request")
        return str(answer(prompt, **kw))

    return call_model


class TestCouldNotProcessThroughTheLoop:
    """The kit splits a failed gate chunk and reports a card that fails on
    its own; this runtime supplies the runtime check through the gate's path."""

    async def test_a_failed_chunk_is_split_and_the_card_that_fails_alone_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _ReportingServer(answers=[{"action": "retry", "attempts": 1, "hidden_message_ids": [], "paused_until": None}])
        server.unread = {"conv_bad": "msg_bad", "conv_ok": "msg_ok"}
        agent = _Agent()
        agent.on_run = lambda: server.answered.__setitem__("conv_ok", "msg_ok")
        inv = _invoker(monkeypatch, agent)
        prompts: list[str] = []
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: _poison_gate(server, prompts))
        events: list[dict[str, Any]] = []
        loop = _loop(inv, server, on_event=events.append, check_runtime=inv.check_runtime)
        await loop.start()
        loop.wake()
        # The innocent peer is answered in the same cycle.
        assert await _until(lambda: ("conv_ok", "reply", "msg_ok") in server.recorded)
        assert await _until(lambda: bool(server.reports))
        await loop.stop()
        both = [p for p in prompts if "conv_bad" in p and "conv_ok" in p]
        assert len(both) == 1, "one chunk call for both cards, then one call per card"
        assert any("conv_ok" in p and "conv_bad" not in p for p in prompts)
        assert server.reports[0] == {
            "conversation_id": "conv_bad",
            "through_message_id": "msg_bad",
            "outcome": "failed",
            "evidence": "other_run_completed",
        }
        assert not any("conv_bad" in p for p in agent.prompts), "no turn for the card the model cannot process"
        assert any(e["type"] == "gate_card" and e["conversation_id"] == "conv_ok" and e["outcome"] == "decided" for e in events)

    async def test_alone_on_the_tray_it_is_reported_once_the_runtime_check_passes_and_the_pause_is_told(self, monkeypatch: pytest.MonkeyPatch) -> None:
        until = "2026-11-21T10:00:00.000Z"
        server = _ReportingServer(answers=[
            {"action": "retry", "attempts": 1, "hidden_message_ids": [], "paused_until": None},
            {"action": "paused", "attempts": 2, "hidden_message_ids": [], "paused_until": until},
        ])
        server.unread = {"conv_bad": "msg_bad"}
        inv = _invoker(monkeypatch, _Agent())
        prompts: list[str] = []
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: _poison_gate(server, prompts))
        events: list[dict[str, Any]] = []
        loop = _loop(inv, server, on_event=events.append, check_runtime=inv.check_runtime)
        await loop.start()
        loop.wake()
        assert await _until(lambda: len(server.reports) >= 2, timeout=10.0)
        await loop.idle()
        await loop.stop()
        assert "Reply with the single word OK." in prompts, "the check went through the gate's own call"
        assert server.reports[0]["evidence"] == "runtime_check" and server.reports[0]["through_message_id"] == "msg_bad"
        reports = [e for e in events if e["type"] == "processing_failure"]
        assert [e["action"] for e in reports][:2] == ["retry", "paused"]
        assert reports[1]["paused_until"] == until and reports[1]["conversation_id"] == "conv_bad"
        assert any(e["type"] == "runtime_check" and e["ok"] is True for e in events)

    async def test_a_turn_that_never_started_is_never_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Hermes agent cannot be built (a runtime-wide breakage): the
        turn fails without looking, so even with the runtime check passing
        nothing is reported for the conversation."""
        server = _ReportingServer(answers=[{"action": "retry", "attempts": 1, "hidden_message_ids": [], "paused_until": None}])
        server.unread = {"conv_a": "msg_a"}
        inv = _invoker(monkeypatch)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: None)
        prompts: list[str] = []
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: _poison_gate(server, prompts, poison="none"))
        events: list[dict[str, Any]] = []
        loop = _loop(inv, server, on_event=events.append, check_runtime=inv.check_runtime)
        await loop.start()
        loop.wake()
        assert await _until(lambda: any(e["type"] == "turn_outcome" for e in events))
        await loop.idle()
        await loop.stop()
        assert server.reports == []

    async def test_a_provider_timeout_on_the_gate_is_a_timeout_never_reported_on_the_check_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate's own request timeout comes before the kit's deadline;
        raised as TimeoutError the kit counts it timed out, which needs
        another conversation's completion, not the runtime check."""

        class APITimeoutError(Exception):
            """As openai / anthropic clients name it (not a TimeoutError)."""

        server = _ReportingServer()
        server.unread = {"conv_slow": "msg_slow"}
        inv = _invoker(monkeypatch, _Agent())

        def slow_model(prompt: str, **_: Any) -> str:
            if prompt == "Reply with the single word OK.":
                return "OK"
            raise APITimeoutError("Request timed out.")

        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: slow_model)
        with pytest.raises(TimeoutError):
            inv.call_model("the gate prompt")
        events: list[dict[str, Any]] = []
        loop = _loop(inv, server, on_event=events.append, check_runtime=inv.check_runtime)
        await loop.start()
        loop.wake()
        assert await _until(lambda: any(e["type"] == "cycle" for e in events))
        await loop.idle()
        await loop.stop()
        assert server.reports == []
        assert not any(e["type"] == "runtime_check" for e in events), "a timeout never asks the check"

    async def test_a_hidden_preview_reaches_the_gate_as_the_served_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json

        note = "[Hidden from you: this message from @peer could not be processed on your side, so it is not shown. You can ask @peer to rephrase or resend it.]"
        server = _ReportingServer()
        server.unread = {"conv_a": "msg_a"}
        server.hidden = {"conv_a": note}
        agent = _Agent()
        agent.on_run = lambda: server.answered.__setitem__("conv_a", "msg_a")
        inv = _invoker(monkeypatch, agent)
        prompts: list[str] = []
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **kw: _poison_gate(server, prompts, poison="none"))
        loop = _loop(inv, server)
        await loop.start()
        loop.wake()
        assert await _until(lambda: server.recorded == [("conv_a", "reply", "msg_a")])
        await loop.stop()
        gate = prompts[0]
        assert json.dumps(note) in gate, "the note as served, untouched"
        assert '"could_not_process":true' in gate.replace(" ", "")
        assert TEXTS.gate_call_preamble and gate.startswith(TEXTS.gate_call_preamble)
        assert TEXTS.gate_call_answer_format and gate.endswith(TEXTS.gate_call_answer_format)
        assert "Card 1 of 1" in gate
        assert "conversation_id: conv_a" in agent.prompts[0], "the turn is for that one conversation"


class _SyncServer:
    """The runtime's sync client as a detached turn uses it."""

    def __init__(self) -> None:
        self.unread = {"conv_a": "msg_a"}
        self.answered: dict[str, str] = {}
        self.recorded: list[tuple[str, str, str | None]] = []
        self.inbox = SimpleNamespace(tray=self._tray)
        self.decisions = SimpleNamespace(record=self._record)
        self.groups = SimpleNamespace(dismiss_invite=lambda invite_id: None)

    def _tray(self, **_kw: Any) -> dict[str, Any]:
        cards = [
            {
                "conversation_id": cid,
                "type": "direct",
                "you_owe_reply": self.answered.get(cid) != newest,
                "preview": {"message_id": newest, "sender": "@peer", "text": "hi", "at": "2026-11-08T10:00:00.000Z"},
            }
            for cid, newest in self.unread.items()
            if not any(r[0] == cid for r in self.recorded)
        ]
        return {"version": 1, "you": "@me", "question": "q", "categories": [], "conversations": cards, "deferred": 0, "truncated": False, "invites": [], "invites_truncated": False}

    def _record(self, conversation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append((conversation_id, kwargs["decision"], kwargs.get("through_message_id")))
        return {}


def _gate_card() -> dict[str, Any]:
    return {"conversation_id": "conv_a", "type": "direct", "you_owe_reply": True, "preview": {"message_id": "msg_a", "sender": "@peer", "text": "hi", "at": "2026-11-08T10:00:00.000Z"}}


class TestAcrossARestart:
    """A turn still running when its receive loop goes away (a supervisor
    rebuild) is detached: the loop records nothing, the conversation stays
    held, and when the turn ends its outcome is recorded here — so a reply
    it sent is recorded and the card is not gated into a second turn."""

    def _request(self) -> TurnRequest:
        request = _reply_request("conv_a", "msg_a")
        return TurnRequest(**{**request.__dict__, "card": _gate_card(), "category": "open_request"})

    async def test_a_detached_turn_records_the_reply_it_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _SyncServer()
        agent = _Agent(block=True, obey=False)
        agent.after_block = agent.result  # it completes, having sent
        inv = _invoker(monkeypatch, agent)
        inv._runtime.client = server
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, poll_s=0.01)
        loop_handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert loop_handle is not None
        admission = await inv.start_turn(self._request())
        assert await _until(agent.started.is_set)

        inv.detach_turns()  # the supervisor rebuilds the loop
        assert await inv.tracker.confirmed_end(loop_handle, admission.done) is None, "the old loop records nothing"
        assert inv.tracker.conversation_running("conv_a"), "held across the restart"

        server.answered["conv_a"] = "msg_a"  # the turn sends its answer...
        agent.release.set()  # ...and ends
        assert await _until(lambda: server.recorded == [("conv_a", "reply", "msg_a")])
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))
        assert inv.live_turns() == 0

    async def test_a_report_still_queued_at_detach_is_recorded_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _SyncServer()
        agent = _Agent()
        agent.on_run = lambda: server.answered.__setitem__("conv_a", "msg_a")
        inv = _invoker(monkeypatch, agent)
        inv._runtime.client = server
        loop_handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert loop_handle is not None
        admission = await inv.start_turn(self._request())
        # The turn finishes while this loop is busy: its report is queued.
        deadline = time.monotonic() + 5.0
        turn = inv._turns["conversation:conv_a"]
        while turn.final is None and time.monotonic() < deadline:
            # Deliberately blocking this loop: the turn's report must stay queued.
            time.sleep(0.01)  # noqa: ASYNC251
        assert turn.final is not None and not admission.done.done()
        inv.detach_turns()  # the loop goes away before it runs the queued report
        assert await inv.tracker.confirmed_end(loop_handle, admission.done) is None
        await asyncio.sleep(0.05)  # the queued report runs now: already being recorded
        assert await _until(lambda: server.recorded == [("conv_a", "reply", "msg_a")])
        await asyncio.sleep(0.1)
        assert server.recorded == [("conv_a", "reply", "msg_a")], "exactly once"
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))

    async def test_a_detached_turn_that_failed_after_sending_records_the_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The kit reads a failed turn on the card too: one that sent its
        answer and then failed must not be gated into a second answer."""
        server = _SyncServer()
        agent = _Agent(block=True, obey=True)
        inv = _invoker(monkeypatch, agent)
        inv._runtime.client = server
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, grace_s=2.0, poll_s=0.01)
        loop_handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert loop_handle is not None
        await inv.start_turn(self._request())
        assert await _until(agent.started.is_set)
        server.answered["conv_a"] = "msg_a"  # it sent its answer...
        inv.detach_turns()
        inv.stop()  # ...and was then interrupted: a failed turn
        assert await _until(lambda: server.recorded == [("conv_a", "reply", "msg_a")])
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))

    async def test_a_detached_turn_that_overran_is_recorded_only_when_its_thread_ends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _SyncServer()
        agent = _Agent(block=True, obey=False)
        inv = _invoker(monkeypatch, agent)
        inv._runtime.client = server
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=0.1, grace_s=0.1, poll_s=0.01)
        loop_handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert loop_handle is not None
        await inv.start_turn(self._request())
        assert await _until(agent.started.is_set)
        inv.detach_turns()
        turn = inv._turns["conversation:conv_a"]
        assert await _until(lambda: turn.final is not None), "the runner gave up on it (timeout)"
        await asyncio.sleep(0.1)
        assert server.recorded == [] and inv.tracker.conversation_running("conv_a"), "it may still send"
        server.answered["conv_a"] = "msg_a"
        agent.release.set()  # the stuck call returns, having sent
        assert await _until(lambda: server.recorded == [("conv_a", "reply", "msg_a")])
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))
        assert inv.live_turns() == 0

    async def test_a_detached_turn_that_failed_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _SyncServer()
        agent = _Agent(block=True, obey=True)
        inv = _invoker(monkeypatch, agent)
        inv._runtime.client = server
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, grace_s=2.0, poll_s=0.01)
        loop_handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert loop_handle is not None
        await inv.start_turn(self._request())
        assert await _until(agent.started.is_set)
        inv.detach_turns()
        inv.stop()  # interrupted: a failed turn
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))
        assert server.recorded == []


class TestOverranInTheLoop:
    """A turn the loop let go of after it overran (the hung handoff) is this
    module's to record: when its thread finally ends, a reply it sent is
    recorded once, and only then is the conversation gated again."""

    async def test_the_reply_it_sent_late_is_recorded_before_it_is_let_go(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _SyncServer()
        agent = _Agent(block=True, obey=False)
        inv = _invoker(monkeypatch, agent)
        inv._runtime.client = server
        woken: list[str] = []
        inv.set_wake(lambda: woken.append("wake"))
        loop_handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert loop_handle is not None
        request = _reply_request("conv_a", "msg_a")
        admission = await inv.start_turn(TurnRequest(**{**request.__dict__, "card": _gate_card(), "category": "open_request"}))
        assert await inv.tracker.confirmed_end(loop_handle, admission.done) is None, "the loop records nothing"
        assert inv.hung_turns == 1 and server.recorded == []

        server.answered["conv_a"] = "msg_a"  # the stuck turn sends after all...
        agent.release.set()  # ...and ends
        assert await _until(lambda: server.recorded == [("conv_a", "reply", "msg_a")])
        assert await _until(lambda: not inv.tracker.conversation_running("conv_a"))
        assert woken == ["wake"] and inv.hung_turns == 0 and inv.live_turns() == 0
        await asyncio.sleep(0.05)
        assert server.recorded == [("conv_a", "reply", "msg_a")], "exactly once"


class TestIdleWaitsForTheRecord:
    """The runtime closes its client and frees the leader lock when the
    invoker is idle: an owned turn is not finished with until its outcome
    is recorded through that client."""

    async def test_idle_comes_only_after_the_detached_outcome_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = _SyncServer()
        agent = _Agent(block=True, obey=False)
        inv = _invoker(monkeypatch, agent)
        inv._runtime.client = server
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, poll_s=0.01)
        recording = threading.Event()
        proceed = threading.Event()
        real_record = server._record

        def slow_record(conversation_id: str, **kwargs: Any) -> dict[str, Any]:
            recording.set()
            proceed.wait(5.0)
            return real_record(conversation_id, **kwargs)

        server.decisions = SimpleNamespace(record=slow_record)
        loop_handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert loop_handle is not None
        request = _reply_request("conv_a", "msg_a")
        await inv.start_turn(TurnRequest(**{**request.__dict__, "card": _gate_card(), "category": "open_request"}))
        assert await _until(agent.started.is_set)
        inv.detach_turns()
        idle: list[list[Any]] = []
        assert inv.when_idle(lambda: idle.append(list(server.recorded))) is False
        server.answered["conv_a"] = "msg_a"
        agent.release.set()  # the thread ends; the outcome is being recorded
        assert await _until(recording.is_set)
        await asyncio.sleep(0.05)
        assert idle == [] and inv.live_turns() == 1, "not idle while the outcome is being recorded"
        proceed.set()
        assert await _until(lambda: bool(idle))
        assert idle == [[("conv_a", "reply", "msg_a")]], "idle only after the record"


class TestIdleAndReadiness:
    async def test_when_idle_waits_for_the_last_turn_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _Agent(block=True, obey=False)
        inv = _invoker(monkeypatch, agent)
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, poll_s=0.01)
        assert inv.when_idle(lambda: None) is True, "nothing running: at once"
        handle = inv.tracker.begin(TrackedTurn.reply("conv_a", "msg_a"))
        assert handle is not None
        await inv.start_turn(_reply_request())
        assert await _until(agent.started.is_set)
        idle: list[str] = []
        assert inv.when_idle(lambda: idle.append("idle")) is False
        assert inv.live_turns() == 1 and idle == []
        inv.stop()
        agent.release.set()
        assert await _until(lambda: idle == ["idle"])

    async def test_readiness_is_not_ready_while_no_turn_can_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_Agent(block=True, obey=False) for _ in range(invoker_module.MAX_HUNG_TURNS)]
        inv = _invoker(monkeypatch)
        built = iter(agents)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: next(built))
        assert inv.readiness().ready is True
        for i in range(invoker_module.MAX_HUNG_TURNS):
            await _run(inv, _reply_request(f"conv_{i}", f"msg_{i}"))
        answer = inv.readiness()
        assert answer.ready is False and "no turn can start" in (answer.reason or ""), "no gate call is spent"
        status = inv.status()
        assert status["hung_turns"] == status["max_hung_turns"] == invoker_module.MAX_HUNG_TURNS
        for agent in agents:
            agent.release.set()
        assert await _until(lambda: inv.hung_turns == 0)
        assert inv.readiness().ready is True

    def test_since_is_the_turns_start_or_the_last_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv = _invoker(monkeypatch, _Agent())
        before = inv.status()["since"]
        inv.start_run(RunRequest(prompt="p", texts=TEXTS, cards=1, invites=0, stopping=asyncio.Event()))
        assert inv.status()["since"] >= before, "the idle state began when the run ended"
        with inv._state_lock:
            inv._current_turn = {"session_id": "s", "started_at": 123.0}
        assert inv.status()["state"] == "turn" and inv.status()["since"] == 123.0
