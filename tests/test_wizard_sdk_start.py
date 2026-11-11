"""Registration and recovery through the SDK, from the request on the wire to
the line the operator reads.

``_register_start`` and ``_recover_start`` call ``AgentChatClient.register``
and ``AgentChatClient.recover``. The fake API in ``sdk_wire`` answers at the
httpx transport, so the SDK's request building, its single attempt and its
error classes all run for real. Each server answer is checked down to the
wizard's own message, and the interactive flows and the named subcommands
are run end to end on the same wire: prompts, re-prompts, and what is saved.

The per-email policy messages are ``test_wizard_email_policy.py``; the
recovery start's own answers are ``test_wizard_recovery.py``.
"""
from __future__ import annotations

import argparse
from typing import Any

import pytest
from fake_keys import api_key
from sdk_wire import Route, Wire, answer, api_error, connect_error, raw, read_timeout, serve

from agentchatme_hermes import cli, wizard
from agentchatme_hermes._version import __version__
from agentchatme_hermes.cli import _EXIT_API_ERR, _EXIT_OK
from agentchatme_hermes.wizard import _register_start, _RegisterError

_REGISTER = "/v1/register"
_REGISTER_VERIFY = "/v1/register/verify"
_RECOVER = "/v1/agents/recover"
_RECOVER_VERIFY = "/v1/agents/recover/verify"


def _start(display_name: str = "") -> str:
    return _register_start(email="a@b.co", handle="alice", display_name=display_name)


def _register_fails(monkeypatch: pytest.MonkeyPatch, route: Route) -> tuple[_RegisterError, Wire]:
    wire = serve(monkeypatch, {_REGISTER: route})
    with pytest.raises(_RegisterError) as info:
        _start()
    return info.value, wire


# ─── the request ───────────────────────────────────────────────────────────


