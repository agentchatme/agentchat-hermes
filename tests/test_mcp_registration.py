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
from fake_keys import PREFIX

from agentchatme_hermes import mcp_registration as reg
from agentchatme_hermes._version import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent

# The entry an earlier release placed: Authorization only.
OLD_ENTRY = {"url": "https://api.agentchat.me/mcp", "headers": {"Authorization": "Bearer ${AGENTCHAT_API_KEY}"}}
CLIENT_HEADERS = {"X-AgentChat-Client": "hermes", "X-AgentChat-Client-Version": __version__}


class TestEntry:
    def test_entry_shape_references_the_key_variable_not_a_value(self) -> None:
        entry = reg.server_entry()
        assert entry == {
            "url": "https://api.agentchat.me/mcp",
            "headers": {"Authorization": "Bearer ${AGENTCHAT_API_KEY}", **CLIENT_HEADERS},
        }
        assert PREFIX not in json.dumps(entry)

    def test_client_headers_are_the_sdk_identity(self) -> None:
        """The same name and version the SDK sends on REST and the socket."""
        from agentchatme_hermes.client_identity import hermes_client_identity

        identity = hermes_client_identity()
        headers = reg.server_entry()["headers"]
        assert headers["X-AgentChat-Client"] == identity.name == "hermes"
        assert headers["X-AgentChat-Client-Version"] == identity.version == __version__

    def test_hosted_url_follows_api_base(self) -> None:
        assert reg.hosted_mcp_url("http://localhost:3000/") == "http://localhost:3000/mcp"

    def test_manual_snippet_is_the_same_entry(self) -> None:
        snippet = reg.manual_snippet()
        assert "mcp_servers:" in snippet
        assert "  agentchat:" in snippet
        assert 'url: "https://api.agentchat.me/mcp"' in snippet
        assert 'Authorization: "Bearer ${AGENTCHAT_API_KEY}"' in snippet
        assert '      X-AgentChat-Client: "hermes"\n' in snippet
        assert f'      X-AgentChat-Client-Version: "{__version__}"\n' in snippet


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

    def test_an_earlier_release_entry_is_upgraded_in_place(self) -> None:
        tuned = {**json.loads(json.dumps(OLD_ENTRY)), "trust": "untrusted", "timeout": 120}
        config: dict[str, Any] = {"mcp_servers": {"agentchat": tuned, "github": {"url": "https://x/mcp"}}}
        assert reg.describe_entry(config) == "ours"
        assert reg.entry_identity(tuned) == "missing"
        assert reg.ensure_entry(config) == "upgraded"
        entry = config["mcp_servers"]["agentchat"]
        assert entry == {
            "url": "https://api.agentchat.me/mcp",
            "headers": {"Authorization": "Bearer ${AGENTCHAT_API_KEY}", **CLIENT_HEADERS},
            "trust": "untrusted",
            "timeout": 120,
        }
        assert config["mcp_servers"]["github"] == {"url": "https://x/mcp"}
        assert reg.ensure_entry(config) == "present", "once current, nothing more to do"

    def test_an_older_version_is_refreshed_whatever_its_casing(self) -> None:
        entry = {
            "url": reg.hosted_mcp_url(),
            "headers": {
                "authorization": "Bearer ${AGENTCHAT_API_KEY}",
                "x-agentchat-client": "Hermes",
                "x-agentchat-client-version": "0.5.0",
                "X-Extra": "kept",
            },
        }
        config: dict[str, Any] = {"mcp_servers": {"agentchat": entry}}
        assert reg.entry_identity(entry) == "stale"
        assert reg.ensure_entry(config) == "upgraded"
        assert config["mcp_servers"]["agentchat"]["headers"] == {
            "authorization": "Bearer ${AGENTCHAT_API_KEY}",
            "X-Extra": "kept",
            **CLIENT_HEADERS,
        }

    def test_a_client_header_naming_another_client_is_left_alone(self) -> None:
        entry = {
            "url": reg.hosted_mcp_url(),
            "headers": {"Authorization": "Bearer ${AGENTCHAT_API_KEY}", "X-AgentChat-Client": "my-harness"},
        }
        config: dict[str, Any] = {"mcp_servers": {"agentchat": json.loads(json.dumps(entry))}}
        assert reg.entry_identity(entry) == "other"
        assert reg.ensure_entry(config) == "present"
        assert config["mcp_servers"]["agentchat"] == entry

    def test_user_managed_entry_without_the_header_is_not_touched(self) -> None:
        theirs = {"url": "https://proxy.example/mcp", "headers": {"Authorization": "Bearer ${AGENTCHAT_API_KEY}"}}
        config: dict[str, Any] = {"mcp_servers": {"agentchat": json.loads(json.dumps(theirs))}}
        assert reg.describe_entry(config) == "user_managed"
        assert reg.entry_identity(theirs) == "missing"
        assert reg.ensure_entry(config) == "user_managed"
        assert config["mcp_servers"]["agentchat"] == theirs

    def test_disabled_old_entry_is_not_upgraded(self) -> None:
        entry = {**json.loads(json.dumps(OLD_ENTRY)), "enabled": False}
        config: dict[str, Any] = {"mcp_servers": {"agentchat": entry}}
        assert reg.ensure_entry(config) == "disabled"
        assert "X-AgentChat-Client" not in config["mcp_servers"]["agentchat"]["headers"]

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ({}, "missing"),
            ({"headers": None}, "missing"),
            ({"headers": {"X-AgentChat-Client": "  "}}, "missing"),
            ({"headers": {"X-AgentChat-Client": "hermes"}}, "stale"),
            ({"headers": {"X-AgentChat-Client": "hermes", "X-AgentChat-Client-Version": __version__}}, "current"),
            ({"headers": {"X-AgentChat-Client": "openclaw"}}, "other"),
            ({"headers": {"X-AgentChat-Client": "hermes", "X-AgentChat-Client-Version": "99.0.0"}}, "current"),
            ({"headers": {"X-AgentChat-Client": "hermes", "X-AgentChat-Client-Version": "0.0.1"}}, "stale"),
            ({"headers": {"X-AgentChat-Client": "hermes", "X-AgentChat-Client-Version": "junk"}}, "stale"),
            ("not a mapping", "missing"),
        ],
    )
    def test_entry_identity(self, entry: Any, expected: str) -> None:
        assert reg.entry_identity(entry) == expected

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
    """``hermes_cli.config`` as the plugin uses it. ``load_config`` expands
    ``${VAR}`` from the environment like Hermes' does (a running Hermes has
    the profile's ``.env`` loaded); ``read_user_config_raw`` returns the file
    as written; ``save_config`` writes what it is given."""

    def __init__(self, initial: dict[str, Any]) -> None:
        self.on_disk = initial
        self.saved: list[dict[str, Any]] = []
        self.managed = False
        self.drop_saves = False

    def load_config(self) -> dict[str, Any]:
        import os
        import re

        text = json.dumps(self.on_disk)
        return json.loads(
            re.sub(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m[1], ""), text)
        )

    def save_config(self, config: dict[str, Any]) -> None:
        self.saved.append(json.loads(json.dumps(config)))
        if not self.drop_saves:
            self.on_disk = json.loads(json.dumps(config))

    def read_user_config_raw(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.on_disk))

    def is_managed(self) -> bool:
        return self.managed


