"""Tests for ``agentchatme_hermes.ws_daemon`` — the socket as a wake, on the SDK as shipped.

* Self-echo filter — own outbound never wakes.
* A peer frame wakes exactly once; the body is never read.
* A ``wake`` frame (a reminder) is one wake, same as ``message.new``.
* The realtime client is the SDK's, unpatched, with ``ack_on_receipt``: a
  ``message.new`` frame is acknowledged before the wake (and so before any
  gate) runs.
* Connect wakes (covers whatever landed while the socket was down).
* Supervisor + invite behaviour from the previous release stays green.
"""
from __future__ import annotations

import json
import logging
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentchatme import RealtimeClient

from agentchatme_hermes.types import AgentIdentity
from agentchatme_hermes.ws_daemon import WSDaemon


def _make_daemon(*, own_handle: str = "alice") -> tuple[WSDaemon, Any, Any]:
    """Construct a daemon with a recording wake callback. Never started."""
    on_wake = MagicMock()
    on_group_invite = MagicMock()
    daemon = WSDaemon(
        config=SimpleNamespace(
            api_key="ac_live_test",
            api_base="https://api.example.test",
            ws_url="wss://api.example.test",
        ),
        identity=AgentIdentity(handle=own_handle),
        on_wake=on_wake,
        on_group_invite=on_group_invite,
    )
    return daemon, on_wake, on_group_invite


def _frame(
    *,
    msg_id: str = "msg_x",
    conv_id: str = "conv_dm_xy",
    sender: str = "@bob",
    text: str = "hi",
    seq: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": msg_id,
        "conversation_id": conv_id,
        "from": sender,
        "type": "text",
        "content": {"text": text},
    }
    if seq is not None:
        payload["seq"] = seq
    return {"type": "message.new", "payload": payload}


# The shape the server sends (conformance/v1/wake-frame.json in both SDKs).
_WAKE_FRAME: dict[str, Any] = {
    "type": "wake",
    "payload": {
        "reason": "reminder",
        "conversation_id": "conv_direct0first01",
        "anchor_message_id": "msg_nova0first00001a",
        "step": 1,
        "at": "2026-11-04T23:13:10.000Z",
    },
}


class TestSelfEchoFilter:
    def test_self_echo_does_not_wake(self) -> None:
        daemon, on_wake, _inv = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@alice"))
        on_wake.assert_not_called()
        assert daemon._frames_self_filtered == 1

    def test_filter_is_case_insensitive_and_strips_at(self) -> None:
        daemon, on_wake, _inv = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@ALICE"))
        daemon._on_message_frame(_frame(sender="alice"))
        on_wake.assert_not_called()


class TestWake:
    def test_peer_message_wakes_once(self) -> None:
        daemon, on_wake, _inv = _make_daemon()
        daemon._on_message_frame(_frame(sender="@bob"))
        on_wake.assert_called_once_with()
        assert daemon._wakes == 1

    def test_wake_carries_no_message_content(self, caplog: pytest.LogCaptureFixture) -> None:
        daemon, on_wake, _inv = _make_daemon()
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.ws_daemon"):
            daemon._on_message_frame(_frame(sender="@bob", text="the secret body"))
        assert on_wake.call_args.args == ()
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "the secret body" not in joined
        assert "msg=msg_x" in joined and "conv=conv_dm_xy" in joined and "@bob" in joined

    def test_sender_field_fallback(self) -> None:
        daemon, on_wake, _inv = _make_daemon()
        frame = _frame()
        frame["payload"]["sender"] = frame["payload"].pop("from")
        daemon._on_message_frame(frame)
        on_wake.assert_called_once()

    def test_malformed_payload_logs_and_drops(self, caplog: pytest.LogCaptureFixture) -> None:
        daemon, on_wake, _inv = _make_daemon()
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.ws_daemon"):
            daemon._on_message_frame({"type": "message.new"})
        on_wake.assert_not_called()
        assert any("without dict payload" in rec.getMessage() for rec in caplog.records)

    def test_frame_without_sender_still_wakes(self) -> None:
        # A frame we cannot attribute is still a sign the tray changed.
        daemon, on_wake, _inv = _make_daemon()
        daemon._on_message_frame({"type": "message.new", "payload": {"id": "m", "conversation_id": "c"}})
        on_wake.assert_called_once()

    def test_wake_callback_failure_does_not_propagate(self, caplog: pytest.LogCaptureFixture) -> None:
        daemon, on_wake, _inv = _make_daemon()
        on_wake.side_effect = RuntimeError("simulated wake failure")
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes.ws_daemon"):
            daemon._on_message_frame(_frame())
        assert any("on_wake callback raised" in rec.getMessage() for rec in caplog.records)

    def test_connect_wakes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        daemon, on_wake, _inv = _make_daemon()
        monkeypatch.setattr(daemon, "_write_health", MagicMock())
        daemon._on_connect()  # no running loop: the invite catch-up logs and skips
        on_wake.assert_called_once()

    def test_counters(self) -> None:
        daemon, _on_wake, _inv = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@bob"))
        daemon._on_message_frame(_frame(sender="@alice"))
        daemon._on_message_frame({"type": "message.new"})
        assert daemon._frames_seen == 3
        assert daemon._frames_self_filtered == 1
        assert daemon._wakes == 1


