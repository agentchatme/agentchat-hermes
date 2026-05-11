"""Tests for the cron out-of-process delivery hook.

`adapter._standalone_send` is invoked by `tools.send_message_tool` when
`hermes cron run` executes in a SEPARATE process from `hermes gateway`,
so no live `AgentChatAdapter` is available. Without this hook,
`deliver=agentchat` cron jobs fail with `No live adapter for platform
'agentchat'` (`tools/send_message_tool.py:478-511`).

Contract (mirrors plugins/platforms/{irc,line,teams,google_chat}/adapter.py):

    async def _standalone_send(
        pconfig,
        chat_id: str,
        message: str,
        *,
        thread_id: str | None = None,
        media_files: list[str] | None = None,
        force_document: bool = False,
    ) -> dict[str, Any]:
        return {"success": True, "message_id": ...}
        # or {"error": "..."}

These tests cover the no-network failure modes (missing key, missing
chat_id) and the routing of `chat_id` into the SDK kwargs — the rest
of the surface needs a live network and lives in
`test_smoke_live.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def clean_env(monkeypatch):
    """Strip AgentChat env vars so each test gets a known baseline."""
    for var in (
        "AGENTCHATME_API_KEY",
        "AGENTCHATME_API_BASE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


async def test_returns_error_when_no_api_key(clean_env):
    """Missing key must fail-fast with a friendly error, not raise."""
    from agentchatme_hermes.adapter import _standalone_send

    pconfig = SimpleNamespace(extra={})
    result = await _standalone_send(pconfig, "@alice", "hello")
    assert isinstance(result, dict)
    assert "error" in result
    assert "AGENTCHATME_API_KEY" in result["error"]


async def test_returns_error_when_no_chat_id(clean_env):
    """Empty chat_id must fail-fast with a friendly error, not raise."""
    from agentchatme_hermes.adapter import _standalone_send

    clean_env.setenv("AGENTCHATME_API_KEY", "ac_test_key_123")
    pconfig = SimpleNamespace(extra={})
    result = await _standalone_send(pconfig, "", "hello")
    assert "error" in result
    assert "chat_id" in result["error"]


async def test_routes_handle_to_send_message_to_kwarg(clean_env):
    """`@alice` chat_id must become `to="@alice"` on the SDK call."""
    from agentchatme_hermes import adapter as adapter_mod

    clean_env.setenv("AGENTCHATME_API_KEY", "ac_test_key_123")

    fake_message = {"id": "msg_abc"}
    fake_result = SimpleNamespace(message=fake_message)
    fake_client = AsyncMock()
    fake_client.send_message = AsyncMock(return_value=fake_result)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(
        adapter_mod, "AsyncAgentChatClient", return_value=fake_client
    ):
        result = await adapter_mod._standalone_send(
            SimpleNamespace(extra={}), "@alice", "hi"
        )

    assert result == {"success": True, "message_id": "msg_abc"}
    fake_client.send_message.assert_awaited_once()
    call_kwargs = fake_client.send_message.await_args.kwargs
    assert call_kwargs["to"] == "@alice"
    assert call_kwargs["content"] == {"type": "text", "text": "hi"}


async def test_routes_conv_to_conversation_id_kwarg(clean_env):
    """`conv_xyz` chat_id must become `conversation_id="conv_xyz"`."""
    from agentchatme_hermes import adapter as adapter_mod

    clean_env.setenv("AGENTCHATME_API_KEY", "ac_test_key_123")

    fake_client = AsyncMock()
    fake_client.send_message = AsyncMock(
        return_value=SimpleNamespace(message={"id": "msg_xyz"})
    )
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(
        adapter_mod, "AsyncAgentChatClient", return_value=fake_client
    ):
        result = await adapter_mod._standalone_send(
            SimpleNamespace(extra={}), "conv_room1", "yo"
        )

    assert result["success"] is True
    call_kwargs = fake_client.send_message.await_args.kwargs
    assert call_kwargs["conversation_id"] == "conv_room1"
    assert "to" not in call_kwargs


async def test_bare_handle_gets_at_prefix(clean_env):
    """Bare `alice` must become `to="@alice"` (matches send() behavior)."""
    from agentchatme_hermes import adapter as adapter_mod

    clean_env.setenv("AGENTCHATME_API_KEY", "ac_test_key_123")

    fake_client = AsyncMock()
    fake_client.send_message = AsyncMock(
        return_value=SimpleNamespace(message={"id": "msg_x"})
    )
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(
        adapter_mod, "AsyncAgentChatClient", return_value=fake_client
    ):
        await adapter_mod._standalone_send(
            SimpleNamespace(extra={}), "alice", "hey"
        )

    call_kwargs = fake_client.send_message.await_args.kwargs
    assert call_kwargs["to"] == "@alice"


async def test_returns_error_dict_when_send_raises_unauthorized(clean_env):
    """A 401 from the SDK must surface as a clean error dict, not raise."""
    from agentchatme.errors import UnauthorizedError

    from agentchatme_hermes import adapter as adapter_mod

    clean_env.setenv("AGENTCHATME_API_KEY", "ac_test_key_123")

    fake_client = AsyncMock()
    # UnauthorizedError signature: (response: Mapping, status: int, request_id=None)
    fake_client.send_message = AsyncMock(
        side_effect=UnauthorizedError(
            {"error": {"message": "nope"}}, 401, None
        )
    )
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(
        adapter_mod, "AsyncAgentChatClient", return_value=fake_client
    ):
        result = await adapter_mod._standalone_send(
            SimpleNamespace(extra={}), "@alice", "hi"
        )

    assert "error" in result
    assert "API key rejected" in result["error"]
    fake_client.__aexit__.assert_awaited_once()


async def test_media_files_appends_deferral_note(clean_env):
    """Out-of-process path can't upload media; recipient should see why."""
    from agentchatme_hermes import adapter as adapter_mod

    clean_env.setenv("AGENTCHATME_API_KEY", "ac_test_key_123")

    fake_client = AsyncMock()
    fake_client.send_message = AsyncMock(
        return_value=SimpleNamespace(message={"id": "msg_z"})
    )
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(
        adapter_mod, "AsyncAgentChatClient", return_value=fake_client
    ):
        await adapter_mod._standalone_send(
            SimpleNamespace(extra={}),
            "@alice",
            "report attached",
            media_files=["/tmp/chart.png", "/tmp/data.csv"],
        )

    call_kwargs = fake_client.send_message.await_args.kwargs
    text = call_kwargs["content"]["text"]
    assert "report attached" in text
    assert "2 attachment" in text
    assert "not deliverable from cron" in text


def test_standalone_sender_is_registered_in_plugin_yaml_contract():
    """The hook must be reachable as a module-level symbol so Hermes can
    bind it on the PlatformEntry. A regression here means someone moved
    it inside the closure and the cron-side path will fail."""
    import inspect

    from agentchatme_hermes import adapter as adapter_mod

    assert hasattr(adapter_mod, "_standalone_send")
    assert callable(adapter_mod._standalone_send)
    assert inspect.iscoroutinefunction(adapter_mod._standalone_send)
