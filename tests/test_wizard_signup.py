"""The wizard's register, recover and paste-a-key flows, end to end.

The SDK's sign-up helper runs for real: the fake API in ``sdk_wire`` answers
at the httpx transport with the contract's answers (``signup_wire``), and a
scripted person answers Hermes' prompts. Each test checks what reached the
server, what the person read (the server's sentences, verbatim, and no
rulebook text of the wizard's own), and what was saved: the key and the
handle the server returned, only when the server answered with a key.

The user-facing bug this replaces: one wrong code used to end the whole
wizard, although the server allows several tries on the same code.
"""
from __future__ import annotations

from typing import Any

import pytest
import signup_wire as sw
from fake_keys import api_key
from key_files import saved
from sdk_wire import Wire, answer, api_error, connect_error, serve

from agentchatme_hermes import allow, credentials, signup, wizard
from agentchatme_hermes._version import __version__

_KEY = api_key("wizard_000000000000")
_CODE_PROMPT = "Enter the 6-digit code (blank to cancel, new for a new code)"
_HANDLE_PROMPT = "Choose a @handle (blank to cancel)"
_EMAIL_PROMPT = "Email address (blank to cancel)"
_DISPLAY_PROMPT = 'Display name (optional, shown next to your @handle, e.g. "Alice")'


class _Person:
    """Hermes' prompt helpers with scripted answers, and one transcript of
    everything the person saw, in order."""

    def __init__(self, answers: list[str], yes_no: list[bool] | None = None) -> None:
        self.answers = list(answers)
        self.yes_no = list(yes_no or [])
        self.transcript: list[tuple[str, str]] = []
        self.defaults: list[tuple[str, Any]] = []
        self.masked: list[str] = []

    def prompt(self, question: str, default: Any = None, password: bool = False) -> str:
        self.transcript.append(("prompt", question))
        self.defaults.append((question, default))
        if password:
            self.masked.append(question)
        value = self.answers.pop(0)
        if value == "" and default:
            return str(default)
        return value

    def prompt_yes_no(self, question: str, default: bool = True) -> bool:
        self.transcript.append(("yes_no", question))
        return self.yes_no.pop(0)

    def info(self, text: str) -> None:
        self.transcript.append(("info", text))

    def warning(self, text: str) -> None:
        self.transcript.append(("warning", text))

    def success(self, text: str) -> None:
        self.transcript.append(("success", text))

    def said(self, kind: str) -> list[str]:
        return [text for k, text in self.transcript if k == kind]


class _Wizard:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, person: _Person) -> None:
        self.person = person
        self.env: dict[str, str] = {}
        self.placed: list[str] = []
        self.waits: list[float] = []
        # The one question (allow.decide) is its own test; here: its answer,
        # and that it is asked before the key is saved.
        self.tools = allow.SetupDecision("yes", allow.ALLOWED_LINE)
        self.tools_decisions: list[Any] = []
        self.asked_with_key_saved: list[bool] = []
        monkeypatch.delenv("AGENTCHATME_API_BASE", raising=False)
        monkeypatch.setattr(wizard, "resolve_texts", lambda **_kw: None)
        monkeypatch.setattr(allow, "decide_for", self._decide)
        monkeypatch.setattr(wizard, "place_identity_and_tools", self._place)
        monkeypatch.setattr(signup, "_pause", self.waits.append)
        # A Hermes whose MCP client reads the key file (secret sources, secrets.agentchat).
        monkeypatch.setattr(credentials, "key_bridge", lambda: (True, ""))

    def save_env_value(self, key: str, value: str) -> None:
        self.env[key] = value

    def _decide(self, handle: str) -> Any:
        self.asked_with_key_saved.append(saved() is not None)
        return self.tools

    def _place(self, *, handle: str, texts: Any, api_base: str, **kw: Any) -> list[Any]:
        self.placed.append(handle)
        self.tools_decisions.append(kw.get("tools_decision"))
        return []

    @property
    def saved(self) -> tuple[str, str] | None:
        """``(key, handle)`` in this profile's key file — never the ``.env``."""
        record = saved()
        return (record.api_key, record.handle) if record is not None else None

    def _common(self) -> dict[str, Any]:
        p = self.person
        return {
            "prompt": p.prompt,
            "prompt_yes_no": p.prompt_yes_no,
            "print_info": p.info,
            "print_success": p.success,
            "print_warning": p.warning,
            "save_env_value": self.save_env_value,
        }

    def register(self) -> bool:
        return wizard._register_new_agent_flow(**self._common())

    def recover(self, existing_handle: str = "") -> bool:
        return wizard._recover_lost_key_flow(existing_handle=existing_handle, **self._common())

    def paste(self) -> bool:
        p = self.person
        return wizard._paste_existing_key_flow(
            p.prompt, p.info, p.success, p.warning, self.save_env_value
        )