class TestWakeFrame:
    def test_wake_frame_is_one_wake(self, caplog: pytest.LogCaptureFixture) -> None:
        daemon, on_wake, _inv = _make_daemon()
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.ws_daemon"):
            daemon._on_wake_frame(_WAKE_FRAME)
        on_wake.assert_called_once_with()
        assert daemon._wake_frames == 1 and daemon._wakes == 1
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "reminder" in joined and "conv_direct0first01" in joined and "step=1" in joined

    def test_wake_frame_without_payload_still_wakes(self) -> None:
        daemon, on_wake, _inv = _make_daemon()
        daemon._on_wake_frame({"type": "wake"})
        on_wake.assert_called_once()


class _FakeWS:
    """The minimum of a ``websockets`` connection the SDK's receive loop touches."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = list(frames)
        self.sent: list[dict[str, Any]] = []
        self.close_code = 1000
        self.close_reason = ""

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        return None

    def __aiter__(self) -> _FakeWS:
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            raise StopAsyncIteration
        return json.dumps(self._frames.pop(0))

    def acks(self) -> list[str]:
        return [f["message_id"] for f in self.sent if f.get("type") == "ack"]


class TestSdkClientAsShipped:
    def test_realtime_client_is_the_sdk_class_with_ack_on_receipt(self) -> None:
        daemon, _on_wake, _inv = _make_daemon()
        http = object()
        daemon._http_client = http  # type: ignore[assignment]
        rt = daemon._build_realtime_client()
        assert type(rt) is RealtimeClient, "no subclass, no patched seam"
        assert rt._opts.ack_on_receipt is True
        assert rt._opts.client is http, "the SDK drains and gap-fills through the HTTP client"
        assert rt._opts.auto_drain_on_connect is True
        assert rt._opts.client_identity.name == "hermes"
        assert rt._opts.base_url == "wss://api.example.test"

    async def test_frame_is_acked_before_the_wake_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Delivered = the machine received it: the ack goes out before the
        wake callback — and so before any gate — sees the frame."""
        daemon, on_wake, _inv = _make_daemon()
        monkeypatch.setattr(daemon, "_write_health", MagicMock())
        ws = _FakeWS(
            [
                {"type": "hello.ok", "capabilities": ["ack"]},
                _frame(msg_id="msg_1", conv_id="conv_1", seq=1),
                _frame(msg_id="msg_2", conv_id="conv_1", seq=2),
            ]
        )
        acks_at_wake: list[list[str]] = []
        on_wake.side_effect = lambda: acks_at_wake.append(list(ws.acks()))

        rt = daemon._build_realtime_client(reconnect=False)
        daemon._subscribe(rt)
        rt._ws = ws
        await rt._recv_loop()

        # connect wake, then one wake per frame; each frame acked before its wake.
        assert acks_at_wake == [[], ["msg_1"], ["msg_1", "msg_2"]]
        assert ws.acks() == ["msg_1", "msg_2"]
        assert daemon._wakes == 3

    async def test_wake_frame_through_the_sdk_reaches_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, on_wake, _inv = _make_daemon()
        monkeypatch.setattr(daemon, "_write_health", MagicMock())
        ws = _FakeWS([{"type": "hello.ok", "capabilities": ["ack"]}, dict(_WAKE_FRAME)])

        rt = daemon._build_realtime_client(reconnect=False)
        daemon._subscribe(rt)
        rt._ws = ws
        await rt._recv_loop()

        assert on_wake.call_count == 2, "connect + the wake frame"
        assert daemon._wake_frames == 1
        assert ws.acks() == [], "a wake frame carries no message: nothing to ack"

    async def test_malformed_wake_frame_is_an_error_not_a_wake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, on_wake, _inv = _make_daemon()
        monkeypatch.setattr(daemon, "_write_health", MagicMock())
        ws = _FakeWS(
            [{"type": "hello.ok", "capabilities": ["ack"]}, {"type": "wake", "payload": {"reason": 1}}]
        )
        rt = daemon._build_realtime_client(reconnect=False)
        daemon._subscribe(rt)
        rt._ws = ws
        await rt._recv_loop()
        assert on_wake.call_count == 1, "connect only"
        assert daemon._wake_frames == 0


