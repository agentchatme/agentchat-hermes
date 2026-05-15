"""Tests for the ``hermes agentchat`` CLI's input validation.

We test the pure validators (no Hermes / no network), not the full
flows. Full-flow tests would need to mock ``AgentChatClient.register``
+ ``verify`` + ``save_env_value``, which is high-effort and brittle
for low signal — the validators below are the load-bearing logic.
"""
from __future__ import annotations

import pytest

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

        args = parser.parse_args(["login", "--api-key", "ac_live_xxx"])
        assert args.action == "login"
        assert args.api_key == "ac_live_xxx"

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

        monkeypatch.setattr(cli, "_read_saved_key", lambda: None)  # type: ignore[attr-defined]
        # Doctor short-circuits after the env-var check when no key,
        # so SDK import doesn't need to be stubbed.
        rc = cli._dispatch_doctor(argparse.Namespace())
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert rc >= 1
        assert "AGENTCHATME_API_KEY not set" in captured.out

    def test_doctor_returns_zero_on_clean_config(
        self, monkeypatch: object, capsys: object
    ) -> None:
        import argparse
        from unittest.mock import MagicMock

        from agentchatme_hermes import cli

        monkeypatch.setattr(cli, "_read_saved_key", lambda: "ac_live_xyz")  # type: ignore[attr-defined]

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
        fake_sdk.AgentChatError = Exception  # type: ignore[attr-defined]
        fake_sdk.UnauthorizedError = Exception  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agentchatme", fake_sdk)  # type: ignore[attr-defined]

        # SOUL anchor + gateway check — let the soul_anchor module work
        # normally (no file means warn), and force gateway-detection
        # off so the test doesn't depend on host process state.
        monkeypatch.setattr(cli, "_other_gateway_running", lambda: False)  # type: ignore[attr-defined]

        rc = cli._dispatch_doctor(argparse.Namespace())
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "Authenticated as @alice" in captured.out
        # Exit code reflects failures only (warnings allowed). Two
        # plausible warns from a fresh checkout: no gateway process,
        # no SOUL.md anchor. Both warnings, neither a failure.
        assert rc == 0, (
            f"doctor returned non-zero exit code {rc} on a clean "
            f"config; output:\n{captured.out}"
        )
