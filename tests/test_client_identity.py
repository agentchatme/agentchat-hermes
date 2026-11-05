"""Client-attribution contract shared by every Hermes network path."""

import re
from pathlib import Path

from agentchatme_hermes._version import __version__
from agentchatme_hermes.client_identity import (
    HERMES_CLIENT_HEADERS,
    hermes_client_identity,
)


def test_hermes_identity_name_and_version() -> None:
    identity = hermes_client_identity()

    assert identity.name == "hermes"
    assert identity.version == __version__
    assert {
        "X-AgentChat-Client": "hermes",
        "X-AgentChat-Client-Version": __version__,
    } == HERMES_CLIENT_HEADERS


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
