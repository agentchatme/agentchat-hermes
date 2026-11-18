"""Saving, moving and clearing the key — and that it never lands in the profile ``.env``.

Up to 0.5.3 every register / login / recover wrote ``AGENTCHAT_API_KEY``
into the profile ``.env``. Hermes loads that file into its process environment,
so the key was in the environment of every command a reply turn ran (and in
Hermes' terminal env snapshots), and ``hermes profile create`` copied it into
every new profile.
"""
from __future__ import annotations

import os
import stat
import sys
import types
from typing import TYPE_CHECKING, Any

import pytest
from fake_keys import api_key
from key_files import profile_home, saved
from sdk_wire import answer, serve

from agentchatme_hermes import cli, credentials, key_store

if TYPE_CHECKING:
    from pathlib import Path

POSIX = os.name != "nt"
BASE = "https://api.agentchat.me"


class _HermesEnv:
    """``hermes_cli.config``'s ``.env`` helpers on the profile's real ``.env`` file."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.saved: list[tuple[str, str]] = []
        self.removed: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = types.ModuleType("hermes_cli.config")
        module.get_env_path = lambda: self.home / ".env"  # type: ignore[attr-defined]
        module.save_env_value = self.save  # type: ignore[attr-defined]
        module.remove_env_value = self.remove  # type: ignore[attr-defined]
        package = types.ModuleType("hermes_cli")
        package.config = module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "hermes_cli", package)
        monkeypatch.setitem(sys.modules, "hermes_cli.config", module)

    def save(self, name: str, value: str) -> None:
        self.saved.append((name, value))
        env = self.home / ".env"
        self.home.mkdir(parents=True, exist_ok=True)
        text = env.read_text(encoding="utf-8") if env.exists() else ""
        env.write_text(text + f"{name}={value}\n", encoding="utf-8")

    def remove(self, name: str) -> bool:
        self.removed.append(name)
        return credentials.remove_env_lines(self.home / ".env", (name,))


def _env(home: Path, text: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".env"
    path.write_text(text, encoding="utf-8")
    return path


class TestSave:
    def test_the_key_goes_to_the_key_file_and_leaves_dotenv(self, tmp_path: Path) -> None:
        home = tmp_path / ".hermes"
        env = _env(home, f"# mine\nOPENAI_API_KEY=x\nAGENTCHAT_API_KEY={api_key('old')}\nAGENTCHATME_HANDLE=alice\n")
        outcome = credentials.save_credentials(api_key=api_key("new"), handle="alice", api_base=BASE, home=home)
        assert outcome.path == key_store.store_path(home) and outcome.fallback_reason is None
        assert outcome.env_lines_removed == ("AGENTCHAT_API_KEY",)
        # Every other line of the user's file is as it was.
        assert env.read_text(encoding="utf-8") == "# mine\nOPENAI_API_KEY=x\nAGENTCHATME_HANDLE=alice\n"
        assert saved(home).api_key == api_key("new")  # type: ignore[union-attr]

    def test_both_names_leave_dotenv(self, tmp_path: Path) -> None:
        home = tmp_path / ".hermes"
        env = _env(home, f"AGENTCHATME_API_KEY={api_key('a')}\nexport AGENTCHAT_API_KEY='{api_key('a')}'\n")
        credentials.save_credentials(api_key=api_key("a"), handle="a", api_base=BASE, home=home)
        assert env.read_text(encoding="utf-8") == ""

    def test_the_dotenv_line_goes_only_after_the_key_file_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never a moment with no saved copy: a key file that cannot be
        written keeps the key in .env (where Hermes keeps secrets) and says so."""
        home = tmp_path / ".hermes"
        _env(home, f"AGENTCHAT_API_KEY={api_key('old')}\n")
        hermes = _HermesEnv(home)
        hermes.install(monkeypatch)

        def no_disk(*_a: Any, **_k: Any) -> Path:
            raise key_store.KeyStoreError("cannot write: read-only file system")

        monkeypatch.setattr(key_store, "write_store", no_disk)
        outcome = credentials.save_credentials(api_key=api_key("new"), handle="a", api_base=BASE, home=home)
        assert outcome.path is None and "read-only" in str(outcome.fallback_reason)
        assert hermes.saved == [("AGENTCHAT_API_KEY", api_key("new"))]
        assert hermes.removed == []

    def test_uses_hermes_remover_for_the_active_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / ".hermes"
        _env(home, f"AGENTCHAT_API_KEY={api_key('old')}\n")
        hermes = _HermesEnv(home)
        hermes.install(monkeypatch)
        credentials.save_credentials(api_key=api_key("new"), handle="a", api_base=BASE, home=home)
        assert hermes.removed == ["AGENTCHAT_API_KEY"]
        assert hermes.saved == [], "nothing written to .env"

    @pytest.mark.skipif(not POSIX, reason="POSIX modes")
    def test_the_dotenv_keeps_its_mode(self, tmp_path: Path) -> None:
        home = tmp_path / ".hermes"
        env = _env(home, f"A=1\nAGENTCHAT_API_KEY={api_key('k')}\n")
        os.chmod(env, 0o600)
        credentials.save_credentials(api_key=api_key("k"), handle="a", api_base=BASE, home=home)
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert env.read_text(encoding="utf-8") == "A=1\n"


