"""Process-wide runtime coordinator.

One :class:`Runtime` per Hermes process. Owns:

* The resolved :class:`AgentIdentity` (handle + agent_id, loaded
  once via ``GET /v1/agents/me``).
* The :class:`MessageQueue` between WS daemon and invoker.
* The :class:`WSDaemon` background thread (inbound).
* The :class:`AgentInvoker` thread pool (Mechanism A turns).
* A sync :class:`AgentChatClient` shared by all tool handlers for
  HTTP API calls (outbound + every other ``/v1/*`` op).

Accessed via :func:`get_runtime` (constructs if needed) and
:func:`get_existing_runtime` (returns the singleton or ``None``).
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from .agent_invoker import AgentInvoker
from .message_queue import MessageQueue
from .types import AgentIdentity
from .ws_daemon import WSDaemon

if TYPE_CHECKING:
    from agentchatme import AgentChatClient

    from .config import Config

logger = logging.getLogger(__name__)


class Runtime:
    """Coordinator for the plugin's stateful subsystems."""

    def __init__(self, config: Config) -> None:
        self._config = config

        self._started = False
        self._stopped = False
        self._lock = threading.Lock()

        self._identity: AgentIdentity | None = None
        self._client: AgentChatClient | None = None
        self._queue: MessageQueue | None = None
        self._ws_daemon: WSDaemon | None = None
        self._invoker: AgentInvoker | None = None

    # -- accessors ----------------------------------------------------------

    @property
    def config(self) -> Config:
        return self._config

    @property
    def identity(self) -> AgentIdentity:
        if self._identity is None:
            raise RuntimeError("Runtime.start() has not completed")
        return self._identity

    @property
    def client(self) -> AgentChatClient:
        """Shared sync HTTP client for tool handlers.

        Tool handlers are called on Hermes' agent thread (synchronous)
        and need a sync client. The WS daemon uses a separate async
        client.
        """
        if self._client is None:
            raise RuntimeError("Runtime.start() has not completed")
        return self._client

    @property
    def queue(self) -> MessageQueue:
        if self._queue is None:
            raise RuntimeError("Runtime.start() has not completed")
        return self._queue

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True

            try:
                self._client = self._build_sync_client()
                self._identity = self._resolve_identity(self._client)
                self._queue = MessageQueue()
                self._invoker = AgentInvoker(
                    config=self._config,
                    identity=self._identity,
                    queue=self._queue,
                )
                self._ws_daemon = WSDaemon(
                    config=self._config,
                    identity=self._identity,
                    queue=self._queue,
                    on_new_event=self._invoker.on_new_event,
                )

                self._invoker.start()
                self._ws_daemon.start()

                logger.info(
                    "agentchat runtime started: handle=@%s id=%s ws=%s",
                    self._identity.handle,
                    self._identity.agent_id,
                    self._config.ws_url,
                )
            except Exception:
                logger.exception("agentchat runtime: start failed")
                # Best-effort cleanup so subsequent get_runtime() calls
                # don't see a half-initialized object.
                self._teardown_partial()
                self._started = False
                raise

    def stop(self) -> None:
        with self._lock:
            if not self._started or self._stopped:
                return
            self._stopped = True

        # Order matters: stop inbound first (WS daemon) so no new
        # events land in the queue, then drain the invoker, then close
        # the HTTP client.
        if self._ws_daemon is not None:
            try:
                self._ws_daemon.stop()
            except Exception:
                logger.exception("agentchat runtime: ws_daemon stop raised")

        if self._invoker is not None:
            try:
                self._invoker.stop()
            except Exception:
                logger.exception("agentchat runtime: invoker stop raised")

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.debug("agentchat runtime: client close raised", exc_info=True)

        logger.info("agentchat runtime stopped")

    # -- internals ----------------------------------------------------------

    def _build_sync_client(self) -> AgentChatClient:
        from agentchatme import AgentChatClient

        return AgentChatClient(
            api_key=self._config.api_key,
            base_url=self._config.api_base,
        )

    def _resolve_identity(self, client: AgentChatClient) -> AgentIdentity:
        """Look up our own handle via ``GET /v1/agents/me``.

        Surfaces auth errors immediately so a bad API key fails fast
        at plugin start rather than silently producing an unauthenticated
        WS that gets closed by the server.
        """
        me = client.get_me()
        # SDK returns a plain dict. Pull id+handle defensively so a
        # forward-compat field rename in the SDK doesn't crash us
        # silently — we surface a clear RuntimeError instead.
        agent_id = me.get("id") if isinstance(me, dict) else None
        handle_raw = me.get("handle") if isinstance(me, dict) else None

        if not isinstance(agent_id, str) or not isinstance(handle_raw, str):
            raise RuntimeError(
                "agentchat runtime: /v1/agents/me response missing id or handle"
            )

        return AgentIdentity(agent_id=agent_id, handle=handle_raw.lstrip("@").lower())

    def _teardown_partial(self) -> None:
        """Best-effort cleanup after a failed :meth:`start`.

        Each subsystem is checked for None — a failure during
        identity resolution leaves the daemons and invoker
        un-constructed.
        """
        if self._ws_daemon is not None:
            try:
                self._ws_daemon.stop()
            except Exception:
                logger.debug(
                    "agentchat runtime: partial teardown ws_daemon",
                    exc_info=True,
                )
        if self._invoker is not None:
            try:
                self._invoker.stop()
            except Exception:
                logger.debug(
                    "agentchat runtime: partial teardown invoker",
                    exc_info=True,
                )
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.debug(
                    "agentchat runtime: partial teardown client",
                    exc_info=True,
                )

        self._identity = None
        self._client = None
        self._queue = None
        self._ws_daemon = None
        self._invoker = None


# Module-level singleton. Hermes calls register(ctx) once per process,
# and that's where get_runtime is first invoked.
_singleton: Runtime | None = None
_singleton_lock = threading.Lock()


def get_runtime(config: Config) -> Runtime:
    """Return the process-wide runtime, constructing if needed.

    Idempotent within a process — the same Config typically resolves
    to the same Runtime. Construction is lock-guarded so concurrent
    register() calls (which shouldn't happen, but defensive) don't
    spawn two daemons.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Runtime(config)
        return _singleton


def get_existing_runtime() -> Runtime | None:
    """Return the singleton if it exists, else None.

    Used by lifecycle hooks (on_session_end) that need to stop the
    runtime without re-constructing it.
    """
    with _singleton_lock:
        return _singleton


def reset_for_tests() -> None:
    """Drop the singleton — test-only.

    Production code paths never call this. Tests use it to isolate
    runtime state between test functions.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            try:
                _singleton.stop()
            except Exception:
                logger.debug("reset_for_tests: stop raised", exc_info=True)
        _singleton = None
