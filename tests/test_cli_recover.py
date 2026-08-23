"""``hermes agentchat recover`` — the scriptable recovery subcommand.

Contract under test:

* ``--handle`` is always sent with ``--email``. When omitted it defaults
  to the locally configured ``AGENTCHATME_HANDLE``; else it is prompted
  for on a TTY; else the command fails (exit 2) naming the flag.
* ``HANDLE_REQUIRED`` from the verify step prints the handles the server
  listed and how to re-run.
* ``register`` rejected for a per-email cap hints at ``recover``.

Network calls (``wizard._recover_start`` / ``_recover_verify``) and the
persistence side effects are patched; stdin TTY-ness is forced per test
so the suite behaves the same under a terminal and under CI.
"""
from __future__ import annotations

import argparse
from typing import Any

import pytest

from agentchatme_hermes import cli, wizard
from agentchatme_hermes.cli import (
    _EXIT_API_ERR,
    _EXIT_ARG_ERR,
    _EXIT_OK,
    _EXIT_USER_CANCEL,
    _MissingFlag,
    _read_saved_handle,
    _resolve_recovery_email,
    _resolve_recovery_handle,
    _UserAbort,
    setup_argparse,
)
from agentchatme_hermes.wizard import _HANDLE_REQUIRED, _RecoverError, _RegisterError


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


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

    def test_invalid_flag_raises_value_error(self, clean_env: None) -> None:
        with pytest.raises(ValueError):
            _resolve_recovery_handle("not a handle")

    def test_saved_handle_used_and_announced(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AGENTCHATME_HANDLE", "@alice")
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        assert _resolve_recovery_handle(None) == "alice"
        out = capsys.readouterr().out
        assert "@alice" in out
        assert "--handle" in out

    def test_non_tty_without_handle_fails_naming_flag(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        with pytest.raises(_MissingFlag) as info:
            _resolve_recovery_handle(None)
        assert "--handle <handle>" in str(info.value)
        assert "--email <email>" in str(info.value)

    def test_tty_without_handle_prompts(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        answers = iter(["bad handle!", "@Alice"])
        monkeypatch.setattr(cli, "_input", lambda _p: next(answers))
        assert _resolve_recovery_handle(None) == "alice"
        out = capsys.readouterr().out
        assert "agent to recover" in out
        # Not the registration wording.
        assert "Pick a handle" not in out

    def test_malformed_saved_handle_is_ignored(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCHATME_HANDLE", "Not Valid!!")
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        with pytest.raises(_MissingFlag):
            _resolve_recovery_handle(None)


class TestResolveRecoveryEmail:
    def test_flag_validated(self, clean_env: None) -> None:
        assert _resolve_recovery_email("a@b.co") == "a@b.co"
        with pytest.raises(ValueError):
            _resolve_recovery_email("nope")

    def test_non_tty_fails_naming_flag(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        with pytest.raises(_MissingFlag) as info:
            _resolve_recovery_email(None)
        assert "--email <email>" in str(info.value)

    def test_tty_prompts(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        monkeypatch.setattr(cli, "_input", lambda _p: "a@b.co")
        assert _resolve_recovery_email(None) == "a@b.co"


class TestReadSavedHandle:
    def test_absent(self, clean_env: None) -> None:
        assert _read_saved_handle() is None

    def test_present_normalised(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCHATME_HANDLE", " @Alice-Bot ")
        assert _read_saved_handle() == "alice-bot"

    def test_malformed_ignored(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTCHATME_HANDLE", "a")
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


# ─── _dispatch_recover end-to-end ──────────────────────────────────────────


class _Harness:
    """Patches every side effect around ``_dispatch_recover``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.starts: list[dict[str, str]] = []
        self.verifies: list[tuple[str, str]] = []
        self.persisted: list[dict[str, str]] = []
        self.anchored: list[str] = []
        self.verify_result: tuple[str, str] = ("ac_live_newkey_000000000000", "alice")
        self.verify_error: _RecoverError | None = None
        self.start_error: _RecoverError | None = None
        self.code = "123456"

        def start(*, email: str, handle: str) -> str:
            self.starts.append({"email": email, "handle": handle})
            if self.start_error is not None:
                raise self.start_error
            return "pnd_1"

        def verify(*, pending_id: str, code: str) -> tuple[str, str]:
            self.verifies.append((pending_id, code))
            if self.verify_error is not None:
                raise self.verify_error
            return self.verify_result

        def persist(*, api_key: str, handle: str, api_base: str) -> None:
            self.persisted.append({"api_key": api_key, "handle": handle, "api_base": api_base})

        monkeypatch.setattr(wizard, "_recover_start", start)
        monkeypatch.setattr(wizard, "_recover_verify", verify)
        monkeypatch.setattr(cli, "_persist_credentials", persist)
        monkeypatch.setattr(cli, "_install_soul_anchor", self.anchored.append)
        monkeypatch.setattr(cli, "_prompt_code", lambda: self.code)
        monkeypatch.setattr(cli, "_sdk_importable", lambda: True)
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)


class TestDispatchRecover:
    def test_flags_happy_path(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        h = _Harness(monkeypatch)
        rc = cli._dispatch_recover(_ns(email="A@b.co", handle="@Alice"))
        captured = capsys.readouterr()

        assert rc == _EXIT_OK
        assert h.starts == [{"email": "A@b.co", "handle": "alice"}]
        assert h.verifies == [("pnd_1", "123456")]
        assert h.persisted == [
            {
                "api_key": "ac_live_newkey_000000000000",
                "handle": "alice",
                "api_base": "https://api.agentchat.me",
            }
        ]
        assert h.anchored == ["alice"]
        assert "Recovered @alice" in captured.out
        assert "previous key is now dead" in captured.out
        assert "ac_live_newkey_000000000000" not in captured.out  # masked

    def test_persists_handle_the_server_returned(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = _Harness(monkeypatch)
        h.verify_result = ("ac_live_newkey_000000000000", "alice")
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle="alice"))
        assert rc == _EXIT_OK
        assert h.persisted[0]["handle"] == "alice"

    def test_handle_defaults_to_saved_identity(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AGENTCHATME_HANDLE", "alice")
        h = _Harness(monkeypatch)
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle=None))
        assert rc == _EXIT_OK
        assert h.starts == [{"email": "a@b.co", "handle": "alice"}]
        assert "Recovering the locally configured agent @alice" in capsys.readouterr().out

    def test_non_interactive_without_handle_exits_2_naming_flag(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        h = _Harness(monkeypatch)
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle=None))
        err = capsys.readouterr().err
        assert rc == _EXIT_ARG_ERR
        assert "--handle <handle>" in err
        assert h.starts == []  # nothing sent to the server

    def test_non_interactive_without_email_exits_2_naming_flag(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        h = _Harness(monkeypatch)
        rc = cli._dispatch_recover(_ns(email=None, handle="alice"))
        assert rc == _EXIT_ARG_ERR
        assert "--email <email>" in capsys.readouterr().err
        assert h.starts == []

    def test_bad_flag_value_exits_2(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Harness(monkeypatch)
        rc = cli._dispatch_recover(_ns(email="not-an-email", handle="alice"))
        assert rc == _EXIT_ARG_ERR
        assert "not a valid email" in capsys.readouterr().err

    def test_handle_required_prints_handles_and_rerun_hint(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        h = _Harness(monkeypatch)
        h.verify_error = _RecoverError(
            "This email backs more than one agent.",
            code=_HANDLE_REQUIRED,
            handles=["alice", "alice-2"],
        )
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle="alice"))
        err = capsys.readouterr().err
        assert rc == _EXIT_API_ERR
        assert "@alice, @alice-2" in err
        assert "hermes agentchat recover --handle <handle> --email a@b.co" in err
        assert h.persisted == []
        assert h.anchored == []

    def test_invalid_code_exits_1_without_persisting(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        h = _Harness(monkeypatch)
        h.verify_error = _RecoverError("Invalid or expired code", code="INVALID_CODE")
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle="alice"))
        assert rc == _EXIT_API_ERR
        assert "Recovery failed: Invalid or expired code" in capsys.readouterr().err
        assert h.persisted == []

    def test_start_failure_exits_1_before_code_prompt(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        h = _Harness(monkeypatch)
        h.start_error = _RecoverError("rate-limited — wait a minute and try again", code="RATE_LIMITED")
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle="alice"))
        assert rc == _EXIT_API_ERR
        assert "Recovery request failed: rate-limited" in capsys.readouterr().err
        assert h.verifies == []

    def test_missing_sdk_is_reported_before_any_request(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        h = _Harness(monkeypatch)
        monkeypatch.setattr(cli, "_sdk_importable", lambda: False)
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle="alice"))
        assert rc == _EXIT_API_ERR
        assert "agentchatme" in capsys.readouterr().err
        assert h.starts == []

    def test_cancel_at_code_prompt(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        h = _Harness(monkeypatch)

        def abort() -> str:
            raise _UserAbort("user cancelled")

        monkeypatch.setattr(cli, "_prompt_code", abort)
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle="alice"))
        assert rc == _EXIT_USER_CANCEL
        assert h.verifies == []
        assert h.persisted == []

    def test_ctrl_c_at_code_prompt(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _Harness(monkeypatch)

        def interrupt() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_prompt_code", interrupt)
        assert cli._dispatch_recover(_ns(email="a@b.co", handle="alice")) == _EXIT_USER_CANCEL


# ─── register: per-email rejection hints at recover ────────────────────────


class TestRegisterEmailPolicyHint:
    def test_limit_reached_hints_recover(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def start(*, email: str, handle: str, display_name: str) -> str:
            raise _RegisterError(
                "This email already backs 10 active agents — the server's per-email limit.",
                field="email",
                code="EMAIL_LIMIT_REACHED",
            )

        monkeypatch.setattr(wizard, "_register_start", start)
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        err = capsys.readouterr().err
        assert rc == _EXIT_API_ERR
        assert "10 active agents" in err
        assert "hermes agentchat recover --handle <handle> --email a@b.co" in err

    def test_other_errors_do_not_hint_recover(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def start(*, email: str, handle: str, display_name: str) -> str:
            raise _RegisterError("taken", field="handle", code="HANDLE_TAKEN")

        monkeypatch.setattr(wizard, "_register_start", start)
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        err = capsys.readouterr().err
        assert rc == _EXIT_API_ERR
        assert "recover" not in err
