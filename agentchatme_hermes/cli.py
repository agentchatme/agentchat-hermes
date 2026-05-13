"""`hermes agentchat [...]` CLI subcommand.

Surfaces:

* **``hermes agentchat``** (no subcommand) — registers a fresh
  agent via the interactive wizard if no key is configured, or
  shows status if one is.
* **``register``** — OTP registration: email → handle → 6-digit
  code → minted API key persisted to ``~/.hermes/.env``.
* **``login``** — paste an existing ``ac_live_…`` key. Validates
  via ``GET /v1/agents/me`` before persisting so we never save a
  key that doesn't authenticate.
* **``status``** — show the configured @handle and account state
  (restrictions, inbox mode, paused-by-owner, etc.).
* **``logout``** — wipe the saved key from ``~/.hermes/.env``.

The wizard is the only path users need. The named subcommands
exist so CI / power users can script the same operations.

Persistence: every successful flow writes through Hermes's
``save_env_value`` (``hermes_cli/config.py``), which mirrors the
gateway and auth flows so users get one well-known location for
their secrets.
"""
from __future__ import annotations

import logging
import re
import sys
from getpass import getpass
from typing import TYPE_CHECKING, Any, Callable

from ._version import __version__

if TYPE_CHECKING:
    import argparse

logger = logging.getLogger(__name__)

# Server-side regex (project_agentchat_handle_rules). Mirrored client-side
# so the user gets immediate feedback on bad input instead of an opaque
# 400 from the server.
_HANDLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Env var keys we persist. Keep in lockstep with the SDK / runtime —
# any rename here is a breaking change for users with a configured key.
_ENV_API_KEY = "AGENTCHATME_API_KEY"
_ENV_HANDLE = "AGENTCHATME_HANDLE"
_ENV_API_BASE = "AGENTCHATME_API_BASE"

_DEFAULT_API_BASE = "https://api.agentchat.me"

_EXIT_OK = 0
_EXIT_ARG_ERR = 2
_EXIT_USER_CANCEL = 130
_EXIT_API_ERR = 1


# ───────────────────────── argparse wiring ─────────────────────────


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    """Build the ``hermes agentchat`` subcommand tree.

    Called by Hermes once at CLI construction time with our scoped
    parser. We attach a sub-subparsers tree and set per-action
    defaults so argparse dispatches to the right handler.
    """
    parser.description = (
        "Manage your AgentChat identity. With no subcommand, launches "
        "the interactive register/status wizard. The named subcommands "
        "are scriptable equivalents. Configuration is persisted to "
        "~/.hermes/.env."
    )
    parser.set_defaults(func=_dispatch_wizard)

    sub = parser.add_subparsers(
        dest="action",
        metavar="<action>",
        help="Action to run. Omit to launch the interactive wizard.",
    )

    p_register = sub.add_parser(
        "register",
        help="Register a new AgentChat agent (email + OTP)",
    )
    p_register.add_argument("--email", help="Email address for OTP verification.")
    p_register.add_argument(
        "--handle",
        help="Desired @handle (3-30 chars, lowercase letters/digits/hyphens, must start with a letter).",
    )
    p_register.add_argument(
        "--display-name",
        dest="display_name",
        help="Optional display name shown next to your @handle.",
    )
    p_register.set_defaults(func=_dispatch_register)

    p_login = sub.add_parser(
        "login",
        help="Paste an existing AgentChat API key",
    )
    p_login.add_argument(
        "--api-key",
        dest="api_key",
        help="ac_live_… key. Prompts (masked input) if omitted.",
    )
    p_login.set_defaults(func=_dispatch_login)

    p_status = sub.add_parser(
        "status",
        help="Show the currently configured @handle and account state",
    )
    p_status.set_defaults(func=_dispatch_status)

    p_logout = sub.add_parser(
        "logout",
        help="Clear the saved AgentChat key from ~/.hermes/.env",
    )
    p_logout.set_defaults(func=_dispatch_logout)


# ───────────────────────── dispatchers ─────────────────────────


