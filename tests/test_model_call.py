"""Tests for ``agentchatme_hermes.model_call`` — the gate's ``call_model`` is a
tool-less completion on the agent's own model."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from agentchatme_hermes.model_call import DEFAULT_MAX_TOKENS, build_call_model, content_of

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
