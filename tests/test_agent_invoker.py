"""Tests for ``agentchatme_hermes.agent_invoker`` — wake → gate → turn, on the SDK gate.

The Hermes side (AIAgent, SessionDB, provider resolution) is stubbed; the
SDK client is a recorder; the model answer is injected through
``build_call_model`` so the real ``agentchatme.gate.run`` does the parsing
and the recording. What must hold:

* a wake becomes exactly one cycle: one tray read, one gate call;
* ``dismiss`` / ``defer`` are recorded at once, with the card's anchor;
* ``reply`` starts ONE turn with the served wake instruction, and each
  ``reply`` is recorded only once the turn has COMPLETED — after
  ``run_conversation`` returned — so the turn's own inbox read still sees
  the card;
* a turn that fails to start, or that raises, records nothing and re-wakes
  on the ladder; a turn is not started while Hermes has not registered the
  hosted ``mcp__agentchat__*`` tools (bounded hold);
* fail-closed records nothing, starts nothing, and re-wakes on the ladder;
* gate disabled → the turn starts straight from the tray, nothing recorded;
* no served texts → no turn, nothing recorded for ``reply``;
* a wake during a cycle earns one more cycle afterwards;
* group invites run their own turn; one that could not start is retried on
  the next wake; one that ran is never run again after a restart.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from agentchatme import gate
from agentchatme.types import InboxTray, Texts

from agentchatme_hermes import agent_invoker as invoker_module
from agentchatme_hermes import hosted_tools as hosted_tools_module
from agentchatme_hermes import turn_runner as turn_runner_module
from agentchatme_hermes.agent_invoker import _FALLBACK_MODEL, AgentInvoker, _coerce_model_string
from agentchatme_hermes.turn import invite_turn_prompt, wake_turn_prompt
from agentchatme_hermes.types import AgentIdentity

if TYPE_CHECKING:
    from pathlib import Path

_REAL_PROBE = hosted_tools_module.hosted_tool_names

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


def _card(cid: str) -> dict[str, Any]:
    return {
        "conversation_id": cid,
        "type": "direct",
        "counterparty": {"handle": "@peer"},
        "relationship": "established",
        "you_owe_reply": True,
        "mentioned_you": False,
        "unread": 1,
        "pace": {"last_60s": 0, "last_10m": 1},
        "preview": {
            "message_id": f"msg_{cid}",
            "sender": "@peer",
            "text": "hello",
            "at": "2026-11-04T22:58:10.000Z",
        },
    }


def _tray(
    *cards: dict[str, Any],
    truncated: bool = False,
    invites: list[dict[str, Any]] | None = None,
    invites_truncated: bool = False,
) -> InboxTray:
    return InboxTray.model_validate(
        {
            "version": 1,
            "you": "@me",
            "question": "Decide per card with agentchat_decide.",
            "categories": ["open_request", "no_action_needed"],
            "conversations": list(cards),
            "deferred": 0,
            "truncated": truncated,
            "invites": invites or [],
            "invites_truncated": invites_truncated,
        }
    )


def _tray_invite(invite_id: str = "ginv_1") -> dict[str, Any]:
    """A pending group invite as the tray carries it (contract v1)."""
    return {
        "invite_id": invite_id,
        "group": {"id": "grp_1", "name": "Council", "member_count": 3},
        "invited_by": "@carol",
        "at": "2026-11-08T10:00:00.000Z",
    }


def _answer(**decisions: str) -> str:
    return json.dumps(
        {"decisions": [{"conversation_id": cid, "decision": d} for cid, d in decisions.items()]}
    )


class _Client:
    """The SDK client as the invoker uses it: ``inbox.tray`` and ``decisions.record``."""

    def __init__(self, trays: list[Any]) -> None:
        self.trays = list(trays)
        self.tray_calls: list[dict[str, Any]] = []
        self.recorded: list[tuple[str, str, str | None]] = []
        self.record_error: Exception | None = None
        # A live fake inbox: a callable that builds the tray from the state
        # the server would hold (what is unread given what was recorded).
        self.live_inbox: Any = None
        # What the server shows after a turn: by default the last served
        # tray minus what was recorded (a card stays unread until something
        # reads it). A test sets ``turn_view`` (a tray, or an exception) to
        # say what the turn left; ``after_turn`` is that view, armed when the
        # turn ends and consumed by the host's one post-turn read.
        self.turn_view: Any = None
        self.after_turn: Any = None
        # Whether the default fake turn sent its answer: the post-turn view
        # then shows the reply cards with ``you_owe_reply: false``.
        self.turn_sends = True
        self.last_served: InboxTray | None = None
        self.inbox = SimpleNamespace(tray=self._tray)
        self.decisions = SimpleNamespace(record=self._record)
        self.dismissed_invites: list[str] = []
        self.groups = SimpleNamespace(dismiss_invite=self.dismissed_invites.append)

    def _tray(self, *, wait_ms: int = 0, limit: int | None = None) -> InboxTray:
        self.tray_calls.append({"wait_ms": wait_ms, "limit": limit})
        if self.live_inbox is not None:
            return self.live_inbox()
        if self.after_turn is not None:
            # The host's one read after a completed turn (``_decided_in_turn``).
            view = self.after_turn
            self.after_turn = None
            if isinstance(view, Exception):
                raise view
            assert isinstance(view, InboxTray)
            return view
        nxt = self.trays.pop(0) if self.trays else _tray()
        if isinstance(nxt, Exception):
            raise nxt
        assert isinstance(nxt, InboxTray)
        self.last_served = nxt
        return nxt

    def _record(
        self,
        conversation_id: str,
        *,
        decision: str,
        category: str | None = None,
        through_message_id: str | None = None,
    ) -> dict[str, Any]:
        if self.record_error is not None:
            raise self.record_error
        self.recorded.append((conversation_id, decision, through_message_id))
        return {"decision": decision}


def _leave_unread_on_the_tray(client: _Client) -> None:
    """A turn that recorded nothing: after it, the server still shows every
    card of the last served tray that nobody recorded (unread stays on the
    tray). The host's post-turn read sees that, unless the test set its own."""
    if client.turn_view is not None:
        client.after_turn = client.turn_view
        return
    served = client.last_served
    if served is None:
        return
    recorded = {cid for cid, _decision, _anchor in client.recorded}
    cards = [c for c in gate.cards_of(served) if c["conversation_id"] not in recorded]
    if client.turn_sends:
        cards = [{**c, "you_owe_reply": False, "your_last_message_at": "2026-11-04T23:00:00.000Z"} for c in cards]
    client.after_turn = _tray(*cards, truncated=bool(served.truncated))


def _fake_stop_wait(monkeypatch: pytest.MonkeyPatch, inv: AgentInvoker, clock: dict[str, float] | None = None) -> list[float]:
    """The hosted-tools hold polls on the invoker's stop event; record those
    waits (and advance an injected clock) instead of sleeping."""
    waits: list[float] = []
    real_is_set = inv._stopped.is_set

    def wait(seconds: float | None = None) -> bool:
        waits.append(float(seconds or 0.0))
        if clock is not None:
            clock["t"] += float(seconds or 0.0)
        return real_is_set()

    monkeypatch.setattr(inv._stopped, "wait", wait)
    return waits


def _invoker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trays: list[Any],
    answer: str | Exception | None = None,
    config: Any = None,
    texts: Texts | None = TEXTS,
) -> tuple[AgentInvoker, _Client, MagicMock, list[str]]:
    """An invoker with a stubbed runtime, agent and model answer.

    ``events`` records the order of the load-bearing steps: ``build``
    (agent constructed), ``record:<id>:<decision>``, ``run`` (turn entered).
    """
    client = _Client(trays)
    runtime = SimpleNamespace(client=client, texts=texts)
    cfg = config or SimpleNamespace(wake_debounce_s=0.0)
    inv = AgentInvoker(config=cfg, identity=AgentIdentity(handle="me"), runtime=runtime)

    events: list[str] = []
    agent = MagicMock()

    def run_conversation(prompt: str, conversation_history: list[Any]) -> dict[str, Any]:
        events.append("run")
        return {"final_response": "ok"}

    agent.run_conversation.side_effect = run_conversation
    original_record = client._record

    def recording(conversation_id: str, **kw: Any) -> dict[str, Any]:
        out = original_record(conversation_id, **kw)
        events.append(f"record:{conversation_id}:{kw['decision']}")
        return out

    client.decisions = SimpleNamespace(record=recording)

    def build_agent(_session_id: str) -> Any:
        events.append("build")
        return agent

    # When a turn ends, the server still shows what nobody recorded; the
    # host's post-turn read (``_decided_in_turn``) sees that view.
    real_start_turn = inv._start_turn

    def start_turn(**kw: Any) -> Any:
        started = real_start_turn(**kw)
        if started is None or "decided to answer" not in str(kw.get("prompt", "")):
            # Not started, or a turn with no gate ``reply`` (invite, gate
            # off): the host makes no post-turn read.
            return started

        def run() -> Any:
            try:
                return started()
            finally:
                _leave_unread_on_the_tray(client)

        return run

    monkeypatch.setattr(inv, "_start_turn", start_turn)
    monkeypatch.setattr(inv, "_ensure_hermes_resolved", lambda: None)
    monkeypatch.setattr(inv, "_build_agent", build_agent)
    monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {"provider": "p"}))
    # Hermes has the hosted tools registered unless a test says otherwise.
    monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", lambda: ["mcp__agentchat__agentchat_inbox"])

    if answer is not None:

        def fake_build_call_model(**_kw: Any) -> Any:
            def call_model(prompt: str) -> str:
                events.append("model")
                if isinstance(answer, Exception):
                    raise answer
                return answer

            return call_model

        monkeypatch.setattr(invoker_module, "build_call_model", fake_build_call_model)
    return inv, client, agent, events


