"""Top-level shim for the Hermes git-clone install path.

When a user runs::

    hermes plugins install --enable agentchatme/agentchat-hermes

Hermes does ``git clone`` into ``~/.hermes/plugins/agentchat/`` and loads
this file as ``hermes_plugins.agentchat`` via
:func:`hermes_cli.plugins.PluginManager._load_directory_module` (see
``hermes_cli/plugins.py:1138``). Hermes's ``hermes plugins install`` does
NOT pip-install our dependencies — it only clones the repo. So this
module owns two jobs:

1. **Lazy-install the agentchatme Python SDK** if the import would fail.
   Same self-bootstrapping pattern Hermes uses internally for optional
   adapters (``hermes_cli/setup.py:1054`` for ``neutts``, ``:1480`` for
   ``modal``, ``:1535`` for ``daytona``). On a PyPI-installed plugin the
   SDK is already a hard dep, so the ``import agentchatme`` succeeds
   immediately and no subprocess fires.

2. **Re-export ``register``** from the canonical
   :mod:`agentchatme_hermes` package which lives as a sibling
   subdirectory in the cloned repo. The relative import ``from
   .agentchatme_hermes`` resolves through Hermes's
   ``submodule_search_locations`` (set to the cloned plugin directory),
   so this works from the git-clone path; on PyPI installs this file
   is never loaded — Hermes's entry-point loader invokes
   ``agentchatme_hermes:register`` directly via the
   ``hermes_agent.plugins`` entry point declared in ``pyproject.toml``.

Production hardening (v0.1.3):

* The lazy install is wrapped in an exclusive file lock so two Hermes
  processes starting concurrently don't race on ``site-packages``.
* Retries 3 times with exponential backoff (1s, 3s, 9s) on transient
  pip failures (DNS blip, PyPI 503, etc.) before surfacing a hard error.
* Prefers ``uv pip install`` when ``uv`` is available — Hermes's venv was
  built with uv, so uv-native install is faster and more compatible than
  stock pip in that environment. Falls back to ``python -m pip install``
  otherwise.
* User-visible install-starting line goes to ``stderr`` via ``print()``,
  not the logging module — module-load-time emission via ``logger.info``
  often loses to default WARNING root config and the user wouldn't see
  the 5-10 second pause was an install. Stderr is unbuffered and always
  reaches the user's terminal.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_SDK_REQUIREMENT = "agentchatme>=1.0.1,<2"
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1  # attempts use base ** (n-1): 1s, 3s, 9s — see _BACKOFF_MULTIPLIER
_BACKOFF_MULTIPLIER = 3


def _resolve_install_cmd() -> list[str]:
    """Return the argv that installs the SDK into Hermes's running Python.

    Prefer ``uv pip install --python sys.executable`` when ``uv`` is on
    PATH (Hermes installs uv as part of its own bootstrap, so it's
    almost always present). Falls back to stock ``python -m pip install``
    so the shim works even when uv is absent.

    Critical: the install must land in ``sys.executable``'s site-packages,
    NOT a different Python on PATH. Both branches pass ``sys.executable``
    explicitly to lock the install target to the venv Hermes is running.
    """
    uv_path = shutil.which("uv")
    if uv_path:
        return [uv_path, "pip", "install", "--quiet", "--python", sys.executable, _SDK_REQUIREMENT]
    return [sys.executable, "-m", "pip", "install", "--quiet", _SDK_REQUIREMENT]


def _try_install_once() -> None:
    """One pip install attempt. Raises ``CalledProcessError`` on failure."""
    subprocess.check_call(_resolve_install_cmd())


def _ensure_sdk_installed(
    *,
    max_attempts: int = _MAX_ATTEMPTS,
    sleep_fn=time.sleep,
    install_fn=_try_install_once,
) -> None:
    """Install ``agentchatme`` into the running Python if it's missing.

    Returns immediately if the import already works. Otherwise serializes
    the install across concurrent plugin loads via a file lock, retries
    on transient pip failure with exponential backoff, and surfaces a
    clear ``RuntimeError`` if every attempt fails.

    The ``sleep_fn`` and ``install_fn`` parameters are dependency-injected
    for unit tests.
    """
    try:
        import agentchatme
        return
    except ImportError:
        pass

    # File lock — serialize concurrent installs so two Hermes processes
    # don't race on site-packages. fcntl is Unix-only; on Windows we
    # degrade to best-effort without locking. Hermes's documented happy
    # paths (Linux / macOS / WSL2) are all Unix, so the degraded branch
    # is purely a safety valve for native Windows beta users.
    lock_fd = None
    try:
        import fcntl  # type: ignore[import-not-found, unused-ignore]

        lock_path = Path(__file__).resolve().parent / ".sdk-install.lock"
        # ``open`` in mode ``w`` always creates the file if absent and
        # truncates it; the file's content doesn't matter, only the
        # OS-level lock on the inode. We never read/write the contents.
        # The file handle outlives the try-block — it's released in the
        # finally below — so we deliberately do NOT use a `with` here.
        lock_fd = open(lock_path, "w")  # noqa: SIM115 — see comment above
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        # Lock unavailable — proceed unprotected. Worst-case is two
        # concurrent installs, which pip generally handles cleanly via
        # its own atomic site-packages writes.
        if lock_fd is not None:
            with contextlib.suppress(Exception):
                lock_fd.close()
            lock_fd = None

    try:
        # Re-check inside the lock — the sibling process that we waited
        # on might have just installed the SDK successfully.
        try:
            import agentchatme  # noqa: F401
            return
        except ImportError:
            pass

        print(
            f"agentchatme-hermes: installing {_SDK_REQUIREMENT} into "
            f"{sys.executable} (one-time, runs on first plugin load)",
            file=sys.stderr,
            flush=True,
        )

        last_error: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                install_fn()
                return
            except subprocess.CalledProcessError as e:
                last_error = e
                if attempt < max_attempts:
                    wait = _BACKOFF_BASE_SECONDS * (_BACKOFF_MULTIPLIER ** (attempt - 1))
                    print(
                        f"agentchatme-hermes: SDK install attempt {attempt}/{max_attempts} "
                        f"failed (pip exit {e.returncode}); retrying in {wait}s",
                        file=sys.stderr,
                        flush=True,
                    )
                    sleep_fn(wait)

        # Every attempt exhausted — surface a clear, actionable error.
        # The relative import below would otherwise fail with the less
        # informative ``ModuleNotFoundError: No module named 'agentchatme'``.
        cmd_str = " ".join(_resolve_install_cmd())
        raise RuntimeError(
            f"agentchatme-hermes: failed to install the agentchatme SDK after "
            f"{max_attempts} attempts. Last pip exit status: "
            f"{getattr(last_error, 'returncode', '?')}. "
            f"Run the install manually: {cmd_str}"
        ) from last_error
    finally:
        if lock_fd is not None:
            with contextlib.suppress(ImportError, OSError):
                import fcntl  # type: ignore[import-not-found, unused-ignore]

                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                lock_fd.close()


# Bootstrap the SDK BEFORE the re-export below, since ``adapter.py``
# imports ``agentchatme`` at module top-level. Skip when the
# environment variable ``AGENTCHATME_HERMES_SKIP_BOOTSTRAP=1`` is set —
# this is the seam tests use to load the shim without firing pip.
if os.environ.get("AGENTCHATME_HERMES_SKIP_BOOTSTRAP") != "1":
    _ensure_sdk_installed()


# ─── Re-export the canonical register() ──────────────────────────────────
#
# Hermes's ``_load_directory_module`` imports this file with
# ``submodule_search_locations`` set to the cloned plugin directory, so
# the relative ``from .agentchatme_hermes`` resolves into the package
# subdirectory inside the clone.
#
# This file is ALSO walked by pytest's collector during local
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
