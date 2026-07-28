"""Client-attribution contract shared by every Hermes network path."""

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


def test_plugin_manifests_match_release() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in ("plugin.yaml", "agentchatme_hermes/plugin.yaml"):
        manifest = (root / relative).read_text(encoding="utf-8")
        assert f"version: {__version__}" in manifest
        assert "agentchatme>=1.0.321,<2" in manifest
