"""Tests for conversation-level tools."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agentchatme_hermes.thread_closures import ThreadClosures
from agentchatme_hermes.tools.conversations import (
    _build_close_local_thread,
    _build_list_conversations,
    _build_list_local_closed_threads,
    _build_list_needs_reply_candidates,
    _build_list_unread_by_sender,
    _build_reopen_local_thread,
)

if TYPE_CHECKING:
    from pathlib import Path


def _runtime(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        thread_closures=ThreadClosures(path=tmp_path / "closed.json"),
        identity=SimpleNamespace(handle="me"),
        client=MagicMock(),
    )


def _direct_conv(conv_id: str, handle: str) -> dict[str, object]:
    return {
        "id": conv_id,
        "type": "direct",
        "participants": [{"handle": handle, "display_name": handle.title()}],
        "last_message_at": "2026-06-17T12:00:00Z",
        "updated_at": "2026-06-17T12:00:00Z",
        "is_muted": False,
    }


def _group_conv(conv_id: str, name: str) -> dict[str, object]:
    return {
        "id": conv_id,
        "type": "group",
        "group_name": name,
        "group_member_count": 3,
        "participants": [],
        "last_message_at": "2026-06-17T11:00:00Z",
        "updated_at": "2026-06-17T11:00:00Z",
        "is_muted": False,
    }


def _message(
    *,
    seq: int,
    sender: str,
    text: str,
    read_at: str | None = None,
    created_at: str = "2026-06-17T12:00:00Z",
) -> dict[str, object]:
    return {
        "id": f"msg_{seq}",
        "conversation_id": "conv_unused",
        "sender": sender,
        "client_msg_id": f"cmid_{seq}",
        "seq": seq,
        "type": "text",
        "content": {"text": text},
        "metadata": {},
        "status": "stored",
        "created_at": created_at,
        "delivered_at": None,
        "read_at": read_at,
    }


class TestLocalThreadTools:
    def test_close_local_thread(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        handler = _build_close_local_thread(runtime)

        result = json.loads(
            handler({"conversation_id": "conv_dm_123", "reason": "done"})
        )

        assert result["ok"] is True
        assert result["closed_conversation_id"] == "conv_dm_123"
        assert result["reason"] == "done"
        assert runtime.thread_closures.is_closed("conv_dm_123") is True

    def test_reopen_local_thread(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        runtime.thread_closures.close("conv_dm_123")
        handler = _build_reopen_local_thread(runtime)

        result = json.loads(handler({"conversation_id": "conv_dm_123"}))

        assert result["ok"] is True
        assert result["reopened"] is True
        assert runtime.thread_closures.is_closed("conv_dm_123") is False

    def test_list_local_closed_threads(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        runtime.thread_closures.close("conv_dm_123", reason="spam")
        runtime.thread_closures.close("conv_grp_999", reason="done")
        handler = _build_list_local_closed_threads(runtime)

        result = json.loads(handler({}))

        assert result["ok"] is True
        ids = {item["conversation_id"] for item in result["closed_threads"]}
        assert ids == {"conv_dm_123", "conv_grp_999"}


class TestInboxTriageTools:
    def test_list_conversations_filters_by_kind(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        runtime.client.list_conversations.return_value = [
            _direct_conv("conv_dm_1", "alice"),
            _group_conv("conv_grp_2", "Build Team"),
        ]
        handler = _build_list_conversations(runtime)

        result = json.loads(handler({"kind": "group", "limit": 1}))

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["kind_filter"] == "group"
        assert result["conversations"][0]["id"] == "conv_grp_2"

    def test_list_unread_by_sender_groups_unread_tail(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        runtime.client.list_conversations.return_value = [
            _direct_conv("conv_dm_1", "alice"),
            _group_conv("conv_grp_2", "Build Team"),
        ]
        runtime.client.get_messages.side_effect = [
            [
                _message(seq=3, sender="alice", text="ping again"),
                _message(seq=2, sender="alice", text="ping"),
                _message(
                    seq=1,
                    sender="me",
                    text="hello",
                    read_at="2026-06-17T10:00:00Z",
                ),
            ],
            [
                _message(seq=7, sender="bob", text="need review"),
                _message(
                    seq=6,
                    sender="carol",
                    text="posted draft",
                    read_at="2026-06-17T09:00:00Z",
                ),
            ],
        ]
        handler = _build_list_unread_by_sender(runtime)

        result = json.loads(
            handler({"conversation_limit": 5, "messages_per_conversation": 10})
        )

        assert result["ok"] is True
        assert result["sender_count"] == 2
        assert result["senders"][0]["sender_handle"] == "alice"
        assert result["senders"][0]["unread_message_count"] == 2
        assert result["senders"][0]["conversations"][0]["conversation_id"] == "conv_dm_1"
        assert result["senders"][1]["sender_handle"] == "bob"

    def test_list_needs_reply_candidates_uses_latest_inbound(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        runtime.client.list_conversations.return_value = [
            _direct_conv("conv_dm_1", "alice"),
            _group_conv("conv_grp_2", "Build Team"),
            _direct_conv("conv_dm_3", "dave"),
        ]
        runtime.client.get_messages.side_effect = [
            [
                _message(seq=5, sender="alice", text="can you take this?", read_at=None),
                _message(
                    seq=4,
                    sender="me",
                    text="yesterday update",
                    read_at="2026-06-16T10:00:00Z",
                ),
            ],
            [
                _message(
                    seq=8,
                    sender="me",
                    text="I handled it",
                    read_at="2026-06-17T11:00:00Z",
                ),
                _message(seq=7, sender="bob", text="can someone look?", read_at=None),
            ],
            [
                _message(
                    seq=2,
                    sender="dave",
                    text="thanks",
                    read_at="2026-06-17T08:00:00Z",
                )
            ],
        ]
        handler = _build_list_needs_reply_candidates(runtime)

        result = json.loads(handler({"only_unread": True}))

        assert result["ok"] is True
        assert result["count"] == 1
        assert result["conversations"][0]["conversation_id"] == "conv_dm_1"
        assert result["conversations"][0]["latest_sender_handle"] == "alice"
        assert result["conversations"][0]["latest_message_unread"] is True
