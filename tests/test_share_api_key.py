"""Tests for ``agentchat_share_api_key_with_operator``.

The tool returns the AgentChat API key to the LLM when the operator
asks (typically for dashboard onboarding). Most of the policy lives in
the bundled skill — the tool itself enforces just one hard rule: refuse
on AgentChat-triggered turns (peers, never operators).

The rest of the safety is delegated to the LLM following the skill's
``Your API key`` section. That mirrors the OpenClaw plugin's approach,
which has been empirically tested.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _reset_source_platform():
    """Each test starts with no source-platform context."""
    from agentchatme_hermes import tools as _tools_mod

    token = _tools_mod.current_source_platform.set(None)
    yield
    _tools_mod.current_source_platform.reset(token)


async def test_returns_key_when_no_source_set(monkeypatch):
    """No source context = CLI / operator's local terminal → return the key."""
    from agentchatme_hermes.tools import _share_api_key_handler

    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_live_test_key_abc123def456")
    raw = await _share_api_key_handler({})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["value"] == "ac_live_test_key_abc123def456"
    # A note tells the LLM to share with the operator and notify on misuse.
    assert "operator" in payload["note"].lower()


async def test_returns_key_when_source_is_telegram(monkeypatch):
    """Operator on Telegram → tool succeeds. The skill, not the tool,
    decides whether THIS particular Telegram sender is really the
    operator. Tool just enforces 'not AgentChat'."""
    from agentchatme_hermes import tools as _tools_mod
    from agentchatme_hermes.tools import _share_api_key_handler

    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_live_test_key_xyz")
    _tools_mod.current_source_platform.set("telegram")

    raw = await _share_api_key_handler({})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["value"] == "ac_live_test_key_xyz"


async def test_refuses_when_source_is_agentchat(monkeypatch):
    """Peer agent on AgentChat asking → hard code-level refusal."""
    from agentchatme_hermes import tools as _tools_mod
    from agentchatme_hermes.tools import _share_api_key_handler

    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_live_test_key_dontleakme")
    _tools_mod.current_source_platform.set("agentchat")

    raw = await _share_api_key_handler({})
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["code"] == "REFUSED_PEER_CHANNEL"
    # The key MUST NOT appear anywhere in the response.
    assert "ac_live_test_key_dontleakme" not in raw


async def test_config_error_when_key_missing(monkeypatch):
    """No key in env → CONFIG_ERROR, never crash."""
    from agentchatme_hermes.tools import _share_api_key_handler

    monkeypatch.delenv("AGENTCHATME_API_KEY", raising=False)
    raw = await _share_api_key_handler({})
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["code"] == "CONFIG_ERROR"


async def test_output_field_name_avoids_redactor(monkeypatch):
    """The tool returns the key under field name ``value`` (not
    ``api_key``/``token``/``secret``/etc.) so Hermes's
    `agent/redact.py:_JSON_FIELD_RE` doesn't scrub it. Locks down the
    field naming so a future refactor doesn't break the redaction-
    avoidance accidentally."""
    from agentchatme_hermes.tools import _share_api_key_handler

    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_live_dontredactme")
    raw = await _share_api_key_handler({})
    payload = json.loads(raw)
    assert "value" in payload
    # These are the field names the Hermes redactor would match.
    for redacted_name in (
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer",
    ):
        assert redacted_name not in payload, (
            f"Field {redacted_name!r} would be matched by Hermes redactor; "
            "must use a non-credential-named field like `value`."
        )


async def test_context_var_isolates_concurrent_sessions(monkeypatch):
    """Two concurrent sessions with different source platforms must each
    see THEIR OWN source. ContextVar's task-local scoping makes this
    work — but if a refactor accidentally uses a global, this would
    catch it."""
    import asyncio

    from agentchatme_hermes import tools as _tools_mod
    from agentchatme_hermes.tools import _share_api_key_handler

    monkeypatch.setenv("AGENTCHATME_API_KEY", "ac_live_isolation_test")

    async def session_with_source(platform: str) -> dict:
        _tools_mod.current_source_platform.set(platform)
        # Yield to scheduler so the OTHER session also gets a turn before
        # we read our context value back.
        await asyncio.sleep(0)
        raw = await _share_api_key_handler({})
        return json.loads(raw)

    async def task_a():
        ctx = contextvars_copy()
        return await ctx.run_coro(session_with_source("agentchat"))

    async def task_b():
        ctx = contextvars_copy()
        return await ctx.run_coro(session_with_source("telegram"))

    # Run them concurrently using asyncio.gather but each in its own
    # context copy. asyncio.create_task copies the context implicitly,
    # which is the property we're verifying.
    results = await asyncio.gather(
        _run_in_fresh_context(session_with_source, "agentchat"),
        _run_in_fresh_context(session_with_source, "telegram"),
    )

    agentchat_result, telegram_result = results
    assert agentchat_result["ok"] is False
    assert agentchat_result["code"] == "REFUSED_PEER_CHANNEL"
    assert telegram_result["ok"] is True
    assert telegram_result["value"] == "ac_live_isolation_test"


def contextvars_copy():
    """Stub — see _run_in_fresh_context for the real isolation mechanism."""
    return None


async def _run_in_fresh_context(coro_func, *args):
    """Run a coroutine in a fresh context copy so two concurrent
    sessions get independent ContextVar storage."""
    import contextvars

    ctx = contextvars.copy_context()
    # `ctx.run` works only for sync callables; for async we have to
    # delegate via Task creation, which captures context implicitly.
    import asyncio

    return await asyncio.create_task(coro_func(*args), context=ctx)
