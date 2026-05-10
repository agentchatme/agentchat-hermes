"""Top-level shim for the Hermes git-clone install path.

When a user runs::

    hermes plugins install --enable agentchatme/agentchat-hermes

Hermes does ``git clone`` into ``~/.hermes/plugins/agentchat/`` and loads
this file as ``hermes_plugins.agentchat`` via
:func:`hermes_cli.plugins.PluginManager._load_directory_module`. Hermes's
``hermes plugins install`` does NOT pip-install our dependencies — it only
clones the repo. So this module owns two jobs:

1. **Lazy-install the agentchatme Python SDK** if the import would fail.
   This is the same pattern Hermes uses internally for its own optional
   adapters (see ``hermes_cli/setup.py:1054`` for ``neutts``,
   ``hermes_cli/setup.py:1480`` for ``modal``, ``:1535`` for ``daytona``).
   On a PyPI-installed plugin the SDK is already a hard dep, so the
   ``import agentchatme`` succeeds immediately and no subprocess fires.

2. **Re-export ``register``** from the canonical
   :mod:`agentchatme_hermes` package which lives as a sibling
   subdirectory in the cloned repo. The relative import ``from
   .agentchatme_hermes`` resolves through Hermes's
   ``submodule_search_locations`` (set to the cloned plugin directory),
   so this works from the git-clone path; on PyPI installs this file
   is never loaded — Hermes's entry-point loader invokes
   ``agentchatme_hermes:register`` directly via the
   ``hermes_agent.plugins`` entry point declared in ``pyproject.toml``.

Importing this module on its own outside of Hermes is intentionally
side-effect-free apart from the SDK presence check; the SDK install
runs only when the import would have failed anyway, so a stray
``import`` from a non-Hermes context still resolves.
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


# ─── Lazy SDK install ─────────────────────────────────────────────────────
#
# `hermes plugins install` does git clone only; our `agentchatme>=1.0.1`
# runtime dep is not auto-installed. On first import after a fresh clone,
# `import agentchatme` raises ImportError. We catch it and run pip in the
# same Python (``sys.executable``) so the install lands in Hermes's venv,
# not the system Python. ``--quiet`` keeps the gateway log clean unless
# the install fails. ``--upgrade-strategy only-if-needed`` avoids
# clobbering a newer SDK an operator may have intentionally pinned.

_SDK_REQUIREMENT = "agentchatme>=1.0.1,<2"


def _ensure_sdk_installed() -> None:
    try:
        import agentchatme  # noqa: F401
        return
    except ImportError:
        pass

    logger.info(
        "agentchatme-hermes: SDK not present in this Python (%s); "
        "installing %s — this runs once on first plugin load. "
        "If this fails, run `pip install %s` manually in the Hermes venv.",
        sys.executable,
        _SDK_REQUIREMENT,
        _SDK_REQUIREMENT,
    )
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--upgrade-strategy",
                "only-if-needed",
                _SDK_REQUIREMENT,
            ]
        )
    except subprocess.CalledProcessError as e:
        # Surface a clear, actionable error before the relative import
        # below explodes with a less informative ImportError.
        raise RuntimeError(
            "agentchatme-hermes: failed to install the agentchatme SDK "
            f"({_SDK_REQUIREMENT}). Run the install manually in the "
            f"Hermes venv: `{sys.executable} -m pip install {_SDK_REQUIREMENT}`. "
            f"pip exit status: {e.returncode}"
        ) from e


_ensure_sdk_installed()


# ─── Re-export the canonical register() ──────────────────────────────────
#
# Hermes's ``_load_directory_module`` imports this file with
# ``submodule_search_locations`` set to the cloned plugin directory, so
# the relative ``from .agentchatme_hermes`` resolves into the package
# subdirectory inside the clone.
#
# But this file is ALSO walked by pytest's collector during local
# development (it sits in the repo root). Pytest imports it as a bare
# module without a parent package, so the relative import would raise
# ``ImportError: attempted relative import with no known parent
# package``. Fall back to the absolute import — which works in dev
# because the package is installed editable, and matches the PyPI
# install path where the entry point invokes ``agentchatme_hermes:register``
# directly without ever loading this shim.

try:
    from .agentchatme_hermes import __version__, register  # type: ignore[no-redef]
except ImportError:
    from agentchatme_hermes import __version__, register  # type: ignore[no-redef]

__all__ = ["__version__", "register"]
