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
``ctx.llm.acomplete`` the call runs that on the invoker's :class:`GateLoop` —
one private event loop on its own thread for the runtime's lifetime, so
Hermes' async client and its connection are reused — and cancels it when the
event is set: the request itself ends. A provider adapter that implements
its async call as ``asyncio.to_thread`` over a sync one (Hermes' plugin-host
child for ``ctx.llm``, some auxiliary adapters) only stops being waited for:
its worker thread runs on until the provider's own timeout. The other paths
ignore ``cancel`` and end at their own ``timeout``.

Lazy import: the plugin must register cleanly where ``agent.auxiliary_client``
is not importable (pytest outside Hermes).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping

logger = logging.getLogger(__name__)

# Output ceiling for the verdict JSON: a few dozen tokens per card. The kit
# asks about at most 10 cards per call (its gate chunk; the tray read is
# capped at 50 cards server-side).
DEFAULT_MAX_TOKENS = 2048

# What Hermes' audit line for the call says it was for.
GATE_CALL_PURPOSE = "agentchat gate call"

# How often a cancellable call looks at its cancel event.
_CANCEL_POLL_S = 0.05

CallModel = Callable[..., str]
"""``call_model(prompt, *, cancel=None) -> str``."""
CallLlm = Callable[..., Any]


# How long closing the gate loop waits for its thread.
_CLOSE_WAIT_S = 2.0


class GateCallCancelled(Exception):
    """The receive loop stopped waiting for this answer; the request was cancelled."""


class GateLoop:
    """One private event loop on its own daemon thread for the gate's async
    model calls, started at the first call and kept until :meth:`close`.

    Hermes binds its cached async client to the loop it was made on, so one
    loop for the runtime's lifetime reuses that client and its connection.
    A call runs in the caller's ``contextvars`` (the profile's scope, which
    the invoker enters in the worker thread): ``run_coroutine_threadsafe``
    schedules it in a copy of the calling thread's context.
    """

    def __init__(self, name: str = "agentchat-gate-call") -> None:
        self._name = name
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def run(self, start: Callable[[], Awaitable[Any]], cancel: threading.Event) -> Any:
        """Run the coroutine ``start()`` makes on the loop and wait for it;
        when ``cancel`` is set first, cancel it and raise
        :class:`GateCallCancelled`."""
        if cancel.is_set():
            raise GateCallCancelled("the receive loop stopped waiting for the gate call")
        loop = self._ensure()

        async def call() -> Any:
            return await start()

        future = asyncio.run_coroutine_threadsafe(call(), loop)
        while True:
            try:
                return future.result(timeout=_CANCEL_POLL_S)
            except concurrent.futures.TimeoutError:
                if cancel.is_set():
                    future.cancel()  # cancels the task on the loop: the request ends
                    raise GateCallCancelled("the receive loop stopped waiting for the gate call") from None
                if self._closed:
                    raise GateCallCancelled("the gate-call loop is closed") from None
            except concurrent.futures.CancelledError:
                raise GateCallCancelled("the gate call was cancelled") from None

    def close(self) -> None:
        """Cancel what runs on the loop and stop its thread (bounded wait)."""
        with self._lock:
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            return  # already closed
        if thread is not threading.current_thread():
            thread.join(_CLOSE_WAIT_S)
            if thread.is_alive():
                logger.warning("agentchat gate call: its loop's thread did not stop within %.0fs", _CLOSE_WAIT_S)

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                raise GateCallCancelled("the gate-call loop is closed")
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=self._serve, args=(loop,), name=self._name, daemon=True)
            thread.start()
            self._loop, self._thread = loop, thread
            return loop

    @staticmethod
    def _serve(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                logger.debug("agentchat gate call: loop shutdown raised", exc_info=True)
            finally:
                asyncio.set_event_loop(None)
                loop.close()


def build_call_model(
    *,
    main_runtime: Mapping[str, Any],
    timeout_s: float,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    call_llm: CallLlm | None = None,
    host_llm: Any = None,
    gate_loop: GateLoop | None = None,
) -> CallModel:
    """A ``call_model`` for :func:`agentchatme.gate.run`.

    ``host_llm`` is Hermes' plugin LLM facade (``ctx.llm``); when given it
    makes the call and ``main_runtime`` is unused. Otherwise ``main_runtime``
    is what :meth:`.agent_invoker.AgentInvoker._resolve_model_and_runtime`
    produced (model + provider credentials) and ``call_llm`` — injectable so
    tests never reach a provider — defaults to Hermes' auxiliary client.
    ``gate_loop`` runs ``ctx.llm.acomplete`` when the caller passes a
    ``cancel`` handle; without one the sync ``complete`` is used.
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
            if cancel is not None and cancellable and gate_loop is not None:
                assert acomplete is not None
                result = gate_loop.run(lambda: acomplete(**request), cancel)
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
