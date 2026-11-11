"""Registration-side handling of the per-email policy in ``wizard``.

An email can back several agents; the server refuses a registration with
``EMAIL_LIMIT_REACHED`` (live cap) or ``EMAIL_EXHAUSTED`` (lifetime cap),
both HTTP 409 with the cap in ``details.limit``. The caps are a tunable
server-side row, so the plugin must quote the number the server sent
rather than hard-code one, and must keep tolerating the retired
``EMAIL_TAKEN`` from servers that have not been upgraded.

``_register_start`` registers through the SDK; the fake API in
``sdk_wire`` answers at the httpx transport, so the SDK's own error
mapping is what these tests exercise, with no network IO.
"""
from __future__ import annotations

from typing import Any

import pytest
from sdk_wire import Route, answer, api_error, serve

from agentchatme_hermes.wizard import (
    _EMAIL_EXHAUSTED,
    _EMAIL_LIMIT_REACHED,
    _EMAIL_POLICY_CODES,
    _api_error_parts,
    _email_error_recovery,
    _email_policy_message,
    _handles_from_details,
    _limit_from_details,
    _register_start,
    _RegisterError,
)


def _patch_post(monkeypatch: pytest.MonkeyPatch, route: Route) -> Any:
    """Answer ``POST /v1/register`` with ``route``; returns the wire record."""
    return serve(monkeypatch, {"/v1/register": route})


# ─── _register_start: error mapping ────────────────────────────────────────


