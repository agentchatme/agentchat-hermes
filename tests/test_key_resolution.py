"""Which key a profile acts as — and the keys it must refuse.

``hermes profile create`` copies the launch profile's ``.env``
(``--clone`` with ``config.yaml``; ``--clone-all`` every file), so with the
key in ``.env`` every new profile silently became the same AgentChat agent —
the plugin even placed the first agent's identity line in the new profile,
and status / doctor said all was well. The key file is not copied by
``create`` / ``--clone``; a ``--clone-all`` copy names the profile it belongs
to; a ``.env`` copy of another profile's key is refused, with the fix.

With a key file the key file decides: an ``AGENTCHAT_API_KEY`` set anywhere
else is not used (the key source supplies the key file's over it). The plugin
never reads a ``.env`` file (Nous review, ask 1): a profile's settings are what
Hermes' ``get_secret`` returns for the profile in scope — here the process
environment, as in a single-profile Hermes process — and another profile is
seen only through its key file.
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
from hermes_doubles import FakeHermesEnv
from key_files import profile_home, save_key, set_up_as_agent

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
    """A ``.env`` file as a user or an earlier release left it (never read by the plugin)."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(text, encoding="utf-8")


def _scope(monkeypatch: pytest.MonkeyPatch, home: Path, **lines: str) -> FakeHermesEnv:
    """Run as profile ``home`` (``HERMES_HOME``), its ``.env`` holding ``lines``,
    loaded into the environment as Hermes does at start; Hermes' ``.env``
    helpers stand in for that profile."""
    monkeypatch.setenv("HERMES_HOME", str(home))
    hermes = FakeHermesEnv(monkeypatch)
    hermes.install()
    hermes.write(home, **lines)
    hermes.load()
    return hermes


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

    def test_a_leftover_pre_v3_setting_is_a_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The key source never supplies the pre-v3 name: Hermes' value for it is
        the profile's own, a copy an earlier release left in .env."""
        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY_LEGACY, ONE)
        resolved = resolve_key()
        assert resolved.api_key == ONE and resolved.refusal is None
        assert any(ENV_API_KEY_LEGACY in note and "doctor --fix` removes it" in note for note in resolved.notes)

    def test_a_different_pre_v3_setting_is_said_and_not_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY_LEGACY, TWO)
        resolved = resolve_key()
        assert resolved.api_key == ONE and resolved.refusal is None
        assert any("holds a different key; it is not used (the key file decides)" in n for n in resolved.notes)
        assert TWO not in " ".join(resolved.notes)


