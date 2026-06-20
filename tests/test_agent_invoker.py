"""Tests for the pure helpers in ``agentchatme_hermes.agent_invoker``.

We unit-test the conversation-history translation in isolation — no
Hermes, no SDK, no runtime. The end-to-end ``AgentInvoker._run_one``
path requires a real Hermes ``AIAgent`` and is exercised through the
integration suite (planned for a later commit).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from agentchatme_hermes.agent_invoker import (
    _FALLBACK_MODEL,
    AgentInvoker,
    _coerce_model_string,
    _extract_messages_list,
    _translate_messages_to_history,
)
from agentchatme_hermes.reply_gate import GateDecision
from agentchatme_hermes.thread_closures import ThreadClosures
from agentchatme_hermes.turn_guard import TurnCircuitBreaker
from agentchatme_hermes.types import AgentIdentity, InboundEvent

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _msg(
    *,
    msg_id: str,
    text: str,
    is_own: bool | None = None,
    sender: str = "alice",
    msg_type: str = "text",
) -> dict[str, Any]:
    """Build a synthetic AgentChat-shaped message payload."""
    payload: dict[str, Any] = {
        "id": msg_id,
        "type": msg_type,
        "content": {"text": text},
        "from": f"@{sender}",
    }
    if is_own is not None:
        payload["is_own"] = is_own
    return payload


# ──────────────────────── _coerce_model_string ────────────────────────


class TestCoerceModelString:
    """Pin down the model-config shape handling.

    The 0.2.1 production hang came from this helper not existing:
    ``cfg.get("model")`` returned the nested dict
    ``{"default": "deepseek-v4-flash", "provider": "deepseek", ...}``,
    which was passed straight into ``AIAgent(model=...)`` and crashed
    deep inside Hermes' ``_anthropic_prompt_cache_policy`` with
    ``'dict' object has no attribute 'lower'``.
    """

    def test_nested_dict_with_default(self) -> None:
        cfg_value = {
            "default": "deepseek-v4-flash",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
        }
        assert _coerce_model_string(cfg_value) == "deepseek-v4-flash"

    def test_nested_dict_default_takes_precedence_over_model(self) -> None:
        cfg_value = {"default": "primary-model", "model": "fallback-model"}
        assert _coerce_model_string(cfg_value) == "primary-model"

    def test_nested_dict_falls_back_to_model_when_no_default(self) -> None:
        cfg_value = {"model": "the-model", "provider": "deepseek"}
        assert _coerce_model_string(cfg_value) == "the-model"

    def test_flat_string(self) -> None:
        assert _coerce_model_string("deepseek-v4-flash") == "deepseek-v4-flash"

    def test_empty_string_falls_back(self) -> None:
        assert _coerce_model_string("") == _FALLBACK_MODEL

    def test_none_falls_back(self) -> None:
        assert _coerce_model_string(None) == _FALLBACK_MODEL

    def test_empty_dict_falls_back(self) -> None:
        assert _coerce_model_string({}) == _FALLBACK_MODEL

    def test_dict_without_recognizable_keys_falls_back(self) -> None:
        # Dict shape but neither ``default`` nor ``model`` is set —
        # we cannot guess what string the operator intended.
        assert _coerce_model_string({"provider": "deepseek"}) == _FALLBACK_MODEL

    def test_dict_with_non_string_default_falls_back(self) -> None:
        # Defensive: a numeric or None value where a string was
        # expected must not silently get passed through.
        assert _coerce_model_string({"default": 42}) == _FALLBACK_MODEL
        assert _coerce_model_string({"default": None, "model": "real"}) == "real"

    def test_completely_unknown_shape_falls_back(self) -> None:
        assert _coerce_model_string(42) == _FALLBACK_MODEL
        assert _coerce_model_string(["a", "b"]) == _FALLBACK_MODEL


# ──────────────────────── _extract_messages_list ────────────────────────


class TestExtractMessagesList:
    def test_dict_with_messages_key(self) -> None:
        result = {"messages": [_msg(msg_id="m1", text="hi")]}
        assert len(_extract_messages_list(result)) == 1

    def test_bare_list(self) -> None:
        assert len(_extract_messages_list([_msg(msg_id="m1", text="hi")])) == 1

    def test_none_returns_empty(self) -> None:
        assert _extract_messages_list(None) == []

    def test_unexpected_shape_returns_empty(self) -> None:
        assert _extract_messages_list("garbage") == []
        assert _extract_messages_list(42) == []
        assert _extract_messages_list({"unexpected": "shape"}) == []

    def test_filters_non_dict_entries(self) -> None:
        result = {"messages": [_msg(msg_id="m1", text="hi"), "garbage", None]}
        assert len(_extract_messages_list(result)) == 1


# ──────────────────────── _translate_messages_to_history ────────────────────────


class TestTranslateMessagesToHistory:
    def _call(
        self,
        messages: list[dict[str, Any]],
        *,
        own_handle: str = "me",
        conversation_kind: str = "direct",
        trigger_message_id: str = "trigger",
    ) -> list[dict[str, Any]]:
        return _translate_messages_to_history(
            messages,
            own_handle=own_handle,
            conversation_kind=conversation_kind,
            trigger_message_id=trigger_message_id,
        )

    # -- is_own → role mapping --

    def test_self_message_becomes_assistant(self) -> None:
        msgs = [_msg(msg_id="m1", text="hello", is_own=True, sender="me")]
        history = self._call(msgs)
        assert history == [{"role": "assistant", "content": "hello"}]

    def test_peer_message_becomes_user(self) -> None:
        msgs = [_msg(msg_id="m1", text="hi", is_own=False, sender="alice")]
        history = self._call(msgs)
        assert history == [{"role": "user", "content": "hi"}]

    # -- is_own fallback to handle compare --

    def test_is_own_fallback_via_handle_match(self) -> None:
        # No is_own field; rely on sender handle comparison
        msgs = [_msg(msg_id="m1", text="hello", sender="me")]
        history = self._call(msgs, own_handle="me")
        assert history == [{"role": "assistant", "content": "hello"}]

    def test_is_own_fallback_via_handle_mismatch(self) -> None:
        msgs = [_msg(msg_id="m1", text="hi", sender="alice")]
        history = self._call(msgs, own_handle="me")
        assert history == [{"role": "user", "content": "hi"}]

    def test_handle_compare_is_case_insensitive(self) -> None:
        msgs = [_msg(msg_id="m1", text="hi", sender="ME")]
        history = self._call(msgs, own_handle="me")
        assert history[0]["role"] == "assistant"

    def test_handle_compare_strips_at_prefix(self) -> None:
        # Server may or may not include @; we accept either.
        msgs = [{"id": "m1", "type": "text", "content": {"text": "hi"}, "from": "me"}]
        history = self._call(msgs, own_handle="me")
        assert history[0]["role"] == "assistant"

    # -- group prefix --

    def test_group_prefixes_peer_messages(self) -> None:
        msgs = [_msg(msg_id="m1", text="hello team", sender="alice", is_own=False)]
        history = self._call(msgs, conversation_kind="group")
        assert history == [{"role": "user", "content": "[@alice] hello team"}]

    def test_group_does_not_prefix_self(self) -> None:
        msgs = [_msg(msg_id="m1", text="hi everyone", sender="me", is_own=True)]
        history = self._call(msgs, conversation_kind="group")
        assert history == [{"role": "assistant", "content": "hi everyone"}]

    def test_direct_does_not_prefix(self) -> None:
        msgs = [_msg(msg_id="m1", text="hi", sender="alice", is_own=False)]
        history = self._call(msgs, conversation_kind="direct")
        assert history == [{"role": "user", "content": "hi"}]

    def test_group_with_unknown_sender_uses_placeholder(self) -> None:
        msgs = [{"id": "m1", "type": "text", "content": {"text": "hi"}}]
        history = self._call(msgs, conversation_kind="group")
        assert history == [{"role": "user", "content": "[@?] hi"}]

    # -- trigger exclusion --

    def test_trigger_message_is_excluded(self) -> None:
        msgs = [
            _msg(msg_id="m1", text="old", is_own=False, sender="alice"),
            _msg(msg_id="trigger", text="latest", is_own=False, sender="alice"),
        ]
        history = self._call(msgs, trigger_message_id="trigger")
        assert len(history) == 1
        assert history[0]["content"] == "old"

    # -- non-text and edge cases --

    def test_non_text_messages_skipped(self) -> None:
        msgs = [
            _msg(msg_id="m1", text="x", msg_type="file"),
            _msg(msg_id="m2", text="y", is_own=False, sender="alice"),
        ]
        history = self._call(msgs)
        assert len(history) == 1
        assert history[0]["content"] == "y"

    def test_missing_content_dict_skipped(self) -> None:
        msgs = [{"id": "m1", "type": "text", "from": "alice"}]
        history = self._call(msgs)
        assert history == []

    def test_empty_text_skipped(self) -> None:
        msgs = [{"id": "m1", "type": "text", "content": {"text": ""}, "from": "alice"}]
        history = self._call(msgs)
        assert history == []

    def test_non_string_text_skipped(self) -> None:
        msgs = [{"id": "m1", "type": "text", "content": {"text": 42}, "from": "alice"}]
        history = self._call(msgs)
        assert history == []

    def test_oldest_first_preserved(self) -> None:
        # The caller is expected to receive messages oldest-first; we
        # don't reorder. Confirm the order survives translation.
        msgs = [
            _msg(msg_id="m1", text="first", sender="alice", is_own=False),
            _msg(msg_id="m2", text="second", sender="me", is_own=True),
            _msg(msg_id="m3", text="third", sender="alice", is_own=False),
        ]
        history = self._call(msgs)
        assert [h["content"] for h in history] == ["first", "second", "third"]


# ──────────────────────── prompts.py ────────────────────────


class TestNotificationPrompt:
    """Lock down the wake-prompt format — it's part of the LLM contract."""

    def _event(self, *, conversation_kind: str = "direct", text: str = "hi") -> Any:
        from datetime import datetime, timezone

        from agentchatme_hermes.types import InboundEvent

        return InboundEvent(
            message_id="m1",
            conversation_id="conv_x",
            conversation_kind=conversation_kind,  # type: ignore[arg-type]
            sender_handle="alice",
            content_text=text,
            received_at=datetime.now(timezone.utc),
        )

    def test_direct_format(self) -> None:
        from agentchatme_hermes.prompts import build_notification_prompt

        prompt = build_notification_prompt(self._event(conversation_kind="direct"))
        assert prompt.startswith("[agentchat] @alice: hi")
        # Skill hint is included so the agent can find the etiquette manual
        # (plugin skills don't appear in <available_skills>).
        assert "skill_view agentchat:agentchat" in prompt
        # Direct prompt should NOT have the group-id annotation
        assert "[agentchat group" not in prompt

    def test_group_format_includes_conv_id(self) -> None:
        from agentchatme_hermes.prompts import build_notification_prompt

        prompt = build_notification_prompt(
            self._event(conversation_kind="group")
        )
        assert prompt.startswith("[agentchat group conv_x] @alice: hi")
        assert "skill_view agentchat:agentchat" in prompt

    def test_prompt_does_not_bias_toward_silence(self) -> None:
        """The wake prompt is data only — no "silence is valid" tail.

        Reply-vs-silence judgment lives in the skill, not the prompt.
        Anything in the prompt that biases the model toward one outcome
        compounds with the LLM's existing biases (cost-per-token,
        safety training) and tilts the agent toward under-replying.
        """
        from agentchatme_hermes.prompts import build_notification_prompt

        prompt = build_notification_prompt(self._event())
        lower = prompt.lower()
        assert "silence is" not in lower
        assert "decide" not in lower
        assert "you may" not in lower

    def test_content_text_is_full_not_truncated(self) -> None:
        from agentchatme_hermes.prompts import build_notification_prompt

        long_text = "x" * 5000
        prompt = build_notification_prompt(self._event(text=long_text))
        assert long_text in prompt