class TestSupervisor:
    def test_event_loop_failure_restarts_inside_the_lock_holding_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, _on_wake, _inv = _make_daemon()
        calls = 0

        async def failing_then_stop() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated loop death")
            daemon._stopped.set()

        monkeypatch.setattr(daemon, "_main", failing_then_stop)
        monkeypatch.setattr(daemon, "_write_health", MagicMock())
        monkeypatch.setattr("agentchatme_hermes.ws_daemon.random.uniform", lambda _low, _high: 0.0)

        # On its own thread, as in production: ``_run_loop`` ends with
        # ``asyncio.set_event_loop(None)``, which on the test's main thread
        # would discard whatever loop pytest-asyncio left current.
        thread = threading.Thread(target=daemon._run_loop, daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive()

        assert calls == 2
        assert daemon._loop is None
        daemon._write_health.assert_any_call(  # type: ignore[attr-defined]
            "restart_wait", last_error="simulated loop death"
        )
        daemon._write_health.assert_called_with("stopped")  # type: ignore[attr-defined]

    async def test_disconnected_watchdog_escalates_to_supervisor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, _on_wake, _inv = _make_daemon()
        daemon._rt_client = SimpleNamespace(is_connected=False)
        daemon._disconnected_since = 0.0
        monkeypatch.setattr("agentchatme_hermes.ws_daemon._HEARTBEAT_INTERVAL_S", 0.0)
        monkeypatch.setattr("agentchatme_hermes.ws_daemon.time.monotonic", lambda: 151.0)
        monkeypatch.setattr(daemon, "_write_health", MagicMock())

        with pytest.raises(RuntimeError, match="remained disconnected"):
            await daemon._heartbeat_loop()

        daemon._write_health.assert_called_with("reconnecting")  # type: ignore[attr-defined]

    def test_disconnect_publishes_recovering_health(self, monkeypatch: pytest.MonkeyPatch) -> None:
        daemon, _on_wake, _inv = _make_daemon()
        health = MagicMock()
        monkeypatch.setattr(daemon, "_write_health", health)

        daemon._on_disconnect({"code": 1006, "reason": "network lost", "was_clean": False})

        assert daemon._disconnected_since is not None
        health.assert_called_with("reconnecting", last_error="network lost")


def _invite_frame(
    *,
    invite_id: str = "ginv_1",
    group_id: str = "grp_abc",
    group_name: str = "Agent Council",
    inviter: str = "@carol",
    member_count: int = 3,
) -> dict[str, Any]:
    return {
        "type": "group.invite.received",
        "payload": {
            "id": invite_id,
            "group_id": group_id,
            "group_name": group_name,
            "group_description": "planning room",
            "group_member_count": member_count,
            "inviter_handle": inviter,
            "created_at": "2026-07-24T14:05:04.713737+00:00",
        },
    }


class TestGroupInvite:
    def test_valid_invite_reaches_callback_parsed(self) -> None:
        daemon, on_wake, on_invite = _make_daemon()
        daemon._on_group_invite_frame(_invite_frame())
        on_invite.assert_called_once()
        (invite,) = on_invite.call_args.args
        assert invite.invite_id == "ginv_1"
        assert invite.group_id == "grp_abc"
        assert invite.inviter_handle == "carol"
        assert invite.member_count == 3
        assert daemon._invites_seen == 1
        on_wake.assert_not_called()  # an invite is not a tray wake

    def test_malformed_invite_payload_is_dropped(self) -> None:
        daemon, _on_wake, on_invite = _make_daemon()
        daemon._on_group_invite_frame({"type": "group.invite.received", "payload": {"id": "ginv_1"}})
        daemon._on_group_invite_frame({"type": "group.invite.received"})
        on_invite.assert_not_called()

    def test_callback_that_raises_does_not_kill_daemon(self, caplog: pytest.LogCaptureFixture) -> None:
        daemon, _on_wake, on_invite = _make_daemon()
        on_invite.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes.ws_daemon"):
            daemon._on_group_invite_frame(_invite_frame())
        assert any("on_group_invite callback raised" in rec.getMessage() for rec in caplog.records)
        assert "ginv_1" not in daemon._seen_invites, "a failed dispatch may be retried"

    def test_same_invite_dispatched_once(self) -> None:
        daemon, _on_wake, on_invite = _make_daemon()
        daemon._on_group_invite_frame(_invite_frame(invite_id="ginv_dup"))
        daemon._on_group_invite_frame(_invite_frame(invite_id="ginv_dup"))
        assert on_invite.call_count == 1


def _invite_item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ginv_pending",
        "group_id": "grp_pending",
        "group_name": "Offline Crew",
        "group_member_count": 2,
        "inviter_handle": "@dave",
        "created_at": "2026-07-24T14:05:04Z",
    }
    base.update(overrides)
    return base


