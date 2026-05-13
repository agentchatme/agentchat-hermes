"""Interactive ``hermes agentchat`` wizard.

External UX is intentionally copied verbatim from the legacy 0.1.x line
— same menus, same prompts, same arrow-key picker primitive
(``prompt_choice``), same styled print helpers from
``hermes_cli.setup``. The wording was engineered to mirror OpenClaw's
``channels add agentchat`` flow over many releases; we re-use it
rather than re-engineering UX from scratch.

Internal mechanics are 0.2.0:

* Network calls go through the ``agentchatme`` SDK (not raw ``httpx``).
* The success paths (register / paste / replace) call
  :func:`agentchatme_hermes.soul_anchor.write_soul_anchor` to install
  the identity anchor in ``~/.hermes/SOUL.md`` — the always-on identity
  surface 0.1.x did not have.
* The logout path calls
  :func:`agentchatme_hermes.soul_anchor.remove_soul_anchor`.

The non-interactive ``hermes agentchat <register|login|status|logout>``
subcommands stay in ``cli.py`` — those are scriptable shortcuts. This
module is only the no-argument interactive entry.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from .soul_anchor import AnchorError, remove_soul_anchor, write_soul_anchor

logger = logging.getLogger(__name__)

_MAX_REGISTER_RETRIES = 3
_HANDLE_MIN = 3
_HANDLE_MAX = 30
_HANDLE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OTP_PATTERN = re.compile(r"^\d{6}$")


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
        "Replace the API key (paste a new one, or register a new agent)",
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
    """Top-level register-or-paste menu for fresh installs.

    Verbatim wording from 0.1.x. The 0.1.x "AGENTCHATME_ALLOW_ALL seed"
    step is NOT ported — that was specific to the platform-adapter
    gateway-authorization layer 0.2.0 doesn't go through.
    """
    choices = [
        "Register a new AgentChat agent (email + 6-digit OTP, ~60s)",
        "I already have an API key (paste ac_live_…)",
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

    if idx == 2:
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
    else:
        ok = _paste_existing_key_flow(
            prompt, print_info, print_success, print_warning, save_env_value
        )

    if not ok:
        return

    _step("Restart the gateway: hermes gateway restart")
    print_success("AgentChat ready")


def _replace_key_branch(
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
    """Replace-key sub-flow reached from the edit menu. Verbatim 0.1.x wording."""
    print()
    print_info(
        "Replacing the saved API key. The current key will be overwritten "
        "in ~/.hermes/.env."
    )
    print()

    choices = [
        "Paste a different API key (ac_live_…)",
        "Register a new agent (mints a brand-new @handle)",
        "Cancel — keep the current key",
    ]
    idx = prompt_choice(
        "How would you like to replace it?",
        choices,
        default=0,
    )
    _step(choices[idx])

    if idx == 2:
        return

    if idx == 0:
        ok = _paste_existing_key_flow(
            prompt, print_info, print_success, print_warning, save_env_value
        )
    else:
        ok = _register_new_agent_flow(
            prompt=prompt,
            prompt_choice=prompt_choice,
            print_info=print_info,
            print_success=print_success,
            print_warning=print_warning,
            save_env_value=save_env_value,
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
      * Errors-as-navigation — ``EMAIL_TAKEN`` / ``EMAIL_EXHAUSTED`` open
        a 3-option recovery menu instead of a flat retry.
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

    Returns one of ``"paste"``, ``"retry-email"``, ``"cancel"``. Verbatim
    wording, defaults, and code-specific reordering from 0.1.x — the
    most-likely-correct action defaults differ by error code.
    """
    print_warning(message)

    if code == "EMAIL_TAKEN":
        choices = [
            "Paste the existing API key for this agent (recommended if you own it)",
            "Use a different email address",
            "Cancel registration",
        ]
        question = "That email is already registered. What now?"
    elif code == "EMAIL_EXHAUSTED":
        choices = [
            "Use a different email address",
            "Paste an existing API key instead",
            "Cancel registration",
        ]
        question = "Too many recent registrations for that email. What now?"
    else:
        choices = [
            "Use a different email address",
            "Paste an existing API key instead",
            "Cancel registration",
        ]
        question = "Couldn't register with that email. What now?"

    idx = prompt_choice(question, choices, default=0)

    # Order of "paste" vs "retry-email" flips between EMAIL_TAKEN
    # (paste first) and EMAIL_EXHAUSTED (retry first). Map by position.
    if code == "EMAIL_TAKEN":
        return str(["paste", "retry-email", "cancel"][idx])
    return str(["retry-email", "paste", "cancel"][idx])


# ─── prompts (external UX from 0.1.x verbatim) ─────────────────────────────


def _prompt_email(prompt: Any, print_warning: Any) -> str | None:
    for _ in range(_MAX_REGISTER_RETRIES):
        value = str(
            prompt(
                "Email — receives the 6-digit verification code (e.g. you@example.com)"
            )
        ).strip()
        if not value:
            print_warning("Email is required.")
            continue
        if not _EMAIL_PATTERN.match(value):
            print_warning("That doesn't look like a valid email. Try again.")
            continue
        return value
    print_warning("Too many invalid email attempts — aborting.")
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
    server's canonical error code (``HANDLE_TAKEN``, ``EMAIL_TAKEN``,
    ``EMAIL_EXHAUSTED``, ``RATE_LIMITED``, …) so the email-error
    recovery menu can default to the most-likely-correct action.
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

    code, message = _parse_register_error(resp)
    if code in {"HANDLE_TAKEN", "INVALID_HANDLE", "RESERVED_HANDLE"}:
        raise _RegisterError(message or code, field="handle", code=code)
    if code in {"EMAIL_TAKEN", "EMAIL_EXHAUSTED"}:
        raise _RegisterError(message or code, field="email", code=code)
    if code == "RATE_LIMITED":
        raise _RegisterError(
            "rate-limited — wait a minute and try again", code=code
        )
    raise _RegisterError(
        message or code or f"HTTP {resp.status_code}", code=code
    )


def _parse_register_error(resp: Any) -> tuple[str | None, str | None]:
    """Pull ``(code, message)`` from a JSON error response.

    Surfaces ``details.fieldErrors`` when present so the user sees what
    was actually wrong instead of the generic top-level ``message`` —
    fixes the "Invalid request" black-box UX the SDK has.
    """
    try:
        data = resp.json()
    except ValueError:
        return None, None
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
    )


def _register_verify(*, pending_id: str, code: str) -> tuple[str, str]:
    """POST /v1/register/verify via the SDK → returns ``(api_key, handle)``."""
    from agentchatme import AgentChatClient

    try:
        agent, api_key, auth_client = AgentChatClient.verify(
            pending_id, code, base_url=_api_base()
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


def _validate_key_remote(api_key: str, print_warning: Any) -> str | None:
    """Validate a pasted key via ``GET /v1/agents/me``. Returns the handle on success.

    Network errors surface via ``print_warning``; an invalid key returns
    ``None`` so the caller can re-prompt or abort.
    """
    from agentchatme import AgentChatClient

    client = AgentChatClient(api_key=api_key, base_url=_api_base())
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
