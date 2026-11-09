"""Tests for ``agentchatme_hermes.config``."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from fake_keys import api_key

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from agentchatme_hermes import config as config_module
from agentchatme_hermes.config import (
    DEFAULT_API_BASE,
    DEFAULT_GATE_TIMEOUT_S,
    DEFAULT_INTEGRATION,
    DEFAULT_REPLY_GATE_ENABLED,
    DEFAULT_WAKE_DEBOUNCE_S,
    ENV_API_KEY,
    ENV_API_KEY_LEGACY,
    ConfigError,
    load_config,
    resolve_api_key,
)


@contextmanager
def env_set(values: dict[str, str | None]) -> Iterator[None]:
    """Set env vars for the duration of a test; restore on exit."""
    original = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _isolated(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> None:
    """Clear all our env vars first, then apply the test's settings."""
    for k in (
        "AGENTCHAT_API_KEY",
        "AGENTCHATME_API_KEY",
        "AGENTCHATME_API_BASE",
        "AGENTCHATME_WS_URL",
        "AGENTCHATME_INTEGRATION",
        "AGENTCHATME_DOORBELL",
        "AGENTCHATME_WAKE_DEBOUNCE_S",
        "AGENTCHATME_KNOCK_DEBOUNCE_S",
        "AGENTCHATME_REPLY_GATE_ENABLED",
        "AGENTCHATME_REPLY_GATE_FAIL_OPEN",
        "AGENTCHATME_GATE_TIMEOUT_S",
        "AGENTCHATME_LOCK_DIR",
        "AGENTCHATME_STATE_DIR",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in values.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


class TestLoadConfig:
    def test_returns_none_when_api_key_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch)
        assert load_config() is None

    def test_returns_none_when_api_key_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHATME_API_KEY="   ")
        assert load_config() is None

    def test_minimal_valid_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHATME_API_KEY=api_key("abc"))
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_key == api_key("abc")
        assert cfg.api_base == DEFAULT_API_BASE
        assert cfg.ws_url == "wss://api.agentchat.me"

    def test_ws_url_derived_from_https_api_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_API_BASE="https://custom.example.com",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_base == "https://custom.example.com"
        assert cfg.ws_url == "wss://custom.example.com"

    def test_ws_url_derived_from_http_api_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_API_BASE="http://localhost:3000",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.ws_url == "ws://localhost:3000"

    def test_api_base_trailing_slash_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_API_BASE="https://api.example.com/",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_base == "https://api.example.com"

    def test_invalid_api_base_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_API_BASE="not-a-url",
        )
        with pytest.raises(ConfigError, match="AGENTCHATME_API_BASE"):
            load_config()

    def test_invalid_api_base_scheme_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_API_BASE="ftp://api.example.com",
        )
        with pytest.raises(ConfigError, match="AGENTCHATME_API_BASE"):
            load_config()

    def test_explicit_ws_url_overrides_derivation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_API_BASE="https://api.example.com",
            AGENTCHATME_WS_URL="wss://ws.example.com",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_base == "https://api.example.com"
        assert cfg.ws_url == "wss://ws.example.com"

    def test_invalid_ws_url_scheme_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_WS_URL="https://wrong.example.com",
        )
        with pytest.raises(ConfigError, match="AGENTCHATME_WS_URL"):
            load_config()

    def test_api_key_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHATME_API_KEY=f"  {api_key('xyz')}  ")
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_key == api_key("xyz")


