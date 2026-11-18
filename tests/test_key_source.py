"""The key file handed to Hermes' MCP client: the ``agentchat`` secret source.

Hermes is not installed in the unit suite; ``agent.secret_sources.base`` is
stood in by the same shape Hermes ships (``ErrorKind``, ``FetchResult``,
``SecretSource``). The real orchestrator is exercised against a real Hermes in
``test_hermes_contract.py``.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import types
from typing import TYPE_CHECKING, Any

import pytest
from fake_keys import api_key
from hermes_doubles import ErrorKind as _ErrorKind
from hermes_doubles import SecretSource as _SecretSource
from hermes_doubles import install_secret_source_base

from agentchatme_hermes import key_source, key_store
from agentchatme_hermes.config import ENV_API_KEY

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def hermes_base(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    return install_secret_source_base(monkeypatch)


ENABLED = {"enabled": True, "api_key_env": ENV_API_KEY}


def _save(home: Path, key: str, handle: str = "alice") -> None:
    key_store.write_store(home, api_key=key, handle=handle, api_base="https://api.agentchat.me")


_HERMES_INSTALLED = importlib.util.find_spec("run_agent") is not None


@pytest.mark.skipif(_HERMES_INSTALLED, reason="Hermes is installed (the hermes-main workflow)")
class TestOutsideHermes:
    def test_no_source_and_no_support(self) -> None:
        assert key_source.build_key_source() is None
        assert key_source.source_supported() is False

    def test_registering_without_hermes_is_unsupported(self) -> None:
        calls: list[Any] = []
        ctx = types.SimpleNamespace(register_secret_source=calls.append)
        assert key_source.register_key_source(ctx) == "unsupported"
        assert calls == []


class TestTheSource:
    def test_shape(self, hermes_base: types.ModuleType) -> None:
        source = key_source.build_key_source()
        assert isinstance(source, _SecretSource)
        assert (source.name, source.shape) == ("agentchat", "mapped")
        # The saved key decides which agent the hosted tools act as: it beats an
        # exported variable or a stale .env line.
        assert source.override_existing({}) is True

    def test_supplies_this_profiles_key(self, hermes_base: types.ModuleType, tmp_path: Path) -> None:
        home = tmp_path / ".hermes"
        _save(home, api_key("mine"))
        result = key_source.build_key_source().fetch(ENABLED, home)  # type: ignore[union-attr]
        assert result.ok and result.secrets == {ENV_API_KEY: api_key("mine")}

    def test_each_profile_gets_its_own(self, hermes_base: types.ModuleType, tmp_path: Path) -> None:
        """Under a multiplexed gateway each profile's hosted tools must
        authenticate as that profile's agent, never another's."""
        root = tmp_path / ".hermes"
        he2 = root / "profiles" / "he2"
        _save(root, api_key("one"), "he-1")
        _save(he2, api_key("two"), "he-2")
        source = key_source.build_key_source()
        assert source.fetch(ENABLED, root).secrets == {ENV_API_KEY: api_key("one")}  # type: ignore[union-attr]
        assert source.fetch(ENABLED, he2).secrets == {ENV_API_KEY: api_key("two")}  # type: ignore[union-attr]

    def test_a_copied_key_file_gets_no_key(self, hermes_base: types.ModuleType, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        clone = root / "profiles" / "he2"
        _save(root, api_key("one"))
        clone.mkdir(parents=True)
        shutil.copytree(key_store.store_dir(root), key_store.store_dir(clone))
        result = key_source.build_key_source().fetch(ENABLED, clone)  # type: ignore[union-attr]
        assert result.ok and result.secrets == {ENV_API_KEY: ""}
        assert "copy of profile default's" in result.warnings[0]

    def test_a_dotenv_copy_is_masked_so_the_tools_do_not_act_as_the_other_agent(
        self, hermes_base: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A profile created from the default one holds its key in .env: the
        hosted tools there would authenticate as the default's agent. The
        source supplies the variable empty, over the copy (override_existing)."""
        root = tmp_path / ".hermes"
        new = root / "profiles" / "t5tmp"
        new.mkdir(parents=True)
        (root / ".env").write_text(f"{ENV_API_KEY}={api_key('one')}\n", encoding="utf-8")
        (new / ".env").write_text(f"{ENV_API_KEY}={api_key('one')}\n", encoding="utf-8")
        source = key_source.build_key_source()
        monkeypatch.setenv("HERMES_HOME", str(new))
        assert source.is_enabled(ENABLED) is True  # type: ignore[union-attr]
        assert source.fetch(ENABLED, new).secrets == {ENV_API_KEY: ""}  # type: ignore[union-attr]
        monkeypatch.setenv("HERMES_HOME", str(root))
        assert source.is_enabled(ENABLED) is False, "the original keeps its .env key"  # type: ignore[union-attr]

    def test_no_key_file_is_not_configured(self, hermes_base: types.ModuleType, tmp_path: Path) -> None:
        result = key_source.build_key_source().fetch(ENABLED, tmp_path / "empty")  # type: ignore[union-attr]
        assert result.secrets == {} and result.error_kind is _ErrorKind.NOT_CONFIGURED
        assert "hermes agentchat login" in str(result.error)

    def test_fetch_never_raises(
        self, hermes_base: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_home: Path) -> Any:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(key_store, "read_store", boom)
        result = key_source.build_key_source().fetch(ENABLED, tmp_path)  # type: ignore[union-attr]
        assert result.error_kind is _ErrorKind.INTERNAL and result.secrets == {}

    def test_the_error_never_carries_the_key(self, hermes_base: types.ModuleType, tmp_path: Path) -> None:
        root = tmp_path / ".hermes"
        clone = root / "profiles" / "he2"
        _save(root, api_key("secret_value"))
        clone.mkdir(parents=True)
        shutil.copytree(key_store.store_dir(root), key_store.store_dir(clone))
        result = key_source.build_key_source().fetch(ENABLED, clone)  # type: ignore[union-attr]
        assert api_key("secret_value") not in str(result.error) + " ".join(result.warnings)

    def test_enabled_only_by_the_block_and_a_usable_key_file(
        self, hermes_base: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / ".hermes"
        monkeypatch.setenv("HERMES_HOME", str(home))
        source = key_source.build_key_source()
        assert source.is_enabled(ENABLED) is False, "no key file yet: no fetch is counted"  # type: ignore[union-attr]
        _save(home, api_key("k"))
        assert source.is_enabled(ENABLED) is True  # type: ignore[union-attr]
        assert source.is_enabled({}) is False  # type: ignore[union-attr]
        assert source.is_enabled({"enabled": False}) is False  # type: ignore[union-attr]

    def test_registered_through_ctx(self, hermes_base: types.ModuleType) -> None:
        calls: list[Any] = []
        ctx = types.SimpleNamespace(register_secret_source=lambda s: calls.append(s) or True)
        assert key_source.register_key_source(ctx) == "registered"
        assert len(calls) == 1 and calls[0].name == "agentchat"

    def test_a_hermes_without_the_hook(self, hermes_base: types.ModuleType) -> None:
        assert key_source.register_key_source(types.SimpleNamespace()) == "unsupported"

    def test_a_refusing_hermes(self, hermes_base: types.ModuleType) -> None:
        # Hermes' ctx returns the registration handle, or None when it refused the source.
        ctx = types.SimpleNamespace(register_secret_source=lambda _s: None)
        assert key_source.register_key_source(ctx) == "failed"


class TestSuppliedByUs:
    def test_reads_hermes_provenance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scope = types.ModuleType("agent.secret_scope")
        labels = {ENV_API_KEY: " (from AgentChat key file)"}
        scope.secret_source_label = lambda name: labels.get(name, "")  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
        assert key_source.supplied_by_us() is True
        labels[ENV_API_KEY] = " (from agentchat)"  # scoped registration seen without its scope
        assert key_source.supplied_by_us() is True
        labels[ENV_API_KEY] = " (from Bitwarden)"
        assert key_source.supplied_by_us() is False
        labels[ENV_API_KEY] = ""
        assert key_source.supplied_by_us() is False

    def test_without_hermes(self) -> None:
        assert key_source.supplied_by_us() is False