def _dispatch_wizard(_args: argparse.Namespace) -> int:
    """No-subcommand entry: register if unconfigured, otherwise status."""
    saved_key = _read_saved_key()
    if saved_key:
        _printline("AgentChat key already configured. Running status check.")
        return _dispatch_status(_args)
    _printline("Welcome to AgentChat. Let's register your agent.")
    return _dispatch_register(_args)


def _dispatch_register(args: argparse.Namespace) -> int:
    try:
        email = _prompt_email(getattr(args, "email", None))
        handle = _prompt_handle(getattr(args, "handle", None))
    except KeyboardInterrupt:
        return _exit_cancel()
    except _UserAbort:
        return _EXIT_USER_CANCEL

    api_base = _api_base()

    try:
        from agentchatme import (
            AgentChatClient,
            AgentChatError,
            ValidationError,
        )
    except ImportError:
        return _exit_with(
            "The `agentchatme` SDK is not installed. Run "
            "`pip install agentchatme` and try again.",
        )

    display_name = getattr(args, "display_name", None)

    _printline(f"Sending verification code to {email}…")
    try:
        register_resp = AgentChatClient.register(
            email=email,
            handle=handle,
            display_name=display_name,
            base_url=api_base,
        )
    except ValidationError as exc:
        return _exit_with(f"Server rejected the request: {exc}")
    except AgentChatError as exc:
        return _exit_with(f"Registration request failed: {exc}")

    pending_id = (
        register_resp.get("pending_id") if isinstance(register_resp, dict) else None
    )
    if not isinstance(pending_id, str):
        return _exit_with(
            "Registration server response did not include pending_id"
        )

    _printline("Check your email — a 6-digit code will arrive shortly.")
    try:
        code = _prompt_code()
    except KeyboardInterrupt:
        return _exit_cancel()
    except _UserAbort:
        return _EXIT_USER_CANCEL

    try:
        _agent, api_key, auth_client = AgentChatClient.verify(
            pending_id, code, base_url=api_base
        )
    except AgentChatError as exc:
        return _exit_with(f"Verification failed: {exc}")

    if not isinstance(api_key, str) or not api_key:
        return _exit_with(
            "Verification server response did not include an api_key"
        )

    # verify() hands back an authenticated AgentChatClient we never
    # use — close it so we don't leak the underlying httpx connection
    # pool. The plugin's long-lived clients are constructed lazily
    # inside the runtime once register persists the key.
    try:
        auth_client.close()
    except Exception:
        logger.debug("auth_client close raised", exc_info=True)

    _persist_credentials(api_key=api_key, handle=handle, api_base=api_base)
    _print_registration_success(handle=handle, api_key=api_key)
    return _EXIT_OK


def _dispatch_login(args: argparse.Namespace) -> int:
    raw_key = getattr(args, "api_key", None)
    if not raw_key:
        try:
            raw_key = _prompt_api_key()
        except KeyboardInterrupt:
            return _exit_cancel()
        except _UserAbort:
            return _EXIT_USER_CANCEL

    api_key = raw_key.strip()
    if not api_key:
        return _exit_with("No API key provided.")

    api_base = _api_base()

    try:
        from agentchatme import AgentChatClient, AgentChatError, UnauthorizedError
    except ImportError:
        return _exit_with(
            "The `agentchatme` SDK is not installed. Run "
            "`pip install agentchatme` and try again.",
        )

    client = AgentChatClient(api_key=api_key, base_url=api_base)
    try:
        try:
            me = client.get_me()
        except UnauthorizedError:
            return _exit_with(
                "That key did not authenticate against /v1/agents/me. "
                "Check that you copied the full ac_live_… key."
            )
        except AgentChatError as exc:
            return _exit_with(f"Key validation failed: {exc}")
    finally:
        client.close()

    handle = me.get("handle") if isinstance(me, dict) else None
    if not isinstance(handle, str):
        return _exit_with(
            "Server response missing handle — refusing to persist a key we "
            "can't identify."
        )

    _persist_credentials(api_key=api_key, handle=handle, api_base=api_base)
    _printline(f"Saved AgentChat key for @{handle}.")
    return _EXIT_OK


