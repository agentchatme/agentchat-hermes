"""Plugin registration entry — wires the integration into a Hermes process.

Called once per Hermes profile by the plugin loader (details at the end
of this docstring). Two registration modes, chosen based on which Hermes
process is loading us:

* **Gateway mode** (the long-lived ``hermes gateway run`` process, AND it
  successfully grabbed the per-account WS leader lock): full runtime —
  identity resolution, the served texts, the WS daemon that wakes, the
  invoker that gates and starts turns, the bundled manual as a skill.

* **CLI mode** (every other Hermes process — TUI/REPL, named subcommands,
  one-shot agents, Hermes-spawned child Python that inherits
  ``_HERMES_GATEWAY=1`` from its parent, or a second gateway whose flock
  acquisition lost the race): light runtime — identity only, no WS, no
  invoker. The skill is still registered; the agent's tools are the hosted
  MCP server declared in ``config.yaml`` and are available in every mode.
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
and account — see :mod:`.profile_scope`.
"""
from __future__ import annotations

import inspect
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from ._version import __version__
from .config import ENV_API_KEY, ENV_API_KEY_LEGACY, ConfigError, load_config
from .kit import kit_problem
from .leader_lock import is_hermes_gateway_process
from .profile_scope import profile_context, profile_label, resolve_hermes_home

if TYPE_CHECKING:
    from pathlib import Path

    from .runtime import Runtime

logger = logging.getLogger(__name__)

# How often a gateway process without a working configuration re-checks
# for one. Cheap when no key exists (a settings read); when a key exists
# but the server is unreachable the watcher backs off exponentially up to
# ACTIVATION_RETRY_MAX_S so a flapping network does not spam the log.
ACTIVATION_RETRY_S = 30.0
ACTIVATION_RETRY_MAX_S = 300.0


def register(ctx: Any) -> None:
    """Hermes plugin entry point. See module docstring."""
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

    # Receiving is the SDK kit's receive loop: an agentchatme without it can
    # neither receive nor place the identity line. Say so once, loudly, and
    # keep the CLI so `hermes agentchat doctor` says it too.
    problem = kit_problem()
    if problem is not None:
        logger.error("agentchat plugin: not receiving — %s", problem)
        _register_surface(ctx)
        return

    hermes_home = resolve_hermes_home()
    profile = profile_label(hermes_home)
    gateway_mode = _is_gateway_context(ctx)

    # The manual is registered unconditionally as the ``agentchat:agentchat``
    # skill. Tools are not registered here: they are the hosted MCP server
    # declared in the profile's config.yaml (see mcp_registration).
    _register_surface(ctx)

    watcher = ActivationWatcher(
        hermes_home=hermes_home,
        profile=profile,
        gateway_mode=gateway_mode,
        host_llm=host_llm_accessor(ctx),
    )
    _close_on_unload(ctx, watcher)
    outcome = watcher.try_activate()

    if outcome == "active":
        return

    if gateway_mode:
        # Long-lived process: keep trying. A gateway that boots before the
        # wizard has run, or before the network is up, activates by
        # itself the moment configuration and connectivity exist. Before
        # 0.2.3111111 that gateway stayed CLI-only until a restart — the
        # most common reason a freshly installed profile never received.
        watcher.start()
        logger.info(
            "agentchat plugin: %s for profile=%s (%s) — gateway will "
            "re-check every %.0fs and activate without a restart.",
            "no API key configured" if outcome == "unconfigured" else "runtime did not start",
            profile,
            hermes_home,
            ACTIVATION_RETRY_S,
        )
    else:
        logger.info(
            "agentchat plugin: %s for profile=%s (%s) — CLI-only in this "
            "process. Run `hermes agentchat register` (new account) or "
            "`hermes agentchat login` (existing key) in this profile.",
            "no API key configured" if outcome == "unconfigured" else "runtime did not start",
            profile,
            hermes_home,
        )