def _codes(wire: Wire) -> list[str]:
    return [body["code"] for body in wire.bodies() if body and "code" in body]


def _no_rulebook_text(person: _Person, wire: Wire) -> None:
    """Every line the wizard printed that is not the server's own sentence
    names no minutes, tries, limits or code format."""
    server = set(wire.messages())
    own = [
        text
        for kind, text in person.transcript
        if kind != "prompt" and text not in server and not text.startswith("Waiting ")
    ]
    own += [text for kind, text in person.transcript if kind == "prompt"]
    for text in own:
        for word in ("minute", "second", "attempt", "tries", "limit", "~60", "valid ~", "digits"):
            assert word not in text.lower(), (word, text)


# ─── register ──────────────────────────────────────────────────────────────


class TestRegister:
    def test_a_wrong_code_then_the_right_code_registers_in_one_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The reported bug: a wrong code ended the wizard. Now the server's
        sentence is shown and the code is asked for again."""
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: sw.code_sent(),
                sw.REGISTER_VERIFY: [sw.wrong_code(attempts=1), sw.registered("alice", _KEY)],
            },
        )
        person = _Person(["a@b.co", "alice", "", "111111", "123456"])
        run = _Wizard(monkeypatch, person)

        assert run.register() is True

        assert wire.calls() == [
            ("POST", sw.REGISTER),
            ("POST", sw.REGISTER_VERIFY),
            ("POST", sw.REGISTER_VERIFY),
        ]
        assert wire.bodies() == [
            {"email": "a@b.co", "handle": "alice"},
            {"pending_id": "pnd_1", "code": "111111"},
            {"pending_id": "pnd_1", "code": "123456"},
        ]
        assert person.transcript[-5:] == [
            ("info", sw.sent_text("register")),
            ("prompt", _CODE_PROMPT),
            ("info", sw.WRONG_CODE),
            ("prompt", _CODE_PROMPT),
            ("info", "Email verified. Logged in as @alice."),
        ]
        assert "Registered as @alice (key ac_wizar…0000)" in capsys.readouterr().out
        assert person.said("warning") == []
        assert person.masked == []
        assert run.env == {}, "nothing written to the profile .env"
        assert run.saved == (_KEY, "alice")
        assert run.placed == ["alice"]
        assert wire.unexpected == []
        _no_rulebook_text(person, wire)

    def test_the_prompts_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(
            monkeypatch,
            {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice", _KEY)},
        )
        person = _Person(["a@b.co", "alice", "Alice", "123456"])
        _Wizard(monkeypatch, person).register()
        assert person.said("prompt") == [_EMAIL_PROMPT, _HANDLE_PROMPT, _DISPLAY_PROMPT, _CODE_PROMPT]

    def test_the_handle_the_server_returned_is_saved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(
            monkeypatch,
            {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice-x", _KEY)},
        )
        run = _Wizard(monkeypatch, _Person(["a@b.co", "@Alice", "", "123456"]))
        assert run.register() is True
        assert run.saved is not None and run.saved[1] == "alice-x"
        assert run.env == {}
        assert run.placed == ["alice-x"]

    def test_the_request_carries_hermes_and_the_display_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = serve(
            monkeypatch,
            {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice", _KEY)},
        )
        person = _Person(["a@b.co", "@Alice", "Alice", "123456"])
        run = _Wizard(monkeypatch, person)
        monkeypatch.setenv("AGENTCHATME_API_BASE", "https://api.test/")
        assert run.register() is True
        first = wire.requests[0]
        assert str(first.url) == "https://api.test/v1/register"
        assert first.headers["X-AgentChat-Client"] == "hermes"
        assert first.headers["X-AgentChat-Client-Version"] == __version__
        assert "authorization" not in first.headers
        assert wire.bodies()[0] == {"email": "a@b.co", "handle": "alice", "display_name": "Alice"}

    def test_new_at_the_code_prompt_sends_a_new_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.code_sent(pending_id="pnd_1"), sw.code_sent(pending_id="pnd_2")],
                sw.REGISTER_VERIFY: sw.registered("alice", _KEY),
            },
        )
        run = _Wizard(monkeypatch, _Person(["a@b.co", "alice", "", "new", "123456"]))
        assert run.register() is True
        assert wire.calls() == [
            ("POST", sw.REGISTER),
            ("POST", sw.REGISTER),
            ("POST", sw.REGISTER_VERIFY),
        ]
        assert wire.bodies()[1] == {"email": "a@b.co", "handle": "alice"}
        assert wire.bodies()[2] == {"pending_id": "pnd_2", "code": "123456"}

    def test_a_new_code_waits_out_what_the_server_said(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.code_sent(resend_after_s=30), sw.code_sent(pending_id="pnd_2")],
                sw.REGISTER_VERIFY: sw.registered("alice", _KEY),
            },
        )
        person = _Person(["a@b.co", "alice", "", "new", "123456"])
        run = _Wizard(monkeypatch, person)
        assert run.register() is True
        assert len(run.waits) == 1
        assert 29 < run.waits[0] <= 30
        assert ("info", "Waiting 30 seconds…") in person.transcript

    def test_blank_at_the_code_prompt_cancels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(monkeypatch, {sw.REGISTER: sw.code_sent()})
        person = _Person(["a@b.co", "alice", "", ""])
        run = _Wizard(monkeypatch, person)
        assert run.register() is False
        assert wire.calls() == [("POST", sw.REGISTER)]
        assert person.said("info")[-1] == "Cancelled. Nothing was saved."
        assert run.env == {}
        assert run.saved is None, "nothing saved in the key file either"

    def test_too_many_wrong_codes_offers_a_new_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.code_sent(pending_id="pnd_1"), sw.code_sent(pending_id="pnd_2")],
                sw.REGISTER_VERIFY: [sw.attempts_exhausted(), sw.registered("alice", _KEY)],
            },
        )
        person = _Person(["a@b.co", "alice", "", "000000", "123456"], yes_no=[True])
        run = _Wizard(monkeypatch, person)
        assert run.register() is True
        assert ("info", sw.TOO_MANY_WRONG) in person.transcript
        assert ("yes_no", "Send a new code?") in person.transcript
        assert _codes(wire) == ["000000", "123456"]
        assert wire.bodies()[-1]["pending_id"] == "pnd_2"

    def test_no_to_a_new_code_cancels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.expired()},
        )
        person = _Person(["a@b.co", "alice", "", "123456"], yes_no=[False])
        run = _Wizard(monkeypatch, person)
        assert run.register() is False
        assert ("info", sw.EXPIRED_CODE) in person.transcript
        assert wire.calls() == [("POST", sw.REGISTER), ("POST", sw.REGISTER_VERIFY)]
        assert run.env == {}
        assert run.saved is None, "nothing saved in the key file either"

    def test_a_taken_handle_asks_for_another_handle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.handle_taken("alice"), sw.code_sent()],
                sw.REGISTER_VERIFY: sw.registered("alice-2", _KEY),
            },
        )
        person = _Person(["a@b.co", "alice", "", "alice-2", "123456"])
        run = _Wizard(monkeypatch, person)
        assert run.register() is True
        assert [body.get("handle") for body in wire.bodies()[:2]] == ["alice", "alice-2"]
        taken = person.transcript.index(("info", "The handle @alice is taken. Try another."))
        assert person.transcript[taken + 1] == ("prompt", _HANDLE_PROMPT)
        assert run.saved is not None and run.saved[1] == "alice-2"
        assert run.env == {}

    def test_the_email_limit_asks_for_another_email(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.email_limit(10), sw.code_sent(email="a+hermes@b.co")],
                sw.REGISTER_VERIFY: sw.registered("alice", _KEY),
            },
        )
        person = _Person(["a@b.co", "alice", "", "a+hermes@b.co", "123456"])
        run = _Wizard(monkeypatch, person)
        assert run.register() is True
        assert [body.get("email") for body in wire.bodies()[:2]] == ["a@b.co", "a+hermes@b.co"]
        limit = (
            "This email address already has 10 active agents, the most allowed. "
            "Use a different email address."
        )
        at = person.transcript.index(("info", limit))
        assert person.transcript[at + 1] == ("prompt", _EMAIL_PROMPT)
        _no_rulebook_text(person, wire)

    def test_a_server_stop_shows_its_sentence_and_saves_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = serve(monkeypatch, {sw.REGISTER: sw.not_sent()})
        person = _Person(["a@b.co", "alice", ""])
        run = _Wizard(monkeypatch, person)
        assert run.register() is False
        assert wire.calls() == [("POST", sw.REGISTER)]
        assert person.said("info")[-1] == sw.NOT_SENT
        assert person.said("warning") == ["Nothing was saved."]
        assert run.env == {}
        assert run.saved is None, "nothing saved in the key file either"

    def test_a_short_wait_is_sat_out_then_the_call_repeated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: [sw.cooldown(seconds=5), sw.code_sent()],
                sw.REGISTER_VERIFY: sw.registered("alice", _KEY),
            },
        )
        person = _Person(["a@b.co", "alice", "", "123456"])
        run = _Wizard(monkeypatch, person)
        assert run.register() is True
        assert run.waits == [5.0]
        said = person.transcript.index(("info", "You can request a new code in 5 seconds."))
        assert person.transcript[said + 1] == ("info", "Waiting 5 seconds…")
        assert wire.calls()[:2] == [("POST", sw.REGISTER), ("POST", sw.REGISTER)]

    def test_unreachable_saves_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.REGISTER: connect_error("dns")})
        person = _Person(["a@b.co", "alice", ""])
        run = _Wizard(monkeypatch, person)
        assert run.register() is False
        assert person.said("warning") == ["Could not reach AgentChat (dns). Nothing was saved."]
        assert run.env == {}
        assert run.saved is None, "nothing saved in the key file either"

    def test_blank_email_cancels_before_any_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(monkeypatch, {})
        person = _Person([""])
        assert _Wizard(monkeypatch, person).register() is False
        assert wire.requests == []
        assert person.said("info")[-1] == "Cancelled. Nothing was saved."

    def test_a_server_without_the_flow_object_still_allows_a_second_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An older server answers with codes only; the helper reads them
        into the same steps."""
        wire = serve(
            monkeypatch,
            {
                sw.REGISTER: answer(200, {"pending_id": "pnd_1"}),
                sw.REGISTER_VERIFY: [
                    api_error(400, "INVALID_CODE", "Invalid or expired code"),
                    answer(200, {"api_key": _KEY, "agent": {"handle": "alice"}}),
                ],
            },
        )
        person = _Person(["a@b.co", "alice", "", "111111", "123456"])
        run = _Wizard(monkeypatch, person)
        assert run.register() is True
        assert _codes(wire) == ["111111", "123456"]
        assert ("info", "Invalid or expired code") in person.transcript
        assert run.env == {}, "nothing written to the profile .env"
        assert run.saved == (_KEY, "alice")

    def test_an_sdk_without_the_helper_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signup, "sdk_problem", lambda: "Sign-up needs agentchatme>=1.5.0.")
        wire = serve(monkeypatch, {})
        person = _Person(["a@b.co", "alice", ""])
        assert _Wizard(monkeypatch, person).register() is False
        assert person.said("warning") == ["Sign-up needs agentchatme>=1.5.0."]
        assert wire.requests == []


