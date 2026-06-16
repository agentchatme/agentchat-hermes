"""Tests for conversation-level tools."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

from agentchatme_hermes.thread_closures import ThreadClosures
from agentchatme_hermes.tools.conversations import (
    _build_close_local_thread,
    _build_list_local_closed_threads,
    _build_reopen_local_thread,
)

if TYPE_CHECKING:
    from pathlib import Path


def _runtime(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        thread_closures=ThreadClosures(path=tmp_path / "closed.json")
    )


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
