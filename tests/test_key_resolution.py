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
        config._snapshot_process_env()  # the plugin is imported
        assert resolve_key().refusal is not None
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


class TestTheEnvironmentSnapshot:
    def test_a_value_hermes_loaded_from_dotenv_is_not_an_export(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gateway started with a key in the profile .env (Hermes loads it into
        the environment). The user then moves to a new key: the old value still
        in the environment is not "exported in the shell", and fixing it takes
        effect without a restart."""
        root = profile_home()
        _env(root, f"{ENV_API_KEY}={ONE}\n")
        monkeypatch.setenv(ENV_API_KEY, ONE)  # Hermes' dotenv load at process start
        config._snapshot_process_env()  # the plugin is imported
        assert resolve_key(root).api_key == ONE
        (root / ".env").write_text("", encoding="utf-8")  # login with another key removed it
        save_key(TWO)
        resolved = resolve_key(root)
        assert (resolved.api_key, resolved.refusal) == (TWO, None)


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
        assert [h.home for h in root_resolved.shared_with] == [clone], "said in the original too"

    def test_a_dotenv_key_another_profile_saved_in_its_key_file_is_refused(self) -> None:
        he1, he2 = _profile("he1"), _profile("he2")
        save_key(ONE, home=he1)
        _env(he2, f"{ENV_API_KEY}={ONE}\n")
        resolved = resolve_key(he2)
        assert resolved.refusal is not None and "profile he1" in resolved.refusal

    def test_two_named_profiles_sharing_a_dotenv_key_are_told(self) -> None:
        """Before 0.6 nothing tells which of two set-up profiles is the one in
        use: both keep working as before, and it is said (doctor fails on it)."""
        he1, he2 = _profile("he1"), _profile("he2")
        _env(he1, f"{ENV_API_KEY}={ONE}\n")
        _env(he2, f"{ENV_API_KEY}={ONE}\n")
        resolved = resolve_key(he2)
        assert resolved.api_key == ONE and resolved.refusal is None
        assert [h.home for h in resolved.shared_with] == [he1]
        note = config.shared_note(resolved.shared_with[0])
        assert "profile he1" in note and "one AgentChat agent" in note and "hermes -p he1 agentchat logout" in note
        cfg = load_config(hermes_home=he2)
        assert cfg is not None and any("profile he1" in n for n in cfg.key_notes), "logged at activation"

    def test_a_dormant_default_does_not_take_a_set_up_named_profile_offline(self) -> None:
        """The default profile still holds the key in .env from long ago; the
        named profile is the one that was set up (identity line placed): it
        keeps working, with the sharing said."""
        root = profile_home()
        _env(root, f"{ENV_API_KEY}={ONE}\n")
        ops = _profile("ops")
        _env(ops, f"{ENV_API_KEY}={ONE}\n")
        (ops / "SOUL.md").write_text("<!-- agentchat:start -->\nI am @ops\n<!-- agentchat:end -->\n", encoding="utf-8")
        resolved = resolve_key(ops)
        assert resolved.api_key == ONE and resolved.refusal is None
        assert [h.home for h in resolved.shared_with] == [root]

    def test_a_clone_with_the_tools_entry_is_shared_not_refused(self) -> None:
        root = profile_home()
        _env(root, f"{ENV_API_KEY}={ONE}\n")
        (root / "config.yaml").write_text(
            'mcp_servers:\n  agentchat:\n    headers:\n      Authorization: "Bearer ${AGENTCHAT_API_KEY}"\n',
            encoding="utf-8",
        )
        clone = _hermes_profile_create(root, "he2")  # --clone: .env and config.yaml
        resolved = resolve_key(clone)
        assert resolved.refusal is None and [h.home for h in resolved.shared_with] == [root]


class TestSharedActivation:
    def test_the_sharing_is_logged_at_activation(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        from types import SimpleNamespace

        from agentchatme_hermes import _register

        he1, he2 = _profile("he1"), _profile("he2")
        _env(he1, f"{ENV_API_KEY}={ONE}\n")
        _env(he2, f"{ENV_API_KEY}={ONE}\n")
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
        _env(root, f"{ENV_API_KEY}={ONE}\n")
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
        monkeypatch.setattr(cli, "_load_served_texts", lambda *a, **k: None)
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
