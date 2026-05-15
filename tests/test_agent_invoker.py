"""Tests for the pure helpers in ``agentchatme_hermes.agent_invoker``.

We unit-test the conversation-history translation in isolation — no
Hermes, no SDK, no runtime. The end-to-end ``AgentInvoker._run_one``
path requires a real Hermes ``AIAgent`` and is exercised through the
integration suite (planned for a later commit).
"""
from __future__ import annotations

from typing import Any

from agentchatme_hermes.agent_invoker import (
    _FALLBACK_MODEL,
    _coerce_model_string,
    _extract_messages_list,
    _translate_messages_to_history,
)


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