class TestGroupInviteCatchup:
    async def test_catch_up_surfaces_pending_invites(self) -> None:
        daemon, _on_wake, on_invite = _make_daemon()
        daemon._http_client = SimpleNamespace(
            list_group_invites=AsyncMock(
                return_value=[_invite_item(id="ginv_a", group_id="grp_a"), _invite_item(id="ginv_b")]
            )
        )
        await daemon._drain_pending_invites()
        assert on_invite.call_count == 2
        assert {"ginv_a", "ginv_b"} <= daemon._seen_invites

    async def test_catch_up_dedups_against_live(self) -> None:
        daemon, _on_wake, on_invite = _make_daemon()
        daemon._on_group_invite_frame(_invite_frame(invite_id="ginv_shared"))
        daemon._http_client = SimpleNamespace(
            list_group_invites=AsyncMock(return_value=[_invite_item(id="ginv_shared")])
        )
        await daemon._drain_pending_invites()
        assert on_invite.call_count == 1

    async def test_catch_up_fetch_failure_is_contained(self, caplog: pytest.LogCaptureFixture) -> None:
        daemon, _on_wake, on_invite = _make_daemon()
        daemon._http_client = SimpleNamespace(
            list_group_invites=AsyncMock(side_effect=RuntimeError("network down"))
        )
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.ws_daemon"):
            await daemon._drain_pending_invites()
        on_invite.assert_not_called()
        assert any("pending-invite catch-up failed" in rec.getMessage() for rec in caplog.records)

    async def test_catch_up_without_http_client_is_noop(self) -> None:
        daemon, _on_wake, on_invite = _make_daemon()
        daemon._http_client = None
        await daemon._drain_pending_invites()
        on_invite.assert_not_called()
