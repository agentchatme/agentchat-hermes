"""Tests for ``agentchatme_hermes.agent_invoker`` — knock → gate → turn.

The Hermes side (AIAgent, SessionDB, provider resolution) is stubbed; the
main line is a recorder. What must hold:

* a knock becomes exactly one cycle: one tray read, one gate call;
* ``reply`` cards start ONE turn with the fixed prompt; none → no turn;
* fail-closed posts nothing, starts nothing, and re-knocks on the ladder;
* gate disabled → the turn starts straight from the tray;
* a knock during a cycle earns one more cycle afterwards;
* group invites run their own fixed-prompt turn.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentchatme_hermes import agent_invoker as invoker_module
from agentchatme_hermes import gate
from agentchatme_hermes.agent_invoker import _FALLBACK_MODEL, AgentInvoker, _coerce_model_string
from agentchatme_hermes.main_line import MainLineError
from agentchatme_hermes.turn import invite_turn_prompt, knock_turn_prompt
from agentchatme_hermes.types import AgentIdentity, GroupInviteEvent


def _card(cid: str) -> dict[str, Any]:
    return {"conversation_id": cid, "preview": {"message_id": f"msg_{cid}"}}


def _tray(*cards: dict[str, Any], truncated: bool = False) -> dict[str, Any]:
    return {
        "you": "@me",
        "question": "Decide.",
        "conversations": list(cards),
        "deferred": 0,
        "truncated": truncated,
    }


class _MainLine:
    def __init__(self, trays: list[Any]) -> None:
        self.trays = list(trays)
        self.inbox_calls = 0
        self.decisions: list[tuple[str, dict[str, Any]]] = []

    def inbox(self, *, wait_ms: int = 0, limit: int = 50) -> dict[str, Any]:
        self.inbox_calls += 1
        nxt = self.trays.pop(0) if self.trays else _tray()
        if isinstance(nxt, Exception):
            raise nxt
        return nxt  # type: ignore[no-any-return]

    def record_decision(self, conversation_id: str, **body: Any) -> dict[str, Any]:
        self.decisions.append((conversation_id, body))
        return {}


def _invoker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trays: list[Any],
    gate_result: gate.GateResult | Exception | None = None,
    config: Any = None,
) -> tuple[AgentInvoker, _MainLine, MagicMock, list[Any]]:
    """An invoker with a stubbed runtime, agent and (optionally) gate."""
    main_line = _MainLine(trays)
    runtime = SimpleNamespace(main_line=main_line)
    cfg = config or SimpleNamespace(knock_debounce_s=0.0)
    inv = AgentInvoker(config=cfg, identity=AgentIdentity(handle="me"), runtime=runtime)

    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "ok"}
    monkeypatch.setattr(inv, "_ensure_hermes_resolved", lambda: None)
    monkeypatch.setattr(inv, "_build_agent", lambda _session_id: agent)
    monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {"provider": "p"}))

    gate_calls: list[Any] = []
    if gate_result is not None:

        def fake_run_gate(tray: Any, **kwargs: Any) -> gate.GateResult:
            gate_calls.append((tray, kwargs))
            if isinstance(gate_result, Exception):
                raise gate_result
            return gate_result

        monkeypatch.setattr(invoker_module.gate, "run_gate", fake_run_gate)
    return inv, main_line, agent, gate_calls


def _llm_result(reply: tuple[str, ...] = (), *, cards: int = 1, **kw: Any) -> gate.GateResult:
    return gate.GateResult(source="llm", cards=cards, reply_conversations=reply, **kw)


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
    def test_reply_starts_one_turn_with_the_fixed_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, main_line, agent, gate_calls = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"), _card("b"))],
            gate_result=_llm_result(("a",), cards=2, posted=1),
        )
        result = inv.run_cycle()
        assert result is not None and result.source == "llm"
        assert main_line.inbox_calls == 1
        assert len(gate_calls) == 1
        tray, kwargs = gate_calls[0]
        assert tray["conversations"] == [_card("a"), _card("b")]
        assert kwargs["main_runtime"] == {"model": "m", "provider": "p"}
        assert kwargs["fail_open"] is False
        agent.run_conversation.assert_called_once_with(knock_turn_prompt(("a",)), conversation_history=[])
        assert inv.turns == 1

    def test_no_reply_cards_means_no_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, agent, _calls = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], gate_result=_llm_result((), posted=1)
        )
        inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert inv.turns == 0

    def test_empty_tray_asks_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, agent, gate_calls = _invoker(
            monkeypatch, trays=[_tray()], gate_result=_llm_result(("a",))
        )
        result = inv.run_cycle()
        assert result is not None and result.source == "empty"
        assert gate_calls == []
        agent.run_conversation.assert_not_called()

    def test_fail_closed_posts_nothing_starts_nothing_and_reknocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inv, _ml, agent, _calls = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"))],
            gate_result=gate.GateResult(source="fail_closed", cards=1, undecided=("a",), failure="down"),
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "knock_after", scheduled.append)
        inv.run_cycle()
        agent.run_conversation.assert_not_called()
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]

    def test_retry_ladder_climbs_then_stops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failing = gate.GateResult(source="fail_closed", cards=1, undecided=("a",))
        inv, _ml, _agent, _calls = _invoker(
            monkeypatch, trays=[_tray(_card("a"))] * 10, gate_result=failing
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "knock_after", scheduled.append)
        for _ in range(5):
            inv.run_cycle()
        assert scheduled == list(invoker_module.RETRY_LADDER_S)

    def test_success_resets_the_ladder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, _agent, _calls = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"))] * 3,
            gate_result=gate.GateResult(source="fail_closed", cards=1, undecided=("a",)),
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "knock_after", scheduled.append)
        inv.run_cycle()
        monkeypatch.setattr(invoker_module.gate, "run_gate", lambda tray, **kw: _llm_result((), posted=1))
        inv.run_cycle()
        assert inv._retry_rung == 0

    def test_fail_open_starts_the_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, agent, _calls = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"))],
            gate_result=gate.GateResult(source="fail_open", cards=1, reply_conversations=("a",)),
            config=SimpleNamespace(knock_debounce_s=0.0, reply_gate_fail_open=True),
        )
        inv.run_cycle()
        agent.run_conversation.assert_called_once()

    def test_gate_disabled_starts_the_turn_from_the_tray(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, agent, gate_calls = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"))],
            gate_result=_llm_result(("a",)),
            config=SimpleNamespace(knock_debounce_s=0.0, reply_gate_enabled=False),
        )
        result = inv.run_cycle()
        assert result is not None and result.source == "disabled"
        assert gate_calls == [], "gate off → the model is not asked by the doorbell"
        agent.run_conversation.assert_called_once_with(knock_turn_prompt(("a",)), conversation_history=[])

    def test_tray_read_failure_reknocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, agent, _calls = _invoker(
            monkeypatch, trays=[MainLineError("503", status=503)], gate_result=_llm_result()
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "knock_after", scheduled.append)
        assert inv.run_cycle() is None
        agent.run_conversation.assert_not_called()
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]

    def test_truncated_tray_knocks_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, _agent, _calls = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"), truncated=True)],
            gate_result=_llm_result((), cards=1, posted=1, truncated=True),
        )
        knocks: list[int] = []
        monkeypatch.setattr(inv._coalescer, "knock", lambda: knocks.append(1))
        inv.run_cycle()
        assert knocks == [1]

    def test_undecided_or_failed_posts_reknock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, _agent, _calls = _invoker(
            monkeypatch,
            trays=[_tray(_card("a"), _card("b"))],
            gate_result=_llm_result(("a",), cards=2, undecided=("b",)),
        )
        scheduled: list[float] = []
        monkeypatch.setattr(inv._coalescer, "knock_after", scheduled.append)
        inv.run_cycle()
        assert scheduled == [invoker_module.RETRY_LADDER_S[0]]

    def test_turn_failure_is_contained(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, agent, _calls = _invoker(
            monkeypatch, trays=[_tray(_card("a"))], gate_result=_llm_result(("a",))
        )
        agent.run_conversation.side_effect = RuntimeError("provider exploded")
        inv.run_cycle()  # must not raise
        assert inv.turns == 0


# ──────────────────────── the worker ────────────────────────


def _wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class TestWorker:
    def test_knock_runs_one_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, main_line, _agent, _calls = _invoker(
            monkeypatch, trays=[_tray()], gate_result=_llm_result()
        )
        inv.start()
        try:
            inv.knock()
            assert _wait_for(lambda: inv.cycles == 1)
            assert main_line.inbox_calls == 1
        finally:
            inv.stop()

    def test_knock_during_a_cycle_earns_one_more_cycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, _agent, _calls = _invoker(monkeypatch, trays=[_tray()] * 5, gate_result=_llm_result())
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
            inv.knock()
            assert in_cycle.wait(1.0)
            inv.knock()  # lands while the first cycle runs
            inv.knock()
            release.set()
            assert _wait_for(lambda: inv.cycles == 2)
            time.sleep(0.05)
            assert inv.cycles == 2, "two knocks during one cycle collapse into one follow-up"
        finally:
            inv.stop()

    def test_invite_runs_its_own_fixed_prompt_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, agent, _calls = _invoker(monkeypatch, trays=[], gate_result=_llm_result())
        invite = GroupInviteEvent(
            invite_id="ginv_1",
            group_id="grp_1",
            group_name="Council",
            inviter_handle="carol",
            received_at=datetime.now(timezone.utc),
        )
        inv.start()
        try:
            inv.handle_group_invite(invite)
            assert _wait_for(lambda: inv.invite_turns == 1)
            agent.run_conversation.assert_called_once_with(invite_turn_prompt(), conversation_history=[])
            assert inv.cycles == 0, "an invite is not a tray knock"
        finally:
            inv.stop()

    def test_stop_is_idempotent_and_quick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, _agent, _calls = _invoker(monkeypatch, trays=[], gate_result=_llm_result())
        inv.start()
        started = time.monotonic()
        inv.stop()
        inv.stop()
        assert time.monotonic() - started < 2.5
        inv.knock()  # ignored after stop


class TestSessionPerTurn:
    def test_knock_turns_get_fresh_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inv, _ml, _agent, _calls = _invoker(
            monkeypatch, trays=[_tray(_card("a"))] * 2, gate_result=_llm_result(("a",), posted=0)
        )
        sessions: list[str] = []
        monkeypatch.setattr(inv, "_build_agent", lambda session_id: sessions.append(session_id) or MagicMock())
        inv.run_cycle()
        inv.run_cycle()
        assert len(sessions) == 2 and sessions[0] != sessions[1]
        assert all(s.startswith("agentchat:knock:") for s in sessions)