class TestRegisterRequest:
    def test_one_post_with_hermes_identity_and_the_old_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTCHATME_API_BASE", "https://api.test/")
        wire = serve(monkeypatch, {_REGISTER: answer(201, {"pending_id": "pnd_1"})})

        assert _start(display_name="Alice") == "pnd_1"

        assert wire.calls() == [("POST", _REGISTER)]
        request = wire.requests[0]
        assert str(request.url) == "https://api.test/v1/register"
        assert request.headers["X-AgentChat-Client"] == "hermes"
        assert request.headers["X-AgentChat-Client-Version"] == __version__
        assert request.headers["content-type"] == "application/json"
        assert "authorization" not in request.headers
        assert request.extensions["timeout"] == {
            "connect": 15.0,
            "read": 15.0,
            "write": 15.0,
            "pool": 15.0,
        }
        assert wire.bodies() == [{"email": "a@b.co", "handle": "alice", "display_name": "Alice"}]

    def test_empty_display_name_and_description_are_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The server validates a present field as a string: an empty
        display name is left out, not sent as ``""``, and no
        ``description`` (``null``) is ever sent."""
        wire = serve(monkeypatch, {_REGISTER: answer(200, {"pending_id": "pnd_1"})})
        _start(display_name="")
        assert wire.bodies() == [{"email": "a@b.co", "handle": "alice"}]

    def test_default_api_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTCHATME_API_BASE", raising=False)
        wire = serve(monkeypatch, {_REGISTER: answer(200, {"pending_id": "pnd_1"})})
        _start()
        assert str(wire.requests[0].url) == "https://api.agentchat.me/v1/register"


# ─── every answer, down to the wizard's message ────────────────────────────


class TestRegisterAnswers:
    @pytest.mark.parametrize("code", ["HANDLE_TAKEN", "INVALID_HANDLE", "RESERVED_HANDLE"])
    def test_handle_codes_are_field_scoped(
        self, monkeypatch: pytest.MonkeyPatch, code: str
    ) -> None:
        err, _wire = _register_fails(monkeypatch, api_error(409, code, "That handle is taken."))
        assert (str(err), err.field, err.code) == ("That handle is taken.", "handle", code)

    def test_handle_code_without_a_message_uses_the_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err, _wire = _register_fails(monkeypatch, api_error(409, "HANDLE_TAKEN", ""))
        assert str(err) == "HANDLE_TAKEN"

    def test_rate_limited_is_one_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, wire = _register_fails(
            monkeypatch, api_error(429, "RATE_LIMITED", "Too many requests", {"retry_after_ms": 10})
        )
        assert str(err) == "rate-limited — wait a minute and try again"
        assert (err.field, err.code) == (None, "RATE_LIMITED")
        assert len(wire.requests) == 1

    def test_validation_error_names_the_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, _wire = _register_fails(
            monkeypatch,
            api_error(
                400,
                "VALIDATION_ERROR",
                "Invalid request",
                {"fieldErrors": {"email": ["Invalid email"]}},
            ),
        )
        assert str(err) == "Invalid request (email: Invalid email)"
        assert (err.field, err.code) == (None, "VALIDATION_ERROR")

    def test_server_error_is_one_attempt_with_the_server_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err, wire = _register_fails(
            monkeypatch, api_error(503, "INTERNAL_ERROR", "Service unavailable")
        )
        assert (str(err), err.code) == ("Service unavailable", "INTERNAL_ERROR")
        assert len(wire.requests) == 1

    def test_unknown_code_is_the_server_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, _wire = _register_fails(monkeypatch, api_error(422, "SOMETHING_NEW", "New rule."))
        assert (str(err), err.field, err.code) == ("New rule.", None, "SOMETHING_NEW")

    def test_error_page_that_is_not_an_api_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A proxy's HTML page: the SDK names it by the HTTP reason phrase."""
        err, _wire = _register_fails(monkeypatch, raw(502, b"<html>bad gateway</html>"))
        assert str(err) == "Bad Gateway"
        assert err.field is None

    def test_bodiless_429_counts_as_rate_limited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, _wire = _register_fails(monkeypatch, raw(429, b""))
        assert str(err) == "rate-limited — wait a minute and try again"

    def test_success_that_is_not_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, _wire = _register_fails(monkeypatch, raw(200, b"<html>captive portal</html>"))
        assert (str(err), err.code) == ("invalid server response", None)

    def test_success_with_an_empty_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, _wire = _register_fails(monkeypatch, raw(200, b"", "application/json"))
        assert str(err) == "invalid server response"

    def test_success_that_is_not_an_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, _wire = _register_fails(monkeypatch, answer(200, ["pnd_1"]))
        assert str(err) == "invalid server response"

    @pytest.mark.parametrize("body", [{}, {"pending_id": ""}, {"pending_id": 7}])
    def test_success_without_a_pending_id(
        self, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
    ) -> None:
        err, _wire = _register_fails(monkeypatch, answer(200, body))
        assert str(err) == "server did not return a pending_id"

    def test_network_error_is_one_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, wire = _register_fails(monkeypatch, connect_error("[Errno 8] nodename nor servname"))
        assert str(err) == "network error: [Errno 8] nodename nor servname"
        assert (err.field, err.code) == (None, None)
        assert len(wire.requests) == 1

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        err, _wire = _register_fails(monkeypatch, read_timeout("The read operation timed out"))
        assert str(err) == "network error: The read operation timed out"

    def test_sdk_bugs_are_not_reported_as_server_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the SDK's own errors become a ``_RegisterError``; anything
        else reaches the flow's "Could not reach AgentChat" catch-all."""
        from agentchatme import AgentChatClient

        def broken(**_kwargs: Any) -> Any:
            raise TypeError("unexpected keyword")

        monkeypatch.setattr(AgentChatClient, "register", staticmethod(broken))
        with pytest.raises(TypeError):
            _start()


# ─── the interactive flows, end to end ─────────────────────────────────────


