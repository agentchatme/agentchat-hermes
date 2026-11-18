"""Which key a profile acts as — and the keys it must refuse.

``hermes profile create`` copies the launch profile's ``.env``
(``--clone`` with ``config.yaml``; ``--clone-all`` every file), so with the
key in ``.env`` every new profile silently became the same AgentChat agent —
the plugin even placed the first agent's identity line in the new profile,
and status / doctor said all was well. The key file is not copied by
``create`` / ``--clone``; a ``--clone-all`` copy names the profile it belongs
to; a ``.env`` copy of another profile's key is refused, with the fix.

Target 6 of the key fix: an ``AGENTCHAT_API_KEY`` exported in the shell (or
left in ``.env``) never silently decides which agent a profile is; when it
disagrees with the saved key, the plugin refuses and says what to do.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import types
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fake_keys import api_key
from key_files import profile_home, save_key

from agentchatme_hermes import cli, config, key_source, key_store
from agentchatme_hermes.config import (
    ENV_API_KEY,
    ENV_API_KEY_LEGACY,
    KEY_SOURCE_STORE,
    KeyRefused,
    load_config,
    resolve_key,
)

if TYPE_CHECKING:
    from pathlib import Path

ONE = api_key("agent_one")
TWO = api_key("agent_two")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ENV_API_KEY, ENV_API_KEY_LEGACY, "AGENTCHATME_API_BASE"):
        monkeypatch.delenv(name, raising=False)


def _env(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(text, encoding="utf-8")


def _profile(name: str) -> Path:
    home = profile_home() / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def _hermes_profile_create(source: Path, name: str, *, clone_all: bool = False) -> Path:
    """What ``hermes profile create`` does to the files this plugin cares
    about: the launch profile's ``.env`` is copied (plain create, after a
    channel strip; ``--clone`` with ``config.yaml`` and ``SOUL.md``);
    ``--clone-all`` copies everything (``hermes_cli/profiles.py``,
    ``profile_provision.mirror_launch_credentials``)."""
    target = profile_home() / "profiles" / name
    if clone_all:
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("profiles"))
        return target
    target.mkdir(parents=True)
    for relpath in (".env", "config.yaml", "SOUL.md"):
        if (source / relpath).exists():
            shutil.copy2(source / relpath, target / relpath)
    return target


class TestTheKeyFile:
    def test_the_key_file_is_the_key(self) -> None:
        save_key(ONE, handle="alice", api_base="https://self.example")
        resolved = resolve_key()
        assert (resolved.api_key, resolved.source, resolved.refusal) == (ONE, KEY_SOURCE_STORE, None)
        cfg = load_config()
        assert cfg is not None and cfg.api_key == ONE and cfg.api_key_source == KEY_SOURCE_STORE
        assert cfg.api_base == "https://self.example", "the key's server comes with it"

    def test_an_explicit_api_base_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(ONE, api_base="https://self.example")
        monkeypatch.setenv("AGENTCHATME_API_BASE", "https://other.example")
        cfg = load_config()
        assert cfg is not None and cfg.api_base == "https://other.example"

    def test_a_leftover_dotenv_copy_is_a_note(self) -> None:
        save_key(ONE)
        _env(profile_home(), f"{ENV_API_KEY}={ONE}\n")
        resolved = resolve_key()
        assert resolved.api_key == ONE and resolved.refusal is None
        assert any("doctor --fix" in note for note in resolved.notes)


class TestAnotherKeyIsNeverQuietlyUsed:
    def test_a_different_dotenv_key_is_refused(self) -> None:
        save_key(ONE, handle="alice")
        _env(profile_home(), f"{ENV_API_KEY}={TWO}\n")
        resolved = resolve_key()
        assert resolved.api_key is None and resolved.refusal is not None
        assert "is not the key this profile saved" in resolved.refusal and "(@alice)" in resolved.refusal
        assert ONE not in resolved.refusal and TWO not in resolved.refusal
        with pytest.raises(KeyRefused):
            load_config()

    def test_an_exported_key_that_disagrees_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, TWO)
        resolved = resolve_key()
        assert resolved.refusal is not None
        assert "this process's environment" in resolved.refusal
        assert "login --api-key-file" in resolved.refusal

    def test_it_stays_refused_after_hermes_rewrites_the_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hermes' re-pull of the key source overwrites the variable with the
        saved key; the export the plugin saw at load is still the user's."""
        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, TWO)
        assert resolve_key().refusal is not None  # the plugin loads: the export is seen
        monkeypatch.setenv(ENV_API_KEY, ONE)  # the source's override_existing write-back
        assert resolve_key().refusal is not None, "the 30 s re-check does not quietly accept it"

    def test_an_exported_key_that_agrees_is_fine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, ONE)
        assert resolve_key().refusal is None

    def test_our_own_sources_earlier_value_is_not_a_conflict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rotation saved while this process runs: the environment still has the
        old key, written there by the plugin's own source (Hermes' provenance)."""
        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, TWO)
        monkeypatch.setattr(key_source, "supplied_by_us", lambda name=ENV_API_KEY: True)
        assert resolve_key().refusal is None

    def test_another_profiles_environment_is_not_this_ones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Under a multiplexed gateway the process environment is the launch profile's."""
        he2 = _profile("he2")
        save_key(TWO, home=he2)
        monkeypatch.setenv(ENV_API_KEY, ONE)  # the launch profile's
        assert resolve_key(he2).refusal is None


