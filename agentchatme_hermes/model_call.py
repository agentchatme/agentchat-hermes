"""How this runtime asks its own model: the ``call_model`` the SDK gate needs.

``agentchatme.gate.run`` builds the prompt from the server's tray and
parses the answer; what it needs from the host is one plain, tool-less
completion: ``call_model(prompt) -> str``. Here that is Hermes' host-owned
auxiliary LLM client (``agent.auxiliary_client.call_llm``) pinned to the
model and provider credentials the agent itself runs on, so the judgement
is the agent's own. No tools are attached — the gate prompt says so, and a
model that could call the hosted tools would act instead of answering.

Lazy import: the plugin must register cleanly where ``agent.auxiliary_client``
is not importable (pytest outside Hermes).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from collections.abc import Mapping

# Output ceiling for the verdict JSON: a few dozen tokens per card. The tray
# is capped at 50 cards server-side.
DEFAULT_MAX_TOKENS = 2048

CallModel = Callable[[str], str]
CallLlm = Callable[..., Any]


def build_call_model(
    *,
    main_runtime: Mapping[str, Any],
    timeout_s: float,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    call_llm: CallLlm | None = None,
) -> CallModel:
    """A ``call_model`` for :func:`agentchatme.gate.run`.

    ``main_runtime`` is what :meth:`.agent_invoker.AgentInvoker._resolve_model_and_runtime`
    produced (model + provider credentials). ``call_llm`` is injectable so
    tests never reach a provider; the default is Hermes' auxiliary client.
    """
    runtime = dict(main_runtime)

    def call_model(prompt: str) -> str:
        llm = call_llm if call_llm is not None else _hermes_call_llm()
        response = llm(
            task=None,
            provider=runtime.get("provider") or None,
            model=runtime.get("model") or None,
            base_url=runtime.get("base_url") or None,
            api_key=runtime.get("api_key") or None,
            main_runtime=runtime or None,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max_tokens,
            timeout=timeout_s,
        )
        return content_of(response)

    return call_model


def content_of(response: Any) -> str:
    """The text of an OpenAI-style completion response; ``""`` when absent."""
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return str(content) if content else ""


def _hermes_call_llm() -> CallLlm:
    from agent.auxiliary_client import call_llm

    return call_llm  # type: ignore[no-any-return]
