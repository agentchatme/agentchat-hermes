"""Unit tests for the wizard's input validation.

These tests do not touch the Hermes runtime or the AgentChat server — they
exercise the pure-Python validators that gate user input before any HTTP
roundtrip. The CLI flow itself is integration-tested via the live smoke
suite (gated on AGENTCHATME_LIVE_API_KEY).
"""

from __future__ import annotations

import pytest

from agentchatme_hermes.setup import (
    _EMAIL_PATTERN,
    _HANDLE_PATTERN,
    _OTP_PATTERN,
    _mask_key,
    _validate_handle,
)


# ─── Handle shape ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "alice",
        "alice-bot",
        "a1b",
        "my-agent-007",
        "a-b-c",
        "abc",  # 3-char min
        "a" + "b" * 29,  # 30-char max
    ],
)
def test_validate_handle_accepts_valid_handles(value):
    assert _validate_handle(value) is None


@pytest.mark.parametrize(
    "value, hint",
    [
        ("", "required"),
        ("ab", "Length"),  # too short
        ("a" * 31, "Length"),  # too long
        ("1abc", "lowercase letter"),  # leading digit
        ("-abc", "lowercase letter"),  # leading hyphen
        ("Abc", "lowercase letters, digits, and hyphens"),  # uppercase
        ("a_b", "lowercase letters, digits, and hyphens"),  # underscore
        ("a.b", "lowercase letters, digits, and hyphens"),  # dot
        ("a--b", "consecutive hyphens"),
        ("alice-", "Cannot end with a hyphen"),
    ],
)
def test_validate_handle_rejects_with_hint(value, hint):
    err = _validate_handle(value)
    assert err is not None
    assert hint.lower() in err.lower(), f"expected hint {hint!r} in {err!r}"


def test_handle_pattern_matches_canonical_regex():
    """The client-side regex must agree with the server (packages/shared/src/validation/handles.ts)."""
    canonical = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
    assert _HANDLE_PATTERN.pattern == canonical


# ─── Email shape ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "you@example.com",
        "alice.bob@example.com",
        "agent+test@subdomain.example.co",
        "x@y.z",
    ],
)
def test_email_pattern_accepts_valid(value):
    assert _EMAIL_PATTERN.match(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no-at-sign",
        "@no-local.com",
        "trailing@",
        "two@@signs.com",
        "spaces in@email.com",
    ],
)
def test_email_pattern_rejects_invalid(value):
    assert _EMAIL_PATTERN.match(value) is None


# ─── OTP shape ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["123456", "000000", "999999"])
def test_otp_pattern_accepts_six_digits(value):
    assert _OTP_PATTERN.match(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "12345",  # 5 digits
        "1234567",  # 7 digits
        "12345a",  # alpha
        " 123456 ",  # whitespace (the prompt should strip)
    ],
)
def test_otp_pattern_rejects_invalid(value):
    assert _OTP_PATTERN.match(value) is None


# ─── Key masking ───────────────────────────────────────────────────────────


def test_mask_key_preserves_prefix_and_suffix():
    key = "ac_live_abcdef1234567890"
    masked = _mask_key(key)
    assert masked.startswith("ac_live_")
    assert masked.endswith("7890")
    # Must not leak the bulk of the key
    assert "abcdef" not in masked
    assert "1234567" not in masked


def test_mask_key_handles_short_string():
    """A short / malformed key still masks without crashing."""
    assert _mask_key("ac_") == "ac_…"
    assert _mask_key("") == "ac_…"
