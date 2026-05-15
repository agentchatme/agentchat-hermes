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

# How many prior messages to rehydrate as conversation_history on
# each wake. 30 is empirically enough for a typical back-and-forth
# to make sense without bloating the prompt for long conversations.
# Power users with chatty groups can scroll further via the
# agentchat_get_conversation_messages tool.
_HISTORY_FETCH_LIMIT = 30


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
        """Signal-only — called by the WS daemon when something landed.

        Logs at DEBUG (not INFO) because this fires on every frame
        and the ws_daemon already logs the receipt. INFO would
        duplicate signal without adding information.
        """
        logger.debug("AgentInvoker: wake signal received")
        self._wake.set()

    # -- dispatcher ---------------------------------------------------------

    def _dispatch_loop(self) -> None:
        logger.info("AgentInvoker: dispatcher loop started")
        try:
            while not self._stopped.is_set():
                self._wake.wait(timeout=60.0)
                if self._stopped.is_set():
                    return
                # Clear AFTER the stopped check so a stop()+wake set
                # in the same instant doesn't get swallowed below.
                woke_by_event = self._wake.is_set()
                self._wake.clear()
                if woke_by_event:
                    logger.debug("AgentInvoker: dispatcher woke (event-driven)")
                self._drain_queue()
        except Exception:
            # The dispatcher thread is the only path from WS to agent
            # — losing it silently is exactly the "gateway runtime
            # appears dead" failure mode the first spike hit. Re-raise
            # here is not useful (thread is daemonic, no one's
            # joining), so log loudly instead. A future enhancement
            # could restart the loop from a supervisor, but the
            # diagnostic comes first.
            logger.exception(
                "AgentInvoker: dispatcher loop crashed — no more turns "
                "will be triggered until the gateway restarts. Check "
                "the error and restart `hermes gateway`."
            )
        finally:
            logger.info("AgentInvoker: dispatcher loop exiting")

    def _drain_queue(self) -> None:
        executor = self._executor
        if executor is None:
            return

        drained = 0
        while True:
            event = self._queue.pop()
            if event is None:
                if drained:
                    logger.info(
                        "AgentInvoker: drained %d event(s) from queue",
                        drained,
                    )
                return

            drained += 1
            logger.info(
                "AgentInvoker: dispatching turn conv=%s msg=%s from=@%s",
                event.conversation_id,
                event.message_id,
                event.sender_handle,
            )
            conv_lock = self._lock_for(event.conversation_id)
            try:
                future = executor.submit(self._run_one, event, conv_lock)
            except RuntimeError:
                # ThreadPoolExecutor refuses submissions after shutdown.
                # Happens during stop() if a queued event was still
                # waiting — not a bug, but worth a debug line.
                logger.debug(
                    "AgentInvoker: executor refused submission "
                    "(shutdown in progress?) for conv=%s msg=%s",
                    event.conversation_id,
                    event.message_id,
                )
                return
            # Future done-callback: belt-and-suspenders catch for
            # any exception that escaped `_run_one`'s own outer
            # try/except. Without this, a silent crash inside an
            # exception handler itself (rare, but possible) would
            # vanish into ThreadPoolExecutor's future without ever
            # being logged. We learned this the hard way after the
            # 0.2.1 inbound silently failed at the AIAgent build
            # stage and produced no log line, no traceback, no
            # visible error.
            # Bind ids as default args so the callback captures THIS
            # event's ids, not the loop-final values (B023 late-binding).
            # Pulling the ids into locals first lets mypy narrow them
            # away from ``InboundEvent | None``.
            ev_conv = event.conversation_id
            ev_msg = event.message_id

            def _callback(
                f: Any,
                _conv: str = ev_conv,
                _msg: str = ev_msg,
            ) -> None:
                self._log_future_outcome(f, _conv, _msg)

            future.add_done_callback(_callback)

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

        Wraps the entire body in an outer ``try/except Exception`` —
        ``ThreadPoolExecutor.submit()`` silently captures unhandled
        exceptions into the future, and we don't ``.result()`` (can't
        — it'd block the dispatcher). Without this outer handler an
        exception in :meth:`_build_agent`, :meth:`_build_conversation_history`,
        or :meth:`_ensure_hermes_resolved` vanishes into the void.
        The 0.2.1 production hang was exactly this: ``AIAgent.__init__``
        raised AFTER our "turn start" log but BEFORE the inner
        ``run_conversation`` try-block; the exception escaped silently
        and no operator-visible signal existed.
        """
        from .prompts import build_notification_prompt

        # Record contention so a slow conversation is observable.
        # ``acquire(blocking=False)`` first to distinguish uncontested
        # vs queued-behind-prior-turn — the latter is normal but
        # worth marking when chasing latency regressions.
        if not conv_lock.acquire(blocking=False):
            logger.info(
                "AgentInvoker: turn queued behind prior turn for "
                "conv=%s msg=%s",
                event.conversation_id,
                event.message_id,
            )
            conv_lock.acquire()
        try:
            try:
                self._run_one_inner(event, build_notification_prompt)
            except Exception:
                # Outer catch — covers _build_agent, history fetch,
                # Hermes imports, anything else above the inner
                # run_conversation try/except. Logs with traceback so
                # the next time this fails we see exactly where.
                logger.exception(
                    "AgentInvoker: turn FAILED with unhandled exception "
                    "(outer catch) conv=%s msg=%s — the agent did not "
                    "produce a reply. See traceback for the failing call.",
                    event.conversation_id,
                    event.message_id,
                )
        finally:
            conv_lock.release()

    def _run_one_inner(
        self,
        event: InboundEvent,
        build_notification_prompt: Any,
    ) -> None:
        """Inner turn body, separated so the outer wrapper can catch."""
        if self._stopped.is_set():
            logger.debug(
                "AgentInvoker: skipping turn — runtime stopping (conv=%s)",
                event.conversation_id,
            )
            return

        logger.info(
            "AgentInvoker: turn start conv=%s msg=%s from=@%s kind=%s",
            event.conversation_id,
            event.message_id,
            event.sender_handle,
            event.conversation_kind,
        )

        self._ensure_hermes_resolved()

        agent = self._build_agent(event.conversation_id)
        if agent is None:
            logger.warning(
                "AgentInvoker: agent construction returned None for "
                "conv=%s msg=%s — turn dropped",
                event.conversation_id,
                event.message_id,
            )
            return  # already logged inside _build_agent

        prompt = build_notification_prompt(event)
        history = self._build_conversation_history(
            conversation_id=event.conversation_id,
            conversation_kind=event.conversation_kind,
            trigger_message_id=event.message_id,
        )

        try:
            result = agent.run_conversation(
                prompt, conversation_history=history
            )
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
            "AgentInvoker: turn complete conv=%s msg=%s history_len=%d final_chars=%d",
            event.conversation_id,
            event.message_id,
            len(history),
            len(final) if isinstance(final, str) else 0,
        )

    def _log_future_outcome(
        self, future: Any, conversation_id: str, message_id: str
    ) -> None:
        """Done-callback on the submitted future.

        Belt-and-suspenders: if ``_run_one``'s outer except itself
        crashed (e.g. logger blew up), the exception would still go
        into the future. This callback fires when the future
        finishes — failure or success — and surfaces any remaining
        hidden exception. The cost is one callback invocation per
        turn; the benefit is "never silently lose a turn failure
        again."
        """
        try:
            exc = future.exception()
        except Exception:
            # The future was cancelled, or .exception() itself raised.
            # Either way, nothing more we can do — log and move on.
            logger.exception(
                "AgentInvoker: future.exception() raised for conv=%s msg=%s",
                conversation_id,
                message_id,
            )
            return
        if exc is not None:
            logger.error(
                "AgentInvoker: turn FAILED (future-callback catch) "
                "conv=%s msg=%s exc=%r — this should already have been "
                "logged with traceback by the outer except in _run_one; "
                "if you only see this line, the outer handler itself "
                "failed.",
                conversation_id,
                message_id,
                exc,
            )

    # -- conversation-history rehydration -----------------------------------

    def _build_conversation_history(
        self,
        *,
        conversation_id: str,
        conversation_kind: str,
        trigger_message_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch and translate recent messages into Hermes turn shape.

        Mirrors ``gateway/run.py:15074-15113`` upstream — the agent
        wakes up with the full thread rehydrated as alternating
        ``user``/``assistant`` turns, just like every other Hermes
        channel. Without this, the agent acts on a single line of text
        with no prior context.

        Translation rules:

        * The triggering message (``trigger_message_id``) is excluded —
          it lands as the new ``user_message`` via the notification
          prompt, not duplicated inside history.
        * Each AgentChat message becomes one Hermes turn:
          ``is_own=True`` → ``role: assistant``, otherwise
          ``role: user``. For groups, non-self messages are prefixed
          with ``[@sender_handle] `` so the agent can tell speakers
          apart in a multi-party thread.
        * Non-text messages are skipped (Hermes' multi-modal pipeline
          would need adapter work to surface them properly; deferred).
        * On transport failure the history is empty — best-effort,
          logged, and the turn still runs (with the notification alone
          as the user-message). Better to wake the agent without
          context than to fail to wake it at all.
        """
        try:
            from agentchatme import AgentChatError
        except ImportError:
            # SDK is a hard dependency; this branch is theoretically
            # unreachable, but defensive in case a partial install
            # lands here before the runtime can fail-fast.
            logger.warning("AgentInvoker: agentchatme SDK not importable")
            return []

        try:
            result = self._runtime_client_get_messages(conversation_id)
        except AgentChatError as exc:
            logger.warning(
                "AgentInvoker: history fetch failed for conv=%s — running "
                "without prior context. (%s)",
                conversation_id,
                exc,
            )
            return []

        messages = _extract_messages_list(result)
        if not messages:
            return []

        return _translate_messages_to_history(
            messages,
            own_handle=self._identity.handle,
            conversation_kind=conversation_kind,
            trigger_message_id=trigger_message_id,
        )

    def _runtime_client_get_messages(self, conversation_id: str) -> Any:
        """Indirection layer so tests can stub the SDK fetch cleanly."""
        return self._runtime.client.get_messages(
            conversation_id, limit=_HISTORY_FETCH_LIMIT
        )

    @property
    def _runtime(self) -> Any:
        """Lazy accessor — the runtime singleton may not exist at construct time
        (we hold `_identity` separately, which is all the invoker truly needs).
        Resolved via the get_existing_runtime singleton at call time.
        """
        from .runtime import get_existing_runtime

        rt = get_existing_runtime()
        if rt is None:
            raise RuntimeError(
                "AgentInvoker: runtime singleton missing — invoker called "
                "before Runtime.start() completed"
            )
        return rt

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


