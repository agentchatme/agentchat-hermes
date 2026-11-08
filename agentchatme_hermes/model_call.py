"""How this runtime asks its own model: the ``call_model`` the SDK gate needs.

``agentchatme.gate.run`` builds the prompt from the server's tray and
parses the answer; what it needs from the host is one plain, tool-less
completion: ``call_model(prompt) -> str``. No tools are attached — the gate
prompt says so, and a model that could call the hosted tools would act
instead of answering.

Two ways to make that call, in this order:

1. **Hermes' public plugin LLM** (``ctx.llm.complete``, Hermes' "host-owned
   completions on the user's active model"). No override is passed, so
   Hermes routes it to the profile's main model and credentials — or to a
   fallback the user declared, never to a provider they did not pick
   (``agent.auxiliary_client._discovery_chain_allowed``). The judgement is
   the agent's own, and this package reads no config and no credential.
2. Otherwise (a Hermes without ``ctx.llm``) Hermes' auxiliary client
   (``agent.auxiliary_client.call_llm``) pinned to the model and provider
   the invoker resolved from the profile's config.

Cancelling: the receive loop hands ``call_model`` a ``cancel`` event, set
when the answer is no longer wanted (the kit's gate-call deadline passed or
the cycle was abandoned). Hermes' sync ``ctx.llm.complete`` and ``call_llm``
cannot be interrupted, so with a ``cancel`` handle and an async
``ctx.llm.acomplete`` the call runs that on a private event loop in this
worker thread and cancels it when the event is set — the request itself
ends. The other paths ignore ``cancel`` and end at their own ``timeout``.

Lazy import: the plugin must register cleanly where ``agent.auxiliary_client``
is not importable (pytest outside Hermes).
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import threading
    from collections.abc import Awaitable, Mapping

# Output ceiling for the verdict JSON: a few dozen tokens per card. The tray
# is capped at 50 cards server-side.
DEFAULT_MAX_TOKENS = 2048

# What Hermes' audit line for the call says it was for.
GATE_CALL_PURPOSE = "agentchat gate call"

# How often a cancellable call looks at its cancel event.
_CANCEL_POLL_S = 0.05

CallModel = Callable[..., str]
"""``call_model(prompt, *, cancel=None) -> str``."""
CallLlm = Callable[..., Any]


class GateCallCancelled(Exception):
    """The receive loop stopped waiting for this answer; the request was cancelled."""


def build_call_model(
    *,
    main_runtime: Mapping[str, Any],
    timeout_s: float,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    call_llm: CallLlm | None = None,
    host_llm: Any = None,
) -> CallModel:
    """A ``call_model`` for :func:`agentchatme.gate.run`.

    ``host_llm`` is Hermes' plugin LLM facade (``ctx.llm``); when given it
    makes the call and ``main_runtime`` is unused. Otherwise ``main_runtime``
    is what :meth:`.agent_invoker.AgentInvoker._resolve_model_and_runtime`
    produced (model + provider credentials) and ``call_llm`` — injectable so
    tests never reach a provider — defaults to Hermes' auxiliary client.
    """
    runtime = dict(main_runtime)

    if host_llm is not None:
        acomplete = getattr(host_llm, "acomplete", None)
        cancellable = inspect.iscoroutinefunction(acomplete)

        def call_host(prompt: str, *, cancel: threading.Event | None = None) -> str:
            request: dict[str, Any] = {
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": max_tokens,
                "timeout": timeout_s,
                "purpose": GATE_CALL_PURPOSE,
            }
            if cancel is not None and cancellable:
                assert acomplete is not None
                result = until_cancelled(lambda: acomplete(**request), cancel)
            else:
                result = host_llm.complete(**request)
            text = getattr(result, "text", None)
            return str(text) if text else ""

        return call_host

    def call_model(prompt: str, *, cancel: threading.Event | None = None) -> str:
        # ``cancel`` is not used: Hermes' ``call_llm`` cannot be interrupted;
        # it ends at ``timeout``.
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


def until_cancelled(start: Callable[[], Awaitable[Any]], cancel: threading.Event) -> Any:
    """Run the coroutine ``start()`` makes on a private event loop in this
    (worker) thread; when ``cancel`` is set first, cancel it and raise
    :class:`GateCallCancelled`."""

    async def watch() -> Any:
        task = asyncio.ensure_future(start())
        while not cancel.is_set():
            done, _ = await asyncio.wait({task}, timeout=_CANCEL_POLL_S)
            if done:
                return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise GateCallCancelled("the receive loop stopped waiting for the gate call")

    if cancel.is_set():
        raise GateCallCancelled("the receive loop stopped waiting for the gate call")
    return asyncio.run(watch())


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
