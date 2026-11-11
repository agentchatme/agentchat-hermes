"""Tests for the ``hermes agentchat`` CLI's input validation.

We test the pure validators (no Hermes / no network), not the full
flows. Full-flow tests would need to mock ``AgentChatClient.register``
+ ``verify`` + ``save_env_value``, which is high-effort and brittle
for low signal — the validators below are the load-bearing logic.
"""
from __future__ import annotations

from typing import Any

import pytest
from fake_keys import api_key

from agentchatme_hermes.cli import _validate_email, _validate_handle


class TestValidateEmail:
    @pytest.mark.parametrize(
        "value",
        [
            "user@example.com",
            "first.last@example.com",
            "test+tag@example.co.uk",
            "user@subdomain.example.com",
            "u@s.co",
        ],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert _validate_email(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "no-at-sign",
            "@example.com",
            "user@",
            "user@nodot",
            "user @ space.com",
        ],
    )
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValueError):
            _validate_email(value)

    def test_rejects_too_long(self) -> None:
        long_local = "a" * 250
        with pytest.raises(ValueError, match="too long"):
            _validate_email(f"{long_local}@example.com")


class TestValidateHandle:
    @pytest.mark.parametrize(
        "value",
        [
            "alice",
            "alice42",
            "alice-bot",
            "agent-1-two",
            "abc",  # minimum length 3
            "a" + "b" * 29,  # max length 30
        ],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert _validate_handle(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "ab",  # too short
            "a" * 31,  # too long
            "1alice",  # starts with digit
            "-alice",  # starts with hyphen
            "alice-",  # ends with hyphen
            "alice--bot",  # doubled hyphen
            "alice_bot",  # underscore
            "Alice",  # uppercase
            "alice.bot",  # period
            "alice bot",  # space
        ],
    )
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValueError):
            _validate_handle(value)


class TestArgparseWiring:
    """Smoke-test that the argparse setup produces a parser that accepts
    the documented subcommand surface."""

    def test_setup_argparse_wires_register(self) -> None:
        import argparse

        from agentchatme_hermes.cli import setup_argparse

        parser = argparse.ArgumentParser(prog="hermes-agentchat")
        setup_argparse(parser)

        args = parser.parse_args(
            ["register", "--email", "u@e.com", "--handle", "alice"]
        )
        assert args.action == "register"
        assert args.email == "u@e.com"
        assert args.handle == "alice"

    def test_setup_argparse_wires_login(self) -> None:
        import argparse

        from agentchatme_hermes.cli import setup_argparse

        parser = argparse.ArgumentParser(prog="hermes-agentchat")
        setup_argparse(parser)

        args = parser.parse_args(["login", "--api-key", api_key("xxx")])
        assert args.action == "login"
        assert args.api_key == api_key("xxx")

    def test_setup_argparse_wires_status_and_logout(self) -> None:
        import argparse

        from agentchatme_hermes.cli import setup_argparse

        parser = argparse.ArgumentParser(prog="hermes-agentchat")
        setup_argparse(parser)

        assert parser.parse_args(["status"]).action == "status"
        assert parser.parse_args(["logout"]).action == "logout"

    def test_no_subcommand_defaults_to_wizard(self) -> None:
        import argparse

        from agentchatme_hermes.cli import _dispatch_wizard, setup_argparse

        parser = argparse.ArgumentParser(prog="hermes-agentchat")
        setup_argparse(parser)

        args = parser.parse_args([])
        assert args.action is None
        assert args.func is _dispatch_wizard

    def test_setup_argparse_wires_doctor(self) -> None:
        """Regression guard: ``doctor`` is referenced from
        ``_register.py``'s error hint, so it must exist."""
        import argparse

        from agentchatme_hermes.cli import _dispatch_doctor, setup_argparse

        parser = argparse.ArgumentParser(prog="hermes-agentchat")
        setup_argparse(parser)

        args = parser.parse_args(["doctor"])
        assert args.action == "doctor"
        assert args.func is _dispatch_doctor
        assert args.fix is False
        assert parser.parse_args(["doctor", "--fix"]).fix is True


