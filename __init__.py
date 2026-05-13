"""Top-level shim for the `hermes plugins install` (git-clone) path.

When a user runs::

    hermes plugins install --enable agentchatme/agentchat-hermes

Hermes ``git clone``s this repo into ``~/.hermes/plugins/agentchat/``
and loads THIS file as ``hermes_plugins.agentchat`` via
:meth:`hermes_cli.plugins.PluginManager._load_directory_module`.
``hermes plugins install`` does NOT pip-install our dependencies,
only clones the repo. So this module owns two jobs:

1. Lazy-install ``agentchatme`` if the import fails (mirrors the
   self-bootstrap pattern in ``hermes_cli/setup.py`` for optional
   adapters like ``modal`` and ``daytona``).
2. Re-export :func:`register` from :mod:`agentchatme_hermes` — the
   canonical package living as a sibling subdirectory in the cloned
   repo. The relative import resolves through Hermes's
   ``submodule_search_locations``.

On PyPI installs this file is never loaded — Hermes's entry-point
loader invokes :mod:`agentchatme_hermes` directly via the
``hermes_agent.plugins`` entry point declared in pyproject.toml.
"""
from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

# Pinned to match pyproject.toml dependency declaration.
_SDK_REQUIREMENT = "agentchatme>=1.0.1,<2"


def _ensure_sdk_installed() -> None:
    """Install agentchatme if importing it would fail.

    Idempotent. Pip runs as a subprocess of the current interpreter so
    the install lands in the same environment Hermes is running in.
    """
    try:
        import agentchatme  # noqa: F401
    except ImportError:
        logger.info("agentchatme SDK not installed; installing %s", _SDK_REQUIREMENT)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", _SDK_REQUIREMENT]
        )


_ensure_sdk_installed()

from .agentchatme_hermes import __version__, register

__all__ = ["__version__", "register"]