class _FakeFuture:
    def add_done_callback(self, _callback: Any) -> None:
        return None

    def exception(self) -> None:
        return None


class _FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[tuple[Any, tuple[Any, ...]]] = []

    def submit(self, fn: Any, *args: Any) -> _FakeFuture:
        self.submitted.append((fn, args))
        return _FakeFuture()


class TestInFlightThreadClose:
    def test_queued_turn_is_suppressed_if_thread_closes_before_worker_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import agentchatme_hermes.runtime as runtime_module

        event = InboundEvent(
            message_id="m1",
            conversation_id="conv_dm_123",
            conversation_kind="direct",
            sender_handle="alice",
            content_text="hi",
            received_at=datetime.now(timezone.utc),
        )
        queue = MagicMock()
        queue.pop.side_effect = [event, None]
        invoker = AgentInvoker(
            config=SimpleNamespace(max_inflight_turns=1),
            identity=AgentIdentity(handle="me"),
            queue=queue,
        )
        executor = _FakeExecutor()
        invoker._executor = executor

        runtime = SimpleNamespace(
            thread_closures=ThreadClosures(path=tmp_path / "closed.json"),
            client=MagicMock(),
        )
        agent = MagicMock()
        monkeypatch.setattr(runtime_module, "get_existing_runtime", lambda: runtime)
        monkeypatch.setattr(invoker, "_ensure_hermes_resolved", lambda: None)
        monkeypatch.setattr(invoker, "_build_agent", lambda _conversation_id: agent)
        monkeypatch.setattr(
            invoker,
            "_build_conversation_history",
            lambda **_kwargs: [],
        )

        invoker._drain_queue()

        assert len(executor.submitted) == 1

        runtime.thread_closures.close("conv_dm_123", reason="closed mid-flight")
        submitted_fn, submitted_args = executor.submitted[0]
        submitted_fn(*submitted_args)

        agent.run_conversation.assert_not_called()


