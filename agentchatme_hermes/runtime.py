"""Per-profile runtime coordinator.

One :class:`Runtime` per (Hermes profile, AgentChat account) in a
process. A classic single-profile gateway therefore has exactly one; a
multiplexed gateway serving several profiles has one per profile, each
bound to that profile's own key, home, SOUL.md and leader lock. Owns:

* The resolved :class:`AgentIdentity` (handle only — internal id is
  server-side, we never extract it) loaded via ``GET /v1/agents/me``
  at start.
* The :class:`MessageQueue` between WS daemon and invoker.
* The :class:`WSDaemon` background thread (inbound).
* The :class:`AgentInvoker` thread pool (Mechanism A turns).
* A sync :class:`AgentChatClient` shared by all tool handlers for
  HTTP API calls (outbound + every other ``/v1/*`` op).

Accessed via :func:`get_runtime` (constructs if needed) and
:func:`get_existing_runtime` (returns the registered runtime or ``None``).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .agent_invoker import AgentInvoker
from .client_identity import hermes_client_identity
from .leader_lock import lock_path_for, release_leader_lock, try_acquire_ws_leader_lock
from .lookup_cache import LookupCache
from .message_queue import MessageQueue
from .profile_scope import (
    account_fingerprint,
    hermes_home_key,
    profile_label,
    resolve_hermes_home,
)
from .thread_closures import ThreadClosures
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
      the per-account WS leader lock (see :mod:`.leader_lock`).
    * **Follower mode** — light runtime: sync HTTP client + identity
      resolution only. No WS connection opened, no invoker thread
      pool. Used by TUI/REPL, named CLI subcommands, one-shot agents,
      AND gateway-class processes that lost the leader-lock race.
      Tool surface remains available so the agent can still call
      ``agentchat_*`` tools; only the live inbound stream is
      leader-only.

    The two-gate design (env var + flock) eliminates every flavor
    of multi-WS-per-account: the gateway env var blocks TUI/CLI/
    oneshot, and the flock blocks concurrent gateways for one account
    while letting sibling profiles with their own accounts receive.
    """

    def __init__(self, config: Config, *, gateway_mode: bool) -> None:
        self._config = config
        self._gateway_mode_requested = gateway_mode

        # Profile binding. Read defensively: production passes a
        # :class:`Config`, tests pass light doubles without these fields.
        home = getattr(config, "hermes_home", None)
        self._hermes_home: Path = Path(home) if home is not None else resolve_hermes_home()
        fingerprint = getattr(config, "account_fingerprint", None)
        if not isinstance(fingerprint, str):
            fingerprint = account_fingerprint(config.api_base, config.api_key)
        self._lock_path = lock_path_for(
            hermes_home=self._hermes_home,
            account_fingerprint=fingerprint,
            lock_dir=getattr(config, "lock_dir", None),
        )

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
        self._lookup_cache = LookupCache()
        self._thread_closures = ThreadClosures()
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
    def lookup_cache(self) -> LookupCache:
        return self._lookup_cache

    @property
    def thread_closures(self) -> ThreadClosures:
        return self._thread_closures

    @property
    def hermes_home(self) -> Path:
        """Home of the Hermes profile this runtime serves."""
        return self._hermes_home

    @property
    def lock_path(self) -> Path:
        """Leader-lock file this runtime competes for (per account)."""
        return self._lock_path

    @property
    def gateway_mode_requested(self) -> bool:
        """What the constructor was asked for — not the resolved mode."""
        return self._gateway_mode_requested

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
                        runtime=self,
                    )
                    self._ws_daemon = WSDaemon(
                        config=self._config,
                        identity=self._identity,
                        queue=self._queue,
                        thread_closures=self._thread_closures,
                        on_new_event=self._invoker.on_new_event,
                        on_group_invite=self._invoker.handle_group_invite,
                    )

                    self._invoker.start()
                    self._ws_daemon.start()

                    logger.info(
                        "agentchat runtime started: profile=%s handle=@%s "
                        "ws=%s lock=%s mode=leader (this process owns the "
                        "live WS for this account)",
                        profile_label(self._hermes_home),
                        self._identity.handle,
                        self._config.ws_url,
                        self._lock_path,
                    )
                elif self._gateway_mode_requested:
                    # Gateway-class process that lost the lock race for
                    # THIS account. Loud warning so the operator can see
                    # why this invocation isn't getting messages.
                    logger.warning(
                        "agentchat runtime started: profile=%s handle=@%s "
                        "mode=follower-gateway (another process holds the "
                        "leader lock for this account at %s — this process "
                        "will register tools and skill but will NOT receive "
                        "live inbound. If a stale gateway or a second "
                        "profile uses the same API key, stop it or give "
                        "each profile its own account.)",
                        profile_label(self._hermes_home),
                        self._identity.handle,
                        self._lock_path,
                    )
                else:
                    logger.info(
                        "agentchat runtime started: profile=%s handle=@%s "
                        "mode=follower-cli (no WS, no invoker — live "
                        "inbound runs in the gateway process)",
                        profile_label(self._hermes_home),
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
        """Attempt the per-account leader-lock acquire. Stores fd on success."""
        fd = try_acquire_ws_leader_lock(self._lock_path)
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
            client_identity=hermes_client_identity(),
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


# ─── registry ──────────────────────────────────────────────────────────────
#
# One runtime per (profile home, server, account). Hermes calls
# register(ctx) once per profile — once per process in the classic
# single-profile gateway, once per served profile in a multiplexed one —
# and each call must get its own runtime: its own key, its own socket,
# its own SOUL.md. A single module-level object (the pre-0.2.3111111
# design) silently handed every profile the first profile's identity.


class _RuntimeKey(NamedTuple):
    home_key: str
    api_base: str
    account_fingerprint: str


def _key_for(config: Any) -> _RuntimeKey:
    home = getattr(config, "hermes_home", None)
    home_path = Path(home) if home is not None else resolve_hermes_home()
    fingerprint = getattr(config, "account_fingerprint", None)
    if not isinstance(fingerprint, str):
        fingerprint = account_fingerprint(config.api_base, config.api_key)
    return _RuntimeKey(
        home_key=hermes_home_key(home_path),
        api_base=str(config.api_base).rstrip("/"),
        account_fingerprint=fingerprint,
    )


_runtimes: dict[_RuntimeKey, Runtime] = {}
_registry_lock = threading.Lock()


def get_runtime(config: Config, *, gateway_mode: bool) -> Runtime:
    """Return the runtime for ``config``'s profile and account, constructing if needed.

    Idempotent per key: the same profile + account resolves to the same
    object for the life of the process. Construction is lock-guarded so
    concurrent ``register()`` calls can't spawn two daemons for one
    account.

    ``gateway_mode`` is honored on first construction for a key. A later
    call with a different mode for the same key is logged — it means two
    code paths disagree about what kind of process this is, which is
    worth seeing in the log rather than silently resolving one way.
    """
    key = _key_for(config)
    with _registry_lock:
        existing = _runtimes.get(key)
        if existing is None:
            existing = Runtime(config, gateway_mode=gateway_mode)
            _runtimes[key] = existing
        elif existing.gateway_mode_requested != gateway_mode:
            logger.warning(
                "agentchat runtime: get_runtime(gateway_mode=%s) for an "
                "account already registered with gateway_mode=%s — keeping "
                "the first construction (profile=%s)",
                gateway_mode,
                existing.gateway_mode_requested,
                profile_label(existing.hermes_home),
            )
        return existing


def get_existing_runtime(config: Config | None = None) -> Runtime | None:
    """Return an already-constructed runtime, or ``None``.

    With ``config`` the lookup is exact. Without it the call is only
    unambiguous when a single runtime exists (the classic single-profile
    process); with several profiles registered and no config to pick
    one, this returns ``None`` rather than guessing another profile's
    identity. Components that belong to a runtime should hold a
    reference instead (see :class:`AgentInvoker`).
    """
    with _registry_lock:
        if config is not None:
            return _runtimes.get(_key_for(config))
        if len(_runtimes) == 1:
            return next(iter(_runtimes.values()))
        if _runtimes:
            logger.debug(
                "get_existing_runtime(): %d runtimes registered and no "
                "config given — refusing to guess",
                len(_runtimes),
            )
        return None


def registered_runtimes() -> list[Runtime]:
    """Snapshot of every runtime in this process (diagnostics)."""
    with _registry_lock:
        return list(_runtimes.values())


def reset_for_tests() -> None:
    """Stop and drop every runtime — test-only.

    Production code paths never call this. Tests use it to isolate
    runtime state between test functions.
    """
    with _registry_lock:
        runtimes = list(_runtimes.values())
        _runtimes.clear()
    for rt in runtimes:
        try:
            rt.stop()
        except Exception:
            logger.debug("reset_for_tests: stop raised", exc_info=True)
