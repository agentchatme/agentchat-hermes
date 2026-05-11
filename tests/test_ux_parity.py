"""Tests for the 0.1.64 OpenClaw-parity UX rewrite.

Covers:

* ``dispatch_cli_command`` with no subcommand launches ``interactive_setup``
  (the bare ``hermes agentchat`` entry — recommended human path).
* ``_build_platform_hint`` interpolates the literal handle from
  ``AGENTCHATME_HANDLE`` so the agent sees its identity in every system
  prompt — the Hermes equivalent of OpenClaw's AGENTS.md anchor write.
* The handle is validated against the canonical regex before being
  inlined (corrupt env doesn't injection-attack the system prompt).
* ``_email_error_recovery`` returns the correct sentinel for each
  recovery-menu position.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

# ── 1. Bare `hermes agentchat` launches the wizard ─────────────────────────


def test_dispatch_with_no_subcommand_launches_wizard():
    from agentchatme_hermes import cli

    with patch("agentchatme_hermes.setup.interactive_setup") as mock_wizard:
        result = cli.dispatch_cli_command(SimpleNamespace(action=None))

    mock_wizard.assert_called_once()
    assert result == 0


def test_dispatch_with_unknown_attr_still_launches_wizard():
    """A Namespace that doesn't define `action` at all (defensive)."""
    from agentchatme_hermes import cli

    with patch("agentchatme_hermes.setup.interactive_setup") as mock_wizard:
        cli.dispatch_cli_command(SimpleNamespace())

    mock_wizard.assert_called_once()


def test_dispatch_named_subcommand_does_not_launch_wizard():
    """Sanity: named subcommands STILL hit their backend, not the wizard."""
    from agentchatme_hermes import cli

    with (
        patch("agentchatme_hermes.setup.interactive_setup") as mock_wizard,
        patch("agentchatme_hermes.setup.cli_whoami", return_value=0) as mock_whoami,
    ):
        cli.dispatch_cli_command(SimpleNamespace(action="whoami"))

    mock_wizard.assert_not_called()
    mock_whoami.assert_called_once()


# ── 2. Platform hint includes the literal handle ───────────────────────────


def test_platform_hint_includes_handle_when_env_set(monkeypatch):
    monkeypatch.setenv("AGENTCHATME_HANDLE", "alice")
    from agentchatme_hermes.adapter import _build_platform_hint

    hint = _build_platform_hint()
    assert "@alice" in hint
    assert "phone number" in hint  # OpenClaw-anchor prose marker
    assert "agentchat_get_my_status" not in hint  # fallback path NOT taken


def test_platform_hint_strips_leading_at(monkeypatch):
    """Wizard saves the handle without `@`, but defensively handle a
    legacy or hand-edited .env that has `@` prefixed."""
    monkeypatch.setenv("AGENTCHATME_HANDLE", "@alice")
    from agentchatme_hermes.adapter import _build_platform_hint

    hint = _build_platform_hint()
    assert "@alice" in hint
    assert "@@alice" not in hint


def test_platform_hint_falls_back_when_handle_missing(monkeypatch):
    monkeypatch.delenv("AGENTCHATME_HANDLE", raising=False)
    from agentchatme_hermes.adapter import _build_platform_hint

    hint = _build_platform_hint()
    assert "agentchat_get_my_status" in hint
    # The handle-template-specific prose is absent.
    assert "phone number" not in hint


def test_platform_hint_rejects_invalid_handle_shape(monkeypatch):
    """A corrupt or hand-edited env (e.g. shell injection attempt) must
    not flow into the system prompt verbatim. Fall back instead."""
    bad_handles = [
        "Alice",            # uppercase
        "al",               # too short
        "a" * 31,           # too long
        "alice_smith",      # underscore
        "alice.smith",      # period
        "-alice",           # leading hyphen
        "alice-",           # trailing hyphen
        "alice--bob",       # double hyphen
        "1alice",           # starts with digit
        "alice; rm -rf /",  # injection
        "",                 # empty
    ]
    for bad in bad_handles:
        monkeypatch.setenv("AGENTCHATME_HANDLE", bad)
        from agentchatme_hermes.adapter import _build_platform_hint

        hint = _build_platform_hint()
        # Primary assertion: fallback template (no-handle form) was used,
        # i.e. the bad handle didn't pass validation.
        assert "agentchat_get_my_status" in hint, (
            f"bad handle {bad!r} should have triggered fallback"
        )
        # Secondary: the bad value didn't make it through as "@<bad>".
        # (Short substrings like "al" appear naturally in copy — "call",
        # "alike" etc. — so we check the `@`-prefixed form which would
        # only appear if interpolation happened.)
        if bad.strip():
            assert f"@{bad}" not in hint, f"bad handle {bad!r} leaked into hint"