# ─── recover ───────────────────────────────────────────────────────────────


class TestRecover:
    def test_recover_sends_the_handle_and_saves_the_server_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.RECOVER: sw.code_sent("recover"),
                sw.RECOVER_VERIFY: [sw.wrong_code("recover"), sw.recovered("alice", _KEY)],
            },
        )
        person = _Person(["a@b.co", "@Alice", "111111", "123456"])
        run = _Wizard(monkeypatch, person)
        assert run.recover() is True
        assert wire.bodies() == [
            {"email": "a@b.co", "handle": "alice"},
            {"pending_id": "pnd_1", "code": "111111"},
            {"pending_id": "pnd_1", "code": "123456"},
        ]
        assert person.said("prompt")[:2] == [
            "Email address the agent was registered with (blank to cancel)",
            "@handle of the agent to recover (blank to cancel)",
        ]
        assert ("info", sw.sent_text("recover")) in person.transcript
        assert run.env == {}, "nothing written to the profile .env"
        assert run.saved == (_KEY, "alice")
        _no_rulebook_text(person, wire)

    def test_the_configured_handle_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(
            monkeypatch,
            {sw.RECOVER: sw.code_sent("recover"), sw.RECOVER_VERIFY: sw.recovered("alice", _KEY)},
        )
        person = _Person(["a@b.co", "", "123456"])
        assert _Wizard(monkeypatch, person).recover(existing_handle="alice") is True
        assert person.defaults[1] == ("@handle of the agent to recover (blank to cancel)", "alice")
        assert wire.bodies()[0] == {"email": "a@b.co", "handle": "alice"}

    def test_several_agents_on_the_email_lists_them_and_asks_which(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = serve(
            monkeypatch,
            {
                sw.RECOVER: [
                    sw.code_sent("recover", "pnd_1"),
                    sw.code_sent("recover", "pnd_2", handle="alice-2"),
                ],
                sw.RECOVER_VERIFY: [
                    sw.handle_required(["alice", "alice-2"]),
                    sw.recovered("alice-2", _KEY),
                ],
            },
        )
        person = _Person(["a@b.co", "alice", "123456", "alice-2", "654321"])
        run = _Wizard(monkeypatch, person)
        assert run.recover() is True
        assert wire.calls() == [
            ("POST", sw.RECOVER),
            ("POST", sw.RECOVER_VERIFY),
            ("POST", sw.RECOVER),
            ("POST", sw.RECOVER_VERIFY),
        ]
        assert wire.bodies()[2] == {"email": "a@b.co", "handle": "alice-2"}
        at = person.transcript.index(
            ("info", "More than one agent uses this email address. Choose the handle to recover.")
        )
        assert person.transcript[at + 1 : at + 3] == [
            ("info", "Agents registered with this email: @alice, @alice-2"),
            ("prompt", "@handle of the agent to recover (blank to cancel)"),
        ]
        assert run.saved is not None and run.saved[1] == "alice-2"
        assert run.env == {}

    def test_blank_handle_cancels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(monkeypatch, {})
        person = _Person(["a@b.co", ""])
        assert _Wizard(monkeypatch, person).recover() is False
        assert wire.requests == []


# ─── paste a key ───────────────────────────────────────────────────────────


class TestPasteKey:
    def test_a_key_the_server_rejects_is_not_saved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(monkeypatch, {sw.ME: api_error(401, "UNAUTHORIZED", "Invalid API key")})
        person = _Person([_KEY])
        run = _Wizard(monkeypatch, person)
        assert run.paste() is False
        assert wire.calls() == [("GET", sw.ME)]
        assert person.said("warning") == ["The key was not saved. AgentChat answered: Invalid API key"]
        assert run.env == {}
        assert run.saved is None, "nothing saved in the key file either"
        assert run.placed == []

    def test_an_accepted_key_is_saved_with_the_server_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wire = serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice", "status": "active"})})
        run = _Wizard(monkeypatch, _Person([_KEY]))
        assert run.paste() is True
        assert wire.requests[0].headers["authorization"] == f"Bearer {_KEY}"
        assert wire.requests[0].headers["X-AgentChat-Client"] == "hermes"
        assert run.env == {}, "nothing written to the profile .env"
        assert run.saved == (_KEY, "alice")
        assert run.placed == ["alice"]

    def test_unreachable_saves_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.ME: connect_error("dns")})
        person = _Person([_KEY])
        run = _Wizard(monkeypatch, person)
        assert run.paste() is False
        assert person.said("warning") == ["Could not reach AgentChat (dns). The key was not saved."]
        assert run.env == {}
        assert run.saved is None, "nothing saved in the key file either"

    def test_a_server_error_saves_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.ME: api_error(503, "INTERNAL_ERROR", "Service unavailable")})
        run = _Wizard(monkeypatch, _Person([_KEY]))
        assert run.paste() is False
        assert run.env == {}
        assert run.saved is None, "nothing saved in the key file either"

    def test_the_key_is_typed_masked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.ME: answer(200, {"handle": "alice"})})
        person = _Person([_KEY])
        assert _Wizard(monkeypatch, person).paste() is True
        assert person.masked == ["API key (ac_…)"]

    def test_no_key_entered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = serve(monkeypatch, {})
        person = _Person([""])
        assert _Wizard(monkeypatch, person).paste() is False
        assert wire.requests == []
        assert person.said("warning") == ["No key entered — skipping AgentChat setup."]


