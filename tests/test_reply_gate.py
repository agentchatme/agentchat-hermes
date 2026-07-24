"""Tests for the reply gate.

Pure pieces (message building, JSON parsing) are tested directly. ``decide``
is tested with an injected caller + clock so no provider is hit and latency is
deterministic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from agentchatme_hermes.reply_gate import (
    ConversationSignals,
    GateDecision,
    build_decision_messages,
    compute_conversation_signals,
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
    mentions: tuple[str, ...] = (),
    group_name: str | None = None,
) -> InboundEvent:
    return InboundEvent(
        message_id=msg,
        conversation_id=conv,
        conversation_kind=kind,  # type: ignore[arg-type]
        sender_handle=sender,
        content_text=text,
        received_at=datetime.now(timezone.utc),
        mentions=mentions,
        group_name=group_name,
    )


def _times(*vals: float) -> Callable[[], float]:
    """A now_fn that yields the given values in order (2 calls per decide)."""
    it = iter(vals)
    return lambda: next(it)


_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds: float) -> str:
    """ISO timestamp ``seconds`` before the fixed _NOW (negative = future)."""
    return (_NOW - timedelta(seconds=seconds)).isoformat()


def _raw(
    msg_id: str,
    *,
    sender: str = "alice",
    is_own: bool | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a raw get_messages-shaped row for signal tests."""
    row: dict[str, Any] = {
        "id": msg_id,
        "type": "text",
        "content": {"text": "x"},
        "from": f"@{sender}",
    }
    if is_own is not None:
        row["is_own"] = is_own
    if created_at is not None:
        row["created_at"] = created_at
    return row


# ──────────────────────── build_decision_messages ────────────────────────


