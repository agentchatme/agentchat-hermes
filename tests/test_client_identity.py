"""Client-attribution contract shared by every Hermes network path."""

import logging
import re
from pathlib import Path
from typing import Any

import pytest

from agentchatme_hermes._version import __version__
from agentchatme_hermes.client_identity import hermes_client_identity


def test_hermes_identity_name_and_version() -> None:
    # The headers the SDK derives from it are asserted on the wire in
    # tests/test_wizard_sdk_start.py.
    identity = hermes_client_identity()

    assert identity.name == "hermes"
    assert identity.version == __version__


def _pyproject_sdk_pin(root: Path) -> str:
    """The ``agentchatme`` requirement from ``[project].dependencies``."""
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^\s*"(agentchatme[^"]*)",?\s*$', pyproject, re.MULTILINE)
    assert match, "pyproject.toml must pin agentchatme in [project].dependencies"
    return match.group(1)


def test_plugin_manifests_match_release() -> None:
    root = Path(__file__).resolve().parents[1]
    pin = _pyproject_sdk_pin(root)
    assert pin.startswith("agentchatme>=") and ",<2" in pin, pin
    for relative in ("plugin.yaml", "agentchatme_hermes/plugin.yaml"):
        manifest = (root / relative).read_text(encoding="utf-8")
        assert f"version: {__version__}" in manifest
        assert f"- {pin}" in manifest, f"{relative} must pin the SDK exactly as pyproject.toml does"
    # The git-clone shim installs the SDK itself; it must ask for the same release.
    shim = (root / "__init__.py").read_text(encoding="utf-8")
    assert f'_SDK_REQUIREMENT = "{pin}"' in shim


def _load_shim() -> Any:
    """The git-clone shim as Hermes loads it (with agentchatme 1.3+ installed
    its bootstrap is a no-op)."""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("agentchat_shim_under_test", root / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shim_upgrades_an_sdk_older_than_the_kit(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from importlib import metadata

    shim = _load_shim()
    installs: list[str] = []
    monkeypatch.setattr(shim, "_install_sdk", lambda: installs.append(shim._SDK_REQUIREMENT))
    installed = {"version": "1.2.1"}

    def version(name: str) -> str:
        assert name == "agentchatme"
        if installed["version"] is None:
            raise metadata.PackageNotFoundError(name)
        return installed["version"]

    monkeypatch.setattr(metadata, "version", version)
    shim._ensure_sdk_current()
    assert installs == ["agentchatme>=1.3.0,<2"], "1.2.1 has no kit: upgraded"
    for current in ("1.3.0", "1.3.1rc1", "1.10.0"):
        installed["version"] = current
        shim._ensure_sdk_current()
    installed["version"] = None  # not installed: the plain install path handles it
    shim._ensure_sdk_current()
    assert installs == ["agentchatme>=1.3.0,<2"]

    def failing() -> None:
        raise RuntimeError("no pip, no uv")

    monkeypatch.setattr(shim, "_install_sdk", failing)
    installed["version"] = "1.2.0"
    with caplog.at_level(logging.WARNING):
        shim._ensure_sdk_current()  # not fatal: register() and doctor report the missing kit
    assert any("could not upgrade the agentchatme SDK" in r.getMessage() for r in caplog.records)
