"""Tests for ``agentchatme_hermes.agent_invoker`` — wake → gate → turn, on the SDK gate.

The Hermes side (AIAgent, SessionDB, provider resolution) is stubbed; the
SDK client is a recorder; the model answer is injected through
``build_call_model`` so the real ``agentchatme.gate.run`` does the parsing
and the recording. What must hold:

* a wake becomes exactly one cycle: one tray read, one gate call;
* ``dismiss`` / ``defer`` are recorded at once, with the card's anchor;
* ``reply`` starts ONE turn with the served wake instruction, and each
  ``reply`` is recorded right after the turn has started — after the agent
  is built, before ``run_conversation`` is entered;
* a turn that fails to start records nothing and re-wakes on the ladder;
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
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from agentchatme.types import InboxTray, Texts

from agentchatme_hermes import agent_invoker as invoker_module
from agentchatme_hermes.agent_invoker import _FALLBACK_MODEL, AgentInvoker, _coerce_model_string
from agentchatme_hermes.invites import InviteLedger
from agentchatme_hermes.turn import invite_turn_prompt, wake_turn_prompt
from agentchatme_hermes.types import AgentIdentity, GroupInviteEvent

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


def _tray(*cards: dict[str, Any], truncated: bool = False) -> InboxTray:
    return InboxTray.model_validate(
        {
            "you": "@me",
            "question": "Decide per card with agentchat_decide.",
            "categories": ["open_request", "no_action_needed"],
            "conversations": list(cards),
            "deferred": 0,
            "truncated": truncated,
        }
    )


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
        self.inbox = SimpleNamespace(tray=self._tray)
        self.decisions = SimpleNamespace(record=self._record)

    def _tray(self, *, wait_ms: int = 0, limit: int | None = None) -> InboxTray:
        self.tray_calls.append({"wait_ms": wait_ms, "limit": limit})
        nxt = self.trays.pop(0) if self.trays else _tray()
        if isinstance(nxt, Exception):
            raise nxt
        assert isinstance(nxt, InboxTray)
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


def _invoker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trays: list[Any],
    answer: str | Exception | None = None,
    config: Any = None,
    texts: Texts | None = TEXTS,
    ledger: InviteLedger | None = None,
) -> tuple[AgentInvoker, _Client, MagicMock, list[str]]:
    """An invoker with a stubbed runtime, agent and model answer.

    ``events`` records the order of the load-bearing steps: ``build``
    (agent constructed), ``record:<id>:<decision>``, ``run`` (turn entered).
    """
    client = _Client(trays)
    runtime = SimpleNamespace(client=client, texts=texts)
    cfg = config or SimpleNamespace(wake_debounce_s=0.0)
    inv = AgentInvoker(config=cfg, identity=AgentIdentity(handle="me"), runtime=runtime, ledger=ledger)

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

    monkeypatch.setattr(inv, "_ensure_hermes_resolved", lambda: None)
    monkeypatch.setattr(inv, "_build_agent", build_agent)
    monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {"provider": "p"}))

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
    def test_reply_starts_one_turn_and_records_after_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, events = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"), _card("b"))],
            answer=_answer(a="reply", b="dismiss"),
        )
        result = inv.run_cycle()
        assert result is not None and result.outcome == "decided"
        assert client.tray_calls == [{"wait_ms": 0, "limit": 50}]
        # dismiss recorded by the SDK gate at once, with the card's anchor;
        # reply recorded by the host after the agent is built, before the turn runs.
        assert events == ["model", "record:b:dismiss", "build", "record:a:reply", "run"]
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
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.agent_invoker"):
            result = inv.run_cycle()
        assert result is not None and result.outcome == "fail_closed"
        agent.run_conversation.assert_not_called()
        assert client.recorded == []
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "logged once"
        assert "provider down" in warnings[0].getMessage()

    def test_unusable_answer_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer="I would rather not say."
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        result = inv.run_cycle()
        assert result is not None and result.outcome == "fail_closed"
        agent.run_conversation.assert_not_called()
        assert client.recorded == [] and scheduled == [invoker_module.RETRY_LADDER_S[0]]

    def test_retry_ladder_climbs_then_stops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))] * 10, answer=RuntimeError("down")
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        for _ in range(5):
            inv.run_cycle()
        assert scheduled == list(invoker_module.RETRY_LADDER_S)

    def test_failure_while_a_retry_is_pending_does_not_climb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))] * 3, answer=RuntimeError("down")
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        inv.run_cycle()
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]
        # A wake lands mid-outage and its cycle fails too, with the 60 s retry still pending.
        monkeypatch.setattr(type(inv._coalescer), "delayed_pending", property(lambda self: True))
        inv.run_cycle()
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]], "one outage, one rung"
        assert inv._retry_rung == 1

    def test_success_resets_the_ladder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, _agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))] * 3, answer=RuntimeError("down")
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        inv.run_cycle()
        assert inv._retry_rung == 1
        monkeypatch.setattr(
            invoker_module, "build_call_model", lambda **_kw: (lambda prompt: _answer(a="dismiss"))
        )
        inv.run_cycle()
        assert inv._retry_rung == 0

    def test_turn_that_fails_to_start_records_nothing_and_rewakes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inv, client, agent, events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), _card("b"))], answer=_answer(a="reply", b="dismiss")
        )
        monkeypatch.setattr(inv, "_build_agent", lambda _session_id: None)
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert client.recorded == [("b", "dismiss", "msg_b")], "reply NOT recorded: it stays unread"
        assert "record:a:reply" not in events
        assert inv.turns == 0 and inv.recorded_replies == 0
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]

    def test_crash_after_start_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``run_conversation`` raising is a crash after the turn started: the
        accepted trade-off — the verdict is recorded, the worker survives."""
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], answer=_answer(a="reply")
        )
        agent.run_conversation.side_effect = RuntimeError("provider exploded")
        inv.run_cycle()  # must not raise
        assert client.recorded == [("a", "reply", "msg_a")]
        assert inv.turns == 0

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
        assert inv.turns == 1 and inv.recorded_replies == 0

    def test_without_texts_no_turn_and_reply_stays_unrecorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inv, client, agent, _events = _invoker(
            monkeypatch, trays=[_tray(_card("a"), _card("b"))], answer=_answer(a="reply", b="defer"), texts=None
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert client.recorded == [("b", "defer", "msg_b")]
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]

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
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        assert inv.run_cycle() is None
        agent.run_conversation.assert_not_called()
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]

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
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        result = inv.run_cycle()
        assert result is not None and result.undecided == ["b"]
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]

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