def _wired_invoker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: Any
) -> tuple[AgentInvoker, Any, dict[str, list[Any]]]:
    """Build an invoker with a stubbed runtime + agent for turn-flow tests.

    ``_build_agent`` and ``_build_conversation_history`` are stubbed so the
    turn never touches Hermes or the SDK; ``calls["build_agent"]`` records
    whether the agent was constructed (it should be, only on the reply path).
    """
    import agentchatme_hermes.runtime as runtime_module

    invoker = AgentInvoker(
        config=config,
        identity=AgentIdentity(handle="me"),
        queue=MagicMock(),
    )
    runtime = SimpleNamespace(
        thread_closures=ThreadClosures(path=tmp_path / "closed.json"),
        client=MagicMock(),
    )
    monkeypatch.setattr(runtime_module, "get_existing_runtime", lambda: runtime)
    monkeypatch.setattr(invoker, "_ensure_hermes_resolved", lambda: None)

    agent = MagicMock()
    calls: dict[str, list[Any]] = {"build_agent": []}

    def _fake_build_agent(conversation_id: str) -> Any:
        calls["build_agent"].append(conversation_id)
        return agent

    monkeypatch.setattr(invoker, "_build_agent", _fake_build_agent)
    monkeypatch.setattr(invoker, "_build_conversation_history", lambda **_kw: [])
    return invoker, agent, calls