# ─── pure helpers (extracted for unit-testability) ─────────────────────────


def _extract_messages_list(result: Any) -> list[dict[str, Any]]:
    """Pull the message list out of a ``get_messages`` response shape.

    The SDK returns ``dict[str, Any]`` with a ``messages`` key holding
    the list. Defensive against shape drift — anything we don't
    recognise yields an empty list rather than raising.
    """
    if isinstance(result, list):
        return [m for m in result if isinstance(m, dict)]
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, list):
            return [m for m in messages if isinstance(m, dict)]
    return []


def _translate_messages_to_history(
    messages: list[dict[str, Any]],
    *,
    own_handle: str,
    conversation_kind: str,
    trigger_message_id: str,
) -> list[dict[str, Any]]:
    """Translate AgentChat messages into Hermes ``{role, content}`` turns.

    Pure function — no SDK, no runtime, no IO. The translation rules:

    * ``is_own`` (server-precomputed) decides role; falls back to a
      sender-handle compare if the server didn't precompute it.
    * Group messages prefix non-self speakers with ``[@handle]`` so
      the agent can attribute lines in a multi-party thread. Direct
      conversations stay unprefixed — the alternation in the turn
      list already encodes who said what.
    * The trigger message is excluded — it lands as ``user_message``
      via the notification prompt, not duplicated in history.
    * Non-text messages are skipped (multi-modal handling is deferred).
    * Empty content is skipped.

    Output is ordered oldest-first, matching how Hermes' AIAgent
    expects ``conversation_history`` (it appends the new ``user``
    turn at the end).
    """
    own_handle_norm = own_handle.lstrip("@").lower()
    is_group = conversation_kind == "group"
    out: list[dict[str, Any]] = []

    for msg in messages:
        if msg.get("id") == trigger_message_id:
            continue
        if msg.get("type", "text") != "text":
            continue
        content = msg.get("content")
        if not isinstance(content, dict):
            continue
        text = content.get("text")
        if not isinstance(text, str) or not text:
            continue

        is_own = msg.get("is_own")
        if not isinstance(is_own, bool):
            sender = (
                msg.get("from") or msg.get("sender_handle") or ""
            )
            is_own = sender.lstrip("@").lower() == own_handle_norm

        if is_own:
            out.append({"role": "assistant", "content": text})
        elif is_group:
            sender = msg.get("from") or msg.get("sender_handle") or "?"
            sender = sender.lstrip("@")
            out.append({"role": "user", "content": f"[@{sender}] {text}"})
        else:
            out.append({"role": "user", "content": text})

    return out
