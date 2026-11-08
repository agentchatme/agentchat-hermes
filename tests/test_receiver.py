"""Tests for ``agentchatme_hermes.receiver`` — the kit's receive loop, hosted.

What must hold:

* the socket is the SDK's ``RealtimeClient`` as shipped: acks on receipt,
  the ``ack`` / ``heartbeat`` / ``rewake`` capabilities, Hermes' identity;
* the wedge guard replaces a socket that stays down, but never while the
  SDK waits out a delay the server asked for or retries after an auth
  rejection; a server that does not take ``rewake`` is said once;
* a crashed loop is rebuilt with backoff, and the health record says so;
* wired end to end (the real ``ReceiveLoop``, ``TextsCache`` and socket
  class, a fake API): a wake runs a cycle, a Hermes turn, the D5 record.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from agentchatme import RealtimeClient
from fake_keys import api_key

from agentchatme_hermes import hosted_tools as hosted_tools_module
from agentchatme_hermes import receiver as receiver_module
from agentchatme_hermes import turn_runner as turn_runner_module
from agentchatme_hermes.agent_invoker import AgentInvoker
from agentchatme_hermes.receiver import _DISCONNECTED_RESTART_AFTER_S, Receiver, _restart_after_s
from agentchatme_hermes.texts import texts_cache
from agentchatme_hermes.types import AgentIdentity
from agentchatme_hermes.ws_health import HealthBoard, read_ws_health

if TYPE_CHECKING:
    from pathlib import Path


def _config() -> Any:
    return SimpleNamespace(
        api_key=api_key("receiver"),
        api_base="https://api.example.test",
        ws_url="wss://api.example.test",
    )


def _receiver(tmp_path: Path, invoker: Any = None, **kwargs: Any) -> Receiver:
    inv = invoker or AgentInvoker(config=SimpleNamespace(), identity=AgentIdentity(handle="me"), runtime=SimpleNamespace())
    return Receiver(
        config=_config(),
        identity=AgentIdentity(handle="me"),
        invoker=inv,
        texts_factory=lambda client: texts_cache(client, tmp_path),
        **kwargs,
    )


class TestSocket:
    def test_the_sdk_socket_as_shipped(self, tmp_path: Path) -> None:
        receiver = _receiver(tmp_path)
        http = object()
        receiver._http = http  # type: ignore[assignment]
        rt = receiver._build_realtime_client()
        assert type(rt) is RealtimeClient, "no subclass, no patched seam"
        assert rt._opts.ack_on_receipt is True
        assert rt._opts.client is http, "the SDK drains and gap-fills through the HTTP client"
        assert rt._opts.client_identity.name == "hermes"
        assert {"heartbeat", "rewake"} <= set(rt._advertised_capabilities())

    def test_the_wedge_guard_waits_out_the_servers_delay(self) -> None:
        assert _restart_after_s(SimpleNamespace(state="backoff", rate_limited_ms=None)) == _DISCONNECTED_RESTART_AFTER_S
        assert _restart_after_s(SimpleNamespace(state="backoff", rate_limited_ms=600_000)) >= 600 * 1.1 + _DISCONNECTED_RESTART_AFTER_S
        assert _restart_after_s(SimpleNamespace(state="blocked", rate_limited_ms=None)) == float("inf")
        assert _restart_after_s(SimpleNamespace(state="backoff", rate_limited_ms=True)) == _DISCONNECTED_RESTART_AFTER_S

    async def test_a_socket_down_too_long_reaches_the_supervisor(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        receiver = _receiver(tmp_path)
        receiver._rt = SimpleNamespace(is_connected=False, state="backoff", rate_limited_ms=None)  # type: ignore[assignment]
        receiver._disconnected_since = 0.0
        monkeypatch.setattr(receiver_module, "_HEARTBEAT_INTERVAL_S", 0.0)
        monkeypatch.setattr(receiver_module.time, "monotonic", lambda: 151.0)
        with pytest.raises(RuntimeError, match="stayed disconnected"):
            await receiver._heartbeat_loop()

    async def test_not_while_the_server_asked_for_a_delay(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        receiver = _receiver(tmp_path)
        receiver._rt = SimpleNamespace(is_connected=False, state="backoff", rate_limited_ms=600_000)  # type: ignore[assignment]
        receiver._disconnected_since = 0.0
        ticks = {"n": 0}

        async def sleep(_s: float) -> None:
            ticks["n"] += 1
            if ticks["n"] > 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(receiver_module.asyncio, "sleep", sleep)
        monkeypatch.setattr(receiver_module.time, "monotonic", lambda: 400.0)
        await receiver._heartbeat_loop()  # returns on cancel: no RuntimeError at 400 s

    def test_missing_rewake_is_said_once(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        receiver = _receiver(tmp_path)
        receiver._rt = SimpleNamespace(capabilities=frozenset({"ack"}))  # type: ignore[assignment]
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.receiver"):
            receiver._on_connect()
            receiver._on_connect()
        assert sum("rewake" in r.getMessage() for r in caplog.records) == 1
        caplog.clear()
        quiet = _receiver(tmp_path)
        quiet._rt = SimpleNamespace(capabilities=frozenset({"ack", "rewake"}))  # type: ignore[assignment]
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.receiver"):
            quiet._on_connect()
        assert not [r for r in caplog.records if "rewake" in r.getMessage()]


class TestEvents:
    def test_cycle_and_outcome_events_are_logged_without_content(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        receiver = _receiver(tmp_path)
        summary = SimpleNamespace(outcome="decided", cards=2, invites=0, reply=1, dismiss=1, defer=0, turns_started=1, turns_queued=0, turns_refused=0, paced=0, held_by_turns=0, failure=None)
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.receiver"):
            receiver._on_event({"type": "wake", "source": "wake_frame", "reason": "unanswered"})
            receiver._on_event({"type": "cycle", "summary": summary})
            receiver._on_event({"type": "turn_outcome", "outcome": SimpleNamespace(reason="answer_sent", action="record_reply"), "recorded": True})
            receiver._on_event({"type": "error", "error": RuntimeError("tray 503")})
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "wake — wake_frame unanswered" in text and "cycle decided" in text and "answer_sent" in text
        assert receiver._last_error == "tray 503"


class TestSupervision:
    def test_a_crashed_loop_is_rebuilt_and_the_health_record_says_so(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        board = HealthBoard(handle="me", path=tmp_path / "ws.health.json")
        receiver = _receiver(tmp_path, health=board)
        board.set_worker_status(receiver.worker_status)
        monkeypatch.setattr(receiver_module, "_SUPERVISOR_BACKOFF_BASE_S", 0.01)
        calls = {"n": 0}
        crashed = threading.Event()

        async def flaky_main() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("loop exploded")
            crashed.set()
            receiver._stop_event = asyncio.Event()
            await receiver._stop_event.wait()

        monkeypatch.setattr(receiver, "_main", flaky_main)
        receiver.start()
        try:
            assert crashed.wait(5.0)
            assert receiver.restarts == 1 and "loop exploded" in (receiver._last_error or "")
            status = receiver.worker_status()
            assert status["alive"] is True and status["restarts"] == 1
        finally:
            receiver.stop()
        assert not receiver.alive
        health = read_ws_health(tmp_path / "ws.health.json")
        assert health is not None and health["state"] == "stopped"
        assert health["worker"]["state"] == "stopped"


# ─── the receiver wired end to end ─────────────────────────────────────────

SERVED_TEXTS = {
    "version": "1",
    "runtime": "hermes",
    "manual": {"url": "https://agentchat.me/skill.md", "version": "2.2.2"},
    "wake_instruction": "You were woken. Call mcp__agentchat__agentchat_inbox first.",
    "invite_prompt": "An invite is pending.",
    "identity_line": "I am @me on AgentChat.",
    "identity_line_template": "I am @<handle> on AgentChat.",
}


class _AsyncApi:
    """The API as the loop and the texts cache call it (async)."""

    def __init__(self, **_kw: Any) -> None:
        self.unread = {"conv_a": "msg_a"}
        self.answered: dict[str, str] = {}
        self.recorded: list[tuple[str, str, str | None]] = []
        self.inbox = SimpleNamespace(tray=self._tray)
        self.decisions = SimpleNamespace(record=self._record)
        self.groups = SimpleNamespace(dismiss_invite=self._dismiss)
        self.texts = SimpleNamespace(get=self._texts)
        _AsyncApi.last = self

    last: _AsyncApi | None = None

    async def _texts(self, *, runtime: str, opts: Any = None) -> Any:
        from agentchatme.types import Texts

        assert runtime == "hermes"
        return Texts.model_validate(SERVED_TEXTS)

    async def _tray(self, **_kw: Any) -> dict[str, Any]:
        read = {cid for cid, decision, _a in self.recorded if decision in ("reply", "dismiss")}
        cards = [
            {
                "conversation_id": cid,
                "type": "direct",
                "counterparty": {"handle": "@peer"},
                "relationship": "established",
                "you_owe_reply": self.answered.get(cid) != newest,
                "mentioned_you": False,
                "unread": 1,
                "pace": {"last_60s": 0, "last_10m": 1},
                "preview": {"message_id": newest, "sender": "@peer", "text": "hello", "at": "2026-11-08T10:00:00.000Z"},
            }
            for cid, newest in self.unread.items()
            if cid not in read
        ]
        return {"version": 1, "you": "@me", "question": "Decide per card with agentchat_decide.", "categories": ["open_request"], "conversations": cards, "deferred": 0, "truncated": False, "invites": [], "invites_truncated": False}

    async def _record(self, conversation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append((conversation_id, kwargs["decision"], kwargs.get("through_message_id")))
        return {}

    async def _dismiss(self, invite_id: str) -> dict[str, Any]:
        return {}

    async def aclose(self) -> None:
        return None


class TestWiredEndToEnd:
    def test_a_wake_runs_a_cycle_a_hermes_turn_and_the_record(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import agentchatme

        monkeypatch.setattr(agentchatme, "AsyncAgentChatClient", _AsyncApi)
        monkeypatch.setattr(hosted_tools_module, "hosted_tool_names", lambda: ["mcp__agentchat__agentchat_inbox"])

        class _Agent:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            def run_conversation(self, prompt: str, conversation_history: list[Any]) -> Any:
                self.prompts.append(prompt)
                assert _AsyncApi.last is not None
                _AsyncApi.last.answered["conv_a"] = "msg_a"  # the turn sent its answer
                return {"final_response": "", "messages": [{"role": "tool", "name": "mcp__agentchat__agentchat_send_message", "content": '{"result": "{}"}'}]}

        agent = _Agent()
        inv = AgentInvoker(config=SimpleNamespace(), identity=AgentIdentity(handle="me"), runtime=SimpleNamespace())
        inv._limits = turn_runner_module.TurnLimits(idle_s=5.0, max_s=10.0, grace_s=1.0, poll_s=0.01)
        monkeypatch.setattr(inv, "_build_agent", lambda _sid: agent)
        monkeypatch.setattr(inv, "_ensure_hermes_resolved", lambda: None)
        monkeypatch.setattr(inv, "_resolve_model_and_runtime", lambda: ("m", {}))
        monkeypatch.setattr(
            "agentchatme_hermes.agent_invoker.build_call_model",
            lambda **kw: (lambda prompt: json.dumps({"decisions": [{"conversation_id": "conv_a", "decision": "reply"}]})),
        )
        placed: list[str] = []
        receiver = _receiver(tmp_path, inv, on_texts=lambda texts: placed.append(texts.identity_line))

        async def never(*_a: Any, **_kw: Any) -> Any:
            raise ConnectionError("no network in tests")

        real_build = receiver._build_realtime_client
        monkeypatch.setattr(receiver, "_build_realtime_client", lambda: real_build(websocket_connect=never, reconnect=False))
        receiver.start()
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and receiver._receive_loop is None:
                time.sleep(0.01)
            receiver.wake()
            while time.monotonic() < deadline:
                api = _AsyncApi.last
                if api is not None and api.recorded:
                    break
                time.sleep(0.02)
        finally:
            receiver.stop()
        api = _AsyncApi.last
        assert api is not None and api.recorded == [("conv_a", "reply", "msg_a")]
        assert agent.prompts and "conversation_id: conv_a" in agent.prompts[0]
        assert placed == ["I am @me on AgentChat."], "the served texts reached the runtime"
        assert (tmp_path / "texts-hermes.json").exists()
        assert not receiver.alive


def test_stop_before_start_is_harmless(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    receiver.stop()
    receiver.stop()
    assert MagicMock is not None and not receiver.alive
