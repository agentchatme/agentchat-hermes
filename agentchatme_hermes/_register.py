"""Plugin registration entry — wires the plugin into a Hermes process.

Called once per Hermes process by the plugin loader. Behavior:

* Always register the ``hermes agentchat`` CLI subcommand (the
  account-setup wizard) so users can configure even before any
  ``AGENTCHATME_API_KEY`` exists.
* If ``AGENTCHATME_API_KEY`` is unset, log a one-line CLI-only notice
  and return. The user can configure via the wizard and restart.
* Otherwise: start the runtime (background WS daemon + inbox + agent
  invoker), register the full tool surface, register the etiquette
  skill, and register the ``on_session_end`` hook so a graceful
  Hermes shutdown stops the WS daemon cleanly.

Idempotent within a process — :func:`runtime.get_runtime` is a
module-level singleton.
"""
from __future__ import annotations

import logging
from typing import Any

from ._version import __version__
from .config import ConfigError, load_config

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

    try:
        config = load_config()
    except ConfigError as exc:
        logger.warning(
            "agentchat plugin: configuration error — running CLI-only. %s", exc
        )
        return

    if config is None:
        logger.info(
            "agentchat plugin: AGENTCHATME_API_KEY not set — running CLI-only. "
            "Run `hermes agentchat register` to create an account."
        )
        return

    from .runtime import get_runtime

    runtime = get_runtime(config)
    runtime.start()

    # Non-interactive identity-activation hook. Wizard users
    # (`hermes agentchat register/login`) already got the SOUL.md
    # anchor written by cli._install_soul_anchor at that step. This
    # backfill is the safety net for env-var-direct / scripted /
    # container setups where the user set AGENTCHATME_API_KEY without
    # running the wizard.
    #
    # Idempotent and respectful: if our markers are already in SOUL.md
    # (even if the block content was hand-edited) we leave the file
    # alone. Only writes when the anchor is genuinely absent.
    _ensure_soul_anchor(runtime)

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

    ctx.register_hook("on_session_end", _on_session_end)

    logger.info(
        "agentchat plugin registered (version=%s, api_base=%s)",
        __version__,
        config.api_base,
    )


def _ensure_soul_anchor(runtime: Any) -> None:
    """Backfill the SOUL.md identity anchor when absent.

    The wizard writes the anchor on register/login (``cli.py``). This
    is the non-wizard path: a user who set ``AGENTCHATME_API_KEY``
    directly (env var, hand-edited ``~/.hermes/.env``, container
    secrets, scripted setup) reaches the plugin's startup with a
    valid runtime + resolved handle but no anchor in SOUL.md. We
    write it on their behalf.

    Respectful — checks for the marker pair first and skips when
    present. If a user explicitly removed the block while keeping
    their key, we do not silently re-add it on every restart.

    Non-fatal — anchor failures are logged as warnings, not raised.
    The plugin's primary mechanics (WS, tools, skill) work
    independent of SOUL.md content.
    """
    try:
        from .soul_anchor import AnchorError, has_anchor, write_soul_anchor
    except ImportError:
        logger.debug("agentchatme_hermes.soul_anchor not importable; skipping backfill")
        return

    try:
        if has_anchor():
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


def _on_session_end(**_kwargs: Any) -> None:
    """Hermes lifecycle hook — graceful stop of the WS daemon on shutdown.

    Called once per session-end. The runtime's :meth:`stop` is
    idempotent, so multiple calls (e.g., from a forced re-register
    during a hot reload) are safe.
    """
    try:
        from .runtime import get_existing_runtime

        runtime = get_existing_runtime()
        if runtime is not None:
            runtime.stop()
    except Exception as exc:
        logger.warning("agentchat plugin: error during runtime stop: %s", exc)
