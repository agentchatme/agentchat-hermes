"""Tests for ``agentchatme_hermes.profile_scope``.

Hermes itself is not importable in the test environment, so the module's
fallback chain is what runs by default. The Hermes-present paths are
exercised with a stand-in ``agent.secret_scope`` module injected into
``sys.modules``.
"""
from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, Any

import pytest

from agentchatme_hermes import profile_scope
from agentchatme_hermes.profile_scope import (
    account_fingerprint,
    hermes_root_of,
    profile_label,
    resolve_hermes_home,
    resolve_setting,
    set_setting_resolver_for_tests,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_override() -> Any:
    set_setting_resolver_for_tests(None)
    yield
    set_setting_resolver_for_tests(None)


class TestResolveHermesHome:
    def test_env_var_wins_without_hermes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "growth"))
        assert resolve_hermes_home() == tmp_path / "profiles" / "growth"

    def test_default_is_dot_hermes_under_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert resolve_hermes_home() == tmp_path / ".hermes"


class TestRootAndLabel:
    def test_profile_home_rolls_up_to_root(self, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        assert hermes_root_of(root / "profiles" / "growth") == root
        assert profile_label(root / "profiles" / "growth") == "growth"

    def test_default_home_is_its_own_root(self, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        assert hermes_root_of(root) == root
        assert profile_label(root) == "default"

    def test_custom_root_without_profiles_dir(self, tmp_path: Path) -> None:
        custom = tmp_path / "hermes-data"
        assert hermes_root_of(custom) == custom
        assert profile_label(custom) == "default"


class TestAccountFingerprint:
    def test_stable_and_short(self) -> None:
        a = account_fingerprint("https://api.agentchat.me", "ac_live_abc")
        assert a == account_fingerprint("https://api.agentchat.me", "ac_live_abc")
        assert len(a) == 16
        int(a, 16)  # hex

    def test_differs_by_key_and_by_server(self) -> None:
        base = "https://api.agentchat.me"
        assert account_fingerprint(base, "k1") != account_fingerprint(base, "k2")
        assert account_fingerprint(base, "k1") != account_fingerprint(
            "https://staging.example", "k1"
        )

    def test_trailing_slash_on_base_is_ignored(self) -> None:
        assert account_fingerprint("https://api.agentchat.me/", "k") == account_fingerprint(
            "https://api.agentchat.me", "k"
        )

    def test_not_the_raw_key(self) -> None:
        assert "ac_live_secret" not in account_fingerprint("https://x", "ac_live_secret")


def _install_fake_secret_scope(
    monkeypatch: pytest.MonkeyPatch, get_secret: Any
) -> type[Exception]:
    """Inject a stand-in for Hermes' ``agent.secret_scope`` module."""

    class UnscopedSecretError(RuntimeError):
        pass

    pkg = types.ModuleType("agent")
    mod = types.ModuleType("agent.secret_scope")
    mod.get_secret = get_secret  # type: ignore[attr-defined]
    mod.UnscopedSecretError = UnscopedSecretError  # type: ignore[attr-defined]
    pkg.secret_scope = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    return UnscopedSecretError


class TestResolveSetting:
    def test_falls_back_to_environ_without_hermes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
        monkeypatch.delitem(sys.modules, "agent", raising=False)
        monkeypatch.setenv("AGENTCHATME_API_KEY", "from-env")
        assert resolve_setting("AGENTCHATME_API_KEY") == "from-env"
        monkeypatch.delenv("AGENTCHATME_API_KEY")
        assert resolve_setting("AGENTCHATME_API_KEY") is None

    def test_hermes_scope_wins_over_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The multiplexing fix: the bound profile's value, never os.environ."""
        monkeypatch.setenv("AGENTCHATME_API_KEY", "launch-profile-key")
        _install_fake_secret_scope(
            monkeypatch, lambda name, default=None: {"AGENTCHATME_API_KEY": "growth-key"}.get(name, default)
        )
        assert resolve_setting("AGENTCHATME_API_KEY") == "growth-key"

    def test_unscoped_under_multiplexing_reads_profile_dotenv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        err = _install_fake_secret_scope(monkeypatch, None)

        def raising(name: str, default: Any = None) -> Any:
            raise err(name)

        sys.modules["agent.secret_scope"].get_secret = raising  # type: ignore[attr-defined]
        home = tmp_path / "profiles" / "ops"
        home.mkdir(parents=True)
        (home / ".env").write_text(
            "# comment\nexport AGENTCHATME_API_KEY='ops-key'  \nAGENTCHATME_HANDLE=ops # trailing\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("AGENTCHATME_API_KEY", "must-not-leak")
        assert resolve_setting("AGENTCHATME_API_KEY") == "ops-key"
        assert resolve_setting("AGENTCHATME_HANDLE") == "ops"
        assert resolve_setting("AGENTCHATME_API_BASE") is None

    def test_override_resolver_for_tests(self) -> None:
        set_setting_resolver_for_tests(lambda name: {"AGENTCHATME_API_KEY": "o"}.get(name))
        assert resolve_setting("AGENTCHATME_API_KEY") == "o"
        assert resolve_setting("OTHER") is None


class TestDotenvReader:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert profile_scope._dotenv_value(tmp_path / "nope", "K") is None

    def test_quotes_and_comments(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text('A="x y"\nB=plain # note\nC = spaced\n#D=hidden\n', encoding="utf-8")
        assert profile_scope._dotenv_value(env, "A") == "x y"
        assert profile_scope._dotenv_value(env, "B") == "plain"
        assert profile_scope._dotenv_value(env, "C") == "spaced"
        assert profile_scope._dotenv_value(env, "D") is None
