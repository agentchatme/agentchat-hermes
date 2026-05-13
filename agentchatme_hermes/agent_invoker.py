"""Mechanism A — the only path from a WS poke to an agent turn.

Why it exists
-------------
A platform/channel adapter would force a mandatory reply via
``send()`` and create infinite loops between two agents. We bypass
that machinery entirely. Per inbound:

1. Construct an :class:`AIAgent` configured with our session_id
   namespace (``agentchat:<conversation_id>``). The agent loads
   prior turns of THIS conversation from the session DB
   automatically.
2. Hand it a short notification prompt
   (``prompts.build_notification_prompt``).
3. Call :meth:`AIAgent.run_conversation`.
4. **Discard the return value.** No auto-routing anywhere. The
   agent's only way to actually send something on AgentChat is to
   call the ``agentchat_send_message`` tool during the turn.

This mirrors the pattern Hermes' cron scheduler uses
(``cron/scheduler.py:1425-1492``): build a transient agent, run a
turn, ignore the response. Cron has done it in production for many
versions — we are not inventing a primitive.

Concurrency
-----------
* A single dispatcher thread reads from the
  :class:`MessageQueue` and submits tasks to a thread pool.
* The pool's ``max_workers`` is ``config.max_inflight_turns`` —
  backpressure against a thundering-herd group blast.
* A per-conversation :class:`threading.Lock` serializes turns for
  one conversation. Different conversations run in parallel.

Lifecycle
---------
* :meth:`start` spins up the dispatcher; :meth:`on_new_event` is the
  callback the WS daemon fires when something landed.
* :meth:`stop` signals shutdown, drains the pool, joins the
  dispatcher.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .message_queue import MessageQueue
    from .types import AgentIdentity, InboundEvent

logger = logging.getLogger(__name__)

_DISPATCHER_THREAD_NAME = "agentchat-invoker"
_DRAIN_GRACE_SECONDS = 30.0

# Hermes' SessionDB is process-wide. We construct it once and pass
# the same instance into every transient AIAgent so a multi-turn
# conversation accumulates history under one session row.
_FALLBACK_MODEL = "claude-sonnet-4-6"


class AgentInvoker:
    """Drives Mechanism A.

    Owned by the :class:`Runtime`. Single-use — call :meth:`start`
    once and :meth:`stop` once.
    """

    def __init__(
        self,
        *,
        config: Config,
        identity: AgentIdentity,
        queue: MessageQueue,
    ) -> None:
        self._config = config
        self._identity = identity
        self._queue = queue

        self._executor: ThreadPoolExecutor | None = None
        self._dispatcher: threading.Thread | None = None
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._conv_locks_guard = threading.Lock()
        self._conv_locks: dict[str, threading.Lock] = {}

        # Hermes-side handles, resolved lazily on first dispatch so the
        # plugin can register cleanly even in environments where
        # Hermes' runtime isn't fully importable yet (e.g., during
        # pytest collection from outside Hermes).
        self._session_db: Any = None
        self._resolved_hermes = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._dispatcher is not None:
            return

        self._executor = ThreadPoolExecutor(
            max_workers=self._config.max_inflight_turns,
            thread_name_prefix="agentchat-turn",
        )
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name=_DISPATCHER_THREAD_NAME,
            daemon=True,
        )
        self._dispatcher.start()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._wake.set()

        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher.is_alive():
            dispatcher.join(timeout=2.0)
            if dispatcher.is_alive():
                logger.warning("AgentInvoker: dispatcher did not exit cleanly")

        executor = self._executor
        if executor is not None:
            # wait=True so in-flight turns get a chance to finish
            # before the runtime exits. SDK calls inside the tool
            # handlers are short, agent loops can be longer — the
            # grace is bounded by ``stop`` callers' patience.
            executor.shutdown(wait=True, cancel_futures=False)

    def on_new_event(self) -> None:
        """Signal-only — called by the WS daemon when something landed."""
        self._wake.set()

    # -- dispatcher ---------------------------------------------------------

    def _dispatch_loop(self) -> None:
        while not self._stopped.is_set():
            self._wake.wait(timeout=60.0)
            self._wake.clear()
            if self._stopped.is_set():
                return
            self._drain_queue()

    def _drain_queue(self) -> None:
        executor = self._executor
        if executor is None:
            return

        while True:
            event = self._queue.pop()
            if event is None:
                return

            conv_lock = self._lock_for(event.conversation_id)
            executor.submit(self._run_one, event, conv_lock)

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._conv_locks_guard:
            lock = self._conv_locks.get(conversation_id)
            if lock is None:
                lock = threading.Lock()
                self._conv_locks[conversation_id] = lock
            return lock

    # -- one turn -----------------------------------------------------------

    def _run_one(self, event: InboundEvent, conv_lock: threading.Lock) -> None:
        """Execute one agent turn for one inbound event.

        Runs on a worker thread from the pool. Each thread holds the
        per-conversation lock for the duration of the turn so a
        second event on the same conversation queues behind it
        instead of racing.
        """
        from .prompts import build_notification_prompt

        with conv_lock:
            if self._stopped.is_set():
                return

            self._ensure_hermes_resolved()

            agent = self._build_agent(event.conversation_id)
            if agent is None:
                return  # already logged inside _build_agent

            prompt = build_notification_prompt(event)

            try:
                result = agent.run_conversation(prompt)
            except Exception:
                logger.exception(
                    "AgentInvoker: run_conversation raised for conv=%s msg=%s",
                    event.conversation_id,
                    event.message_id,
                )
                return

            # We deliberately do NOT route the agent's final_response
            # anywhere. Any actual outbound went through the
            # agentchat_send_message tool during the turn. The text
            # here is the model's post-tool reasoning — useful for
            # operator visibility, not for transmission.
            final = (
                result.get("final_response") if isinstance(result, dict) else None
            )
            logger.info(
                "AgentInvoker: turn complete conv=%s msg=%s final_chars=%d",
                event.conversation_id,
                event.message_id,
                len(final) if isinstance(final, str) else 0,
            )

    # -- AIAgent construction (lazy Hermes import) --------------------------

    def _ensure_hermes_resolved(self) -> None:
        if self._resolved_hermes:
            return
        self._resolved_hermes = True
        try:
            from hermes_state import SessionDB

            self._session_db = SessionDB()
        except Exception as exc:
            logger.warning(
                "AgentInvoker: Hermes SessionDB unavailable — sessions will "
                "not persist across turns: %s",
                exc,
            )

    def _build_agent(self, conversation_id: str) -> Any:
        """Construct a transient AIAgent for one turn.

        Mirrors the cron scheduler's pattern at
        ``cron/scheduler.py:1425-1492``: fresh AIAgent per call,
        Hermes' default model + provider, session_id namespaced under
        ``agentchat:``. ``platform="agentchat"`` so trajectories /
        logs / metrics can be filtered for our traffic.

        Returns ``None`` if Hermes' runtime cannot be imported (the
        plugin running outside a Hermes process — defensive, should
        not happen in production).
        """
        try:
            from run_agent import AIAgent
        except ImportError as exc:
            logger.error(
                "AgentInvoker: cannot import run_agent.AIAgent — "
                "is the plugin running inside Hermes? (%s)",
                exc,
            )
            return None

        model, runtime_kwargs = self._resolve_model_and_runtime()

        return AIAgent(
            model=model,
            session_id=f"agentchat:{conversation_id}",
            session_db=self._session_db,
            platform="agentchat",
            quiet_mode=True,
            load_soul_identity=True,
            skip_context_files=True,
            **runtime_kwargs,
        )

    def _resolve_model_and_runtime(self) -> tuple[str, dict[str, Any]]:
        """Pull the user's default model + provider creds from Hermes config."""
        try:
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider
        except ImportError:
            logger.warning(
                "AgentInvoker: Hermes config helpers unavailable, falling "
                "back to %s with no provider creds",
                _FALLBACK_MODEL,
            )
            return _FALLBACK_MODEL, {}

        try:
            cfg = load_config()
            model = cfg.get("model") or _FALLBACK_MODEL
            runtime = resolve_runtime_provider(target_model=model)
        except Exception:
            logger.exception(
                "AgentInvoker: resolve_runtime_provider failed, falling "
                "back to model=%s",
                _FALLBACK_MODEL,
            )
            return _FALLBACK_MODEL, {}

        runtime_kwargs: dict[str, Any] = {}
        for key in ("api_key", "base_url", "provider", "api_mode"):
            value = runtime.get(key)
            if value is not None:
                runtime_kwargs[key] = value
        return model, runtime_kwargs