class TestCopiesAreNotAdopted:
    def test_plain_create_and_clone_copy_no_key(self) -> None:
        root = profile_home()
        save_key(ONE)
        (root / "config.yaml").write_text("secrets:\n  agentchat:\n    enabled: true\n", encoding="utf-8")
        # Plain `profile create` and `--clone` copy the same files this plugin cares about.
        for name in ("t5tmp", "he2"):
            new = _hermes_profile_create(root, name)
            resolved = resolve_key(new)
            assert resolved.api_key is None and resolved.refusal is None, name
            assert load_config(hermes_home=new) is None, "a new profile is unconfigured, not agent one"

    def test_clone_all_copies_the_key_file_and_it_is_refused(self) -> None:
        root = profile_home()
        save_key(ONE)
        clone = _hermes_profile_create(root, "he2", clone_all=True)
        resolved = resolve_key(clone)
        assert resolved.api_key is None and resolved.refusal is not None
        assert "copy of profile default's" in resolved.refusal
        assert "hermes -p he2 agentchat logout" in resolved.refusal
        assert resolve_key(root).api_key == ONE, "the original is still the agent"

    def test_a_dotenv_copy_of_the_default_profiles_key_is_refused(self) -> None:
        """A profile created before this release: the key was in .env and
        Hermes copied it. The default profile stays the agent."""
        root = profile_home()
        _env(root, f"{ENV_API_KEY}={ONE}\n")
        clone = _hermes_profile_create(root, "he2")
        resolved = resolve_key(clone)
        assert resolved.refusal is not None and "same AgentChat key as profile default" in resolved.refusal
        assert "hermes -p he2 agentchat logout" in resolved.refusal
        assert "hermes agentchat logout" in resolved.refusal, "or the other way round"
        root_resolved = resolve_key(root)
        assert root_resolved.api_key == ONE and root_resolved.refusal is None
        assert any("profile he2" in note for note in root_resolved.notes), "said in the original too"

    def test_a_dotenv_key_another_profile_saved_in_its_key_file_is_refused(self) -> None:
        he1, he2 = _profile("he1"), _profile("he2")
        save_key(ONE, home=he1)
        _env(he2, f"{ENV_API_KEY}={ONE}\n")
        resolved = resolve_key(he2)
        assert resolved.refusal is not None and "profile he1" in resolved.refusal

    def test_two_named_profiles_sharing_a_dotenv_key_are_told(self) -> None:
        he1, he2 = _profile("he1"), _profile("he2")
        _env(he1, f"{ENV_API_KEY}={ONE}\n")
        _env(he2, f"{ENV_API_KEY}={ONE}\n")
        resolved = resolve_key(he2)
        assert resolved.api_key == ONE
        assert resolved.notes and "profile he1" in resolved.notes[0] and "one AgentChat agent" in resolved.notes[0]


class TestRefusedActivation:
    def test_a_copied_profile_starts_nothing_and_places_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from agentchatme_hermes import _register

        root = profile_home()
        save_key(ONE)
        clone = _hermes_profile_create(root, "he2", clone_all=True)
        started: list[Any] = []
        monkeypatch.setattr(_register, "_try_start_runtime", lambda *a, **k: started.append(a))
        watcher = _register.ActivationWatcher(hermes_home=clone, profile="he2", gateway_mode=True)
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes._register"):
            assert watcher.try_activate() == "refused"
            assert watcher.try_activate() == "refused"
        assert started == []
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1, "said once, not every 30 s"
        assert "copy of profile default's" in errors[0]
        assert not (clone / "SOUL.md").exists()