# ──────────────────────── _coerce_model_string ────────────────────────


class TestCoerceModelString:
    def test_nested_dict_with_default(self) -> None:
        assert _coerce_model_string({"default": "d", "provider": "x", "model": "d"}) == "d"

    def test_default_beats_model(self) -> None:
        assert _coerce_model_string({"default": "primary", "model": "fallback"}) == "primary"

    def test_model_when_no_default(self) -> None:
        assert _coerce_model_string({"model": "the-model"}) == "the-model"

    def test_flat_string(self) -> None:
        assert _coerce_model_string("flat") == "flat"

    @pytest.mark.parametrize("value", ["", None, {}, {"provider": "x"}, {"default": 42}, 42, ["a"]])
    def test_unusable_shapes_fall_back(self, value: Any) -> None:
        assert _coerce_model_string(value) == _FALLBACK_MODEL


# ──────────────────────── one cycle ────────────────────────


class TestRunCycle:
    def test_reply_starts_one_turn_and_records_after_the_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, events = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"), _card("b"))],
            answer=_answer(a="reply", b="dismiss"),
        )
        result = inv.run_cycle()
        assert result is not None and result.outcome == "decided"
        # The gate's tray, then one read after the completed turn to see
        # whether the turn recorded its own verdict.
        assert client.tray_calls == [{"wait_ms": 0, "limit": 50}, {"wait_ms": 0, "limit": 50}]
        # dismiss recorded by the SDK gate at once, with the card's anchor;
        # reply recorded by the host only after the turn has completed.
        assert events == ["model", "record:b:dismiss", "build", "run", "record:a:reply"]
        assert client.recorded == [("b", "dismiss", "msg_b"), ("a", "reply", "msg_a")]
        agent.run_conversation.assert_called_once_with(
            wake_turn_prompt(TEXTS, ["a"]), conversation_history=[]
        )
        assert inv.turns == 1 and inv.recorded_replies == 1

    def test_all_three_verdicts_are_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, _agent, _events = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"), _card("b"), _card("c"))],
            answer=_answer(a="reply", b="dismiss", c="defer"),
        )
        inv.run_cycle()
        assert sorted(client.recorded) == [
            ("a", "reply", "msg_a"),
            ("b", "dismiss", "msg_b"),
            ("c", "defer", "msg_c"),
        ]

    def test_dismiss_only_means_no_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="dismiss")
        )
        inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert client.recorded == [("a", "dismiss", "msg_a")]
        assert inv.turns == 0

    def test_empty_tray_asks_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, events = _invoker(monkeypatch, trays=[_tray()], answer=_answer(a="reply"))
        result = inv.run_cycle()
        assert result is not None and result.outcome == "empty"
        assert events == []
        agent.run_conversation.assert_not_called()
        assert client.recorded == []

    def test_fail_closed_records_nothing_starts_nothing_and_rewakes(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=RuntimeError("provider down")
        )
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            result = inv.run_cycle()
        assert result is not None and result.outcome == "fail_closed"
        agent.run_conversation.assert_not_called()
        assert client.recorded == []
        assert inv.left_on_tray == 1
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "logged once"
        assert "provider down" in warnings[0].getMessage()

    def test_unusable_answer_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer="I would rather not say."
        )
        result = inv.run_cycle()
        assert result is not None and result.outcome == "fail_closed"
        agent.run_conversation.assert_not_called()
        assert client.recorded == [] and inv.left_on_tray == 1

    def test_failed_cycles_schedule_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Contract v1 (D3): the server re-wakes an agent with unanswered
        cards, on the socket that negotiated ``rewake``. No local ladder: a
        failed cycle arms no timer of its own."""
        inv, _client, _agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))] * 5, answer=RuntimeError("down")
        )
        for _ in range(5):
            inv.run_cycle()
        assert inv.left_on_tray == 5 and inv.last_left_reason is not None
        assert not inv._coalescer.armed, "nothing scheduled"
        assert not hasattr(invoker_module, "RETRY_LADDER_S")

    def test_turn_that_fails_to_start_records_nothing_and_rewakes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), _card("b"))], answer=_answer(a="reply", b="dismiss")
        )
        monkeypatch.setattr(inv, "_build_agent", lambda _session_id: None)
        inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert client.recorded == [("b", "dismiss", "msg_b")], "reply NOT recorded: it stays unread"
        assert "record:a:reply" not in events
        assert inv.turns == 0 and inv.recorded_replies == 0
        assert inv.left_on_tray == 1

    def test_crashed_turn_records_nothing_and_rewakes(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``run_conversation`` raising: the turn did not look, so nothing is
        recorded — the card stays on the tray, the ladder brings the next
        wake, the worker survives."""
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), _card("b"))], answer=_answer(a="reply", b="dismiss")
        )
        agent.run_conversation.side_effect = RuntimeError("provider exploded")
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            inv.run_cycle()  # must not raise
        assert client.recorded == [("b", "dismiss", "msg_b")], "reply NOT recorded"
        assert "record:a:reply" not in events
        assert inv.turns == 0 and inv.recorded_replies == 0
        assert inv.left_on_tray == 1
        assert any("nothing recorded" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "result",
        [
            {"completed": False, "failed": True, "error": "usage limit reached", "failure_reason": "usage_limit"},
            {"completed": False, "failed": True, "error": "content_policy_blocked: x"},
            {"final_response": "half", "completed": False, "error": "half", "partial": True},
            {"completed": False},
        ],
        ids=["failed", "policy", "partial", "not-completed"],
    )
    def test_failed_or_partial_result_is_a_crash_nothing_recorded(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, result: dict[str, Any]
    ) -> None:
        """Hermes reports provider errors, usage limits, policy blocks and partial
        turns IN the result, not as exceptions. Such a turn read nothing."""
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), _card("b"))], answer=_answer(a="reply", b="dismiss")
        )
        agent.run_conversation.side_effect = lambda prompt, conversation_history: result
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            inv.run_cycle()
        assert client.recorded == [("b", "dismiss", "msg_b")], "reply NOT recorded"
        assert "record:a:reply" not in events
        assert inv.turns == 0 and inv.recorded_replies == 0
        assert inv.left_on_tray == 1
        messages = [r.getMessage() for r in caplog.records]
        assert any("did not complete" in m for m in messages)
        if result.get("error"):
            assert any(str(result["error"])[:20] in m for m in messages), "the error text is logged"

    def test_turn_whose_agentchat_tool_calls_all_failed_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Seen live under a flapping MCP transport: the turn ran to the end,
        every hosted tool call returned ``{"error": ...}``, the model gave up
        in text — a 'completed' turn that never looked."""
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )
        transcript = [
            {"role": "user", "content": "You were woken"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "mcp__agentchat__agentchat_inbox", "arguments": "{}"}}]},
            {"role": "tool", "name": "mcp__agentchat__agentchat_inbox", "tool_call_id": "c1", "content": '{"error": "MCP call failed: MCPError: Connection closed"}'},
            {"role": "tool", "name": "tool_call", "tool_call_id": "c2", "content": '{"error": "MCP server \'agentchat\' transport is down; reconnect requested."}'},
            {"role": "assistant", "content": "Could not reach AgentChat."},
        ]
        agent.run_conversation.side_effect = lambda prompt, conversation_history: {"final_response": "Could not reach AgentChat.", "messages": transcript, "completed": True}
        client.turn_sends = False
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.agent_invoker"):
            inv.run_cycle()
        assert client.recorded == [] and "record:a:reply" not in events
        assert inv.outcomes["could_not_look"] == 1 and inv.left_on_tray == 1
        assert any("could_not_look" in r.getMessage() and "1 AgentChat call(s), 1 failed" in r.getMessage() for r in caplog.records)

    def test_turn_with_no_agentchat_call_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The reply rule: a turn that made no AgentChat call could not look.
        It is not a success (the pre-kit integration recorded ``reply`` for
        it): nothing is recorded, the card stays on the tray."""
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )
        transcript = [
            {"role": "user", "content": "You were woken"},
            {"role": "tool", "name": "terminal", "tool_call_id": "c1", "content": "ok"},
            {"role": "assistant", "content": "Nothing to do."},
        ]
        agent.run_conversation.side_effect = lambda prompt, conversation_history: {"final_response": "Nothing to do.", "messages": transcript, "completed": True}
        client.turn_sends = False
        inv.run_cycle()
        assert client.recorded == [] and "record:a:reply" not in events
        assert inv.outcomes["could_not_look"] == 1 and inv.left_on_tray == 1
        assert inv.status()["last_turn"]["agentchat_calls"] == {"made": 0, "failed": 0}

    def test_count_agentchat_calls_helper(self) -> None:
        ok_wrapped = '<untrusted_tool_result source="mcp__agentchat__agentchat_inbox">\nThe following content ...\n\n{"result": "{\\n  \\"you\\": \\"@me\\"}"}'
        err = '{"error": "MCP call failed: MCPError: Connection closed"}'
        count = turn_runner_module.count_agentchat_calls
        # one success among failures → looked
        both = count({"messages": [
            {"role": "tool", "name": "mcp__agentchat__agentchat_inbox", "content": err},
            {"role": "tool", "name": "mcp__agentchat__agentchat_get_conversation", "content": ok_wrapped},
        ]})
        assert both == turn_runner_module.AgentChatCallCount(made=2, failed=1)
        # through Hermes' tool_call bridge: the wrapper names the hosted tool
        bridged = count({"messages": [{"role": "tool", "name": "tool_call", "content": ok_wrapped}]})
        assert bridged == turn_runner_module.AgentChatCallCount(made=1, failed=0)
        # every AgentChat call failed
        failed = count({"messages": [
            {"role": "tool", "name": "mcp__agentchat__agentchat_inbox", "content": err},
            {"role": "tool", "name": "mcp__agentchat__agentchat_send_message", "content": err},
        ]})
        assert failed == turn_runner_module.AgentChatCallCount(made=2, failed=2)
        # no AgentChat tool call at all → zero (the reply rule: could not look)
        none = count({"messages": [{"role": "tool", "name": "terminal", "content": err}, {"role": "assistant", "content": "hi"}]})
        assert none == turn_runner_module.AgentChatCallCount(made=0, failed=0)
        assert count({"messages": []}) == turn_runner_module.AgentChatCallCount(made=0, failed=0)
        # no transcript: unknown (None), not zero — the reply rule takes it as "it looked"
        for unknown in ({"final_response": "ok"}, "not a dict", None):
            assert count(unknown) is None
        # a wrapped error payload counts as an error; a wrapped result does not
        assert turn_runner_module._is_tool_error('<untrusted_tool_result source="mcp__agentchat__agentchat_inbox">\n\n{"error": "GROUP_DELETED"}') is True
        assert turn_runner_module._is_tool_error(ok_wrapped) is False
        assert turn_runner_module._is_tool_error("plain text") is False

    def test_turn_failure_helper(self) -> None:
        failure = turn_runner_module.turn_failure
        assert failure({"final_response": "ok"}) is None
        assert failure({"completed": True}) is None
        assert failure("not a dict") is None
        assert failure(None) is None
        assert failure({"failed": True, "error": "boom", "failure_reason": "provider_error"}) == "failed, provider_error: boom"
        assert failure({"partial": True, "error": "cut"}) == "partial: cut"
        assert failure({"completed": False}) == "not completed: no error text"
        assert failure({"completed": False, "interrupted": True, "turn_exit_reason": "interrupted_by_user"}) == "interrupted, interrupted_by_user: no error text"

    def test_turn_that_recorded_its_own_verdict_is_not_recorded_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The served texts tell the model to record reply after its send (or
        # dismiss when it read and chose not to answer). The card then left
        # the tray: the host records nothing more, so the agent's own last
        # verdict stands and the reply is counted once.
        inv, client, _agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )
        client.turn_view = _tray()
        inv.run_cycle()
        assert events == ["model", "build", "run"]
        assert client.recorded == []
        assert inv.outcomes["card_left_tray"] == 1 and inv.recorded_replies == 0
        assert inv.turns == 1

    def test_a_verdict_in_the_turn_seen_on_a_card_kept_by_a_newer_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The turn answered and recorded reply; a newer peer message keeps the
        # card on the tray. Its your_last_decision moved: no second reply.
        before = _card("a")
        after = {**_card("a"), "your_last_decision": {"decision": "reply", "category": None, "at": "2026-11-07T10:00:05.000Z"}}
        inv, client, _agent, _events = _invoker(monkeypatch, trays=[_tray(before)], answer=_answer(a="reply"))
        client.turn_view = _tray(after)
        inv.run_cycle()
        assert client.recorded == []
        assert inv.outcomes["turn_recorded_verdict"] == 1

    def test_an_older_verdict_on_the_card_is_not_the_turns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The card carried a verdict from an earlier cycle; the turn sent its
        # answer and recorded nothing (same your_last_decision after it, older
        # than the turn): the host records reply.
        old = {"decision": "defer", "category": None, "at": "2026-11-06T09:00:00.000Z"}
        card = {**_card("a"), "your_last_decision": old}
        inv, client, _agent, _events = _invoker(monkeypatch, trays=[_tray(card)], answer=_answer(a="reply"))
        client.turn_view = _tray({**card, "you_owe_reply": False})
        inv.run_cycle()
        assert client.recorded == [("a", "reply", "msg_a")]
        assert inv.outcomes["answer_sent"] == 1 and inv.recorded_replies == 1

    def test_a_failed_re_read_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Contract v1 (D5): what the turn did is unknown, so nothing is
        # recorded; the card stays and the next wake looks again (the
        # pre-kit integration recorded reply here).
        inv, client, _agent, _events = _invoker(monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply"))
        client.turn_view = RuntimeError("inbox down")
        inv.run_cycle()
        assert client.recorded == [] and inv.outcomes["card_unknown"] == 1
        assert inv.left_on_tray == 1

    def test_a_card_missing_from_a_truncated_tray_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Past a truncated page the card may still be unread: claim nothing.
        inv, client, _agent, _events = _invoker(monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply"))
        client.turn_view = _tray(_card("x"), truncated=True)
        inv.run_cycle()
        assert client.recorded == [] and inv.outcomes["card_unknown"] == 1

    def test_turn_reads_the_card_before_reply_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """What the ordering is for: the turn's first ``agentchat_inbox`` must
        still see the card. A fake hosted inbox serves the card while the
        conversation is unread and an empty tray (as the server does) once a
        ``reply`` / ``dismiss`` verdict has been recorded for it."""
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )
        read_by_turn: list[list[str]] = []

        def hosted_inbox() -> list[str]:
            read = {cid for cid, decision, _anchor in client.recorded if decision in ("reply", "dismiss")}
            return [cid for cid in ("a",) if cid not in read]

        def run_conversation(prompt: str, conversation_history: list[Any]) -> dict[str, Any]:
            events.append("run")
            read_by_turn.append(hosted_inbox())  # the turn's first step, per the wake instruction
            return {"final_response": "sent"}

        agent.run_conversation.side_effect = run_conversation
        inv.run_cycle()
        assert read_by_turn == [["a"]], "the card was on the tray when the turn looked — no empty park"
        assert client.recorded == [("a", "reply", "msg_a")]
        assert events.index("run") < events.index("record:a:reply")

    def test_a_turn_that_looked_and_did_nothing_is_dismissed_the_second_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Contract v1 (D5): looked, sent nothing, recorded nothing → nothing
        # the first time (the card stays, the next wake looks again); the
        # second time for the same anchor → dismiss, no_action_needed.
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a")), _tray(_card("a"))], answer=_answer(a="reply")
        )
        client.turn_sends = False
        agent.run_conversation.side_effect = lambda prompt, conversation_history: {"final_response": ""}
        inv.run_cycle()
        assert client.recorded == [] and inv.outcomes["silent_first"] == 1
        inv.run_cycle()
        assert client.recorded == [("a", "dismiss", "msg_a")] and inv.outcomes["silent_again"] == 1
        assert inv.recorded_dismisses == 1 and inv.turns == 2

    def test_a_newer_anchor_starts_the_silence_count_over(self, monkeypatch: pytest.MonkeyPatch) -> None:
        newer = _card("a")
        newer["preview"]["message_id"] = "msg_a2"
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a")), _tray(newer)], answer=_answer(a="reply")
        )
        client.turn_sends = False
        agent.run_conversation.side_effect = lambda prompt, conversation_history: {"final_response": ""}
        inv.run_cycle()
        inv.run_cycle()
        assert client.recorded == [] and inv.outcomes["silent_first"] == 2

    def test_turn_waits_for_the_hosted_tools(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """Hermes registers the ``agentchat`` MCP tools after the gateway starts;
        the turn is held until they are there, then runs and records."""
        inv, client, _agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )
        answers: list[list[str]] = [[], []]
        monkeypatch.setattr(
            hosted_tools_module, "hosted_tool_names",
            lambda: answers.pop(0) if answers else ["mcp__agentchat__agentchat_inbox"],
        )
        slept = _fake_stop_wait(monkeypatch, inv)
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.hosted_tools"):
            inv.run_cycle()
        assert slept == [hosted_tools_module.HOSTED_TOOLS_POLL_S] * 2
        assert events[-2:] == ["run", "record:a:reply"]
        assert client.recorded == [("a", "reply", "msg_a")]
        assert any("holding the turn" in r.getMessage() for r in caplog.records)

    def test_no_hosted_tools_within_the_bound_is_a_turn_not_started(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Past the bound nothing is recorded, no turn runs, the ladder re-wakes
        (leg 7a of the 0.4.0 live-fire: a tool-less turn had recorded the reply)."""
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), _card("b"))], answer=_answer(a="reply", b="dismiss")
        )
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", lambda: [])
        clock = {"t": 0.0}
        _fake_stop_wait(monkeypatch, inv, clock)
        monkeypatch.setattr(hosted_tools_module.time, "monotonic", lambda: clock["t"])
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.hosted_tools"):
            inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert "build" not in events, "no agent is even built"
        assert client.recorded == [("b", "dismiss", "msg_b")], "reply NOT recorded"
        assert inv.turns == 0 and inv.recorded_replies == 0
        assert inv.left_on_tray == 1
        assert clock["t"] == pytest.approx(hosted_tools_module.HOSTED_TOOLS_WAIT_S)
        assert any("not started" in r.getMessage() for r in caplog.records)

    def test_stop_during_the_hold_starts_nothing(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """A stop (leader lease gone) ends the hold at once: no turn starts
        seconds after the lease, nothing is recorded."""
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", lambda: [])
        def wait_then_stop(seconds: float | None = None) -> bool:
            inv._stopped.set()  # the stop lands during the first poll
            return True

        monkeypatch.setattr(inv._stopped, "wait", wait_then_stop)
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.hosted_tools"):
            inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert "build" not in events and client.recorded == []
        assert any("stopping while holding" in r.getMessage() for r in caplog.records)

    def test_outside_hermes_the_hold_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", lambda: None)
        slept = _fake_stop_wait(monkeypatch, inv)
        inv.run_cycle()
        assert slept == [] and events[-1] == "record:a:reply"

    def test_record_failure_after_start_is_logged_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )

        def failing_record(conversation_id: str, **_kw: Any) -> dict[str, Any]:
            raise RuntimeError("503")

        client.decisions = SimpleNamespace(record=failing_record)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            inv.run_cycle()
        agent.run_conversation.assert_called_once()
        assert any("not recorded" in r.getMessage() for r in caplog.records)
        assert inv.turns == 1 and inv.recorded_replies == 0, "the turn ran; the verdict did not land"
        # The card is still on the tray: the server's re-wake brings the next look.
        assert inv.left_on_tray == 1

    def test_reply_record_failure_does_not_leak_into_the_next_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, _agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a")), _tray(_card("a"))], answer=_answer(a="reply")
        )
        calls = {"n": 0}
        real = client._record

        def flaky(conversation_id: str, **kw: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("503")
            return real(conversation_id, **kw)

        client.decisions = SimpleNamespace(record=flaky)
        inv.run_cycle()
        inv.run_cycle()
        assert inv.left_on_tray == 1, "only the failed cycle left a card"
        assert client.recorded == [("a", "reply", "msg_a")]

    def test_without_texts_the_gate_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate prompt's words are served: without texts the SDK gate
        # judges nothing (no model call), records nothing, starts nothing.
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), _card("b"))], answer=_answer(a="reply", b="defer"), texts=None
        )
        result = inv.run_cycle()
        assert result is not None and result.outcome == "fail_closed"
        agent.run_conversation.assert_not_called()
        assert client.recorded == [] and "model" not in events
        assert inv.left_on_tray == 1

    def test_gate_disabled_starts_the_turn_from_the_tray(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, events = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"))],
            answer=_answer(a="reply"),
            config=SimpleNamespace(wake_debounce_s=0.0, reply_gate_enabled=False),
        )
        result = inv.run_cycle()
        assert result is not None and result.outcome == "decided"
        assert "model" not in events, "gate off → the model is not asked by the integration"
        agent.run_conversation.assert_called_once_with(wake_turn_prompt(TEXTS), conversation_history=[])
        assert client.recorded == [], "the turn records its own verdicts with the hosted tools"

    def test_tray_read_failure_rewakes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, agent, _events = _invoker(
            monkeypatch, trays=[ConnectionError("503")], answer=_answer(a="reply")
        )
        assert inv.run_cycle() is None
        agent.run_conversation.assert_not_called()
        assert inv.left_on_tray == 1

    def test_truncated_tray_wakes_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), truncated=True)], answer=_answer(a="dismiss")
        )
        wakes: list[int] = []
        monkeypatch.setattr(inv._coalescer, "wake", lambda: wakes.append(1))
        inv.run_cycle()
        assert wakes == [1]

    def test_undecided_cards_rewake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), _card("b"))], answer=_answer(a="dismiss")
        )
        result = inv.run_cycle()
        assert result is not None and result.undecided == ["b"]
        assert inv.left_on_tray == 1

    def test_gate_prompt_spells_tools_for_hermes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        prompts: list[str] = []

        def fake_build_call_model(**_kw: Any) -> Any:
            def call_model(prompt: str) -> str:
                prompts.append(prompt)
                return _answer(a="dismiss")

            return call_model

        inv, _client, _agent, _events = _invoker(monkeypatch, trays=[_tray(_card("a"))])
        monkeypatch.setattr(invoker_module, "build_call_model", fake_build_call_model)
        inv.run_cycle()
        assert len(prompts) == 1
        assert "mcp__agentchat__agentchat_decide" in prompts[0]
        assert " agentchat_decide" not in prompts[0]
        assert "Decide per card" in prompts[0], "the served question, verbatim apart from tool names"


# ──────────────────────── the worker ────────────────────────


def _wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class TestWorker:
    def test_wake_runs_one_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, _agent, _events = _invoker(monkeypatch, trays=[_tray()], answer=_answer())
        inv.start()
        try:
            inv.wake()
            assert _wait_for(lambda: inv.cycles == 1)
            assert len(client.tray_calls) == 1
        finally:
            inv.stop()

    def test_wake_during_a_cycle_earns_one_more_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(monkeypatch, trays=[_tray()] * 5, answer=_answer())
        in_cycle = threading.Event()
        release = threading.Event()
        real_cycle = inv.run_cycle

        def slow_cycle() -> Any:
            in_cycle.set()
            release.wait(1.0)
            return real_cycle()

        monkeypatch.setattr(inv, "run_cycle", slow_cycle)
        inv.start()
        try:
            inv.wake()
            assert in_cycle.wait(1.0)
            inv.wake()  # lands while the first cycle runs
            inv.wake()
            release.set()
            assert _wait_for(lambda: inv.cycles == 2)
            time.sleep(0.05)
            assert inv.cycles == 2, "two wakes during one cycle collapse into one follow-up"
        finally:
            inv.stop()

    def test_stop_is_idempotent_and_quick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        inv.start()
        started = time.monotonic()
        inv.stop()
        inv.stop()
        assert time.monotonic() - started < 2.5
        inv.wake()  # ignored after stop


def _server_like_inbox(client: _Client, unread: dict[str, str], answered: dict[str, str] | None = None) -> Any:
    """The tray as the server builds it: one card per conversation with a
    message newer than its last ``reply``/``dismiss`` anchor. ``unread`` maps
    conversation id → newest message id and is mutated by the test as peers
    write; a conversation the fake turn answered through its newest message
    (``answered``: conversation id → message id) shows ``you_owe_reply: false``."""

    def tray() -> InboxTray:
        read_through = {
            cid: anchor for cid, decision, anchor in client.recorded if decision in ("reply", "dismiss")
        }
        cards = []
        for cid, newest in unread.items():
            if read_through.get(cid) == newest:
                continue
            card = _card(cid)
            card["preview"]["message_id"] = newest
            if answered is not None and answered.get(cid) == newest:
                card["you_owe_reply"] = False
            cards.append(card)
        return _tray(*cards)

    return tray


class TestInFlightConversations:
    """``reply`` is recorded at the end of the turn, so the card is on the
    tray for the whole turn. Through the worker — the production path — a
    wake that lands mid-turn re-reads the tray only after the turn has
    ended and recorded: one turn, one record. A message newer than the
    anchor, arriving after the turn, earns a new turn."""

    def test_wake_during_a_turn_does_not_gate_the_conversation_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inv, client, agent, events = _invoker(monkeypatch, trays=[], answer=_answer(a="reply"))
        unread = {"a": "msg_a"}
        answered: dict[str, str] = {}
        client.live_inbox = _server_like_inbox(client, unread, answered)
        in_turn = threading.Event()
        release = threading.Event()

        def long_turn(prompt: str, conversation_history: list[Any]) -> dict[str, Any]:
            events.append("run")
            in_turn.set()
            assert release.wait(5.0)
            answered["a"] = unread["a"]  # the turn sent its answer
            return {"final_response": "sent"}

        agent.run_conversation.side_effect = long_turn
        # Deterministic: wakes are delivered to the worker the way the
        # coalescer delivers them (``_on_wake_fired``: mark a cycle due), and
        # every cycle end releases a semaphore the test acquires — nothing
        # here waits on a clock.
        cycles_done = threading.Semaphore(0)
        real_cycle = inv.run_cycle

        def signalled_cycle() -> Any:
            try:
                return real_cycle()
            finally:
                cycles_done.release()

        monkeypatch.setattr(inv, "run_cycle", signalled_cycle)
        inv.start()
        try:
            inv._on_wake_fired()  # wake 1
            assert in_turn.wait(10.0), "turn 1 is running"
            assert inv.in_flight() == {"a"}
            inv._on_wake_fired()  # wakes while the turn runs: the card is still on the tray
            inv._on_wake_fired()
            assert inv._cycle_due.is_set(), "exactly one follow-up cycle is due"
            release.set()
            assert cycles_done.acquire(timeout=10.0), "cycle 1 (the turn) ended"
            assert cycles_done.acquire(timeout=10.0), "cycle 2 (the follow-up) ended"
            assert inv.cycles == 2 and not inv._cycle_due.is_set()
            assert inv.in_flight() == set()
            assert agent.run_conversation.call_count == 1, "one turn for the conversation"
            assert client.recorded == [("a", "reply", "msg_a")], "one record"
            assert events.count("model") == 1, "gated once: the second cycle saw an empty tray"

            # A newer message after the turn: a new card, a new turn, a new record.
            unread["a"] = "msg_a2"
            inv._on_wake_fired()
            assert cycles_done.acquire(timeout=10.0), "cycle 3 (the new turn) ended"
            assert client.recorded == [("a", "reply", "msg_a"), ("a", "reply", "msg_a2")]
            assert agent.run_conversation.call_count == 2 and inv.cycles == 3
        finally:
            release.set()
            inv.stop()

    def test_concurrent_cycle_skips_the_in_flight_card_and_rereads_later(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Belt and braces for any caller that is not the worker: a cycle run
        while a turn is in flight drops that card before the gate, records
        nothing for it, and asks for one re-read once the turn has ended."""
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[], answer=_answer(a="reply", b="dismiss")
        )
        unread = {"a": "msg_a"}
        answered: dict[str, str] = {}
        client.live_inbox = _server_like_inbox(client, unread, answered)
        in_turn = threading.Event()
        release = threading.Event()

        def long_turn(prompt: str, conversation_history: list[Any]) -> dict[str, Any]:
            events.append("run")
            in_turn.set()
            assert release.wait(5.0)
            answered["a"] = unread["a"]  # the turn sent its answer
            return {"final_response": "sent"}

        agent.run_conversation.side_effect = long_turn
        rereads: list[int] = []
        monkeypatch.setattr(inv._coalescer, "wake", lambda: rereads.append(1))
        first = threading.Thread(target=inv.run_cycle)
        first.start()
        try:
            assert in_turn.wait(2.0)
            unread["b"] = "msg_b"  # a second peer writes while a's turn runs
            with caplog.at_level(logging.INFO, logger="agentchatme_hermes.agent_invoker"):
                result = inv.run_cycle()  # a concurrent caller, mid-turn
            assert result is not None and result.outcome == "decided"
            assert client.recorded == [("b", "dismiss", "msg_b")], "a is not gated again, b is"
            assert rereads == [1], "one re-read asked for, for when the turn ends"
            assert any("already have a turn in flight (a)" in r.getMessage() for r in caplog.records)
        finally:
            release.set()
            first.join(5.0)
        assert client.recorded == [("b", "dismiss", "msg_b"), ("a", "reply", "msg_a")]
        assert inv.in_flight() == set()
        assert agent.run_conversation.call_count == 1

    def test_in_flight_is_cleared_when_the_turn_crashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply"))
        agent.run_conversation.side_effect = RuntimeError("boom")
        inv.run_cycle()
        assert inv.in_flight() == set() and client.recorded == []

    def test_gate_less_turn_covers_every_card(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, agent, _events = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"), _card("b"))],
            config=SimpleNamespace(wake_debounce_s=0.0, reply_gate_enabled=False),
        )
        seen: list[set[str]] = []
        agent.run_conversation.side_effect = lambda prompt, conversation_history: seen.append(inv.in_flight()) or {"final_response": ""}
        inv.run_cycle()
        assert seen == [{"a", "b"}] and inv.in_flight() == set()