class TestMove:
    def test_doctor_fix_moves_the_dotenv_key(self, tmp_path: Path) -> None:
        home = tmp_path / ".hermes"
        env = _env(home, f"X=1\nAGENTCHAT_API_KEY={api_key('k')}\n")
        moved = credentials.move_env_key(handle="alice", api_base=BASE, home=home)
        assert moved.moved and moved.env_lines_removed == ("AGENTCHAT_API_KEY",)
        assert env.read_text(encoding="utf-8") == "X=1\n"
        assert saved(home).handle == "alice"  # type: ignore[union-attr]

    def test_only_the_line_holding_the_moved_key_goes(self, tmp_path: Path) -> None:
        home = tmp_path / ".hermes"
        env = _env(home, f"AGENTCHAT_API_KEY={api_key('k')}\nAGENTCHATME_API_KEY={api_key('other')}\n")
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=home)
        assert moved.env_lines_removed == ("AGENTCHAT_API_KEY",)
        assert env.read_text(encoding="utf-8") == f"AGENTCHATME_API_KEY={api_key('other')}\n"

    def test_nothing_to_move(self, tmp_path: Path) -> None:
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=tmp_path)
        assert not moved.moved and "no key" in moved.detail

    def test_a_failed_write_moves_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / ".hermes"
        env = _env(home, f"AGENTCHAT_API_KEY={api_key('k')}\n")

        def no_disk(*_a: Any, **_k: Any) -> Path:
            raise key_store.KeyStoreError("disk full")

        monkeypatch.setattr(key_store, "write_store", no_disk)
        moved = credentials.move_env_key(handle="a", api_base=BASE, home=home)
        assert not moved.moved and "stays in the profile .env" in moved.detail
        assert env.read_text(encoding="utf-8") == f"AGENTCHAT_API_KEY={api_key('k')}\n"


class TestClear:
    def test_logout_removes_the_file_and_the_lines(self, tmp_path: Path) -> None:
        home = tmp_path / ".hermes"
        env = _env(home, f"KEEP=1\nAGENTCHAT_API_KEY={api_key('k')}\nAGENTCHATME_HANDLE=alice\n")
        key_store.write_store(home, api_key=api_key("k"), handle="alice", api_base=BASE)
        removed_file, removed_lines = credentials.clear_credentials(home)
        assert removed_file is True
        assert set(removed_lines) == {"AGENTCHAT_API_KEY", "AGENTCHATME_HANDLE"}
        assert env.read_text(encoding="utf-8") == "KEEP=1\n"
        assert key_store.read_store(home).status == "missing"


