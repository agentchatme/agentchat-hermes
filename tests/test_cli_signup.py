"""``hermes agentchat register | recover | login``, end to end on the fake API.

At a person's terminal ``register`` and ``recover`` are one interactive run
each on the SDK's sign-up helper (one step per command, an agent's terminal's
way, is ``test_cli_signup_steps.py``): the flags give what they give, stdin answers the rest (a
blank line cancels, ``new`` at the code prompt asks for a new code), and the
server's sentences are printed as they are. ``login`` asks the server about
the key before anything is saved. Saving and placement are patched; stdin
TTY-ness is forced so the suite behaves the same under a terminal and CI.
"""
from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any

import pytest
import signup_wire as sw
from fake_keys import api_key
from key_files import save_key
from sdk_wire import Wire, answer, api_error, connect_error, serve

from agentchatme_hermes import allow, cli, credentials, signup, signup_steps
from agentchatme_hermes.cli import _EXIT_API_ERR, _EXIT_ARG_ERR, _EXIT_OK, _EXIT_USER_CANCEL

if TYPE_CHECKING:
    from pathlib import Path

_KEY = api_key("cli_0000000000000000")
_CODE_PROMPT = "Enter the 6-digit code (blank to cancel, new for a new code): "


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class _Cli:
    """stdin with scripted lines (``""`` once they run out: the end of
    stdin), and what would have been saved and placed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []
        self.persisted: list[dict[str, str]] = []
        self.placed: list[str] = []
        self.waits: list[float] = []
        # The one question (allow.decide) is its own test; here: what it
        # answered, and when it was asked relative to the save.
        self.tools = allow.SetupDecision("yes", allow.ALLOWED_LINE)
        self.events: list[str] = []
        self.tools_decisions: list[Any] = []
        for name in ("AGENTCHATME_HANDLE", "AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY", "AGENTCHATME_API_BASE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(cli, "_input", self._input)
        monkeypatch.setattr(
            cli,
            "_persist_credentials",
            lambda *, api_key, handle, api_base: self.events.append("persist")
            or self.persisted.append({"api_key": api_key, "handle": handle, "api_base": api_base})
            or True,
        )
        monkeypatch.setattr(allow, "decide_for", lambda handle: self.events.append(f"decide {handle}") or self.tools)
        monkeypatch.setattr(cli, "_install_placement", self._place)
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
        # The interactive run (a person at a terminal answering prompts);
        # one step per command is test_cli_signup_steps.py.
        monkeypatch.setattr(signup_steps, "one_step_per_run", lambda: False)
        monkeypatch.setattr(signup, "_pause", self.waits.append)

    def _input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.lines.pop(0) if self.lines else ""

    def _place(self, handle: str, *, api_base: str, api_key: str, tools_decision: Any = None) -> list[Any]:
        self.events.append("place")
        self.placed.append(handle)
        self.tools_decisions.append(tools_decision)
        return []


# ─── register ──────────────────────────────────────────────────────────────


class TestRegister:
    def test_a_wrong_code_then_the_right_code_in_one_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: sw.code_sent(),
                sw.REGISTER_VERIFY: [sw.wrong_code(), sw.registered("alice", _KEY)],
            },
        )
        run = _Cli(monkeypatch, ["111111", "123456"])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        out = capsys.readouterr().out

        assert rc == _EXIT_OK
        assert wire.bodies() == [
            {"email": "a@b.co", "handle": "alice"},
            {"pending_id": "pnd_1", "code": "111111"},
            {"pending_id": "pnd_1", "code": "123456"},
        ]
        assert run.prompts == [_CODE_PROMPT, _CODE_PROMPT]
        assert out.splitlines()[:3] == [
            sw.sent_text("register"),
            sw.WRONG_CODE,
            "Email verified. Logged in as @alice.",
        ]
        assert "Registered @alice" in out
        assert _KEY not in out  # masked
        assert run.persisted == [
            {"api_key": _KEY, "handle": "alice", "api_base": "https://api.agentchat.me"}
        ]
        assert run.placed == ["alice"]

    def test_the_handle_the_server_returned_is_saved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(
            monkeypatch,
            {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice-x", _KEY)},
        )
        run = _Cli(monkeypatch, ["123456"])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="@Alice", display_name="Al"))
        assert rc == _EXIT_OK
        assert run.persisted[0]["handle"] == "alice-x"
        assert run.placed == ["alice-x"]

    def test_missing_flags_are_asked_on_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice", _KEY)},
        )
        run = _Cli(monkeypatch, ["a@b.co", "alice", "123456"])
        rc = cli._dispatch_register(_ns(email=None, handle=None, display_name=None))
        assert rc == _EXIT_OK
        assert run.prompts == [
            "Email address (blank to cancel): ",
            "Choose a @handle (blank to cancel): ",
            _CODE_PROMPT,
        ]
        assert wire.bodies()[0] == {"email": "a@b.co", "handle": "alice"}

    def test_new_at_the_code_prompt_sends_a_new_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.code_sent(pending_id="pnd_1"), sw.code_sent(pending_id="pnd_2")],
                sw.REGISTER_VERIFY: sw.registered("alice", _KEY),
            },
        )
        _Cli(monkeypatch, ["new", "123456"])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_OK
        assert wire.calls() == [("POST", sw.REGISTER), ("POST", sw.REGISTER), ("POST", sw.REGISTER_VERIFY)]
        assert wire.bodies()[2] == {"pending_id": "pnd_2", "code": "123456"}

    def test_a_blank_line_cancels(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(monkeypatch, {sw.REGISTER: sw.code_sent()})
        run = _Cli(monkeypatch, [""])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_USER_CANCEL
        assert wire.calls() == [("POST", sw.REGISTER)]
        assert "Cancelled." in capsys.readouterr().err
        assert run.persisted == []

    def test_the_end_of_stdin_cancels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.REGISTER: sw.code_sent()})
        run = _Cli(monkeypatch, [])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_USER_CANCEL
        assert run.persisted == []

    def test_too_many_wrong_codes_offers_a_new_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.code_sent(pending_id="pnd_1"), sw.code_sent(pending_id="pnd_2")],
                sw.REGISTER_VERIFY: [sw.attempts_exhausted(), sw.registered("alice", _KEY)],
            },
        )
        run = _Cli(monkeypatch, ["000000", "y", "123456"])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_OK
        assert run.prompts[1] == "Send a new code? [y/N]: "
        assert wire.bodies()[-1] == {"pending_id": "pnd_2", "code": "123456"}

    def test_a_taken_handle_is_asked_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.handle_taken("alice"), sw.code_sent()],
                sw.REGISTER_VERIFY: sw.registered("alice-2", _KEY),
            },
        )
        run = _Cli(monkeypatch, ["alice-2", "123456"])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_OK
        assert run.prompts[0] == "Choose a @handle (blank to cancel): "
        assert [body.get("handle") for body in wire.bodies()[:2]] == ["alice", "alice-2"]
        assert run.persisted[0]["handle"] == "alice-2"

    def test_the_email_limit_asks_for_another_email(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.email_limit(10), sw.code_sent(email="a+hermes@b.co")],
                sw.REGISTER_VERIFY: sw.registered("alice", _KEY),
            },
        )
        run = _Cli(monkeypatch, ["a+hermes@b.co", "123456"])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_OK
        assert run.prompts[0] == "Email address (blank to cancel): "
        assert [body.get("email") for body in wire.bodies()[:2]] == ["a@b.co", "a+hermes@b.co"]
        assert capsys.readouterr().out.splitlines()[0] == (
            "This email address already has 10 active agents, the most allowed. "
            "Use a different email address."
        )

    def test_a_server_stop_exits_1_and_saves_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        serve(monkeypatch, {sw.REGISTER: sw.not_sent()})
        run = _Cli(monkeypatch, [])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        captured = capsys.readouterr()
        assert rc == _EXIT_API_ERR
        assert captured.out.splitlines() == [sw.NOT_SENT]
        assert captured.err.strip() == "Nothing was saved."
        assert run.prompts == []
        assert run.persisted == []

    def test_unreachable(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        serve(monkeypatch, {sw.REGISTER: connect_error("dns")})
        run = _Cli(monkeypatch, [])
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_API_ERR
        assert "Could not reach AgentChat (dns). Nothing was saved." in capsys.readouterr().err
        assert run.persisted == []

    def test_an_sdk_without_the_helper_is_named(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(monkeypatch, {})
        _Cli(monkeypatch, [])
        monkeypatch.setattr(signup, "sdk_problem", lambda: "Sign-up needs agentchatme>=1.5.0.")
        rc = cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_API_ERR
        assert "Sign-up needs agentchatme>=1.5.0." in capsys.readouterr().err
        assert wire.requests == []


# ─── recover ───────────────────────────────────────────────────────────────


class TestRecover:
    def test_flags_then_a_wrong_code_then_the_right_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.RECOVER: sw.code_sent("recover"),
                sw.RECOVER_VERIFY: [sw.wrong_code("recover"), sw.recovered("alice", _KEY)],
            },
        )
        run = _Cli(monkeypatch, ["111111", "123456"])
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle="@Alice"))
        out = capsys.readouterr().out
        assert rc == _EXIT_OK
        assert wire.bodies()[0] == {"email": "a@b.co", "handle": "alice"}
        assert out.splitlines()[:4] == [
            "Completing recovery rotates the API key — anything still using the old key stops working.",
            sw.sent_text("recover"),
            sw.WRONG_CODE,
            "Email verified. Logged in as @alice. Your old API key no longer works.",
        ]
        assert "Recovered @alice" in out
        assert run.persisted[0]["handle"] == "alice"

    def test_the_saved_handle_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(
            monkeypatch,
            {sw.RECOVER: sw.code_sent("recover"), sw.RECOVER_VERIFY: sw.recovered("alice", _KEY)},
        )
        _Cli(monkeypatch, ["123456"])
        save_key(api_key("lost_but_saved_0000000"), handle="alice")
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle=None))
        assert rc == _EXIT_OK
        assert wire.bodies()[0] == {"email": "a@b.co", "handle": "alice"}
        assert "Recovering the locally configured agent @alice" in capsys.readouterr().out

    def test_several_agents_on_the_email_lists_them_and_asks_which(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.RECOVER: [sw.code_sent("recover", "pnd_1"), sw.code_sent("recover", "pnd_2")],
                sw.RECOVER_VERIFY: [sw.handle_required(["alice", "alice-2"]), sw.recovered("alice-2", _KEY)],
            },
        )
        run = _Cli(monkeypatch, ["123456", "alice-2", "654321"])
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle="alice"))
        out = capsys.readouterr().out
        assert rc == _EXIT_OK
        assert run.prompts[1] == "@handle of the agent to recover (blank to cancel): "
        assert "Agents registered with this email: @alice, @alice-2" in out
        assert wire.bodies()[2] == {"email": "a@b.co", "handle": "alice-2"}
        assert run.persisted[0]["handle"] == "alice-2"

    def test_non_interactive_without_handle_exits_2_naming_the_flag(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(monkeypatch, {})
        _Cli(monkeypatch, [])
        rc = cli._dispatch_recover(_ns(email="a@b.co", handle=None))
        assert rc == _EXIT_ARG_ERR
        assert "--handle <handle>" in capsys.readouterr().err
        assert wire.requests == []

    def test_non_interactive_without_email_exits_2_naming_the_flag(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(monkeypatch, {})
        _Cli(monkeypatch, [])
        rc = cli._dispatch_recover(_ns(email=None, handle="alice"))
        assert rc == _EXIT_ARG_ERR
        assert "--email <email>" in capsys.readouterr().err
        assert wire.requests == []

    def test_on_a_terminal_the_missing_fields_are_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {sw.RECOVER: sw.code_sent("recover"), sw.RECOVER_VERIFY: sw.recovered("alice", _KEY)},
        )
        run = _Cli(monkeypatch, ["a@b.co", "@Alice", "123456"])
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        rc = cli._dispatch_recover(_ns(email=None, handle=None))
        assert rc == _EXIT_OK
        assert run.prompts[:2] == [
            "Email address the agent was registered with (blank to cancel): ",
            "@handle of the agent to recover (blank to cancel): ",
        ]
        assert wire.bodies()[0] == {"email": "a@b.co", "handle": "alice"}

    def test_blank_on_a_terminal_cancels_before_any_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(monkeypatch, {})
        _Cli(monkeypatch, [""])
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        assert cli._dispatch_recover(_ns(email=None, handle="alice")) == _EXIT_USER_CANCEL
        assert wire.requests == []

    def test_ctrl_c_at_the_code_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.RECOVER: sw.code_sent("recover")})
        run = _Cli(monkeypatch, [])

        def interrupt(_prompt: str) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_input", interrupt)
        assert cli._dispatch_recover(_ns(email="a@b.co", handle="alice")) == _EXIT_USER_CANCEL
        assert run.persisted == []


# ─── login ─────────────────────────────────────────────────────────────────


class TestLogin:
    def test_a_key_the_server_rejects_is_not_saved(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(monkeypatch, {sw.ME: api_error(401, "UNAUTHORIZED", "Invalid API key")})
        run = _Cli(monkeypatch, [])
        rc = cli._dispatch_login(_ns(api_key=_KEY))
        assert rc == _EXIT_API_ERR
        assert "The key was not saved. AgentChat answered: Invalid API key" in capsys.readouterr().err
        assert wire.calls() == [("GET", sw.ME)]
        assert run.persisted == []
        assert run.placed == []

    def test_an_accepted_key_is_saved_with_the_server_handle(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice", "status": "active"})})
        run = _Cli(monkeypatch, [])
        rc = cli._dispatch_login(_ns(api_key=f"  {_KEY}  "))
        assert rc == _EXIT_OK
        assert wire.requests[0].headers["authorization"] == f"Bearer {_KEY}"
        assert run.persisted == [
            {"api_key": _KEY, "handle": "alice", "api_base": "https://api.agentchat.me"}
        ]
        assert "Saved AgentChat key for @alice." in capsys.readouterr().out

    def test_a_server_error_saves_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.ME: api_error(503, "INTERNAL_ERROR", "Service unavailable")})
        run = _Cli(monkeypatch, [])
        assert cli._dispatch_login(_ns(api_key=_KEY)) == _EXIT_API_ERR
        assert run.persisted == []

    def test_the_key_prompt_is_masked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``login`` in a terminal without an option reads the key with
        ``getpass`` (no echo), then checks it with the server like a flag."""
        wire = serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        asked: list[str] = []
        monkeypatch.setattr(cli, "getpass", lambda text: asked.append(text) or f"{_KEY}\n")
        assert cli._dispatch_login(_ns(api_key=None)) == _EXIT_OK
        assert asked == ["API key (ac_…): "]
        assert run.prompts == []  # nothing read through the echoing prompt
        assert wire.requests[0].headers["authorization"] == f"Bearer {_KEY}"
        assert run.persisted[0]["handle"] == "alice"

    def test_ctrl_c_at_the_key_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _Cli(monkeypatch, [])
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)

        def interrupt() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_prompt_api_key", interrupt)
        assert cli._dispatch_login(_ns(api_key=None)) == _EXIT_USER_CANCEL