class TestGateConfig:
    """Reply-gate knobs in :func:`load_config`."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHATME_API_KEY="k")
        cfg = load_config()
        assert cfg is not None
        assert cfg.reply_gate_enabled is DEFAULT_REPLY_GATE_ENABLED
        assert cfg.gate_timeout_s == DEFAULT_GATE_TIMEOUT_S
        assert not hasattr(cfg, "reply_gate_fail_open"), "retired: the gate fails closed"

    def test_enabled_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_REPLY_GATE_ENABLED="0",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.reply_gate_enabled is False

    def test_retired_fail_open_is_accepted_ignored_and_warned_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_REPLY_GATE_FAIL_OPEN="true",
        )
        monkeypatch.setattr(config_module, "_fail_open_warned", False)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes.config"):
            cfg = load_config()
            load_config()
        assert cfg is not None
        warnings = [r for r in caplog.records if "REPLY_GATE_FAIL_OPEN" in r.getMessage()]
        assert len(warnings) == 1, "logged once, not on every load"
        assert "fails closed" in warnings[0].getMessage()

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on", "On"])
    def test_bool_truthy_tokens(
        self, monkeypatch: pytest.MonkeyPatch, truthy: str
    ) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_REPLY_GATE_ENABLED=truthy,
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.reply_gate_enabled is True

    @pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "off", "Off"])
    def test_bool_falsy_tokens(
        self, monkeypatch: pytest.MonkeyPatch, falsy: str
    ) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_REPLY_GATE_ENABLED=falsy,
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.reply_gate_enabled is False

    def test_bool_invalid_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_REPLY_GATE_ENABLED="maybe",
        )
        with pytest.raises(ConfigError, match="not a boolean"):
            load_config()

    def test_timeout_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_GATE_TIMEOUT_S="8.5",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.gate_timeout_s == 8.5

    def test_timeout_below_minimum_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_GATE_TIMEOUT_S="0",
        )
        with pytest.raises(ConfigError, match="below the minimum"):
            load_config()


# ───────────────────────── profile scoping ─────────────────────────


class TestProfileScopedConfig:
    def test_hermes_home_argument_is_bound(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolated(monkeypatch, AGENTCHATME_API_KEY=api_key("x"))
        home = tmp_path / ".hermes" / "profiles" / "growth"
        cfg = load_config(hermes_home=home)
        assert cfg is not None
        assert cfg.hermes_home == home
        assert cfg.lock_dir is None

    def test_lock_dir_setting(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated(monkeypatch, AGENTCHATME_API_KEY=api_key("x"))
        monkeypatch.setenv("AGENTCHATME_LOCK_DIR", str(tmp_path / "run"))
        cfg = load_config(hermes_home=tmp_path)
        assert cfg is not None
        assert cfg.lock_dir == tmp_path / "run"

    def test_account_fingerprint_matches_helper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from agentchatme_hermes.profile_scope import account_fingerprint

        _isolated(monkeypatch, AGENTCHATME_API_KEY=api_key("x"))
        cfg = load_config(hermes_home=tmp_path)
        assert cfg is not None
        assert cfg.account_fingerprint == account_fingerprint(DEFAULT_API_BASE, api_key("x"))

    def test_values_come_from_the_profile_resolver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Process env says one key, the profile scope another: profile wins."""
        from agentchatme_hermes.profile_scope import set_setting_resolver_for_tests

        _isolated(monkeypatch, AGENTCHATME_API_KEY="launch-profile-key")
        set_setting_resolver_for_tests(
            lambda name: {"AGENTCHATME_API_KEY": "growth-key"}.get(name)
        )
        try:
            cfg = load_config(hermes_home=tmp_path)
        finally:
            set_setting_resolver_for_tests(None)
        assert cfg is not None
        assert cfg.api_key == "growth-key"


# ───────────────────────── key names ─────────────────────────


class TestKeyNames:
    def test_canonical_name_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="new", AGENTCHATME_API_KEY="old")
        assert resolve_api_key() == ("new", ENV_API_KEY)
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_key == "new"
        assert cfg.api_key_source == ENV_API_KEY

    def test_legacy_name_is_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHATME_API_KEY="old")
        assert resolve_api_key() == ("old", ENV_API_KEY_LEGACY)
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_key == "old"
        assert cfg.api_key_source == ENV_API_KEY_LEGACY

    def test_neither_name_means_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch)
        assert resolve_api_key() is None
        assert load_config() is None

    def test_blank_canonical_falls_through_to_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="  ", AGENTCHATME_API_KEY="old")
        assert resolve_api_key() == ("old", ENV_API_KEY_LEGACY)


