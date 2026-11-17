"""``agentchatme_hermes.signup``: how a typed line becomes an answer, the
prompts Hermes asks with, and the calls it hands the SDK helper.

The conversations themselves are ``test_wizard_signup.py`` and
``test_cli_signup.py``.
"""
from __future__ import annotations

import pytest
from agentchatme.integration.account import AccountAnswer, AccountQuestion, RunResult

from agentchatme_hermes import signup


def _question(field: str, label: str | None = "Label", handles: tuple[str, ...] | None = None) -> AccountQuestion:
    answers = {
        "code": ("value", "new_code", "cancel"),
        "new_code": ("new_code", "cancel"),
    }.get(field, ("value", "cancel"))
    return AccountQuestion(field=field, label=label, answers=answers, flow=None, handles=handles)  # type: ignore[arg-type]


class TestAnswerOf:
    @pytest.mark.parametrize("field", ["email", "handle", "code"])
    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_blank_cancels(self, field: str, text: str) -> None:
        assert signup.answer_of(_question(field), text) == AccountAnswer("cancel")

    @pytest.mark.parametrize("text", ["new", " NEW ", "New"])
    def test_new_at_the_code_prompt_asks_for_a_new_code(self, text: str) -> None:
        assert signup.answer_of(_question("code"), text) == AccountAnswer("new_code")

    def test_new_elsewhere_is_a_value(self) -> None:
        assert signup.answer_of(_question("handle"), "new") == AccountAnswer("value", "new")

    def test_a_code_is_sent_as_typed(self) -> None:
        """No local format check: the server answers a malformed code itself."""
        assert signup.answer_of(_question("code"), " 12 34 ") == AccountAnswer("value", "12 34")

    def test_a_handle_loses_its_at_and_case(self) -> None:
        assert signup.answer_of(_question("handle"), " @Alice ") == AccountAnswer("value", "alice")

    def test_an_email_is_stripped_only(self) -> None:
        assert signup.answer_of(_question("email"), " A@b.co ") == AccountAnswer("value", "A@b.co")


class TestPrompts:
    def test_code_prompt_says_how_to_cancel_and_get_a_new_code(self) -> None:
        question = _question("code", "Enter the 6-digit code")
        assert signup.prompt_text(question) == "Enter the 6-digit code (blank to cancel, new for a new code)"

    def test_field_prompts(self) -> None:
        assert signup.prompt_text(_question("email", "Email address")) == "Email address (blank to cancel)"
        assert signup.field_prompt("register", "handle") == "Choose a @handle (blank to cancel)"
        assert signup.field_prompt("recover", "handle") == "@handle of the agent to recover (blank to cancel)"

    def test_handles_line(self) -> None:
        assert signup.handles_line(_question("handle")) is None
        assert (
            signup.handles_line(_question("handle", handles=("alice", "alice-2")))
            == "Agents registered with this email: @alice, @alice-2"
        )

    def test_new_code_answer(self) -> None:
        assert signup.new_code_answer(True) == AccountAnswer("new_code")
        assert signup.new_code_answer(False) == AccountAnswer("cancel")


class TestSavedCredentials:
    def test_done_gives_the_key_and_the_server_handle(self) -> None:
        result = RunResult(outcome="done", api_key="k", handle="alice")
        assert signup.saved_credentials(result) == ("k", "alice")

    @pytest.mark.parametrize(
        "result",
        [
            RunResult(outcome="cancelled"),
            RunResult(outcome="stopped", code="OTP_FAILED", message="no"),
            RunResult(outcome="done", api_key=None, handle="alice"),
            RunResult(outcome="done", api_key="k", handle=None),
        ],
    )
    def test_anything_else_saves_nothing(self, result: RunResult) -> None:
        assert signup.saved_credentials(result) is None


class TestSdkProblem:
    def test_none_with_the_helper_installed(self) -> None:
        assert signup.sdk_problem() is None

    def test_names_the_requirement_without_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        from agentchatme_hermes.kit import KIT_REQUIREMENT

        real = importlib.import_module

        def without_account(name: str, *args: object) -> object:
            if name == "agentchatme.integration.account":
                raise ImportError(name)
            return real(name, *args)  # type: ignore[arg-type]

        monkeypatch.setattr(importlib, "import_module", without_account)
        problem = signup.sdk_problem()
        assert problem is not None
        assert KIT_REQUIREMENT in problem
        assert "pip install -U" in problem