class TestRegisterStartEmailPolicy:
    def test_limit_reached_quotes_server_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_post(
            monkeypatch,
            api_error(409, "EMAIL_LIMIT_REACHED", "server wording", {"limit": 7}),
        )
        with pytest.raises(_RegisterError) as info:
            _register_start(email="a@b.co", handle="alice", display_name="")
        err = info.value
        assert err.field == "email"
        assert err.code == _EMAIL_LIMIT_REACHED
        # The number comes from details.limit, not from the server prose
        # and not from a constant in the plugin.
        assert "7 active agents" in str(err)
        assert "server wording" not in str(err)

    def test_exhausted_quotes_server_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_post(
            monkeypatch,
            api_error(409, "EMAIL_EXHAUSTED", "server wording", {"limit": 42}),
        )
        with pytest.raises(_RegisterError) as info:
            _register_start(email="a@b.co", handle="alice", display_name="")
        err = info.value
        assert err.field == "email"
        assert err.code == _EMAIL_EXHAUSTED
        assert "42" in str(err)
        assert "lifetime" in str(err)

    def test_missing_limit_falls_back_to_server_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_post(monkeypatch, api_error(409, "EMAIL_LIMIT_REACHED", "exact server text"))
        with pytest.raises(_RegisterError) as info:
            _register_start(email="a@b.co", handle="alice", display_name="")
        assert str(info.value) == "exact server text"
        assert info.value.field == "email"

    def test_legacy_email_taken_treated_as_limit_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A not-yet-upgraded server still says EMAIL_TAKEN (one live agent
        per email). Same user action as the live cap, so it maps onto
        EMAIL_LIMIT_REACHED; no details.limit → server message verbatim."""
        _patch_post(
            monkeypatch, api_error(409, "EMAIL_TAKEN", "This email already has an agent.")
        )
        with pytest.raises(_RegisterError) as info:
            _register_start(email="a@b.co", handle="alice", display_name="")
        err = info.value
        assert err.code == _EMAIL_LIMIT_REACHED
        assert err.field == "email"
        assert str(err) == "This email already has an agent."

    def test_handle_errors_still_field_scoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_post(monkeypatch, api_error(409, "HANDLE_TAKEN", "taken"))
        with pytest.raises(_RegisterError) as info:
            _register_start(email="a@b.co", handle="alice", display_name="")
        assert info.value.field == "handle"
        assert info.value.code == "HANDLE_TAKEN"

    def test_success_returns_pending_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = _patch_post(monkeypatch, answer(200, {"pending_id": "pnd_1"}))
        assert (
            _register_start(email="a@b.co", handle="alice", display_name="Al") == "pnd_1"
        )
        assert wire.bodies() == [{"email": "a@b.co", "handle": "alice", "display_name": "Al"}]

    def test_policy_code_set_is_exactly_the_two_codes(self) -> None:
        assert set(_EMAIL_POLICY_CODES) == {"EMAIL_LIMIT_REACHED", "EMAIL_EXHAUSTED"}


# ─── _email_policy_message / _limit_from_details ───────────────────────────


class TestEmailPolicyMessage:
    def test_limit_reached_with_limit(self) -> None:
        text = _email_policy_message(_EMAIL_LIMIT_REACHED, "ignored", 10)
        assert "10 active agents" in text
        assert "ignored" not in text

    def test_exhausted_with_limit(self) -> None:
        text = _email_policy_message(_EMAIL_EXHAUSTED, "ignored", 30)
        assert "30" in text
        assert "lifetime" in text

    def test_no_limit_uses_server_message(self) -> None:
        assert _email_policy_message(_EMAIL_LIMIT_REACHED, "server says", None) == "server says"

    def test_no_limit_no_message_uses_code(self) -> None:
        assert _email_policy_message(_EMAIL_EXHAUSTED, None, None) == "EMAIL_EXHAUSTED"


class TestLimitFromDetails:
    @pytest.mark.parametrize(
        ("details", "expected"),
        [
            (None, None),
            ({}, None),
            ({"limit": 10}, 10),
            ({"limit": 1}, 1),
            ({"limit": 0}, None),
            ({"limit": -3}, None),
            ({"limit": "10"}, None),
            ({"limit": 10.0}, None),
            ({"limit": True}, None),  # bool is an int subclass; must not read as 1
        ],
    )
    def test_cases(self, details: Any, expected: Any) -> None:
        assert _limit_from_details(details) == expected


class TestHandlesFromDetails:
    @pytest.mark.parametrize(
        ("details", "expected"),
        [
            (None, []),
            ("nope", []),
            ({}, []),
            ({"handles": "alice"}, []),
            ({"handles": ["alice", "bob"]}, ["alice", "bob"]),
            ({"handles": ["alice", 3, "", None, "bob"]}, ["alice", "bob"]),
        ],
    )
    def test_cases(self, details: Any, expected: list[str]) -> None:
        assert _handles_from_details(details) == expected


# ─── _api_error_parts ──────────────────────────────────────────────────────


def _sdk_error(status: int, body: dict[str, Any]) -> Any:
    """The exception the SDK raises for this error response."""
    from agentchatme.errors import create_agentchat_error

    return create_agentchat_error(body, status)


class TestApiErrorParts:
    def test_returns_code_message_and_details(self) -> None:
        code, message, details = _api_error_parts(
            _sdk_error(409, {"code": "X", "message": "m", "details": {"limit": 3}})
        )
        assert (code, message) == ("X", "m")
        assert details == {"limit": 3}

    def test_field_errors_spliced_into_message(self) -> None:
        code, message, details = _api_error_parts(
            _sdk_error(
                400,
                {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request",
                    "details": {"fieldErrors": {"handle": ["too short"], "email": ["bad"]}},
                },
            )
        )
        assert code == "VALIDATION_ERROR"
        assert message == "Invalid request (handle: too short; email: bad)"
        assert details is not None and "fieldErrors" in details

    def test_field_errors_without_a_message(self) -> None:
        _code, message, _details = _api_error_parts(
            _sdk_error(
                400,
                {"code": "VALIDATION_ERROR", "message": "", "details": {"fieldErrors": {"handle": ["x"]}}},
            )
        )
        assert message == "handle: x"

    def test_empty_or_malformed_field_errors_leave_the_message(self) -> None:
        for field_errors in ({}, {"handle": []}, {"handle": "not a list"}, ["x"]):
            _code, message, _details = _api_error_parts(
                _sdk_error(
                    400,
                    {
                        "code": "VALIDATION_ERROR",
                        "message": "Invalid request",
                        "details": {"fieldErrors": field_errors},
                    },
                )
            )
            assert message == "Invalid request", field_errors

    def test_no_details(self) -> None:
        assert _api_error_parts(_sdk_error(500, {"code": "INTERNAL_ERROR", "message": "boom"})) == (
            "INTERNAL_ERROR",
            "boom",
            None,
        )


# ─── _email_error_recovery menu positions ──────────────────────────────────


class TestEmailErrorRecoveryMenu:
    @staticmethod
    def _run(code: str, pick: int) -> tuple[str, list[str], str]:
        warnings: list[str] = []
        seen: dict[str, Any] = {}

        def prompt_choice(question: str, choices: list[str], default: int = 0, **_: Any) -> int:
            seen["question"] = question
            seen["choices"] = choices
            seen["default"] = default
            return pick

        result = _email_error_recovery(
            code=code,
            message="the message",
            prompt_choice=prompt_choice,
            print_warning=warnings.append,
        )
        assert warnings == ["the message"]
        return result, list(seen["choices"]), str(seen["question"])

    @pytest.mark.parametrize("code", [_EMAIL_LIMIT_REACHED, _EMAIL_EXHAUSTED, "SOMETHING_ELSE"])
    def test_positions(self, code: str) -> None:
        assert self._run(code, 0)[0] == "retry-email"
        assert self._run(code, 1)[0] == "paste"
        assert self._run(code, 2)[0] == "recover"
        assert self._run(code, 3)[0] == "cancel"

    def test_different_email_is_default_and_mentions_plus_alias(self) -> None:
        _result, choices, _question = self._run(_EMAIL_LIMIT_REACHED, 0)
        assert len(choices) == 4
        assert "+alias" in choices[0]
        assert "Recover" in choices[2]

    def test_questions_name_the_cap_class(self) -> None:
        assert "live-agent limit" in self._run(_EMAIL_LIMIT_REACHED, 0)[2]
        assert "no registrations left" in self._run(_EMAIL_EXHAUSTED, 0)[2]
        assert "Couldn't register" in self._run("OTHER", 0)[2]