class TestProfileContext:
    """Under a multiplexed gateway Hermes keys the tool registry and the home
    by a context-local HERMES_HOME override. The worker thread enters its
    profile's override, so the hosted-tools probe reads that profile's
    overlay and the agent is built for that home."""

    @staticmethod
    def _fake_hermes(monkeypatch: pytest.MonkeyPatch, home_with_tools: str) -> Any:
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

    def test_worker_probes_and_turns_inside_its_profile(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home_b = tmp_path / "profiles" / "b"
        override = self._fake_hermes(monkeypatch, str(home_b))
        inv, client, agent, _events = _invoker(monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply"))
        # The fixture stubs the probe; put the real one back: it must read profile b's overlay.
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", _REAL_PROBE)
        inv._runtime.hermes_home = home_b  # the profile this invoker serves
        seen: list[str | None] = []
        agent.run_conversation.side_effect = lambda prompt, conversation_history: seen.append(override.get()) or {"final_response": "ok"}
        slept = _fake_stop_wait(monkeypatch, inv)
        inv.start()
        try:
            inv.wake()
            assert _wait_for(lambda: inv.turns == 1, timeout=5.0)
        finally:
            inv.stop()
        assert slept == [], "the probe saw profile b's tools at once — no hold"
        assert seen == [str(home_b)], "the turn ran inside profile b's context"
        assert client.recorded == [("a", "reply", "msg_a")]
        assert override.get() is None, "the test thread's context is untouched"

    def test_without_the_profile_context_the_overlay_is_invisible(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """What the fix is for: the same registry read from a bare thread sees nothing."""
        from agentchatme_hermes.profile_scope import profile_context

        home_b = tmp_path / "profiles" / "b"
        self._fake_hermes(monkeypatch, str(home_b))
        assert _REAL_PROBE() == []
        with profile_context(home_b):
            assert _REAL_PROBE() == ["mcp__agentchat__agentchat_inbox"]
        assert _REAL_PROBE() == []


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


class TestProfileTurnScope:
    """A served profile's cycle — the gate's model call and the turn — must
    resolve that profile's credentials, in the scope the gateway binds for
    that profile's own turns (``gateway/run.py`` ``_profile_runtime_scope``)."""

    @staticmethod
    def _homes(tmp_path: Path) -> tuple[Path, Path]:
        return tmp_path / "hermes", tmp_path / "hermes" / "profiles" / "up"

    @staticmethod
    def _secrets(launch: Path, up: Path) -> dict[str, dict[str, str]]:
        return {str(launch): {"LIVEFIRE_STUB_KEY": "key-launch"}, str(up): {"LIVEFIRE_STUB_KEY": "key-up"}}

    def _worker(self, monkeypatch: pytest.MonkeyPatch, fake: _FakeHermesScopes, home: Path, trays: list[Any]) -> tuple[AgentInvoker, _Client, Any, dict[str, list[Any]]]:
        inv, client, agent, _events = _invoker(monkeypatch, trays=trays, answer=_answer(a="reply"))
        inv._runtime.hermes_home = home
        seen: dict[str, list[Any]] = {"gate": [], "turn": [], "turn_home": [], "between_cycles": []}

        def resolve_model_and_runtime() -> tuple[str, dict[str, Any]]:
            # What Hermes's provider resolution does: read the key through get_secret.
            seen["gate"].append(fake.get_secret("LIVEFIRE_STUB_KEY"))
            return "m", {"provider": "p"}

        def run_conversation(prompt: str, conversation_history: list[Any]) -> dict[str, Any]:
            seen["turn"].append(fake.get_secret("LIVEFIRE_STUB_KEY"))
            seen["turn_home"].append(fake.override.get())
            return {"final_response": "ok"}

        real_guarded = inv._guarded_cycle

        def observe_then_cycle() -> None:
            # Runs on the worker thread at every wake, OUTSIDE any cycle's scope
            # (the cycle enters it inside): what is bound here is what an
            # earlier cycle left behind.
            seen["between_cycles"].append((fake.scope.get(), fake.override.get()))
            real_guarded()

        monkeypatch.setattr(inv, "_resolve_model_and_runtime", resolve_model_and_runtime)
        monkeypatch.setattr(inv, "_guarded_cycle", observe_then_cycle)
        agent.run_conversation.side_effect = run_conversation
        return inv, client, agent, seen

    def test_served_profile_turn_sees_its_own_secret_and_nothing_leaks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, self._secrets(launch, up))
        inv, client, _agent, seen = self._worker(monkeypatch, fake, up, [_tray(_card("a")), _tray()])
        inv.start()
        try:
            inv.wake()
            assert _wait_for(lambda: inv.turns == 1, timeout=5.0)
            inv.wake()  # a second cycle on the same worker thread
            assert _wait_for(lambda: len(seen["between_cycles"]) == 2 and inv.cycles == 2, timeout=5.0)
        finally:
            inv.stop()
        assert seen["gate"] == ["key-up"], "the gate's model call used profile up's own provider key"
        assert seen["turn"] == ["key-up"], "the turn used profile up's own provider key"
        assert seen["turn_home"] == [str(up)]
        assert fake.runtime_scope_calls == [str(up), str(up)], "Hermes's own scope, entered per cycle"
        assert fake.runtime_scope_exits == [None, None], "and exited after each successful cycle"
        # On the worker thread, between cycles: the profile's home override (the
        # worker's lifetime context) and NO secret scope left over from cycle 1.
        assert seen["between_cycles"] == [(None, str(up)), (None, str(up))]
        assert client.recorded == [("a", "reply", "msg_a")]

        inv2, _client2, _agent2, seen2 = self._worker(monkeypatch, fake, launch, [_tray(_card("a"))])
        inv2.start()
        try:
            inv2.wake()
            assert _wait_for(lambda: inv2.turns == 1, timeout=5.0)
        finally:
            inv2.stop()
        assert seen2["gate"] == ["key-launch"] and seen2["turn"] == ["key-launch"]

    def test_scope_is_exited_when_the_cycle_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, self._secrets(launch, up))
        inv, _client, _agent, _seen = self._worker(monkeypatch, fake, up, [_tray(_card("a"))])

        def exploding_cycle() -> Any:
            assert fake.scope.get() == {"LIVEFIRE_STUB_KEY": "key-up"}
            raise RuntimeError("boom inside the scope")

        monkeypatch.setattr(inv, "run_cycle", exploding_cycle)
        inv._guarded_cycle()
        assert fake.runtime_scope_exits == ["RuntimeError"], "exited, with the exception"
        assert fake.scope.get() is None and fake.override.get() is None
        assert inv.left_on_tray == 1

    def test_fallback_enters_exactly_the_gateway_pieces_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, self._secrets(launch, up), with_gateway_helper=False)
        inv, _client, _agent, seen = self._worker(monkeypatch, fake, up, [_tray(_card("a")), _tray()])
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.profile_scope"):
            inv._guarded_cycle()
            inv._guarded_cycle()
        assert seen["turn"] == ["key-up"] and seen["gate"] == ["key-up"]
        assert seen["turn_home"] == [str(up)]
        assert fake.hydrated == [str(up), str(up)], "external secret sources hydrated per cycle"
        assert fake.set_scope_calls == [({"LIVEFIRE_STUB_KEY": "key-up"}, str(up))] * 2, "owner scope, stamped"
        assert fake.terminal_scopes == [str(up), str(up)], "the profile's terminal policy installed"
        assert fake.runtime_scope_calls == []
        assert len([r for r in caplog.records if "not importable" in r.getMessage()]) == 1, "warned once"
        assert fake.scope.get() is None and fake.override.get() is None

    @pytest.mark.parametrize(
        "missing", ["hermes_cli.env_loader", "tui_gateway.launch_profile_policy", "tools.terminal_scope"]
    )
    def test_fallback_with_a_missing_piece_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, self._secrets(launch, up), with_gateway_helper=False, missing_pieces=(missing,))
        inv, client, agent, _seen = self._worker(monkeypatch, fake, up, [_tray(_card("a"))])
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            inv._guarded_cycle()
        assert client.tray_calls == [] and client.recorded == [] and fake.set_scope_calls == []
        agent.run_conversation.assert_not_called()
        assert inv.left_on_tray == 1
        assert any("cycle not run" in r.getMessage() and "not importable" in r.getMessage() for r in caplog.records)

    def test_helper_signature_drift_is_one_warning_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, self._secrets(launch, up), helper_signature_drift=True)
        inv, client, _agent, _seen = self._worker(monkeypatch, fake, up, [_tray(_card("a"))])
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            inv._guarded_cycle()
        assert client.tray_calls == []
        lines = [r for r in caplog.records if r.name == "agentchatme_hermes.agent_invoker"]
        assert [r.levelno for r in lines] == [logging.WARNING, logging.WARNING]  # cycle not run + re-wake
        assert "cycle not run" in lines[0].getMessage() and "TypeError" in lines[0].getMessage()
        assert all(r.exc_info is None for r in lines), "no traceback per wake"

    def test_scope_that_cannot_be_entered_runs_nothing_and_rewakes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, {str(up): {}}, helper_raises=OSError("secret source unreachable"))
        inv, client, agent, _seen = self._worker(monkeypatch, fake, up, [_tray(_card("a"))])
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            inv._guarded_cycle()
        assert client.tray_calls == [] and client.recorded == []
        agent.run_conversation.assert_not_called()
        assert inv.left_on_tray == 1
        assert any("cycle not run" in r.getMessage() and "secret source unreachable" in r.getMessage() for r in caplog.records)

    def test_single_profile_gateway_uses_the_standalone_launch_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        launch, _up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, {str(launch): {"LIVEFIRE_STUB_KEY": "key-launch"}}, multiplex=False)
        inv, client, _agent, seen = self._worker(monkeypatch, fake, launch, [_tray(_card("a"))])
        inv._guarded_cycle()
        assert fake.standalone_calls == 1 and fake.runtime_scope_calls == []
        assert seen["turn"] == ["process-env:LIVEFIRE_STUB_KEY"], "single profile: the process env, as before"
        assert client.recorded == [("a", "reply", "msg_a")]

    def test_single_profile_standalone_failure_still_runs_the_cycle_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        launch, _up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, {str(launch): {}}, multiplex=False,
                                 standalone_raises=RuntimeError("launch policy drifted"))
        inv, client, _agent, seen = self._worker(monkeypatch, fake, launch, [_tray(_card("a")), _tray(_card("b"))])
        monkeypatch.setattr(invoker_module, "build_call_model", lambda **_kw: (lambda prompt: _answer(a="reply", b="reply")))
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.profile_scope"):
            inv._guarded_cycle()
            inv._guarded_cycle()
        assert seen["turn"] == ["process-env:LIVEFIRE_STUB_KEY"] * 2, "both cycles ran, unscoped"
        assert [r[0] for r in client.recorded] == ["a", "b"]
        assert len([r for r in caplog.records if "standalone launch scope" in r.getMessage()]) == 1

    def test_invite_turn_runs_in_the_profile_scope(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        launch, up = self._homes(tmp_path)
        fake = _FakeHermesScopes(monkeypatch, self._secrets(launch, up))
        inv, _client, agent, _seen = self._worker(
            monkeypatch, fake, up, [_tray(invites=[_tray_invite()]), _tray()]
        )
        got: list[Any] = []
        agent.run_conversation.side_effect = lambda prompt, conversation_history: got.append(fake.get_secret("LIVEFIRE_STUB_KEY")) or {"final_response": ""}
        inv._guarded_cycle()
        assert inv.invite_turns == 1
        assert got == ["key-up"] and fake.runtime_scope_calls == [str(up)] and fake.runtime_scope_exits == [None]

    def test_outside_hermes_the_scope_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sys

        from agentchatme_hermes.profile_scope import profile_turn_scope

        for name in ("agent.secret_scope", "gateway.run", "gateway.run_turn"):
            monkeypatch.setitem(sys.modules, name, None)
        ran: list[int] = []
        with profile_turn_scope(tmp_path):
            ran.append(1)
        assert ran == [1]


class TestTurnNotStarted:
    def test_agent_constructor_raising_is_a_turn_not_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``AIAgent(...)`` raising (anything but ImportError): nothing
        recorded, the invite stays on the tray, the server re-wakes."""
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(invites=[_tray_invite()])], answer=_answer()
        )

        import sys
        import types

        class ExplodingAgent:
            def __init__(self, **_kwargs: Any) -> None:
                raise RuntimeError("provider config exploded")

        module = types.ModuleType("run_agent")
        module.AIAgent = ExplodingAgent  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "run_agent", module)
        monkeypatch.setattr(inv, "_build_agent", AgentInvoker._build_agent.__get__(inv))
        inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert inv.left_on_tray == 1 and inv.invite_turns == 0
        assert client.dismissed_invites == []

    def test_build_agent_swallows_constructor_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        import types

        class ExplodingAgent:
            def __init__(self, **_kwargs: Any) -> None:
                raise RuntimeError("no session db")

        module = types.ModuleType("run_agent")
        module.AIAgent = ExplodingAgent  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "run_agent", module)
        inv = AgentInvoker(
            config=SimpleNamespace(wake_debounce_s=0.0),
            identity=AgentIdentity(handle="me"),
            runtime=SimpleNamespace(client=None, texts=TEXTS),
        )
        monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {}))
        assert inv._build_agent("agentchat:wake:test") is None
        assert inv._start_turn(prompt="p", session_id="s") is None

    def test_invite_without_texts_stays_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(invites=[_tray_invite()])], answer=_answer(), texts=None
        )
        inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert inv.left_on_tray >= 1 and client.dismissed_invites == []


class TestSessionPerTurn:
    def test_wake_turns_get_fresh_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))] * 2, answer=_answer(a="reply")
        )
        sessions: list[str] = []
        monkeypatch.setattr(inv, "_build_agent", lambda session_id: sessions.append(session_id) or MagicMock())
        inv.run_cycle()
        inv.run_cycle()
        assert len(sessions) == 2 and sessions[0] != sessions[1]
        assert all(s.startswith("agentchat:wake:") for s in sessions)


class _BlockingAgent:
    """An ``AIAgent`` whose turn blocks until released; ``obey`` releases it
    on ``hard_interrupt`` (Hermes' own stop), otherwise it ignores the stop."""

    def __init__(self, *, obey: bool) -> None:
        self.obey = obey
        self.release = threading.Event()
        self.started = threading.Event()
        self.interrupts: list[str | None] = []
        self.runs = 0

    def run_conversation(self, prompt: str, conversation_history: list[Any]) -> dict[str, Any]:
        self.runs += 1
        self.started.set()
        self.release.wait(10.0)
        return {"completed": False, "interrupted": True, "messages": []}

    def get_activity_summary(self) -> dict[str, Any]:
        return {"last_activity_at": 1.0, "api_call_count": 1, "current_tool": None}

    def hard_interrupt(self, message: str | None = None, *, tool_reason: str | None = None) -> None:
        self.interrupts.append(tool_reason)
        if self.obey:
            self.release.set()


def _fast_limits(inv: AgentInvoker, *, grace_s: float = 0.05) -> None:
    inv._limits = turn_runner_module.TurnLimits(idle_s=0.05, max_s=5.0, grace_s=grace_s, poll_s=0.01)


class TestTurnTimeLimit:
    """A turn that hangs must not stop receiving: it is interrupted with
    Hermes' own stop, counts as failed (nothing recorded), and its
    conversations stay held until its thread has really ended."""

    def test_overrun_records_nothing_and_holds_the_card_until_the_turn_ends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inv, client, _agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a")), _tray(_card("a"))], answer=_answer(a="reply")
        )
        stuck = _BlockingAgent(obey=False)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: stuck)
        _fast_limits(inv)
        wakes: list[str] = []
        monkeypatch.setattr(inv._coalescer, "wake", lambda: wakes.append("wake"))

        inv.run_cycle()
        assert client.recorded == [] and "record:a:reply" not in events
        assert inv.left_on_tray == 1
        assert stuck.interrupts == [turn_runner_module.INTERRUPT_TOOL_REASON]
        assert inv.in_flight() == {"a"}, "held: the stuck turn may still send"
        status = inv.status()
        assert status["hung_turns"] == 1
        assert status["last_turn"]["status"] == "timeout" and status["last_turn"]["ended"] is False

        # The next wake does not gate or start a second turn for "a".
        inv.run_cycle()
        assert events.count("model") == 1 and stuck.runs == 1

        stuck.release.set()  # the stuck call finally returns
        assert _wait_for(lambda: inv.in_flight() == set())
        assert inv.status()["hung_turns"] == 0
        assert "wake" in wakes, "the tray is read again once the overrun turn ended"

    def test_interrupted_turn_that_ends_is_a_timeout_not_a_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, _agent, _events = _invoker(monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply"))
        obeying = _BlockingAgent(obey=True)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: obeying)
        _fast_limits(inv, grace_s=2.0)
        inv.run_cycle()
        assert client.recorded == [] and inv.turns == 0
        assert inv.in_flight() == set() and inv.status()["hung_turns"] == 0
        assert inv.status()["last_turn"]["ended"] is True
        assert inv.left_on_tray == 1

    def test_no_new_turn_while_too_many_turns_are_hung(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        inv, client, agent, _events = _invoker(monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply"))
        for cid in ("x", "y")[: invoker_module.MAX_HUNG_TURNS]:
            inv._running.abandon(inv._running.hold(frozenset({cid})))
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes.agent_invoker"):
            inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert client.recorded == [] and inv.left_on_tray == 1
        assert any("have not ended" in r.getMessage() for r in caplog.records)

    def test_stop_interrupts_the_running_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, _agent, _events = _invoker(monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply"))
        turn = _BlockingAgent(obey=True)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: turn)
        inv._limits = turn_runner_module.TurnLimits(idle_s=None, max_s=None, grace_s=2.0, poll_s=0.01)
        inv.start()
        inv._on_wake_fired()
        assert turn.started.wait(5.0)
        inv.stop()
        assert _wait_for(lambda: turn.interrupts == [turn_runner_module.INTERRUPT_STOP_REASON])
        assert _wait_for(lambda: inv.status()["state"] == "stopped")
        assert client.recorded == [], "a turn stopped mid-way records nothing"



class TestInvitesOnTheTray:
    """Contract v1 (D4): pending invites are on the tray. Each gets a turn of
    its own after the cards; what is recorded after it is the kit's
    ``invite_outcome``. Nothing is remembered on disk."""

    def _pending(self, client: _Client, pending: list[str]) -> None:
        """The server's view: the invites still pending (mutated by a turn)."""
        client.live_inbox = lambda: _tray(invites=[_tray_invite(i) for i in pending])

    def test_an_invite_gets_its_own_turn_naming_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        pending = ["ginv_1"]
        self._pending(client, pending)
        agent.run_conversation.side_effect = lambda prompt, conversation_history: pending.clear() or {"final_response": "accepted"}
        inv.run_cycle()
        agent.run_conversation.assert_called_once_with(invite_turn_prompt(TEXTS, "ginv_1"), conversation_history=[])
        assert "This turn is for the invite ginv_1." in invite_turn_prompt(TEXTS, "ginv_1")
        assert inv.invite_turns == 1 and inv.outcomes["invite_invite_answered"] == 1
        assert client.dismissed_invites == [] and inv.left_on_tray == 0

    def test_left_pending_twice_is_dismissed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        self._pending(client, ["ginv_1"])
        agent.run_conversation.side_effect = lambda prompt, conversation_history: {"final_response": ""}
        inv.run_cycle()
        assert client.dismissed_invites == [] and inv.outcomes["invite_silent_first"] == 1
        inv.run_cycle()
        assert client.dismissed_invites == ["ginv_1"] and inv.outcomes["invite_silent_again"] == 1

    def test_a_failed_invite_turn_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        self._pending(client, ["ginv_1"])
        agent.run_conversation.side_effect = RuntimeError("provider exploded")
        inv.run_cycle()
        inv.run_cycle()
        assert client.dismissed_invites == [], "a failed turn never counts as a look"
        assert inv.outcomes["invite_turn_failed"] == 2 and inv.left_on_tray == 2

    def test_invites_run_after_the_cards_even_on_an_empty_card_tray(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, _agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), invites=[_tray_invite()])], answer=_answer(a="dismiss")
        )
        inv.run_cycle()
        assert client.recorded == [("a", "dismiss", "msg_a")] and inv.invite_turns == 1
        assert events.index("model") < events.index("run"), "cards are gated first"

    def test_a_few_invite_turns_per_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        many = [f"ginv_{i}" for i in range(invoker_module.MAX_INVITE_TURNS_PER_CYCLE + 2)]
        inv, client, agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        self._pending(client, many)
        wakes: list[str] = []
        monkeypatch.setattr(inv._coalescer, "wake", lambda: wakes.append("wake"))
        inv.run_cycle()
        assert agent.run_conversation.call_count == invoker_module.MAX_INVITE_TURNS_PER_CYCLE
        assert wakes == ["wake"], "the rest on the next cycle"

    def test_no_second_turn_while_an_invite_turn_is_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        self._pending(client, ["ginv_1"])
        inv._running.abandon(inv._running.hold(frozenset({"invite:ginv_1"})))
        inv.run_cycle()
        agent.run_conversation.assert_not_called()

    def test_a_server_without_invites_on_the_tray(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        client.live_inbox = lambda: InboxTray.model_validate(
            {"you": "@me", "question": "q", "categories": [], "conversations": [], "deferred": 0, "truncated": False}
        )
        inv.run_cycle()
        agent.run_conversation.assert_not_called()


class TestWorkerSupervision:
    """A worker loop that dies is restarted with backoff and reads the tray
    again; its state reaches the health record."""

    def test_crashed_worker_restarts_and_reads_the_tray_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(monkeypatch, trays=[_tray(), _tray()], answer=_answer())
        monkeypatch.setattr(invoker_module, "WORKER_BACKOFF_BASE_S", 0.01)
        calls = {"n": 0}
        real = inv._guarded_cycle

        def flaky() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("cycle exploded")
            real()

        monkeypatch.setattr(inv, "_guarded_cycle", flaky)
        inv.start()
        try:
            inv._on_wake_fired()
            assert _wait_for(lambda: inv.worker_restarts == 1 and inv.cycles >= 1, timeout=5.0)
            status = inv.status()
            assert status["alive"] is True and status["restarts"] == 1
            assert "cycle exploded" in (status["last_error"] or "")
            assert _wait_for(lambda: inv.status()["state"] == "idle")
        finally:
            inv.stop()
        assert _wait_for(lambda: inv.status()["state"] == "stopped" and not inv.status()["alive"])

    def test_backoff_grows_and_stop_ends_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        monkeypatch.setattr(invoker_module, "WORKER_BACKOFF_BASE_S", 0.01)
        delays: list[float] = []
        real_wait = inv._stopped.wait

        def wait(timeout: float | None = None) -> bool:
            if timeout is not None and timeout < 1.0:
                delays.append(timeout)
            return real_wait(timeout)

        monkeypatch.setattr(inv._stopped, "wait", wait)
        monkeypatch.setattr(inv, "_worker_body", lambda: (_ for _ in ()).throw(RuntimeError("always")))
        inv.start()
        assert _wait_for(lambda: len(delays) >= 3, timeout=5.0)
        inv.stop()
        assert delays[1] > delays[0] * 1.2 and delays[2] > delays[1] * 1.2, "doubling backoff"
        assert _wait_for(lambda: inv.status()["state"] == "stopped")

    def test_worker_state_reaches_the_health_record(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from agentchatme_hermes.ws_health import HealthBoard, read_ws_health

        inv, _client, _agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        board = HealthBoard(handle="me", path=tmp_path / "ws.health.json", worker_status=inv.status)
        inv._health = board
        board.socket("connected", last_connected_at=1.0)
        inv._set_state("cycle")
        health = read_ws_health(tmp_path / "ws.health.json")
        assert health is not None and health["state"] == "connected"
        assert health["worker"]["state"] == "cycle" and health["worker"]["hung_turns"] == 0
        inv._set_state("restart_wait", error="RuntimeError: boom")
        health = read_ws_health(tmp_path / "ws.health.json")
        assert health is not None and health["worker"]["state"] == "restart_wait"
        assert health["worker"]["last_error"] == "RuntimeError: boom"
        assert health["state"] == "connected", "the socket's state is kept across worker writes"


class TestRunBudget:
    """The ceiling also reaches Hermes' own cooperative budget
    (``AIAgent(run_budget_seconds=...)``) when the constructor takes it."""

    def _build(self, monkeypatch: pytest.MonkeyPatch, agent_cls: Any, config: Any) -> Any:
        import sys
        import types

        module = types.ModuleType("run_agent")
        module.AIAgent = agent_cls  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "run_agent", module)
        inv = AgentInvoker(config=config, identity=AgentIdentity(handle="me"), runtime=SimpleNamespace(client=None, texts=TEXTS))
        monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {}))
        return inv._build_agent("agentchat:wake:test")

    def test_budget_passed_when_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class NewAgent:
            def __init__(self, *, run_budget_seconds: float | None = None, **kwargs: Any) -> None:
                self.budget = run_budget_seconds
                self.kwargs = kwargs

        built = self._build(monkeypatch, NewAgent, SimpleNamespace(wake_debounce_s=0.0, turn_max_s=1200.0))
        assert built.budget == 1200.0 and built.kwargs["platform"] == "agentchat"
        off = self._build(monkeypatch, NewAgent, SimpleNamespace(wake_debounce_s=0.0, turn_max_s=0.0))
        assert off.budget is None, "no ceiling, no budget"

    def test_budget_omitted_for_an_older_hermes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class OldAgent:
            def __init__(self, *, model: str, session_id: str, session_db: Any, platform: str, quiet_mode: bool, load_soul_identity: bool, skip_context_files: bool) -> None:
                self.platform = platform

        built = self._build(monkeypatch, OldAgent, SimpleNamespace(wake_debounce_s=0.0))
        assert built is not None and built.platform == "agentchat"