class TestTheKeyFileDecides:
    def test_another_agentchat_api_key_is_not_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An export, a service's Environment=, a line left in .env: with a key
        file, the key source supplies the key file's over it, so it decides."""
        save_key(ONE, handle="alice")
        monkeypatch.setenv(ENV_API_KEY, TWO)
        resolved = resolve_key()
        assert (resolved.api_key, resolved.refusal, resolved.notes) == (ONE, None, ())
        cfg = load_config()
        assert cfg is not None and cfg.api_key == ONE

    def test_another_profiles_environment_is_not_this_ones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Under a multiplexed gateway the process environment is the launch profile's."""
        he2 = _profile("he2")
        save_key(TWO, home=he2)
        monkeypatch.setenv(ENV_API_KEY, ONE)  # the launch profile's
        assert resolve_key(he2).refusal is None and resolve_key(he2).api_key == TWO

    def test_another_profiles_settings_are_never_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """For a home that is not the profile in scope only its key file counts."""
        monkeypatch.setenv(ENV_API_KEY, ONE)  # this profile's (the default's)
        resolved = resolve_key(_profile("he2"))
        assert (resolved.api_key, resolved.refusal) == (None, None)


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

    def test_a_settings_copy_of_the_default_profiles_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A profile created before 0.6: Hermes copied the key in .env. The default
        profile saved it in its key file, so it is that agent."""
        root = profile_home()
        save_key(ONE)
        clone = _profile("he2")
        _scope(monkeypatch, clone, **{ENV_API_KEY: ONE})
        resolved = resolve_key(clone)
        assert resolved.refusal is not None and "same AgentChat key that profile default" in resolved.refusal
        assert "hermes -p he2 agentchat logout" in resolved.refusal
        assert "hermes agentchat logout" in resolved.refusal, "or the other way round"
        assert ONE not in resolved.refusal
        assert config.copy_of(clone) == root, "found for the key source"
        with pytest.raises(KeyRefused):
            load_config(hermes_home=clone)

    def test_a_copy_stays_refused_once_its_setting_is_masked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After Hermes applies the key source the copy's setting reads empty;
        it stays refused while the other profile holds the key, and not after."""
        root = profile_home()
        save_key(ONE)
        clone = _profile("he2")
        _scope(monkeypatch, clone, **{ENV_API_KEY: ONE})
        assert resolve_key(clone).refusal is not None
        monkeypatch.setenv(ENV_API_KEY, "")  # the source's empty value
        assert resolve_key(clone).refusal is not None
        assert key_source._supplies_for(clone), "the source keeps masking it"
        key_store.delete_store(root)  # the default profile logged out
        resolved = resolve_key(clone)
        assert (resolved.api_key, resolved.refusal) == (None, None)
        assert config.copy_of(clone) is None

    def test_a_settings_key_another_named_profile_saved_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        he1, he2 = _profile("he1"), _profile("he2")
        save_key(ONE, home=he1)
        _scope(monkeypatch, he2, **{ENV_API_KEY: ONE})
        resolved = resolve_key(he2)
        assert resolved.refusal is not None and "profile he1" in resolved.refusal

    def test_two_profiles_holding_a_key_only_in_their_settings_are_not_told(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changed in 0.7: another profile's .env is never read, so two pre-0.6
        set-up profiles sharing one key only there are not detected across
        processes until one of them saves it (``doctor --fix``, ``login``)."""
        he1, he2 = _profile("he1"), _profile("he2")
        set_up_as_agent(he2)
        _env(he1, f"{ENV_API_KEY}={ONE}\n")  # left as it was: never read
        _scope(monkeypatch, he2, **{ENV_API_KEY: ONE})
        resolved = resolve_key(he2)
        assert (resolved.api_key, resolved.refusal, resolved.shared_with) == (ONE, None, ())

    def test_two_profiles_one_process_resolves_with_one_key_are_told_root_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One multiplexed gateway resolves every profile in its own scope: two that
        hold one key in their settings are said to be one agent, the default first."""
        root, ops = profile_home(), _profile("ops")
        set_up_as_agent(ops, "ops")
        _scope(monkeypatch, root, **{ENV_API_KEY: ONE})
        assert resolve_key(root).shared_with == ()
        _scope(monkeypatch, ops, **{ENV_API_KEY: ONE})
        ops_resolved = resolve_key(ops)
        assert ops_resolved.refusal is None and [(h.home, h.where) for h in ops_resolved.shared_with] == [(root, "settings")]
        assert "also holds this key in its settings" in config.shared_note(ops_resolved.shared_with[0])
        monkeypatch.setenv("HERMES_HOME", str(root))
        assert [h.home for h in resolve_key(root).shared_with] == [ops]

    def test_a_named_profile_never_set_up_is_a_likely_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """What `hermes profile create` makes of the launch profile's .env: a named
        profile with a key in its settings and no AgentChat placement. Refused,
        with the way out if it is meant to be its own agent; nothing starts."""
        clone = _profile("he2")
        _scope(monkeypatch, clone, **{ENV_API_KEY: ONE})
        resolved = resolve_key(clone)
        assert resolved.api_key is None and resolved.refusal is not None
        assert "never set up as an AgentChat agent" in resolved.refusal
        assert "hermes -p he2 agentchat logout" in resolved.refusal
        assert "run `hermes -p he2 agentchat login` with it" in resolved.refusal
        assert ONE not in resolved.refusal
        with pytest.raises(KeyRefused):
            load_config(hermes_home=clone)
        set_up_as_agent(clone)
        assert resolve_key(clone).api_key == ONE, "an identity line placed: its own agent"

    def test_the_hosted_tools_entry_counts_as_set_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clone = _profile("he2")
        (clone / "config.yaml").write_text(
            'mcp_servers:\n  agentchat:\n    headers:\n      Authorization: "Bearer ${AGENTCHAT_API_KEY}"\n',
            encoding="utf-8",
        )
        _scope(monkeypatch, clone, **{ENV_API_KEY: ONE})
        assert resolve_key(clone).api_key == ONE

    def test_the_default_profile_is_never_a_likely_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _scope(monkeypatch, profile_home(), **{ENV_API_KEY: ONE})
        resolved = resolve_key()
        assert (resolved.api_key, resolved.refusal) == (ONE, None)

    def test_a_never_set_up_copy_places_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import _register

        clone = _profile("he2")
        _scope(monkeypatch, clone, **{ENV_API_KEY: ONE})
        touched: list[str] = []
        monkeypatch.setattr(_register, "_try_start_runtime", lambda *a, **k: touched.append("runtime"))
        for name in ("_reconcile_soul_anchor", "_reconcile_key_store", "_reconcile_secrets_block", "_reconcile_mcp_registration"):
            monkeypatch.setattr(_register, name, lambda *_a, n=name: touched.append(n))
        watcher = _register.ActivationWatcher(hermes_home=clone, profile="he2", gateway_mode=True)
        assert watcher.try_activate() == "refused"
        assert touched == []
        assert not (clone / "SOUL.md").exists() and not (clone / "config.yaml").exists()

    def test_two_key_files_holding_one_key_are_told(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both profiles saved the same key (two logins): both keep working and it
        is said (doctor fails on it)."""
        he1, he2 = _profile("he1"), _profile("he2")
        save_key(ONE, home=he1)
        save_key(ONE, home=he2)
        monkeypatch.setenv("HERMES_HOME", str(he2))
        resolved = resolve_key(he2)
        assert resolved.api_key == ONE and resolved.refusal is None
        assert [h.home for h in resolved.shared_with] == [he1]
        note = config.shared_note(resolved.shared_with[0])
        assert "profile he1" in note and "one AgentChat agent" in note and "hermes -p he1 agentchat logout" in note
        cfg = load_config(hermes_home=he2)
        assert cfg is not None and any("profile he1" in n for n in cfg.key_notes), "logged at activation"

    def test_two_different_names_are_said(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_API_KEY, ONE)
        monkeypatch.setenv(ENV_API_KEY_LEGACY, TWO)
        resolved = resolve_key()
        assert resolved.api_key == ONE and resolved.source == ENV_API_KEY
        assert any("hold different keys" in n for n in resolved.notes)


class TestSharedActivation:
    def test_the_sharing_is_logged_at_activation(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        he1, he2 = _profile("he1"), _profile("he2")
        save_key(ONE, home=he1)
        save_key(ONE, home=he2)
        monkeypatch.setenv("HERMES_HOME", str(he2))
        runtime = SimpleNamespace(identity=SimpleNamespace(handle="x"), hermes_home=he2)
        monkeypatch.setattr(_register, "_try_start_runtime", lambda *a, **k: runtime)
        for name in ("_reconcile_soul_anchor", "_reconcile_key_store", "_reconcile_secrets_block", "_reconcile_mcp_registration"):
            monkeypatch.setattr(_register, name, lambda *_a: None)
        watcher = _register.ActivationWatcher(hermes_home=he2, profile="he2", gateway_mode=True)
        with caplog.at_level(logging.WARNING, logger="agentchatme_hermes._register"):
            assert watcher.try_activate() == "active"
        assert any("profile he1" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


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


class TestPreV3KeyAtActivation:
    """A profile set up by a release before v3 (the catalog's August build) has
    only ``AGENTCHATME_API_KEY``. The hosted tools read ``AGENTCHAT_API_KEY``,
    which only the key file supplies: once the server has accepted the key (the
    runtime resolved its identity), the profile copies it into its key file —
    the root, and a named profile set up as an agent — unless another profile's
    key file holds it, under the per-account lock."""

    @staticmethod
    def _watcher(monkeypatch: pytest.MonkeyPatch, home: Path, *, verified: bool = True) -> tuple[Any, list[Any]]:
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        started: list[Any] = []

        def start(config: Any, **_kw: Any) -> Any:
            started.append(config)
            if not verified:
                return None  # the server refused the key (revoked): no runtime
            return SimpleNamespace(identity=SimpleNamespace(handle="x"), hermes_home=home, stop=MagicMock())

        monkeypatch.setattr(_register, "_try_start_runtime", start)
        for name in ("_reconcile_soul_anchor", "_reconcile_key_store", "_reconcile_secrets_block", "_reconcile_mcp_registration"):
            monkeypatch.setattr(_register, name, lambda *_a: None)
        monkeypatch.setattr(key_source, "source_supported", lambda: True)
        return _register.ActivationWatcher(hermes_home=home, profile=home.name, gateway_mode=True), started

    @pytest.mark.parametrize("named", [False, True], ids=["root", "named"])
    def test_the_key_is_copied_into_the_key_file_after_the_server_accepted_it(
        self, monkeypatch: pytest.MonkeyPatch, named: bool
    ) -> None:
        home = _profile("growth") if named else profile_home()
        if named:
            set_up_as_agent(home, "growth")
        _scope(monkeypatch, home, **{ENV_API_KEY_LEGACY: ONE})
        watcher, started = self._watcher(monkeypatch, home)
        assert watcher.try_activate() == "active"
        assert [c.api_key for c in started] == [ONE]
        stored = key_store.read_store(home)
        assert stored.usable and stored.key is not None and stored.key.api_key == ONE

    def test_a_key_the_server_did_not_accept_is_never_saved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        home = profile_home()
        _scope(monkeypatch, home, **{ENV_API_KEY_LEGACY: ONE})
        watcher, started = self._watcher(monkeypatch, home, verified=False)
        assert watcher.try_activate() == "failed"
        assert len(started) == 1 and key_store.read_store(home).status == "missing"

    def test_a_named_profile_never_set_up_adopts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clone = _profile("clone")
        _scope(monkeypatch, clone, **{ENV_API_KEY_LEGACY: ONE})
        watcher, started = self._watcher(monkeypatch, clone)
        assert watcher.try_activate() == "refused"
        assert started == [] and key_store.read_store(clone).status == "missing"

    def test_a_clone_of_an_adopted_key_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        save_key(ONE)  # the default profile adopted it first
        clone = _profile("clone")
        set_up_as_agent(clone)  # a --clone copies SOUL.md too
        _scope(monkeypatch, clone, **{ENV_API_KEY_LEGACY: ONE})
        watcher, started = self._watcher(monkeypatch, clone)
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes._register"):
            assert watcher.try_activate() == "refused"
        assert started == [] and key_store.read_store(clone).status == "missing"
        assert any("same AgentChat key that profile default" in r.getMessage() for r in caplog.records)

    def test_taken_between_the_check_and_the_adoption_is_refused_and_stopped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two profiles holding one key: the other one's key file appears after this
        profile resolved its key. The adoption under the lock sees it: this
        profile's runtime is stopped and it is refused as the other's copy."""
        from agentchatme_hermes import credentials

        root = profile_home()
        clone = _profile("clone")
        set_up_as_agent(clone)
        _scope(monkeypatch, clone, **{ENV_API_KEY_LEGACY: ONE})
        watcher, started = self._watcher(monkeypatch, clone)
        real = credentials.adopt_legacy_key

        def root_first(**kw: Any) -> Any:
            save_key(ONE, home=root)  # the launch profile adopted it meanwhile
            return real(**kw)

        monkeypatch.setattr(credentials, "adopt_legacy_key", root_first)
        assert watcher.try_activate() == "refused"
        assert len(started) == 1 and watcher.runtime is None
        assert key_store.read_store(clone).status == "missing"
        assert config.copy_of(clone) == root

    def test_another_profile_adopting_the_same_key_right_now_stops_this_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-account lock is held (another profile is adopting the same key):
        this attempt keeps nothing; the next one is decided by that profile's key file."""
        import contextlib

        from agentchatme_hermes import leader_lock

        clone = _profile("clone")
        set_up_as_agent(clone)
        _scope(monkeypatch, clone, **{ENV_API_KEY_LEGACY: ONE})
        watcher, started = self._watcher(monkeypatch, clone)

        @contextlib.contextmanager
        def busy(_path: Path, **_kw: Any) -> Any:
            yield "busy"

        monkeypatch.setattr(leader_lock, "held_lock", busy)
        assert watcher.try_activate() == "failed"
        assert len(started) == 1 and watcher.runtime is None
        assert key_store.read_store(clone).status == "missing"


class TestSplitIdentity:
    """The socket and the gate act as the key file's agent; the hosted tools as
    whatever ``AGENTCHAT_API_KEY`` Hermes' MCP client gets. With the key source
    off, a different key there would be another agent: nothing starts."""

    def _watcher(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[Any]]:
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        started: list[Any] = []
        monkeypatch.setattr(
            _register,
            "_try_start_runtime",
            lambda config, **_kw: started.append(config) or SimpleNamespace(identity=SimpleNamespace(handle="x"), hermes_home=profile_home()),
        )
        for name in ("_reconcile_soul_anchor", "_reconcile_key_store", "_reconcile_secrets_block", "_reconcile_mcp_registration"):
            monkeypatch.setattr(_register, name, lambda *_a: None)
        return _register.ActivationWatcher(hermes_home=profile_home(), profile="default", gateway_mode=True), started

    def test_the_source_off_and_another_key_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from agentchatme_hermes import credentials

        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, TWO)
        monkeypatch.setattr(credentials, "key_bridge", lambda: (False, "secrets.agentchat is disabled"))
        monkeypatch.setattr(credentials, "source_carries_key", lambda: False)
        watcher, started = self._watcher(monkeypatch)
        with caplog.at_level(logging.ERROR, logger="agentchatme_hermes._register"):
            assert watcher.try_activate() == "refused"
        assert started == []
        (line,) = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert "they would act as another agent" in line and "doctor --fix` places secrets.agentchat" in line
        assert ONE not in line and TWO not in line

    def test_the_source_on_the_key_file_decides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import credentials

        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, TWO)  # supplied over by the source
        monkeypatch.setattr(credentials, "key_bridge", lambda: (True, ""))
        watcher, started = self._watcher(monkeypatch)
        assert watcher.try_activate() == "active" and [c.api_key for c in started] == [ONE]

    def test_the_source_off_and_the_same_key_starts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import credentials

        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, ONE)
        monkeypatch.setattr(credentials, "key_bridge", lambda: (False, "no secret sources"))
        monkeypatch.setattr(credentials, "source_carries_key", lambda: False)
        watcher, started = self._watcher(monkeypatch)
        assert watcher.try_activate() == "active" and [c.api_key for c in started] == [ONE]


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

    def test_doctor_fails_on_a_shared_key_and_flags_old_snapshots(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = profile_home()
        ops = _profile("ops")
        save_key(ONE, home=ops)
        save_key(ONE, home=root)
        scratch = ops / "cache" / "scratch"
        scratch.mkdir(parents=True)
        (scratch / "hermes-snap-abc.sh").write_text(f"declare -x {ENV_API_KEY}={ONE}\n", encoding="utf-8")
        (scratch / "hermes-snap-def.sh").write_text("declare -x PATH=/bin\n", encoding="utf-8")
        self._in(monkeypatch, ops)
        monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: True)
        monkeypatch.setitem(sys.modules, "agentchatme", None)  # stop after the key checks
        rc = cli._dispatch_doctor(argparse.Namespace(fix=False))
        out = capsys.readouterr().out
        assert rc >= 1
        assert "[fail] profile default (" in out and "the two profiles are one AgentChat agent" in out
        assert "[warn] 1 Hermes terminal snapshot(s)" in out and "recover` to rotate it" in out
        assert ONE not in out

    def test_doctor(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        root = profile_home()
        save_key(ONE, home=root)
        clone = _hermes_profile_create(root, "he2")
        self._in(monkeypatch, clone)
        monkeypatch.setenv(ENV_API_KEY, ONE)  # the clone's settings: its copied .env
        monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: True)
        rc = cli._dispatch_doctor(argparse.Namespace(fix=False))
        out = capsys.readouterr().out
        assert rc >= 1
        assert "[fail] This profile's settings hold the same AgentChat key that profile default" in out
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
        monkeypatch.setattr(cli, "_uninstall_soul_anchor", lambda _identity: None)
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

    def test_logout_of_a_settings_copy_removes_its_line_through_hermes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = profile_home()
        save_key(ONE)
        clone = _profile("he2")
        hermes = _scope(monkeypatch, clone, **{ENV_API_KEY: ONE})
        monkeypatch.setattr(cli, "_uninstall_soul_anchor", lambda _identity: None)
        monkeypatch.setattr(cli, "_uninstall_secrets_registration", lambda: None)
        monkeypatch.setattr(cli, "_uninstall_mcp_registration", lambda: None)
        assert resolve_key(clone).refusal is not None
        assert cli._dispatch_logout(argparse.Namespace()) == 0
        out = capsys.readouterr().out
        assert "the profile .env (AGENTCHAT_API_KEY)" in out and "still set" not in out
        assert hermes.file(clone) == {} and config.copy_of(clone) is None
        assert key_store.read_store(root).usable, "the original keeps its key"

    def test_logout_says_what_the_environment_still_sets(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _scope(monkeypatch, profile_home())
        monkeypatch.setenv(ENV_API_KEY, ONE)  # exported, in no .env line
        monkeypatch.setattr(cli, "_uninstall_soul_anchor", lambda _identity: None)
        monkeypatch.setattr(cli, "_uninstall_secrets_registration", lambda: None)
        monkeypatch.setattr(cli, "_uninstall_mcp_registration", lambda: None)
        assert cli._dispatch_logout(argparse.Namespace()) == 0
        assert "AGENTCHAT_API_KEY is still set in this process's environment" in capsys.readouterr().out


class TestProfileAwareHints:
    """R2-F05: hints name the profile (`hermes -p <name> agentchat …`)."""

    def test_status_without_a_key_in_a_named_profile(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentchatme_hermes import profile_scope

        home = profile_home() / "profiles" / "ms63"
        home.mkdir(parents=True)
        monkeypatch.setattr(profile_scope, "resolve_hermes_home", lambda: home)
        assert cli._dispatch_status(argparse.Namespace()) == 0
        assert (
            "No AgentChat key configured. Run `hermes -p ms63 agentchat register` or `hermes -p ms63 agentchat login`."
        ) in capsys.readouterr().out

    def test_the_command_line_key_warning_names_the_profile(self) -> None:
        line = cli._argv_key_warning("hermes -p ms63 agentchat")
        assert "hermes -p ms63 agentchat login --api-key-file <path>" in line
        assert "hermes agentchat login" not in line


class TestGitCopiesInSeveralProfiles:
    """T5-F07: Hermes installs one plugin package for every profile; a second
    profile's git install fails ("Two workspace members are both named
    `agentchatme-hermes`") and its copy stays, disabled."""

    def _copy(self, home: Path) -> None:
        plugin = home / "plugins" / "agentchat"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text("name: agentchat\nversion: 0.6.3\n", encoding="utf-8")

    def test_two_profiles_with_a_copy_get_the_remedy(self) -> None:
        root = profile_home()
        ms63 = root / "profiles" / "ms63"
        self._copy(root)
        self._copy(ms63)
        (root / "profiles" / "other").mkdir(parents=True)
        for here in (root, ms63):
            line = cli._git_copies_line(here) or ""
            assert line.startswith("agentchat is installed from git in 2 profiles (default, ms63).")
            assert "Two workspace members are both named `agentchatme-hermes`" in line
            assert line.endswith(
                "Keep the copy in default; in ms63: `hermes -p ms63 plugins remove agentchat`, then "
                "`hermes -p ms63 plugins enable agentchat` (enable uses the package already installed)"
            )

    def test_one_copy_says_nothing(self) -> None:
        root = profile_home()
        self._copy(root)
        assert cli._git_copies_line(root) is None
        assert cli._git_copies_line(root / "profiles" / "ms63") is None


class TestLogoutWithAGatewayRunning:
    """A running gateway keeps the socket it opened with the key: logout says
    so, and how to stop it, when a gateway lives for the account (the
    ``doctor`` / ``status`` rule: a fresh record, its pid alive, the lock held)."""

    def _quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_uninstall_soul_anchor", lambda _identity: None)
        monkeypatch.setattr(cli, "_uninstall_secrets_registration", lambda: None)
        monkeypatch.setattr(cli, "_uninstall_mcp_registration", lambda: None)

    @pytest.mark.skipif(sys.platform == "win32", reason="the leader lock is fcntl, POSIX-only")
    def test_a_live_gateway_for_the_account_is_said(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import os
        import time

        from agentchatme_hermes.leader_lock import (
            lock_path_for,
            release_leader_lock,
            try_acquire_ws_leader_lock,
        )
        from agentchatme_hermes.profile_scope import account_fingerprint
        from agentchatme_hermes.ws_health import health_path_for, write_ws_health

        home = profile_home()
        save_key(ONE)
        self._quiet(monkeypatch)
        lock = lock_path_for(hermes_home=home, account_fingerprint=account_fingerprint(cli._api_base(), ONE))
        health = health_path_for(lock)
        note = (
            f"The running gateway (pid {os.getpid()}) still holds @alice's AgentChat connection, so this profile "
            "is still woken and gated, now without the hosted tools: restart it (`hermes gateway restart`) so it "
            "stops serving this profile."
        )

        write_ws_health(handle="alice", state="connected", path=health)
        assert cli._dispatch_logout(argparse.Namespace()) == 0
        assert note not in capsys.readouterr().out, "the lock is free: no gateway lives behind the record"

        fd = try_acquire_ws_leader_lock(lock)
        assert fd is not None
        try:
            save_key(ONE)
            write_ws_health(handle="alice", state="connected", path=health, now=time.time() - 3600)
            assert cli._dispatch_logout(argparse.Namespace()) == 0
            assert note not in capsys.readouterr().out, "a stale record is not a live gateway"

            save_key(ONE)
            write_ws_health(handle="alice", state="connected", path=health)
            assert cli._dispatch_logout(argparse.Namespace()) == 0
            out = capsys.readouterr().out
        finally:
            release_leader_lock(fd)
        assert "AgentChat key removed from this profile" in out
        assert out.rstrip().endswith(note)
        assert ONE not in out

    def test_without_a_gateway_nothing_is_said(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        save_key(ONE)
        self._quiet(monkeypatch)
        assert cli._dispatch_logout(argparse.Namespace()) == 0
        assert "still holds" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("host_pid", "restart"),
        [
            # MS-F17: a multiplexing gateway (the default profile's) holds the socket:
            # `hermes -p growth gateway restart` reconnects only Hermes' adapters there.
            (4242, "restart it (`hermes gateway restart`: the default profile's gateway serves this profile)"),
            (1111, "restart it (`hermes -p growth gateway restart`: this profile's own gateway)"),
            (None, "restart it (`hermes gateway restart` when the default profile's gateway serves this profile, "
                   "else `hermes -p growth gateway restart`)"),
        ],
        ids=["served-by-the-default-gateway", "own-gateway", "cannot-tell"],
    )
    def test_a_named_profile_names_the_restart_that_frees_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], host_pid: int | None, restart: str
    ) -> None:
        from agentchatme_hermes import placement, profile_scope, ws_health

        home = profile_home() / "profiles" / "growth"
        home.mkdir(parents=True)
        monkeypatch.setattr(profile_scope, "resolve_hermes_home", lambda: home)
        save_key(ONE, home=home, handle="growth-bot")
        self._quiet(monkeypatch)
        seen: list[tuple[str, Path | None]] = []

        def live(api_base: str, key: str, hermes_home: Path | None = None) -> dict[str, Any]:
            seen.append((key, hermes_home))
            return {"pid": 4242, "handle": "growth-bot", "state": "connected"}

        monkeypatch.setattr(ws_health, "live_health", live)
        asked: list[Path] = []
        monkeypatch.setattr(placement, "default_gateway_pid", lambda h: asked.append(h) or host_pid)
        assert cli._dispatch_logout(argparse.Namespace()) == 0
        out = capsys.readouterr().out
        assert seen == [(ONE, home)], "read with the key, before it is removed"
        assert asked == [home]
        assert (
            "The running gateway (pid 4242) still holds @growth-bot's AgentChat connection, so this profile is "
            f"still woken and gated, now without the hosted tools: {restart} so it stops serving this profile."
        ) in out

    def test_the_default_gateway_pid_is_hermes_scoped_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import types as _types

        from agentchatme_hermes.placement import default_gateway_pid

        calls: list[tuple[Any, bool]] = []
        status = _types.ModuleType("gateway.status")
        status.get_running_pid = lambda pid_path=None, *, cleanup_stale=True: calls.append((pid_path, cleanup_stale)) or 4242  # type: ignore[attr-defined]
        package = _types.ModuleType("gateway")
        package.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "gateway", package)
        monkeypatch.setitem(sys.modules, "gateway.status", status)
        root = tmp_path / ".hermes"
        assert default_gateway_pid(root / "profiles" / "growth") == 4242
        assert calls == [(root / "gateway.pid", False)], "the root's files, read-only"
        assert default_gateway_pid(root) is None, "the default profile itself"
        monkeypatch.setitem(sys.modules, "gateway.status", None)
        assert default_gateway_pid(root / "profiles" / "growth") is None, "no Hermes: cannot tell"

    @pytest.mark.parametrize("profile", ["default", "growth"])
    def test_the_wizard_logout_says_it_and_names_the_profile(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], profile: str
    ) -> None:
        from agentchatme_hermes import (
            mcp_registration,
            profile_scope,
            secrets_registration,
            wizard,
            ws_health,
        )

        home = profile_home() if profile == "default" else profile_home() / "profiles" / profile
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(profile_scope, "resolve_hermes_home", lambda: home)
        monkeypatch.setattr(wizard, "resolve_hermes_home", lambda: home)
        save_key(ONE, home=home)
        monkeypatch.setattr(secrets_registration, "uninstall_block", lambda *a, **k: False)
        monkeypatch.setattr(mcp_registration, "uninstall_entry", lambda *a, **k: False)
        monkeypatch.setattr(
            ws_health, "live_health", lambda *_a, **_k: {"pid": 77, "handle": "alice", "state": "connected"}
        )
        info: list[str] = []
        done: list[str] = []
        wizard._logout_flow(lambda *_a: True, info.append, done.append, lambda *_a: None)
        assert key_store.read_store(home).status == "missing"
        assert [line for line in info if "still holds @alice's AgentChat connection" in line] != []
        cmd = "hermes agentchat" if profile == "default" else f"hermes -p {profile} agentchat"
        assert done == [f"Logged out. Run `{cmd}` to reconfigure."]


