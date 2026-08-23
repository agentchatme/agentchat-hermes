"""Interactive ``hermes agentchat`` wizard.

External UX is intentionally copied verbatim from the legacy 0.1.x line
— same menus, same prompts, same arrow-key picker primitive
(``prompt_choice``), same styled print helpers from
``hermes_cli.setup``. The wording was engineered to mirror OpenClaw's
``channels add agentchat`` flow over many releases; we re-use it
rather than re-engineering UX from scratch.

Internal mechanics are 0.2.0:

* Network calls go through the ``agentchatme`` SDK (not raw ``httpx``),
  except the two unauthenticated "start" requests (``/v1/register`` and
  ``/v1/agents/recover``) which the plugin posts itself — see
  :func:`_register_start` / :func:`_recover_start` for why.
* The success paths (register / paste / replace / recover) call
  :func:`agentchatme_hermes.soul_anchor.write_soul_anchor` to install
  the identity anchor in ``~/.hermes/SOUL.md`` — the always-on identity
  surface 0.1.x did not have.
* The logout path calls
  :func:`agentchatme_hermes.soul_anchor.remove_soul_anchor`.
* The recover path (lost / leaked API key) re-keys an existing agent
  from its email **and** @handle — an email can back several agents, so
  the email alone no longer identifies one.

The non-interactive ``hermes agentchat <register|login|recover|status|logout>``
subcommands stay in ``cli.py`` — those are scriptable shortcuts. This
module is only the no-argument interactive entry.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from typing import Any

from .client_identity import HERMES_CLIENT_HEADERS, hermes_client_identity
from .soul_anchor import AnchorError, remove_soul_anchor, write_soul_anchor

logger = logging.getLogger(__name__)

_MAX_REGISTER_RETRIES = 3
_HANDLE_MIN = 3
_HANDLE_MAX = 30
_HANDLE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OTP_PATTERN = re.compile(r"^\d{6}$")

# Per-email policy rejections from ``POST /v1/register`` (both HTTP 409).
# The caps themselves live in a server-side DB row the operator can tune
# without a deploy, so the plugin never hard-codes them — it quotes the
# number the server reports in ``details.limit``.
_EMAIL_LIMIT_REACHED = "EMAIL_LIMIT_REACHED"  # email already backs max_active live agents
_EMAIL_EXHAUSTED = "EMAIL_EXHAUSTED"  # email has used its lifetime registration budget
_EMAIL_POLICY_CODES = frozenset({_EMAIL_LIMIT_REACHED, _EMAIL_EXHAUSTED})
# Retired code from servers that still enforce one live agent per email.
# Same user action as today's live cap, so it is folded into it.
_LEGACY_EMAIL_TAKEN = "EMAIL_TAKEN"

# ``POST /v1/agents/recover/verify`` answers this (HTTP 409) when the
# recovery was started without a handle on an email that backs several
# agents. ``details.handles`` lists the live handles on that email.
_HANDLE_REQUIRED = "HANDLE_REQUIRED"


# ─── public entry ──────────────────────────────────────────────────────────


def interactive_setup() -> None:
    """Run the interactive wizard. Wraps the body with ``KeyboardInterrupt``
    handling so Ctrl+C exits cleanly instead of dumping a traceback."""
    try:
        _interactive_setup_body()
    except KeyboardInterrupt:
        print()
        try:
            from hermes_cli.setup import print_info

            print_info("Cancelled.")
        except ImportError:
            print("Cancelled.")


def _step(message: str) -> None:
    """Step indicator — matches 0.1.x format verbatim."""
    print(f"  ✓ {message}")


def _interactive_setup_body() -> None:
    """Wizard core. State detection + branch into edit-menu or fresh-menu.

    Lazy-imports the Hermes ``hermes_cli`` helpers so this module imports
    cleanly outside a Hermes process (pytest, doc builds, etc.).
    """
    from hermes_cli.cli_output import prompt, prompt_yes_no
    from hermes_cli.setup import (
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt_choice,
        save_env_value,
    )

    print_header("AgentChat")
    print()

    existing_key = (get_env_value("AGENTCHATME_API_KEY") or "").strip()
    existing_handle = (get_env_value("AGENTCHATME_HANDLE") or "").strip().lstrip("@")

    if existing_key:
        _edit_menu(
            existing_key=existing_key,
            existing_handle=existing_handle,
            prompt=prompt,
            prompt_yes_no=prompt_yes_no,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
            get_env_value=get_env_value,
        )
        return

    _fresh_setup_menu(
        prompt=prompt,
        prompt_yes_no=prompt_yes_no,
        prompt_choice=prompt_choice,
        print_info=print_info,
        print_success=print_success,
        print_warning=print_warning,
        save_env_value=save_env_value,
        get_env_value=get_env_value,
    )


# ─── menus (external UX from 0.1.x) ────────────────────────────────────────


def _edit_menu(
    *,
    existing_key: str,
    existing_handle: str,
    prompt: Any,
    prompt_yes_no: Any,
    prompt_choice: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
    get_env_value: Any,
) -> None:
    """Already-configured edit menu. Mirrors the 0.1.x version verbatim."""
    masked = _mask_key(existing_key)
    identity_line = (
        f"AgentChat: configured (@{existing_handle}) with key {masked}"
        if existing_handle
        else f"AgentChat: configured with key {masked} (handle not cached)"
    )
    print_info(identity_line)
    print()

    choices = [
        "Keep current configuration",
        "Replace the API key (paste a new one, register a new agent, or recover a lost key)",
        "Logout (clear the saved key)",
    ]
    description = "ENTER to confirm a choice. ESC keeps the current configuration."
    idx = prompt_choice(
        "AgentChat is already configured. What would you like to do?",
        choices,
        default=0,
        description=description,
    )
    _step(choices[idx])

    if idx == 0:
        return
    if idx == 1:
        _replace_key_branch(
            existing_handle=existing_handle,
            prompt=prompt,
            prompt_yes_no=prompt_yes_no,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
            get_env_value=get_env_value,
        )
        return
    if idx == 2:
        _logout_flow(prompt_yes_no, print_info, print_success, save_env_value)


def _fresh_setup_menu(
    *,
    prompt: Any,
    prompt_yes_no: Any,
    prompt_choice: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
    get_env_value: Any,
) -> None:
    """Top-level register / paste / recover menu for fresh installs.

    Register and paste wording is verbatim from 0.1.x; the recover entry
    is new — a lost or leaked key used to mean "register again", which
    the per-email policy makes the wrong answer (every registration
    spends a slot of the email's lifetime budget). The 0.1.x
    "AGENTCHATME_ALLOW_ALL seed" step is NOT ported — that was specific
    to the platform-adapter gateway-authorization layer 0.2.0 doesn't go
    through.
    """
    choices = [
        "Register a new AgentChat agent (email + 6-digit OTP, ~60s)",
        "I already have an API key (paste ac_live_…)",
        "Recover a lost API key (email + @handle + 6-digit OTP)",
        "Skip for now",
    ]
    description = "Register is recommended for new users — it mints a fresh @handle."
    idx = prompt_choice(
        "How would you like to configure AgentChat?",
        choices,
        default=0,
        description=description,
    )
    _step(choices[idx])

    if idx == 3:
        return

    if idx == 0:
        ok = _register_new_agent_flow(
            prompt=prompt,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
        )
    elif idx == 1:
        ok = _paste_existing_key_flow(
            prompt, print_info, print_success, print_warning, save_env_value
        )
    else:
        ok = _recover_lost_key_flow(
            prompt=prompt,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
        )

    if not ok:
        return

    _step("Restart the gateway: hermes gateway restart")
    print_success("AgentChat ready")


def _replace_key_branch(
    *,
    existing_handle: str,
    prompt: Any,
    prompt_yes_no: Any,
    prompt_choice: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
    get_env_value: Any,
) -> None:
    """Replace-key sub-flow reached from the edit menu.

    Paste / register wording is verbatim 0.1.x. The recover entry covers
    the "my key leaked, rotate it" case for the agent this profile is
    already configured as — ``existing_handle`` is offered as the default
    handle so the operator only has to confirm it.
    """
    print()
    print_info(
        "Replacing the saved API key. The current key will be overwritten "
        "in ~/.hermes/.env."
    )
    print()

    choices = [
        "Paste a different API key (ac_live_…)",
        "Register a new agent (mints a brand-new @handle)",
        "Recover a lost API key (email + @handle + 6-digit OTP — rotates the key)",
        "Cancel — keep the current key",
    ]
    idx = prompt_choice(
        "How would you like to replace it?",
        choices,
        default=0,
    )
    _step(choices[idx])

    if idx == 3:
        return

    if idx == 0:
        ok = _paste_existing_key_flow(
            prompt, print_info, print_success, print_warning, save_env_value
        )
    elif idx == 1:
        ok = _register_new_agent_flow(
            prompt=prompt,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
        )
    else:
        ok = _recover_lost_key_flow(
            prompt=prompt,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
            existing_handle=existing_handle,
        )

    if ok:
        _step("Restart the gateway: hermes gateway restart")


def _logout_flow(
    prompt_yes_no: Any, print_info: Any, print_success: Any, save_env_value: Any
) -> None:
    """Clear saved credentials + strip the SOUL.md anchor.

    Verbatim 0.1.x confirmation copy; 0.2.0 adds the anchor strip after
    the env clear.
    """
    print()
    if not prompt_yes_no(
        "Clear AGENTCHATME_API_KEY and AGENTCHATME_HANDLE from ~/.hermes/.env? "
        "Your AgentChat agent will remain on the server — this only removes "
        "credentials from THIS Hermes profile.",
        False,
    ):
        print_info("Cancelled. Existing credentials retained.")
        return

    save_env_value("AGENTCHATME_API_KEY", "")
    save_env_value("AGENTCHATME_HANDLE", "")

    # 0.2.0 addition: strip the SOUL.md identity anchor so the agent
    # loses its AgentChat awareness across all contexts. Idempotent —
    # no-op when the block is already absent.
    try:
        removed = remove_soul_anchor()
        if removed:
            _step("Identity anchor removed from ~/.hermes/SOUL.md")
    except OSError as exc:
        # Non-fatal — credentials are cleared, the anchor's just stuck.
        # User can delete the block manually if it matters.
        logger.warning("logout: SOUL.md anchor strip failed: %s", exc)

    print_success("Logged out. Run `hermes agentchat` to reconfigure.")


# ─── flows (external UX from 0.1.x, internal mechanics from 0.2.0) ─────────


def _paste_existing_key_flow(
    prompt: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
) -> bool:
    print()
    print_info(
        "Paste your AgentChat API key. Mint one with `hermes agentchat register` "
        "or via the AgentChat docs if you don't have one yet."
    )
    api_key = prompt("API key (ac_live_…)").strip()
    if not api_key:
        print_warning("No key entered — skipping AgentChat setup.")
        return False
    if len(api_key) < 20:
        print_warning(
            f"That key is too short ({len(api_key)} chars) — refusing to save it."
        )
        return False

    handle = _validate_key_remote(api_key, print_warning)
    if not handle:
        print_warning("Key validation failed — not persisted. Try again with a fresh key.")
        return False

    save_env_value("AGENTCHATME_API_KEY", api_key)
    save_env_value("AGENTCHATME_HANDLE", handle)
    _step(f"Key validated — you are @{handle}")
    _install_anchor_or_warn(handle, print_warning)
    return True


def _register_new_agent_flow(
    *,
    prompt: Any,
    prompt_choice: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
) -> bool:
    """Email-OTP register flow. External UX verbatim from 0.1.x.

    Two recovery layers (also from 0.1.x):
      * Field-scoped retry — handle-class errors re-prompt only the
        handle; email-class errors re-prompt only the offending field.
      * Errors-as-navigation — the per-email policy rejections
        (``EMAIL_LIMIT_REACHED`` / ``EMAIL_EXHAUSTED``, plus the legacy
        ``EMAIL_TAKEN``) open a recovery menu instead of a flat retry.
    """
    print()
    print_info(
        "Registration mints a new AgentChat agent identity tied to your email."
    )
    print_info(
        "You will receive a 6-digit code to verify — check your inbox (and spam)."
    )
    print()

    email = _prompt_email(prompt, print_warning)
    if email is None:
        return False
    handle = _prompt_handle(prompt, print_warning)
    if handle is None:
        return False
    display_name = prompt(
        'Display name (shown next to your @handle, e.g. "Alice")'
    ).strip()

    pending_id: str | None = None
    for attempt in range(1, _MAX_REGISTER_RETRIES + 1):
        try:
            pending_id = _register_start(
                email=email, handle=handle, display_name=display_name
            )
            break
        except _RegisterError as err:
            if err.field == "handle" and attempt < _MAX_REGISTER_RETRIES:
                print_warning(f"Handle problem: {err}")
                new_handle = _prompt_handle(prompt, print_warning)
                if new_handle is None:
                    return False
                handle = new_handle
                continue

            if err.field == "email" and attempt < _MAX_REGISTER_RETRIES:
                next_step = _email_error_recovery(
                    code=err.code or "",
                    message=str(err),
                    prompt_choice=prompt_choice,
                    print_warning=print_warning,
                )
                if next_step == "cancel":
                    return False
                if next_step == "paste":
                    print()
                    return _paste_existing_key_flow(
                        prompt, print_info, print_success, print_warning, save_env_value
                    )
                if next_step == "recover":
                    # The email that hit the cap is the one the agents
                    # live under — pre-fill it so the operator only has
                    # to name the handle.
                    print()
                    return _recover_lost_key_flow(
                        prompt=prompt,
                        print_info=print_info,
                        print_success=print_success,
                        print_warning=print_warning,
                        save_env_value=save_env_value,
                        default_email=email,
                    )
                new_email = _prompt_email(prompt, print_warning)
                if new_email is None:
                    return False
                email = new_email
                continue

            print_warning(f"Registration failed: {err}")
            return False
        except Exception as e:
            print_warning(f"Could not reach AgentChat: {e}")
            return False

    if not pending_id:
        return False

    print()
    print_info(f"Verification code sent to {email}. Check your inbox.")
    code = _prompt_otp(prompt, print_warning)
    if not code:
        return False

    try:
        api_key, resolved_handle = _register_verify(pending_id=pending_id, code=code)
    except _RegisterError as err:
        print_warning(f"Verification failed: {err}")
        return False
    except Exception as e:
        print_warning(f"Verification request failed: {e}")
        return False

    save_env_value("AGENTCHATME_API_KEY", api_key)
    save_env_value("AGENTCHATME_HANDLE", resolved_handle)
    masked = _mask_key(api_key)
    _step(f"Registered as @{resolved_handle} (key {masked})")
    _install_anchor_or_warn(resolved_handle, print_warning)
    return True


def _email_error_recovery(
    *,
    code: str,
    message: str,
    prompt_choice: Any,
    print_warning: Any,
) -> str:
    """Errors-as-navigation menu for email-class server NACKs.

    Returns one of ``"retry-email"``, ``"paste"``, ``"recover"``,
    ``"cancel"``.

    Both policy codes get the same menu: the email is either full (live
    cap) or spent (lifetime cap), so the most-likely-correct action is a
    different email — and a ``+`` alias *is* a different email as far as
    the server is concerned, which is why the first choice says so.
    Pasting or recovering an existing agent's key come next: hitting a
    per-email cap means agents already exist on it, and "I wanted to
    re-key one of those" is the usual mistake behind a failed register.
    The legacy ``EMAIL_TAKEN`` never reaches here — :func:`_register_start`
    folds it into ``EMAIL_LIMIT_REACHED``.
    """
    print_warning(message)

    if code == _EMAIL_LIMIT_REACHED:
        question = "That email is at its live-agent limit. What now?"
    elif code == _EMAIL_EXHAUSTED:
        question = "That email has no registrations left. What now?"
    else:
        question = "Couldn't register with that email. What now?"

    choices = [
        "Use a different email address (a +alias like you+hermes@example.com counts as different)",
        "Paste the API key of an agent already registered under this email",
        "Recover the key of an agent already registered under this email (email + @handle + OTP)",
        "Cancel registration",
    ]
    idx = prompt_choice(question, choices, default=0)
    return str(["retry-email", "paste", "recover", "cancel"][idx])


def _recover_lost_key_flow(
    *,
    prompt: Any,
    print_info: Any,
    print_success: Any,
    print_warning: Any,
    save_env_value: Any,
    existing_handle: str = "",
    default_email: str = "",
) -> bool:
    """Email-OTP recovery flow: re-key an agent whose API key is lost or leaked.

    Recovery needs the agent's @handle as well as its email — an email
    can back several agents, so the email alone no longer identifies
    one. ``existing_handle`` (the handle this Hermes profile is already
    configured as) is offered as the prompt default because "my own key
    leaked, rotate it" is the overwhelmingly common case; the operator
    can still type any handle registered under that email.

    The server's first response is deliberately identical whether or not
    the handle/email pair exists (no email-existence oracle), so the
    only proof of a match is the code arriving in the inbox. A wrong
    pair surfaces as ``INVALID_CODE`` at the verify step, exactly like a
    mistyped code — the copy below is worded so that outcome is not a
    surprise.
    """
    print()
    print_info(
        "Recovery re-keys an existing AgentChat agent: prove control of its "
        "email with a 6-digit code and a fresh API key is issued."
    )
    print_info(
        "The old key stops working the moment the new one is issued — "
        "update anything still using it."
    )
    print()

    email = _prompt_email(prompt, print_warning, default=default_email)
    if email is None:
        return False
    handle = _prompt_recovery_handle(prompt, print_warning, default=existing_handle)
    if handle is None:
        return False

    try:
        pending_id = _recover_start(email=email, handle=handle)
    except _RecoverError as err:
        print_warning(f"Recovery request failed: {err}")
        return False
    except Exception as e:
        print_warning(f"Could not reach AgentChat: {e}")
        return False

    print()
    print_info(
        f"If @{handle} is registered under {email}, a 6-digit code is on its "
        "way (valid ~10 minutes). Check your inbox (and spam)."
    )
    code = _prompt_otp(prompt, print_warning)
    if not code:
        return False

    try:
        api_key, resolved_handle = _recover_verify(pending_id=pending_id, code=code)
    except _RecoverError as err:
        if err.code == _HANDLE_REQUIRED:
            for line in _handle_required_lines(err):
                print_warning(line)
            return False
        print_warning(f"Recovery failed: {err}")
        return False
    except Exception as e:
        print_warning(f"Recovery request failed: {e}")
        return False

    save_env_value("AGENTCHATME_API_KEY", api_key)
    save_env_value("AGENTCHATME_HANDLE", resolved_handle)
    masked = _mask_key(api_key)
    _step(f"Recovered @{resolved_handle} (new key {masked}) — the previous key is now dead")
    _install_anchor_or_warn(resolved_handle, print_warning)
    return True


def _handle_required_lines(err: _RecoverError) -> list[str]:
    """Operator-facing lines for a ``HANDLE_REQUIRED`` verify rejection.

    Shared by the wizard and the ``recover`` subcommand so both print the
    same thing: the server's message, the live handles it listed (the
    caller has just proven inbox control, which is the only time the
    server is willing to enumerate them), and the instruction to go
    again with one of them.
    """
    lines = [
        str(err)
        or "This email backs more than one agent — recovery needs the @handle too."
    ]
    if err.handles:
        lines.append(
            "Agents registered under this email: "
            + ", ".join(f"@{handle}" for handle in err.handles)
        )
    lines.append("Run recovery again and enter the @handle you want to re-key.")
    return lines


# ─── prompts (external UX from 0.1.x verbatim) ─────────────────────────────


def _prompt_email(prompt: Any, print_warning: Any, *, default: str = "") -> str | None:
    """Prompt for the email. ``default`` (when set) is shown by Hermes'
    ``prompt`` in brackets and returned on a bare ENTER — used when the
    flow already knows the email, e.g. recovery reached from a failed
    registration."""
    question = "Email — receives the 6-digit verification code (e.g. you@example.com)"
    for _ in range(_MAX_REGISTER_RETRIES):
        raw = prompt(question, default=default) if default else prompt(question)
        value = str(raw).strip()
        if not value:
            print_warning("Email is required.")
            continue
        if not _EMAIL_PATTERN.match(value):
            print_warning("That doesn't look like a valid email. Try again.")
            continue
        return value
    print_warning("Too many invalid email attempts — aborting.")
    return None


def _prompt_recovery_handle(
    prompt: Any, print_warning: Any, *, default: str = ""
) -> str | None:
    """Prompt for the @handle of the agent to recover.

    Deliberately separate from :func:`_prompt_handle`: that one asks the
    user to *choose* a new handle, this one asks which existing agent
    to re-key, and offers ``default`` (the locally configured handle,
    when there is one) on a bare ENTER. Same shape validation — a
    malformed handle would only ever produce a decoy recovery, so catch
    it client-side.
    """
    question = "@handle of the agent to recover"
    for _ in range(_MAX_REGISTER_RETRIES):
        raw = prompt(question, default=default) if default else prompt(question)
        value = str(raw).strip().lstrip("@").lower()
        err = _validate_handle(value)
        if err:
            print_warning(err)
            continue
        return value
    print_warning("Too many invalid handle attempts — aborting.")
    return None


def _prompt_handle(prompt: Any, print_warning: Any) -> str | None:
    for _ in range(_MAX_REGISTER_RETRIES):
        value = (
            str(
                prompt(
                    "Choose a @handle (3-30 chars, lowercase letters/digits/hyphens, "
                    "must start with a letter)"
                )
            )
            .strip()
            .lstrip("@")
            .lower()
        )
        err = _validate_handle(value)
        if err:
            print_warning(err)
            continue
        return value
    print_warning("Too many invalid handle attempts — aborting.")
    return None


def _prompt_otp(prompt: Any, print_warning: Any) -> str | None:
    for _ in range(_MAX_REGISTER_RETRIES):
        value = str(prompt("Enter the 6-digit code from your inbox")).strip()
        if _OTP_PATTERN.match(value):
            return value
        print_warning("Codes are 6 digits. Try again.")
    print_warning("Too many invalid code attempts — aborting.")
    return None


def _validate_handle(value: str) -> str | None:
    """Return an error message on shape failure, ``None`` on success.

    Verbatim error wording from 0.1.x.
    """
    if not value:
        return "Handle is required."
    if len(value) < _HANDLE_MIN or len(value) > _HANDLE_MAX:
        return (
            f"Length must be {_HANDLE_MIN}-{_HANDLE_MAX} chars "
            f"(you entered {len(value)})."
        )
    if not value[0].isalpha():
        return "Must start with a lowercase letter."
    if re.search(r"[^a-z0-9-]", value):
        return (
            "Only lowercase letters, digits, and hyphens — "
            "no underscores, dots, or symbols."
        )
    if "--" in value:
        return "No consecutive hyphens."
    if value.endswith("-"):
        return "Cannot end with a hyphen."
    if not _HANDLE_PATTERN.match(value):
        return "Invalid handle shape."
    return None


def _mask_key(key: str) -> str:
    """Render a key for display without leaking the bulk of the secret.

    Verbatim format from 0.1.x: 8-char prefix + 4-char suffix.
    """
    if len(key) < 12:
        return "ac_…"
    prefix = key[:8]
    suffix = key[-4:]
    return f"{prefix}…{suffix}"


# ─── anchor integration (0.2.0 only) ───────────────────────────────────────


def _install_anchor_or_warn(handle: str, print_warning: Any) -> None:
    """Upsert the SOUL.md anchor after a successful register / paste.

    Non-fatal — if the anchor write fails for any reason the credentials
    are still persisted and the runtime will boot. We surface a clear
    warning so the operator can repair manually. Mirrors the posture in
    :func:`agentchatme_hermes.cli._install_soul_anchor`.
    """
    try:
        path = write_soul_anchor(handle)
        _step(f"Identity anchor written to {path}")
    except (AnchorError, OSError) as exc:
        print_warning(
            f"Could not update ~/.hermes/SOUL.md with your AgentChat identity "
            f"({exc}). Your account is configured, but the agent will lack "
            "AgentChat awareness outside AgentChat-triggered turns until this "
            "is repaired."
        )


# ─── internal mechanics (0.2.0 — SDK-based) ────────────────────────────────


class _RegisterError(Exception):
    """Server-side registration failure with field-scoped context.

    ``field`` lets the wizard re-prompt only the offending input
    (``"handle"`` / ``"email"`` / ``None``). ``code`` carries the
    server's canonical error code (``HANDLE_TAKEN``,
    ``EMAIL_LIMIT_REACHED``, ``EMAIL_EXHAUSTED``, ``RATE_LIMITED``, …)
    so the email-error recovery menu can default to the
    most-likely-correct action.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.code = code


