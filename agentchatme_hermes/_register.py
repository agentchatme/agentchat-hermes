"""Plugin registration entry — wires the integration into a Hermes process.

Called once per Hermes profile by the plugin loader (details at the end
of this docstring). Two registration modes, chosen based on which Hermes
process is loading us:

* **Gateway mode** (the long-lived ``hermes gateway run`` process, AND it
  successfully grabbed the per-account WS leader lock): full runtime —
  identity resolution, the served texts, the WS daemon that wakes, the
  invoker that gates and starts turns, the manual as a skill (this
  release's copy; with the plugin setting ``live_texts`` on, kept current
  from the server by the receive loop).

* **CLI mode** (every other Hermes process — TUI/REPL, named subcommands,
  one-shot agents, Hermes-spawned child Python that inherits
  ``_HERMES_GATEWAY=1`` from its parent, or a second gateway whose flock
  acquisition lost the race): light runtime — identity only, no WS, no
  invoker. The skill is still registered — as the manual is found; only the
  gateway writes it (a gateway that lost the race syncs it once) — and the
  agent's tools are the hosted MCP server declared in ``config.yaml``,
  available in every mode.
  Live inbound stays exclusively with the leader-lock holder.

Mode selection has two layered signals (see :mod:`.leader_lock`):

1. argv says ``hermes gateway run`` (the ``_HERMES_GATEWAY=1`` marker alone
   is no longer enough — every child the gateway spawns inherits it).
2. ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the per-account lock file under
   ``<hermes root>/agentchat-locks/``. Even with the argv signal, only one
   process can hold this OS-level lock for a given account at a time.
   Losers fall back to CLI mode and retry the election.

What this plugin does NOT register: tools. The pre-v3 plugin registered 47
native ``agentchat_*`` tools and a reply gate of its own; both were one of
three drifting copies of the product. Since 0.3.0 the tools are the
hosted MCP server (one definition for every runtime) and the gate question
and cards come from the server — see ``docs/decisions/2026-11-main-line.md``.

There is NO ``on_session_end`` hook. Hermes fires that on every individual
session ending (TUI sessions, cron jobs, adapter chats — not just process
shutdown), which would spuriously stop the runtime mid-flight. Daemon
threads (``daemon=True``) die with the process on real shutdown.

The plugin's own unload is different: ``ctx.on_unload`` runs when Hermes
unloads this plugin (reload, disable, remove) in a process that keeps
running. Then the runtime is stopped — socket closed, wake worker and timers
stopped, leader lock released — and dropped from the registry, so the next
``register(ctx)`` (the reloaded code) can take the lock and the socket
instead of finding them held by the old one.

Called once per Hermes **profile**: once per process in a classic gateway,
once per served profile in a multiplexed gateway (Hermes keeps one plugin
manager per profile home). Every call resolves its own profile home and its
own settings and gets its own :class:`runtime.Runtime`, keyed by profile
and account. ``register`` captures the context Hermes loaded it in
(:class:`.profile_scope.ProfileScope`); every thread and cycle the plugin
runs for the profile runs in that scope — see :mod:`.profile_scope`.
"""
from __future__ import annotations

import inspect
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Literal

from ._version import __version__
from .config import ENV_API_KEY_LEGACY, KEY_SOURCE_STORE, ConfigError, KeyRefused, load_config
from .kit import kit_problem
from .leader_lock import is_hermes_gateway_process
from .profile_scope import (
    ProfileScope,
    ProfileScopeError,
    hermes_home_key,
    profile_label,
    resolve_hermes_home,
)
from .texts import mode_for, read_mode, record_mode

if TYPE_CHECKING:
    from pathlib import Path

    from .runtime import Runtime, StartFailure

logger = logging.getLogger(__name__)

# How often a gateway process without a working configuration re-checks
# for one. Cheap when no key exists (a settings read); when a key exists
# but the server is unreachable the watcher backs off exponentially up to
# ACTIVATION_RETRY_MAX_S so a flapping network does not spam the log.
ACTIVATION_RETRY_S = 30.0
ACTIVATION_RETRY_MAX_S = 300.0

# Why the runtime last failed to start, per profile home (:func:`.runtime.start_failure`):
# ``register()`` says the matching remedy, never login for a network outage.
_last_start_failure: dict[str, StartFailure] = {}