class TestBuildDecisionMessages:
    def test_two_messages_system_then_user(self) -> None:
        msgs = build_decision_messages(
            handle="me", event=_event(), history=[]
        )
        assert [m["role"] for m in msgs] == ["system", "user"]

    def test_system_carries_handle_and_json_shape(self) -> None:
        msgs = build_decision_messages(
            handle="me", event=_event(), history=[]
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
        )
        user = msgs[1]["content"]
        assert "Conversation type: direct" in user
        assert "Prior messages in this thread: 2" in user
        assert "are you there?" in user
        assert "@bob" in user
        # history rendered with speaker labels
        assert "peer: first" in user
        assert "you: second" in user

    def test_group_states_mention_only_when_mentioned(self) -> None:
        # Mention is derived from the SERVER's parsed list (membership test),
        # not a substring of the text. When named, the positive fact appears.
        msgs = build_decision_messages(
            handle="me",
            event=_event(
                kind="group", text="hey @me can you check this", mentions=("me",)
            ),
            history=[],
        )
        user = msgs[1]["content"]
        assert "Conversation type: group" in user
        assert "You were @-mentioned in this message." in user

    def test_group_omits_mention_line_when_not_mentioned(self) -> None:
        # No "not addressed to you" line at all — negative framing is dropped.
        msgs = build_decision_messages(
            handle="me",
            event=_event(kind="group", text="anyone around?", mentions=()),
            history=[],
        )
        assert "@-mentioned" not in msgs[1]["content"]
        assert "addresses you" not in msgs[1]["content"].lower()

    def test_group_names_the_room_when_the_server_supplies_it(self) -> None:
        msgs = build_decision_messages(
            handle="me",
            event=_event(kind="group", group_name="Ops"),
            history=[],
        )
        assert 'Conversation type: group "Ops"' in msgs[1]["content"]

    def test_direct_has_no_mention_line(self) -> None:
        msgs = build_decision_messages(
            handle="me", event=_event(kind="direct", mentions=("me",)), history=[]
        )
        # Even if somehow flagged, a DM never shows a mention line — you are
        # always the addressee there.
        assert "@-mentioned" not in msgs[1]["content"]

    def test_first_contact_renders_no_history(self) -> None:
        msgs = build_decision_messages(
            handle="me", event=_event(), history=[]
        )
        assert "first contact" in msgs[1]["content"].lower()

    def test_history_is_tail_capped(self) -> None:
        history = [{"role": "user", "content": f"m{i}"} for i in range(40)]
        msgs = build_decision_messages(
            handle="me",
            event=_event(),
            history=history,
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


# ──────────────────────── compute_conversation_signals ────────────────────────


def _signals(
    messages: list[Any], *, trigger: str = "trigger", own: str = "me"
) -> ConversationSignals:
    return compute_conversation_signals(
        messages, own_handle=own, trigger_message_id=trigger, now=_NOW
    )


class TestConversationSignals:
    def test_empty_is_first_contact(self) -> None:
        s = _signals([])
        assert s.first_contact is True
        assert s.you_have_spoken is False
        assert s.seconds_since_previous is None
        assert s.messages_last_window == 1  # just the new message

    def test_you_have_spoken_when_own_present(self) -> None:
        msgs = [
            _raw("m1", sender="me", is_own=True, created_at=_ago(30)),
            _raw("m2", sender="alice", is_own=False, created_at=_ago(10)),
        ]
        s = _signals(msgs)
        assert s.first_contact is False
        assert s.you_have_spoken is True

    def test_you_have_not_spoken_when_only_peer(self) -> None:
        msgs = [_raw("m1", sender="alice", is_own=False, created_at=_ago(10))]
        s = _signals(msgs)
        assert s.first_contact is False
        assert s.you_have_spoken is False

    def test_handle_fallback_when_no_is_own(self) -> None:
        # No is_own field → fall back to handle compare.
        s = _signals([_raw("m1", sender="me", created_at=_ago(10))])
        assert s.you_have_spoken is True

    def test_cadence_window_counts_recent_plus_new(self) -> None:
        msgs = [
            _raw("m1", created_at=_ago(120)),  # outside the 60s window
            _raw("m2", created_at=_ago(30)),  # inside
            _raw("m3", created_at=_ago(5)),  # inside
        ]
        s = _signals(msgs)
        assert s.messages_last_window == 3  # 2 inside + the new message

    def test_seconds_since_previous_uses_latest(self) -> None:
        msgs = [_raw("m1", created_at=_ago(40)), _raw("m2", created_at=_ago(8))]
        s = _signals(msgs)
        assert s.seconds_since_previous == 8.0

    def test_trigger_message_excluded(self) -> None:
        s = _signals([_raw("trigger", created_at=_ago(5))], trigger="trigger")
        assert s.first_contact is True  # the only row was the trigger

    def test_malformed_timestamps_skipped(self) -> None:
        msgs = [
            {"id": "m1", "from": "@alice", "created_at": "not-a-date"},
            {"id": "m2", "from": "@alice"},  # no created_at
        ]
        s = _signals(msgs)
        assert s.first_contact is False  # rows exist
        assert s.seconds_since_previous is None  # but none parse
        assert s.messages_last_window == 1

    def test_naive_timestamp_assumed_utc(self) -> None:
        # Naive (no tz) is assumed UTC — 10s before _NOW.
        s = _signals([_raw("m1", created_at="2026-06-21T11:59:50")])
        assert s.seconds_since_previous == 10.0

    def test_future_timestamp_clamped_to_zero(self) -> None:
        s = _signals([_raw("m1", created_at=_ago(-5))])  # 5s into the future
        assert s.seconds_since_previous == 0.0

    def test_non_dict_rows_ignored(self) -> None:
        s = _signals(["garbage", None, _raw("m1", created_at=_ago(5))])
        assert s.first_contact is False
        assert s.messages_last_window == 2  # 1 valid in-window + new


# ──────────────────────── signals rendering ────────────────────────


class TestSignalsRendering:
    def _user(self, signals: ConversationSignals | None) -> str:
        msgs = build_decision_messages(
            handle="me",
            event=_event(),
            history=[{"role": "user", "content": "hi"}],
            signals=signals,
        )
        return msgs[1]["content"]

    def test_established_and_pace_render(self) -> None:
        sig = ConversationSignals(
            first_contact=False,
            you_have_spoken=True,
            messages_last_window=5,
            seconds_since_previous=8.0,
        )
        user = self._user(sig)
        assert "Relationship: established" in user
        assert "Pace: 5 message(s) in the last" in user
        assert "8s since the" in user

    def test_first_contact_renders_and_no_pace_without_gap(self) -> None:
        sig = ConversationSignals(
            first_contact=True,
            you_have_spoken=False,
            messages_last_window=1,
            seconds_since_previous=None,
        )
        user = self._user(sig)
        assert "first contact" in user.lower()
        assert "Pace:" not in user  # no gap → no pace line

    def test_none_signals_omits_both_lines(self) -> None:
        user = self._user(None)
        assert "Relationship:" not in user
        assert "Pace:" not in user