class TestGateOnHostLlm:
    def test_gate_uses_ctx_llm_and_resolves_no_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _Client([_tray(_card("a"))])
        calls: list[dict[str, Any]] = []

        class _PluginLlm:
            def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                calls.append(kwargs)
                return SimpleNamespace(text=_answer(a="dismiss"))

        inv = AgentInvoker(
            config=SimpleNamespace(wake_debounce_s=0.0),
            identity=AgentIdentity(handle="me"),
            runtime=SimpleNamespace(client=client, texts=TEXTS),
            host_llm=_PluginLlm,
        )

        def no_resolve() -> Any:
            raise AssertionError("no config or credential read when ctx.llm is there")

        monkeypatch.setattr(inv, "_resolve_model_and_runtime", no_resolve)
        inv.run_cycle()
        assert len(calls) == 1 and client.recorded == [("a", "dismiss", "msg_a")]

    def test_a_failing_accessor_falls_back_once_with_a_warning(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        inv, _client, _agent, events = _invoker(monkeypatch, trays=[_tray(_card("a")), _tray(_card("b"))], answer=_answer(a="dismiss", b="dismiss"))

        def broken() -> Any:
            raise ImportError("agent.plugin_llm")

        inv._host_llm = broken
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            inv.run_cycle()
            inv.run_cycle()
        assert events.count("model") == 2, "the fallback path made the gate call"
        assert sum("ctx.llm" in r.getMessage() for r in caplog.records) == 1