class TestDoctorFixKeepsAKeyTheToolsWouldLose:
    def test_without_the_bridge_the_key_stays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import credentials

        root = profile_home()
        hermes = _scope(monkeypatch, root, **{ENV_API_KEY: ONE})
        monkeypatch.setattr(credentials, "key_bridge", lambda: (False, "this Hermes has no secret sources"))
        lines = cli._fix_key(resolve_key(root), handle="alice", api_base="https://api.agentchat.me", home=root)
        assert lines == [("fail", "fix: the key stays in this profile's settings — this Hermes has no secret sources")]
        assert hermes.file(root) == {ENV_API_KEY: ONE} and hermes.removed == []
        assert key_store.read_store(root).status == "missing"


class TestDoctorFixMovesTheKey:
    def test_fix_moves_a_settings_key_into_the_key_file(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentchatme_hermes import kit, mcp_registration, secrets_registration

        root = profile_home()
        hermes = _scope(monkeypatch, root, KEEP="1", **{ENV_API_KEY: ONE})
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
        monkeypatch.setattr(cli, "_doctor_identity_texts", lambda *a, **k: (None, None, None))
        monkeypatch.setattr(cli, "place_identity_and_tools", lambda **_kw: [])
        monkeypatch.setattr(kit, "kit_problem", lambda: None)
        monkeypatch.setattr(mcp_registration, "inspect_entry_identity", lambda **_kw: ("ours", "current"))
        monkeypatch.setattr(secrets_registration, "inspect_block", lambda: "ours")
        from agentchatme_hermes import credentials

        monkeypatch.setattr(credentials, "key_bridge", lambda: (True, ""))

        cli._dispatch_doctor(argparse.Namespace(fix=True))
        out = capsys.readouterr().out
        assert "[warn] key read from this profile's settings as AGENTCHAT_API_KEY" in out
        assert "[ok]   fix: moved the key from this profile's settings to" in out
        assert hermes.file(root) == {"KEEP": "1"}
        record = key_store.read_store(root)
        assert record.usable and record.key is not None and (record.key.api_key, record.key.handle) == (ONE, "alice")
        assert ONE not in out
        assert config.resolve_key(root).source == KEY_SOURCE_STORE


class TestTheKeyFileDecidesDoctorLines:
    """What ``doctor`` says in each case where the key file decides (it cannot
    see an ``AGENTCHAT_API_KEY`` in .env: the key source supplies the key
    file's over it)."""

    @staticmethod
    def _key_lines(resolution: Any) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        cli._doctor_key_checks(
            resolution,
            cmd="hermes agentchat",
            ok=lambda t: lines.append(("ok", t)),
            warn=lambda t: lines.append(("warn", t)),
            fail=lambda t: lines.append(("fail", t)),
        )
        return lines

    def test_the_key_file_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, TWO)  # invisible to the decision
        (line,) = self._key_lines(resolve_key())
        assert line[0] == "ok" and "the key file decides which agent this profile is" in line[1]
        assert "AGENTCHAT_API_KEY in the profile .env or the environment is not used" in line[1]

    def test_a_leftover_pre_v3_line_is_removed_by_fix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = profile_home()
        save_key(ONE)
        hermes = _scope(monkeypatch, root, **{ENV_API_KEY_LEGACY: ONE})
        lines = self._key_lines(resolve_key())
        assert ("warn" in {lvl for lvl, _ in lines}) and any("pre-v3 name" in t and "doctor --fix` removes it" in t for _, t in lines)
        fixed = cli._fix_key(resolve_key(), handle="alice", api_base="https://api.agentchat.me", home=root)
        assert fixed == [("ok", "fix: removed AGENTCHATME_API_KEY from the profile .env")]
        assert hermes.file(root) == {}

    def test_an_exported_pre_v3_key_is_said_not_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = profile_home()
        save_key(ONE)
        _scope(monkeypatch, root)
        monkeypatch.setenv(ENV_API_KEY_LEGACY, ONE)  # set by the service, in no .env line
        fixed = cli._fix_key(resolve_key(), handle="alice", api_base="https://api.agentchat.me", home=root)
        assert fixed == [(
            "warn",
            "fix: AGENTCHATME_API_KEY is not in the profile .env — it is set in the environment (the shell or "
            "the service): unset it there",
        )]

    def test_a_different_pre_v3_key_is_said_and_left(self, monkeypatch: pytest.MonkeyPatch) -> None:
        root = profile_home()
        save_key(ONE)
        hermes = _scope(monkeypatch, root, **{ENV_API_KEY_LEGACY: TWO})
        lines = self._key_lines(resolve_key())
        assert any(lvl == "warn" and "holds a different key; it is not used" in t for lvl, t in lines)
        assert cli._fix_key(resolve_key(), handle="alice", api_base="https://api.agentchat.me", home=root) == []
        assert hermes.file(root) == {ENV_API_KEY_LEGACY: TWO}

    def test_the_tools_would_act_as_another_agent_without_the_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """secrets.agentchat missing: Hermes' MCP client takes AGENTCHAT_API_KEY
        from the settings, and a different key there is another agent."""
        from agentchatme_hermes import secrets_registration

        save_key(ONE)
        monkeypatch.setenv(ENV_API_KEY, TWO)
        monkeypatch.setattr(key_source, "source_supported", lambda: True)
        monkeypatch.setattr(secrets_registration, "inspect_block", lambda: "missing")
        monkeypatch.setattr(cli, "_hermes_strips_key_from_commands", lambda: None)
        lines: list[tuple[str, str]] = []
        cli._doctor_secrets_checks(
            resolve_key(),
            cmd="hermes agentchat",
            ok=lambda t: lines.append(("ok", t)),
            warn=lambda t: lines.append(("warn", t)),
            fail=lambda t: lines.append(("fail", t)),
        )
        assert any(lvl == "fail" and "they would act as another agent" in t for lvl, t in lines)
        assert TWO not in " ".join(t for _, t in lines)
        monkeypatch.setenv(ENV_API_KEY, ONE)
        lines.clear()
        cli._doctor_secrets_checks(
            resolve_key(),
            cmd="hermes agentchat",
            ok=lambda t: lines.append(("ok", t)),
            warn=lambda t: lines.append(("warn", t)),
            fail=lambda t: lines.append(("fail", t)),
        )
        assert not any("another agent" in t for _, t in lines)
