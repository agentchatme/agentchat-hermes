"""Tests for the pure helpers in ``agentchatme_hermes.wizard``.

The interactive surfaces (menus, prompts) require Hermes' ``prompt_choice`` /
``print_X`` helpers that only exist inside a Hermes process — we don't
unit-test those. The pure helpers (handle validation, email validation, OTP
validation, key masking, error mapping) are testable in isolation and cover
the parts most likely to regress.
"""
from __future__ import annotations

import pytest

from agentchatme_hermes.wizard import (
    _mask_key,
    _RegisterError,
    _validate_handle,
)

# ─── _validate_handle ──────────────────────────────────────────────────────


class TestValidateHandle:
    @pytest.mark.parametrize(
        "value",
        [
            "alice",
            "alice42",
            "alice-bot",
            "agent-1-two",
            "abc",  # minimum length
            "a" + "b" * 29,  # max length
        ],
    )
    def test_accepts_valid(self, value: str) -> None:
        assert _validate_handle(value) is None

    def test_empty_handle(self) -> None:
        err = _validate_handle("")
        assert err is not None
        assert "required" in err.lower()

    def test_too_short(self) -> None:
        err = _validate_handle("ab")
        assert err is not None
        assert "3-30" in err

    def test_too_long(self) -> None:
        err = _validate_handle("a" + "b" * 30)
        assert err is not None
        assert "3-30" in err

    def test_starts_with_digit(self) -> None:
        err = _validate_handle("1alice")
        assert err is not None
        assert "lowercase letter" in err.lower()

    def test_starts_with_hyphen(self) -> None:
        err = _validate_handle("-alice")
        assert err is not None

    def test_ends_with_hyphen(self) -> None:
        err = _validate_handle("alice-")
        assert err is not None
        assert "hyphen" in err.lower()

    def test_doubled_hyphen(self) -> None:
        err = _validate_handle("alice--bot")
        assert err is not None
        assert "consecutive" in err.lower()

    def test_underscore_rejected(self) -> None:
        err = _validate_handle("alice_bot")
        assert err is not None
        assert "underscore" in err.lower()

    def test_uppercase_rejected(self) -> None:
        err = _validate_handle("Alice")
        assert err is not None

    def test_dot_rejected(self) -> None:
        err = _validate_handle("alice.bot")
        assert err is not None


# ─── _mask_key ─────────────────────────────────────────────────────────────


class TestMaskKey:
    def test_long_key_masked(self) -> None:
        masked = _mask_key("ac_live_abcdef1234567890ghij")
        assert masked.startswith("ac_live_")
        assert masked.endswith("ghij")
        # Bulk of the secret is replaced by the ellipsis
        assert "abcdef1234567890" not in masked
        assert "…" in masked

    def test_short_key_uses_placeholder(self) -> None:
        # Keys under 12 chars get a non-leaking placeholder
        assert _mask_key("ac_short") == "ac_…"

    def test_exactly_12_chars(self) -> None:
        # At 12 chars we should mask normally (8 + 4)
        masked = _mask_key("ac_live_xxxx")
        assert masked == "ac_live_…xxxx"


# ─── _RegisterError ────────────────────────────────────────────────────────


class TestRegisterError:
    def test_field_and_code_carried(self) -> None:
        err = _RegisterError("nope", field="handle", code="HANDLE_TAKEN")
        assert str(err) == "nope"
        assert err.field == "handle"
        assert err.code == "HANDLE_TAKEN"

    def test_field_and_code_default_to_none(self) -> None:
        err = _RegisterError("generic failure")
        assert err.field is None
        assert err.code is None

    def test_is_exception(self) -> None:
        with pytest.raises(_RegisterError):
            raise _RegisterError("x")