@pytest.fixture
def fake_hermes(monkeypatch: pytest.MonkeyPatch) -> _FakeHermesConfig:
    # A running Hermes has the profile's .env in the environment.
    monkeypatch.setenv("AGENTCHAT_API_KEY", PREFIX + "secret_value_000000000000")
    fake = _FakeHermesConfig({"model": {"default": "x"}})
    module = types.ModuleType("hermes_cli.config")
    module.load_config = fake.load_config  # type: ignore[attr-defined]
    module.save_config = fake.save_config  # type: ignore[attr-defined]
    module.read_user_config_raw = fake.read_user_config_raw  # type: ignore[attr-defined]
    module.is_managed = fake.is_managed  # type: ignore[attr-defined]
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
        assert reg.inspect_entry_identity() == ("missing", None)
        reg.install_entry()
        assert reg.inspect_entry() == "ours"
        assert reg.inspect_entry_identity() == ("ours", "current")

    def test_the_fake_expands_like_hermes(self, fake_hermes: _FakeHermesConfig) -> None:
        """Guards the tests below: through the expanding loader our own entry
        would read as hand-written (a literal key)."""
        fake_hermes.on_disk = {"mcp_servers": {"agentchat": json.loads(json.dumps(OLD_ENTRY))}}
        import hermes_cli.config as hermes_config  # the fake

        assert reg.describe_entry(hermes_config.load_config()) == "user_managed"
        assert reg.describe_entry(fake_hermes.read_user_config_raw()) == "ours"

    def test_upgrade_keeps_the_key_a_template(self, fake_hermes: _FakeHermesConfig) -> None:
        fake_hermes.on_disk = {"mcp_servers": {"agentchat": json.loads(json.dumps(OLD_ENTRY))}}
        assert reg.install_entry() == "upgraded"
        written = json.dumps(fake_hermes.saved)
        assert "secret_value" not in written, "the key must never be written into config.yaml"
        assert fake_hermes.on_disk["mcp_servers"]["agentchat"]["headers"]["Authorization"] == "Bearer ${AGENTCHAT_API_KEY}"

    def test_logout_removes_ours_with_the_key_loaded_or_cleared(
        self, fake_hermes: _FakeHermesConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for value in (PREFIX + "loaded_000000000000", ""):
            monkeypatch.setenv("AGENTCHAT_API_KEY", value)
            fake_hermes.on_disk = {"mcp_servers": {"agentchat": reg.server_entry()}}
            assert reg.uninstall_entry() is True
            assert "mcp_servers" not in fake_hermes.on_disk

    def test_a_managed_hermes_gets_the_entry_to_add_and_no_write(self, fake_hermes: _FakeHermesConfig) -> None:
        fake_hermes.managed = True
        fake_hermes.on_disk = {"mcp_servers": {"agentchat": json.loads(json.dumps(OLD_ENTRY))}}
        with pytest.raises(reg.McpRegistrationError, match="managed by a package manager") as info:
            reg.install_entry()
        assert "X-AgentChat-Client" in str(info.value)
        assert fake_hermes.saved == []
        fake_hermes.on_disk = {"mcp_servers": {"agentchat": reg.server_entry()}}
        assert reg.install_entry() == "present", "nothing to write, nothing to refuse"

    def test_a_save_that_does_not_land_is_an_error(self, fake_hermes: _FakeHermesConfig) -> None:
        fake_hermes.drop_saves = True
        with pytest.raises(reg.McpRegistrationError, match="did not save"):
            reg.install_entry()

    def test_install_then_inspect_agree(self, fake_hermes: _FakeHermesConfig) -> None:
        """doctor --fix and the check after it read the same file the same way."""
        fake_hermes.on_disk = {"mcp_servers": {"agentchat": json.loads(json.dumps(OLD_ENTRY))}}
        assert reg.inspect_entry_identity() == ("ours", "missing")
        reg.install_entry()
        assert reg.inspect_entry_identity() == ("ours", "current")

    def test_install_upgrades_an_old_entry_with_one_write(self, fake_hermes: _FakeHermesConfig) -> None:
        fake_hermes.on_disk = {"model": {"default": "x"}, "mcp_servers": {"agentchat": json.loads(json.dumps(OLD_ENTRY))}}
        assert reg.inspect_entry_identity() == ("ours", "missing")
        assert reg.install_entry() == "upgraded"
        assert len(fake_hermes.saved) == 1
        assert fake_hermes.on_disk["mcp_servers"]["agentchat"]["headers"] == {
            "Authorization": "Bearer ${AGENTCHAT_API_KEY}",
            **CLIENT_HEADERS,
        }
        assert reg.install_entry() == "present"
        assert len(fake_hermes.saved) == 1

    def test_install_never_writes_a_user_managed_entry(self, fake_hermes: _FakeHermesConfig) -> None:
        theirs = {"url": "https://proxy.example/mcp", "headers": {"Authorization": "Bearer x"}}
        fake_hermes.on_disk = {"mcp_servers": {"agentchat": theirs}}
        assert reg.install_entry() == "user_managed"
        assert fake_hermes.saved == []
        assert reg.inspect_entry_identity() == ("user_managed", "missing")

    def test_uninstall_removes_ours_only(self, fake_hermes: _FakeHermesConfig) -> None:
        reg.install_entry()
        assert reg.uninstall_entry() is True
        assert "mcp_servers" not in fake_hermes.on_disk
        assert reg.uninstall_entry() is False

    def test_a_newer_version_is_never_downgraded(self) -> None:
        entry = {
            "url": reg.hosted_mcp_url(),
            "headers": {
                "Authorization": "Bearer ${AGENTCHAT_API_KEY}",
                "X-AgentChat-Client": "hermes",
                "X-AgentChat-Client-Version": "99.0.0",
            },
        }
        config: dict[str, Any] = {"mcp_servers": {"agentchat": json.loads(json.dumps(entry))}}
        assert reg.ensure_entry(config) == "present"
        assert config["mcp_servers"]["agentchat"] == entry

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
        assert server["headers"] == {"Authorization": "Bearer ${AGENTCHAT_API_KEY}", **CLIENT_HEADERS}

    def test_repo_root_mcp_json_is_the_declaration(self) -> None:
        on_disk = json.loads((REPO_ROOT / "mcp.json").read_text(encoding="utf-8"))
        assert on_disk == reg.mcp_json_document()

    def test_config_yaml_entry_and_portable_entry_agree(self) -> None:
        portable = reg.mcp_json_document()["mcpServers"]["agentchat"]
        native = reg.server_entry()
        assert portable["url"] == native["url"]
        assert portable["headers"] == native["headers"]

    def test_readme_snippet_names_this_version(self) -> None:
        """The README's hand-written entry carries the version; a release
        bump must update it (and mcp.json) with the package."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert '      X-AgentChat-Client: "hermes"\n' in readme
        assert f'      X-AgentChat-Client-Version: "{__version__}"\n' in readme
