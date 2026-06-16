"""Tests for local conversation-closure state."""
from __future__ import annotations

from typing import TYPE_CHECKING

from agentchatme_hermes.thread_closures import ThreadClosures

if TYPE_CHECKING:
    from pathlib import Path


class TestThreadClosures:
    def test_close_persists_and_reloads(self, tmp_path: Path) -> None:
        path = tmp_path / "closed.json"
        store = ThreadClosures(path=path)

        record = store.close("conv_dm_123", reason="spam")

        assert record.conversation_id == "conv_dm_123"
        assert record.reason == "spam"
        assert store.is_closed("conv_dm_123") is True

        reloaded = ThreadClosures(path=path)
        assert reloaded.is_closed("conv_dm_123") is True
        listed = reloaded.list_closed()
        assert len(listed) == 1
        assert listed[0].conversation_id == "conv_dm_123"
        assert listed[0].reason == "spam"

    def test_reopen_removes_state(self, tmp_path: Path) -> None:
        path = tmp_path / "closed.json"
        store = ThreadClosures(path=path)
        store.close("conv_dm_123")

        assert store.reopen("conv_dm_123") is True
        assert store.is_closed("conv_dm_123") is False

        reloaded = ThreadClosures(path=path)
        assert reloaded.is_closed("conv_dm_123") is False

    def test_reopen_missing_thread_is_false(self, tmp_path: Path) -> None:
        store = ThreadClosures(path=tmp_path / "closed.json")
        assert store.reopen("conv_missing") is False