def _invite(invite_id: str = "ginv_1") -> GroupInviteEvent:
    return GroupInviteEvent(
        invite_id=invite_id,
        group_id="grp_1",
        group_name="Council",
        inviter_handle="carol",
        received_at=datetime.now(timezone.utc),
    )


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

    def test_invite_runs_its_own_turn_with_the_served_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _client, agent, _events = _invoker(monkeypatch, trays=[], answer=_answer())
        inv.start()
        try:
            inv.handle_group_invite(_invite())
            assert _wait_for(lambda: inv.invite_turns == 1)
            agent.run_conversation.assert_called_once_with(invite_turn_prompt(TEXTS), conversation_history=[])
            assert inv.cycles == 0, "an invite is not a tray wake"
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


class TestInviteRetry:
    def test_invite_whose_turn_fails_to_start_is_retried_on_the_next_wake(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ledger = InviteLedger(tmp_path / "invites.json")
        inv, _client, agent, _events = _invoker(
            monkeypatch, trays=[_tray()] * 3, answer=_answer(), ledger=ledger
        )
        build_ok = {"value": False}
        real_build = inv._build_agent
        monkeypatch.setattr(
            inv, "_build_agent", lambda sid: real_build(sid) if build_ok["value"] else None
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)

        inv.start()
        try:
            inv.handle_group_invite(_invite())
            assert _wait_for(lambda: scheduled == [invoker_module.RETRY_LADDER_S[0]])
            assert inv.invite_turns == 0
            assert [i.invite_id for i in ledger.pending()] == ["ginv_1"], "offered, not started"

            build_ok["value"] = True
            inv.wake()  # the next wake re-queues the pending invite
            assert _wait_for(lambda: inv.invite_turns == 1)
            agent.run_conversation.assert_called_once_with(invite_turn_prompt(TEXTS), conversation_history=[])
            assert ledger.is_started("ginv_1") and ledger.pending() == []
        finally:
            inv.stop()

    def test_invite_that_ran_is_not_run_again_after_a_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = tmp_path / "invites.json"
        first = InviteLedger(path)
        first.offer(_invite())
        first.mark_started("ginv_1")

        # A new process: the connect-time catch-up offers the still-pending invite again.
        inv, _client, agent, _events = _invoker(
            monkeypatch, trays=[], answer=_answer(), ledger=InviteLedger(path)
        )
        inv.start()
        try:
            inv.handle_group_invite(_invite())
            time.sleep(0.1)
            agent.run_conversation.assert_not_called()
            assert inv.invite_turns == 0
        finally:
            inv.stop()

    def test_invite_without_texts_stays_pending(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        ledger = InviteLedger(tmp_path / "invites.json")
        inv, _client, agent, _events = _invoker(
            monkeypatch, trays=[], answer=_answer(), texts=None, ledger=ledger
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "wake_after", scheduled.append)
        inv.start()
        try:
            inv.handle_group_invite(_invite())
            assert _wait_for(lambda: scheduled == [invoker_module.RETRY_LADDER_S[0]])
            agent.run_conversation.assert_not_called()
            assert [i.invite_id for i in ledger.pending()] == ["ginv_1"]
        finally:
            inv.stop()


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
