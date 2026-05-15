"""Process-wide runtime coordinator.

One :class:`Runtime` per Hermes process. Owns:

* The resolved :class:`AgentIdentity` (handle only — internal id is
  server-side, we never extract it) loaded via ``GET /v1/agents/me``
  at start.
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
from .leader_lock import release_leader_lock, try_acquire_ws_leader_lock
from .message_queue import MessageQueue
from .types import AgentIdentity
from .ws_daemon import WSDaemon

if TYPE_CHECKING:
    from agentchatme import AgentChatClient

    from .config import Config

logger = logging.getLogger(__name__)


class Runtime:
    """Coordinator for the plugin's stateful subsystems.

    Has two operational modes, resolved at :meth:`start` time:

    * **Leader mode** — full runtime: sync HTTP client + identity
      resolution + WSDaemon background thread + AgentInvoker thread
      pool. Reached only when the constructor was called with
      ``gateway_mode=True`` AND this process successfully acquired
      the per-machine WS leader lock (see :mod:`.leader_lock`).
    * **Follower mode** — light runtime: sync HTTP client + identity
      resolution only. No WS connection opened, no invoker thread
      pool. Used by TUI/REPL, named CLI subcommands, one-shot agents,
      AND gateway-class processes that lost the leader-lock race.
      Tool surface remains available so the agent can still call
      ``agentchat_*`` tools; only the live inbound stream is
      leader-only.

    The two-gate design (env var + flock) eliminates every flavor
    of multi-WS-per-machine: the gateway env var blocks TUI/CLI/
    oneshot, and the flock blocks concurrent gateway invocations.
    """

    def __init__(self, config: Config, *, gateway_mode: bool) -> None:
        self._config = config
        self._gateway_mode_requested = gateway_mode

        # Resolved at :meth:`start` time after the leader-lock attempt.
        # ``True`` only when both the env-var check AND the lock acquire
        # succeeded. Tools and the doctor command read this to report
        # the real operational mode (not just what was requested).
        self._is_leader = False

        self._started = False
        self._stopped = False
        self._lock = threading.Lock()

        self._identity: AgentIdentity | None = None
        self._client: AgentChatClient | None = None
        self._queue: MessageQueue | None = None
        self._ws_daemon: WSDaemon | None = None
        self._invoker: AgentInvoker | None = None

        # File-descriptor for the leader lock when held. ``None`` when
        # not the leader (or before :meth:`start`). Held open for the
        # process lifetime — POSIX flock auto-releases on close, so we
        # explicitly close in :meth:`stop`/:meth:`_teardown_partial`.
        self._leader_lock_fd: int | None = None

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

    @property
    def is_leader(self) -> bool:
        """``True`` iff this process holds the WS leader lock.

        ``False`` in follower modes (CLI/TUI/oneshot) AND in
        gateway-class processes that lost the lock race. Tool handlers
        and the doctor command read this to surface the real
        operational mode — not just whether the constructor was given
        ``gateway_mode=True``.
        """
        return self._is_leader

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True

            try:
                # Always needed — the sync client backs every tool
                # handler, and the resolved handle anchors SOUL.md +
                # gates the WS self-echo filter when one runs.
                self._client = self._build_sync_client()
                self._identity = self._resolve_identity(self._client)

                # Leader election. Only attempt when the caller asked
                # for gateway mode (i.e. ``_HERMES_GATEWAY=1`` was set).
                # The follower path covers TUI/CLI/oneshot AND
                # gateway-class processes that lost the flock race.
                self._is_leader = (
                    self._gateway_mode_requested
                    and self._try_acquire_leader()
                )

                if self._is_leader:
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
                        "agentchat runtime started: handle=@%s ws=%s "
                        "mode=leader (this process owns the live WS)",
                        self._identity.handle,
                        self._config.ws_url,
                    )
                elif self._gateway_mode_requested:
                    # Gateway-class process that lost the lock race.
                    # Loud warning so the operator can see why their
                    # second-gateway invocation isn't getting messages.
                    logger.warning(
                        "agentchat runtime started: handle=@%s "
                        "mode=follower-gateway "
                        "(another process holds the WS leader lock — "
                        "this process will register tools and skill but "
                        "will NOT receive live inbound. If this is a "
                        "stale gateway, stop it first.)",
                        self._identity.handle,
                    )
                else:
                    logger.info(
                        "agentchat runtime started: handle=@%s "
                        "mode=follower-cli (no WS, no invoker — live "
                        "inbound runs in the gateway process)",
                        self._identity.handle,
                    )
            except Exception:
                logger.exception("agentchat runtime: start failed")
                # Best-effort cleanup so subsequent get_runtime() calls
                # don't see a half-initialized object.
                self._teardown_partial()
                self._started = False
                raise

    def _try_acquire_leader(self) -> bool:
        """Attempt the leader-lock acquire. Stores fd on success."""
        fd = try_acquire_ws_leader_lock()
        if fd is None:
            return False
        self._leader_lock_fd = fd
        return True

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

        # Release the leader lock last — only after WS + invoker are
        # gone, so any next-elected leader doesn't briefly see two
        # active WS daemons during the handoff.
        if self._leader_lock_fd is not None:
            release_leader_lock(self._leader_lock_fd)
            self._leader_lock_fd = None
            self._is_leader = False

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

        Only ``handle`` is extracted — internal database ids are
        server-side per the platform's identity model and are not
        present in this response. Surfaces auth errors immediately
        so a bad API key fails fast at plugin start rather than
        silently producing an unauthenticated WS that gets closed by
        the server.
        """
        me = client.get_me()
        handle_raw = me.get("handle") if isinstance(me, dict) else None
        if not isinstance(handle_raw, str) or not handle_raw:
            raise RuntimeError(
                "agentchat runtime: /v1/agents/me returned no handle — "
                "refusing to start without a confirmed identity. "
                "Check that AGENTCHATME_API_KEY is valid."
            )
        return AgentIdentity(handle=handle_raw.lstrip("@").lower())

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

        if self._leader_lock_fd is not None:
            release_leader_lock(self._leader_lock_fd)
            self._leader_lock_fd = None

        self._identity = None
        self._client = None
        self._queue = None
        self._ws_daemon = None
        self._invoker = None
        self._is_leader = False


# Module-level singleton. Hermes calls register(ctx) once per process,
# and that's where get_runtime is first invoked.
_singleton: Runtime | None = None
_singleton_lock = threading.Lock()


def get_runtime(config: Config, *, gateway_mode: bool) -> Runtime:
    """Return the process-wide runtime, constructing if needed.

    Idempotent within a process — the same Config typically resolves
    to the same Runtime. Construction is lock-guarded so concurrent
    register() calls (which shouldn't happen, but defensive) don't
    spawn two daemons.

    ``gateway_mode`` is honored only on first construction; subsequent
    calls within the same process return the existing singleton
    regardless. That's the intended behavior — Hermes calls
    ``register(ctx)`` once per process and the mode is fixed for the
    process lifetime.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Runtime(config, gateway_mode=gateway_mode)
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
