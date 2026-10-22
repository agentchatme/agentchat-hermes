"""Plugin registration entry — wires the plugin into a Hermes process.

Called once per Hermes profile by the plugin loader (details at the end
of this docstring). Two registration modes, chosen based on which Hermes
process is loading us:

* **Gateway mode** (the long-lived ``hermes gateway run`` process,
  AND it successfully grabbed the per-account WS leader lock):
  full runtime — sync HTTP client, background WS daemon for live
  inbound delivery, agent invoker thread pool for waking the agent
  on each ``message.new`` frame, tools, skill.

* **CLI mode** (every other Hermes process — TUI/REPL, named
  subcommands, one-shot agents, Hermes-spawned child Python that
  inherits ``_HERMES_GATEWAY=1`` from its parent, or even a second
  gateway whose flock acquisition lost the race): light runtime —
  sync HTTP client and identity only, no WS, no invoker. Tools and
  skill are still registered so the agent can call
  ``agentchat_send_message`` etc., but live inbound stays exclusively
  with the leader-lock holder. This is the production-grade fix for
  the multi-WS-per-machine bug.

Mode selection has two layered signals (see :mod:`.leader_lock`):

1. ``_HERMES_GATEWAY=1`` env var, set by ``gateway/run.py:370`` at
   module load (before plugin discovery), and used by Hermes' own
   ``cli.py:538``. This is the authoritative "am I the gateway?"
   marker.
2. ``fcntl.flock(LOCK_EX | LOCK_NB)`` on the per-account lock file
   under ``<hermes root>/agentchat-locks/``. Even with the env var set,
   only one process can hold this OS-level lock for a given account at
   a time. Losers fall back to CLI mode.

The earlier 0.2.0 detector used ``ctx._manager._cli_ref is None``,
which silently misfired: Hermes sets ``_cli_ref`` *after* plugin
discovery, so it's ``None`` in both gateway and TUI processes when
``register()`` runs. That's why every TUI invocation was opening a
second WS. Fixed in 0.2.1.

There is NO ``on_session_end`` hook. Hermes fires that on every
individual session ending (TUI sessions, cron jobs, adapter chats —
not just process shutdown), which would spuriously stop the runtime
mid-flight. Daemon threads (``daemon=True``) die with the process on
real shutdown; no explicit cleanup hook is needed or wanted.

Called once per Hermes **profile**: once per process in a classic
gateway, once per served profile in a multiplexed gateway (Hermes keeps
one plugin manager per profile home). Every call resolves its own
profile home and its own ``AGENTCHATME_*`` settings and gets its own
:class:`runtime.Runtime`, keyed by profile and account — see
:mod:`.profile_scope`.
"""
from __future__ import annotations

import logging
from typing import Any

