"""Tests for ``agentchatme_hermes.config``."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from agentchatme_hermes.config import (
    DEFAULT_API_BASE,
    DEFAULT_MAX_INFLIGHT_TURNS,
    DEFAULT_TURN_INACTIVITY_TIMEOUT_S,
    ConfigError,
    load_config,
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
        "AGENTCHATME_API_KEY",
        "AGENTCHATME_API_BASE",
        "AGENTCHATME_WS_URL",
        "AGENTCHATME_MAX_INFLIGHT_TURNS",
        "AGENTCHATME_TURN_INACTIVITY_TIMEOUT_S",
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
        _isolated(monkeypatch, AGENTCHATME_API_KEY="ac_live_abc")
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_key == "ac_live_abc"
        assert cfg.api_base == DEFAULT_API_BASE
        assert cfg.ws_url == "wss://api.agentchat.me"
        assert cfg.max_inflight_turns == DEFAULT_MAX_INFLIGHT_TURNS
        assert cfg.turn_inactivity_timeout_s == DEFAULT_TURN_INACTIVITY_TIMEOUT_S

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

    def test_max_inflight_turns_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_MAX_INFLIGHT_TURNS="16",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.max_inflight_turns == 16

    def test_max_inflight_turns_below_minimum_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_MAX_INFLIGHT_TURNS="0",
        )
        with pytest.raises(ConfigError, match="below the minimum"):
            load_config()

    def test_max_inflight_turns_not_integer_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_MAX_INFLIGHT_TURNS="four",
        )
        with pytest.raises(ConfigError, match="not an integer"):
            load_config()

    def test_inactivity_timeout_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_TURN_INACTIVITY_TIMEOUT_S="120.5",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.turn_inactivity_timeout_s == 120.5

    def test_inactivity_timeout_zero_is_valid_disable_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_TURN_INACTIVITY_TIMEOUT_S="0",
        )
        cfg = load_config()
        assert cfg is not None
        assert cfg.turn_inactivity_timeout_s == 0.0

    def test_inactivity_timeout_negative_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(
            monkeypatch,
            AGENTCHATME_API_KEY="k",
            AGENTCHATME_TURN_INACTIVITY_TIMEOUT_S="-1",
        )
        with pytest.raises(ConfigError, match="below the minimum"):
            load_config()

    def test_api_key_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolated(monkeypatch, AGENTCHATME_API_KEY="  ac_live_xyz  ")
        cfg = load_config()
        assert cfg is not None
        assert cfg.api_key == "ac_live_xyz"