class TestLoginKeyNeverOnTheCommandLine:
    """``login --api-key <key>`` was the only scriptable way in, so the
    key sat on argv (readable by every user of the box). It can now come
    from stdin or a file, the documented ways never put it on argv, and the
    old flag still works but says why it should not be used."""

    def _stdin(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        import io

        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    def test_a_pipe_with_no_option(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        self._stdin(monkeypatch, f"{_KEY}\n")
        assert cli._dispatch_login(_ns(api_key=None, api_key_file=None)) == _EXIT_OK
        assert wire.requests[0].headers["authorization"] == f"Bearer {_KEY}"
        assert run.persisted[0]["api_key"] == _KEY
        captured = capsys.readouterr()
        assert _KEY not in captured.out + captured.err
        assert "Warning" not in captured.err

    def test_api_key_dash_reads_stdin(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        self._stdin(monkeypatch, f"  {_KEY}  \n")
        assert cli._dispatch_login(_ns(api_key="-", api_key_file=None)) == _EXIT_OK
        assert run.persisted[0]["api_key"] == _KEY
        assert "Warning" not in capsys.readouterr().err

    def test_api_key_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]) -> None:
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        key_file = tmp_path / "key.txt"
        key_file.write_text(f"\n{_KEY}\nsomething else\n", encoding="utf-8")
        assert cli._dispatch_login(_ns(api_key=None, api_key_file=str(key_file))) == _EXIT_OK
        assert run.persisted[0]["api_key"] == _KEY
        assert key_file.exists(), "the caller's file is left as it is"
        captured = capsys.readouterr()
        assert _KEY not in captured.out + captured.err

    def test_api_key_dash_at_a_terminal_does_not_echo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
        asked: list[str] = []
        monkeypatch.setattr(cli, "getpass", lambda text: asked.append(text) or _KEY)
        assert cli._dispatch_login(_ns(api_key="-", api_key_file=None)) == _EXIT_OK
        assert asked == ["API key (ac_…): "] and run.persisted[0]["api_key"] == _KEY

    def test_api_key_file_dash_reads_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        self._stdin(monkeypatch, f"{_KEY}\n")
        assert cli._dispatch_login(_ns(api_key=None, api_key_file="-")) == _EXIT_OK
        assert run.persisted[0]["api_key"] == _KEY

    def test_a_missing_or_empty_file_saves_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire = serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        assert cli._dispatch_login(_ns(api_key=None, api_key_file=str(tmp_path / "nope"))) == _EXIT_API_ERR
        assert "Could not read the key from" in capsys.readouterr().err
        empty = tmp_path / "empty"
        empty.write_text("\n  \n", encoding="utf-8")
        assert cli._dispatch_login(_ns(api_key=None, api_key_file=str(empty))) == _EXIT_API_ERR
        assert "holds no key" in capsys.readouterr().err
        assert wire.calls() == []
        assert run.persisted == []

    def test_an_empty_pipe_is_no_key(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        wire = serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        self._stdin(monkeypatch, "")
        assert cli._dispatch_login(_ns(api_key=None, api_key_file=None)) == _EXIT_API_ERR
        assert "No API key provided." in capsys.readouterr().err
        assert wire.calls() == [] and run.persisted == []

    def test_the_key_on_argv_still_works_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        run = _Cli(monkeypatch, [])
        assert cli._dispatch_login(_ns(api_key=_KEY, api_key_file=None)) == _EXIT_OK
        assert run.persisted[0]["api_key"] == _KEY
        captured = capsys.readouterr()
        assert "--api-key <key> puts the key on the command line" in captured.err
        assert "hermes agentchat login --api-key-file <path>" in captured.err
        assert _KEY not in captured.out + captured.err

    def test_no_printed_next_step_puts_a_key_on_a_command_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """What ``login`` prints after a save names no ``--api-key <key>`` recipe."""
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        _Cli(monkeypatch, [])
        self._stdin(monkeypatch, f"{_KEY}\n")
        assert cli._dispatch_login(_ns(api_key="-", api_key_file=None)) == _EXIT_OK
        captured = capsys.readouterr()
        assert "--api-key " not in captured.out
        assert "ac_" not in captured.out.replace(_KEY[:8], "")

    def test_the_two_options_exclude_each_other(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        cli.setup_argparse(parser)
        args = parser.parse_args(["login", "--api-key-file", "/k"])
        assert args.api_key_file == "/k" and args.api_key is None
        args = parser.parse_args(["login", "--api-key", "-"])
        assert args.api_key == "-"
        with pytest.raises(SystemExit):
            parser.parse_args(["login", "--api-key", "-", "--api-key-file", "/k"])


# ─── the other subcommands, offline ────────────────────────────────────────


@pytest.fixture
def _offline(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """``GET /v1/agents/me`` cannot connect. The SDK retries a GET; its
    backoff sleeps are skipped."""
    import time
    import types

    from agentchatme import _http

    monkeypatch.setattr(
        _http, "time", types.SimpleNamespace(sleep=lambda _s: None, perf_counter=time.perf_counter)
    )
    return serve(monkeypatch, {sw.ME: connect_error("dns")})


class TestOfflineSubcommands:
    """The SDK's ``ConnectionError`` is not an ``AgentChatError``: login,
    status and doctor say they could not connect instead of a traceback."""

    def test_login(
        self, monkeypatch: pytest.MonkeyPatch, _offline: Wire, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run = _Cli(monkeypatch, [])
        rc = cli._dispatch_login(_ns(api_key=_KEY))
        assert rc == _EXIT_API_ERR
        assert "Could not reach AgentChat (dns). The key was not saved." in capsys.readouterr().err
        assert run.persisted == []
        assert _offline.calls() == [("GET", sw.ME)]

    def test_status(
        self, monkeypatch: pytest.MonkeyPatch, _offline: Wire, capsys: pytest.CaptureFixture[str]
    ) -> None:
        save_key(_KEY)
        rc = cli._dispatch_status(_ns())
        assert rc == _EXIT_API_ERR
        assert "Status fetch failed: dns" in capsys.readouterr().err

    def test_doctor(
        self, monkeypatch: pytest.MonkeyPatch, _offline: Wire, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentchatme_hermes import kit, mcp_registration

        save_key(_KEY)
        monkeypatch.setattr(cli, "_other_gateway_running", lambda: False)
        monkeypatch.setattr(cli, "_hermes_mcp_client_importable", lambda: True)
        monkeypatch.setattr(mcp_registration, "inspect_entry_identity", lambda **_kw: ("ours", "current"))
        monkeypatch.setattr(kit, "kit_problem", lambda: None)
        rc = cli._dispatch_doctor(_ns(fix=False))
        out = capsys.readouterr().out
        assert rc >= 1
        assert "[fail] Could not reach /v1/agents/me: dns" in out


# ─── the one question about the tools ──────────────────────────────────────


class TestToolsQuestion:
    """Asked after the server answered with the key and before it is saved,
    so the tools' entry is written once, with the answer."""

    def test_register_asks_before_the_key_is_saved(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        serve(monkeypatch, {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice", _KEY)})
        run = _Cli(monkeypatch, ["123456"])
        assert cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None)) == _EXIT_OK
        assert run.events == ["decide alice", "persist", "place"]
        assert run.tools_decisions == ["yes"]
        assert allow.ALLOWED_LINE in capsys.readouterr().out

    def test_recover_asks_before_the_key_is_saved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.RECOVER: sw.code_sent("recover"), sw.RECOVER_VERIFY: sw.recovered("alice", _KEY)})
        run = _Cli(monkeypatch, ["123456"])
        run.tools = allow.SetupDecision("no", allow.NOT_ALLOWED_LINE.format(who="@alice"))
        assert cli._dispatch_recover(_ns(email="a@b.co", handle="alice")) == _EXIT_OK
        assert run.events == ["decide alice", "persist", "place"]
        assert run.tools_decisions == ["no"]

    def test_login_asks_before_the_key_is_saved(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice", "status": "active"})})
        run = _Cli(monkeypatch, [])
        run.tools = allow.SetupDecision(None, allow.AGENT_TERMINAL_LINE.format(who="@alice"))
        assert cli._dispatch_login(_ns(api_key=_KEY)) == _EXIT_OK
        assert run.events == ["decide alice", "persist", "place"]
        assert run.tools_decisions == [None], "nobody asked: no tools entry"
        assert "/agentchat allow" in capsys.readouterr().out

    def test_a_refused_key_asks_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.ME: api_error(401, "UNAUTHORIZED", "Invalid API key")})
        run = _Cli(monkeypatch, [])
        assert cli._dispatch_login(_ns(api_key=_KEY)) == _EXIT_API_ERR
        assert run.events == []


# ─── whether the key just saved needs a restart (lab R3, L1-F02 / L1-F04) ───


class TestKeyInUse:
    """Whether a restart is needed is decided by whether a running gateway
    holds an AgentChat connection for this profile (it keeps the key it
    started with): a first key in a profile no gateway serves needs none; a
    different key while one serves does — also after a logout, which leaves
    that gateway serving — and in an agent's own terminal the restart is the
    person's, never the agent's."""

    @pytest.fixture(autouse=True)
    def _a_gateway_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gateway serves the profile unless a test says none does (with
        Hermes installed, its liveness would read this test home's as stopped)."""
        from agentchatme_hermes import placement

        monkeypatch.setattr(placement, "gateway_runs_for", lambda home: True)

    def _register(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice", _KEY)})
        _Cli(monkeypatch, ["123456"])
        assert cli._dispatch_register(_ns(email="a@b.co", handle="alice", display_name=None)) == _EXIT_OK

    def _serving(self, monkeypatch: pytest.MonkeyPatch, serving: bool) -> None:
        from agentchatme_hermes import placement

        monkeypatch.setattr(placement, "gateway_serves_profile", lambda home, *, api_base: serving)

    def test_a_first_key_needs_no_restart(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        self._register(monkeypatch)
        out = capsys.readouterr().out
        assert credentials.NO_RESTART_LINE in out and "hermes gateway restart" not in out

    def test_in_an_agents_terminal_a_first_key_names_no_restart_at_all(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HERMES_SESSION_ID", "20261127_002100_r3c")
        self._register(monkeypatch)
        out = capsys.readouterr().out
        assert credentials.NO_RESTART_LINE in out and "restart" not in out.replace("no restart", "")

    def test_another_key_while_a_gateway_serves_needs_the_restart(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        save_key(api_key("earlier_00000000000000"))
        self._serving(monkeypatch, True)
        self._register(monkeypatch)
        out = capsys.readouterr().out
        assert credentials.RESTART_HINT in out and credentials.NO_RESTART_LINE not in out

    def test_register_with_no_gateway_running(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from agentchatme_hermes import placement

        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        monkeypatch.setattr(placement, "gateway_runs_for", lambda home: False)
        self._register(monkeypatch)
        out = capsys.readouterr().out
        assert "AgentChat reaches this agent once one runs (`hermes gateway run`" in out
        assert credentials.NO_RESTART_LINE not in out

    def test_another_key_with_no_gateway_serving_needs_none(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        save_key(api_key("earlier_00000000000000"))
        self._serving(monkeypatch, False)
        self._register(monkeypatch)
        assert credentials.NO_RESTART_LINE in capsys.readouterr().out

    def test_in_an_agents_terminal_the_restart_is_the_persons(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HERMES_SESSION_ID", "20261127_002100_r3c")
        save_key(api_key("earlier_00000000000000"))
        self._serving(monkeypatch, True)
        self._register(monkeypatch)
        out = capsys.readouterr().out
        assert credentials.RESTART_HINT_AGENT in out and "Restart the gateway" not in out
        assert "`hermes gateway restart`" not in out

    @pytest.mark.parametrize(("held", "said"), [("same", "none"), ("other", "restart")])
    def test_login(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], held: str, said: str) -> None:
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        save_key(_KEY if held == "same" else api_key("earlier_00000000000000"))
        self._serving(monkeypatch, True)
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice", "status": "active"})})
        _Cli(monkeypatch, [])
        assert cli._dispatch_login(_ns(api_key=_KEY)) == _EXIT_OK
        out = capsys.readouterr().out
        assert (credentials.RESTART_HINT if said == "restart" else credentials.NO_RESTART_LINE) in out

    def test_logout_then_another_key_while_that_gateway_serves_needs_the_restart(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Logout says the running gateway still holds the old connection; the
        next sign-up in the profile agrees: a restart is needed, until that
        gateway (pid and process start time) is gone."""
        import os

        from agentchatme_hermes import allow, placement
        from agentchatme_hermes.profile_scope import resolve_hermes_home

        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        home = resolve_hermes_home()
        save_key(api_key("earlier_00000000000000"))
        alive = {"pid": os.getpid(), "handle": "bob"}
        monkeypatch.setattr(placement, "serving_gateway", lambda h, *, api_base: dict(alive) if h == home else None)
        monkeypatch.setattr(allow, "process_start_time", lambda pid: 4242)
        assert "still holds @bob's AgentChat connection" in (placement.gateway_still_serving(home, api_base="x") or "")
        monkeypatch.setattr(placement, "serving_gateway", lambda h, *, api_base: None)  # the key is gone
        marker = home / "agentchat" / placement.SERVING_GATEWAY_FILENAME
        seen: dict[str, Any] = {"now": (os.getpid(), 4242)}
        monkeypatch.setattr(allow, "gateway_seen", lambda h: seen["now"])
        assert placement.gateway_serves_profile(home, api_base="x") is True
        self._register(monkeypatch)
        assert credentials.RESTART_HINT in capsys.readouterr().out
        for unsure in ("unknown", (os.getpid(), None)):  # cannot be told / one start time readable: kept
            seen["now"] = unsure
            assert placement.gateway_serves_profile(home, api_base="x") is True and marker.exists()
        seen["now"] = (os.getpid(), 99999)  # that gateway restarted: shown gone
        assert placement.gateway_serves_profile(home, api_base="x") is False
        assert not marker.exists(), "forgotten once shown gone"

    def test_a_yes_the_gateway_takes_up_only_at_its_restart_is_not_called_allowed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentchatme_hermes.placement import PlacementStep

        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice", "status": "active"})})
        run = _Cli(monkeypatch, [])
        monkeypatch.setattr(
            cli,
            "_install_placement",
            lambda handle, **_kw: run.placed.append(handle) or [PlacementStep("mcp", "warned", "not in effect yet", "restart")],
        )
        assert cli._dispatch_login(_ns(api_key=_KEY)) == _EXIT_OK
        assert allow.ALLOWED_LINE not in capsys.readouterr().out


class TestKeyInUseLine:
    @pytest.mark.parametrize(
        ("before", "serving", "terminal", "line"),
        [
            (None, False, False, "NO_RESTART_LINE"),
            (None, False, True, "NO_RESTART_LINE"),
            ("other", False, False, "NO_RESTART_LINE"),  # no gateway holds anything
            ("k", True, False, "NO_RESTART_LINE"),  # the same key again
            ("other", True, False, "RESTART_HINT"),
            ("other", True, True, "RESTART_HINT_AGENT"),
            (None, True, False, "RESTART_HINT"),  # after a logout that gateway still serves
            ("", True, False, "RESTART_HINT"),  # a refused copy whose key is not known
        ],
    )
    def test_the_line(
        self, monkeypatch: pytest.MonkeyPatch, before: str | None, serving: bool, terminal: bool, line: str
    ) -> None:
        if terminal:
            monkeypatch.setenv("HERMES_SESSION_ID", "s")
        else:
            monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        assert credentials.key_in_use_line(before, "k", serving=serving) == getattr(credentials, line)

    @pytest.mark.parametrize(("runs", "start"), [(False, True), (None, False), (True, False)])
    def test_with_no_gateway_running_it_says_the_agent_is_reached_once_one_runs(
        self, tmp_path: Path, runs: bool | None, start: bool
    ) -> None:
        """Lab R3 (L2-F09): "no restart needed" was said with no gateway
        running at all; nothing said the agent waits for one."""
        line = credentials.key_in_use_line(None, "k", serving=False, runs=runs, home=tmp_path / ".hermes")
        if start:
            assert line.startswith("No gateway runs for this profile now: AgentChat reaches this agent once one runs")
            assert "`hermes gateway run`" in line and "restart" not in line
        else:
            assert line == credentials.NO_RESTART_LINE

    def test_a_named_profile_names_its_own_gateway(self, tmp_path: Path) -> None:
        line = credentials.start_gateway_line(tmp_path / ".hermes" / "profiles" / "r3x")
        assert "`hermes -p r3x gateway run`" in line

    def test_what_the_profile_held(self) -> None:
        assert credentials.key_held() is None
        save_key(_KEY)
        assert credentials.key_held() == _KEY