class TestRemoveEnvLines:
    def test_comments_blanks_and_lookalikes_survive(self, tmp_path: Path) -> None:
        env = _env(
            tmp_path,
            "# AGENTCHAT_API_KEY=commented\n\nAGENTCHAT_API_KEY_OLD=1\n  AGENTCHAT_API_KEY = spaced\nZ=1",
        )
        assert credentials.remove_env_lines(env, ("AGENTCHAT_API_KEY",)) is True
        assert env.read_text(encoding="utf-8") == "# AGENTCHAT_API_KEY=commented\n\nAGENTCHAT_API_KEY_OLD=1\nZ=1"

    def test_no_file_no_change(self, tmp_path: Path) -> None:
        assert credentials.remove_env_lines(tmp_path / ".env", ("AGENTCHAT_API_KEY",)) is False


class TestKeyBridge:
    """The key leaves ``.env`` only when Hermes' MCP client can read the key file."""

    def test_without_secret_sources_the_key_stays_in_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentchatme_hermes import key_source

        monkeypatch.setattr(key_source, "source_supported", lambda: False)
        ready, why = credentials.key_bridge()
        assert ready is False and "no secret sources" in why
        home = tmp_path / ".hermes"
        hermes = _HermesEnv(home)
        hermes.install(monkeypatch)
        outcome = credentials.save_credentials(
            api_key=api_key("k"), handle="a", api_base=BASE, home=home, use_key_file=ready, not_ready=why
        )
        assert outcome.path is None and "no secret sources" in str(outcome.fallback_reason)
        assert hermes.saved == [("AGENTCHAT_API_KEY", api_key("k"))]
        assert key_store.read_store(home).status == "missing"

    def test_ready_when_the_block_turns_the_source_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import key_source, placement, secrets_registration

        monkeypatch.setattr(key_source, "source_supported", lambda: True)
        monkeypatch.setattr(placement, "place_secrets", lambda: placement.PlacementStep("secrets", "ok", ""))
        monkeypatch.setattr(secrets_registration, "inspect_source_enabled", lambda: True)
        assert credentials.key_bridge() == (True, "")
        monkeypatch.setattr(secrets_registration, "inspect_source_enabled", lambda: False)
        monkeypatch.setattr(
            placement, "place_secrets", lambda: placement.PlacementStep("secrets", "skipped", "disabled by hand")
        )
        assert credentials.key_bridge() == (False, "disabled by hand")


class TestLoginNeverWritesTheKeyToDotenv:
    """End to end through ``hermes agentchat login``: the key lands in the
    key file (owner-only) and the profile ``.env`` holds no key afterwards."""

    def test_login(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        assert_private_file: Any,
    ) -> None:
        import argparse
        import io

        home = profile_home()
        env = _env(home, f"OPENAI_API_KEY=sk-other\nAGENTCHAT_API_KEY={api_key('old')}\n")
        _HermesEnv(home).install(monkeypatch)
        serve(monkeypatch, {"/v1/agents/me": answer(200, {"handle": "alice", "status": "active"})})
        monkeypatch.setattr(cli, "_install_placement", lambda handle, *, api_base, api_key: [])
        monkeypatch.setattr(credentials, "key_bridge", lambda: (True, ""))
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{api_key('new')}\n"))
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        for name in ("AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY", "AGENTCHATME_API_BASE"):
            monkeypatch.delenv(name, raising=False)

        rc = cli._dispatch_login(argparse.Namespace(api_key=None, api_key_file=None))
        out = capsys.readouterr().out

        assert rc == 0
        assert "AGENTCHAT_API_KEY" not in env.read_text(encoding="utf-8")
        assert "ac_" not in env.read_text(encoding="utf-8")
        assert env.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-other\n"
        record = saved(home)
        assert record is not None and (record.api_key, record.handle) == (api_key("new"), "alice")
        assert_private_file(key_store.store_path(home))
        assert api_key("new") not in out
