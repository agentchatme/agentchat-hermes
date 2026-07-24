"""Tests for ``agentchatme_hermes.ws_daemon``.

Focused on the regressions fixed in 0.2.0:

* Self-echo filter — own outbound is suppressed.
* Per-frame logging — frames produce INFO log records the operator
  can see in the gateway log.
* Defensive try/except — a malformed payload does not raise out of
  the frame callback (would kill the WS thread).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from agentchatme_hermes.thread_closures import ThreadClosures
from agentchatme_hermes.types import AgentIdentity
from agentchatme_hermes.ws_daemon import WSDaemon

if TYPE_CHECKING:
    import pytest


def _make_daemon(*, own_handle: str = "alice") -> tuple[WSDaemon, Any, Any, Any]:
    """Construct a daemon with mocked queue + callback for unit testing.

    The daemon is NEVER started — we only exercise the synchronous
    ``_on_message_frame`` callback path. Background-thread / loop
    machinery is integration territory.
    """
    queue = MagicMock()
    thread_closures = MagicMock(spec=ThreadClosures)
    thread_closures.is_closed.return_value = False
    on_new_event = MagicMock()
    # on_group_invite is reachable via daemon._on_group_invite in invite tests;
    # kept off the return tuple so existing 4-tuple call sites are untouched.
    on_group_invite = MagicMock()
    daemon = WSDaemon(
        config=SimpleNamespace(
            api_key="ac_live_test",
            api_base="https://api.example.test",
            ws_url="wss://api.example.test/v1/ws",
        ),
        identity=AgentIdentity(handle=own_handle),
        queue=queue,
        thread_closures=thread_closures,
        on_new_event=on_new_event,
        on_group_invite=on_group_invite,
    )
    return daemon, queue, on_new_event, thread_closures


def _frame(
    *,
    msg_id: str = "msg_x",
    conv_id: str = "conv_dm_xy",
    sender: str = "@bob",
    text: str = "hi",
) -> dict[str, Any]:
    return {
        "type": "message.new",
        "payload": {
            "id": msg_id,
            "conversation_id": conv_id,
            "from": sender,
            "type": "text",
            "content": {"text": text},
        },
    }


class TestSelfEchoFilter:
    def test_self_echo_does_not_push(self) -> None:
        daemon, queue, on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@alice"))
        queue.push.assert_not_called()
        on_new_event.assert_not_called()

    def test_self_echo_filter_is_case_insensitive(self) -> None:
        daemon, queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@ALICE"))
        queue.push.assert_not_called()

    def test_self_echo_filter_strips_at_prefix(self) -> None:
        daemon, queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="alice"))
        queue.push.assert_not_called()


class TestHappyPath:
    def test_peer_message_pushed_and_wakes_invoker(self) -> None:
        daemon, queue, on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@bob"))
        queue.push.assert_called_once()
        on_new_event.assert_called_once()

    def test_locally_closed_thread_is_suppressed(self) -> None:
        daemon, queue, on_new_event, closures = _make_daemon(own_handle="alice")
        closures.is_closed.return_value = True
        daemon._on_message_frame(_frame(sender="@bob"))
        queue.push.assert_not_called()
        on_new_event.assert_not_called()
        assert daemon._frames_thread_closed == 1


class TestPerFrameLogging:
    def test_peer_message_logs_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        with caplog.at_level(logging.INFO, logger="agentchatme_hermes.ws_daemon"):
            daemon._on_message_frame(_frame(sender="@bob", text="hello"))
        # The INFO line should mention the message and conversation id.
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "msg=msg_x" in joined
        assert "conv=conv_dm_xy" in joined
        assert "@bob" in joined

    def test_malformed_payload_logs_warning_and_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon, queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.ws_daemon"):
            # Missing the entire payload field.
            daemon._on_message_frame({"type": "message.new"})
        queue.push.assert_not_called()
        # The warning should fire on a bad frame.
        assert any(
            "without dict payload" in rec.getMessage()
            for rec in caplog.records
        )


class TestDefensiveHandling:
    def test_queue_push_failure_does_not_propagate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A queue.push that raises must NOT bubble out of the callback.

        The WS event loop runs this callback. An unhandled exception
        here would kill the loop and silently end live inbound — the
        exact failure mode we are fixing.
        """
        daemon, queue, on_new_event, _closures = _make_daemon(own_handle="alice")
        queue.push.side_effect = RuntimeError("simulated queue failure")

        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes.ws_daemon"):
            daemon._on_message_frame(_frame(sender="@bob"))

        on_new_event.assert_not_called()  # we returned before signalling
        assert any(
            "queue.push raised" in rec.getMessage()
            for rec in caplog.records
        )

    def test_on_new_event_failure_does_not_propagate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon, queue, on_new_event, _closures = _make_daemon(own_handle="alice")
        on_new_event.side_effect = RuntimeError("simulated wake failure")

        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes.ws_daemon"):
            daemon._on_message_frame(_frame(sender="@bob"))

        # Push still happened — the event is in the queue and the next
        # wake will pick it up.
        queue.push.assert_called_once()
        assert any(
            "on_new_event callback raised" in rec.getMessage()
            for rec in caplog.records
        )