class TestDoctor:
    """End-to-end doctor on a clean / broken config.

    We intercept ``_read_saved_key`` so tests don't depend on the
    runner's actual env, and stub the SDK so no network IO happens.
    """

    def test_doctor_reports_missing_key(
        self, monkeypatch: object, capsys: object
    ) -> None:
        import argparse

        from agentchatme_hermes import cli

        monkeypatch.setattr(cli, "_read_saved_key_source", lambda: None)  # type: ignore[attr-defined]
        # Doctor short-circuits after the env-var check when no key,
        # so SDK import doesn't need to be stubbed.
        rc = cli._dispatch_doctor(argparse.Namespace(fix=False))
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert rc >= 1
        assert "AGENTCHAT_API_KEY not set" in captured.out

    def test_doctor_returns_zero_on_clean_config(
        self, monkeypatch: object, capsys: object
    ) -> None:
        import argparse
        from unittest.mock import MagicMock

        from agentchatme_hermes import cli

        monkeypatch.setattr(  # type: ignore[attr-defined]
            cli, "_read_saved_key_source", lambda: (api_key("xyz"), cli._ENV_API_KEY)
        )

        fake_client = MagicMock()
        fake_client.get_me.return_value = {
            "handle": "alice",
            "status": "active",
            "settings": {"inbox_mode": "open"},
            "paused_by_owner": "none",
        }
        fake_client_cls = MagicMock(return_value=fake_client)

        import sys
        import types as _types

        fake_sdk = _types.ModuleType("agentchatme")
        fake_sdk.AgentChatClient = fake_client_cls  # type: ignore[attr-defined]
        fake_sdk.AgentChatClientIdentity = MagicMock  # type: ignore[attr-defined]
        fake_sdk.AgentChatError = Exception  # type: ignore[attr-defined]
        fake_sdk.UnauthorizedError = Exception  # type: ignore[attr-defined]

        class _ConnectionError(Exception):
            pass

        fake_sdk.ConnectionError = _ConnectionError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agentchatme", fake_sdk)  # type: ignore[attr-defined]

        # SOUL anchor + gateway check — let the soul_anchor module work
        # normally (no file means warn), and force gateway-detection
        # off so the test doesn't depend on host process state.
        monkeypatch.setattr(cli, "_other_gateway_running", lambda: False)  # type: ignore[attr-defined]
        # Hermes' MCP client is present on a clean install (the [mcp] extra).
        monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: True)  # type: ignore[attr-defined]
        # A clean install has the hosted-tools entry. Stated, not read from
        # Hermes: with Hermes installed (the hermes-main workflow) the real
        # check would read this test machine's config.yaml.
        from agentchatme_hermes import kit, mcp_registration

        monkeypatch.setattr(mcp_registration, "inspect_entry_identity", lambda **_kw: ("ours", "current"))  # type: ignore[attr-defined]
        # The SDK stand-in above has no kit; a clean install has it.
        monkeypatch.setattr(kit, "kit_problem", lambda: None)  # type: ignore[attr-defined]

        rc = cli._dispatch_doctor(argparse.Namespace(fix=False))
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Authenticated as @alice" in captured.out
        assert "[ok]   Hermes MCP client importable" in captured.out
        assert "manual bundled" in captured.out
        # Exit code reflects failures only (warnings allowed). Plausible
        # warns from a fresh checkout: no gateway process, no SOUL.md
        # anchor, hosted-tools entry not checkable without Hermes. All
        # warnings, none a failure.
        assert rc == 0, (
            f"doctor returned non-zero exit code {rc} on a clean "
            f"config; output:\n{captured.out}"
        )

    def test_doctor_warns_on_legacy_key_name(self, monkeypatch: object, capsys: object) -> None:
        import argparse

        from agentchatme_hermes import cli

        monkeypatch.setattr(  # type: ignore[attr-defined]
            cli, "_read_saved_key_source", lambda: (api_key("xyz"), cli._ENV_API_KEY_LEGACY)
        )
        monkeypatch.setattr(cli, "_sdk_importable", lambda: False)  # type: ignore[attr-defined]
        import sys

        monkeypatch.setitem(sys.modules, "agentchatme", None)  # type: ignore[attr-defined]
        cli._dispatch_doctor(argparse.Namespace(fix=False))
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "pre-v3 name AGENTCHATME_API_KEY" in captured.out


