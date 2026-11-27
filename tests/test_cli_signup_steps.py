"""Sign-up one step per command, end to end on the fake API.

In an agent's terminal (``HERMES_SESSION_ID``) and wherever stdin is not a
terminal, ``register --email E --handle H`` sends the code, keeps the
server's flow in ``<profile>/agentchat/signup.json`` and ends;
``register --code C`` finishes. Nothing is ever asked at a prompt there.
The server's sentences are printed as they are; Hermes adds only the
command to run next. Saving and placement are patched (their own suites
test them); the waiting file is the real one, in the test's profile.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
from typing import TYPE_CHECKING, Any

import pytest
import signup_wire as sw
from fake_keys import api_key
from key_files import profile_home, save_key
from sdk_wire import api_error, connect_error, serve

from agentchatme_hermes import allow, cli, signup_steps
from agentchatme_hermes.cli import _EXIT_API_ERR, _EXIT_ARG_ERR, _EXIT_OK

if TYPE_CHECKING:
    from pathlib import Path

_KEY = api_key("steps_00000000000000")
_FINISH = (
    "Then finish with: hermes agentchat register --code <code> (the code from that email: read it "
    "if you can, or ask your person for it)."
)


def _ns(**kwargs: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"email": None, "handle": None, "display_name": None, "code": None}
    return argparse.Namespace(**{**base, **kwargs})


class _Steps:
    """One step per command: any prompt fails the test; what would have been
    saved and placed is recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.persisted: list[dict[str, str]] = []
        self.placed: list[str] = []
        for name in ("AGENTCHATME_HANDLE", "AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY", "AGENTCHATME_API_BASE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HERMES_SESSION_ID", "20261127_120000_steps")
        monkeypatch.setattr(cli, "_input", self._no_prompt)
        monkeypatch.setattr(
            cli,
            "_persist_credentials",
            lambda *, api_key, handle, api_base: self.persisted.append(
                {"api_key": api_key, "handle": handle, "api_base": api_base}
            )
            or True,
        )
        monkeypatch.setattr(
            allow, "decide_for", lambda handle: allow.SetupDecision(None, allow.AGENT_TERMINAL_LINE.format(who=f"@{handle}"))
        )
        monkeypatch.setattr(cli, "_install_placement", self._place)

    @staticmethod
    def _no_prompt(prompt: str) -> str:
        raise AssertionError(f"asked at a prompt where nobody answers: {prompt!r}")

    def _place(self, handle: str, *, api_base: str, api_key: str, tools_decision: Any = None) -> list[Any]:
        self.placed.append(handle)
        return []


def key_files_path() -> Path:
    from agentchatme_hermes import key_store

    return key_store.store_path(profile_home())


def _waiting() -> dict[str, Any] | None:
    path = signup_steps.pending_path(profile_home())
    return json.loads(path.read_text()) if path.exists() else None


def _start(monkeypatch: pytest.MonkeyPatch, **routes: Any) -> _Steps:
    run = _Steps(monkeypatch)
    serve(monkeypatch, {sw.REGISTER: sw.code_sent(), **routes})
    assert cli._dispatch_register(_ns(email="a@b.co", handle="@Alice")) == _EXIT_OK
    return run


class TestWhenOneStepPerCommand:
    def test_an_agents_terminal_even_with_a_pty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_SESSION_ID", "s")
        monkeypatch.setattr(allow, "stdin_is_tty", lambda: True)
        assert signup_steps.one_step_per_run() is True

    def test_no_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        monkeypatch.setattr(allow, "stdin_is_tty", lambda: False)
        assert signup_steps.one_step_per_run() is True

    def test_a_persons_terminal_is_the_interactive_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        monkeypatch.setattr(allow, "stdin_is_tty", lambda: True)
        assert signup_steps.one_step_per_run() is False


