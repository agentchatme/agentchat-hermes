"""Registration-side handling of the per-email policy in ``wizard``.

An email can back several agents; the server refuses a registration with
``EMAIL_LIMIT_REACHED`` (live cap) or ``EMAIL_EXHAUSTED`` (lifetime cap),
both HTTP 409 with the cap in ``details.limit``. The caps are a tunable
server-side row, so the plugin must quote the number the server sent
rather than hard-code one, and must keep tolerating the retired
``EMAIL_TAKEN`` from servers that have not been upgraded.

``_register_start`` posts with ``httpx`` directly; ``httpx.post`` is
patched so no network IO happens.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from agentchatme_hermes.wizard import (
    _EMAIL_EXHAUSTED,
    _EMAIL_LIMIT_REACHED,
    _EMAIL_POLICY_CODES,
    _email_error_recovery,
    _email_policy_message,
    _handles_from_details,
    _limit_from_details,
    _parse_error_response,
    _register_start,
    _RegisterError,
)


class _FakeResponse:
    def __init__(
        self, status_code: int, payload: Any = None, *, invalid_json: bool = False
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._invalid_json = invalid_json

    def json(self) -> Any:
        if self._invalid_json:
            raise ValueError("not json")
        return self._payload


def _patch_post(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> list[dict[str, Any]]:
    """Replace ``httpx.post`` with a stub returning ``response``; records calls."""
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


# ─── _register_start: error mapping ────────────────────────────────────────


class TestRegisterStartEmailPolicy:
    def test_limit_reached_quotes_server_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_post(
            monkeypatch,
            _FakeResponse(
                409,
                {
                    "code": "EMAIL_LIMIT_REACHED",
                    "message": "server wording",
                    "details": {"limit": 7},
                },
            ),
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
            _FakeResponse(
                409,
                {
                    "code": "EMAIL_EXHAUSTED",
                    "message": "server wording",
                    "details": {"limit": 42},
                },
            ),
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
        _patch_post(
            monkeypatch,
            _FakeResponse(
                409, {"code": "EMAIL_LIMIT_REACHED", "message": "exact server text"}
            ),
        )
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
            monkeypatch,
            _FakeResponse(
                409, {"code": "EMAIL_TAKEN", "message": "This email already has an agent."}
            ),
        )
        with pytest.raises(_RegisterError) as info:
            _register_start(email="a@b.co", handle="alice", display_name="")
        err = info.value
        assert err.code == _EMAIL_LIMIT_REACHED
        assert err.field == "email"
        assert str(err) == "This email already has an agent."

    def test_handle_errors_still_field_scoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_post(
            monkeypatch,
            _FakeResponse(409, {"code": "HANDLE_TAKEN", "message": "taken"}),
        )
        with pytest.raises(_RegisterError) as info:
            _register_start(email="a@b.co", handle="alice", display_name="")
        assert info.value.field == "handle"
        assert info.value.code == "HANDLE_TAKEN"

    def test_success_returns_pending_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"pending_id": "pnd_1"}))
        assert (
            _register_start(email="a@b.co", handle="alice", display_name="Al") == "pnd_1"
        )
        assert calls[0]["json"] == {"email": "a@b.co", "handle": "alice", "display_name": "Al"}

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


# ─── _parse_error_response ─────────────────────────────────────────────────


class TestParseErrorResponse:
    def test_returns_code_message_and_details(self) -> None:
        code, message, details = _parse_error_response(
            _FakeResponse(409, {"code": "X", "message": "m", "details": {"limit": 3}})
        )
        assert (code, message) == ("X", "m")
        assert details == {"limit": 3}

    def test_field_errors_spliced_into_message(self) -> None:
        code, message, details = _parse_error_response(
            _FakeResponse(
                400,
                {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request",
                    "details": {"fieldErrors": {"handle": ["too short"]}},
                },
            )
        )
        assert code == "VALIDATION_ERROR"
        assert message == "Invalid request (handle: too short)"
        assert details is not None and "fieldErrors" in details

    def test_invalid_json(self) -> None:
        assert _parse_error_response(_FakeResponse(500, invalid_json=True)) == (None, None, None)

    def test_non_dict_body(self) -> None:
        assert _parse_error_response(_FakeResponse(500, ["not", "a", "dict"])) == (
            None,
            None,
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
