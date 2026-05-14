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
from unittest.mock import MagicMock

from agentchatme_hermes.types import AgentIdentity
from agentchatme_hermes.ws_daemon import WSDaemon

if TYPE_CHECKING:
    import pytest


def _make_daemon(*, own_handle: str = "alice") -> tuple[WSDaemon, Any, Any]:
    """Construct a daemon with mocked queue + callback for unit testing.

    The daemon is NEVER started — we only exercise the synchronous
    ``_on_message_frame`` callback path. Background-thread / loop
    machinery is integration territory.
    """
    queue = MagicMock()
    on_new_event = MagicMock()
    daemon = WSDaemon(
        config=SimpleNamespace(
            api_key="ac_live_test",
            api_base="https://api.example.test",
            ws_url="wss://api.example.test/v1/ws",
        ),
        identity=AgentIdentity(handle=own_handle),
        queue=queue,
        on_new_event=on_new_event,
    )
    return daemon, queue, on_new_event


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
        daemon, queue, on_new_event = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@alice"))
        queue.push.assert_not_called()
        on_new_event.assert_not_called()

    def test_self_echo_filter_is_case_insensitive(self) -> None:
        daemon, queue, _on_new_event = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@ALICE"))
        queue.push.assert_not_called()

    def test_self_echo_filter_strips_at_prefix(self) -> None:
        daemon, queue, _on_new_event = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="alice"))
        queue.push.assert_not_called()


class TestHappyPath:
    def test_peer_message_pushed_and_wakes_invoker(self) -> None:
        daemon, queue, on_new_event = _make_daemon(own_handle="alice")
        daemon._on_message_frame(_frame(sender="@bob"))
        queue.push.assert_called_once()
        on_new_event.assert_called_once()


class TestPerFrameLogging:
    def test_peer_message_logs_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon, _queue, _on_new_event = _make_daemon(own_handle="alice")
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
        daemon, queue, _on_new_event = _make_daemon(own_handle="alice")
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
        daemon, queue, on_new_event = _make_daemon(own_handle="alice")
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
        daemon, queue, on_new_event = _make_daemon(own_handle="alice")
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
        daemon, _queue, _on_new_event = _make_daemon(own_handle="alice")

        # 1 peer frame, 1 self-echo, 1 malformed → frames_seen=3,
        # filtered=1, queued=1.
        daemon._on_message_frame(_frame(sender="@bob"))
        daemon._on_message_frame(_frame(sender="@alice"))
        daemon._on_message_frame({"type": "message.new"})

        assert daemon._frames_seen == 3
        assert daemon._frames_self_filtered == 1
        assert daemon._frames_queued == 1
