"""Regression tests for the `_safe` tool wrapper's call signature.

Hermes's `tools/registry.py:dispatch` invokes every tool handler as
``handler(args, **kwargs)`` where ``kwargs`` carries dispatch-context
fields (``task_id``, possibly more in future Hermes versions). Our
``_safe(handler)`` wrapper must accept and silently drop these, or
the call raises ``TypeError`` before any user logic runs and the
agent's tool call returns ``[error]`` instantly.

We hit this in v0.1.5 when a real user ran the plugin: every
``agentchat_*`` call failed with the TypeError. v0.1.6 fixed the
wrapper signature to accept ``**_kwargs``. These tests pin that
behavior so a future refactor doesn't reintroduce the bug.
"""

from __future__ import annotations

import asyncio

import pytest

from agentchatme_hermes.tools import _safe


def test_safe_wrapper_accepts_arbitrary_kwargs() -> None:
    """`handler(args, **kwargs)` — kwargs must be ignored, not raised on."""
    captured: list[dict] = []

    async def fake_handler(args: dict) -> dict:
        captured.append(args)
        return {"echo": args.get("text", "")}

    wrapped = _safe(fake_handler)

    # Hermes passes task_id and may pass other future fields.
    # Call patterns we MUST support:
    result = asyncio.run(
        wrapped({"text": "hello"}, task_id="abc-123", trace_id="zyx", agent_id="agt_foo")
    )

    assert result["ok"] is True
    assert result["result"] == {"echo": "hello"}
    assert captured == [{"text": "hello"}]


def test_safe_wrapper_accepts_no_kwargs() -> None:
    """The handler must still work when invoked the old way (args-only)."""

    async def fake_handler(args: dict) -> dict:
        return {"args_were": args}

    wrapped = _safe(fake_handler)
    result = asyncio.run(wrapped({"key": "value"}))

    assert result["ok"] is True
    assert result["result"]["args_were"] == {"key": "value"}


def test_safe_wrapper_handles_none_args_with_kwargs() -> None:
    """Hermes occasionally calls with `args=None` (zero-arg tools)."""

    async def fake_handler(args: dict) -> dict:
        return {"got": args}

    wrapped = _safe(fake_handler)
    # The wrapper coalesces None → {}. Add kwargs to verify both paths
    # interact cleanly.
    result = asyncio.run(wrapped(None, task_id="abc"))

    assert result["ok"] is True
    assert result["result"]["got"] == {}


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
    result = asyncio.run(wrapped({}, **kwargs))
    assert result["ok"] is True