def _dispatch_status(_args: argparse.Namespace) -> int:
    saved_key = _read_saved_key()
    if not saved_key:
        _printline(
            "No AgentChat key configured. Run `hermes agentchat register` "
            "or `hermes agentchat login`."
        )
        return _EXIT_OK

    api_base = _api_base()

    try:
        from agentchatme import AgentChatClient, AgentChatError, UnauthorizedError
    except ImportError:
        return _exit_with(
            "The `agentchatme` SDK is not installed."
        )

    client = AgentChatClient(api_key=saved_key, base_url=api_base)
    try:
        try:
            me = client.get_me()
        except UnauthorizedError:
            return _exit_with(
                "Saved key no longer authenticates. Run "
                "`hermes agentchat login` with a fresh key."
            )
        except AgentChatError as exc:
            return _exit_with(f"Status fetch failed: {exc}")
    finally:
        client.close()

    _print_status(me)
    return _EXIT_OK


def _dispatch_logout(_args: argparse.Namespace) -> int:
    if not _read_saved_key():
        _printline("No AgentChat key was configured. Nothing to clear.")
        return _EXIT_OK

    try:
        from hermes_cli.config import save_env_value
    except ImportError:
        return _exit_with(
            "Cannot reach Hermes' env-config helper — is this running "
            "inside a Hermes install?"
        )

    save_env_value(_ENV_API_KEY, "")
    save_env_value(_ENV_HANDLE, "")
    _printline(
        "AgentChat key cleared from ~/.hermes/.env. Your account on the "
        "server is unchanged."
    )
    return _EXIT_OK


# ───────────────────────── prompts ─────────────────────────


class _UserAbort(Exception):
    """Raised when the user explicitly types 'q' / 'quit' / 'cancel'."""


def _prompt_email(provided: str | None) -> str:
    if provided:
        return _validate_email(provided)
    while True:
        raw = _input("Email address: ").strip()
        _check_abort(raw)
        try:
            return _validate_email(raw)
        except ValueError as exc:
            _printline(f"  {exc}")


def _prompt_handle(provided: str | None) -> str:
    if provided:
        return _validate_handle(provided)
    _printline(
        "Pick a handle (3-30 chars, lowercase letters/digits/hyphens, "
        "must start with a letter)."
    )
    while True:
        raw = _input("@").strip().lstrip("@").lower()
        _check_abort(raw)
        try:
            return _validate_handle(raw)
        except ValueError as exc:
            _printline(f"  {exc}")


def _prompt_code() -> str:
    while True:
        raw = _input("Verification code (6 digits): ").strip()
        _check_abort(raw)
        if re.fullmatch(r"\d{6}", raw):
            return raw
        _printline("  Code must be exactly 6 digits.")


def _prompt_api_key() -> str:
    return getpass("API key (ac_live_…): ").strip()


# ───────────────────────── validators ─────────────────────────


def _validate_email(value: str) -> str:
    value = value.strip()
    if not _EMAIL_RE.match(value):
        raise ValueError(f"{value!r} is not a valid email address.")
    if len(value) > 254:
        raise ValueError("Email is too long.")
    return value


def _validate_handle(value: str) -> str:
    if len(value) < 3 or len(value) > 30:
        raise ValueError("Handle must be 3-30 characters.")
    if not _HANDLE_RE.match(value):
        raise ValueError(
            "Handle must be lowercase letters/digits/hyphens, start with a "
            "letter, and not contain doubled or trailing hyphens."
        )
    return value


# ───────────────────────── helpers ─────────────────────────


def _read_saved_key() -> str | None:
    import os

    value = os.environ.get(_ENV_API_KEY, "").strip()
    return value or None


def _api_base() -> str:
    import os

    raw = os.environ.get(_ENV_API_BASE, "").strip()
    return raw.rstrip("/") if raw else _DEFAULT_API_BASE


def _persist_credentials(*, api_key: str, handle: str, api_base: str) -> None:
    """Write through Hermes's env-config so subsequent processes see it."""
    from hermes_cli.config import save_env_value

    save_env_value(_ENV_API_KEY, api_key)
    save_env_value(_ENV_HANDLE, handle)
    # Only persist the API base when the user overrode the default —
    # leaves the default unchanged for everyone else.
    if api_base != _DEFAULT_API_BASE:
        save_env_value(_ENV_API_BASE, api_base)


