"""Tests for ``agentchatme_hermes.model_call`` — the gate's ``call_model`` is a
tool-less completion on the agent's own model."""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from types import SimpleNamespace
from typing import Any

import pytest

from agentchatme_hermes.model_call import (
    DEFAULT_MAX_TOKENS,
    GateCallCancelled,
    build_call_model,
    content_of,
)

_TOOL_KWARGS = {"tools", "tool_choice", "functions", "function_call", "tool_schemas", "parallel_tool_calls"}


def _response(text: str | None) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class TestBuildCallModel:
    def test_plain_completion_on_the_agents_model_with_no_tools(self) -> None:
        seen: dict[str, Any] = {}

        def fake_llm(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return _response('{"decisions": []}')

        main_runtime = {
            "model": "deepseek-v4-flash",
            "provider": "openrouter",
            "base_url": "https://llm.example.test/v1",
            "api_key": "sk-test",
            "api_mode": "chat_completions",
        }
        call_model = build_call_model(main_runtime=main_runtime, timeout_s=12.5, call_llm=fake_llm)

        assert call_model("THE PROMPT") == '{"decisions": []}'
        assert seen["messages"] == [{"role": "user", "content": "THE PROMPT"}]
        assert seen["model"] == "deepseek-v4-flash"
        assert seen["provider"] == "openrouter"
        assert seen["base_url"] == "https://llm.example.test/v1"
        assert seen["api_key"] == "sk-test"
        assert seen["main_runtime"] == main_runtime
        assert seen["task"] is None
        assert seen["temperature"] == 0.0
        assert seen["max_tokens"] == DEFAULT_MAX_TOKENS
        assert seen["timeout"] == 12.5
        assert not (_TOOL_KWARGS & set(seen)), "the gate call attaches no tools"

    def test_empty_runtime_passes_none_not_empty_strings(self) -> None:
        seen: dict[str, Any] = {}

        def fake_llm(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return _response("ok")

        build_call_model(main_runtime={}, timeout_s=1.0, call_llm=fake_llm)("p")
        assert seen["provider"] is None and seen["model"] is None
        assert seen["base_url"] is None and seen["api_key"] is None
        assert seen["main_runtime"] is None

    def test_max_tokens_override(self) -> None:
        seen: dict[str, Any] = {}

        def fake_llm(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return _response("ok")

        build_call_model(main_runtime={"model": "m"}, timeout_s=1.0, max_tokens=64, call_llm=fake_llm)("p")
        assert seen["max_tokens"] == 64

    def test_provider_errors_propagate_to_the_gate(self) -> None:
        def failing(**_kwargs: Any) -> Any:
            raise RuntimeError("provider down")

        call_model = build_call_model(main_runtime={"model": "m"}, timeout_s=1.0, call_llm=failing)
        with pytest.raises(RuntimeError, match="provider down"):
            call_model("p")

    def test_default_is_hermes_auxiliary_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def call_llm(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return _response("from hermes")

        agent_pkg = types.ModuleType("agent")
        aux = types.ModuleType("agent.auxiliary_client")
        aux.call_llm = call_llm  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agent", agent_pkg)
        monkeypatch.setitem(sys.modules, "agent.auxiliary_client", aux)

        assert build_call_model(main_runtime={"model": "m"}, timeout_s=3.0)("p") == "from hermes"
        assert seen["model"] == "m" and seen["timeout"] == 3.0
        assert not (_TOOL_KWARGS & set(seen))


class TestContentOf:
    def test_reads_the_first_choice(self) -> None:
        assert content_of(_response("text")) == "text"

    @pytest.mark.parametrize(
        "response",
        [None, SimpleNamespace(choices=[]), _response(None), _response(""), object()],
    )
    def test_odd_shapes_are_empty(self, response: Any) -> None:
        assert content_of(response) == ""


class TestHostLlm:
    """Hermes' public plugin LLM (``ctx.llm``) makes the gate call when the
    host has it: no override (the profile's own model), no tools."""

    def test_complete_on_the_hosts_llm(self) -> None:
        seen: dict[str, Any] = {}

        class _PluginLlm:
            def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                seen["messages"] = messages
                seen.update(kwargs)
                return SimpleNamespace(text='{"decisions": []}')

        def never(**_kw: Any) -> Any:
            raise AssertionError("the auxiliary client is not used when ctx.llm is there")

        call_model = build_call_model(main_runtime={"model": "ignored"}, timeout_s=9.0, call_llm=never, host_llm=_PluginLlm())
        assert call_model("THE PROMPT") == '{"decisions": []}'
        assert seen["messages"] == [{"role": "user", "content": "THE PROMPT"}]
        assert seen["temperature"] == 0.0 and seen["timeout"] == 9.0
        assert seen["max_tokens"] == DEFAULT_MAX_TOKENS and seen["purpose"] == "agentchat gate call"
        assert not ({"provider", "model", "agent_id", "profile"} & set(seen)), "no override: the profile's own model"
        assert not (_TOOL_KWARGS & set(seen))

    def test_empty_text_is_empty(self) -> None:
        class _PluginLlm:
            def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                return SimpleNamespace(text=None)

        assert build_call_model(main_runtime={}, timeout_s=1.0, host_llm=_PluginLlm())("p") == ""


class _AsyncPluginLlm:
    """``ctx.llm`` with Hermes' async sibling: ``acomplete`` can be cancelled."""

    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.calls: list[str] = []
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append("complete")
        return SimpleNamespace(text="sync")

    async def acomplete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append("acomplete")
        assert kwargs["purpose"] == "agentchat gate call" and kwargs["temperature"] == 0.0
        self.started.set()
        try:
            if self.hang:
                await asyncio.sleep(60)
            return SimpleNamespace(text="async")
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _in_a_worker(fn: Any) -> Any:
    """Run ``fn`` on a worker thread, as the receive loop runs ``call_model``
    (its private event loop is that thread's, never the main thread's)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10.0)
    assert not thread.is_alive()
    if "error" in box:
        raise box["error"]
    return box["value"]


class TestCancel:
    """The receive loop's ``cancel`` handle: Hermes' async ``ctx.llm`` call is
    cancelled when it is set; the paths that cannot be interrupted ignore it."""

    def test_with_a_cancel_handle_the_async_call_is_used(self) -> None:
        llm = _AsyncPluginLlm()
        call_model = build_call_model(main_runtime={}, timeout_s=5.0, host_llm=llm)
        assert _in_a_worker(lambda: call_model("p", cancel=threading.Event())) == "async"
        assert llm.calls == ["acomplete"]

    def test_without_one_the_sync_call_is_used(self) -> None:
        llm = _AsyncPluginLlm()
        assert build_call_model(main_runtime={}, timeout_s=5.0, host_llm=llm)("p") == "sync"
        assert llm.calls == ["complete"]

    def test_setting_it_cancels_the_request(self) -> None:
        llm = _AsyncPluginLlm(hang=True)
        cancel = threading.Event()
        call_model = build_call_model(main_runtime={}, timeout_s=5.0, host_llm=llm)
        threading.Thread(target=lambda: llm.started.wait(5.0) and cancel.set(), daemon=True).start()
        started = time.monotonic()
        with pytest.raises(GateCallCancelled):
            _in_a_worker(lambda: call_model("p", cancel=cancel))
        assert time.monotonic() - started < 5.0
        assert llm.cancelled.is_set(), "the Hermes request itself was cancelled"

    def test_already_set_makes_no_call(self) -> None:
        llm = _AsyncPluginLlm()
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(GateCallCancelled):
            build_call_model(main_runtime={}, timeout_s=5.0, host_llm=llm)("p", cancel=cancel)
        assert llm.calls == []

    def test_a_sync_only_host_ignores_it(self) -> None:
        class _PluginLlm:
            def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
                return SimpleNamespace(text="sync")

        assert build_call_model(main_runtime={}, timeout_s=5.0, host_llm=_PluginLlm())("p", cancel=threading.Event()) == "sync"

    def test_the_fallback_ignores_it(self) -> None:
        call_model = build_call_model(main_runtime={"model": "m"}, timeout_s=5.0, call_llm=lambda **kw: _response("aux"))
        assert call_model("p", cancel=threading.Event()) == "aux"


def test_host_llm_accessor_is_static_and_lazy() -> None:
    from agentchatme_hermes._register import host_llm_accessor

    reads: list[str] = []

    class _Ctx:
        @property
        def llm(self) -> Any:
            reads.append("llm")
            return "facade"

    accessor = host_llm_accessor(_Ctx())
    assert accessor is not None and reads == [], "the property is not read at register time"
    assert accessor() == "facade" and reads == ["llm"]
    assert host_llm_accessor(SimpleNamespace()) is None, "an older Hermes: no ctx.llm"
