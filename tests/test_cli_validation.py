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