class TestDoctorHermesMcpClient:
    """Hermes connects the hosted tools with the ``mcp`` SDK (its ``[mcp]``
    extra). Without it the ``mcp_servers.agentchat`` entry can never connect
    and every turn runs tool-less — the 0.4.0 live-fire's first failure."""

    def _run(self, monkeypatch: object, capsys: object, importable: bool) -> tuple[int, str]:
        import argparse

        from agentchatme_hermes import cli

        monkeypatch.setattr(cli, "_read_saved_key_source", lambda: None)  # type: ignore[attr-defined]
        monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: importable)  # type: ignore[attr-defined]
        rc = cli._dispatch_doctor(argparse.Namespace(fix=False))
        return rc, capsys.readouterr().out  # type: ignore[attr-defined]

    def test_missing_mcp_sdk_fails_with_the_fix(self, monkeypatch: object, capsys: object) -> None:
        rc, out = self._run(monkeypatch, capsys, importable=False)
        assert "[fail] Hermes cannot connect the hosted tools" in out
        assert "hermes-agent[mcp]" in out
        assert rc >= 2, "the missing key and the missing SDK are two failures"

    def test_present_mcp_sdk_is_ok(self, monkeypatch: object, capsys: object) -> None:
        rc, out = self._run(monkeypatch, capsys, importable=True)
        assert "[ok]   Hermes MCP client importable" in out
        assert rc == 1, "only the missing key fails"

    def test_probe_imports_the_streamable_http_client(self, monkeypatch: object) -> None:
        import importlib
        import sys

        from agentchatme_hermes import cli

        monkeypatch.setitem(sys.modules, "mcp", None)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", None)  # type: ignore[attr-defined]
        assert cli._hermes_mcp_client_importable() is False
        real = importlib.import_module

        def fake_import(name: str) -> object:
            if name == "mcp.client.streamable_http":
                return object()
            return real(name)

        monkeypatch.setattr(importlib, "import_module", fake_import)  # type: ignore[attr-defined]
        assert cli._hermes_mcp_client_importable() is True


