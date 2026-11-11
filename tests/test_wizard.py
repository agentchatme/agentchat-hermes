"""Tests for the pure helpers in ``agentchatme_hermes.wizard``.

The interactive surfaces (menus, prompts) require Hermes' ``prompt_choice`` /
``print_X`` helpers that only exist inside a Hermes process — we don't
unit-test those. The pure helpers (handle validation, email validation, OTP
validation, key masking, error mapping) are testable in isolation and cover
the parts most likely to regress.
"""
from __future__ import annotations

import pytest
from fake_keys import api_key, real_shaped_key

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
    def test_real_shaped_key_shows_first_8_and_last_4(self) -> None:
        key = real_shaped_key()
        assert len(key) == 46
        masked = _mask_key(key)
        assert masked == f"{key[:8]}…{key[-4:]}"
        assert masked.startswith("ac_")
        # 34 of the 43 random characters stay hidden.
        assert key[8:-4] not in masked

    def test_long_key_masked(self) -> None:
        masked = _mask_key(api_key("abcdef1234567890ghij"))
        assert masked == "ac_abcde…ghij"
        assert "1234567890" not in masked

    def test_short_value_uses_placeholder(self) -> None:
        assert _mask_key("ac_short") == "ac_…"
        assert _mask_key("") == "ac_…"

    def test_twelve_characters_are_never_shown_whole(self) -> None:
        # 8 + 4 would be the whole value.
        assert _mask_key(api_key("123456789")) == "ac_…"
        assert _mask_key(api_key("1234567890")) == "ac_12345…7890"

    def test_cli_uses_the_same_mask(self) -> None:
        from agentchatme_hermes import cli

        assert cli._mask_key is _mask_key


class TestKeyShapeText:
    def test_no_shipped_text_names_a_live_prefix(self) -> None:
        """Keys are ``ac_`` and 43 random characters; there is no
        ``ac_live_`` prefix, so no prompt, help text or doc may show one
        (the CHANGELOG keeps its history)."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        shipped = [*sorted((root / "agentchatme_hermes").rglob("*.py"))]
        shipped += [root / name for name in ("README.md", "SECURITY.md", "CONTRIBUTING.md", "RUNBOOK.md")]
        needle = "ac_" + "live"
        offenders = [
            str(path.relative_to(root))
            for path in shipped
            if needle in path.read_text(encoding="utf-8")
        ]
        assert offenders == []


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
