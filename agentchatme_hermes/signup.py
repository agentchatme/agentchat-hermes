"""Sign-up for the wizard and the subcommands: register, recover, check a key.

The rules are the server's. ``agentchatme.integration.account`` follows the
server's answer (contract v1, "Sign-up flows"): which call comes next, which
input to ask for again, how long to wait, when a new code may be sent. Every
sentence the person reads about a code, a handle or an email address is the
server's ``message``, shown as it is. Hermes keeps no attempt count, limit,
code format or timing of its own.

What is Hermes's, and lives here: the labels it asks with, how a typed line
becomes an answer (blank cancels; ``new`` at the code prompt asks for a new
code), the line that lists the handles an email backs, the line shown while
a wait the server named is sat out, and the client identity and API base
every call carries. The wizard (:mod:`.wizard`) and the
subcommands (:mod:`.cli`) read lines their own way and share the rest.

The SDK is imported inside the functions: ``hermes agentchat doctor`` must
still load, and say so, when it is missing.
"""
from __future__ import annotations

import importlib
import math
import time
from typing import TYPE_CHECKING, Any, Callable, Literal

from .client_identity import hermes_client_identity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentchatme.integration.account import (
        AccountAnswer,
        AccountQuestion,
        CheckKeyResult,
        RunResult,
    )

Kind = Literal["register", "recover"]

LABELS: dict[str, dict[str, str]] = {
    "register": {"email": "Email address", "handle": "Choose a @handle"},
    "recover": {
        "email": "Email address the agent was registered with",
        "handle": "@handle of the agent to recover",
    },
}
"""The field labels per flow. The code's label is the SDK's
(``account.DEFAULT_LABELS["code"]``)."""

FIELD_HINT = "blank to cancel"
CODE_HINT = "blank to cancel, new for a new code"
NEW_CODE_WORD = "new"
NEW_CODE_QUESTION = "Send a new code?"


def sdk_problem() -> str | None:
    """``None`` when the SDK's sign-up helper imports; otherwise what to tell
    the person (it shipped in the release this package names as its floor)."""
    from .kit import KIT_REQUIREMENT, installed_sdk_version, sdk_update_sentence

    try:
        importlib.import_module("agentchatme.integration.account")
    except ImportError:
        return (
            f"Sign-up needs {KIT_REQUIREMENT} (installed agentchatme: "
            f"{installed_sdk_version()}). {sdk_update_sentence('run this again')}"
        )
    return None


def prompt_text(question: AccountQuestion) -> str:
    """The prompt for a field or code question: its label and how to cancel
    (and, at the code prompt, how to ask for a new code)."""
    hint = CODE_HINT if question.field == "code" else FIELD_HINT
    return f"{question.label} ({hint})"


def field_prompt(kind: Kind, field: Literal["email", "handle"]) -> str:
    """The prompt for ``field`` before any call (the wizard asks the fields
    itself first, to keep its order and offer what it already knows)."""
    return f"{LABELS[kind][field]} ({FIELD_HINT})"


def handles_line(question: AccountQuestion) -> str | None:
    """After ``HANDLE_REQUIRED``: the handles the email backs, to choose from."""
    return handles_text(question.handles)


def handles_text(handles: tuple[str, ...] | None) -> str | None:
    """The line listing the handles an email backs, or ``None`` without any."""
    if not handles:
        return None
    return "Agents registered with this email: " + ", ".join(f"@{h}" for h in handles)


def typed_value(field: str, text: str) -> str:
    """A typed value as it is sent: stripped; a handle without the ``@`` the
    prompts show, in lower case (people type handles the way they see them)."""
    value = text.strip()
    if field == "handle":
        value = value.lstrip("@").lower()
    return value


def answer_of(question: AccountQuestion, text: str) -> AccountAnswer:
    """What a typed line means for ``question`` (an email, handle or code
    question): blank cancels, ``new`` at the code prompt asks for a new
    code, anything else is the value."""
    from agentchatme.integration.account import AccountAnswer

    value = typed_value(question.field, text)
    if not value:
        return AccountAnswer("cancel")
    if question.field == "code" and value.lower() == NEW_CODE_WORD:
        return AccountAnswer("new_code")
    return AccountAnswer("value", value)


def new_code_answer(yes: bool) -> AccountAnswer:
    """The answer to "Send a new code?"."""
    from agentchatme.integration.account import AccountAnswer

    return AccountAnswer("new_code" if yes else "cancel")


def waiting_line(seconds: float) -> str:
    """What the person reads while the helper sits out a wait the server
    named (a short ``retry_after_s``, or what is left of ``resend_after_s``
    before a new code may be sent)."""
    whole = max(1, math.ceil(seconds))
    return f"Waiting {whole} second{'' if whole == 1 else 's'}…"


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def run(
    kind: Kind,
    *,
    ask: Callable[[AccountQuestion], AccountAnswer],
    show: Callable[[str], Any],
    input: Mapping[str, Any],
    api_base: str,
) -> RunResult:
    """The whole sign-up conversation for ``kind``, on the SDK helper.

    ``input`` holds what is known up front (email, handle, display name); the
    helper asks only for what is missing or what the server names. ``show``
    gets the server's sentences, and a line before each wait the helper sits
    out. Raises ``agentchatme.ConnectionError`` when the server cannot be
    reached."""
    from agentchatme.integration import account

    def sleep(seconds: float) -> None:
        show(waiting_line(seconds))
        _pause(seconds)

    return account.run(
        kind=kind,
        ask=ask,
        show=show,
        input=input,
        labels=LABELS[kind],
        sleep=sleep,
        client_identity=hermes_client_identity(),
        api_base=api_base,
    )


def check_key(api_key: str, *, api_base: str) -> CheckKeyResult:
    """Does the server accept ``api_key``? ``ok`` with the handle it belongs
    to, or not ``ok`` with the server's code and message. Raises
    ``agentchatme.ConnectionError`` when no usable answer came."""
    from agentchatme.integration import account

    return account.check_key(api_key, client_identity=hermes_client_identity(), api_base=api_base)


def saved_credentials(result: RunResult) -> tuple[str, str] | None:
    """``(api_key, handle)`` of a run that ended ``done``, else ``None``. The
    handle is the one the server returned, never the one typed."""
    if result.outcome != "done" or not result.api_key or not result.handle:
        return None
    return result.api_key, result.handle