def _register_surface(ctx: Any) -> None:
    """Register the bundled manual as a skill. No tools — see module docstring."""
    try:
        from .skills import register_skill

        register_skill(ctx)
    except ImportError:
        logger.debug("agentchatme_hermes.skills not yet present; skipping skill registration")


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
    ) -> None:
        self._runtime: Runtime | None = None
        self._host_llm = host_llm
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

    @property
    def runtime(self) -> Runtime | None:
        return self._runtime

    def try_activate(self) -> str:
        """One attempt. Returns ``"active"``, ``"unconfigured"``, ``"failed"``
        or ``"closed"`` (the plugin was unloaded)."""
        with self._state_lock:
            if self._closed:
                return "closed"
            if self._runtime is not None:
                return "active"
        try:
            config = load_config(hermes_home=self._hermes_home)
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

        runtime = _try_start_runtime(
            config, gateway_mode=self._gateway_mode, host_llm=self._host_llm
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
        # The identity line is served (GET /v1/texts); the anchor is
        # reconciled as soon as texts are loaded — now when the server (or
        # the cache) answered, later when the first fetch succeeds — and
        # again whenever the served line changes.
        _reconcile_soul_anchor_when_texts_arrive(runtime)
        _reconcile_key_name(config)
        _reconcile_mcp_registration(runtime)

        logger.info(
            "agentchat plugin registered (version=%s, profile=%s, home=%s, "
            "handle=@%s, api_base=%s, mode=%s)",
            __version__,
            self._profile,
            self._hermes_home,
            runtime.identity.handle,
            config.api_base,
            "gateway" if self._gateway_mode else "cli",
        )
        return "active"

    def next_delay(self) -> float:
        """Backoff for the watcher thread: flat while unconfigured, exponential after failures."""
        if self._failures == 0:
            return ACTIVATION_RETRY_S
        return min(ACTIVATION_RETRY_MAX_S, ACTIVATION_RETRY_S * (2.0 ** (self._failures - 1)))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="agentchat-activation", daemon=True
        )
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
        # A late activation builds the runtime from this thread: do it in the
        # profile's own context, as register() did (multiplex: the launch
        # profile's environment is not this profile's).
        with profile_context(self._hermes_home):
            while not self._stop.wait(self.next_delay()):
                try:
                    if self.try_activate() in ("active", "closed"):
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
    config: Any, *, gateway_mode: bool, host_llm: Callable[[], Any] | None = None
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
    try:
        from .runtime import get_runtime

        runtime = get_runtime(config, gateway_mode=gateway_mode, host_llm=host_llm)
        runtime.start()
    except Exception as exc:
        logger.error(
            "agentchat plugin: runtime startup failed — there is no live "
            "inbound until it starts. Reason: %s. See the Hermes errors log for the full traceback. "
            "Try `hermes agentchat doctor` to diagnose, or `hermes "
            "agentchat login` to rotate to a known-good key.",
            exc,
            exc_info=True,
        )
        return None
    return runtime


def _reconcile_soul_anchor_when_texts_arrive(runtime: Any) -> None:
    """Run :func:`_reconcile_soul_anchor` as soon as the served texts exist.

    ``runtime.on_texts`` calls back immediately when texts are already
    loaded and again on every change; a runtime double without it (tests)
    is reconciled directly.
    """
    on_texts = getattr(runtime, "on_texts", None)
    if callable(on_texts):
        on_texts(lambda _texts: _reconcile_soul_anchor(runtime))
    else:
        _reconcile_soul_anchor(runtime)


def _reconcile_soul_anchor(runtime: Any) -> None:
    """Backfill the SOUL.md identity anchor, and flag an identity mismatch.

    The wizard writes the anchor on register/login (``cli.py``). This
    is the non-wizard path: a user who set ``AGENTCHAT_API_KEY``
    directly (env var, hand-edited profile ``.env``, container secrets,
    scripted setup) reaches the plugin's startup with a valid runtime +
    resolved handle but no anchor in SOUL.md. We write it on their
    behalf — with the identity line the server serves for this agent
    (``runtime.identity_line()``); without texts there is nothing to
    place yet, and the runtime calls again when they arrive.

    When an anchor IS present we leave the file alone — a user who
    hand-edited the block made an explicit choice. But if the anchor
    names a *different* handle than the key authenticates as, this
    profile believes it is one agent while the server sees another.
    That is never intentional, and it is exactly how one fleet ended up
    with a profile labelled *Growth* sending as *Operations*. We log at
    ERROR and leave the anchor untouched: rewriting it would hide the
    credential mix-up; ``hermes agentchat doctor`` reports it too.

    Non-fatal — anchor failures are logged, not raised. The plugin's
    primary mechanics (WS, tools, skill) work independent of SOUL.md.
    """
    try:
        from .soul_anchor import (
            AnchorError,
            anchor_is_current,
            has_anchor,
            read_anchor_handle,
            resolve_soul_path,
            write_soul_anchor,
        )
    except ImportError:
        logger.debug("agentchatme_hermes.soul_anchor not importable; skipping backfill")
        return

    identity_line = runtime.identity_line() if callable(getattr(runtime, "identity_line", None)) else None
    if not isinstance(identity_line, str) or not identity_line.strip():
        logger.info(
            "agentchat plugin: identity line not placed yet for profile=%s — /v1/texts "
            "unreachable and nothing cached; it is placed when the server answers",
            profile_label(runtime.hermes_home),
        )
        return
    # The served template recognises a block an earlier release wrote
    # without a handle line, whatever the current wording is.
    template_fn = getattr(runtime, "identity_line_template", None)
    template = template_fn() if callable(template_fn) else None
    if not isinstance(template, str):
        template = None

    try:
        if has_anchor():
            anchored = read_anchor_handle(template=template)
            actual = runtime.identity.handle
            if anchored is not None and anchored != actual:
                logger.error(
                    "agentchat plugin: IDENTITY MISMATCH for profile=%s — "
                    "%s says this agent is @%s but AGENTCHAT_API_KEY "
                    "authenticates as @%s. This profile is configured with "
                    "another agent's key (or another agent's SOUL.md). Fix "
                    "the key with `hermes agentchat login` in this profile, "
                    "or re-run `hermes agentchat register`. Messages this "
                    "profile sends will be attributed to @%s.",
                    profile_label(runtime.hermes_home),
                    resolve_soul_path(),
                    anchored,
                    actual,
                    actual,
                )
                return
            if anchored is not None and anchor_is_current(identity_line, template=template) is False:
                # Our own block from an older release: same handle, other
                # text. The served identity line is the one every
                # integration places; refresh it. A hand-edited block
                # (``anchored is None``) is the user's and is left alone.
                path = write_soul_anchor(identity_line, handle=actual)
                logger.info(
                    "agentchat plugin: SOUL.md identity line refreshed to the served "
                    "text (handle=@%s, path=%s)",
                    actual,
                    path,
                )
            return
        path = write_soul_anchor(identity_line, handle=runtime.identity.handle)
        logger.info(
            "agentchat plugin: SOUL.md identity anchor backfilled for "
            "non-wizard install path (handle=@%s, path=%s)",
            runtime.identity.handle,
            path,
        )
    except (AnchorError, OSError) as exc:
        logger.warning(
            "agentchat plugin: SOUL.md anchor backfill failed (%s). The "
            "agent will lack AgentChat awareness outside AgentChat-"
            "triggered turns. Run `hermes agentchat login` to invoke the "
            "wizard's anchor write, or hand-edit SOUL.md.",
            exc,
        )


