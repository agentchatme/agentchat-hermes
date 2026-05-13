"""Tests for ``agentchatme_hermes.tools._common``."""
from __future__ import annotations

import json
from typing import Any

import pytest

from agentchatme_hermes.tools._common import (
    ToolArgError,
    err,
    format_sdk_error,
    handle_arg_error,
    normalize_handle,
    ok,
    optional_bool,
    optional_int,
    optional_str,
    require_str,
)


class TestEnvelope:
    def test_ok_basic(self) -> None:
        result = json.loads(ok({"foo": "bar"}))
        assert result == {"ok": True, "foo": "bar"}

    def test_err_basic(self) -> None:
        result = json.loads(err("CODE_X", "Something happened"))
        assert result == {"ok": False, "error": {"code": "CODE_X", "message": "Something happened"}}

    def test_err_with_extras(self) -> None:
        result = json.loads(err("RATE_LIMITED", "Slow down", retry_after_seconds=30))
        assert result["error"]["code"] == "RATE_LIMITED"
        assert result["error"]["retry_after_seconds"] == 30


class TestNormalizeHandle:
    def test_strip_at_prefix(self) -> None:
        assert normalize_handle("@alice") == "alice"

    def test_lowercase(self) -> None:
        assert normalize_handle("Alice") == "alice"

    def test_combined(self) -> None:
        assert normalize_handle("  @Alice  ") == "alice"

    def test_with_digits(self) -> None:
        assert normalize_handle("alice42") == "alice42"

    def test_with_hyphens(self) -> None:
        assert normalize_handle("alice-bot") == "alice-bot"

    def test_must_start_with_letter(self) -> None:
        with pytest.raises(ToolArgError):
            normalize_handle("1alice")

    def test_no_underscores(self) -> None:
        with pytest.raises(ToolArgError):
            normalize_handle("alice_bot")

    def test_no_doubled_hyphens(self) -> None:
        with pytest.raises(ToolArgError):
            normalize_handle("alice--bot")

    def test_no_trailing_hyphen(self) -> None:
        with pytest.raises(ToolArgError):
            normalize_handle("alice-")

    def test_empty_string(self) -> None:
        with pytest.raises(ToolArgError):
            normalize_handle("")

    def test_non_string(self) -> None:
        with pytest.raises(ToolArgError):
            normalize_handle(42)  # type: ignore[arg-type]


class TestRequireStr:
    def test_present(self) -> None:
        assert require_str({"x": "hello"}, "x") == "hello"

    def test_missing_raises(self) -> None:
        with pytest.raises(ToolArgError, match="x is required"):
            require_str({}, "x")

    def test_wrong_type_raises(self) -> None:
        with pytest.raises(ToolArgError):
            require_str({"x": 42}, "x")

    def test_max_len_enforced(self) -> None:
        with pytest.raises(ToolArgError, match="max length"):
            require_str({"x": "abcdef"}, "x", max_len=3)

    def test_max_len_at_boundary_ok(self) -> None:
        assert require_str({"x": "abc"}, "x", max_len=3) == "abc"


class TestOptionalStr:
    def test_missing_returns_none(self) -> None:
        assert optional_str({}, "x") is None

    def test_explicit_none_returns_none(self) -> None:
        assert optional_str({"x": None}, "x") is None

    def test_present_returns_value(self) -> None:
        assert optional_str({"x": "hello"}, "x") == "hello"

    def test_wrong_type_raises(self) -> None:
        with pytest.raises(ToolArgError):
            optional_str({"x": 42}, "x")


class TestOptionalInt:
    def test_missing_returns_none(self) -> None:
        assert optional_int({}, "x") is None

    def test_basic(self) -> None:
        assert optional_int({"x": 5}, "x") == 5

    def test_bool_rejected(self) -> None:
        # bool is a subclass of int in Python — explicitly rejected
        with pytest.raises(ToolArgError):
            optional_int({"x": True}, "x")

    def test_minimum_enforced(self) -> None:
        with pytest.raises(ToolArgError, match="below the minimum"):
            optional_int({"x": 0}, "x", minimum=1)

    def test_maximum_enforced(self) -> None:
        with pytest.raises(ToolArgError, match="above the maximum"):
            optional_int({"x": 100}, "x", maximum=50)


