"""Tests for ``agentchatme_hermes.mcp_registration``.

Pure config-dict operations plus the Hermes-backed install path with
``hermes_cli.config`` stubbed, and the lockstep between the config.yaml entry,
the repository's ``mcp.json`` and Hermes' loader rules.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from agentchatme_hermes import mcp_registration as reg

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestEntry:
    def test_entry_shape_references_the_key_variable_not_a_value(self) -> None:
        entry = reg.server_entry()
        assert entry == {
            "url": "https://api.agentchat.me/mcp",
            "headers": {"Authorization": "Bearer ${AGENTCHAT_API_KEY}"},
        }
        assert "ac_live" not in json.dumps(entry)

    def test_hosted_url_follows_api_base(self) -> None:
        assert reg.hosted_mcp_url("http://localhost:3000/") == "http://localhost:3000/mcp"

    def test_manual_snippet_is_the_same_entry(self) -> None:
        snippet = reg.manual_snippet()
        assert "mcp_servers:" in snippet
        assert "  agentchat:" in snippet
        assert 'url: "https://api.agentchat.me/mcp"' in snippet
        assert 'Authorization: "Bearer ${AGENTCHAT_API_KEY}"' in snippet


class TestDescribeEnsureRemove:
    def test_missing_then_added_then_present(self) -> None:
        config: dict[str, Any] = {}
        assert reg.describe_entry(config) == "missing"
        assert reg.ensure_entry(config) == "added"
        assert config["mcp_servers"]["agentchat"] == reg.server_entry()
        assert reg.describe_entry(config) == "ours"
        assert reg.ensure_entry(config) == "present"

    def test_other_servers_are_kept(self) -> None:
        config: dict[str, Any] = {"mcp_servers": {"github": {"url": "https://x/mcp"}}}
        reg.ensure_entry(config)
        assert set(config["mcp_servers"]) == {"github", "agentchat"}

    def test_user_managed_entry_is_never_modified(self) -> None:
        theirs = {"url": "https://proxy.example/mcp", "headers": {"Authorization": "Bearer abc"}}
        config: dict[str, Any] = {"mcp_servers": {"agentchat": dict(theirs)}}
        assert reg.describe_entry(config) == "user_managed"
        assert reg.ensure_entry(config) == "user_managed"
        assert config["mcp_servers"]["agentchat"] == theirs
        assert reg.remove_entry(config) is False

    def test_disabled_entry_is_left_alone(self) -> None:
        config: dict[str, Any] = {"mcp_servers": {"agentchat": {**reg.server_entry(), "enabled": False}}}
        assert reg.describe_entry(config) == "disabled"
        assert reg.ensure_entry(config) == "disabled"

    def test_tuned_entry_still_counts_as_ours(self) -> None:
        tuned = {**reg.server_entry(), "trust": "untrusted", "timeout": 120}
        config: dict[str, Any] = {"mcp_servers": {"agentchat": tuned}}
        assert reg.describe_entry(config) == "ours"
        assert reg.ensure_entry(config) == "present"
        assert config["mcp_servers"]["agentchat"] == tuned

    def test_env_colon_reference_counts_as_ours(self) -> None:
        entry = {"url": reg.hosted_mcp_url(), "headers": {"authorization": "Bearer ${env:AGENTCHAT_API_KEY}"}}
        assert reg.describe_entry({"mcp_servers": {"agentchat": entry}}) == "ours"

    def test_remove_only_ours_and_drops_empty_section(self) -> None:
        config: dict[str, Any] = {}
        reg.ensure_entry(config)
        assert reg.remove_entry(config) is True
        assert "mcp_servers" not in config
        config = {"mcp_servers": {"github": {}, "agentchat": reg.server_entry()}}
        assert reg.remove_entry(config) is True
        assert config["mcp_servers"] == {"github": {}}

    def test_non_mapping_servers_section_is_replaced_only_when_adding(self) -> None:
        config: dict[str, Any] = {"mcp_servers": None}
        assert reg.ensure_entry(config) == "added"
        assert reg.describe_entry(config) == "ours"


class _FakeHermesConfig:
    def __init__(self, initial: dict[str, Any]) -> None:
        self.on_disk = initial
        self.saved: list[dict[str, Any]] = []

    def load_config(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.on_disk))

    def save_config(self, config: dict[str, Any]) -> None:
        self.saved.append(config)
        self.on_disk = config

    def read_user_config_raw(self) -> dict[str, Any]:
        return self.on_disk


@pytest.fixture
def fake_hermes(monkeypatch: pytest.MonkeyPatch) -> _FakeHermesConfig:
    fake = _FakeHermesConfig({"model": {"default": "x"}})
    module = types.ModuleType("hermes_cli.config")
    module.load_config = fake.load_config  # type: ignore[attr-defined]
    module.save_config = fake.save_config  # type: ignore[attr-defined]
    module.read_user_config_raw = fake.read_user_config_raw  # type: ignore[attr-defined]
    package = types.ModuleType("hermes_cli")
    package.config = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    return fake


class TestHermesBacked:
    def test_install_writes_through_hermes_once(self, fake_hermes: _FakeHermesConfig) -> None:
        assert reg.install_entry() == "added"
        assert len(fake_hermes.saved) == 1
        assert fake_hermes.on_disk["mcp_servers"]["agentchat"] == reg.server_entry()
        assert fake_hermes.on_disk["model"] == {"default": "x"}, "other sections survive"
        assert reg.install_entry() == "present"
        assert len(fake_hermes.saved) == 1, "present means no second write"

    def test_inspect_reads_the_raw_file(self, fake_hermes: _FakeHermesConfig) -> None:
        assert reg.inspect_entry() == "missing"
        reg.install_entry()
        assert reg.inspect_entry() == "ours"

    def test_uninstall_removes_ours_only(self, fake_hermes: _FakeHermesConfig) -> None:
        reg.install_entry()
        assert reg.uninstall_entry() is True
        assert "mcp_servers" not in fake_hermes.on_disk
        assert reg.uninstall_entry() is False

    def test_without_hermes_the_error_carries_the_manual_snippet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "hermes_cli", None)
        monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
        with pytest.raises(reg.McpRegistrationError, match="mcp_servers:"):
            reg.install_entry()


class TestPortableDeclaration:
    def test_mcp_json_matches_hermes_loader_rules(self) -> None:
        doc = reg.mcp_json_document()
        # hermes_cli/agent_plugins.py: top-level keys exactly {$schema, mcpServers}.
        assert set(doc) == {"$schema", "mcpServers"}
        assert doc["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
        server = doc["mcpServers"]["agentchat"]
        assert set(server) <= {"type", "url", "headers"}  # _REMOTE_FIELDS
        assert server["type"] == "streamable-http"
        assert server["url"].startswith("https://")
        assert server["headers"] == {"Authorization": "Bearer ${AGENTCHAT_API_KEY}"}

    def test_repo_root_mcp_json_is_the_declaration(self) -> None:
        on_disk = json.loads((REPO_ROOT / "mcp.json").read_text(encoding="utf-8"))
        assert on_disk == reg.mcp_json_document()

    def test_config_yaml_entry_and_portable_entry_agree(self) -> None:
        portable = reg.mcp_json_document()["mcpServers"]["agentchat"]
        native = reg.server_entry()
        assert portable["url"] == native["url"]
        assert portable["headers"] == native["headers"]
