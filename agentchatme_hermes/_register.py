"""Plugin registration entry — wires the integration into a Hermes process.

Called once per Hermes profile by the plugin loader (details at the end
of this docstring). Two registration modes, chosen based on which Hermes
process is loading us:

* **Gateway mode** (the long-lived ``hermes gateway run`` process, AND it
  successfully grabbed the per-account WS leader lock): full runtime —
  identity resolution, the served texts, the WS daemon that wakes, the
  invoker that gates and starts turns, the manual as a skill (kept current
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
from .profile_scope import ProfileScope, ProfileScopeError, profile_label, resolve_hermes_home

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
    gateway_mode = _is_gateway_context(ctx)
    problem = kit_problem()
    if problem is not None:
        logger.error("agentchat plugin: not receiving — %s", problem)
        # The key source still masks a copied key for this process's tools.
        _find_copy(scope)
        _register_surface(ctx, hermes_home, place=False)
        return

    profile = profile_label(hermes_home)

    # The manual is registered unconditionally as the ``agentchat:agentchat``
    # skill: this profile's kept-current file. Only the gateway writes it
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

    reason = {
        "unconfigured": "no API key configured",
        "refused": "the key this profile holds is refused (the error above says why)",
    }.get(outcome, "runtime did not start")
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
    else:
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
            if config is not None and getattr(config, "api_key_source", KEY_SOURCE_STORE) == ENV_API_KEY_LEGACY:
                adopted = _adopt_legacy_key(config)
                if adopted == "copy":
                    # Another profile's key file took this key first: this
                    # profile is its copy, refused with the copy sentence.
                    config = load_config(hermes_home=self._hermes_home)
                elif adopted == "retry":
                    # Another profile is adopting the same key right now: start
                    # nothing until it is decided which profile is the agent.
                    self._failures += 1
                    return "failed"
        except KeyRefused as exc:
            # Not acting as an agent this profile must not be: no socket, no
            # placement. Said once; the watcher keeps checking, so fixing it
            # (logout, login) takes effect without a restart.
            message = str(exc)
            if message != self._refusal:
                self._refusal = message
                logger.error(
                    "agentchat plugin: not starting for profile=%s — %s", self._profile, message
                )
            return "refused"
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
        # The identity line is served (GET /v1/texts); the anchor is
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
            "handle=@%s, api_base=%s, mode=%s)",
            __version__,
            self._profile,
            self._hermes_home,
            runtime.identity.handle,
            config.api_base,
            "gateway" if self._gateway_mode else "cli",
        )
        return "active"

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
    try:
        from .runtime import get_runtime

        runtime = get_runtime(config, gateway_mode=gateway_mode, host_llm=host_llm, scope=scope)
        runtime.start()
    except Exception as exc:
        from pathlib import Path as _Path

        from .config import cli_prefix

        home = getattr(config, "hermes_home", None)
        cmd = cli_prefix(_Path(home)) if home else "hermes agentchat"
        logger.error(
            "agentchat plugin: runtime startup failed — there is no live "
            "inbound until it starts. Reason: %s. See the Hermes errors log for the full traceback. "
            "Try `%s doctor` to diagnose, or `%s login` to rotate to a known-good key.",
            exc,
            cmd,
            cmd,
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
    """Keep this agent's SOUL.md identity block at the served line; backfill it
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

    Without texts there is nothing to place yet; the runtime calls again when
    they arrive. Non-fatal: failures are logged, not raised.
    """
    try:
        from .soul_anchor import AnchorError, follow_soul, place_soul_anchor
    except ImportError:
        logger.debug("agentchatme_hermes.soul_anchor not importable; skipping backfill")
        return

    label = profile_label(runtime.hermes_home)
    identity_line = runtime.identity_line() if callable(getattr(runtime, "identity_line", None)) else None
    if not isinstance(identity_line, str) or not identity_line.strip():
        logger.info(
            "agentchat plugin: identity line not placed yet for profile=%s — /v1/texts "
            "unreachable and nothing cached; it is placed when the server answers",
            label,
        )
        return
    # The served template: a block an earlier release wrote without a handle
    # line is this agent's when its text is that template, filled in, exactly.
    template_fn = getattr(runtime, "identity_line_template", None)
    template = template_fn() if callable(template_fn) else None
    if not isinstance(template, str):
        template = None
    actual = runtime.identity.handle

    try:
        result = follow_soul(handle=actual, identity_line=identity_line, template=template)
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
            placement = place_soul_anchor(identity_line, handle=actual, template=template)
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
        logger.info("agentchat plugin: SOUL.md — %s; tried again on the next texts change", result.detail)
    else:
        logger.warning("agentchat plugin: SOUL.md identity line not followed for profile=%s — %s", label, result.detail)


def _adopt_legacy_key(config: Any) -> Literal["go", "copy", "retry"]:
    """A key found only under the pre-v3 name: copy it into the key file, before
    the runtime starts (:func:`.credentials.adopt_legacy_key`). Never writes
    ``.env`` (``doctor --fix`` removes the old line).

    ``"copy"``: another profile's key file holds the key — this profile is that
    agent's copy and must not start. ``"retry"``: another profile is adopting the
    same key right now — start nothing until that is decided. ``"go"``: adopted,
    or not adopted for another reason, which is said; the activation goes on, as
    before 0.7 (the hosted tools then lack the key).
    """
    from .config import cli_prefix
    from .credentials import adopt_legacy_key

    home = getattr(config, "hermes_home", None)
    api_key = getattr(config, "api_key", None)
    if home is None or not isinstance(api_key, str):
        return "go"
    cmd = cli_prefix(home)
    outcome = adopt_legacy_key(
        home=home,
        api_key=api_key,
        api_base=getattr(config, "api_base", ""),
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

    (A key found only under the pre-v3 name is copied into the key file
    before the runtime starts: :func:`_adopt_legacy_key`.) Non-fatal:
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


def _reconcile_mcp_registration(runtime: Any) -> None:
    """Ensure ``mcp_servers.agentchat`` exists in this profile's ``config.yaml``
    and names this client (``X-AgentChat-Client: hermes``).

    The agent's tools live on the hosted MCP server; without this entry a
    wake starts a turn that has no AgentChat tools. The wizard writes it;
    this is the non-wizard path (upgrade from pre-v3, env-var-direct
    install, an entry an earlier release placed without the client
    header). Adds an absent entry, and the client header to ours when it has
    none — the entry is static, so this writes at most once per install; a
    user-managed entry is never touched. The gateway's config watcher
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