# ───────────────────────── the integration ─────────────────────────


class TestIntegrationConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k")
        cfg = load_config()
        assert cfg is not None
        assert cfg.integration == DEFAULT_INTEGRATION == "on"
        assert cfg.integration_enabled is True
        assert cfg.wake_debounce_s == DEFAULT_WAKE_DEBOUNCE_S == 2.0
        assert cfg.state_dir is None

    def test_off_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_INTEGRATION="OFF")
        cfg = load_config()
        assert cfg is not None
        assert cfg.integration == "off"
        assert cfg.integration_enabled is False

    def test_unknown_value_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_INTEGRATION="v3")
        with pytest.raises(ConfigError, match="AGENTCHATME_INTEGRATION"):
            load_config()

    def test_debounce_override_and_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_WAKE_DEBOUNCE_S="0.5")
        cfg = load_config()
        assert cfg is not None and cfg.wake_debounce_s == 0.5
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_WAKE_DEBOUNCE_S="-1")
        with pytest.raises(ConfigError, match="below the minimum"):
            load_config()

    def test_state_dir_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_STATE_DIR=str(tmp_path / "st"))
        cfg = load_config()
        assert cfg is not None and cfg.state_dir == tmp_path / "st"


class TestRenamedKeysKeepWorking:
    """``doorbell`` → ``integration``, ``knock`` → ``wake``: a config.yaml or
    .env written for the old names keeps working; the new name wins when both
    are set."""

    def test_legacy_doorbell_v3_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_DOORBELL="v3")
        cfg = load_config()
        assert cfg is not None and cfg.integration == "on" and cfg.integration_enabled

    def test_legacy_doorbell_off_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_DOORBELL="OFF")
        cfg = load_config()
        assert cfg is not None and cfg.integration == "off" and not cfg.integration_enabled

    def test_new_name_wins_over_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_INTEGRATION="on", AGENTCHATME_DOORBELL="off"
        )
        cfg = load_config()
        assert cfg is not None and cfg.integration_enabled is True

    def test_legacy_v2_is_rejected_with_the_pin_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_DOORBELL="v2")
        with pytest.raises(ConfigError, match=r"0\.2\.3111111"):
            load_config()

    def test_legacy_unknown_value_names_the_new_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_DOORBELL="loud")
        with pytest.raises(ConfigError, match=r"AGENTCHATME_INTEGRATION=on\|off"):
            load_config()

    def test_legacy_knock_debounce_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_KNOCK_DEBOUNCE_S="0.25")
        cfg = load_config()
        assert cfg is not None and cfg.wake_debounce_s == 0.25

    def test_new_debounce_wins_over_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHAT_API_KEY="k",
            AGENTCHATME_WAKE_DEBOUNCE_S="1.5",
            AGENTCHATME_KNOCK_DEBOUNCE_S="0.25",
        )
        cfg = load_config()
        assert cfg is not None and cfg.wake_debounce_s == 1.5

    def test_legacy_debounce_errors_name_the_legacy_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHAT_API_KEY="k", AGENTCHATME_KNOCK_DEBOUNCE_S="soon")
        with pytest.raises(ConfigError, match="AGENTCHATME_KNOCK_DEBOUNCE_S"):
            load_config()


class TestGateFailsClosed:
    def test_there_is_no_fail_open_switch(self) -> None:
        """The SDK gate fails closed and the host starts nothing on a failed
        gate; the old opt-in no longer exists as a setting."""
        assert not hasattr(config_module, "DEFAULT_REPLY_GATE_FAIL_OPEN")
        assert "reply_gate_fail_open" not in config_module.Config.__dataclass_fields__