def test_platform_hint_accepts_canonical_handles(monkeypatch):
    """Any handle that matches the canonical regex must be inlined."""
    good_handles = ["alice", "alice123", "alice-bob", "a1b", "a" * 30]
    for handle in good_handles:
        monkeypatch.setenv("AGENTCHATME_HANDLE", handle)
        from agentchatme_hermes.adapter import _build_platform_hint

        hint = _build_platform_hint()
        assert f"@{handle}" in hint, f"good handle {handle!r} didn't make it"


# ── 3. Email-error recovery menu position mapping ─────────────────────────


def test_email_taken_recovery_paste_position():
    """EMAIL_TAKEN menu order: paste / retry / cancel. Picking 0 returns 'paste'."""
    from agentchatme_hermes.setup import _email_error_recovery

    captured = {}

    def fake_prompt_choice(question, choices, default=0, description=None):
        captured["question"] = question
        captured["choices"] = list(choices)
        captured["default"] = default
        return 0  # paste

    result = _email_error_recovery(
        code="EMAIL_TAKEN",
        message="That email is already registered.",
        prompt=lambda *_a, **_kw: "",
        prompt_choice=fake_prompt_choice,
        print_info=lambda *_a: None,
        print_warning=lambda *_a: None,
    )
    assert result == "paste"
    assert "Paste" in captured["choices"][0]  # paste is recommended-default
    assert captured["default"] == 0


def test_email_taken_recovery_retry_position():
    """Position 1 on EMAIL_TAKEN is 'retry-email' (different email)."""
    from agentchatme_hermes.setup import _email_error_recovery

    result = _email_error_recovery(
        code="EMAIL_TAKEN",
        message="taken",
        prompt=lambda *_a, **_kw: "",
        prompt_choice=lambda *_a, **_kw: 1,
        print_info=lambda *_a: None,
        print_warning=lambda *_a: None,
    )
    assert result == "retry-email"


def test_email_taken_recovery_cancel_position():
    from agentchatme_hermes.setup import _email_error_recovery

    result = _email_error_recovery(
        code="EMAIL_TAKEN",
        message="taken",
        prompt=lambda *_a, **_kw: "",
        prompt_choice=lambda *_a, **_kw: 2,
        print_info=lambda *_a: None,
        print_warning=lambda *_a: None,
    )
    assert result == "cancel"


def test_email_exhausted_menu_order_flipped():
    """EMAIL_EXHAUSTED defaults to 'retry-email' (different email) — the
    paste-key path is position 1, not 0. Order is intentionally different
    from EMAIL_TAKEN."""
    from agentchatme_hermes.setup import _email_error_recovery

    # Position 0 on EMAIL_EXHAUSTED → retry-email
    assert _email_error_recovery(
        code="EMAIL_EXHAUSTED",
        message="x",
        prompt=lambda *_a, **_kw: "",
        prompt_choice=lambda *_a, **_kw: 0,
        print_info=lambda *_a: None,
        print_warning=lambda *_a: None,
    ) == "retry-email"

    # Position 1 on EMAIL_EXHAUSTED → paste
    assert _email_error_recovery(
        code="EMAIL_EXHAUSTED",
        message="x",
        prompt=lambda *_a, **_kw: "",
        prompt_choice=lambda *_a, **_kw: 1,
        print_info=lambda *_a: None,
        print_warning=lambda *_a: None,
    ) == "paste"


# ── 4. RegisterError carries the code so the wizard can branch ─────────────


def test_register_error_carries_code():
    from agentchatme_hermes.setup import _RegisterError

    err = _RegisterError("taken", field="email", code="EMAIL_TAKEN")
    assert err.field == "email"
    assert err.code == "EMAIL_TAKEN"
    assert str(err) == "taken"


def test_register_error_code_optional():
    """Older call sites without `code=` still work."""
    from agentchatme_hermes.setup import _RegisterError

    err = _RegisterError("boom")
    assert err.code is None
    assert err.field is None
