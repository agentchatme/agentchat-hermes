"""Unit tests for the v0.1.2+ install shim at the repo root.

The shim (``./__init__.py``) is what Hermes loads after
``hermes plugins install`` git-clones the repo. It does two jobs:

1. Lazy-install the ``agentchatme`` SDK if missing
   (``_ensure_sdk_installed``).
2. Re-export ``register`` from the ``agentchatme_hermes`` package via a
   relative import that resolves through Hermes's
   ``submodule_search_locations``.

These tests exercise both jobs in isolation.

The shim runs ``_ensure_sdk_installed()`` at module-load time. The test
fixture sets ``AGENTCHATME_HERMES_SKIP_BOOTSTRAP=1`` and re-loads the
shim via :func:`importlib.util.spec_from_file_location` — the same
mechanism Hermes's ``_load_directory_module`` uses (see
``hermes_cli/plugins.py:1138-1172`` in the upstream repo). Skipping the
bootstrap at load lets us call ``_ensure_sdk_installed`` directly with
dependency-injected ``install_fn`` / ``sleep_fn`` for the failure-path
branches.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def shim(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load the root __init__.py via importlib, bootstrap skipped."""
    monkeypatch.setenv("AGENTCHATME_HERMES_SKIP_BOOTSTRAP", "1")
    sys.modules.pop("agentchatme_hermes_shim_under_test", None)

    spec = importlib.util.spec_from_file_location(
        "agentchatme_hermes_shim_under_test",
        str(REPO_ROOT / "__init__.py"),
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agentchatme_hermes_shim_under_test"] = mod
    spec.loader.exec_module(mod)

    yield mod

    sys.modules.pop("agentchatme_hermes_shim_under_test", None)


# ─── Re-export surface ─────────────────────────────────────────────────────


def test_root_shim_exports_register(shim: Any) -> None:
    """Hermes calls ``module.register(ctx)`` after loading. It must exist."""
    assert hasattr(shim, "register")
    assert callable(shim.register)
    assert hasattr(shim, "__version__")
    assert isinstance(shim.__version__, str)


# ─── _resolve_install_cmd ──────────────────────────────────────────────────


def test_resolve_install_cmd_prefers_uv_when_available(
    shim: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv is faster + uv-venv-compatible. Use it when present."""
    import shutil as _shutil

    monkeypatch.setattr(
        _shutil, "which", lambda name: "/fake/uv" if name == "uv" else None
    )
    cmd = shim._resolve_install_cmd()
    assert cmd[0] == "/fake/uv"
    assert cmd[1:3] == ["pip", "install"]
    assert sys.executable in cmd, (
        "uv must install into Hermes's Python (sys.executable), not whatever uv defaults to"
    )
    assert "agentchatme>=1.0.1,<2" in cmd


def test_resolve_install_cmd_falls_back_to_pip(
    shim: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No uv on PATH → use ``python -m pip install``."""
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda _name: None)
    cmd = shim._resolve_install_cmd()
    assert cmd[:3] == [sys.executable, "-m", "pip"]
    assert "install" in cmd
    assert "agentchatme>=1.0.1,<2" in cmd
    assert "--upgrade-strategy" not in cmd, (
        "dead --upgrade-strategy arg should not be present (it has no effect without -U)"
    )


# ─── _ensure_sdk_installed: control flow ───────────────────────────────────


def test_ensure_sdk_returns_immediately_when_installed(shim: Any) -> None:
    """SDK is in the dev environment — install_fn must not fire."""
    install_calls: list[int] = []

    def fake_install() -> None:
        install_calls.append(1)

    shim._ensure_sdk_installed(install_fn=fake_install)
    assert install_calls == []


def test_ensure_sdk_installs_when_missing(
    shim: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``import agentchatme`` raises, install_fn fires exactly once."""
    # ``None`` in sys.modules makes Python raise ModuleNotFoundError on import,
    # which is the ImportError shape our function catches.
    monkeypatch.setitem(sys.modules, "agentchatme", None)

    install_calls: list[int] = []

    def fake_install() -> None:
        install_calls.append(1)

    shim._ensure_sdk_installed(install_fn=fake_install)
    assert install_calls == [1]


def test_ensure_sdk_retries_on_transient_failure(
    shim: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pip fails twice then succeeds — verify exponential backoff fired."""
    monkeypatch.setitem(sys.modules, "agentchatme", None)

    attempts: list[int] = []

    def flaky_install() -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise subprocess.CalledProcessError(returncode=1, cmd=["pip", "install"])

    sleep_calls: list[float] = []

    shim._ensure_sdk_installed(install_fn=flaky_install, sleep_fn=sleep_calls.append)

    assert len(attempts) == 3, "should have retried until success"
    # Backoff: 1s after attempt 1 fails, 3s after attempt 2 fails. No sleep
    # after the last (successful) attempt.
    assert sleep_calls == [1, 3]


def test_ensure_sdk_raises_runtime_error_after_max_attempts(
    shim: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All attempts fail — surface a clear, actionable RuntimeError."""
    monkeypatch.setitem(sys.modules, "agentchatme", None)

    def always_fail() -> None:
        raise subprocess.CalledProcessError(returncode=42, cmd=["pip", "install"])

    with pytest.raises(RuntimeError) as excinfo:
        shim._ensure_sdk_installed(
            install_fn=always_fail, sleep_fn=lambda _s: None
        )

    msg = str(excinfo.value)
    assert "failed to install" in msg.lower()
    assert "agentchatme>=1.0.1,<2" in msg
    assert "Run the install manually" in msg, (
        "error message must point operators at the manual fix"
    )
    # Original pip exception preserved as cause for trace clarity.
    assert isinstance(excinfo.value.__cause__, subprocess.CalledProcessError)


def test_ensure_sdk_respects_custom_max_attempts(
    shim: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_attempts`` parameter is honored."""
    monkeypatch.setitem(sys.modules, "agentchatme", None)

    attempts: list[int] = []

    def always_fail() -> None:
        attempts.append(1)
        raise subprocess.CalledProcessError(returncode=1, cmd=["pip"])

    with pytest.raises(RuntimeError):
        shim._ensure_sdk_installed(
            install_fn=always_fail, sleep_fn=lambda _s: None, max_attempts=5
        )

    assert len(attempts) == 5


# ─── Drift guard: root plugin.yaml == package plugin.yaml ──────────────────


def test_plugin_yaml_root_matches_package() -> None:
    """The root ``plugin.yaml`` (consumed by ``hermes plugins install``
    after git clone) must stay byte-identical to
    ``agentchatme_hermes/plugin.yaml`` (consumed by ``register_skill`` /
    ``importlib.resources`` on PyPI installs).

    Drift here means operators see different manifests depending on
    install method. If you intentionally changed one, mirror the change
    to the other in the same commit.
    """
    root_path = REPO_ROOT / "plugin.yaml"
    pkg_path = REPO_ROOT / "agentchatme_hermes" / "plugin.yaml"

    assert root_path.exists(), f"missing root plugin.yaml at {root_path}"
    assert pkg_path.exists(), f"missing package plugin.yaml at {pkg_path}"

    root = root_path.read_bytes()
    pkg = pkg_path.read_bytes()

    assert root == pkg, (
        "plugin.yaml drift detected between repo root and "
        "agentchatme_hermes/. They must stay byte-identical to give "
        "the same install experience across `hermes plugins install` "
        "and `pip install agentchatme-hermes`."
    )
