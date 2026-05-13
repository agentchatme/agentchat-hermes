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
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

# Pinned to match pyproject.toml dependency declaration.
_SDK_REQUIREMENT = "agentchatme>=1.0.1,<2"


def _ensure_sdk_installed() -> None:
    """Install agentchatme if importing it would fail.

    Idempotent. Tries ``pip`` first (the universal path); falls back
    to ``uv pip`` if the venv was built with uv and doesn't ship pip
    — which is the case for the standard Hermes install (verified
    against the upstream installer script).

    The install always lands in the same environment ``sys.executable``
    points at, regardless of which tool runs it.
    """
    try:
        import agentchatme  # noqa: F401
        return
    except ImportError:
        pass

    logger.info("agentchatme SDK not installed; installing %s", _SDK_REQUIREMENT)

    # Path 1: pip via the current interpreter. Works on any venv that
    # has pip available (most do; the uv-built ones do not).
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", _SDK_REQUIREMENT],
            stderr=subprocess.DEVNULL,
        )
        return
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Path 2: uv. Hermes' canonical installer builds the venv with uv
    # (see scripts/install.sh upstream), so pip is unavailable but uv
    # itself exists at ~/.local/bin/uv. Search the standard locations
    # since plugin loaders may strip PATH.
    uv_bin = (
        shutil.which("uv")
        or _existing_path("/root/.local/bin/uv")
        or _existing_path(os.path.expanduser("~/.local/bin/uv"))
        or _existing_path("/usr/local/bin/uv")
    )
    if uv_bin is not None:
        subprocess.check_call(
            [
                uv_bin,
                "pip",
                "install",
                "--quiet",
                "--python",
                sys.executable,
                _SDK_REQUIREMENT,
            ]
        )
        return

    raise RuntimeError(
        f"Could not install {_SDK_REQUIREMENT} — neither pip nor uv is "
        "available to the plugin loader. Install the SDK manually via "
        f"`uv pip install --python {sys.executable} '{_SDK_REQUIREMENT}'`, "
        "then restart Hermes."
    )


def _existing_path(path: str) -> str | None:
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


_ensure_sdk_installed()

# Re-export from the canonical package. The relative import only
# resolves when this file is loaded as part of a package (which is
# how Hermes' directory-install loader sets it up via
# ``submodule_search_locations``). In other import contexts (pytest
# crawling the repo root, ad-hoc ``python -c "import __init__"``),
# the import would fail with "attempted relative import with no known
# parent package" — we tolerate that silently because non-Hermes
# contexts have no business with these symbols.
try:
    from .agentchatme_hermes import __version__, register  # type: ignore[no-redef]

    __all__ = ["__version__", "register"]
except ImportError:
    __all__ = []
