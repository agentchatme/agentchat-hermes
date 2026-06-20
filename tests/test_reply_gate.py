"""Tests for the reply gate.

Pure pieces (message building, JSON parsing) are tested directly. ``decide``
is tested with an injected caller + clock so no provider is hit and latency is
deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from agentchatme_hermes.reply_gate import (
    GateDecision,
    build_decision_messages,
    decide,
    parse_decision,
)
from agentchatme_hermes.types import InboundEvent


def _event(
    *,
    kind: str = "direct",
    text: str = "hello",
    sender: str = "alice",
    conv: str = "conv_dm_1",
    msg: str = "m1",
) -> InboundEvent:
    return InboundEvent(
        message_id=msg,
        conversation_id=conv,
        conversation_kind=kind,  # type: ignore[arg-type]
        sender_handle=sender,
        content_text=text,
        received_at=datetime.now(timezone.utc),
    )


def _times(*vals: float) -> Callable[[], float]:
    """A now_fn that yields the given values in order (2 calls per decide)."""
    it = iter(vals)
    return lambda: next(it)


# ──────────────────────── build_decision_messages ────────────────────────


class TestBuildDecisionMessages:
    def test_two_messages_system_then_user(self) -> None:
        msgs = build_decision_messages(
            handle="me", event=_event(), history=[], recent_reply_count=0
        )
        assert [m["role"] for m in msgs] == ["system", "user"]

    def test_system_carries_handle_and_json_shape(self) -> None:
        msgs = build_decision_messages(
            handle="me", event=_event(), history=[], recent_reply_count=0
        )
        system = msgs[0]["content"]
        assert "@me" in system
        # The decision contract the model must emit.
        assert '"decision"' in system
        assert "no_reply" in system
        # The JSON braces survived the handle substitution (no str.format bug).
        assert "{" in system and "}" in system

    def test_user_carries_signals_and_new_message(self) -> None:
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
        msgs = build_decision_messages(
            handle="me",
            event=_event(text="are you there?", sender="bob"),
            history=history,
            recent_reply_count=4,
        )
        user = msgs[1]["content"]
        assert "Conversation type: direct" in user
        assert "Prior messages in this thread: 2" in user
        assert "recently: 4" in user
        assert "are you there?" in user
        assert "@bob" in user
        # history rendered with speaker labels
        assert "peer: first" in user
        assert "you: second" in user

    def test_group_adds_addressing_hint_yes(self) -> None:
        msgs = build_decision_messages(
            handle="me",
            event=_event(kind="group", text="hey @me can you check this"),
            history=[],
            recent_reply_count=0,
        )
        user = msgs[1]["content"]
        assert "Conversation type: group" in user
        assert "directly addresses you: yes" in user.lower()

    def test_group_addressing_hint_not_explicit(self) -> None:
        msgs = build_decision_messages(
            handle="me",
            event=_event(kind="group", text="anyone around?"),
            history=[],
            recent_reply_count=0,
        )
        assert "not explicitly" in msgs[1]["content"].lower()

    def test_direct_has_no_addressing_hint(self) -> None:
        msgs = build_decision_messages(
            handle="me", event=_event(kind="direct"), history=[], recent_reply_count=0
        )
        assert "directly addresses you" not in msgs[1]["content"].lower()

    def test_first_contact_renders_no_history(self) -> None:
        msgs = build_decision_messages(
            handle="me", event=_event(), history=[], recent_reply_count=0
        )
        assert "first contact" in msgs[1]["content"].lower()

    def test_history_is_tail_capped(self) -> None:
        history = [{"role": "user", "content": f"m{i}"} for i in range(40)]
        msgs = build_decision_messages(
            handle="me",
            event=_event(),
            history=history,
            recent_reply_count=0,
            max_history=5,
        )
        user = msgs[1]["content"]
        assert "m39" in user  # most recent kept
        assert "m0" not in user  # oldest dropped
        # but the true thread depth is still reported from the full history
        assert "Prior messages in this thread: 40" in user


# ──────────────────────── parse_decision ────────────────────────


class TestParseDecision:
    def test_plain_reply(self) -> None:
        d = parse_decision(
            '{"decision": "reply", "reason": "open question", "category": "open_request"}'
        )
        assert d is not None
        assert d.reply is True
        assert d.category == "open_request"
        assert d.reason == "open question"
        assert d.source == "llm"

    def test_plain_no_reply(self) -> None:
        d = parse_decision(
            '{"decision": "no_reply", "reason": "just a thanks", "category": "closing"}'
        )
        assert d is not None
        assert d.reply is False
        assert d.category == "closing"

    def test_fenced_json(self) -> None:
        text = '```json\n{"decision": "no_reply", "reason": "ack", "category": "acknowledgement"}\n```'
        d = parse_decision(text)
        assert d is not None
        assert d.reply is False

    def test_surrounding_prose(self) -> None:
        text = 'Sure, here is my decision: {"decision": "reply", "reason": "x", "category": "new_info"} — done.'
        d = parse_decision(text)
        assert d is not None
        assert d.reply is True
        assert d.category == "new_info"

    def test_reply_synonyms(self) -> None:
        for token in ("reply", "REPLY", "yes", "respond"):
            d = parse_decision(f'{{"decision": "{token}"}}')
            assert d is not None and d.reply is True

    def test_no_reply_synonyms(self) -> None:
        for token in ("no_reply", "no-reply", "noreply", "no", "skip", "silent"):
            d = parse_decision(f'{{"decision": "{token}"}}')
            assert d is not None and d.reply is False

    def test_unknown_category_normalised(self) -> None:
        d = parse_decision('{"decision": "reply", "category": "made_up"}')
        assert d is not None
        assert d.category == "other"

    def test_missing_category_defaults_other(self) -> None:
        d = parse_decision('{"decision": "reply"}')
        assert d is not None
        assert d.category == "other"
        assert d.reason == ""

    def test_reason_truncated(self) -> None:
        long_reason = "x" * 500
        d = parse_decision(f'{{"decision": "reply", "reason": "{long_reason}"}}')
        assert d is not None
        assert len(d.reason) == 280

    def test_missing_decision_returns_none(self) -> None:
        assert parse_decision('{"reason": "no decision key"}') is None

    def test_unknown_decision_token_returns_none(self) -> None:
        assert parse_decision('{"decision": "maybe"}') is None

    def test_non_json_returns_none(self) -> None:
        assert parse_decision("not json at all") is None
        assert parse_decision("") is None
        assert parse_decision("   ") is None

    def test_json_array_returns_none(self) -> None:
        # Valid JSON, but not an object — _extract_json grabs {..} only.
        assert parse_decision("[1, 2, 3]") is None

    def test_propagates_source_and_latency(self) -> None:
        d = parse_decision(
            '{"decision": "reply"}', source="llm", latency_ms=123
        )
        assert d is not None
        assert d.latency_ms == 123


# ──────────────────────── decide ────────────────────────


class TestDecide:
    def _decide(
        self,
        caller: Callable[..., str],
        *,
        fail_open: bool = True,
        now_fn: Callable[[], float] | None = None,
    ) -> GateDecision:
        return decide(
            handle="me",
            event=_event(),
            history=[],
            recent_reply_count=0,
            main_runtime={"model": "test-model"},
            fail_open=fail_open,
            timeout_s=5.0,
            caller=caller,
            now_fn=now_fn or _times(10.0, 10.5),
        )

    def test_reply_decision(self) -> None:
        def caller(**_kw: Any) -> str:
            return '{"decision": "reply", "reason": "open q", "category": "open_request"}'

        d = self._decide(caller)
        assert d.reply is True
        assert d.source == "llm"
        assert d.category == "open_request"
        assert d.latency_ms == 500

    def test_no_reply_decision(self) -> None:
        def caller(**_kw: Any) -> str:
            return '{"decision": "no_reply", "reason": "ack", "category": "acknowledgement"}'

        d = self._decide(caller)
        assert d.reply is False
        assert d.source == "llm"

    def test_caller_exception_fails_open(self) -> None:
        def caller(**_kw: Any) -> str:
            raise RuntimeError("provider down")

        d = self._decide(caller, fail_open=True)
        assert d.reply is True
        assert d.source == "fail_open"
        assert d.category == "fallback"

    def test_caller_exception_fails_closed(self) -> None:
        def caller(**_kw: Any) -> str:
            raise RuntimeError("provider down")

        d = self._decide(caller, fail_open=False)
        assert d.reply is False
        assert d.source == "fail_closed"

    def test_unparseable_output_fails_open(self) -> None:
        def caller(**_kw: Any) -> str:
            return "the model rambled and produced no json"

        d = self._decide(caller, fail_open=True)
        assert d.reply is True
        assert d.source == "fail_open"

    def test_unparseable_output_fails_closed(self) -> None:
        def caller(**_kw: Any) -> str:
            return ""

        d = self._decide(caller, fail_open=False)
        assert d.reply is False
        assert d.source == "fail_closed"

    def test_caller_receives_expected_kwargs(self) -> None:
        captured: dict[str, Any] = {}

        def caller(**kw: Any) -> str:
            captured.update(kw)
            return '{"decision": "no_reply"}'

        self._decide(caller)
        assert "messages" in captured
        assert captured["main_runtime"] == {"model": "test-model"}
        assert captured["timeout"] == 5.0
        assert isinstance(captured["messages"], list)
