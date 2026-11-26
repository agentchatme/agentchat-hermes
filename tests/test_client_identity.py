"""Client-attribution contract shared by every Hermes network path."""

import re
from pathlib import Path
from typing import Any

import pytest

from agentchatme_hermes._version import __version__
from agentchatme_hermes.client_identity import hermes_client_identity


def test_hermes_identity_name_and_version() -> None:
    # The headers the SDK derives from it are asserted on the wire in
    # tests/test_wizard_signup.py.
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
    floors = set()
    for relative in ("plugin.yaml", "agentchatme_hermes/plugin.yaml"):
        manifest = (root / relative).read_text(encoding="utf-8")
        assert f"version: {__version__}" in manifest
        # pyproject.toml is the one declaration of the SDK: Hermes installs what it
        # declares, and reads a manifest's list only for a tree without [project].
        assert "pip_dependencies" not in manifest and "python_dependencies" not in manifest, relative
        floor = re.search(r'^requires_hermes: ">=(\d+\.\d+\.\d+)"$', manifest, re.MULTILINE)
        assert floor, f"{relative} must name the Hermes floor"
        floors.add(floor.group(1))
    assert len(floors) == 1, "both manifests name the same Hermes floor"
    (hermes_floor,) = floors
    assert f"**Requires** Hermes v{hermes_floor} or later" in (root / "README.md").read_text(encoding="utf-8")
    assert f'requires_hermes: ">={hermes_floor}"' in (root / "docs" / "hermes-coupling.md").read_text(encoding="utf-8")
    from agentchatme_hermes.kit import KIT_MIN_VERSION, SDK_REQUIREMENT

    assert pin == SDK_REQUIREMENT, "the pip command installs exactly what pyproject.toml declares"
    assert pin[len("agentchatme>="):].split(",", 1)[0] == KIT_MIN_VERSION, "kit.py names the same floor"


def test_the_shim_installs_nothing_and_re_exports_register(monkeypatch: pytest.MonkeyPatch) -> None:
    """The top-level ``__init__.py`` as Hermes' directory loader imports it (a
    package whose ``submodule_search_locations`` is the plugin directory):
    ``register`` is the package's, and nothing is installed or run."""
    import importlib.util
    import subprocess
    import sys

    import agentchatme_hermes

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the shim must not start a process")

    for name in ("check_call", "run", "Popen", "call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)
    root = Path(__file__).resolve().parents[1]
    shim_source = (root / "__init__.py").read_text(encoding="utf-8")
    assert "import subprocess" not in shim_source and "pip" not in shim_source.split('"""', 2)[2]
    name = "agentchat_shim_under_test"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    try:
        spec.loader.exec_module(module)
        assert module.__all__ == ["__version__", "register"]
        assert module.register.__module__ == f"{name}.agentchatme_hermes._register"
        assert module.register.__name__ == agentchatme_hermes.register.__name__
    finally:
        for loaded in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
            sys.modules.pop(loaded, None)
