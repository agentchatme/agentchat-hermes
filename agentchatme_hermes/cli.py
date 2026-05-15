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
from .soul_anchor import AnchorError, remove_soul_anchor, write_soul_anchor

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

    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnose AgentChat configuration & connectivity",
    )
    p_doctor.set_defaults(func=_dispatch_doctor)


# ───────────────────────── dispatchers ─────────────────────────


def _dispatch_wizard(_args: argparse.Namespace) -> int:
    """No-subcommand entry → the interactive wizard.

    External UX (menus, prompts, styled text, arrow-key picker via
    ``prompt_choice``) is the same shape the 0.1.x line had — see
    :mod:`agentchatme_hermes.wizard` for the body and the rationale
    for re-using the engineered text verbatim. The named subcommands
    (``register``/``login``/``status``/``logout``) remain in this file
    as scriptable shortcuts; the wizard is the human-facing entry.
    """
    from .wizard import interactive_setup

    interactive_setup()
    return _EXIT_OK


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
        from agentchatme import AgentChatClient, AgentChatError
    except ImportError:
        return _exit_with(
            "The `agentchatme` SDK is not installed. Run "
            "`pip install agentchatme` and try again.",
        )

    from .wizard import _register_start, _RegisterError

    display_name = getattr(args, "display_name", None) or ""

    _printline(f"Sending verification code to {email}…")
    # Route through wizard._register_start so this path shares the same
    # null-field workaround for the SDK's strict-validation bug. Both
    # entry points use the same registration mechanics; the wizard adds
    # field-scoped retry logic on top, which the named subcommand doesn't
    # need (the operator either succeeds or runs the command again).
    try:
        pending_id = _register_start(
            email=email, handle=handle, display_name=display_name
        )
    except _RegisterError as exc:
        return _exit_with(f"Registration request failed: {exc}")

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
    _install_soul_anchor(handle)
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
    _install_soul_anchor(handle)
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


def _dispatch_doctor(_args: argparse.Namespace) -> int:
    """Health-check the plugin's configuration end-to-end.

    Surfaces in a single checklist:

    * env vars (api key, handle, api base)
    * SDK importability
    * API reachability + auth (``GET /v1/agents/me``)
    * Account-side flags that would silently suppress live delivery
      (paused-by-owner, inbox mode = strict, etc.)
    * Undelivered backlog (``GET /v1/messages/undelivered/count``)
    * SOUL.md anchor presence
    * Gateway process detection (informational only — the doctor
      itself is a CLI process, so we can only check whether *another*
      process matching ``hermes gateway`` is running)

    Exit code is the number of failed checks; ``0`` means clean.
    Each line is prefixed with ``[ok]`` / ``[warn]`` / ``[fail]``
    so the output is grep-friendly for CI / scripted setups.
    """
    failures = 0
    warnings_count = 0

    def ok(line: str) -> None:
        _printline(f"  [ok]   {line}")

    def warn(line: str) -> None:
        nonlocal warnings_count
        warnings_count += 1
        _printline(f"  [warn] {line}")

    def fail(line: str) -> None:
        nonlocal failures
        failures += 1
        _printline(f"  [fail] {line}")

    _printline("")
    _printline(f"AgentChat plugin doctor — agentchatme-hermes {__version__}")
    _printline("")

    saved_key = _read_saved_key()
    if saved_key:
        ok(f"AGENTCHATME_API_KEY set ({_mask_key(saved_key)})")
    else:
        fail(
            "AGENTCHATME_API_KEY not set — run "
            "`hermes agentchat register` or `hermes agentchat login`"
        )

    api_base = _api_base()
    if api_base == _DEFAULT_API_BASE:
        ok(f"API base: {api_base} (default)")
    else:
        ok(f"API base: {api_base} (overridden via AGENTCHATME_API_BASE)")

    import os

    saved_handle = os.environ.get(_ENV_HANDLE, "").strip()
    if saved_handle:
        ok(f"Local handle hint: @{saved_handle}")
    else:
        warn(
            "AGENTCHATME_HANDLE not set (informational — the runtime "
            "resolves the handle from /v1/agents/me at startup)"
        )

    try:
        from agentchatme import AgentChatClient, AgentChatError, UnauthorizedError
    except ImportError:
        fail(
            "agentchatme SDK is not importable — "
            "run `pip install agentchatme` or `uv pip install agentchatme`"
        )
        return _doctor_finalize(failures, warnings_count)

    ok("agentchatme SDK importable")

    if not saved_key:
        # No key to test connectivity with — skip the rest.
        return _doctor_finalize(failures, warnings_count)

    client = AgentChatClient(api_key=saved_key, base_url=api_base)
    try:
        try:
            me = client.get_me()
        except UnauthorizedError:
            fail(
                "API key did NOT authenticate against /v1/agents/me — "
                "rotate via `hermes agentchat login`"
            )
            return _doctor_finalize(failures, warnings_count)
        except AgentChatError as exc:
            fail(f"Could not reach /v1/agents/me: {exc}")
            return _doctor_finalize(failures, warnings_count)

        handle = me.get("handle") if isinstance(me, dict) else None
        if isinstance(handle, str) and handle:
            ok(f"Authenticated as @{handle}")
        else:
            fail("Server response missing handle — refusing to trust")
            return _doctor_finalize(failures, warnings_count)

        status = me.get("status") if isinstance(me, dict) else None
        if status == "active":
            ok(f"Account status: {status}")
        elif status:
            warn(f"Account status: {status} — may suppress live delivery")
        else:
            warn("Account status field missing from /v1/agents/me response")

        settings = me.get("settings") if isinstance(me, dict) else None
        if isinstance(settings, dict):
            inbox_mode = settings.get("inbox_mode")
            if inbox_mode == "open":
                ok(f"Inbox mode: {inbox_mode}")
            elif inbox_mode:
                warn(
                    f"Inbox mode: {inbox_mode} — non-contact senders "
                    "may be blocked. Adjust on the AgentChat dashboard "
                    "if you expect cold inbound."
                )

        paused = (
            me.get("paused_by_owner") if isinstance(me, dict) else None
        )
        if paused and paused != "none":
            warn(
                f"Account paused by owner ({paused}) — agent will "
                "not receive new inbound until un-paused"
            )

        # NOTE: deliberately do NOT call /v1/messages/sync from the
        # doctor. The gateway drains sync on every (re)connect, and a
        # doctor invocation while the gateway is up would race the
        # drain — the SDK has no peek/count variant, only "pull and
        # ack." We instead rely on the WSDaemon heartbeat in the
        # gateway log for queue-depth visibility.
    finally:
        client.close()

    try:
        from .soul_anchor import has_anchor

        if has_anchor():
            ok("SOUL.md identity anchor present")
        else:
            warn(
                "SOUL.md identity anchor missing — run "
                "`hermes agentchat login` (or re-register) to install it"
            )
    except ImportError:
        warn("soul_anchor module not importable — skipping anchor check")

    if _other_gateway_running():
        ok("Hermes gateway process detected (live inbound active)")
    else:
        warn(
            "No `hermes gateway` process detected — start one "
            "(`hermes gateway`) for live inbound delivery"
        )

    # Report the WS leader-lock state. The doctor itself never holds
    # the lock (it's a one-shot CLI invocation), so the meaningful
    # states are "held" (gateway running — good), "free" (no leader —
    # bad if a gateway should be running), or "not present" (file
    # never created — gateway hasn't run since last cleanup).
    try:
        from .leader_lock import default_lock_path, describe_lock_holder

        state = describe_lock_holder()
        lock_path = default_lock_path()
        if state == "held":
            ok(f"WS leader lock held ({lock_path})")
        elif state == "free":
            warn(
                f"WS leader lock file present but no holder ({lock_path}) — "
                "gateway is not running"
            )
        elif state == "not present":
            warn(
                f"WS leader lock file missing ({lock_path}) — gateway "
                "has not run since the last cleanup"
            )
        else:
            warn(f"WS leader lock state: {state}")
    except ImportError:
        warn("leader_lock module not importable — skipping lock check")

    return _doctor_finalize(failures, warnings_count)


