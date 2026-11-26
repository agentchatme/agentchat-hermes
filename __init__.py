"""Top-level shim for a directory install (``hermes plugins install``).

When a user runs::

    hermes plugins install agentchat                     # Hermes' plugin catalog
    hermes plugins install https://github.com/agentchatme/agentchat-hermes.git --ref "$sha"

Hermes clones this repo, scans the clone with its install-time security scan
(it shows the result first and asks to confirm a ``caution`` verdict: the
plugin runs a socket daemon and holds a key file; a ``dangerous`` verdict is
refused, ``--force`` included), installs it into
``~/.hermes/plugins/agentchat/`` and loads THIS file through its
directory-module loader (``hermes_cli/plugins_loader.py``; the module is
``hermes_plugins.agentchat`` for the first profile, a per-profile name for
others).

The plugin's one Python dependency, the ``agentchatme`` SDK, is declared in
pyproject.toml, and Hermes installs it: at install, reinstall and update it
asks first (``--yes-deps`` answers yes when headless), installs it into its
own environment before the plugin loads, and leaves the plugin disabled on a
no (Hermes v0.23.2 and later, the ``requires_hermes`` floor in plugin.yaml).
This file installs nothing. When the SDK is missing or older than this
release needs, ``register()`` logs one error and ``hermes agentchat doctor``
fails, both naming the command that installs it
(:func:`agentchatme_hermes.kit.sdk_update_command`).

This file only re-exports :func:`register` from :mod:`agentchatme_hermes`,
the canonical package living as a sibling subdirectory in the cloned repo.
The relative import resolves through Hermes's
``submodule_search_locations``.

On PyPI installs this file is never loaded — Hermes's entry-point loader
invokes :mod:`agentchatme_hermes` directly via the ``hermes_agent.plugins``
entry point declared in pyproject.toml.
"""
from __future__ import annotations

# The relative import only resolves when this file is loaded as part of a
# package (which is how Hermes' directory-install loader sets it up via
# ``submodule_search_locations``). In other import contexts (pytest crawling
# the repo root, ad-hoc ``python -c "import __init__"``), it fails with
# "attempted relative import with no known parent package" — tolerated
# silently because non-Hermes contexts have no business with these symbols.
# The package imports without the SDK, so a missing SDK is reported by
# ``register()``, not hidden here.
try:
    from .agentchatme_hermes import __version__, register  # type: ignore[no-redef]

    __all__ = ["__version__", "register"]
except ImportError:
    __all__ = []
