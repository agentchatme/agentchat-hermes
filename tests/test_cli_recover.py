"""``hermes agentchat recover`` — which agent and which email, before any call.

Contract under test:

* ``--handle`` is always sent with ``--email``. When omitted it defaults
  to the locally configured ``AGENTCHATME_HANDLE``; else it is prompted
  for on a TTY; else the command fails (exit 2) naming the flag.
* ``--email`` follows the same TTY rule.
* What is typed is sent as typed (a handle without its ``@``, in lower
  case); its form is the server's to judge.

The run itself (codes, several agents on one email, saving) is
``test_cli_signup.py``.
"""
from __future__ import annotations

import argparse

import pytest

from agentchatme_hermes import cli
from agentchatme_hermes.cli import (
    _MissingFlag,
    _read_saved_handle,
    _resolve_recovery_email,
    _resolve_recovery_handle,
    _UserAbort,
    setup_argparse,
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTCHATME_HANDLE", raising=False)
    monkeypatch.delenv("AGENTCHATME_API_KEY", raising=False)
    monkeypatch.delenv("AGENTCHATME_API_BASE", raising=False)


# ─── argparse wiring ───────────────────────────────────────────────────────


class TestArgparseWiring:
    def test_recover_accepts_handle_and_email(self) -> None:
        parser = argparse.ArgumentParser(prog="hermes-agentchat")
        setup_argparse(parser)
        args = parser.parse_args(["recover", "--handle", "alice", "--email", "a@b.co"])
        assert args.action == "recover"
        assert args.handle == "alice"
        assert args.email == "a@b.co"
        assert args.func is cli._dispatch_recover

    def test_recover_flags_are_optional(self) -> None:
        parser = argparse.ArgumentParser(prog="hermes-agentchat")
        setup_argparse(parser)
        args = parser.parse_args(["recover"])
        assert args.handle is None
        assert args.email is None


# ─── handle / email resolution ─────────────────────────────────────────────


class TestResolveRecoveryHandle:
    def test_flag_wins_and_is_normalised(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCHATME_HANDLE", "someone-else")
        assert _resolve_recovery_handle(" @Alice ") == "alice"

    def test_flag_is_sent_as_typed_for_the_server_to_judge(self, clean_env: None) -> None:
        assert _resolve_recovery_handle("not a handle") == "not a handle"

    def test_saved_handle_used_and_announced(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A profile set up before 0.6: its key and its handle in the settings."""
        from fake_keys import api_key

        monkeypatch.setenv("AGENTCHAT_API_KEY", api_key("recover_handle"))
        monkeypatch.setenv("AGENTCHATME_HANDLE", "@alice")
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        assert _resolve_recovery_handle(None) == "alice"
        out = capsys.readouterr().out
        assert "@alice" in out
        assert "--handle" in out

    def test_the_key_files_handle_is_the_default(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from fake_keys import api_key
        from key_files import save_key

        save_key(api_key("recover_kf"), handle="ops")
        monkeypatch.setenv("AGENTCHATME_HANDLE", "someone-else")
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        assert _resolve_recovery_handle(None) == "ops"

    def test_a_leftover_handle_is_said_but_never_the_default(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Lab R2-F04 / R3 L1-F11: with no key, AGENTCHATME_HANDLE is another
        profile's copied .env line: said, and the person types the handle."""
        monkeypatch.setenv("AGENTCHATME_HANDLE", "fixtest-lab-he-1")
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        with pytest.raises(_MissingFlag):
            _resolve_recovery_handle(None)
        err = capsys.readouterr().err
        assert "a leftover setting" in err and "may be another profile's agent" in err
        assert "`hermes config unset AGENTCHATME_HANDLE`" in err
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        prompts: list[str] = []
        monkeypatch.setattr(cli, "_input", lambda p: prompts.append(p) or "@Typed")
        assert _resolve_recovery_handle(None) == "typed"
        assert prompts == ["@handle of the agent to recover (blank to cancel): "], "asked, not pre-filled"

    def test_non_tty_without_handle_fails_naming_flag(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        with pytest.raises(_MissingFlag) as info:
            _resolve_recovery_handle(None)
        assert "--handle <handle>" in str(info.value)
        assert "--email <email>" in str(info.value)

    def test_tty_without_handle_prompts(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        prompts: list[str] = []
        monkeypatch.setattr(cli, "_input", lambda p: prompts.append(p) or "@Alice")
        assert _resolve_recovery_handle(None) == "alice"
        assert prompts == ["@handle of the agent to recover (blank to cancel): "]

    def test_tty_blank_cancels(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        monkeypatch.setattr(cli, "_input", lambda _p: "")
        with pytest.raises(_UserAbort):
            _resolve_recovery_handle(None)


class TestResolveRecoveryEmail:
    def test_flag_is_stripped(self, clean_env: None) -> None:
        assert _resolve_recovery_email(" a@b.co ") == "a@b.co"

    def test_non_tty_fails_naming_flag(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        with pytest.raises(_MissingFlag) as info:
            _resolve_recovery_email(None)
        assert "--email <email>" in str(info.value)

    def test_tty_prompts(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        prompts: list[str] = []
        monkeypatch.setattr(cli, "_input", lambda p: prompts.append(p) or "a@b.co")
        assert _resolve_recovery_email(None) == "a@b.co"
        assert prompts == ["Email address the agent was registered with (blank to cancel): "]


class TestReadSavedHandle:
    def test_absent(self, clean_env: None) -> None:
        assert _read_saved_handle() is None

    def test_present_normalised(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        from fake_keys import api_key

        monkeypatch.setenv("AGENTCHAT_API_KEY", api_key("saved_handle"))
        monkeypatch.setenv("AGENTCHATME_HANDLE", " @Alice-Bot ")
        assert _read_saved_handle() == "alice-bot"

    def test_a_settings_handle_with_no_key_is_not_this_profiles(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentchatme_hermes.config import profile_handles

        monkeypatch.setenv("AGENTCHATME_HANDLE", "@Fixtest-Lab-He-1")
        assert _read_saved_handle() is None
        assert profile_handles() == (None, "fixtest-lab-he-1")

    def test_blank(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCHATME_HANDLE", "  @ ")
        assert _read_saved_handle() is None


class TestStdinIsTty:
    def test_false_when_stdin_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.sys, "stdin", None)
        assert cli._stdin_is_tty() is False

    def test_false_when_isatty_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Closed:
            def isatty(self) -> bool:
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(cli.sys, "stdin", _Closed())
        assert cli._stdin_is_tty() is False


class TestInput:
    def test_the_end_of_stdin_reads_as_a_blank_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import io

        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("123456\n"))
        assert cli._input("Code: ") == "123456"
        assert cli._input("Code: ") == ""
        assert capsys.readouterr().out == "Code: Code: "