def register(ctx: Any) -> None:
    """Hermes plugin entry point. See module docstring."""
    # The code this process loaded, taken now (before an update can replace
    # the files): the health record names it, so doctor can tell a gateway
    # still running an earlier build (ws_health.plugin_build).
    try:
        from .ws_health import plugin_build

        plugin_build()
    except Exception:
        logger.debug("agentchat plugin: the plugin build was not read", exc_info=True)
    # CLI subcommand is registered first and unconditionally so the
    # wizard remains reachable even when API key configuration is
    # missing or broken.
    try:
        from .cli import register_cli

        register_cli(ctx)
    except Exception:
        logger.error(
            "agentchat plugin: the `hermes agentchat` commands could not be registered",
            exc_info=True,
        )

    # The key file reaches Hermes' MCP client (the hosted tools) through a
    # secret source, in every Hermes process: registered before anything else
    # can fail, so the tools authenticate even where nothing else runs.
    try:
        from .key_source import register_key_source

        register_key_source(ctx)
    except Exception:
        logger.error("agentchat plugin: the key source could not be registered", exc_info=True)

    # Receiving is the SDK kit's receive loop: an agentchatme without it can
    # neither receive nor place the identity line. Say so once, loudly, and
    # keep the CLI so `hermes agentchat doctor` says it too.
    hermes_home = resolve_hermes_home()
    # The profile's scope, as Hermes loaded this plugin for it: every thread
    # and cycle below runs in it (profile_scope.ProfileScope).
    scope = ProfileScope.capture(hermes_home)
    # Which manual and identity line this profile uses (the plugin setting
    # ``live_texts``), read here, where Hermes has bound this profile, and
    # used by every later step in this process (texts.mode_for).
    mode = read_mode(ctx)
    record_mode(hermes_home, mode)
    if mode.problem is not None:
        logger.warning(
            "agentchat plugin: profile=%s — %s; this release's manual and identity line are used "
            "(`hermes agentchat doctor` says how to set it)",
            profile_label(hermes_home),
            mode.problem,
        )

    # ``/agentchat allow``: the person allows the AgentChat tools from a chat
    # with the agent (Hermes' own prompt, asked in the command's own context;
    # the switch read and written in this profile's scope). Registered
    # unconditionally, like the CLI: it needs no key and no runtime.
    try:
        from .allow import register_allow_command

        register_allow_command(ctx, scope)
    except Exception:
        logger.error("agentchat plugin: /agentchat could not be registered", exc_info=True)

    # The setup line in each new session's system prompt, until setup is done
    # (how to sign up; the code that finishes it; "/agentchat allow"): the
    # agent asked to join AgentChat reads its next step here, not the manual's
    # HTTP sign-up. Registered unconditionally, like the command; empty once set up.
    try:
        from .setup_prompt import register_setup_section

        register_setup_section(ctx, scope)
    except Exception:
        logger.error("agentchat plugin: the setup section could not be registered", exc_info=True)

    # plugins.isolation: host runs this code outside the gateway: nothing can
    # hold the socket, so the agent is never woken. Said once per process
    # (Hermes calls register() once per profile and process); /agentchat says it too.
    try:
        from .allow import host_isolation_note, plugin_host_isolation

        if scope.run(plugin_host_isolation):
            logger.warning(
                "agentchat plugin: profile=%s — %s", profile_label(hermes_home), host_isolation_note()
            )
    except Exception:
        logger.debug("agentchat plugin: plugins.isolation not read", exc_info=True)

    gateway_mode = _is_gateway_context(ctx)
    problem = kit_problem()
    if gateway_mode:
        # Whether this gateway can run AgentChat at all, for every other
        # process to read (placement.gateway_cannot_load): a first install
        # into a running gateway has no SDK here until the gateway restarts.
        try:
            from .placement import clear_gateway_cannot_load, note_gateway_cannot_load

            if problem is not None:
                note_gateway_cannot_load(hermes_home, problem)
            else:
                clear_gateway_cannot_load(hermes_home)
        except Exception:
            logger.debug("agentchat plugin: the gateway's plugin load was not recorded", exc_info=True)
    if problem is not None:
        if gateway_mode:
            logger.error(
                "agentchat plugin: not receiving — %s — a gateway that was already running when the plugin "
                "was installed loads its dependency only after a restart: `hermes gateway restart` (or "
                "/restart in a messaging chat)",
                problem,
            )
        else:
            logger.error("agentchat plugin: not receiving — %s", problem)
        # The key source still masks a copied key for this process's tools.
        _find_copy(scope)
        _register_surface(ctx, hermes_home, place=False)
        return

    profile = profile_label(hermes_home)

    # The manual is registered unconditionally as the ``agentchat:agentchat``
    # skill: this release's copy by default; with live_texts on, this
    # profile's kept-current file. Only the gateway writes that file
    # (from the bundled copy when missing; local only, no network); every
    # other process registers it as it is (see manual.py). ``gateway_mode``
    # is the same flag the runtime is built with, so this and
    # runtime.keeps_placements_current (the follower's manual sync, the
    # SOUL.md reconcile) are one decision. Tools are not registered here:
    # they are the hosted MCP server declared in the profile's config.yaml
    # (see mcp_registration).
    _register_surface(ctx, hermes_home, place=gateway_mode)

    watcher = ActivationWatcher(
        hermes_home=hermes_home,
        profile=profile,
        gateway_mode=gateway_mode,
        host_llm=host_llm_accessor(ctx),
        scope=scope,
    )
    _close_on_unload(ctx, watcher)
    outcome = watcher.attempt()

    if outcome == "active":
        return

    failure = _last_start_failure.get(hermes_home_key(hermes_home)) if outcome == "failed" else None
    reason = {
        "unconfigured": "no API key configured",
        "refused": "the key this profile holds is refused (the error above says why)",
    }.get(outcome, "AgentChat could not be reached" if failure == "unreachable" else "runtime did not start")
    if gateway_mode:
        # Long-lived process: keep trying. A gateway that boots before the
        # wizard has run, or before the network is up, activates by
        # itself the moment configuration and connectivity exist. Before
        # 0.2.3111111 that gateway stayed CLI-only until a restart — the
        # most common reason a freshly installed profile never received.
        watcher.start()
        logger.info(
            "agentchat plugin: %s for profile=%s (%s) — gateway will %s and activate without a "
            "restart.",
            reason,
            profile,
            hermes_home,
            watcher.retry_schedule(),
        )
    elif outcome == "unconfigured":
        from .config import cli_prefix

        cmd = cli_prefix(hermes_home)
        logger.info(
            "agentchat plugin: %s for profile=%s (%s) — CLI-only in this "
            "process. Run `%s register` (new account) or "
            "`%s login` (existing key) in this profile.",
            reason,
            profile,
            hermes_home,
            cmd,
            cmd,
        )
    elif failure == "unreachable":
        logger.info(
            "agentchat plugin: %s for profile=%s (%s) — CLI-only in this process. Nothing to change "
            "here: the gateway, which receives, tries again on its own; check this machine's network "
            "if it lasts.",
            reason,
            profile,
            hermes_home,
        )
    else:
        logger.info(
            "agentchat plugin: %s for profile=%s (%s) — CLI-only in this process; the line above says why.",
            reason,
            profile,
            hermes_home,
        )


