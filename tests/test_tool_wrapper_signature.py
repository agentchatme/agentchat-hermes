"""Regression tests for the `_safe` tool wrapper's call signature + return type.

Two contracts our wrapper must satisfy, both verified here:

1. **Call signature** — Hermes's `tools/registry.py:dispatch` invokes every
   tool handler as ``handler(args, **kwargs)`` where ``kwargs`` carries
   dispatch-context fields (``task_id`` and possibly more in future
   versions). Our ``_safe(handler)`` wrapper must accept and silently
   drop them, or Python raises ``TypeError`` before any user logic runs.
   We hit this in v0.1.5; v0.1.6 added ``**_kwargs``.

2. **Return type** — must be a JSON-serialized ``str``, not a Python
   ``dict``. Hermes passes the handler's return value straight through
   to the LLM as the ``content`` field of the OpenAI tool message.
   Strict OpenAI-compat providers (DeepSeek, NVIDIA NIM, MiniMax) reject
   non-string ``content`` with HTTP 400 ``messages[n].content should be
   a string or a list``. We hit this in v0.1.6 when a real user wired
   the plugin to DeepSeek and asked the agent to message another
   agent; v0.1.7 added ``json.dumps(...)`` to every return path.

A future refactor that breaks either contract trips these tests.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentchatme_hermes.tools import _safe


def _call(wrapped, args, **kwargs):
    """Run an async wrapper and JSON-parse the string result."""
    result = asyncio.run(wrapped(args, **kwargs))
    assert isinstance(result, str), (
        f"_safe wrapper must return str (Hermes/OpenAI tool-message contract), "
        f"got {type(result).__name__}: {result!r}"
    )
    return json.loads(result)


# ─── Return type ───────────────────────────────────────────────────────────


def test_safe_wrapper_returns_json_string() -> None:
    """The most important contract — Hermes passes the return straight
    into the OpenAI tool-message's ``content`` field. Strict
    OpenAI-compat providers reject non-string content."""

    async def fake_handler(args: dict) -> dict:
        return {"hello": "world"}

    wrapped = _safe(fake_handler)
    result = asyncio.run(wrapped({}))
    assert isinstance(result, str)
    # Must be valid JSON
    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["result"] == {"hello": "world"}


def test_safe_wrapper_returns_string_on_error() -> None:
    """Error paths must also return a JSON string, not a dict."""

    async def fake_handler(args: dict) -> dict:
        raise RuntimeError("boom")

    wrapped = _safe(fake_handler)
    result = asyncio.run(wrapped({}))
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["ok"] is False
    assert parsed["code"] == "UNEXPECTED"
    assert "boom" in parsed["message"]


def test_safe_wrapper_serializes_non_ascii() -> None:
    """``ensure_ascii=False`` so handles with CJK / emoji don't bloat the
    payload to ``\\uXXXX`` escapes and waste model context budget."""

    async def fake_handler(args: dict) -> dict:
        return {"handle": "テスト", "emoji": "💬"}

    wrapped = _safe(fake_handler)
    result = asyncio.run(wrapped({}))
    # Literal UTF-8 characters in the JSON string, not Unicode escapes
    assert "テスト" in result
    assert "💬" in result


# ─── Call signature ────────────────────────────────────────────────────────


def test_safe_wrapper_accepts_arbitrary_kwargs() -> None:
    """``handler(args, **kwargs)`` — kwargs must be ignored, not raised on."""
    captured: list[dict] = []

    async def fake_handler(args: dict) -> dict:
        captured.append(args)
        return {"echo": args.get("text", "")}

    wrapped = _safe(fake_handler)
    parsed = _call(
        wrapped, {"text": "hello"}, task_id="abc-123", trace_id="zyx", agent_id="agt_foo"
    )

    assert parsed["ok"] is True
    assert parsed["result"] == {"echo": "hello"}
    assert captured == [{"text": "hello"}]


def test_safe_wrapper_accepts_no_kwargs() -> None:
    """The handler must still work when invoked the old way (args-only)."""

    async def fake_handler(args: dict) -> dict:
        return {"args_were": args}

    wrapped = _safe(fake_handler)
    parsed = _call(wrapped, {"key": "value"})

    assert parsed["ok"] is True
    assert parsed["result"]["args_were"] == {"key": "value"}


def test_safe_wrapper_handles_none_args_with_kwargs() -> None:
    """Hermes occasionally calls with ``args=None`` (zero-arg tools)."""

    async def fake_handler(args: dict) -> dict:
        return {"got": args}

    wrapped = _safe(fake_handler)
    parsed = _call(wrapped, None, task_id="abc")

    assert parsed["ok"] is True
    assert parsed["result"]["got"] == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"task_id": "t-1"},
        {"task_id": "t-2", "trace_id": "tr-1"},
        {"task_id": "t-3", "agent_id": "a-1", "session_id": "s-1"},
        {"random_future_field": "x"},
    ],
)
def test_safe_wrapper_kwargs_combinations(kwargs: dict) -> None:
    """Combinations of dispatch kwargs Hermes may pass — none should raise."""

    async def fake_handler(args: dict) -> dict:
        return {}

    wrapped = _safe(fake_handler)
    parsed = _call(wrapped, {}, **kwargs)
    assert parsed["ok"] is True


# ─── Hermes-style invocation simulation ───────────────────────────────────


def test_safe_wrapper_via_hermes_dispatch_shape() -> None:
    """Simulate the exact call Hermes's tools/registry.py:dispatch makes.

    Hermes does::

        if entry.is_async:
            return _run_async(entry.handler(args, **kwargs))
        return entry.handler(args, **kwargs)

    The return value is set as the ``content`` field of a tool message
    sent to the LLM. We replicate this shape end-to-end here so a
    refactor that drops back to dict returns trips immediately.
    """

    async def fake_handler(args: dict) -> dict:
        return {"value": 42}

    wrapped = _safe(fake_handler)
    # Exact Hermes invocation shape: positional args dict + kwargs.
    raw = asyncio.run(wrapped({"input": "x"}, task_id="dispatch-1"))
    # This is what becomes message["content"]. It MUST be a str.
    assert isinstance(raw, str)
    # It MUST be valid JSON (so the LLM can parse it back).
    parsed = json.loads(raw)
    assert parsed["ok"] is True
    assert parsed["result"]["value"] == 42