from ._version import __version__
from .config import ConfigError, load_config
from .leader_lock import is_hermes_gateway_process
from .profile_scope import profile_label, resolve_hermes_home

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Hermes plugin entry point. See module docstring."""
    # CLI subcommand is registered first and unconditionally so the
    # wizard remains reachable even when API key configuration is
    # missing or broken.
    try:
        from .cli import register_cli

        register_cli(ctx)
    except ImportError:
        # CLI module is built incrementally — tolerated during early
        # bring-up. Once cli.py lands this branch is unreachable.
        logger.debug("agentchatme_hermes.cli not yet present; skipping CLI registration")

    hermes_home = resolve_hermes_home()
    profile = profile_label(hermes_home)

    try:
        config = load_config(hermes_home=hermes_home)
    except ConfigError as exc:
        logger.warning(
            "agentchat plugin: configuration error (profile=%s) — running "
            "CLI-only. %s",
            profile,
            exc,
        )
        return

    if config is None:
        logger.info(
            "agentchat plugin: AGENTCHATME_API_KEY not set for profile=%s "
            "(%s) — running CLI-only. Run `hermes agentchat register` to "
            "create an account for this profile.",
            profile,
            hermes_home,
        )
        return

    gateway_mode = _is_gateway_context(ctx)

    runtime = _try_start_runtime(config, gateway_mode=gateway_mode)
    if runtime is None:
        # Runtime failed to start. We deliberately do NOT raise the
        # error up to the plugin loader — that would mark the whole
        # plugin as failed-to-load and hide it from `hermes plugins
        # list`, which then makes the issue invisible. Instead we log
        # at ERROR level (so it shows up in ~/.hermes/logs/errors.log
        # AND in journalctl), surface a status hint, and register the
        # plugin in degraded mode. The user can then run `hermes
        # agentchat doctor` to see a clear diagnostic and either
        # rotate keys, re-login, or check connectivity.
        logger.info(
            "agentchat plugin registered in degraded mode (version=%s). "
            "Tools, skill, and live inbound are disabled until the "
            "runtime can start — run `hermes agentchat doctor` to "
            "diagnose.",
            __version__,
        )
        return

    # Identity reconciliation. Wizard users (`hermes agentchat
    # register/login`) already got the SOUL.md anchor written at that
    # step; this backfills it for env-var-direct / scripted / container
    # setups — and refuses to stay quiet when the anchor and the key
    # disagree about who this profile is.
    _reconcile_soul_anchor(runtime)

    try:
        from .tools import register_tools

        register_tools(ctx, runtime)
    except ImportError:
        logger.debug("agentchatme_hermes.tools not yet present; skipping tool registration")

    try:
        from .skills import register_skill

        register_skill(ctx)
    except ImportError:
        logger.debug("agentchatme_hermes.skills not yet present; skipping skill registration")

    logger.info(
        "agentchat plugin registered (version=%s, profile=%s, home=%s, "
        "handle=@%s, api_base=%s, mode=%s)",
        __version__,
        profile,
        hermes_home,
        runtime.identity.handle,
        config.api_base,
        "gateway" if gateway_mode else "cli",
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


def _try_start_runtime(config: Any, *, gateway_mode: bool) -> Any:
    """Construct + start the runtime. Returns the runtime or ``None`` on failure.

    Reasons start can fail:
    * Invalid API key (UnauthorizedError on /v1/agents/me)
    * Network unreachable (ConnectionError)
    * Server schema regression (RuntimeError from _resolve_identity)
    * Hermes runtime helpers unimportable from inside this process

    All of these are recoverable — the user fixes the underlying issue
    and restarts the gateway. None of them should kill the whole plugin
    load; we want the CLI subcommand to remain reachable so the user
    can diagnose.
    """
    from .runtime import get_runtime

    try:
        runtime = get_runtime(config, gateway_mode=gateway_mode)
        runtime.start()
    except Exception as exc:
        logger.error(
            "agentchat plugin: runtime startup failed — the plugin will "
            "be inactive (no live inbound, no tools, no skill). Reason: %s. "
            "See ~/.hermes/logs/errors.log for the full traceback. "
            "Try `hermes agentchat doctor` to diagnose, or `hermes "
            "agentchat login` to rotate to a known-good key.",
            exc,
            exc_info=True,
        )
        return None
    return runtime


def _reconcile_soul_anchor(runtime: Any) -> None:
    """Backfill the SOUL.md identity anchor, and flag an identity mismatch.

    The wizard writes the anchor on register/login (``cli.py``). This
    is the non-wizard path: a user who set ``AGENTCHATME_API_KEY``
    directly (env var, hand-edited profile ``.env``, container secrets,
    scripted setup) reaches the plugin's startup with a valid runtime +
    resolved handle but no anchor in SOUL.md. We write it on their
    behalf.

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
            has_anchor,
            read_anchor_handle,
            resolve_soul_path,
            write_soul_anchor,
        )
    except ImportError:
        logger.debug("agentchatme_hermes.soul_anchor not importable; skipping backfill")
        return

    try:
        if has_anchor():
            anchored = read_anchor_handle()
            actual = runtime.identity.handle
            if anchored is not None and anchored != actual:
                logger.error(
                    "agentchat plugin: IDENTITY MISMATCH for profile=%s — "
                    "%s says this agent is @%s but AGENTCHATME_API_KEY "
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
        path = write_soul_anchor(runtime.identity.handle)
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
