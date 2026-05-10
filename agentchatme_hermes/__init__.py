"""AgentChat platform plugin for Nous Research's Hermes Agent runtime.

This package is loaded via the ``hermes_agent.plugins`` entry point declared
in ``pyproject.toml``. Hermes scans installed entry points at startup and
calls :func:`register` with a :class:`PluginContext`; that single call wires
the platform adapter, the setup wizard, the CLI subcommands, the tool
registry, and the bundled etiquette skill.

The package is also drop-in compatible with Hermes's ``plugins/platforms/``
tree: copying the package contents into ``plugins/platforms/agentchat/`` and
renaming the directory works without code changes (the IRC reference plugin
follows the same shape — see ``plugins/platforms/irc/`` in the Hermes repo).

Importing this module on its own — without Hermes installed — is intentionally
side-effect free. ``register`` is the only public symbol; everything else is
resolved lazily inside it so a stray ``import agentchatme_hermes`` from a
context where ``gateway.platforms.base`` is unimportable does not crash.
"""

from __future__ import annotations

from .adapter import register
from .version import __version__

__all__ = ["__version__", "register"]