def _find_copy(scope: ProfileScope) -> None:
    """Resolve the key once in the profile's scope, so a key this profile holds
    as a copy of another profile's is known to the key source. Never raises."""
    from .config import resolve_key

    try:
        scope.run(resolve_key, scope.home)
    except Exception:
        logger.debug("agentchat plugin: key resolution raised", exc_info=True)


def _register_surface(ctx: Any, hermes_home: Path, *, place: bool = True) -> None:
    """Register the manual as the ``agentchat:agentchat`` skill (``place``: the
    gateway, which may write it). No tools — see module docstring. Never
    raises: a skill that cannot be registered must not cost the CLI or the
    receive loop."""
    try:
        from .skills import register_skill

        register_skill(ctx, hermes_home=hermes_home, place=place)
    except Exception:
        logger.error(
            "agentchat plugin: the manual could not be registered as the agentchat:agentchat skill "
            "for %s",
            hermes_home,
            exc_info=True,
        )


class ActivationWatcher:
    """Brings a profile's runtime up as soon as it can, and keeps trying.

    One instance per ``register(ctx)`` call, i.e. per profile.
    :meth:`try_activate` performs a single attempt and is what the tests
    drive; :meth:`start` runs it on a daemon thread with backoff until it
    succeeds or the process exits.
    """

    def __init__(
        self,
        *,
        hermes_home: Path,
        profile: str,
        gateway_mode: bool,
        host_llm: Callable[[], Any] | None = None,
        scope: ProfileScope | None = None,
    ) -> None:
        self._runtime: Runtime | None = None
        self._host_llm = host_llm
        # The profile's scope (``register`` passes the one it captured).
        self._scope = scope if scope is not None else ProfileScope.capture(hermes_home)
        # Guards ``_runtime`` and ``_closed`` between the activation thread
        # and :meth:`close` (the plugin's unload).
        self._state_lock = threading.Lock()
        self._closed = False
        self._hermes_home = hermes_home
        self._profile = profile
        self._gateway_mode = gateway_mode
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures = 0
        # The last refusal logged, so a watcher re-checking every 30 s says it once.
        self._refusal: str | None = None
        self._scope_problem: str | None = None

    @property
    def runtime(self) -> Runtime | None:
        return self._runtime

    def attempt(self) -> str:
        """One attempt, in the profile's scope: :meth:`try_activate`, or
        ``"failed"`` when the scope cannot be entered (said once; nothing runs
        unscoped, the next attempt tries again)."""
        try:
            return self._scope.run(self.try_activate)
        except ProfileScopeError as exc:
            self._failures += 1
            message = str(exc)
            if message != self._scope_problem:
                self._scope_problem = message
                logger.error(
                    "agentchat plugin: not starting for profile=%s — this profile's scope could not "
                    "be entered (%s); nothing runs outside it, and the next attempt tries again",
                    self._profile,
                    message,
                )
            return "failed"
        except Exception as exc:
            # register() must never raise into Hermes' plugin load: anything a
            # Hermes helper raised (an unreadable config.yaml, a changed API)
            # is said once and the next attempt tries again.
            self._failures += 1
            message = f"{type(exc).__name__}: {exc}"
            if message != self._scope_problem:
                self._scope_problem = message
                logger.error(
                    "agentchat plugin: activation attempt failed for profile=%s (%s); the next "
                    "attempt tries again",
                    self._profile,
                    message,
                    exc_info=True,
                )
            return "failed"

    def try_activate(self) -> str:
        """One attempt in the calling scope. Returns ``"active"``,
        ``"unconfigured"``, ``"refused"``, ``"failed"`` or ``"closed"`` (the
        plugin was unloaded)."""
        with self._state_lock:
            if self._closed:
                return "closed"
            if self._runtime is not None:
                return "active"
        try:
            config = load_config(hermes_home=self._hermes_home)
        except KeyRefused as exc:
            # Not acting as an agent this profile must not be: no socket, no
            # placement. Said once; the watcher keeps checking, so fixing it
            # (logout, login) takes effect without a restart.
            return self._refused(str(exc))
        except ConfigError as exc:
            logger.warning(
                "agentchat plugin: configuration error (profile=%s) — %s",
                self._profile,
                exc,
            )
            self._failures += 1
            return "failed"
        if config is None:
            return "unconfigured"
        split = _split_identity(config)
        if split is not None:
            # The socket and the gate would be one agent, the hosted tools another.
            return self._refused(split)
        _legacy_verified.discard(hermes_home_key(self._hermes_home))
        if getattr(config, "api_key_source", KEY_SOURCE_STORE) == ENV_API_KEY_LEGACY:
            # Verified with the server, then copied into the key file under the
            # per-account lock — before the runtime opens any socket, so a copy
            # never holds the account's socket.
            adopted = _adopt_legacy_key(config)
            if adopted == "copy":
                try:
                    load_config(hermes_home=self._hermes_home)
                except KeyRefused as exc:
                    return self._refused(str(exc))
            if adopted != "go":
                self._failures += 1
                return "failed"

        runtime = _try_start_runtime(
            config, gateway_mode=self._gateway_mode, host_llm=self._host_llm, scope=self._scope
        )
        if runtime is None:
            self._failures += 1
            return "failed"

        if not self._adopt(runtime):
            # Unloaded while this attempt was starting the runtime: it must
            # not keep the socket or the lock for code that is gone.
            _shutdown_runtime(runtime, profile=self._profile)
            return "closed"

        self._failures = 0

        # Placement reconciliation. Wizard users (`hermes agentchat
        # register/login`) already got everything placed at that step; this
        # backfills it for env-var-direct / scripted / container setups and
        # for profiles upgraded from a pre-v3 release — and refuses to stay
        # quiet when the anchor and the key disagree about who this profile is.
        # By default the identity line is this release's: placed now, and
        # re-applied (a no-op when it matches) whenever the texts change.
        # With live_texts on it is served (GET /v1/texts); the anchor is
        # reconciled as soon as texts are loaded — now when the server (or
        # the cache) answered, later when the first fetch succeeds — and
        # again whenever the served line changes.
        self._refusal = None
        for note in getattr(config, "key_notes", ()):
            # Not refused, not silent: the same agent in two profiles (or a
            # leftover copy of the key) is said where the operator looks.
            logger.warning("agentchat plugin: profile=%s — %s", self._profile, note)
        # SOUL.md is kept current by the gateway only (the rule the manual
        # follows: runtime.keeps_placements_current); every other process
        # reports it as found. Placement on request is the CLI's own step.
        from .runtime import keeps_placements_current

        if keeps_placements_current(runtime):
            _reconcile_soul_anchor_when_texts_arrive(runtime)
        _reconcile_key_store(config)
        _reconcile_secrets_block(runtime)
        _reconcile_mcp_registration(runtime)

        logger.info(
            "agentchat plugin registered (version=%s, profile=%s, home=%s, "
            "handle=@%s, api_base=%s, mode=%s, texts=%s)",
            __version__,
            self._profile,
            self._hermes_home,
            runtime.identity.handle,
            config.api_base,
            "gateway" if self._gateway_mode else "cli",
            "live" if mode_for(self._hermes_home).live else "release",
        )
        return "active"

    def _refused(self, message: str) -> str:
        """Said once (the watcher re-checks every 30 s); nothing starts or is placed."""
        if message != self._refusal:
            self._refusal = message
            logger.error("agentchat plugin: not starting for profile=%s — %s", self._profile, message)
        return "refused"

    def retry_schedule(self) -> str:
        """What :meth:`next_delay` does from here, in words, for the log."""
        if self._failures == 0:
            return f"re-check every {ACTIVATION_RETRY_S:.0f}s"
        return (
            f"retry in {self.next_delay():.0f}s, then at longer intervals up to every "
            f"{ACTIVATION_RETRY_MAX_S:.0f}s while it keeps failing"
        )

    def next_delay(self) -> float:
        """Backoff for the watcher thread: flat while unconfigured, exponential after failures."""
        if self._failures == 0:
            return ACTIVATION_RETRY_S
        return min(ACTIVATION_RETRY_MAX_S, ACTIVATION_RETRY_S * (2.0 ** (self._failures - 1)))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = self._scope.thread(self._loop, name="agentchat-activation")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _adopt(self, runtime: Runtime) -> bool:
        """Keep ``runtime`` unless the plugin was unloaded meanwhile."""
        with self._state_lock:
            if self._closed:
                return False
            self._runtime = runtime
            return True

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def close(self) -> None:
        """The plugin is being unloaded: stop retrying and stop the runtime
        (socket, wake worker, timers, leader lock). Idempotent."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            runtime = self._runtime
            self._runtime = None
        self._stop.set()
        if runtime is not None:
            _shutdown_runtime(runtime, profile=self._profile)

    def _loop(self) -> None:
        # A late activation builds the runtime from this thread, each attempt
        # in the profile's scope as register() did (freshly bound secrets on a
        # multiplexed gateway: the launch profile's are not this profile's).
        while not self._stop.wait(self.next_delay()):
            try:
                if self.attempt() in ("active", "closed"):
                    return
            except Exception:
                # The watcher must outlive any surprise; log and keep going.
                logger.exception("agentchat plugin: activation attempt raised")
                self._failures += 1


def _close_on_unload(ctx: Any, watcher: ActivationWatcher) -> None:
    """Have Hermes close ``watcher`` (and its runtime) when it unloads the plugin.

    ``ctx.on_unload`` is Hermes' public cleanup hook for a plugin's unload
    (reload, disable, remove); a Hermes without it unloads plugins only by
    exiting, and the daemon threads go with the process.
    """
    try:
        inspect.getattr_static(ctx, "on_unload")
    except AttributeError:
        logger.debug("agentchat plugin: this Hermes has no ctx.on_unload")
        return
    on_unload = getattr(ctx, "on_unload", None)
    if not callable(on_unload):
        return

    def agentchat_runtime_stop() -> None:
        watcher.close()

    try:
        on_unload(agentchat_runtime_stop)
    except Exception:
        logger.warning(
            "agentchat plugin: could not register the unload hook — a plugin reload keeps the "
            "old runtime until the process exits",
            exc_info=True,
        )


def _shutdown_runtime(runtime: Any, *, profile: str) -> None:
    """Stop a runtime and drop it from the registry (the plugin's unload)."""
    from .runtime import forget_runtime

    try:
        runtime.stop()
    except Exception:
        logger.exception("agentchat plugin: runtime stop raised during unload")
    finally:
        forget_runtime(runtime)
    logger.info(
        "agentchat plugin unloaded for profile=%s: socket closed, wake worker stopped, leader "
        "lock released",
        profile,
    )


def _is_gateway_context(ctx: Any) -> bool:
    """Decide whether this Hermes process is the gateway candidate.

    Returns ``True`` iff ``_HERMES_GATEWAY=1`` is set in the process
    environment. That marker is set by :mod:`gateway.run` at module
    load (``gateway/run.py:370``), before any plugin discovery, and
    is the same signal Hermes' own ``cli.py:538`` uses internally to
    detect gateway context. It is the only reliable signal at
    ``register()`` time — Hermes' ``PluginManager._cli_ref`` is set
    by ``HermesCLI.__init__`` AFTER discovery and is ``None`` in both
    gateway and TUI processes when this code runs.

    The ``ctx`` parameter is unused but kept for signature stability
    and so callers can be confident the decision is per-process, not
    per-context.

    A ``True`` return here makes this process a *candidate* leader.
    The actual WS opens only if it also acquires the leader-lock
    (see :class:`runtime.Runtime` and :mod:`.leader_lock`). That
    second gate handles the edge case of two ``_HERMES_GATEWAY=1``
    processes coexisting (concurrent restart, ``--replace`` drain
    overlap, or a Hermes child that inherited the env var).
    """
    del ctx  # decision is process-level, not ctx-level
    return is_hermes_gateway_process()


def host_llm_accessor(ctx: Any) -> Callable[[], Any] | None:
    """A zero-argument accessor for Hermes' public plugin LLM (``ctx.llm``).

    ``None`` when this Hermes' plugin context has no ``llm`` (older Hermes,
    test doubles). Looked up statically, so the property — which imports
    Hermes' LLM layer — runs only when the gate first needs it.
    """
    try:
        inspect.getattr_static(ctx, "llm")
    except AttributeError:
        return None
    return lambda: ctx.llm


def _try_start_runtime(
    config: Any,
    *,
    gateway_mode: bool,
    host_llm: Callable[[], Any] | None = None,
    scope: ProfileScope | None = None,
) -> Any:
    """Construct + start the runtime. Returns the runtime or ``None`` on failure.

    Reasons start can fail:
    * Invalid API key (UnauthorizedError on /v1/agents/me)
    * Network unreachable (ConnectionError)
    * Server schema regression (RuntimeError from _resolve_identity)
    * Hermes runtime helpers unimportable from inside this process

    All of these are recoverable — in a gateway the activation watcher
    retries with backoff; elsewhere the user fixes the underlying issue
    and restarts. None of them should kill the whole plugin load; we want
    the CLI subcommand to remain reachable so the user can diagnose.
    """
    from pathlib import Path as _Path

    from .runtime import start_failure

    home = getattr(config, "hermes_home", None)
    try:
        from .runtime import get_runtime

        runtime = get_runtime(config, gateway_mode=gateway_mode, host_llm=host_llm, scope=scope)
        runtime.start()
    except Exception as exc:
        from .config import cli_prefix

        cmd = cli_prefix(_Path(home)) if home else "hermes agentchat"
        label = profile_label(_Path(home)) if home else "?"
        failure = start_failure(exc)
        if home:
            _last_start_failure[hermes_home_key(_Path(home))] = failure
        if failure == "unreachable":
            # Not the key: no traceback, no login. The watcher retries with backoff.
            logger.warning(
                "agentchat plugin: AgentChat could not be reached for profile=%s (%s) — nothing is "
                "received until it answers. %s; check this machine's network (DNS, a proxy, a "
                "firewall) if it lasts.",
                label,
                exc,
                "The gateway tries again on its own" if gateway_mode else "The gateway, which receives, tries again on its own",
            )
        elif failure == "key_refused":
            logger.error(
                "agentchat plugin: AgentChat refused this profile's key (profile=%s: %s) — nothing is "
                "received until it holds a key AgentChat accepts: `%s login` with a current key, or "
                "`%s recover` to issue a new one.",
                label,
                exc,
                cmd,
                cmd,
            )
        else:
            logger.error(
                "agentchat plugin: runtime startup failed — there is no live "
                "inbound until it starts. Reason: %s. See the Hermes errors log for the full traceback. "
                "Try `%s doctor` to diagnose.",
                exc,
                cmd,
                exc_info=True,
            )
        return None
    if home:
        _last_start_failure.pop(hermes_home_key(_Path(home)), None)
    return runtime


def _reconcile_soul_anchor_when_texts_arrive(runtime: Any) -> None:
    """Run :func:`_reconcile_soul_anchor` as soon as its line exists, and again
    whenever the served texts change.

    ``runtime.on_texts`` calls back immediately when texts are already loaded
    and again on every change. With ``live_texts`` off (the default) the line
    is this release's and needs no texts: when none are loaded yet it is
    placed now, and each texts change re-applies the same line (no write when
    it matches; the retry after a write that met an edit in flight) — the
    served line never reaches SOUL.md, because ``identity_text`` does not use
    the served texts then. A runtime double without ``on_texts`` (tests) is
    reconciled directly.
    """
    on_texts = getattr(runtime, "on_texts", None)
    if not callable(on_texts):
        _reconcile_soul_anchor(runtime)
        return
    called: list[bool] = []

    def reconcile(_texts: Any) -> None:
        called.append(True)
        _reconcile_soul_anchor(runtime)

    on_texts(reconcile)
    if not called and not mode_for(runtime.hermes_home).live:
        _reconcile_soul_anchor(runtime)


_said_about_soul: set[tuple[str, str, str]] = set()


def _say_once(path: Any, outcome: str, detail: str, message: str, *args: Any) -> None:
    """A warning about SOUL.md said once per process for the same state (the
    reconcile runs on every texts change)."""
    key = (str(path), outcome, detail)
    if key in _said_about_soul:
        return
    _said_about_soul.add(key)
    logger.warning(message, *args)


def _reconcile_soul_anchor(runtime: Any) -> None:
    """Keep this agent's SOUL.md identity block at the line in use
    (``runtime.identity_text()``: this release's, or with ``live_texts`` on the
    served one — below, "the served line"); backfill it
    when there is none; flag an identity mismatch. Run only in a gateway-mode
    process (:func:`.runtime.keeps_placements_current`, the manual's rule):
    everywhere else SOUL.md is reported as found and never written.

    The rule for whose block it is, and the write, are the SDK kit's
    (``anchor.follow``, strict): only this agent's own block — its handle line,
    or, without one, a text AgentChat served word for word — is brought to the
    served line, atomically, the rest of the file kept byte for byte. A block
    whose words merely name ``@handle`` was edited by hand: it is the user's,
    left alone, and said (also by ``login`` and ``doctor --fix``: the served
    line replaces it only once the user deletes it). With no block at all the
    served line is placed
    (the non-wizard path: a key set in the environment, a container, a
    profile upgraded from before v3).

    When the block is another agent's, this profile believes it is one agent
    while the server sees another — how one fleet ended up with a profile
    labelled *Growth* sending as *Operations*. Logged at ERROR and left as it
    is: rewriting it would hide the credential mix-up; ``doctor`` reports it.

    With ``live_texts`` on and no texts there is nothing to place yet; the
    runtime calls again when they arrive. Non-fatal: failures are logged, not
    raised.
    """
    try:
        from .soul_anchor import AnchorError, follow_soul, place_soul_anchor, text_name
    except ImportError:
        logger.debug("agentchatme_hermes.soul_anchor not importable; skipping backfill")
        return

    label = profile_label(runtime.hermes_home)
    identity = runtime.identity_text()
    identity_line = identity.line
    if not isinstance(identity_line, str) or not identity_line.strip():
        if identity.source == "served":
            logger.info(
                "agentchat plugin: identity line not placed yet for profile=%s — /v1/texts "
                "unreachable and nothing cached; it is placed when the server answers",
                label,
            )
        else:
            logger.warning("agentchat plugin: profile=%s — %s", label, identity.missing)
        return
    # The template: a block an earlier release wrote without a handle line is
    # this agent's when its text is that template, filled in, exactly.
    template = identity.template
    actual = runtime.identity.handle

    try:
        result = follow_soul(
            handle=actual, identity_line=identity_line, template=template, text_label=text_name(identity.source)
        )
    except AnchorError as exc:
        _say_once("", "kit", str(exc), "agentchat plugin: SOUL.md identity line not followed for profile=%s — %s", label, exc)
        return
    path = result.path
    outcome = result.outcome
    if outcome == "updated":
        logger.info("agentchat plugin: SOUL.md — %s (handle=@%s)", result.detail, actual)
    elif outcome == "unchanged" or outcome == "setup_offer":
        logger.debug("agentchat plugin: SOUL.md — %s", result.detail)
    elif outcome == "absent":
        try:
            placement = place_soul_anchor(identity_line, handle=actual, template=template, text_label=text_name(identity.source))
        except AnchorError as exc:
            logger.warning("agentchat plugin: SOUL.md identity line not placed for profile=%s — %s", label, exc)
            return
        if placement.ok:
            logger.info(
                "agentchat plugin: SOUL.md identity anchor backfilled for non-wizard install path "
                "(handle=@%s, path=%s)",
                actual,
                placement.path,
            )
        else:
            logger.warning("agentchat plugin: SOUL.md identity line not placed for profile=%s — %s", label, placement.detail)
    elif outcome == "other_agent":
        from .config import cli_prefix

        cmd = cli_prefix(runtime.hermes_home)
        logger.error(
            "agentchat plugin: IDENTITY MISMATCH for profile=%s — %s holds @%s's AgentChat block, but "
            "this profile's saved key authenticates as @%s: the key or that block is another agent's. "
            "Fix the key (`%s login`) or remove that block (then `%s doctor --fix` places @%s's). "
            "Until then, messages this profile sends are attributed to @%s.",
            label,
            path,
            result.claimed,
            actual,
            cmd,
            cmd,
            actual,
            actual,
        )
    elif outcome == "hand_edited":
        from .soul_anchor import hand_edited_remedy

        _say_once(
            path,
            outcome,
            result.detail,
            "agentchat plugin: SOUL.md — %s. It is yours: not refreshed; %s.",
            result.detail,
            hand_edited_remedy(path),
        )
    elif outcome == "several_blocks":
        _say_once(
            path,
            outcome,
            result.detail,
            "agentchat plugin: SOUL.md — %s: one write would remove the others. Remove the extra blocks by "
            "hand; the gateway then keeps the one left current.",
            result.detail,
        )
    elif outcome == "edited_meanwhile":
        logger.info("agentchat plugin: SOUL.md — %s; tried again when the served texts next change", result.detail)
    else:
        logger.warning("agentchat plugin: SOUL.md identity line not followed for profile=%s — %s", label, result.detail)


def _split_identity(config: Any) -> str | None:
    """Why the socket and the hosted tools would act as two agents, or ``None``.

    With a key file the key source hands Hermes' MCP client the key file's key.
    When the source is not on (``secrets.agentchat`` missing and not placeable,
    disabled, or this Hermes has no secret sources), the client takes
    ``AGENTCHAT_API_KEY`` from the profile's settings instead: a different key
    there is another agent (:func:`.credentials.tools_key_conflict`, the line
    ``doctor`` shows too). The block is placed first when it is missing, as
    every activation does; a block this activation places is used by the
    hosted tools after Hermes applies secret sources again — the re-pull right
    after plugin discovery when it is placed while the gateway loads plugins,
    else the next gateway restart. A ``config.yaml`` Hermes cannot read is a
    refusal with its error (said once), never an exception out of
    ``register()``.
    """
    if getattr(config, "api_key_source", KEY_SOURCE_STORE) != KEY_SOURCE_STORE:
        return None
    home = getattr(config, "hermes_home", None)
    api_key = getattr(config, "api_key", None)
    if home is None or not isinstance(api_key, str):
        return None
    from .credentials import key_bridge, tools_key_conflict

    ready, _why = key_bridge()
    if ready:
        return None
    return tools_key_conflict(home, api_key)


# Profiles (their home keys) whose pre-0.3 key the server accepted at the
# activation attempt running now (:func:`_adopt_legacy_key`): the one-time
# carry-over of the tools (:func:`_reconcile_mcp_registration`) needs it.
_legacy_verified: set[str] = set()


def _adopt_legacy_key(config: Any) -> Literal["go", "copy", "retry"]:
    """A key found only under the pre-v3 name: verify it with the server, then
    copy it into the key file (:func:`.credentials.adopt_legacy_key`), before
    the runtime starts. Never writes ``.env`` (``doctor --fix`` removes the old
    line).

    * The server must accept the key (``GET /v1/agents/me``, the wizard's
      check): a revoked key is never saved. Unreachable or refused:
      ``"retry"`` (the runtime would fail the same way).
    * A named profile adopts only if it was set up as an agent before this
      activation (:func:`.config.was_set_up_as_agent`): a ``.env`` that
      ``hermes profile create`` copied must not take the default profile's
      agent before the default profile starts. Said; ``"go"`` (it runs, and
      its next start, set up by this one, adopts).
    * ``"copy"``: another profile's key file holds the key — this profile is
      that agent's copy and must not start. ``"retry"`` also when another
      profile is adopting the same key right now. ``"go"``: adopted, or not
      adopted for a reason that is said; the activation goes on, as before 0.7
      (the hosted tools then lack the key).
    """
    from . import signup
    from .config import cli_prefix, was_set_up_as_agent
    from .credentials import adopt_legacy_key
    from .profile_scope import hermes_root_of

    home = getattr(config, "hermes_home", None)
    api_key = getattr(config, "api_key", None)
    if home is None or not isinstance(api_key, str):
        return "go"
    cmd = cli_prefix(home)
    api_base = getattr(config, "api_base", "")
    from .key_store import same_home

    if not same_home(home, hermes_root_of(home)) and not was_set_up_as_agent(home):
        logger.warning(
            "agentchat plugin: only the pre-v3 name %s is set in profile %s, which was not set up as "
            "an AgentChat agent before — the key is not copied into its key file now (a .env that "
            "`hermes profile create` copied would take another profile's agent); the hosted tools "
            "cannot authenticate until `%s doctor --fix` moves it, or this profile's next start does",
            ENV_API_KEY_LEGACY,
            profile_label(home),
            cmd,
        )
        return "go"
    try:
        checked = signup.check_key(api_key, api_base=api_base)
    except Exception as exc:
        logger.info(
            "agentchat plugin: the pre-v3 key could not be checked with AgentChat (%s) — tried again "
            "shortly",
            exc,
        )
        return "retry"
    if not checked.ok:
        logger.warning(
            "agentchat plugin: AgentChat does not accept the key under the pre-v3 name %s (%s) — it is "
            "not saved; run `%s login` with a current key",
            ENV_API_KEY_LEGACY,
            checked.message or checked.code,
            cmd,
        )
        return "retry"
    _legacy_verified.add(hermes_home_key(home))
    outcome = adopt_legacy_key(
        home=home,
        api_key=api_key,
        api_base=api_base,
        handle=checked.handle or "",
        lock_dir=getattr(config, "lock_dir", None),
    )
    if outcome.status == "copy":
        return "copy"
    if outcome.status == "busy":
        logger.info(
            "agentchat plugin: another profile is saving the same pre-v3 key right now — "
            "trying again shortly"
        )
        return "retry"
    if outcome.status == "adopted":
        logger.info(
            "agentchat plugin: %s was the only key name set — the key is now also in %s, which the "
            "hosted tools read (restart the gateway if they still report an unset variable); "
            "`%s doctor --fix` removes the old line from the profile .env",
            ENV_API_KEY_LEGACY,
            outcome.path,
            cmd,
        )
    elif outcome.status == "unsupported":
        logger.warning(
            "agentchat plugin: only the pre-v3 name %s is set and %s, so the hosted tools cannot "
            "authenticate — run `%s login`",
            ENV_API_KEY_LEGACY,
            outcome.detail,
            cmd,
        )
    else:
        logger.warning(
            "agentchat plugin: only the pre-v3 name %s is set, and the key was not copied into the "
            "key file (%s) — the hosted tools cannot authenticate until it is; `%s doctor --fix` "
            "moves it",
            ENV_API_KEY_LEGACY,
            outcome.detail,
            cmd,
        )
    return "go"


def _reconcile_key_store(config: Any) -> None:
    """Keep this profile's key file in shape at activation. Never writes ``.env``.

    * A key file moved here (profile renamed, home restored elsewhere) gets
      this profile recorded in it.
    * A key file the group or others can read is made ``0600``.

    (A key found only under the pre-v3 name is copied into the key file once
    the runtime has verified it: :func:`_adopt_legacy_key`.) Non-fatal:
    failures are logged with the fix.
    """
    from . import key_store
    from .config import cli_prefix

    home = getattr(config, "hermes_home", None)
    if home is None or getattr(config, "api_key_source", KEY_SOURCE_STORE) != KEY_SOURCE_STORE:
        return
    cmd = cli_prefix(home)
    try:
        read = key_store.read_store(home)
        if read.status == "moved" and read.key is not None:
            path = key_store.write_store(
                home, api_key=read.key.api_key, handle=read.key.handle, api_base=read.key.api_base
            )
            logger.info(
                "agentchat plugin: the key file was saved for %s; it now records this profile (%s)",
                read.origin_home,
                path,
            )
        elif read.loose_mode and key_store.tighten(home):
            logger.info("agentchat plugin: %s made readable by you only", read.path)
    except key_store.KeyStoreError as exc:
        logger.warning(
            "agentchat plugin: the key file could not be updated (%s) — run `%s doctor`", exc, cmd
        )


def _reconcile_secrets_block(runtime: Any) -> None:
    """Ensure ``secrets.agentchat`` in this profile's ``config.yaml``.

    It turns on the key source and tells Hermes to keep
    ``AGENTCHAT_API_KEY`` out of every command the agent runs — also for a
    profile whose key is still in its ``.env``. Placed before the hosted
    tools' entry, which needs it. Non-fatal: failures are logged with the fix.
    """
    try:
        from .placement import place_secrets
    except ImportError:
        return
    step = place_secrets()
    if step.failed:
        logger.warning(
            "agentchat plugin: secrets.agentchat not placed for profile=%s — %s",
            profile_label(runtime.hermes_home),
            step.detail,
        )
    elif step.status == "changed":
        logger.info("agentchat plugin: %s", step.detail)


_said_not_allowed: set[str] = set()


def _reconcile_mcp_registration(runtime: Any) -> None:
    """Keep ``mcp_servers.agentchat`` as decided, in this profile's ``config.yaml``.

    The entry is written only once a person decided whether the agent may use
    the AgentChat tools on its own (:mod:`.allow`); with nobody present (this
    path: a gateway or any other Hermes process activating) nothing is added,
    with two exceptions, both recorded decisions:

    * a profile set up before 0.3 (only the old ``AGENTCHATME_API_KEY``, no
      entry) gets the entry with ``trust: full``, once
      (:func:`.allow.first_placement`), when the server accepted that key at
      this activation (:func:`_adopt_legacy_key`), in any process — the
      gateway or a CLI chat — whether or not the key file took it: it was set
      up when the tools acted without asking;
    * a recorded ``trust: full`` not written yet (a yes applied in two steps,
      or a carry-over, interrupted) is written: by the gateway once Hermes
      runs no AgentChat server for the profile (:func:`.allow.start_completion`),
      by another process only while no gateway runs for the account. One that
      cannot be written is given up after a few attempts
      (:func:`.allow.note_write_failure`).

    An entry that is there gets the client header once when it has none (the
    entry is static, so this writes at most once per install); a user-managed
    entry is never touched. The gateway's config reconcile connects a new
    entry within about a minute. Non-fatal: failures are logged with the fix.
    """
    try:
        from .allow import (
            complete_pending,
            first_placement,
            gateway_lives,
            held_by_this_process,
            note_write_failure,
            read_switch,
            reapply_in_two_steps,
            start_completion,
        )
        from .mcp_registration import McpRegistrationError, inspect_entry
        from .placement import place_mcp
        from .runtime import keeps_placements_current
    except ImportError:
        logger.debug("agentchatme_hermes.placement not importable; skipping MCP registration")
        return
    api_base = getattr(getattr(runtime, "config", None), "api_base", None)
    if not isinstance(api_base, str) or not api_base:
        return
    home = runtime.hermes_home
    label = profile_label(home)
    switch = read_switch(home, api_base=api_base)
    if switch.awaits_restart is not None and keeps_placements_current(runtime) and held_by_this_process(switch):
        # This gateway kept the earlier connection while its AgentChat runtime
        # was down; now that it runs, it applies the yes itself, in two steps.
        reapply_in_two_steps(ProfileScope.capture(home), api_base=api_base)
        return
    if switch.state == "applying":
        if keeps_placements_current(runtime):
            # This attempt runs in the profile's scope: the thread gets a copy of it.
            start_completion(ProfileScope.capture(home), api_base=api_base)
            logger.info("agentchat plugin: applying the allowed AgentChat tools for profile=%s", label)
        elif not gateway_lives(home, api_base=api_base):
            complete_pending(home, api_base=api_base, wait=False)
        return
    decision = None
    try:
        entry = inspect_entry(api_base=api_base)
    except McpRegistrationError:
        entry = None
    if entry is not None:
        # Only a pre-0.3 key the server accepted at this activation
        # (_adopt_legacy_key) — never a copied .env line nobody verified.
        verified = hermes_home_key(home) in _legacy_verified
        decision = first_placement(home, entry=entry, legacy_key_set=verified)
    step = place_mcp(api_base, decision=decision)
    if step.failed and decision == "carried_over":
        note_write_failure(home, step.detail, api_base=api_base)
    elif step.failed:
        logger.warning("agentchat plugin: hosted tools not registered for profile=%s — %s", label, step.detail)
    elif decision == "carried_over" and step.outcome == "added":
        logger.info(
            "agentchat plugin: AgentChat tools allowed for profile=%s — carried over from your setup under "
            "an earlier release (mcp_servers.agentchat trust: full; done once, recorded in %s)",
            label,
            _record_path(home),
        )
    elif step.outcome == "missing":
        _say_not_allowed_yet(runtime, label)
    elif step.status == "changed":
        logger.info("agentchat plugin: %s", step.detail)
    else:
        logger.debug("agentchat plugin: mcp registration %s — %s", step.status, step.detail)


def _record_path(home: Any) -> Any:
    from .allow import record_path

    return record_path(home)


def _say_not_allowed_yet(runtime: Any, label: str) -> None:
    """Once per process and profile, at WARNING in every process: the agent
    answers nothing on AgentChat until the person allows the tools (a CLI chat
    included, whose person may never run the gateway)."""
    key = str(runtime.hermes_home)
    if key in _said_not_allowed:
        return
    _said_not_allowed.add(key)
    from .allow import operator_route, who
    from .config import cli_prefix

    handle = getattr(getattr(runtime, "identity", None), "handle", None)
    api_base = getattr(getattr(runtime, "config", None), "api_base", None)
    logger.warning(
        "agentchat plugin: AgentChat tools are not allowed yet for profile=%s (%s): no AgentChat work "
        "starts until the person allows them — /agentchat allow in a chat with it, or `%s allow` in a "
        "terminal. %s",
        label,
        who(handle),
        cli_prefix(runtime.hermes_home),
        operator_route(api_base if isinstance(api_base, str) else None),
    )


# NOTE: there is intentionally NO ``on_session_end`` hook in this
# module. The first spike showed that Hermes fires on_session_end on
# EVERY individual session ending — TUI sessions, cron jobs, adapter
# chats. Wiring our runtime.stop() to that hook killed the WS daemon
# mid-conversation, dropping inbound deliveries from peers. Daemon
# threads (daemon=True) die cleanly with the process on real shutdown;
# no explicit cleanup hook is needed or wanted. If a future version of
# Hermes ever exposes a per-process "the gateway itself is exiting"
# hook (vs per-session "a session ended"), THAT would be the right
# integration point — but it does not exist as of 2026-05.