class _Prompt:
    """Hermes' ``prompt(question, default=None, password=False)`` with scripted answers."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str, default: Any = None, password: bool = False) -> str:
        self.questions.append(question)
        value = self.answers.pop(0)
        if value == "" and default is not None:
            return str(default)
        return value


class _Session:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answers: list[str], *, menu: int = 0) -> None:
        self.prompt = _Prompt(answers)
        self.menu = menu
        self.menus: list[str] = []
        self.info: list[str] = []
        self.success: list[str] = []
        self.warning: list[str] = []
        self.env: dict[str, str] = {}
        self.placed: list[str] = []
        monkeypatch.setattr(wizard, "resolve_identity_line", lambda **_kw: "I am @x on AgentChat.")
        monkeypatch.setattr(
            wizard,
            "place_identity_and_tools",
            lambda *, handle, identity_line, api_base, soul_path=None: self.placed.append(handle) or [],
        )

    def prompt_choice(self, question: str, choices: list[str], default: int = 0, **_: Any) -> int:
        self.menus.append(question)
        return self.menu

    def save_env_value(self, key: str, value: str) -> None:
        self.env[key] = value

    def register(self) -> bool:
        return wizard._register_new_agent_flow(
            prompt=self.prompt,
            prompt_choice=self.prompt_choice,
            print_info=self.info.append,
            print_success=self.success.append,
            print_warning=self.warning.append,
            save_env_value=self.save_env_value,
        )

    def recover(self) -> bool:
        return wizard._recover_lost_key_flow(
            prompt=self.prompt,
            print_info=self.info.append,
            print_success=self.success.append,
            print_warning=self.warning.append,
            save_env_value=self.save_env_value,
        )


_KEY = api_key("wizard_000000000000")


class TestRegisterFlowOnTheWire:
    def test_taken_handle_reprompts_only_the_handle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                _REGISTER: [
                    api_error(409, "HANDLE_TAKEN", "Handle already taken"),
                    answer(200, {"pending_id": "pnd_1"}),
                ],
                _REGISTER_VERIFY: answer(200, {"api_key": _KEY, "agent": {"handle": "alice-2"}}),
            },
        )
        session = _Session(monkeypatch, ["a@b.co", "alice", "", "alice-2", "123456"])

        assert session.register() is True

        assert wire.calls() == [("POST", _REGISTER), ("POST", _REGISTER), ("POST", _REGISTER_VERIFY)]
        assert wire.bodies() == [
            {"email": "a@b.co", "handle": "alice"},
            {"email": "a@b.co", "handle": "alice-2"},
            {"pending_id": "pnd_1", "code": "123456"},
        ]
        assert session.warning == ["Handle problem: Handle already taken"]
        assert session.prompt.questions[3].startswith("Choose a @handle")
        assert "Verification code sent to a@b.co. Check your inbox." in session.info
        assert session.env == {"AGENTCHAT_API_KEY": _KEY, "AGENTCHATME_HANDLE": "alice-2"}
        assert session.placed == ["alice-2"]
        assert wire.unexpected == []

    def test_handle_errors_stop_after_three_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(monkeypatch, {_REGISTER: api_error(409, "HANDLE_TAKEN", "taken")})
        session = _Session(monkeypatch, ["a@b.co", "alice", "", "bob", "carol"])
        assert session.register() is False
        assert len(wire.requests) == 3
        assert session.warning == [
            "Handle problem: taken",
            "Handle problem: taken",
            "Registration failed: taken",
        ]
        assert session.env == {}

    def test_live_cap_offers_recovery_with_the_email_filled_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                _REGISTER: api_error(409, "EMAIL_LIMIT_REACHED", "limit", {"limit": 10}),
                _RECOVER: answer(200, {"pending_id": "pnd_r", "message": "sent"}),
                _RECOVER_VERIFY: answer(200, {"handle": "alice", "api_key": _KEY}),
            },
        )
        # email, handle, display name; menu → recover; email (ENTER keeps it), handle, code
        session = _Session(monkeypatch, ["a@b.co", "alice", "", "", "alice", "654321"], menu=2)

        assert session.register() is True

        assert session.menus == ["That email is at its live-agent limit. What now?"]
        assert session.warning == [
            "This email already backs 10 active agents — the server's per-email limit. "
            "Delete one of them, or register with a different email."
        ]
        assert wire.calls() == [("POST", _REGISTER), ("POST", _RECOVER), ("POST", _RECOVER_VERIFY)]
        assert wire.bodies()[1] == {"email": "a@b.co", "handle": "alice"}
        assert session.env == {"AGENTCHAT_API_KEY": _KEY, "AGENTCHATME_HANDLE": "alice"}

    def test_lifetime_cap_then_a_different_email(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                _REGISTER: [
                    api_error(409, "EMAIL_EXHAUSTED", "spent", {"limit": 30}),
                    answer(200, {"pending_id": "pnd_1"}),
                ],
                _REGISTER_VERIFY: answer(200, {"api_key": _KEY, "agent": {"handle": "alice"}}),
            },
        )
        session = _Session(monkeypatch, ["a@b.co", "alice", "", "a+hermes@b.co", "123456"], menu=0)

        assert session.register() is True

        assert session.menus == ["That email has no registrations left. What now?"]
        assert [body.get("email") for body in wire.bodies()[:2]] == ["a@b.co", "a+hermes@b.co"]

    @pytest.mark.parametrize(
        ("route", "warning"),
        [
            (connect_error("dns"), "Registration failed: network error: dns"),
            (
                api_error(429, "RATE_LIMITED", "slow down"),
                "Registration failed: rate-limited — wait a minute and try again",
            ),
            (
                api_error(400, "VALIDATION_ERROR", "Invalid request", {"fieldErrors": {"email": ["Invalid email"]}}),
                "Registration failed: Invalid request (email: Invalid email)",
            ),
            (raw(200, b"<html/>"), "Registration failed: invalid server response"),
            (answer(200, {}), "Registration failed: server did not return a pending_id"),
        ],
    )
    def test_failures_print_one_line_and_never_ask_for_a_code(
        self, monkeypatch: pytest.MonkeyPatch, route: Route, warning: str
    ) -> None:
        wire = serve(monkeypatch, {_REGISTER: route})
        # Three answers only: an OTP prompt would run out of them.
        session = _Session(monkeypatch, ["a@b.co", "alice", ""])
        assert session.register() is False
        assert session.warning == [warning]
        assert wire.calls() == [("POST", _REGISTER)]
        assert session.env == {}

    def test_wrong_code_at_verify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(
            monkeypatch,
            {
                _REGISTER: answer(200, {"pending_id": "pnd_1"}),
                _REGISTER_VERIFY: api_error(400, "INVALID_CODE", "Invalid or expired code"),
            },
        )
        session = _Session(monkeypatch, ["a@b.co", "alice", "", "000000"])
        assert session.register() is False
        assert session.warning == ["Verification failed: Invalid or expired code"]
        assert session.env == {}


class TestRecoverFlowOnTheWire:
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                _RECOVER: answer(200, {"pending_id": "pnd_r", "message": "sent"}),
                _RECOVER_VERIFY: answer(200, {"handle": "alice", "api_key": _KEY}),
            },
        )
        session = _Session(monkeypatch, ["a@b.co", "alice", "123456"])
        assert session.recover() is True
        assert wire.bodies() == [
            {"email": "a@b.co", "handle": "alice"},
            {"pending_id": "pnd_r", "code": "123456"},
        ]
        assert session.env == {"AGENTCHAT_API_KEY": _KEY, "AGENTCHATME_HANDLE": "alice"}
        assert session.warning == []

    @pytest.mark.parametrize(
        ("route", "warning"),
        [
            (connect_error("dns"), "Recovery request failed: network error: dns"),
            (
                api_error(429, "RATE_LIMITED", "slow down"),
                "Recovery request failed: rate-limited — wait a minute and try again",
            ),
            (
                answer(200, {"message": "if it exists"}),
                "Recovery request failed: no recovery code was issued (the server returned "
                "no pending_id) — check the email and @handle and try again",
            ),
            (raw(502, b"<html/>"), "Recovery request failed: Bad Gateway"),
        ],
    )
    def test_start_failures(self, monkeypatch: pytest.MonkeyPatch, route: Route, warning: str) -> None:
        wire = serve(monkeypatch, {_RECOVER: route})
        session = _Session(monkeypatch, ["a@b.co", "alice"])
        assert session.recover() is False
        assert session.warning == [warning]
        assert wire.calls() == [("POST", _RECOVER)]

    def test_handle_required_lists_the_handles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(
            monkeypatch,
            {
                _RECOVER: answer(200, {"pending_id": "pnd_r"}),
                _RECOVER_VERIFY: api_error(
                    409,
                    "HANDLE_REQUIRED",
                    "This email backs more than one agent.",
                    {"handles": ["alice", "alice-2"]},
                ),
            },
        )
        session = _Session(monkeypatch, ["a@b.co", "alice", "123456"])
        assert session.recover() is False
        assert session.warning == [
            "This email backs more than one agent.",
            "Agents registered under this email: @alice, @alice-2",
            "Run recovery again and enter the @handle you want to re-key.",
        ]


# ─── the named subcommands, end to end ─────────────────────────────────────


@pytest.fixture
def _cli_env(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    for name in ("AGENTCHATME_HANDLE", "AGENTCHAT_API_KEY", "AGENTCHATME_API_KEY", "AGENTCHATME_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    persisted: list[dict[str, str]] = []
    monkeypatch.setattr(
        cli,
        "_persist_credentials",
        lambda *, api_key, handle, api_base: persisted.append({"api_key": api_key, "handle": handle}),
    )
    monkeypatch.setattr(cli, "_install_placement", lambda handle, *, api_base, api_key: [])
    monkeypatch.setattr(cli, "_prompt_code", lambda: "123456")
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    return persisted


class TestSubcommandsOnTheWire:
    def test_register(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        _cli_env: list[dict[str, str]],
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                _REGISTER: answer(200, {"pending_id": "pnd_1"}),
                _REGISTER_VERIFY: answer(200, {"api_key": _KEY, "agent": {"handle": "alice"}}),
            },
        )
        rc = cli._dispatch_register(argparse.Namespace(email="a@b.co", handle="alice", display_name=None))
        assert rc == _EXIT_OK
        assert wire.bodies()[0] == {"email": "a@b.co", "handle": "alice"}
        assert _cli_env == [{"api_key": _KEY, "handle": "alice"}]
        assert "Registered @alice" in capsys.readouterr().out

    def test_register_lifetime_cap_hints_recover(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        _cli_env: list[dict[str, str]],
    ) -> None:
        serve(monkeypatch, {_REGISTER: api_error(409, "EMAIL_EXHAUSTED", "spent", {"limit": 30})})
        rc = cli._dispatch_register(argparse.Namespace(email="a@b.co", handle="alice", display_name=None))
        err = capsys.readouterr().err
        assert rc == _EXIT_API_ERR
        assert (
            "Registration request failed: This email has used all 30 of its lifetime "
            "registrations — the server's per-email limit. Register with a different email."
        ) in err
        assert "hermes agentchat recover --handle <handle> --email a@b.co" in err
        assert _cli_env == []

    def test_register_network_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        _cli_env: list[dict[str, str]],
    ) -> None:
        serve(monkeypatch, {_REGISTER: connect_error("dns")})
        rc = cli._dispatch_register(argparse.Namespace(email="a@b.co", handle="alice", display_name="Al"))
        assert rc == _EXIT_API_ERR
        assert "Registration request failed: network error: dns" in capsys.readouterr().err

    def test_recover(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        _cli_env: list[dict[str, str]],
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                _RECOVER: answer(200, {"pending_id": "pnd_r"}),
                _RECOVER_VERIFY: answer(200, {"handle": "alice", "api_key": _KEY}),
            },
        )
        rc = cli._dispatch_recover(argparse.Namespace(email="a@b.co", handle="alice"))
        assert rc == _EXIT_OK
        assert wire.bodies() == [
            {"email": "a@b.co", "handle": "alice"},
            {"pending_id": "pnd_r", "code": "123456"},
        ]
        assert _cli_env == [{"api_key": _KEY, "handle": "alice"}]

    def test_recover_rate_limited(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        _cli_env: list[dict[str, str]],
    ) -> None:
        serve(monkeypatch, {_RECOVER: api_error(429, "RATE_LIMITED", "slow down")})
        rc = cli._dispatch_recover(argparse.Namespace(email="a@b.co", handle="alice"))
        assert rc == _EXIT_API_ERR
        assert (
            "Recovery request failed: rate-limited — wait a minute and try again"
            in capsys.readouterr().err
        )
        assert _cli_env == []