def _reconcile_key_name(config: Any) -> None:
    """Mirror a legacy ``AGENTCHATME_API_KEY`` into ``AGENTCHAT_API_KEY`` once.

    The hosted MCP declaration authenticates with ``Bearer
    ${AGENTCHAT_API_KEY}``; a profile configured before v3 has only the
    legacy name, so without this the socket would connect and every tool
    call would fail closed on an unset variable. Writes through Hermes'
    own ``.env`` helpers; a no-op where Hermes is not importable or the
    canonical name already exists.
    """
    if getattr(config, "api_key_source", ENV_API_KEY) != ENV_API_KEY_LEGACY:
        return
    try:
        from hermes_cli.config import get_env_value, save_env_value
    except ImportError:
        logger.debug("hermes_cli.config not importable; skipping key-name migration")
        return
    try:
        from .placement import migrate_legacy_key

        if migrate_legacy_key(save_env_value, get_env_value):
            logger.info(
                "agentchat plugin: %s was the only key name set — mirrored into %s so the "
                "hosted tools can authenticate (restart the gateway if they still report an "
                "unset variable)",
                ENV_API_KEY_LEGACY,
                ENV_API_KEY,
            )
    except Exception as exc:
        logger.warning(
            "agentchat plugin: could not mirror %s into %s (%s). Run `hermes agentchat "
            "login` in this profile, or copy the value by hand.",
            ENV_API_KEY_LEGACY,
            ENV_API_KEY,
            exc,
        )


def _reconcile_mcp_registration(runtime: Any) -> None:
    """Ensure ``mcp_servers.agentchat`` exists in this profile's ``config.yaml``,
    with current client headers.

    The agent's tools live on the hosted MCP server; without this entry a
    wake starts a turn that has no AgentChat tools. The wizard writes it;
    this is the non-wizard path (upgrade from pre-v3, env-var-direct
    install, an entry an earlier release placed without the client
    headers). Adds an absent entry and sets the two client headers on ours;
    a user-managed entry is never touched. The gateway's config watcher
    connects a new entry within about a minute; changed headers are used
    once Hermes reconnects the hosted tools. Non-fatal: failures are logged
    with the fix.
    """
    try:
        from .placement import place_mcp
    except ImportError:
        logger.debug("agentchatme_hermes.placement not importable; skipping MCP registration")
        return
    api_base = getattr(getattr(runtime, "config", None), "api_base", None)
    if not isinstance(api_base, str) or not api_base:
        return
    step = place_mcp(api_base)
    if step.failed:
        logger.warning(
            "agentchat plugin: hosted tools not registered for profile=%s — %s",
            profile_label(runtime.hermes_home),
            step.detail,
        )
    elif step.status == "changed":
        logger.info("agentchat plugin: %s", step.detail)
    else:
        logger.debug("agentchat plugin: mcp registration %s — %s", step.status, step.detail)


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