class TestCounters:
    def test_counters_track_frames(self) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")

        # 1 peer frame, 1 self-echo, 1 malformed → frames_seen=3,
        # filtered=1, queued=1.
        daemon._on_message_frame(_frame(sender="@bob"))
        daemon._on_message_frame(_frame(sender="@alice"))
        daemon._on_message_frame({"type": "message.new"})

        assert daemon._frames_seen == 3
        assert daemon._frames_self_filtered == 1
        assert daemon._frames_queued == 1
        assert daemon._frames_thread_closed == 0


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
    """`group.invite.received` frames wake the consent-decision path.

    The bug this covers: the daemon only subscribed to `message.new`, so an
    invited agent was never notified. These lock in that a well-formed invite
    reaches the `on_group_invite` callback with a parsed event, that garbage
    frames are dropped (never the daemon-killing raise), and that a callback
    that raises is contained.
    """

    def test_valid_invite_reaches_callback_parsed(self) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_group_invite_frame(_invite_frame())

        daemon._on_group_invite.assert_called_once()
        (invite,) = daemon._on_group_invite.call_args.args
        assert invite.invite_id == "ginv_1"
        assert invite.group_id == "grp_abc"
        assert invite.group_name == "Agent Council"
        assert invite.inviter_handle == "carol"  # @-stripped, lowered
        assert invite.member_count == 3
        assert daemon._invites_seen == 1

    def test_invite_is_not_queued_and_does_not_wake_message_path(self) -> None:
        # Invites bypass the message queue + reply-gate entirely.
        daemon, queue, on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_group_invite_frame(_invite_frame())
        queue.push.assert_not_called()
        on_new_event.assert_not_called()

    def test_malformed_invite_payload_is_dropped(self) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        # Missing the load-bearing group_id → GroupInviteEvent.from_ws_frame None.
        daemon._on_group_invite_frame(
            {"type": "group.invite.received", "payload": {"id": "ginv_1"}}
        )
        daemon._on_group_invite.assert_not_called()

    def test_non_dict_payload_is_dropped(self) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_group_invite_frame({"type": "group.invite.received"})
        daemon._on_group_invite.assert_not_called()

    def test_callback_that_raises_does_not_kill_daemon(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_group_invite.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes.ws_daemon"):
            # Must NOT raise.
            daemon._on_group_invite_frame(_invite_frame())
        assert any(
            "on_group_invite callback raised" in rec.getMessage()
            for rec in caplog.records
        )

    def test_same_invite_dispatched_once(self) -> None:
        # Two live frames for the same invite_id → decided once (dedup).
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_group_invite_frame(_invite_frame(invite_id="ginv_dup"))
        daemon._on_group_invite_frame(_invite_frame(invite_id="ginv_dup"))
        assert daemon._on_group_invite.call_count == 1

    def test_failed_dispatch_is_not_marked_seen(self) -> None:
        # A callback that raises leaves the invite unseen so a later catch-up
        # can retry — a lost decision must not become permanent.
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_group_invite.side_effect = RuntimeError("boom")
        daemon._on_group_invite_frame(_invite_frame(invite_id="ginv_retry"))
        assert "ginv_retry" not in daemon._seen_invites


def _invite_item(**overrides: Any) -> dict[str, Any]:
    """A pending-invite REST item (same GroupInvitation shape as the frame
    payload) as returned by ``GET /v1/groups/invites``."""
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
    """Connect-time catch-up: pending invites missed while offline are surfaced.

    Group-invite frames are live-only (not part of the message `/sync` drain),
    so an agent invited while disconnected would never learn of it. On every
    (re)connect the daemon drains `GET /v1/groups/invites` and routes anything
    unseen through the same decision path — deduped so a still-pending invite
    isn't re-decided each reconnect.
    """

    async def test_catch_up_surfaces_pending_invites(self) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._http_client = SimpleNamespace(
            list_group_invites=AsyncMock(
                return_value=[
                    _invite_item(id="ginv_a", group_id="grp_a"),
                    _invite_item(id="ginv_b", group_id="grp_b"),
                ]
            )
        )
        await daemon._drain_pending_invites()
        assert daemon._on_group_invite.call_count == 2
        assert {"ginv_a", "ginv_b"} <= daemon._seen_invites

    async def test_catch_up_dedups_against_live(self) -> None:
        # Live frame handled first; the same invite in the catch-up is skipped.
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._on_group_invite_frame(_invite_frame(invite_id="ginv_shared"))
        daemon._http_client = SimpleNamespace(
            list_group_invites=AsyncMock(
                return_value=[_invite_item(id="ginv_shared")]
            )
        )
        await daemon._drain_pending_invites()
        # Live once; catch-up skips the duplicate → still exactly one decision.
        assert daemon._on_group_invite.call_count == 1

    async def test_catch_up_fetch_failure_is_contained(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._http_client = SimpleNamespace(
            list_group_invites=AsyncMock(side_effect=RuntimeError("network down"))
        )
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.ws_daemon"):
            await daemon._drain_pending_invites()  # must not raise
        daemon._on_group_invite.assert_not_called()
        assert any(
            "pending-invite catch-up failed" in rec.getMessage()
            for rec in caplog.records
        )

    async def test_catch_up_without_http_client_is_noop(self) -> None:
        daemon, _queue, _on_new_event, _closures = _make_daemon(own_handle="alice")
        daemon._http_client = None
        await daemon._drain_pending_invites()  # must not raise
        daemon._on_group_invite.assert_not_called()