def _print_registration_success(*, handle: str, api_key: str) -> None:
    masked = _mask_key(api_key)
    _printline("")
    _printline(f"  Registered @{handle}")
    _printline(f"  Key:   {masked}   (stored in ~/.hermes/.env)")
    _printline("")
    _printline(
        "Restart Hermes (or any running `hermes gateway`) so the new key "
        "is picked up. Then your agent is on the network."
    )


def _print_status(me: dict[str, Any]) -> None:
    handle = me.get("handle", "unknown")
    status = me.get("status", "unknown")
    settings = me.get("settings") or {}
    inbox_mode = settings.get("inbox_mode", "unknown") if isinstance(settings, dict) else "unknown"
    discoverable = settings.get("discoverable", None) if isinstance(settings, dict) else None
    display_name = me.get("display_name") or ""
    paused_by_owner = me.get("paused_by_owner") or "none"

    _printline("")
    _printline(f"  Handle:        @{handle}")
    if display_name:
        _printline(f"  Display name:  {display_name}")
    _printline(f"  Status:        {status}")
    _printline(f"  Inbox mode:    {inbox_mode}")
    if discoverable is not None:
        _printline(f"  Discoverable:  {discoverable}")
    if paused_by_owner != "none":
        _printline(f"  Paused:        {paused_by_owner}  (owner-paused)")
    _printline(f"  Plugin:        agentchatme-hermes {__version__}")
    _printline("")


def _mask_key(key: str) -> str:
    if len(key) <= 12:
        return "ac_live_***"
    return f"{key[:11]}…{key[-4:]}"


# ───────────────────────── I/O glue ─────────────────────────


def _printline(message: str) -> None:
    """Send to stdout. Hermes captures stdout for the CLI surface."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _input(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise _UserAbort("EOF on stdin")
    return line.rstrip("\n")


def _check_abort(value: str) -> None:
    if value.lower() in {"q", "quit", "cancel", "exit"}:
        raise _UserAbort("user cancelled")


def _exit_with(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()
    return _EXIT_API_ERR


def _exit_cancel() -> int:
    sys.stderr.write("\nCancelled.\n")
    sys.stderr.flush()
    return _EXIT_USER_CANCEL


# ───────────────────────── Hermes plugin entry ─────────────────────────


def register_cli(ctx: Any) -> None:
    """Wire ``hermes agentchat …`` into Hermes' top-level CLI.

    Per ``hermes_cli/plugins.py:376-398`` the ``setup_fn`` receives a
    pre-built argparse subparser scoped under our subcommand and we
    attach our subtree there. ``handler_fn`` is set via per-action
    ``set_defaults(func=...)`` so this top-level handler only fires
    when no subcommand is given — it routes to the wizard.
    """
    ctx.register_cli_command(
        name="agentchat",
        help="Manage your AgentChat identity (register, login, status, logout)",
        setup_fn=_setup_with_signature(setup_argparse),
        handler_fn=_dispatch_top,
        description=(
            "Manage your AgentChat identity. Run with no subcommand for "
            "the interactive register/status wizard."
        ),
    )


def _dispatch_top(args: argparse.Namespace) -> int:
    """Top-level dispatcher when argparse sees no subcommand.

    Argparse's ``set_defaults(func=...)`` calls this with the parsed
    namespace; we fall through to whichever action's func was set, or
    the wizard if nothing matched.
    """
    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is _dispatch_top or func is None:
        return _dispatch_wizard(args)
    return func(args)


def _setup_with_signature(setup_fn: Callable[[argparse.ArgumentParser], None]) -> Callable[[argparse.ArgumentParser], None]:
    """Identity wrapper — placeholder for future shimming.

    Lets us interpose on the argparse wiring (e.g., to inject global
    flags, attach an environment-version banner) without rewriting
    callsites in the future. Right now it's a pass-through.
    """
    return setup_fn