def _doctor_finalize(failures: int, warnings_count: int) -> int:
    """Print the doctor footer and return the exit code.

    Exit code is the failure count so CI can gate on a clean
    doctor run. Warnings are surfaced but don't fail the check.
    """
    _printline("")
    if failures == 0 and warnings_count == 0:
        _printline("All checks passed.")
    elif failures == 0:
        _printline(f"{warnings_count} warning(s); no failures.")
    else:
        _printline(
            f"{failures} failure(s), {warnings_count} warning(s). "
            "Resolve the [fail] lines above."
        )
    _printline("")
    return failures


def _other_gateway_running() -> bool:
    """Best-effort: is a *different* ``hermes gateway`` process alive?

    The doctor itself is a CLI process, so we exclude our own pid.
    Cross-platform via ``psutil`` when available, falls back to
    ``False`` (informational warning) when not.
    """
    try:
        import os

        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return False

    own_pid = os.getpid()
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["pid"] == own_pid:
                    continue
                cmdline = proc.info.get("cmdline") or []
                if not cmdline:
                    continue
                joined = " ".join(str(arg) for arg in cmdline)
                if "hermes" in joined and "gateway" in cmdline:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        logger.debug("doctor: process iteration failed", exc_info=True)
    return False


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
    _uninstall_soul_anchor()
    _printline(
        "AgentChat key cleared from ~/.hermes/.env. Your account on the "
        "server is unchanged."
    )
    return _EXIT_OK


def _install_soul_anchor(handle: str) -> None:
    """Upsert the AgentChat identity block into SOUL.md.

    Failure is non-fatal: credentials are already persisted, the account
    is fully operational, and the anchor is the "subconscious identity
    everywhere" enhancement. Surface a clear warning so the operator can
    repair it later — mirrors the OpenClaw plugin's posture at
    ``channel.wizard.ts:749``.
    """
    try:
        path = write_soul_anchor(handle)
    except (AnchorError, OSError) as exc:
        sys.stderr.write(
            "Warning: failed to update ~/.hermes/SOUL.md with your "
            f"AgentChat identity. Your account is configured, but the "
            "agent will not be told about its handle outside of "
            "AgentChat-triggered turns until this is repaired. "
            f"(Reason: {exc})\n"
        )
        sys.stderr.flush()
        return
    _printline(f"Identity anchor written to {path}")


def _uninstall_soul_anchor() -> None:
    """Strip the AgentChat identity block from SOUL.md (idempotent)."""
    try:
        removed = remove_soul_anchor()
    except OSError as exc:
        sys.stderr.write(
            f"Warning: could not remove the AgentChat block from SOUL.md "
            f"({exc}). You can delete the block between "
            "`<!-- agentchat:start -->` and `<!-- agentchat:end -->` "
            "manually if needed.\n"
        )
        sys.stderr.flush()
        return
    if removed:
        _printline("Identity anchor removed from ~/.hermes/SOUL.md")


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
    display_name = me.get("display_name") or ""
    paused_by_owner = me.get("paused_by_owner") or "none"

    _printline("")
    _printline(f"  Handle:        @{handle}")
    if display_name:
        _printline(f"  Display name:  {display_name}")
    _printline(f"  Status:        {status}")
    _printline(f"  Inbox mode:    {inbox_mode}")
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
