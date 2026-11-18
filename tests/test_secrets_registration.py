"""``secrets.agentchat`` in ``config.yaml``: the key source on, the key kept out of the agent's commands."""
from __future__ import annotations

import copy
import sys
import types
from typing import Any

import pytest

from agentchatme_hermes import secrets_registration as sr
from agentchatme_hermes.mcp_registration import McpRegistrationError

OURS = {"enabled": True, "api_key_env": "AGENTCHAT_API_KEY"}


class TestPureOperations:
    def test_added_when_absent(self) -> None:
        config: dict[str, Any] = {"model": {"default": "x"}}
        assert sr.ensure_block(config) == "added"
        assert config["secrets"] == {"agentchat": OURS}
        assert config["model"] == {"default": "x"}
        assert sr.ensure_block(config) == "present"

    def test_added_next_to_other_sources(self) -> None:
        config: dict[str, Any] = {"secrets": {"sources": ["bitwarden"], "bitwarden": {"enabled": True}}}
        assert sr.ensure_block(config) == "added"
        assert config["secrets"]["bitwarden"] == {"enabled": True}
        assert config["secrets"]["sources"] == ["bitwarden"]
        assert config["secrets"]["agentchat"] == OURS

    def test_an_enabled_block_without_the_pointer_gets_it(self) -> None:
        config: dict[str, Any] = {"secrets": {"agentchat": {"enabled": True, "timeout_seconds": 5}}}
        assert sr.describe_block(config) == "incomplete"
        assert sr.ensure_block(config) == "completed"
        assert config["secrets"]["agentchat"] == {**OURS, "timeout_seconds": 5}

    def test_disabled_and_hand_written_blocks_are_left_alone(self) -> None:
        off: dict[str, Any] = {"secrets": {"agentchat": {"enabled": False}}}
        assert sr.ensure_block(off) == "disabled"
        assert off == {"secrets": {"agentchat": {"enabled": False}}}
        other: dict[str, Any] = {"secrets": {"agentchat": {"enabled": True, "api_key_env": "MY_KEY"}}}
        assert sr.ensure_block(other) == "user_managed"
        assert other["secrets"]["agentchat"]["api_key_env"] == "MY_KEY"
        weird: dict[str, Any] = {"secrets": {"agentchat": "on"}}
        assert sr.ensure_block(weird) == "user_managed"

    def test_remove_only_ours(self) -> None:
        config: dict[str, Any] = {"secrets": {"agentchat": dict(OURS)}}
        assert sr.remove_block(config) is True
        assert config == {}
        kept: dict[str, Any] = {"secrets": {"agentchat": dict(OURS), "bitwarden": {"enabled": True}}}
        assert sr.remove_block(kept) is True
        assert kept == {"secrets": {"bitwarden": {"enabled": True}}}
        theirs: dict[str, Any] = {"secrets": {"agentchat": {**OURS, "note": "mine"}}}
        assert sr.remove_block(theirs) is False
        off: dict[str, Any] = {"secrets": {"agentchat": {"enabled": False}}}
        assert sr.remove_block(off) is False

    def test_the_snippet_is_the_block(self) -> None:
        assert sr.manual_snippet() == (
            "secrets:\n  agentchat:\n    enabled: true\n    api_key_env: AGENTCHAT_API_KEY\n"
        )


class _HermesConfig:
    """``hermes_cli.config``'s raw reader, saver and managed check, on a dict."""

    def __init__(self, data: dict[str, Any], *, managed: bool = False, drops: bool = False) -> None:
        self.data = data
        self.managed = managed
        self.drops = drops
        self.saves = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = types.ModuleType("hermes_cli.config")
        # A read is the file as written: a fresh copy every time.
        module.read_user_config_raw = lambda: copy.deepcopy(self.data)  # type: ignore[attr-defined]
        module.save_config = self.save  # type: ignore[attr-defined]
        module.is_managed = lambda: self.managed  # type: ignore[attr-defined]
        package = types.ModuleType("hermes_cli")
        package.config = module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "hermes_cli", package)
        monkeypatch.setitem(sys.modules, "hermes_cli.config", module)

    def save(self, config: dict[str, Any]) -> None:
        self.saves += 1
        if not self.drops:
            self.data = copy.deepcopy(config)


class TestThroughHermes:
    def test_install_writes_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hermes = _HermesConfig({})
        hermes.install(monkeypatch)
        assert sr.install_block() == "added"
        assert sr.install_block() == "present"
        assert hermes.saves == 1
        assert sr.inspect_block() == "ours"

    def test_managed_install_says_what_to_add(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _HermesConfig({}, managed=True).install(monkeypatch)
        with pytest.raises(McpRegistrationError, match="managed by a package manager") as info:
            sr.install_block()
        assert sr.manual_snippet() in str(info.value)

    def test_a_write_that_did_not_land_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _HermesConfig({}, drops=True).install(monkeypatch)
        with pytest.raises(McpRegistrationError, match="did not save"):
            sr.install_block()

    def test_uninstall(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hermes = _HermesConfig({"secrets": {"agentchat": dict(OURS)}})
        hermes.install(monkeypatch)
        assert sr.uninstall_block() is True
        assert hermes.data == {}
        assert sr.uninstall_block() is False

    def test_without_hermes_the_error_names_this_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "hermes_cli", None)
        with pytest.raises(McpRegistrationError) as info:
            sr.inspect_block()
        assert sr.manual_snippet() in str(info.value)
        assert "mcp_servers" not in str(info.value)