def _inbound(conv: str = "conv_dm_a") -> InboundEvent:
    return InboundEvent(
        message_id="m1",
        conversation_id=conv,
        conversation_kind="direct",
        sender_handle="alice",
        content_text="hi",
        received_at=datetime.now(timezone.utc),
    )


class TestReplyGateWiring:
    """The gate's effect on the turn flow in ``_run_one_inner``."""

    def test_reply_decision_runs_conversation_and_records_breaker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes.prompts import build_notification_prompt

        invoker, agent, calls = _wired_invoker(
            monkeypatch, tmp_path, SimpleNamespace(max_inflight_turns=1)
        )
        monkeypatch.setattr(
            invoker,
            "_decide_reply",
            lambda _ev, _hist: GateDecision(
                reply=True, reason="open q", category="open_request", source="llm"
            ),
        )

        invoker._run_one_inner(_inbound("conv_dm_a"), build_notification_prompt)

        agent.run_conversation.assert_called_once()
        assert calls["build_agent"] == ["conv_dm_a"]
        # The reply was counted toward the circuit breaker.
        assert invoker._breaker.recent_count("conv_dm_a") == 1

    def test_no_reply_decision_skips_everything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes.prompts import build_notification_prompt

        invoker, agent, calls = _wired_invoker(
            monkeypatch, tmp_path, SimpleNamespace(max_inflight_turns=1)
        )
        monkeypatch.setattr(
            invoker,
            "_decide_reply",
            lambda _ev, _hist: GateDecision(
                reply=False, reason="ack", category="closing", source="llm"
            ),
        )

        invoker._run_one_inner(_inbound("conv_dm_a"), build_notification_prompt)

        agent.run_conversation.assert_not_called()
        # The agent was never even constructed on the no-reply path.
        assert calls["build_agent"] == []
        assert invoker._breaker.recent_count("conv_dm_a") == 0

    def test_gate_disabled_bypasses_decision(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes.prompts import build_notification_prompt

        invoker, agent, _calls = _wired_invoker(
            monkeypatch,
            tmp_path,
            SimpleNamespace(max_inflight_turns=1, reply_gate_enabled=False),
        )
        decide_calls: list[Any] = []
        monkeypatch.setattr(
            invoker,
            "_decide_reply",
            lambda _ev, _hist: decide_calls.append(1)
            or GateDecision(reply=True, reason="", category="other", source="llm"),
        )

        invoker._run_one_inner(_inbound("conv_dm_a"), build_notification_prompt)

        agent.run_conversation.assert_called_once()
        assert decide_calls == []  # gate disabled → no decision call at all

    def test_decide_reply_circuit_breaker_short_circuits_llm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        invoker, _agent, _calls = _wired_invoker(
            monkeypatch, tmp_path, SimpleNamespace(max_inflight_turns=1)
        )
        invoker._breaker = TurnCircuitBreaker(max_replies=1, window_seconds=60)
        invoker._breaker.record_reply("conv_dm_a")  # now at cap → tripped

        decide_calls: list[Any] = []
        monkeypatch.setattr(
            "agentchatme_hermes.reply_gate.decide",
            lambda **kw: decide_calls.append(kw)
            or GateDecision(reply=True, reason="", category="other", source="llm"),
        )

        decision = invoker._decide_reply(_inbound("conv_dm_a"), [])

        assert decision.reply is False
        assert decision.source == "circuit_breaker"
        # The expensive LLM decision was never consulted.
        assert decide_calls == []

    def test_decide_reply_feeds_signals_to_llm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        invoker, _agent, _calls = _wired_invoker(
            monkeypatch, tmp_path, SimpleNamespace(max_inflight_turns=1)
        )
        invoker._breaker = TurnCircuitBreaker(max_replies=5, window_seconds=60)
        invoker._breaker.record_reply("conv_dm_a")
        invoker._breaker.record_reply("conv_dm_a")  # recent=2, under cap

        monkeypatch.setattr(
            invoker,
            "_resolve_model_and_runtime",
            lambda: ("deepseek-x", {"provider": "deepseek"}),
        )
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            "agentchatme_hermes.reply_gate.decide",
            lambda **kw: captured.update(kw)
            or GateDecision(
                reply=True, reason="x", category="open_request", source="llm"
            ),
        )

        decision = invoker._decide_reply(_inbound("conv_dm_a"), [])

        assert decision.reply is True
        assert captured["handle"] == "me"
        assert captured["recent_reply_count"] == 2
        assert captured["main_runtime"] == {
            "model": "deepseek-x",
            "provider": "deepseek",
        }