class TestRegisterStart:
    def test_the_code_is_sent_the_flow_kept_and_the_command_ends(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = _Steps(monkeypatch)
        wire = serve(monkeypatch, {sw.REGISTER: sw.code_sent()})
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="@Alice", display_name="Al"))
        out = capsys.readouterr().out
        assert rc == _EXIT_OK
        assert wire.bodies() == [{"email": "a@b.co", "handle": "alice", "display_name": "Al"}]
        assert out.splitlines() == [sw.sent_text("register"), _FINISH]
        waiting = _waiting()
        assert waiting is not None
        assert waiting["kind"] == "register"
        assert waiting["flow"]["pending_id"] == "pnd_1"
        assert waiting["flow"]["next_step"] == "verify"
        assert (waiting["email"], waiting["handle"], waiting["display_name"]) == ("a@b.co", "alice", "Al")
        assert run.persisted == [] and run.placed == []

    def test_the_waiting_file_is_the_owners_alone_and_holds_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _start(monkeypatch)
        path = signup_steps.pending_path(profile_home())
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert "api_key" not in path.read_text()

    def test_missing_flags_are_named_and_nothing_is_sent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        wire = serve(monkeypatch, {})
        assert cli._dispatch_register(_ns(email="a@b.co")) == _EXIT_ARG_ERR
        err = capsys.readouterr().err
        assert "Missing --handle." in err
        assert "hermes agentchat register --email <email> --handle <handle> sends the code" in err
        assert wire.requests == []

    def test_a_profile_set_up_is_not_signed_up_again(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        save_key(api_key("earlier_00000000000000"), handle="bob")
        wire = serve(monkeypatch, {})
        assert cli._dispatch_register(_ns(email="a@b.co", handle="alice")) == _EXIT_API_ERR
        err = capsys.readouterr().err
        assert "already set up as @bob" in err and "Nothing was sent" in err and "logout" in err
        assert wire.requests == []
        assert _waiting() is None

    def test_a_taken_handle_names_the_flag_and_keeps_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        serve(monkeypatch, {sw.REGISTER: sw.handle_taken("alice")})
        assert cli._dispatch_register(_ns(email="a@b.co", handle="alice")) == _EXIT_ARG_ERR
        captured = capsys.readouterr()
        assert captured.out.splitlines() == ["The handle @alice is taken. Try another."]
        assert "Run it again with a different --handle." in captured.err
        assert _waiting() is None

    def test_a_wait_says_run_it_again(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _Steps(monkeypatch)
        serve(monkeypatch, {sw.REGISTER: sw.cooldown(seconds=5)})
        assert cli._dispatch_register(_ns(email="a@b.co", handle="alice")) == _EXIT_API_ERR
        captured = capsys.readouterr()
        assert "You can request a new code in 5 seconds." in captured.out
        assert "Then run the same command again." in captured.err
        assert _waiting() is None

    def test_a_server_stop_shows_its_message_alone(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        serve(monkeypatch, {sw.REGISTER: sw.not_sent()})
        assert cli._dispatch_register(_ns(email="a@b.co", handle="alice")) == _EXIT_API_ERR
        captured = capsys.readouterr()
        assert captured.out.splitlines() == [sw.NOT_SENT]
        assert captured.err == ""
        assert _waiting() is None

    def test_unreachable_sends_nothing_and_keeps_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        serve(monkeypatch, {sw.REGISTER: connect_error()})
        assert cli._dispatch_register(_ns(email="a@b.co", handle="alice")) == _EXIT_API_ERR
        assert "No code was sent." in capsys.readouterr().err
        assert _waiting() is None


class TestRegisterCode:
    def test_the_code_finishes_saves_places_and_removes_the_waiting_file(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = _start(monkeypatch, **{sw.REGISTER_VERIFY: sw.registered("alice", _KEY)})
        capsys.readouterr()
        wire = serve(monkeypatch, {sw.REGISTER_VERIFY: sw.registered("alice", _KEY)})
        rc = cli._dispatch_register(_ns(code=" 123456 "))
        out = capsys.readouterr().out
        assert rc == _EXIT_OK
        assert wire.bodies() == [{"pending_id": "pnd_1", "code": "123456"}]
        assert out.splitlines()[0] == "Email verified. Logged in as @alice."
        assert "Registered @alice" in out and _KEY not in out
        assert allow.AGENT_TERMINAL_LINE.format(who="@alice") in out
        assert run.persisted == [{"api_key": _KEY, "handle": "alice", "api_base": "https://api.agentchat.me"}]
        assert run.placed == ["alice"]
        assert _waiting() is None

    def test_the_handle_the_server_returned_is_saved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = _start(monkeypatch)
        serve(monkeypatch, {sw.REGISTER_VERIFY: sw.registered("alice-x", _KEY)})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_OK
        assert run.persisted[0]["handle"] == "alice-x" and run.placed == ["alice-x"]

    def test_a_wrong_code_keeps_the_sign_up_waiting(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = _start(monkeypatch)
        capsys.readouterr()
        serve(monkeypatch, {sw.REGISTER_VERIFY: [sw.wrong_code(attempts=1), sw.registered("alice", _KEY)]})
        assert cli._dispatch_register(_ns(code="111111")) == _EXIT_ARG_ERR
        captured = capsys.readouterr()
        assert captured.out.splitlines() == [sw.WRONG_CODE]
        assert "Run it again with the right code: hermes agentchat register --code <code>" in captured.err
        waiting = _waiting()
        assert waiting is not None and waiting["flow"]["attempts"] == 1
        assert waiting["flow"]["pending_id"] == "pnd_1"
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_OK
        assert run.placed == ["alice"] and _waiting() is None

    @pytest.mark.parametrize("route", [sw.expired, sw.attempts_exhausted])
    def test_a_code_that_is_gone_ends_the_wait_and_names_the_start(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], route: Any
    ) -> None:
        run = _start(monkeypatch)
        capsys.readouterr()
        serve(monkeypatch, {sw.REGISTER_VERIFY: route()})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_API_ERR
        err = capsys.readouterr().err
        assert "For a new code: hermes agentchat register --email a@b.co --handle alice" in err
        assert _waiting() is None and run.persisted == []

    def test_unreachable_keeps_the_sign_up_waiting(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _start(monkeypatch)
        capsys.readouterr()
        serve(monkeypatch, {sw.REGISTER_VERIFY: connect_error()})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_API_ERR
        assert "run the same command again" in capsys.readouterr().err
        assert _waiting() is not None

    def test_no_waiting_sign_up_names_the_start(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        wire = serve(monkeypatch, {})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_ARG_ERR
        assert capsys.readouterr().err.strip() == (
            "No sign-up is waiting for a code in this profile. Start with: "
            "hermes agentchat register --email <email> --handle <handle>"
        )
        assert wire.requests == []

    def test_a_waiting_recovery_is_finished_by_recover(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        serve(monkeypatch, {sw.RECOVER: sw.code_sent("recover")})
        assert cli._dispatch_recover(_ns(email="a@b.co", handle="alice")) == _EXIT_OK
        capsys.readouterr()
        wire = serve(monkeypatch, {})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_ARG_ERR
        assert "finish with hermes agentchat recover --code <code>" in capsys.readouterr().err
        assert wire.requests == []

    @pytest.mark.parametrize(
        "record",
        [
            {"version": 1, "kind": "register"},
            {"version": 2, "kind": "register", "flow": {"next_step": "verify", "pending_id": "p"}},
            {"version": 1, "kind": "register", "api_base": "https://api.agentchat.me", "flow": {"next_step": "verify"}},
            {"version": 1, "kind": "register", "api_base": "https://api.agentchat.me",
             "flow": {"next_step": "register", "pending_id": "p"}},
        ],
    )
    def test_a_file_that_is_not_a_waiting_sign_up_is_read_as_none_and_left(
        self, monkeypatch: pytest.MonkeyPatch, record: dict[str, Any]
    ) -> None:
        _Steps(monkeypatch)
        path = signup_steps.pending_path(profile_home())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**record, "profile_home": str(profile_home())}))
        wire = serve(monkeypatch, {})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_ARG_ERR
        assert path.exists(), "a file this release cannot finish from is not deleted (a later start replaces it)"
        assert wire.requests == []

    def test_a_server_error_ends_the_wait_and_points_to_recovery_never_a_second_account(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = _start(monkeypatch)
        capsys.readouterr()
        details = {"version": 1, "flow": "register", "next_step": "register"}
        serve(monkeypatch, {sw.REGISTER_VERIFY: api_error(500, "INTERNAL_ERROR", "Something went wrong.", details)})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_API_ERR
        captured = capsys.readouterr()
        assert captured.out.splitlines() == ["Something went wrong."]
        assert "Nothing was saved." in captured.err
        assert "hermes agentchat recover --email a@b.co --handle alice" in captured.err
        assert "register --email" not in captured.err and "different" not in captured.err
        assert _waiting() is None and run.persisted == []

    def test_a_refused_input_is_not_suggested_again(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _start(monkeypatch)
        capsys.readouterr()
        details = {"version": 1, "flow": "register", "next_step": "register", "param": "handle"}
        serve(monkeypatch, {sw.REGISTER_VERIFY: api_error(409, "HANDLE_TAKEN", "The handle @alice is taken.", details)})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_ARG_ERR
        err = capsys.readouterr().err
        assert "Start again with a different --handle: hermes agentchat register --email a@b.co --handle <handle>" in err
        assert _waiting() is None

    def test_set_up_meanwhile_the_code_does_not_replace_that_agent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = _start(monkeypatch)
        capsys.readouterr()
        # A person set the profile up some other way since the code was sent
        # (written directly: save_credentials would end the wait itself).
        save_key(api_key("meanwhile_000000000000"), handle="bob")
        wire = serve(monkeypatch, {sw.REGISTER_VERIFY: sw.registered("alice", _KEY)})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_API_ERR
        err = capsys.readouterr().err
        assert "already set up as @bob" in err and "dropped" in err
        assert wire.requests == [] and run.persisted == [] and _waiting() is None

    def test_any_key_saved_in_the_profile_ends_the_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentchatme_hermes import credentials

        _start(monkeypatch)
        assert _waiting() is not None
        credentials.save_credentials(api_key=_KEY, handle="alice", api_base="https://api.agentchat.me")
        assert _waiting() is None

    def test_a_copied_waiting_file_is_not_this_profiles_sign_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import shutil

        _start(monkeypatch)
        clone = tmp_path / "clone-all"
        shutil.copytree(profile_home() / "agentchat", clone / "agentchat")
        assert signup_steps.read_pending(clone) is None
        assert signup_steps.read_pending(profile_home()) is not None

    def test_a_second_code_command_waits_for_the_first(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import contextlib

        from agentchatme_hermes import leader_lock

        _start(monkeypatch)
        capsys.readouterr()

        @contextlib.contextmanager
        def busy(path: Any, **kwargs: Any) -> Any:
            yield "busy"

        monkeypatch.setattr(leader_lock, "held_lock", busy)
        wire = serve(monkeypatch, {})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_API_ERR
        assert "is running in this profile" in capsys.readouterr().err
        assert wire.requests == [] and _waiting() is not None

    def test_the_key_files_path_is_never_shown_in_an_agents_terminal(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _start(monkeypatch)
        capsys.readouterr()
        monkeypatch.setattr(cli, "_last_saved_path", key_files_path())
        serve(monkeypatch, {sw.REGISTER_VERIFY: sw.registered("alice", _KEY)})
        assert cli._dispatch_register(_ns(code="123456")) == _EXIT_OK
        out = capsys.readouterr().out
        assert "credentials.json" not in out
        assert "saved in this profile's key file, readable by you only" in out


class TestRecoverSteps:
    def test_start_then_code(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        run = _Steps(monkeypatch)
        wire = serve(monkeypatch, {sw.RECOVER: sw.code_sent("recover"), sw.RECOVER_VERIFY: sw.recovered("alice", _KEY)})
        assert cli._dispatch_recover(_ns(email="a@b.co", handle="@Alice")) == _EXIT_OK
        out = capsys.readouterr().out
        assert out.splitlines() == [
            "Completing recovery rotates the API key — anything still using the old key stops working.",
            sw.sent_text("recover"),
            _FINISH.replace("register --code", "recover --code"),
        ]
        assert run.persisted == []
        assert cli._dispatch_recover(_ns(code="123456")) == _EXIT_OK
        assert wire.bodies() == [{"email": "a@b.co", "handle": "alice"}, {"pending_id": "pnd_1", "code": "123456"}]
        assert "Recovered @alice" in capsys.readouterr().out
        assert run.persisted[0]["handle"] == "alice" and _waiting() is None

    @pytest.mark.skipif(os.name == "nt", reason="file modes mean nothing on Windows")
    def test_a_refused_key_file_is_not_signed_up_over(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        path = save_key(api_key("loose_00000000000000000"), handle="bob")
        os.chmod(path, 0o664)
        wire = serve(monkeypatch, {})
        assert cli._dispatch_register(_ns(email="a@b.co", handle="alice")) == _EXIT_API_ERR
        assert "Nothing was sent." in capsys.readouterr().err
        assert wire.requests == []

    def test_a_missing_handle_is_named_never_asked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        monkeypatch.setattr(allow, "stdin_is_tty", lambda: True)  # a PTY: still no prompt
        wire = serve(monkeypatch, {})
        assert cli._dispatch_recover(_ns(email="a@b.co")) == _EXIT_ARG_ERR
        assert "re-run with: hermes agentchat recover --handle <handle> --email <email>" in capsys.readouterr().err
        assert wire.requests == []

    def test_several_agents_on_the_email_are_listed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        serve(monkeypatch, {sw.RECOVER: sw.handle_required(["alice", "bob"])})
        assert cli._dispatch_recover(_ns(email="a@b.co", handle="carol")) == _EXIT_ARG_ERR
        captured = capsys.readouterr()
        assert "Agents registered with this email: @alice, @bob" in captured.out
        assert "Run it again with a different --handle." in captured.err


class TestTheOtherCommands:
    def test_the_wizard_in_an_agents_terminal_says_the_two_steps(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _Steps(monkeypatch)
        from agentchatme_hermes import wizard

        monkeypatch.setattr(wizard, "interactive_setup", lambda: pytest.fail("the wizard's menus ran"))
        assert cli._dispatch_wizard(_ns()) == _EXIT_OK
        out = capsys.readouterr().out
        assert cli.NO_WIZARD_HERE in out
        assert "1. hermes agentchat register --email <email> --handle <handle>" in out
        assert "2. hermes agentchat register --code <code>" in out
        assert "/agentchat allow" in out

    def test_the_wizard_names_the_code_a_sign_up_waits_for(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _start(monkeypatch)
        capsys.readouterr()
        assert cli._dispatch_wizard(_ns()) == _EXIT_OK
        assert capsys.readouterr().out.strip() == (
            "A sign-up for @alice is waiting for its emailed code: finish with "
            "hermes agentchat register --code <code>."
        )

    def test_the_wizard_in_a_profile_set_up_shows_its_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _Steps(monkeypatch)
        save_key(_KEY)
        shown: list[Any] = []
        monkeypatch.setattr(cli, "_dispatch_status", lambda args: shown.append(args) or _EXIT_OK)
        assert cli._dispatch_wizard(_ns()) == _EXIT_OK
        assert len(shown) == 1

    def test_status_names_a_waiting_sign_up(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _start(monkeypatch)
        capsys.readouterr()
        assert cli._dispatch_status(_ns()) == _EXIT_OK
        assert "A sign-up for @alice is waiting for its emailed code" in capsys.readouterr().out

    def test_logout_drops_a_waiting_sign_up(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _start(monkeypatch)
        capsys.readouterr()
        assert cli._dispatch_logout(_ns()) == _EXIT_OK
        out = capsys.readouterr().out
        assert "The sign-up waiting for its emailed code was dropped." in out
        assert _waiting() is None


def test_a_waiting_file_of_another_profile_is_not_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    other = tmp_path / "other-profile"
    signup_steps.save_pending(
        other, signup_steps.Pending(kind="register", flow={"version": 1, "flow": "register", "next_step": "verify",
                                                             "pending_id": "pnd_x"}, api_base="https://api.agentchat.me")
    )
    assert signup_steps.read_pending(other) is not None
    assert signup_steps.read_pending(profile_home()) is None