class TestStatusAndDoctorSayItIsAnotherProfilesAgent:
    def _in(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        from agentchatme_hermes import profile_scope

        monkeypatch.setattr(profile_scope, "resolve_hermes_home", lambda: home)

    def test_status(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        root = profile_home()
        save_key(ONE)
        clone = _hermes_profile_create(root, "he2", clone_all=True)
        self._in(monkeypatch, clone)
        assert cli._dispatch_status(argparse.Namespace()) == 1
        err = capsys.readouterr().err
        assert "copy of profile default's" in err and ONE not in err

    def test_doctor(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        root = profile_home()
        _env(root, f"{ENV_API_KEY}={ONE}\n")
        clone = _hermes_profile_create(root, "he2")
        self._in(monkeypatch, clone)
        monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: True)
        rc = cli._dispatch_doctor(argparse.Namespace(fix=False))
        out = capsys.readouterr().out
        assert rc >= 1
        assert "[fail] This profile's .env holds the same AgentChat key as profile default" in out
        assert "All checks passed" not in out
        assert ONE not in out


class TestLogout:
    def test_logout_in_a_copied_profile_removes_only_the_copy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentchatme_hermes import profile_scope

        root = profile_home()
        save_key(ONE)
        clone = _hermes_profile_create(root, "he2", clone_all=True)
        monkeypatch.setattr(profile_scope, "resolve_hermes_home", lambda: clone)
        monkeypatch.setattr(cli, "_uninstall_soul_anchor", lambda: None)
        monkeypatch.setattr(cli, "_uninstall_secrets_registration", lambda: None)
        monkeypatch.setattr(cli, "_uninstall_mcp_registration", lambda: None)
        assert cli._dispatch_logout(argparse.Namespace()) == 0
        assert key_store.read_store(clone).status == "missing"
        assert key_store.read_store(root).key.api_key == ONE  # type: ignore[union-attr]
        assert "removed from this profile" in capsys.readouterr().out
        assert resolve_key(clone).api_key is None and resolve_key(clone).refusal is None

    def test_logout_with_nothing(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli._dispatch_logout(argparse.Namespace()) == 0
        assert "Nothing to clear" in capsys.readouterr().out


class TestDoctorFixKeepsAKeyTheToolsWouldLose:
    def test_without_the_bridge_the_key_stays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import credentials

        root = profile_home()
        _env(root, f"{ENV_API_KEY}={ONE}\n")
        monkeypatch.setattr(credentials, "key_bridge", lambda: (False, "this Hermes has no secret sources"))
        lines = cli._fix_key(resolve_key(root), handle="alice", api_base="https://api.agentchat.me", home=root)
        assert lines == [("fail", "fix: the key stays in the profile .env — this Hermes has no secret sources")]
        assert (root / ".env").read_text(encoding="utf-8") == f"{ENV_API_KEY}={ONE}\n"
        assert key_store.read_store(root).status == "missing"


class TestDoctorFixMovesTheKey:
    def test_fix_moves_a_dotenv_key_into_the_key_file(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentchatme_hermes import kit, mcp_registration, secrets_registration

        root = profile_home()
        _env(root, f"KEEP=1\n{ENV_API_KEY}={ONE}\n")
        fake_client = MagicMock()
        fake_client.get_me.return_value = {"handle": "alice", "status": "active"}
        sdk = types.ModuleType("agentchatme")
        sdk.AgentChatClient = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]
        sdk.AgentChatClientIdentity = MagicMock  # type: ignore[attr-defined]
        sdk.AgentChatError = Exception  # type: ignore[attr-defined]
        sdk.UnauthorizedError = Exception  # type: ignore[attr-defined]
        sdk.ConnectionError = ConnectionError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agentchatme", sdk)
        monkeypatch.setattr(cli, "_other_gateway_running", lambda: False)
        monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: True)
        monkeypatch.setattr(cli, "_doctor_identity_texts", lambda *a, **k: (None, None))
        monkeypatch.setattr(cli, "place_identity_and_tools", lambda **_kw: [])
        monkeypatch.setattr(kit, "kit_problem", lambda: None)
        monkeypatch.setattr(mcp_registration, "inspect_entry_identity", lambda **_kw: ("ours", "current"))
        monkeypatch.setattr(secrets_registration, "inspect_block", lambda: "ours")
        from agentchatme_hermes import credentials

        monkeypatch.setattr(credentials, "key_bridge", lambda: (True, ""))

        cli._dispatch_doctor(argparse.Namespace(fix=True))
        out = capsys.readouterr().out
        assert "[warn] key in the profile .env as AGENTCHAT_API_KEY" in out
        assert "[ok]   fix: moved the key from the profile .env to" in out
        assert (root / ".env").read_text(encoding="utf-8") == "KEEP=1\n"
        record = key_store.read_store(root)
        assert record.usable and record.key is not None and (record.key.api_key, record.key.handle) == (ONE, "alice")
        assert ONE not in out
        assert config.resolve_key(root).source == KEY_SOURCE_STORE