class TestOptionalBool:
    def test_missing_returns_none(self) -> None:
        assert optional_bool({}, "x") is None

    def test_true(self) -> None:
        assert optional_bool({"x": True}, "x") is True

    def test_false(self) -> None:
        assert optional_bool({"x": False}, "x") is False

    def test_string_rejected(self) -> None:
        with pytest.raises(ToolArgError):
            optional_bool({"x": "true"}, "x")


class TestHandleArgError:
    def test_returns_validation_error_envelope(self) -> None:
        result = json.loads(handle_arg_error(ToolArgError("bad input")))
        assert result["ok"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert result["error"]["message"] == "bad input"


class TestFormatSdkError:
    """Verifies each SDK error class lands on its stable code.

    The SDK's error classes all take ``(response: Mapping, status: int)``
    — we build a minimal response dict per test so the resulting
    exception has the right code+message attributes.
    """

    def _decode(self, envelope: str) -> dict[str, Any]:
        return json.loads(envelope)["error"]

    @staticmethod
    def _resp(code: str, message: str) -> dict[str, Any]:
        return {"code": code, "message": message}

    def test_rate_limited(self) -> None:
        try:
            from agentchatme import RateLimitedError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        exc = RateLimitedError(
            self._resp("RATE_LIMITED", "slow down"),
            status=429,
            retry_after_ms=15_000,
        )
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["code"] == "RATE_LIMITED"
        assert decoded["retry_after_seconds"] == 15

    def test_rate_limited_sub_second_ceils_to_one(self) -> None:
        try:
            from agentchatme import RateLimitedError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        exc = RateLimitedError(
            self._resp("RATE_LIMITED", "slow down"),
            status=429,
            retry_after_ms=200,
        )
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["retry_after_seconds"] == 1

    def test_rate_limited_missing_retry_after_omits_field(self) -> None:
        try:
            from agentchatme import RateLimitedError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        exc = RateLimitedError(
            self._resp("RATE_LIMITED", "slow down"),
            status=429,
        )
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["code"] == "RATE_LIMITED"
        assert decoded.get("retry_after_seconds") is None

    def test_blocked(self) -> None:
        try:
            from agentchatme import BlockedError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        exc = BlockedError(self._resp("BLOCKED", "blocked"), status=403)
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["code"] == "BLOCKED"

    def test_awaiting_reply(self) -> None:
        try:
            from agentchatme import AwaitingReplyError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        exc = AwaitingReplyError(self._resp("AWAITING_REPLY", "wait"), status=409)
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["code"] == "AWAITING_REPLY"

    def test_not_found(self) -> None:
        try:
            from agentchatme import NotFoundError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        exc = NotFoundError(self._resp("NOT_FOUND", "nope"), status=404)
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["code"] == "NOT_FOUND"

    def test_unauthorized(self) -> None:
        try:
            from agentchatme import UnauthorizedError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        exc = UnauthorizedError(self._resp("UNAUTHORIZED", "bad key"), status=401)
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["code"] == "UNAUTHORIZED"

    def test_validation(self) -> None:
        try:
            from agentchatme import ValidationError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        exc = ValidationError(self._resp("VALIDATION_ERROR", "bad"), status=400)
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["code"] == "VALIDATION_ERROR"

    def test_unknown_base_error(self) -> None:
        try:
            from agentchatme import AgentChatError
        except ImportError:
            pytest.skip("agentchatme SDK not installed")
        # Generic AgentChatError (not a known subclass) falls through
        # to the AGENTCHAT_ERROR bucket.
        exc = AgentChatError(self._resp("UNKNOWN_CODE", "anything"), status=500)
        decoded = self._decode(format_sdk_error(exc))
        assert decoded["code"] == "AGENTCHAT_ERROR"
        assert "type" in decoded
