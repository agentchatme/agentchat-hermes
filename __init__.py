"""Top-level shim for the `hermes plugins install` (git-clone) path.

When a user runs::

    hermes plugins install --enable agentchatme/agentchat-hermes

Hermes ``git clone``s this repo into ``~/.hermes/plugins/agentchat/``
and loads THIS file through its directory-module loader
(``hermes_cli/plugins_loader.py``; the module is ``hermes_plugins.agentchat``
for the first profile, a per-profile name for others). Older Hermes
releases only clone the repo; current ones ask before installing the
declared Python dependencies at install, reinstall or update
(``--yes-deps`` when headless) and leave the plugin disabled on a no.
Either way the SDK may be missing or too old, so this module owns two
jobs:

1. Lazy-install ``agentchatme`` if the import fails, or upgrade one older
   than the release with the integration kit (mirrors the self-bootstrap
   pattern in ``hermes_cli/setup.py`` for optional adapters like
   ``modal`` and ``daytona``).
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

# Pinned to match pyproject.toml's dependency declaration (a test keeps the
# two identical): the git-clone path installs the SDK from here. The plugin
# needs the SDK's integration kit (agentchatme.integration, since 1.3.0), and
# this release the kit as of 1.3.3 (a system notice is not a wake; a card
# without a preview is anchored at its last message).
_SDK_REQUIREMENT = "agentchatme>=1.3.3,<2"
_SDK_FLOOR = (1, 3, 3)


def _installed_sdk_version() -> tuple[int, ...] | None:
    """The installed ``agentchatme`` release, read from its metadata (the
    SDK is not imported: a later upgrade must be what this process loads);
    ``None`` when it is not installed."""
    from importlib import metadata

    try:
        raw = metadata.version("agentchatme")
    except metadata.PackageNotFoundError:
        return None
    parts: list[int] = []
    for piece in raw.split(".")[:3]:
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits or 0))
    return tuple(parts)


def _ensure_sdk_current() -> None:
    """Upgrade an ``agentchatme`` older than the kit's release. A failed
    upgrade is not fatal: the plugin then loads, says once that it cannot
    receive, and ``hermes agentchat doctor`` reports it."""
    installed = _installed_sdk_version()
    if installed is None or installed >= _SDK_FLOOR:
        return
    logger.info("agentchatme SDK is older than the integration kit's release; installing %s", _SDK_REQUIREMENT)
    try:
        _install_sdk()
    except Exception as exc:
        logger.warning("could not upgrade the agentchatme SDK (%s)", exc)


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
    _install_sdk()


def _install_sdk() -> None:
    """``pip install`` (else ``uv pip install``) the requirement into the
    environment ``sys.executable`` points at; raises when neither works."""
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


_ensure_sdk_current()
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