# ─── menu routing ──────────────────────────────────────────────────────────


class _Menus:
    """Drives ``_fresh_setup_menu`` / ``_replace_key_branch`` with a fixed
    picker index and records which flow ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, pick: int) -> None:
        self.pick = pick
        self.ran: list[tuple[str, dict[str, Any]]] = []
        self.person = _Person([])
        self.choices: list[str] = []

        def recover(**kwargs: Any) -> bool:
            self.ran.append(("recover", kwargs))
            return True

        def register(**kwargs: Any) -> bool:
            self.ran.append(("register", kwargs))
            return True

        def paste(*args: Any) -> bool:
            self.ran.append(("paste", {}))
            return True

        monkeypatch.setattr(wizard, "_recover_lost_key_flow", recover)
        monkeypatch.setattr(wizard, "_register_new_agent_flow", register)
        monkeypatch.setattr(wizard, "_paste_existing_key_flow", paste)

    def prompt_choice(self, question: str, choices: list[str], default: int = 0, **_: Any) -> int:
        self.choices = list(choices)
        return self.pick

    def kwargs(self) -> dict[str, Any]:
        p = self.person
        return {
            "prompt": p.prompt,
            "prompt_yes_no": p.prompt_yes_no,
            "prompt_choice": self.prompt_choice,
            "print_info": p.info,
            "print_success": p.success,
            "print_warning": p.warning,
            "save_env_value": lambda _k, _v: None,
            "get_env_value": lambda _k: "",
        }


class TestMenuRouting:
    def test_fresh_menu_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        menus = _Menus(monkeypatch, pick=2)
        wizard._fresh_setup_menu(**menus.kwargs())
        assert [name for name, _ in menus.ran] == ["recover"]
        assert menus.choices == [
            "Register a new AgentChat agent (email + verification code)",
            "I already have an API key (paste ac_…)",
            "Recover a lost API key (email + @handle + verification code)",
            "Skip for now",
        ]
        assert menus.person.said("success") == ["AgentChat ready"]
        # A profile's first key, no gateway serving it: no restart line (lab R3, L1-F02).
        assert menus.person.said("info") == [credentials.NO_RESTART_LINE]
        assert not any("restart" in text.lower() and "no restart" not in text.lower() for _, text in menus.person.transcript)

    def test_fresh_menu_skip_runs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        menus = _Menus(monkeypatch, pick=3)
        wizard._fresh_setup_menu(**menus.kwargs())
        assert menus.ran == []

    def test_fresh_menu_register_and_paste_positions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for pick, expected in ((0, "register"), (1, "paste")):
            menus = _Menus(monkeypatch, pick=pick)
            wizard._fresh_setup_menu(**menus.kwargs())
            assert [name for name, _ in menus.ran] == [expected]

    def test_replace_branch_recover_passes_existing_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        menus = _Menus(monkeypatch, pick=2)
        wizard._replace_key_branch(existing_key=api_key("old"), existing_handle="alice", **menus.kwargs())
        name, kwargs = menus.ran[0]
        assert name == "recover"
        assert kwargs["existing_handle"] == "alice"
        assert "rotates the key" in menus.choices[2]

    def test_replace_branch_cancel_is_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        menus = _Menus(monkeypatch, pick=3)
        wizard._replace_key_branch(existing_key=api_key("old"), existing_handle="alice", **menus.kwargs())
        assert menus.ran == []
        assert menus.choices[3].startswith("Cancel")
        assert menus.person.said("info")[-1:] != [credentials.RESTART_HINT], "nothing replaced"

    @pytest.mark.parametrize(("saved_key", "said"), [("new", "restart"), ("old", "none")], ids=["another-key", "the-same-key"])
    def test_replace_branch_says_restart_only_for_another_key_while_a_gateway_serves(
        self, monkeypatch: pytest.MonkeyPatch, saved_key: str, said: str
    ) -> None:
        from types import SimpleNamespace

        menus = _Menus(monkeypatch, pick=0)
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        monkeypatch.setattr(wizard, "gateway_serves_profile", lambda home, *, api_base: True)
        monkeypatch.setattr(wizard, "resolve_key", lambda: SimpleNamespace(api_key=api_key(saved_key)))
        wizard._replace_key_branch(existing_key=api_key("old"), existing_handle="alice", **menus.kwargs())
        expected = credentials.RESTART_HINT if said == "restart" else credentials.NO_RESTART_LINE
        assert menus.person.said("info")[-1] == expected


class TestToolsQuestion:
    """The one question comes after the server answered and before the key is
    saved; the answer reaches the tools' entry; its line is shown."""

    def test_register_asks_before_saving_and_places_the_answer(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        serve(monkeypatch, {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice", _KEY)})
        run = _Wizard(monkeypatch, _Person(["a@b.co", "alice", "", "123456"]))
        assert run.register() is True
        assert run.asked_with_key_saved == [False]
        assert run.tools_decisions == ["yes"]
        assert "✓ AgentChat tools allowed" in capsys.readouterr().out

    def test_a_no_is_placed_and_said(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.REGISTER: sw.code_sent(), sw.REGISTER_VERIFY: sw.registered("alice", _KEY)})
        person = _Person(["a@b.co", "alice", "", "123456"])
        run = _Wizard(monkeypatch, person)
        run.tools = allow.SetupDecision("no", allow.NOT_ALLOWED_LINE.format(who="@alice"))
        assert run.register() is True
        assert run.tools_decisions == ["no"]
        assert person.said("info")[-1].startswith("AgentChat tools not allowed. @alice")

    def test_nobody_asked_places_no_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        serve(monkeypatch, {sw.RECOVER: sw.code_sent("recover"), sw.RECOVER_VERIFY: sw.recovered("alice", _KEY)})
        person = _Person(["a@b.co", "alice", "123456"])
        run = _Wizard(monkeypatch, person)
        run.tools = allow.SetupDecision(None, allow.AGENT_TERMINAL_LINE.format(who="@alice"))
        assert run.recover() is True
        assert run.asked_with_key_saved == [False]
        assert run.tools_decisions == [None]
        assert "/agentchat allow" in person.said("info")[-1]