class TestDoctorClientHeader:
    """The hosted-tools entry must say it is Hermes (``X-AgentChat-Client``),
    or the hosted tools treat the agent as an unknown client and offer it the
    wake-webhook tools a socket integration does not need."""

    @staticmethod
    def _run(monkeypatch: Any, capsys: Any, state: str, identity: str | None) -> str:
        import argparse
        import sys
        import types as _types
        from unittest.mock import MagicMock

        from agentchatme_hermes import cli, kit, mcp_registration

        monkeypatch.setattr(cli, "_read_saved_key_source", lambda: (api_key("xyz"), cli._ENV_API_KEY))
        fake_client = MagicMock()
        fake_client.get_me.return_value = {"handle": "alice", "status": "active"}
        fake_sdk = _types.ModuleType("agentchatme")
        fake_sdk.AgentChatClient = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]
        fake_sdk.AgentChatClientIdentity = MagicMock  # type: ignore[attr-defined]
        fake_sdk.AgentChatError = Exception  # type: ignore[attr-defined]
        fake_sdk.UnauthorizedError = Exception  # type: ignore[attr-defined]

        class _ConnectionError(Exception):
            pass

        fake_sdk.ConnectionError = _ConnectionError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agentchatme", fake_sdk)
        monkeypatch.setattr(cli, "_other_gateway_running", lambda: False)
        monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: True)
        monkeypatch.setattr(kit, "kit_problem", lambda: None)
        monkeypatch.setattr(mcp_registration, "inspect_entry_identity", lambda **_kw: (state, identity))
        cli._dispatch_doctor(argparse.Namespace(fix=False))
        return str(capsys.readouterr().out)

    def test_current_entry_has_no_header_warning(self, monkeypatch: Any, capsys: Any) -> None:
        out = self._run(monkeypatch, capsys, "ours", "current")
        assert "hosted tools registered" in out
        assert "X-AgentChat-Client" not in out

    def test_entry_from_an_earlier_release_warns_with_the_fix(self, monkeypatch: Any, capsys: Any) -> None:
        out = self._run(monkeypatch, capsys, "ours", "missing")
        assert "[warn] mcp_servers.agentchat does not say it is Hermes" in out
        assert "doctor --fix" in out

    def test_another_client_name_is_left_alone_and_said(self, monkeypatch: Any, capsys: Any) -> None:
        out = self._run(monkeypatch, capsys, "ours", "other")
        assert "names another client, not hermes — left alone" in out

    def test_hand_written_entry_without_the_header_says_what_to_add(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        out = self._run(monkeypatch, capsys, "user_managed", "missing")
        assert "managed by hand in config.yaml — left alone" in out
        assert "the hand-written mcp_servers.agentchat does not say it is Hermes" in out
        assert "replace any existing X-AgentChat-Client line; a key may appear only once, in any casing" in out
        # The README entry's indent, under `headers:`; static, no version.
        assert '\n      X-AgentChat-Client: "hermes"\n' in out
        assert "X-AgentChat-Client-Version" not in out

    def test_hand_written_entry_naming_another_client(self, monkeypatch: Any, capsys: Any) -> None:
        out = self._run(monkeypatch, capsys, "user_managed", "other")
        assert "names another client" in out
        assert "does not say it is Hermes" not in out
        assert '      X-AgentChat-Client: "hermes"' in out

    def test_hand_written_entry_with_the_header_is_fine(self, monkeypatch: Any, capsys: Any) -> None:
        out = self._run(monkeypatch, capsys, "user_managed", "current")
        assert "does not say it is Hermes" not in out


class TestDoctorTwoPlugins:
    """A git-clone directory plugin next to the pip package shadows it:
    Hermes keeps the directory plugin and drops the same-named entry point,
    so upgrading the pip package silently changes nothing."""

    @staticmethod
    def _entry_points(monkeypatch: Any, names: list[str], *, as_dict: bool = False) -> None:
        """``entry_points()`` as 3.10+ returns it (``.select``), or as 3.9's dict."""
        from importlib import metadata
        from types import SimpleNamespace

        eps = [SimpleNamespace(name=n) for n in names]

        class _Found:
            def select(self, *, group: str) -> list[Any]:
                return eps if group == "hermes_agent.plugins" else []

        found: Any = {"hermes_agent.plugins": eps} if as_dict else _Found()
        monkeypatch.setattr(metadata, "entry_points", lambda: found)
        monkeypatch.setattr(metadata, "version", lambda name: "0.5.1")

    @staticmethod
    def _directory_plugin(
        home: Any, name: str, manifest: str, *, filename: str = "plugin.yaml", encoding: str = "utf-8"
    ) -> Any:
        directory = home / "plugins" / name
        directory.mkdir(parents=True)
        (directory / filename).write_text(manifest, encoding=encoding)
        return directory

    def test_finds_the_directory_plugin_and_the_pip_entry_point(self, monkeypatch: Any, tmp_path: Any) -> None:
        from agentchatme_hermes import cli

        self._entry_points(monkeypatch, ["agentchat", "other"])
        ours = self._directory_plugin(tmp_path, "agentchat", "name: agentchat\nversion: 0.4.0\n")
        self._directory_plugin(tmp_path, "weather", "name: weather\n")
        (tmp_path / "plugins" / "broken").mkdir()
        assert cli._plugin_copies(tmp_path) == ([ours], "0.5.1")

    def test_manifest_shapes_hermes_accepts(self, monkeypatch: Any, tmp_path: Any) -> None:
        from agentchatme_hermes import cli

        self._entry_points(monkeypatch, [], as_dict=True)  # Python 3.9's entry_points()
        a = self._directory_plugin(tmp_path, "a-yml", "name: agentchat\n", filename="plugin.yml")
        b = self._directory_plugin(tmp_path, "b-comment", "name: 'agentchat'  # the integration\n")
        c = self._directory_plugin(tmp_path, "c-bom", "name: agentchat\n", encoding="utf-8-sig")
        d = self._directory_plugin(tmp_path, "agentchat", "version: 0.3.0\n")  # no name: the directory's
        self._directory_plugin(tmp_path, "e-other", "name: agentchat-extra\n")
        assert cli._plugin_copies(tmp_path) == (sorted([a, b, c, d]), None)

    def test_no_plugins_dir_and_no_entry_point(self, monkeypatch: Any, tmp_path: Any) -> None:
        from agentchatme_hermes import cli

        self._entry_points(monkeypatch, ["other"])
        assert cli._plugin_copies(tmp_path) == ([], None)

    def _doctor(self, monkeypatch: Any, capsys: Any, home: Any) -> str:
        import argparse

        from agentchatme_hermes import cli, profile_scope

        monkeypatch.setattr(profile_scope, "resolve_hermes_home", lambda: home)
        monkeypatch.setattr(cli, "_read_saved_key_source", lambda: None)
        cli._dispatch_doctor(argparse.Namespace(fix=False))
        return str(capsys.readouterr().out)

    def test_doctor_warns_on_two_copies_with_the_fix(self, monkeypatch: Any, capsys: Any, tmp_path: Any) -> None:
        self._entry_points(monkeypatch, ["agentchat"])
        self._directory_plugin(tmp_path, "agentchat", "name: agentchat\n")
        out = self._doctor(monkeypatch, capsys, tmp_path)
        assert "[warn] 2 agentchat plugins in this profile" in out
        assert "agentchatme-hermes 0.5.1 from pip" in out
        # Removal also takes agentchat off plugins.enabled; the pip copy needs it back.
        assert (
            "`hermes plugins remove agentchat`, then `hermes plugins enable agentchat`" in out
        )
        assert "`hermes gateway restart`" in out

    def test_doctor_names_the_copy_hermes_loads(self, monkeypatch: Any, capsys: Any, tmp_path: Any) -> None:
        self._entry_points(monkeypatch, ["agentchat"])
        self._directory_plugin(tmp_path, "agentchat", "name: agentchat\n")
        backup = self._directory_plugin(tmp_path, "agentchat.bak", "name: agentchat\n")
        out = self._doctor(monkeypatch, capsys, tmp_path)
        assert "[warn] 3 agentchat plugins in this profile" in out
        assert f"Hermes loads {backup}" in out
        assert "`hermes plugins remove agentchat` and `hermes plugins remove agentchat.bak`" in out

    def test_two_directory_copies_without_pip_warn(self, monkeypatch: Any, capsys: Any, tmp_path: Any) -> None:
        self._entry_points(monkeypatch, [])
        self._directory_plugin(tmp_path, "agentchat", "name: agentchat\n")
        backup = self._directory_plugin(tmp_path, "agentchat.bak", "name: agentchat\n")
        out = self._doctor(monkeypatch, capsys, tmp_path)
        assert "[warn] 2 agentchat directory plugins in this profile" in out
        assert f"Hermes loads {backup}" in out

    def test_doctor_notes_a_directory_only_install(self, monkeypatch: Any, capsys: Any, tmp_path: Any) -> None:
        self._entry_points(monkeypatch, [])
        self._directory_plugin(tmp_path, "agentchat", "name: agentchat\n")
        out = self._doctor(monkeypatch, capsys, tmp_path)
        assert "[ok]   installed as a directory plugin" in out
        assert "unless installed with --ref" in out
        assert "agentchat plugins in this profile" not in out

    def test_pip_only_says_nothing_extra(self, monkeypatch: Any, capsys: Any, tmp_path: Any) -> None:
        self._entry_points(monkeypatch, ["agentchat"])
        out = self._doctor(monkeypatch, capsys, tmp_path)
        assert "directory plugin" not in out
