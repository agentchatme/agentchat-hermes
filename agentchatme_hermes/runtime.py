"""Per-profile runtime coordinator.

One :class:`Runtime` per (Hermes profile, AgentChat account) in a
process. A classic single-profile gateway therefore has exactly one; a
multiplexed gateway serving several profiles has one per profile, each
bound to that profile's own key, home, SOUL.md and leader lock. Owns:

* The resolved :class:`AgentIdentity` (handle only — internal id is
  server-side, we never extract it) loaded via ``GET /v1/agents/me``
  at start.
* The sync SDK :class:`AgentChatClient`: identity, the tray, the verdicts
  and the texts all go through it.
* The :class:`TextsStore` (the served texts, cached, refreshed daily) in
  the account's state directory.
* The :class:`WSDaemon` background thread (where wakes come from), leader only.
* The :class:`AgentInvoker` worker (wake → gate → turn), leader only.

Accessed via :func:`get_runtime` (constructs if needed) and
:func:`get_existing_runtime` (returns the registered runtime or ``None``).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

from .agent_invoker import AgentInvoker
from .client_identity import hermes_client_identity
from .leader_lock import lock_path_for, release_leader_lock, try_acquire_ws_leader_lock
from .profile_scope import (
    account_fingerprint,
    hermes_home_key,
    profile_context,
    profile_label,
    resolve_hermes_home,
)
from .state import LEGACY_INVITES_FILENAME, TEXTS_FILENAME, state_dir_for
from .texts import TextsCallback, TextsStore
from .turn import identity_line as render_identity_line
from .turn import identity_line_template as render_identity_line_template
from .types import AgentIdentity
from .ws_daemon import WSDaemon
from .ws_health import HealthBoard, health_path_for

if TYPE_CHECKING:
    from agentchatme import AgentChatClient
    from agentchatme.types import Texts

    from .config import Config

logger = logging.getLogger(__name__)

# A gateway-class process that lost the leader election keeps trying.
# When the leader for this account exits (crash, restart, profile
# migration) the next attempt wins and this process takes over the live
# socket without anyone restarting it. Module constant so tests can
# shrink it.
LEADER_RETRY_INTERVAL_S = 15.0


class Runtime:
    """Coordinator for the plugin's stateful subsystems.

    Has two operational modes, resolved at :meth:`start` time:

    * **Leader mode** — full runtime: identity resolution + texts + WSDaemon
      background thread + AgentInvoker worker. Reached only when the
      constructor was called with ``gateway_mode=True``, the integration is
      not switched off, AND this process successfully acquired the
      per-account WS leader lock (see :mod:`.leader_lock`).
    * **Follower mode** — light runtime: identity resolution and texts
      only. No WS connection opened, no invoker. Used by TUI/REPL, named
      CLI subcommands, one-shot agents, AND gateway-class processes that
      lost the leader-lock race. The agent's tools are the hosted MCP
      server in every mode; only the wake is leader-only.

    The two-gate design (env var + flock) eliminates every flavor
    of multi-WS-per-account: the gateway env var blocks TUI/CLI/
    oneshot, and the flock blocks concurrent gateways for one account
    while letting sibling profiles with their own accounts receive.
    """

    def __init__(
        self,
        config: Config,
        *,
        gateway_mode: bool,
        host_llm: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config
        self._gateway_mode_requested = gateway_mode
        # Accessor for Hermes' public plugin LLM (``ctx.llm``) of the profile
        # that registered this runtime; the invoker's gate call uses it.
        self._host_llm = host_llm

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
        self._state_dir = state_dir_for(
            hermes_home=self._hermes_home,
            account_fingerprint=fingerprint,
            state_dir=getattr(config, "state_dir", None),
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
        self._texts: TextsStore | None = None
        self._ws_daemon: WSDaemon | None = None
        self._invoker: AgentInvoker | None = None

        # File-descriptor for the leader lock when held. ``None`` when
        # not the leader (or before :meth:`start`). Held open for the
        # process lifetime — POSIX flock auto-releases on close, so we
        # explicitly close in :meth:`stop`/:meth:`_teardown_partial`.
        self._leader_lock_fd: int | None = None

        # Follower-gateway takeover: retries the leader election until
        # it wins or the runtime stops.
        self._retry_stop = threading.Event()
        self._leader_retry_thread: threading.Thread | None = None

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
        """Sync SDK client: identity, tray, verdicts, texts (the daemon has its own async one)."""
        if self._client is None:
            raise RuntimeError("Runtime.start() has not completed")
        return self._client

    @property
    def texts_store(self) -> TextsStore:
        if self._texts is None:
            raise RuntimeError("Runtime.start() has not completed")
        return self._texts

    @property
    def texts(self) -> Texts | None:
        """The served texts in use, or ``None`` while none could be loaded."""
        return self._texts.get() if self._texts is not None else None

    def identity_line(self) -> str | None:
        """The served identity line for this agent, spelled for Hermes; ``None`` without texts."""
        texts = self.texts
        return render_identity_line(texts) if texts is not None else None

    def identity_line_template(self) -> str | None:
        """The served template (``<handle>`` unfilled), spelled for Hermes; ``None`` without texts."""
        texts = self.texts
        return render_identity_line_template(texts) if texts is not None else None

    def on_texts(self, callback: TextsCallback) -> None:
        """Run ``callback(texts)`` now if texts are loaded, and whenever they change."""
        self.texts_store.on_texts(callback)

    @property
    def integration_enabled(self) -> bool:
        return bool(getattr(self._config, "integration_enabled", True))

    @property
    def hermes_home(self) -> Path:
        """Home of the Hermes profile this runtime serves."""
        return self._hermes_home

    @property
    def lock_path(self) -> Path:
        """Leader-lock file this runtime competes for (per account)."""
        return self._lock_path

    @property
    def state_dir(self) -> Path:
        """Where this account's texts cache lives."""
        return self._state_dir

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
                # Always needed — the resolved handle anchors SOUL.md and
                # gates the WS self-echo filter when one runs.
                self._client = self._build_sync_client()
                self._identity = self._resolve_identity(self._client)
                # The served texts: one synchronous attempt now (server, else
                # the cache), then a daily refresh. Starting without them is
                # allowed; the store says so once and keeps trying.
                home = self._hermes_home
                self._texts = TextsStore(
                    client=self._client,
                    cache_path=self._state_dir / TEXTS_FILENAME,
                    # The refresh thread's listeners (the SOUL.md reconcile)
                    # must see this profile's home, not the launch profile's.
                    thread_scope=lambda: profile_context(home),
                )
                self._texts.start()
                # Invites are on the tray (contract v1); the ledger file a
                # release before kept is not read by anything any more.
                _remove_quietly(self._state_dir / LEGACY_INVITES_FILENAME)

                # Leader election. Only attempt when the caller asked for
                # gateway mode (a real ``hermes gateway run``) and the
                # integration is on. The follower path covers TUI/CLI/oneshot
                # AND gateway-class processes that lost the flock race.
                self._is_leader = (
                    self._gateway_mode_requested
                    and self.integration_enabled
                    and self._try_acquire_leader()
                )

                if self._is_leader:
                    self._start_leader_components()
                    logger.info(
                        "agentchat runtime started: profile=%s handle=@%s "
                        "ws=%s lock=%s mode=leader (this process owns the "
                        "live WS for this account)",
                        profile_label(self._hermes_home),
                        self._identity.handle,
                        self._config.ws_url,
                        self._lock_path,
                    )
                elif self._gateway_mode_requested and not self.integration_enabled:
                    logger.warning(
                        "agentchat runtime started: profile=%s handle=@%s "
                        "mode=integration-off (AGENTCHATME_INTEGRATION=off — no socket, no "
                        "wakes, no turns; the hosted tools and the manual stay "
                        "available and the agent checks AgentChat only when run)",
                        profile_label(self._hermes_home),
                        self._identity.handle,
                    )
                elif self._gateway_mode_requested:
                    # Gateway-class process that lost the lock race for
                    # THIS account. Loud warning so the operator can see
                    # why this invocation isn't getting messages — and a
                    # retry loop so it takes over when the leader goes.
                    logger.warning(
                        "agentchat runtime started: profile=%s handle=@%s "
                        "mode=follower-gateway (another process holds the "
                        "leader lock for this account at %s — this process "
                        "serves tools and skill but will NOT receive live "
                        "inbound until that process exits; it retries the "
                        "election every %.0fs. If a second profile uses the "
                        "same API key, give each profile its own account.)",
                        profile_label(self._hermes_home),
                        self._identity.handle,
                        self._lock_path,
                        LEADER_RETRY_INTERVAL_S,
                    )
                    self._start_leader_retry()
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

    def _start_leader_components(self) -> None:
        """Bring up invoker and WS daemon. Caller holds ``_lock``.

        Requires :attr:`_identity` and the leader lock. Shared by the
        start-time election and the follower takeover path so both
        produce exactly the same leader.
        """
        assert self._identity is not None
        # One health record per account: the socket's state and the worker's,
        # composed by one board so ``doctor`` sees both.
        health = HealthBoard(handle=self._identity.handle, path=health_path_for(self._lock_path))
        self._invoker = AgentInvoker(
            config=self._config,
            identity=self._identity,
            runtime=self,
            health=health,
            host_llm=self._host_llm,
        )
        health.set_worker_status(self._invoker.status)
        self._ws_daemon = WSDaemon(
            config=self._config,
            identity=self._identity,
            on_wake=self._invoker.wake,
            health_path=health_path_for(self._lock_path),
            health=health,
        )
        self._invoker.start()
        self._ws_daemon.start()

    def _start_leader_retry(self) -> None:
        """Keep contesting the election from a follower-gateway runtime."""
        if self._leader_retry_thread is not None:
            return
        self._retry_stop.clear()
        thread = threading.Thread(
            target=self._leader_retry_loop,
            name="agentchat-leader-retry",
            daemon=True,
        )
        self._leader_retry_thread = thread
        thread.start()

    def _leader_retry_loop(self) -> None:
        while not self._retry_stop.wait(LEADER_RETRY_INTERVAL_S):
            with self._lock:
                if self._stopped or self._is_leader:
                    return
                if not self._try_acquire_leader():
                    continue
                try:
                    self._start_leader_components()
                except Exception:
                    # Give the lock back so another process can take it;
                    # the next tick retries from scratch.
                    logger.exception(
                        "agentchat runtime: takeover failed after winning the lock"
                    )
                    self._teardown_leader_components()
                    continue
                self._is_leader = True
                logger.warning(
                    "agentchat runtime: took over as WS leader for @%s "
                    "(profile=%s, lock=%s) — the previous holder exited",
                    self._identity.handle if self._identity else "?",
                    profile_label(self._hermes_home),
                    self._lock_path,
                )
                return

    def _teardown_leader_components(self) -> None:
        """Undo :meth:`_start_leader_components` and release the lock."""
        if self._ws_daemon is not None:
            try:
                self._ws_daemon.stop()
            except Exception:
                logger.debug("agentchat runtime: ws_daemon stop raised", exc_info=True)
            self._ws_daemon = None
        if self._invoker is not None:
            try:
                self._invoker.stop()
            except Exception:
                logger.debug("agentchat runtime: invoker stop raised", exc_info=True)
            self._invoker = None
        if self._leader_lock_fd is not None:
            release_leader_lock(self._leader_lock_fd)
            self._leader_lock_fd = None
        self._is_leader = False

    def stop(self) -> None:
        with self._lock:
            if not self._started or self._stopped:
                return
            self._stopped = True
            self._retry_stop.set()

        # Order matters: stop inbound first (WS daemon) so no new
        # events land in the queue, then drain the invoker, then the
        # texts refresh, then close the HTTP client.
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

        if self._texts is not None:
            self._texts.stop()

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
                "Check that AGENTCHAT_API_KEY is valid."
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
        if self._texts is not None:
            self._texts.stop()
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

        self._retry_stop.set()
        self._identity = None
        self._client = None
        self._texts = None
        self._ws_daemon = None
        self._invoker = None
        self._is_leader = False


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug("agentchat runtime: could not remove %s: %s", path, exc)
        return
    logger.info("agentchat runtime: removed the old invite ledger %s (invites are on the tray)", path)


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


def get_runtime(
    config: Config, *, gateway_mode: bool, host_llm: Callable[[], Any] | None = None
) -> Runtime:
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
            existing = Runtime(config, gateway_mode=gateway_mode, host_llm=host_llm)
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