class _RecoverError(Exception):
    """Server-side recovery failure.

    ``code`` carries the server's canonical error code (``INVALID_CODE``,
    ``HANDLE_REQUIRED``, ``RATE_LIMITED``, ``VALIDATION_ERROR``, …).
    ``handles`` is only populated for ``HANDLE_REQUIRED`` — the live
    handles on that email, which the server lists at the verify step
    and nowhere else — so the caller can show the operator what to
    re-run with.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        handles: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.handles: list[str] = list(handles or [])


def _api_base() -> str:
    return (
        os.environ.get("AGENTCHATME_API_BASE", "https://api.agentchat.me").strip()
        or "https://api.agentchat.me"
    ).rstrip("/")


def _register_start(*, email: str, handle: str, display_name: str) -> str:
    """POST /v1/register via raw httpx, omitting null fields.

    Bypasses the SDK's static :meth:`AgentChatClient.register` because that
    method sends ``description: null`` unconditionally; the server's strict
    Zod validation rejects nulls with ``Expected string, received null``,
    and the SDK swallows the helpful ``details.fieldErrors`` into a generic
    ``Invalid request`` exception message — both of which are SDK bugs to
    fix in ``agentchatme`` proper, but until that ships we work around in
    the plugin. Httpx is already a transitive dep via the SDK, so no new
    package on the dependency closure.

    Returns the ``pending_id`` for the OTP verify step. Maps server error
    codes onto field hints so the wizard's retry loop re-prompts the
    correct field.
    """
    import httpx

    body: dict[str, Any] = {"email": email, "handle": handle}
    if display_name:
        body["display_name"] = display_name

    try:
        resp = httpx.post(
            f"{_api_base()}/v1/register",
            json=body,
            headers=HERMES_CLIENT_HEADERS,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise _RegisterError(f"network error: {exc}") from exc

    if resp.status_code in (200, 201):
        try:
            data = resp.json()
        except ValueError as exc:
            raise _RegisterError("invalid server response") from exc
        pending_id = data.get("pending_id")
        if not isinstance(pending_id, str) or not pending_id:
            raise _RegisterError("server did not return a pending_id")
        return pending_id

    code, message, details = _parse_error_response(resp)
    if code in {"HANDLE_TAKEN", "INVALID_HANDLE", "RESERVED_HANDLE"}:
        raise _RegisterError(message or code, field="handle", code=code)
    if code == _LEGACY_EMAIL_TAKEN:
        # A server that still enforces one live agent per email. The
        # user's options are the same as at today's live cap (different
        # email, or paste / recover the existing agent's key), so it is
        # folded into EMAIL_LIMIT_REACHED; it carries no details.limit,
        # so the message falls back to the server's own wording.
        code = _EMAIL_LIMIT_REACHED
    if code in _EMAIL_POLICY_CODES:
        raise _RegisterError(
            _email_policy_message(code, message, _limit_from_details(details)),
            field="email",
            code=code,
        )
    if code == "RATE_LIMITED":
        raise _RegisterError(
            "rate-limited — wait a minute and try again", code=code
        )
    raise _RegisterError(
        message or code or f"HTTP {resp.status_code}", code=code
    )


def _email_policy_message(code: str, server_message: str | None, limit: int | None) -> str:
    """User-facing text for a per-email policy rejection.

    Quotes the cap the server reported in ``details.limit``: the numbers
    live in a server-side DB row the operator can tune without a deploy,
    so nothing client-side may hard-code them. Without ``details.limit``
    (a legacy ``EMAIL_TAKEN`` server, or a future shape change) the
    server's own message is used verbatim.
    """
    if limit is None:
        return server_message or code
    if code == _EMAIL_LIMIT_REACHED:
        return (
            f"This email already backs {limit} active agents — the server's "
            "per-email limit. Delete one of them, or register with a different email."
        )
    return (
        f"This email has used all {limit} of its lifetime registrations — the "
        "server's per-email limit. Register with a different email."
    )


def _limit_from_details(details: Mapping[str, Any] | None) -> int | None:
    """``details.limit`` as a positive int, else ``None`` (bool is an int
    subclass in Python — ``True`` must not read as "limit 1")."""
    if details is None:
        return None
    limit = details.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return None
    return limit


def _handles_from_details(details: Any) -> list[str]:
    """``details.handles`` as a list of non-empty strings, else ``[]``."""
    if not isinstance(details, Mapping):
        return []
    raw = details.get("handles")
    if not isinstance(raw, list):
        return []
    return [handle for handle in raw if isinstance(handle, str) and handle]


def _parse_error_response(
    resp: Any,
) -> tuple[str | None, str | None, Mapping[str, Any] | None]:
    """Pull ``(code, message, details)`` from a JSON error response.

    Surfaces ``details.fieldErrors`` when present so the user sees what
    was actually wrong instead of the generic top-level ``message`` —
    fixes the "Invalid request" black-box UX the SDK has. ``details`` is
    returned as-is (or ``None``) so callers can read code-specific keys
    such as ``limit``.
    """
    try:
        data = resp.json()
    except ValueError:
        return None, None, None
    code = data.get("code") if isinstance(data, dict) else None
    message = data.get("message") if isinstance(data, dict) else None

    # If validation failed with field-specific errors, splice them onto
    # the message so the user can see which field broke.
    details = data.get("details") if isinstance(data, dict) else None
    if isinstance(details, dict):
        field_errors = details.get("fieldErrors")
        if isinstance(field_errors, dict) and field_errors:
            parts = []
            for field, errors in field_errors.items():
                if isinstance(errors, list) and errors:
                    parts.append(f"{field}: {errors[0]}")
            if parts:
                detail = "; ".join(parts)
                message = f"{message} ({detail})" if message else detail

    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
        details if isinstance(details, dict) else None,
    )


def _register_verify(*, pending_id: str, code: str) -> tuple[str, str]:
    """POST /v1/register/verify via the SDK → returns ``(api_key, handle)``."""
    from agentchatme import AgentChatClient

    try:
        agent, api_key, auth_client = AgentChatClient.verify(
            pending_id,
            code,
            base_url=_api_base(),
            client_identity=hermes_client_identity(),
        )
    except Exception as exc:
        err_code = getattr(exc, "code", None)
        message = str(exc)
        raise _RegisterError(message or (err_code or "verification failed"), code=err_code) from exc

    # The SDK hands us back an authenticated client we never use here —
    # close it so the underlying httpx connection pool doesn't leak.
    try:
        auth_client.close()
    except Exception:
        logger.debug("auth_client close after verify raised", exc_info=True)

    handle = agent.get("handle") if isinstance(agent, dict) else None
    if not isinstance(api_key, str) or not api_key:
        raise _RegisterError("server response missing api_key")
    if not isinstance(handle, str) or not handle:
        raise _RegisterError("server response missing handle")
    return api_key, handle


def _recover_start(*, email: str, handle: str) -> str:
    """POST /v1/agents/recover via raw httpx → the ``pending_id`` for the OTP step.

    Always sends ``handle`` alongside ``email``: an email can back several
    agents, so the server needs both to know which one to re-key. The
    SDK's static :meth:`AgentChatClient.recover` only grew a ``handle``
    parameter after this plugin's SDK floor, so the plugin posts the
    request itself — the same arrangement :func:`_register_start` uses,
    httpx already being on the dependency closure — instead of raising
    the floor and breaking installs whose SDK hasn't been upgraded. The
    verify step's body is unchanged, so it still goes through the SDK.

    The server answers ``200 {pending_id, message}`` for every outcome
    (match, no match, OTP rate-limited, send failure) so that nothing
    about the response reveals whether the pair exists; a wrong pair
    surfaces as ``INVALID_CODE`` at verify, exactly like a wrong code.
    Pre-policy servers omit ``pending_id`` when nothing matched — that
    is reported as "no code issued" rather than as a transport bug.
    """
    import httpx

    body: dict[str, Any] = {"email": email, "handle": handle}

    try:
        resp = httpx.post(
            f"{_api_base()}/v1/agents/recover",
            json=body,
            headers=HERMES_CLIENT_HEADERS,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise _RecoverError(f"network error: {exc}") from exc

    if resp.status_code in (200, 201):
        try:
            data = resp.json()
        except ValueError as exc:
            raise _RecoverError("invalid server response") from exc
        pending_id = data.get("pending_id") if isinstance(data, dict) else None
        if not isinstance(pending_id, str) or not pending_id:
            raise _RecoverError(
                "no recovery code was issued (the server returned no pending_id) — "
                "check the email and @handle and try again"
            )
        return pending_id

    code, message, _details = _parse_error_response(resp)
    if code == "RATE_LIMITED":
        raise _RecoverError("rate-limited — wait a minute and try again", code=code)
    raise _RecoverError(message or code or f"HTTP {resp.status_code}", code=code)


def _recover_verify(*, pending_id: str, code: str) -> tuple[str, str]:
    """POST /v1/agents/recover/verify via the SDK → ``(api_key, handle)``.

    Success rotates the agent's key server-side: the returned key is the
    only valid one from this moment on.

    ``HANDLE_REQUIRED`` (HTTP 409) means the recovery was started without
    a handle on an email that backs several agents. This plugin always
    sends the handle, so a matching server never answers that — but it
    is handled anyway (older client builds, a handle the server did not
    accept): ``details.handles`` is carried up on the error so the caller
    can show which handles the operator may re-run with.
    """
    from agentchatme import AgentChatClient

    try:
        handle, api_key, auth_client = AgentChatClient.recover_verify(
            pending_id,
            code,
            base_url=_api_base(),
            client_identity=hermes_client_identity(),
        )
    except Exception as exc:
        err_code = getattr(exc, "code", None)
        message = str(exc)
        raise _RecoverError(
            message or (err_code if isinstance(err_code, str) else None) or "recovery failed",
            code=err_code if isinstance(err_code, str) else None,
            handles=_handles_from_details(getattr(exc, "details", None)),
        ) from exc

    # Same as verify(): the SDK hands back an authenticated client we
    # never use here — close it so the httpx connection pool doesn't leak.
    try:
        auth_client.close()
    except Exception:
        logger.debug("auth_client close after recover_verify raised", exc_info=True)

    if not isinstance(api_key, str) or not api_key:
        raise _RecoverError("server response missing api_key")
    if not isinstance(handle, str) or not handle:
        raise _RecoverError("server response missing handle")
    return api_key, handle


def _validate_key_remote(api_key: str, print_warning: Any) -> str | None:
    """Validate a pasted key via ``GET /v1/agents/me``. Returns the handle on success.

    Network errors surface via ``print_warning``; an invalid key returns
    ``None`` so the caller can re-prompt or abort.
    """
    from agentchatme import AgentChatClient

    client = AgentChatClient(
        api_key=api_key,
        base_url=_api_base(),
        client_identity=hermes_client_identity(),
    )
    try:
        try:
            me = client.get_me()
        except Exception as exc:
            code = getattr(exc, "code", None)
            status = getattr(exc, "status", None)
            if status in {401, 403} or code in {"UNAUTHORIZED", "INVALID_API_KEY"}:
                print_warning(
                    "Key was rejected by the server (401/403). "
                    "Double-check it and try again."
                )
                return None
            print_warning(f"Could not reach AgentChat: {exc}")
            return None
    finally:
        try:
            client.close()
        except Exception:
            logger.debug("client close after get_me raised", exc_info=True)

    handle = me.get("handle")
    return handle if isinstance(handle, str) else None
